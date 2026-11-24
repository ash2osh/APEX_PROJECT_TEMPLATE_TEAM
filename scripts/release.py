#!/usr/bin/env python3
"""Lean 6-step release bundle CLI (list, new, collect, check, run).

Zero interactive prompts: safety is enforced through strict bundle validation,
target identity verification, patch base-hash checks, --confirm-target matching,
pre-apply target backups, and invalid-object delta checks.
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import groupby
from pathlib import Path
from typing import Any

from .db_targets import (
    ORACLE_IDENTIFIER,
    Target,
    TargetResolutionError,
    looks_like_production_identity,
    resolve_target,
)
from .local_config import ConfigError, read_project_env
from .path_utils import PathReparseError, relative_to_root
from .sqlcl_session import SqlclError, SqlclTimeout, bash_command, driver_epilogue, run_sqlcl


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = 1
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\Z", re.ASCII)
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z", re.ASCII)
SQL_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$#]*", re.ASCII)
PLSQL_CODE_TYPE_RE = re.compile(
    r"(?im)^[ \t]*ALTER[ \t]+SESSION[ \t]+SET[ \t]+PLSQL_CODE_TYPE[ \t]*=[ \t]*INTERPRETED[ \t]*;?[ \t]*\r?\n?"
)
PLSQL_CODE_TYPE_ANY_RE = re.compile(r"(?is)\bALTER\s+SESSION\s+SET\s+PLSQL_CODE_TYPE\s*=\s*INTERPRETED\b")
_TRIGGER_ENABLE_TAIL_RE = re.compile(
    r"(?is)\n/\s*\n[ \t]*ALTER[ \t]+TRIGGER\b[^\n;]*\bENABLE\s*;?\s*\Z"
)
_TRIGGER_DISABLE_TAIL_RE = re.compile(
    r"(?is)\n/\s*\n[ \t]*ALTER[ \t]+TRIGGER\b[^\n;]*\bDISABLE\s*;?\s*\Z"
)

CODE_MODES = frozenset({"new", "replace", "patch"})
ENVIRONMENTS = ("dev", "staging", "prod")
SUPPORTED_OBJECT_TYPES = {
    "PACKAGE": "PACKAGE",
    "PACKAGE_SPEC": "PACKAGE",
    "PACKAGE SPEC": "PACKAGE",
    "PACKAGE BODY": "PACKAGE BODY",
    "PACKAGE_BODY": "PACKAGE BODY",
    "PROCEDURE": "PROCEDURE",
    "FUNCTION": "FUNCTION",
    "TRIGGER": "TRIGGER",
    "VIEW": "VIEW",
    "TYPE": "TYPE",
    "TYPE_SPEC": "TYPE",
    "TYPE SPEC": "TYPE",
    "TYPE BODY": "TYPE BODY",
    "TYPE_BODY": "TYPE BODY",
}
SECTION_DIRS = (
    ("ddl", "01-ddl"),
    ("code", "02-code"),
    ("ords", "03-ords"),
    ("verify", "04-verify"),
)
_GIT_ENV_DROP = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE"})


class ReleaseError(RuntimeError):
    """Refusal or validation error during a release operation (exit 1)."""


class ReleaseUsageError(ValueError):
    """Usage or configuration error (exit 2)."""


@dataclass(frozen=True)
class BundleStep:
    section: str
    schema: str
    rel_file: str
    path: Path
    object_type: str | None = None
    object_name: str | None = None
    mode: str | None = None
    base_sha256: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _validate_slug(raw: str) -> str:
    if not isinstance(raw, str) or not raw or ".." in raw or raw.endswith("."):
        raise ReleaseUsageError(f"invalid release slug: {raw!r}")
    if SLUG_RE.fullmatch(raw) is None:
        raise ReleaseUsageError(f"release slug must match [a-z0-9][a-z0-9._-]*: {raw!r}")
    return raw


def _parse_bundle_slug(bundle: str) -> str:
    text = bundle.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("releases/"):
        text = text[len("releases/"):]
    text = text.rstrip("/")
    return _validate_slug(text)


def _normalize_object_spec(spec: str) -> tuple[str, str]:
    if not isinstance(spec, str) or ":" not in spec:
        raise ReleaseUsageError("--object must use TYPE:NAME (for example PACKAGE_BODY:ORDERS_PKG)")
    raw_type, raw_name = spec.split(":", 1)
    norm_type_key = " ".join(raw_type.strip().upper().replace("_", " ").split())
    object_type = SUPPORTED_OBJECT_TYPES.get(norm_type_key)
    if object_type is None:
        raise ReleaseUsageError(f"unsupported object type: {raw_type!r}")
    object_name = raw_name.strip().upper()
    if ORACLE_IDENTIFIER.fullmatch(object_name) is None:
        raise ReleaseUsageError(f"object name must be an uppercase Oracle identifier: {raw_name!r}")
    return object_type, object_name


def _validate_schema_name(schema: str) -> str:
    value = schema.strip().upper()
    if ORACLE_IDENTIFIER.fullmatch(value) is None:
        raise ReleaseUsageError(f"schema must be an uppercase Oracle identifier: {schema!r}")
    return value


def _write_json_lf(path: Path, payload: Mapping[str, Any]) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReleaseError(f"{label} is missing or not a regular file: {path}")
    try:
        raw = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReleaseError(f"cannot read {label}: {exc}") from exc
    if raw.startswith("\ufeff"):
        raise ReleaseError(f"{label} must not contain a UTF-8 BOM")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"{label} is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ReleaseError(f"{label} must be a JSON object")
    return data


def _safe_bundle_dir(repo_root: Path, slug: str) -> Path:
    releases_root = repo_root / "releases"
    candidate = releases_root / slug
    try:
        rel = relative_to_root(candidate, releases_root)
    except (PathReparseError, ValueError) as exc:
        raise ReleaseError(f"unsafe release bundle path for {slug!r}: {exc}") from exc
    if rel.as_posix() != slug:
        raise ReleaseError(f"unsafe release bundle path for {slug!r}")
    if candidate.is_symlink():
        raise ReleaseError(f"release bundle directory cannot be a symlink: {candidate}")
    return candidate


def _resolve_section_file(bundle_dir: Path, subdir: str, raw_file: str) -> tuple[str, Path]:
    if not isinstance(raw_file, str) or not raw_file.strip() or "\\" in raw_file or raw_file.startswith("/"):
        raise ReleaseError(f"invalid relative bundle file path: {raw_file!r}")
    norm = raw_file.strip()
    while norm.startswith("./"):
        norm = norm[2:]
    if norm.startswith(subdir + "/"):
        inner = norm[len(subdir) + 1 :]
    else:
        inner = norm
    if not inner or inner.startswith("/") or any(part in {"", ".", ".."} for part in inner.split("/")):
        raise ReleaseError(f"invalid file path {raw_file!r} under {subdir}/")
    section_root = bundle_dir / subdir
    candidate = section_root / inner
    try:
        rel = relative_to_root(candidate, section_root)
    except (PathReparseError, ValueError) as exc:
        raise ReleaseError(f"file path escapes {subdir}/: {raw_file!r}") from exc
    if candidate.is_symlink() or not candidate.is_file():
        raise ReleaseError(f"referenced bundle file is missing or not a regular file: {subdir}/{rel.as_posix()}")
    return f"{subdir}/{rel.as_posix()}", candidate


def strip_plsql_code_type(sql_text: str) -> str:
    """Remove ALTER SESSION SET PLSQL_CODE_TYPE = INTERPRETED lines for ADB compatibility."""
    return PLSQL_CODE_TYPE_RE.sub("", sql_text)


def normalize_source_for_hash(text: str) -> str:
    """Normalize Oracle ALL_SOURCE / DDL text deterministically for SHA-256 comparison."""
    cleaned = strip_plsql_code_type(text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [line.rstrip() for line in cleaned.split("\n")]
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    return ("\n".join(lines) + "\n") if lines else ""


def compute_source_sha256(text: str) -> str:
    return hashlib.sha256(normalize_source_for_hash(text).encode("utf-8")).hexdigest()


def _load_env(repo_root: Path, environ: Mapping[str, str] | None) -> dict[str, str]:
    if environ is not None:
        return dict(environ)
    env_override = os.environ.get("PROJECT_ENV_FILE")
    env_path = Path(env_override) if env_override else repo_root / ".env"
    if not env_path.is_file():
        raise ReleaseUsageError(f"configuration file not found: {env_path}")
    try:
        return read_project_env(env_path)
    except ConfigError as exc:
        raise ReleaseUsageError(str(exc)) from exc


def _resolve_schema_target(values: Mapping[str, str], environment: str, schema: str, *, operation: str = "migration") -> Target:
    try:
        return resolve_target(values, environment, operation, schema=schema)
    except TargetResolutionError as exc:
        raise ReleaseUsageError(str(exc)) from exc


def _load_and_validate_manifest(bundle_dir: Path, expected_slug: str) -> tuple[dict[str, Any], list[BundleStep], list[str], list[int], list[str]]:
    manifest_path = bundle_dir / "release.json"
    manifest = _read_json_object(manifest_path, f"releases/{expected_slug}/release.json")
    if type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != SCHEMA_VERSION:
        raise ReleaseError(f"release.json schemaVersion must be {SCHEMA_VERSION}")
    slug = manifest.get("slug")
    if not isinstance(slug, str) or slug != expected_slug:
        raise ReleaseError(f"release.json slug {slug!r} does not match directory {expected_slug!r}")
    status = manifest.get("status")
    if status not in {"draft", "deployed"}:
        raise ReleaseError(f"release.json status must be 'draft' or 'deployed', got {status!r}")

    declared_schemas: list[str] = []
    raw_schemas = manifest.get("schemas")
    if raw_schemas is not None:
        if not isinstance(raw_schemas, list):
            raise ReleaseError("release.json 'schemas' must be a list")
        for item in raw_schemas:
            if not isinstance(item, str) or ORACLE_IDENTIFIER.fullmatch(item) is None:
                raise ReleaseError(f"release.json contains invalid schema name: {item!r}")
            if item not in declared_schemas:
                declared_schemas.append(item)

    forbidden_patterns: list[str] = []
    raw_forbidden = manifest.get("forbiddenPatterns", [])
    if raw_forbidden is not None:
        if not isinstance(raw_forbidden, list):
            raise ReleaseError("release.json 'forbiddenPatterns' must be a list")
        for pat in raw_forbidden:
            if not isinstance(pat, str) or not pat.strip():
                raise ReleaseError("release.json 'forbiddenPatterns' entries must be non-empty strings")
            forbidden_patterns.append(pat.strip().upper())

    apps: list[int] = []
    raw_apps = manifest.get("apps", [])
    if raw_apps is not None:
        if not isinstance(raw_apps, list):
            raise ReleaseError("release.json 'apps' must be a list")
        for app_id in raw_apps:
            if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
                raise ReleaseError(f"release.json 'apps' must contain positive integer IDs, got {app_id!r}")
            if app_id not in apps:
                apps.append(app_id)

    steps: list[BundleStep] = []
    inferred_schemas: list[str] = list(declared_schemas)

    for section, subdir in SECTION_DIRS:
        entries = manifest.get(section, [])
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise ReleaseError(f"release.json '{section}' must be a list")
        for idx, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise ReleaseError(f"release.json '{section}[{idx}]' must be an object")
            schema = entry.get("schema")
            if not isinstance(schema, str) or ORACLE_IDENTIFIER.fullmatch(schema) is None:
                raise ReleaseError(f"release.json '{section}[{idx}].schema' must be an uppercase Oracle identifier")
            raw_file = entry.get("file")
            rel_file, file_path = _resolve_section_file(bundle_dir, subdir, str(raw_file or ""))
            if section == "code":
                raw_type = entry.get("objectType")
                raw_name = entry.get("objectName")
                mode = entry.get("mode")
                if not isinstance(raw_type, str) or raw_type.upper() not in set(SUPPORTED_OBJECT_TYPES.values()):
                    raise ReleaseError(f"release.json 'code[{idx}].objectType' is invalid: {raw_type!r}")
                if not isinstance(raw_name, str) or ORACLE_IDENTIFIER.fullmatch(raw_name) is None:
                    raise ReleaseError(f"release.json 'code[{idx}].objectName' is invalid: {raw_name!r}")
                if mode not in CODE_MODES:
                    raise ReleaseError(f"release.json 'code[{idx}].mode' must be new, replace, or patch")
                base_sha = entry.get("baseSha256")
                if mode == "patch":
                    if not isinstance(base_sha, str) or SHA256_RE.fullmatch(base_sha) is None:
                        raise ReleaseError(f"release.json 'code[{idx}]' mode 'patch' requires a 64-char hex baseSha256")
                elif base_sha is not None and (not isinstance(base_sha, str) or SHA256_RE.fullmatch(base_sha) is None):
                    raise ReleaseError(f"release.json 'code[{idx}].baseSha256' must be 64-char hex when present")
                steps.append(BundleStep(
                    section=section,
                    schema=schema,
                    rel_file=rel_file,
                    path=file_path,
                    object_type=raw_type.upper(),
                    object_name=raw_name,
                    mode=mode,
                    base_sha256=base_sha,
                ))
            else:
                steps.append(BundleStep(section=section, schema=schema, rel_file=rel_file, path=file_path))
            if schema not in inferred_schemas:
                inferred_schemas.append(schema)

    return manifest, steps, inferred_schemas, apps, forbidden_patterns


def _matches_pattern(candidate: str, pattern: str) -> bool:
    upper = candidate.upper()
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatchcase(upper, pattern)
    return upper.startswith(pattern)


def _check_bundle_contents(steps: Sequence[BundleStep], forbidden_patterns: Sequence[str]) -> None:
    for step in steps:
        try:
            sql_text = step.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ReleaseError(f"cannot read {step.rel_file} as UTF-8: {exc}") from exc
        if PLSQL_CODE_TYPE_ANY_RE.search(sql_text):
            raise ReleaseError(
                f"{step.rel_file} contains ALTER SESSION SET PLSQL_CODE_TYPE = INTERPRETED; "
                "strip it (or re-collect) so ADB non-ADMIN logins do not fail with ORA-01031"
            )
        if forbidden_patterns:
            name_candidates = [step.rel_file, step.path.name]
            if step.object_name:
                name_candidates.append(step.object_name)
            for pattern in forbidden_patterns:
                for cand in name_candidates:
                    if _matches_pattern(cand, pattern):
                        raise ReleaseError(f"forbidden pattern {pattern!r} matched {cand!r} in {step.rel_file}")
                for token in SQL_TOKEN_RE.findall(sql_text):
                    if _matches_pattern(token, pattern):
                        raise ReleaseError(f"forbidden pattern {pattern!r} matched SQL identifier {token!r} in {step.rel_file}")


def _extract_framed_json(output: str, marker: str) -> dict[str, Any]:
    begin = f"RELEASE_{marker}_BEGIN"
    end = f"RELEASE_{marker}_END"
    verified = f"RELEASE_{marker}_VERIFIED"
    lines = output.splitlines()
    try:
        start = lines.index(begin)
        finish = lines.index(end, start + 1)
    except ValueError as exc:
        raise ReleaseError(f"SQLcl output missing {begin}/{end} frame") from exc
    if verified not in lines[finish + 1 :]:
        raise ReleaseError(f"SQLcl output missing {verified} completion sentinel")
    raw_json = "".join(lines[start + 1 : finish])
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ReleaseError(f"SQLcl output {marker} JSON is malformed: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ReleaseError(f"SQLcl output {marker} payload must be a JSON object")
    return payload


def _verify_live_identity(identity: Mapping[str, Any], target: Target) -> dict[str, str]:
    fields = ("session_user", "current_schema", "db_name", "db_unique_name", "service_name", "con_name")
    observed = {k: str(identity.get(k, "")).strip() for k in fields}
    if not observed["session_user"] or not observed["db_unique_name"]:
        raise ReleaseError("live target identity is incomplete (missing session_user or db_unique_name)")
    if observed["session_user"].upper() != target.expected_user:
        raise ReleaseError(
            f"target {target.environment} expected session user {target.expected_user}, "
            f"observed {observed['session_user']}"
        )
    if observed["current_schema"].upper() != target.schema:
        raise ReleaseError(
            f"target {target.environment} expected current schema {target.schema}, "
            f"observed {observed['current_schema']}"
        )
    if target.environment != "prod" and looks_like_production_identity(
        observed["db_name"], observed["db_unique_name"], observed["service_name"]
    ):
        raise ReleaseError(
            f"observed database identity ({observed['db_unique_name']}) resembles production "
            f"but --env is {target.environment}"
        )
    return observed


def _plsql_emit_clob_lines(var_name: str, marker: str) -> list[str]:
    return [
        f"  DBMS_OUTPUT.PUT_LINE('RELEASE_{marker}_BEGIN');",
        "  DECLARE",
        "    l_offset PLS_INTEGER := 1;",
        "    l_chunk  VARCHAR2(32767);",
        "  BEGIN",
        f"    WHILE l_offset <= DBMS_LOB.GETLENGTH({var_name}) LOOP",
        f"      l_chunk := DBMS_LOB.SUBSTR({var_name}, 8000, l_offset);",
        "      DBMS_OUTPUT.PUT_LINE(l_chunk);",
        "      l_offset := l_offset + LENGTH2(l_chunk);",
        "    END LOOP;",
        "  END;",
        f"  DBMS_OUTPUT.PUT_LINE('RELEASE_{marker}_END');",
        f"  DBMS_OUTPUT.PUT_LINE('RELEASE_{marker}_VERIFIED');",
    ]


def _plsql_identity_lines(obj_var: str = "l_id") -> list[str]:
    return [
        f"  {obj_var}.put('session_user', SYS_CONTEXT('USERENV', 'SESSION_USER'));",
        f"  {obj_var}.put('current_schema', SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA'));",
        f"  {obj_var}.put('db_name', SYS_CONTEXT('USERENV', 'DB_NAME'));",
        f"  {obj_var}.put('db_unique_name', SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME'));",
        f"  {obj_var}.put('service_name', NVL(SYS_CONTEXT('USERENV', 'SERVICE_NAME'), '<NO_SERVICE>'));",
        f"  {obj_var}.put('con_name', NVL(SYS_CONTEXT('USERENV', 'CON_NAME'), 'NON-CDB'));",
    ]


def _render_fetch_objects_driver(
    target: Target,
    items: Sequence[tuple[str, str]],
    *,
    include_ddl: bool = False,
) -> str:
    """Render a read-only SQLcl driver fetching identity, ALL_OBJECTS existence, and source/DDL."""
    lines = [
        "SET ECHO OFF",
        "SET FEEDBACK OFF",
        "SET HEADING OFF",
        "SET VERIFY OFF",
        "SET SERVEROUTPUT ON SIZE UNLIMITED",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
    ]
    if target.acts_as_other_schema:
        lines.append(f"ALTER SESSION SET CURRENT_SCHEMA = {target.schema};")
    lines.extend([
        "SET TRANSACTION READ ONLY;",
        "DECLARE",
        "  l_payload JSON_OBJECT_T := JSON_OBJECT_T();",
        "  l_id      JSON_OBJECT_T := JSON_OBJECT_T();",
        "  l_items   JSON_ARRAY_T  := JSON_ARRAY_T();",
        "  l_item    JSON_OBJECT_T;",
        "  l_src     CLOB;",
        "  l_ddl     CLOB;",
        "  l_exists  NUMBER;",
        "  l_out     CLOB;",
        "BEGIN",
        *_plsql_identity_lines("l_id"),
        "  l_payload.put('identity', l_id);",
    ])
    for object_type, object_name in items:
        q_schema = _sql_literal(target.schema)
        q_type = _sql_literal(object_type)
        q_name = _sql_literal(object_name)
        meta_type = object_type.replace(" ", "_")
        q_meta_type = _sql_literal(meta_type)
        lines.extend([
            "  l_item := JSON_OBJECT_T();",
            f"  l_item.put('objectType', {q_type});",
            f"  l_item.put('objectName', {q_name});",
            "  SELECT COUNT(*) INTO l_exists FROM ALL_OBJECTS",
            f"   WHERE OWNER = {q_schema} AND OBJECT_TYPE = {q_type} AND OBJECT_NAME = {q_name};",
            "  l_item.put('exists', l_exists > 0);",
            "  l_src := EMPTY_CLOB();",
            "  DBMS_LOB.CREATETEMPORARY(l_src, TRUE);",
            "  FOR r IN (SELECT text FROM ALL_SOURCE",
            f"             WHERE OWNER = {q_schema} AND TYPE = {q_type} AND NAME = {q_name}",
            "             ORDER BY line) LOOP",
            "    DBMS_LOB.APPEND(l_src, TO_CLOB(r.text));",
            "  END LOOP;",
            "  l_item.put('source', l_src);",
            "  DBMS_LOB.FREETEMPORARY(l_src);",
        ])
        if include_ddl or object_type in {"VIEW", "TRIGGER"}:
            lines.extend([
                "  IF l_exists > 0 THEN",
                "    BEGIN",
                "      DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'SQLTERMINATOR', TRUE);",
                f"      l_ddl := DBMS_METADATA.GET_DDL({q_meta_type}, {q_name}, {q_schema});",
                "      l_item.put('ddl', l_ddl);",
                "    EXCEPTION WHEN OTHERS THEN",
                "      NULL;",
                "    END;",
                "  END IF;",
            ])
        lines.append("  l_items.append(l_item);")
    lines.extend([
        "  l_payload.put('objects', l_items);",
        "  l_out := l_payload.to_clob();",
        *_plsql_emit_clob_lines("l_out", "FETCH"),
        "END;",
        "/",
        driver_epilogue(False),
        "",
    ])
    return "\n".join(lines)


def _sqlcl_child_env(values: Mapping[str, str] | None) -> dict[str, str] | None:
    if not values:
        return None
    env = dict(os.environ)
    for key in ("SQLCL_PERSISTENT_SESSIONS", "SQLCL_POOL_MAX_READERS", "MIGRATION_APPLY_TIMEOUT_SECONDS", "MIGRATION_CHECK_TIMEOUT_SECONDS"):
        if key in values:
            env[key] = str(values[key])
    return env


def _preserve_sqlcl_failure_log(run_dir: Path, exc: BaseException) -> tuple[Path | None, str]:
    """Copy sqlcl-output.log out of TemporaryDirectory and extract key error lines."""
    log_src = run_dir / "sqlcl-output.log"
    output = getattr(exc, "output", None)
    if not isinstance(output, str) and log_src.is_file():
        output = log_src.read_text(encoding="utf-8", errors="replace")
    output_text = output if isinstance(output, str) else ""

    saved_log: Path | None = None
    scratch_root = run_dir.parent.parent
    if scratch_root.name == "scratch" and (log_src.is_file() or output_text):
        logs_dir = scratch_root / "release-logs"
        logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        saved_log = logs_dir / f"{_utc_stamp()}-{run_dir.name}-sqlcl-output.log"
        descriptor = os.open(saved_log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(output_text)

    snippet = ""
    if output_text.strip():
        lines = [line.rstrip() for line in output_text.splitlines() if line.strip()]
        interesting = [
            line for line in lines
            if re.search(r"(?:ORA-|PLS-|SP2-|Error starting at line|Error report)", line)
        ]
        excerpt = interesting[:6] if interesting else lines[-6:]
        snippet = "\n  " + "\n  ".join(excerpt)
    return saved_log, snippet


def _run_driver(
    runner: Callable,
    target: Target,
    run_dir: Path,
    driver_name: str,
    driver_sql: str,
    *,
    phase: str,
    environ: Mapping[str, str] | None = None,
) -> str:
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    driver = run_dir / driver_name
    driver.write_text(driver_sql, encoding="utf-8", newline="\n")
    try:
        driver.chmod(0o600)
    except OSError:
        pass
    try:
        result = runner(target, driver, run_dir, environment=_sqlcl_child_env(environ), phase=phase)
    except (SqlclError, SqlclTimeout) as exc:
        saved_log, snippet = _preserve_sqlcl_failure_log(run_dir, exc)
        log_ref = saved_log if saved_log is not None else (run_dir / "sqlcl-output.log")
        raise ReleaseError(f"{exc} (see {log_ref}){snippet}") from exc
    return result.output


def _fetch_schema_objects(
    runner: Callable,
    target: Target,
    run_dir: Path,
    items: Sequence[tuple[str, str]],
    *,
    include_ddl: bool = False,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], dict[tuple[str, str], dict[str, Any]]]:
    driver_sql = _render_fetch_objects_driver(target, items, include_ddl=include_ddl)
    output = _run_driver(runner, target, run_dir, "release-fetch.sql", driver_sql, phase="inventory", environ=environ)
    payload = _extract_framed_json(output, "FETCH")
    identity = _verify_live_identity(payload.get("identity") or {}, target)
    objects: dict[tuple[str, str], dict[str, Any]] = {}
    for item in payload.get("objects", []):
        if isinstance(item, dict):
            key = (str(item.get("objectType", "")).upper(), str(item.get("objectName", "")).upper())
            objects[key] = item
    return identity, objects


def _format_collected_sql(object_type: str, object_name: str, schema: str, item: Mapping[str, Any]) -> str:
    if object_type in {"VIEW", "TRIGGER"} and isinstance(item.get("ddl"), str) and item["ddl"].strip():
        text = strip_plsql_code_type(item["ddl"])
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if object_type == "TRIGGER":
            # DBMS_METADATA appends ALTER TRIGGER ... ENABLE; which fails ORA-01031
            # without ALTER ANY TRIGGER and caused a stray trailing slash after ';'.
            text = _TRIGGER_ENABLE_TAIL_RE.sub("\n/", text)
            if _TRIGGER_DISABLE_TAIL_RE.search(text):
                if not text.endswith(";"):
                    text += ";"
            elif not text.endswith("/"):
                text += "\n/"
        elif object_type == "VIEW" and not text.endswith((";", "/")):
            text += ";"
        return text + "\n"
    src = item.get("source")
    if not isinstance(src, str) or not src.strip():
        raise ReleaseError(f"{schema}.{object_name} ({object_type}) was not found or has empty source")
    cleaned = strip_plsql_code_type(src.replace("\r\n", "\n").replace("\r", "\n")).rstrip()
    if not re.match(r"(?is)^\s*CREATE\b", cleaned):
        cleaned = "CREATE OR REPLACE " + cleaned.lstrip()
    if object_type in {"PACKAGE", "PACKAGE BODY", "PROCEDURE", "FUNCTION", "TRIGGER", "TYPE", "TYPE BODY"}:
        if not cleaned.rstrip().endswith("/"):
            cleaned += "\n/"
    elif not cleaned.rstrip().endswith(";"):
        cleaned += ";"
    return cleaned + "\n"


def _source_text_for_hash(object_type: str, item: Mapping[str, Any]) -> str:
    src = item.get("source")
    if isinstance(src, str) and src.strip():
        return src
    ddl = item.get("ddl")
    if isinstance(ddl, str) and ddl.strip():
        return ddl
    return ""


def _default_code_filename(object_type: str, object_name: str) -> str:
    type_slug = object_type.lower().replace(" ", "_")
    return f"{object_name.lower()}_{type_slug}.sql"


def _git_head_sha(repo_root: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return "unknown"


def _git_create_release_tag(repo_root: Path, slug: str) -> str:
    tag = f"release/{slug}"
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    try:
        check_repo = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--git-dir"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
            timeout=10,
        )
        if check_repo.returncode != 0:
            return "skipped (not a Git repository)"
        exists = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
            timeout=10,
        )
        if exists.returncode == 0:
            return f"already exists ({tag})"
        created = subprocess.run(
            ["git", "-C", str(repo_root), "tag", tag],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
            timeout=10,
        )
        if created.returncode != 0:
            detail = created.stderr.strip() or f"exit {created.returncode}"
            raise ReleaseError(f"failed to create Git tag {tag}: {detail}")
        return f"created ({tag})"
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseError(f"Git tag check/creation failed: {exc}") from exc


# --- Subcommands -------------------------------------------------------------


def cmd_list(repo_root: Path, fmt: str) -> int:
    releases_dir = repo_root / "releases"
    drafts: list[dict[str, Any]] = []
    deployed: list[dict[str, Any]] = []
    if releases_dir.is_dir():
        for child in sorted(releases_dir.iterdir(), key=lambda p: p.name):
            if not child.is_dir() or child.is_symlink():
                continue
            manifest_path = child / "release.json"
            if not manifest_path.is_file():
                continue
            data = _read_json_object(manifest_path, f"releases/{child.name}/release.json")
            entry = {
                "slug": data.get("slug", child.name),
                "status": data.get("status", "unknown"),
                "createdAt": data.get("createdAt"),
                "deployedAt": data.get("deployedAt"),
                "deployedTarget": data.get("deployedTarget"),
                "deployedCommit": data.get("deployedCommit"),
                "schemas": data.get("schemas", []),
                "counts": {
                    "ddl": len(data.get("ddl") or []),
                    "code": len(data.get("code") or []),
                    "ords": len(data.get("ords") or []),
                    "verify": len(data.get("verify") or []),
                    "apps": len(data.get("apps") or []),
                },
            }
            if entry["status"] == "deployed":
                deployed.append(entry)
            else:
                drafts.append(entry)
    if fmt == "json":
        print(json.dumps({"schemaVersion": SCHEMA_VERSION, "draft": drafts, "deployed": deployed}, indent=2))
        return 0
    print("Open draft releases:")
    if drafts:
        for index, item in enumerate(drafts, start=1):
            schemas = ", ".join(item["schemas"]) if item["schemas"] else "-"
            counts = item["counts"]
            print(
                f"  {index}- {item['slug']} (schemas: {schemas}; "
                f"ddl={counts['ddl']}, code={counts['code']}, ords={counts['ords']}, verify={counts['verify']}, apps={counts['apps']})"
            )
    else:
        print("  (none)")
    print("Deployed (locked) releases:")
    if deployed:
        for item in deployed:
            print(f"  - {item['slug']} (deployedAt={item['deployedAt'] or '-'}, target={item['deployedTarget'] or '-'})")
    else:
        print("  (none)")
    return 0


def cmd_new(repo_root: Path, raw_slug: str) -> int:
    slug = _validate_slug(raw_slug)
    bundle_dir = _safe_bundle_dir(repo_root, slug)
    if bundle_dir.exists():
        raise ReleaseError(f"release bundle already exists: releases/{slug}")
    bundle_dir.mkdir(parents=True, exist_ok=False)
    for _, subdir in SECTION_DIRS:
        (bundle_dir / subdir).mkdir(parents=True, exist_ok=True)
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "slug": slug,
        "status": "draft",
        "createdAt": _utc_now(),
        "schemas": [],
        "ddl": [],
        "code": [],
        "ords": [],
        "verify": [],
        "apps": [],
        "forbiddenPatterns": [],
    }
    _write_json_lf(bundle_dir / "release.json", manifest)
    print(f"Created draft release bundle: releases/{slug}")
    return 0


def cmd_collect(
    repo_root: Path,
    bundle_arg: str,
    *,
    from_env: str,
    schema_arg: str,
    object_spec: str,
    mode: str,
    target_env: str | None,
    file_arg: str | None,
    environ: Mapping[str, str] | None,
    runner: Callable,
) -> int:
    slug = _parse_bundle_slug(bundle_arg)
    bundle_dir = _safe_bundle_dir(repo_root, slug)
    if not bundle_dir.is_dir():
        raise ReleaseError(f"release bundle not found: releases/{slug}")
    manifest_path = bundle_dir / "release.json"
    manifest = _read_json_object(manifest_path, f"releases/{slug}/release.json")
    if manifest.get("status") == "deployed":
        raise ReleaseError(f"release bundle '{slug}' is deployed and locked")

    schema = _validate_schema_name(schema_arg)
    object_type, object_name = _normalize_object_spec(object_spec)
    if mode not in CODE_MODES:
        raise ReleaseUsageError(f"unsupported mode {mode!r}; use new, replace, or patch")
    if mode == "patch" and not target_env:
        raise ReleaseUsageError("--mode patch requires --target-env <dev|staging|prod> to compute baseSha256")

    values = _load_env(repo_root, environ)
    source_target = _resolve_schema_target(values, from_env, schema, operation="read")

    scratch_root = repo_root / "scratch"
    scratch_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-collect-", dir=scratch_root) as tmp_dir:
        tmp_path = Path(tmp_dir)
        _, fetched = _fetch_schema_objects(
            runner,
            source_target,
            tmp_path / "source",
            [(object_type, object_name)],
            include_ddl=object_type in {"VIEW", "TRIGGER"},
            environ=values,
        )
        item = fetched.get((object_type, object_name))
        if not item or item.get("exists") is not True:
            raise ReleaseError(f"{schema}.{object_name} ({object_type}) does not exist in {from_env}")
        sql_text = _format_collected_sql(object_type, object_name, schema, item)

        base_sha256: str | None = None
        if mode == "patch":
            assert target_env is not None
            patch_target = _resolve_schema_target(values, target_env, schema, operation="migration")
            _, target_fetched = _fetch_schema_objects(
                runner,
                patch_target,
                tmp_path / "target",
                [(object_type, object_name)],
                include_ddl=object_type in {"VIEW", "TRIGGER"},
                environ=values,
            )
            target_item = target_fetched.get((object_type, object_name))
            if not target_item or target_item.get("exists") is not True:
                raise ReleaseError(f"{schema}.{object_name} ({object_type}) does not exist in {target_env} for patch")
            target_source = _source_text_for_hash(object_type, target_item)
            if not target_source.strip():
                raise ReleaseError(f"{schema}.{object_name} ({object_type}) returned empty source in {target_env}")
            base_sha256 = compute_source_sha256(target_source)

    code_dir = bundle_dir / "02-code"
    code_dir.mkdir(parents=True, exist_ok=True)
    raw_rel = file_arg if file_arg else _default_code_filename(object_type, object_name)
    norm_rel = raw_rel.replace("\\", "/").strip()
    while norm_rel.startswith("./"):
        norm_rel = norm_rel[2:]
    if norm_rel.startswith("02-code/"):
        norm_rel = norm_rel[len("02-code/") :]
    if not norm_rel or norm_rel.startswith("/") or any(p in {"", ".", ".."} for p in norm_rel.split("/")):
        raise ReleaseUsageError(f"invalid --file path under 02-code/: {raw_rel!r}")
    dest_path = code_dir / norm_rel
    try:
        checked_rel = relative_to_root(dest_path, code_dir)
    except (PathReparseError, ValueError) as exc:
        raise ReleaseUsageError(f"invalid --file path under 02-code/: {exc}") from exc
    if dest_path.is_symlink():
        raise ReleaseError(f"destination cannot be a symlink: {dest_path}")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(sql_text, encoding="utf-8", newline="\n")

    rel_entry_file = f"02-code/{checked_rel.as_posix()}"
    new_entry: dict[str, Any] = {
        "schema": schema,
        "file": rel_entry_file,
        "objectType": object_type,
        "objectName": object_name,
        "mode": mode,
    }
    if base_sha256 is not None:
        new_entry["baseSha256"] = base_sha256

    code_list = list(manifest.get("code") or [])
    replaced = False
    for idx, existing in enumerate(code_list):
        if (
            isinstance(existing, dict)
            and existing.get("schema") == schema
            and existing.get("objectType") == object_type
            and existing.get("objectName") == object_name
        ):
            code_list[idx] = new_entry
            replaced = True
            break
    if not replaced:
        code_list.append(new_entry)
    manifest["code"] = code_list
    schemas = list(manifest.get("schemas") or [])
    if schema not in schemas:
        schemas.append(schema)
    manifest["schemas"] = schemas
    _write_json_lf(manifest_path, manifest)

    msg = f"Collected {schema}.{object_name} ({object_type}, mode={mode}) -> releases/{slug}/{rel_entry_file}"
    if base_sha256:
        msg += f" (baseSha256={base_sha256[:12]}...)"
    print(msg)
    return 0


def _preflight_bundle(
    repo_root: Path,
    slug: str,
    env: str,
    *,
    allow_existing: bool,
    confirm_target: str | None,
    values: Mapping[str, str],
    runner: Callable,
    work_dir: Path,
) -> tuple[dict[str, Any], list[BundleStep], list[str], list[int], dict[str, Target], dict[str, dict[str, str]], dict[str, dict[tuple[str, str], dict[str, Any]]], list[str]]:
    bundle_dir = _safe_bundle_dir(repo_root, slug)
    if not bundle_dir.is_dir():
        raise ReleaseError(f"release bundle not found: releases/{slug}")
    manifest, steps, schemas, apps, forbidden_patterns = _load_and_validate_manifest(bundle_dir, slug)
    if manifest.get("status") == "deployed":
        raise ReleaseError(f"release bundle '{slug}' is already deployed and locked")
    _check_bundle_contents(steps, forbidden_patterns)

    targets: dict[str, Target] = {}
    identities: dict[str, dict[str, str]] = {}
    fetched_by_schema: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    failures: list[str] = []

    for schema in schemas:
        targets[schema] = _resolve_schema_target(values, env, schema, operation="migration")

    for schema in schemas:
        target = targets[schema]
        schema_code_steps = [s for s in steps if s.schema == schema and s.section == "code"]
        items = list(dict.fromkeys((s.object_type, s.object_name) for s in schema_code_steps if s.object_type and s.object_name))
        need_ddl = any(s.object_type in {"VIEW", "TRIGGER"} for s in schema_code_steps)
        identity, fetched = _fetch_schema_objects(
            runner,
            target,
            work_dir / f"preflight-{schema}",
            items,
            include_ddl=need_ddl,
            environ=values,
        )
        identities[schema] = identity
        fetched_by_schema[schema] = fetched

        if confirm_target is not None:
            if identity["db_unique_name"].casefold() != confirm_target.strip().casefold():
                raise ReleaseError(
                    f"--confirm-target {confirm_target!r} does not match live DB_UNIQUE_NAME "
                    f"{identity['db_unique_name']!r} for schema {schema}"
                )

        for step in schema_code_steps:
            assert step.object_type and step.object_name
            obj = fetched.get((step.object_type, step.object_name), {})
            exists = obj.get("exists") is True
            if step.mode == "new" and exists and not allow_existing:
                failures.append(
                    f"{schema}.{step.object_name} ({step.object_type}) already exists in {env} "
                    f"(pass --allow-existing or change mode)"
                )
            elif step.mode == "patch":
                assert step.base_sha256 is not None
                if not exists:
                    failures.append(f"{schema}.{step.object_name} ({step.object_type}) does not exist in {env} for patch")
                    continue
                live_text = _source_text_for_hash(step.object_type, obj)
                live_sha = compute_source_sha256(live_text)
                if live_sha != step.base_sha256:
                    diff_dir = repo_root / "scratch" / "release-diffs"
                    diff_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                    type_slug = step.object_type.lower().replace(" ", "_")
                    diff_path = diff_dir / f"{slug}-{env}-{schema}-{type_slug}-{step.object_name}.diff"
                    proposed_text = step.path.read_text(encoding="utf-8")
                    diff_lines = difflib.unified_diff(
                        normalize_source_for_hash(live_text).splitlines(keepends=True),
                        normalize_source_for_hash(proposed_text).splitlines(keepends=True),
                        fromfile=f"live:{env}/{schema}.{step.object_name}",
                        tofile=f"bundle:{step.rel_file}",
                    )
                    diff_path.write_text("".join(diff_lines) or "(source whitespace/content differs)\n", encoding="utf-8", newline="\n")
                    failures.append(
                        f"patch baseSha256 mismatch for {schema}.{step.object_name} ({step.object_type}): "
                        f"expected {step.base_sha256[:12]}..., live {live_sha[:12]}... (diff: {diff_path.relative_to(repo_root).as_posix()})"
                    )

    return manifest, steps, schemas, apps, targets, identities, fetched_by_schema, failures


def cmd_check(
    repo_root: Path,
    bundle_arg: str,
    *,
    env: str,
    fmt: str,
    allow_existing: bool,
    environ: Mapping[str, str] | None,
    runner: Callable,
) -> int:
    slug = _parse_bundle_slug(bundle_arg)
    values = _load_env(repo_root, environ)
    scratch_root = repo_root / "scratch"
    scratch_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-check-", dir=scratch_root) as tmp_dir:
        _, steps, schemas, apps, targets, identities, _, failures = _preflight_bundle(
            repo_root,
            slug,
            env,
            allow_existing=allow_existing,
            confirm_target=None,
            values=values,
            runner=runner,
            work_dir=Path(tmp_dir),
        )
    plan = {
        "schemaVersion": SCHEMA_VERSION,
        "slug": slug,
        "environment": env,
        "passed": not failures,
        "schemas": schemas,
        "targets": {
            schema: {
                "connection": targets[schema].connection,
                "expectedUser": targets[schema].expected_user,
                "schema": targets[schema].schema,
                "identity": identities[schema],
            }
            for schema in schemas
        },
        "steps": [
            {
                "section": s.section,
                "schema": s.schema,
                "file": s.rel_file,
                **({"objectType": s.object_type, "objectName": s.object_name, "mode": s.mode} if s.section == "code" else {}),
            }
            for s in steps
        ],
        "apps": apps,
        "failures": failures,
    }
    if fmt == "json":
        print(json.dumps(plan, indent=2))
    else:
        print(f"Release check: {slug} -> {env}")
        for schema in schemas:
            ident = identities[schema]
            print(
                f"  Target {schema}: DB_UNIQUE_NAME={ident['db_unique_name']} "
                f"CON_NAME={ident['con_name']} USER={ident['session_user']}"
            )
        print(f"  Execution plan: {len(steps)} SQL step(s), {len(apps)} app deploy(s)")
        for s in steps:
            extra = f" [{s.mode} {s.object_type}:{s.object_name}]" if s.section == "code" else ""
            print(f"    - {s.schema}: {s.rel_file}{extra}")
        for app_id in apps:
            print(f"    - APEX app deploy: {app_id}")
        if failures:
            print("  FAILURES:")
            for msg in failures:
                print(f"    * {msg}")
        else:
            print("  Preflight checks passed.")
    return 1 if failures else 0


def _render_backup_driver(target: Target, existing_items: Sequence[tuple[str, str]]) -> str:
    return _render_fetch_objects_driver(target, existing_items, include_ddl=True)


def _apply_session_prelude(target: Target) -> list[str]:
    lines = [
        "SET ECHO OFF",
        "SET FEEDBACK OFF",
        "SET HEADING OFF",
        "SET VERIFY OFF",
        "SET SQLBLANKLINES ON",
        "SET SERVEROUTPUT ON SIZE UNLIMITED",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
    ]
    if target.acts_as_other_schema:
        lines.append(f"ALTER SESSION SET CURRENT_SCHEMA = {target.schema};")
    return lines


def _before_invalids_lines(target: Target) -> list[str]:
    q_schema = _sql_literal(target.schema)
    return [
        "DECLARE",
        "  l_payload JSON_OBJECT_T := JSON_OBJECT_T();",
        "  l_id      JSON_OBJECT_T := JSON_OBJECT_T();",
        "  l_arr     JSON_ARRAY_T  := JSON_ARRAY_T();",
        "  l_obj     JSON_OBJECT_T;",
        "  l_out     CLOB;",
        "BEGIN",
        *_plsql_identity_lines("l_id"),
        "  l_payload.put('identity', l_id);",
        f"  FOR r IN (SELECT object_type, object_name FROM ALL_OBJECTS WHERE owner = {q_schema} AND status <> 'VALID' ORDER BY object_type, object_name) LOOP",
        "    l_obj := JSON_OBJECT_T();",
        "    l_obj.put('objectType', r.object_type);",
        "    l_obj.put('objectName', r.object_name);",
        "    l_arr.append(l_obj);",
        "  END LOOP;",
        "  l_payload.put('invalids', l_arr);",
        "  l_out := l_payload.to_clob();",
        *_plsql_emit_clob_lines("l_out", "BEFORE_INVALID"),
        "END;",
        "/",
    ]


def _sql_step_lines(section_steps: Sequence[BundleStep]) -> list[str]:
    lines: list[str] = []
    for step in section_steps:
        text = step.path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        lines.extend([
            "SET DEFINE OFF",
            "SET SQLBLANKLINES ON",
            "WHENEVER SQLERROR EXIT SQL.SQLCODE",
            f"PROMPT RELEASE_STEP_START:{step.rel_file}",
            text.rstrip("\n"),
            f"PROMPT RELEASE_STEP_END:{step.rel_file}",
        ])
    return lines


def _post_apply_lines(target: Target, code_steps: Sequence[BundleStep]) -> list[str]:
    q_schema = _sql_literal(target.schema)
    lines = [
        "SET DEFINE OFF",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "DECLARE",
        "  l_payload JSON_OBJECT_T := JSON_OBJECT_T();",
        "  l_errs    JSON_ARRAY_T  := JSON_ARRAY_T();",
        "  l_invals  JSON_ARRAY_T  := JSON_ARRAY_T();",
        "  l_row     JSON_OBJECT_T;",
        "  l_out     CLOB;",
        "BEGIN",
    ]
    unique_code = list(dict.fromkeys((s.object_type, s.object_name) for s in code_steps if s.object_type and s.object_name))
    if unique_code:
        pairs = ", ".join(f"({_sql_literal(t)}, {_sql_literal(n)})" for t, n in unique_code)
        lines.extend([
            "  FOR r IN (SELECT type, name, line, position, text FROM ALL_ERRORS",
            f"             WHERE owner = {q_schema} AND (type, name) IN ({pairs})",
            "             ORDER BY type, name, sequence) LOOP",
            "    l_row := JSON_OBJECT_T();",
            "    l_row.put('objectType', r.type);",
            "    l_row.put('objectName', r.name);",
            "    l_row.put('line', r.line);",
            "    l_row.put('position', r.position);",
            "    l_row.put('text', r.text);",
            "    l_errs.append(l_row);",
            "  END LOOP;",
        ])
    lines.extend([
        f"  FOR r IN (SELECT object_type, object_name FROM ALL_OBJECTS WHERE owner = {q_schema} AND status <> 'VALID' ORDER BY object_type, object_name) LOOP",
        "    l_row := JSON_OBJECT_T();",
        "    l_row.put('objectType', r.object_type);",
        "    l_row.put('objectName', r.object_name);",
        "    l_invals.append(l_row);",
        "  END LOOP;",
        "  l_payload.put('errors', l_errs);",
        "  l_payload.put('invalids', l_invals);",
        "  l_out := l_payload.to_clob();",
        *_plsql_emit_clob_lines("l_out", "POST_APPLY"),
        "END;",
        "/",
    ])
    return lines


def _render_apply_driver(target: Target, schema_steps: Sequence[BundleStep]) -> str:
    """Render 1 SQLcl driver for single-schema bundles: before-invalids, 01-ddl -> 02-code -> 03-ords, ALL_ERRORS, after-invalids."""
    ddl_steps = [s for s in schema_steps if s.section == "ddl"]
    code_steps = [s for s in schema_steps if s.section == "code"]
    ords_steps = [s for s in schema_steps if s.section == "ords"]

    lines = _apply_session_prelude(target)
    lines.extend(_before_invalids_lines(target))
    if ddl_steps:
        lines.extend(_sql_step_lines(ddl_steps))
    if code_steps:
        lines.extend(_sql_step_lines(code_steps))
    if ords_steps:
        lines.extend(_sql_step_lines(ords_steps))
    lines.extend(_post_apply_lines(target, code_steps))
    lines.extend([driver_epilogue(True), ""])
    return "\n".join(lines)


def _render_before_invalids_driver(target: Target) -> str:
    """Render read-only invalid-object baseline driver for multi-schema bundles."""
    lines = _apply_session_prelude(target)
    lines.append("SET TRANSACTION READ ONLY;")
    lines.extend(_before_invalids_lines(target))
    lines.extend([driver_epilogue(False), ""])
    return "\n".join(lines)


def _render_batch_apply_driver(target: Target, batch_steps: Sequence[BundleStep]) -> str:
    """Render 1 SQLcl apply driver for one consecutive schema batch within a phase."""
    lines = _apply_session_prelude(target)
    lines.extend(_sql_step_lines(batch_steps))
    lines.extend(["PROMPT RELEASE_BATCH_APPLIED", driver_epilogue(True), ""])
    return "\n".join(lines)


def _render_post_apply_driver(target: Target, code_steps: Sequence[BundleStep]) -> str:
    """Render read-only ALL_ERRORS and after-invalids check driver for multi-schema bundles."""
    lines = _apply_session_prelude(target)
    lines.append("SET TRANSACTION READ ONLY;")
    lines.extend(_post_apply_lines(target, code_steps))
    lines.extend([driver_epilogue(False), ""])
    return "\n".join(lines)


def _render_verify_driver(target: Target, verify_steps: Sequence[BundleStep]) -> str:
    lines = [
        "SET ECHO OFF",
        "SET FEEDBACK OFF",
        "SET HEADING OFF",
        "SET VERIFY OFF",
        "SET SQLBLANKLINES ON",
        "SET SERVEROUTPUT ON SIZE UNLIMITED",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
    ]
    if target.acts_as_other_schema:
        lines.append(f"ALTER SESSION SET CURRENT_SCHEMA = {target.schema};")
    lines.append("SET TRANSACTION READ ONLY;")
    for step in verify_steps:
        text = step.path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        lines.extend([
            "SET DEFINE OFF",
            f"PROMPT RELEASE_VERIFY_START:{step.rel_file}",
            text.rstrip("\n"),
            f"PROMPT RELEASE_VERIFY_END:{step.rel_file}",
        ])
    lines.extend(["PROMPT RELEASE_VERIFY_OK", driver_epilogue(False), ""])
    return "\n".join(lines)


def _invalid_set(items: Sequence[Mapping[str, Any]]) -> set[tuple[str, str]]:
    return {(str(i.get("objectType", "")).upper(), str(i.get("objectName", "")).upper()) for i in items}


def _invoke_app_deploy(
    repo_root: Path,
    app_id: int,
    env: str,
    confirm_target: str,
    values: Mapping[str, str],
) -> None:
    deploy_sh = repo_root / "scripts" / "deploy.sh"
    if not deploy_sh.is_file():
        raise ReleaseError(f"scripts/deploy.sh not found; cannot deploy APEX app {app_id}")
    child_env = {**os.environ, **{k: str(v) for k, v in values.items()}}
    cmd = [
        bash_command(),
        deploy_sh.as_posix(),
        str(app_id),
        "--env",
        env,
        "--yes",
        "--confirm-target",
        confirm_target,
    ]
    proc = subprocess.run(
        cmd,
        cwd=repo_root,
        env=child_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise ReleaseError(f"APEX app {app_id} deploy failed: {detail}")


def cmd_run(
    repo_root: Path,
    bundle_arg: str,
    *,
    env: str,
    confirm_target: str,
    allow_existing: bool,
    skip_backup: bool,
    report_arg: str | None,
    environ: Mapping[str, str] | None,
    runner: Callable,
    app_deployer: Callable[[Path, int, str, str, Mapping[str, str]], None] = _invoke_app_deploy,
) -> int:
    if not isinstance(confirm_target, str) or not confirm_target.strip():
        raise ReleaseUsageError("--confirm-target <db_unique_name> is required and must be non-empty")
    slug = _parse_bundle_slug(bundle_arg)
    values = _load_env(repo_root, environ)
    scratch_root = repo_root / "scratch"
    scratch_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = _utc_stamp()
    timings: dict[str, float] = {}

    with tempfile.TemporaryDirectory(prefix="release-run-", dir=scratch_root) as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Step 1-2: Preflight + target confirmation
        t0 = time.monotonic()
        manifest, steps, schemas, apps, targets, identities, fetched_by_schema, failures = _preflight_bundle(
            repo_root,
            slug,
            env,
            allow_existing=allow_existing,
            confirm_target=confirm_target,
            values=values,
            runner=runner,
            work_dir=tmp_path / "preflight",
        )
        timings["preflight"] = round(time.monotonic() - t0, 3)
        if failures:
            raise ReleaseError("Preflight refused:\n  - " + "\n  - ".join(failures))

        # Step 3: Backup existing target objects
        t1 = time.monotonic()
        backup_label = "skipped"
        if not skip_backup:
            backup_dir = scratch_root / "backups" / f"{slug}-{env}-{stamp}"
            backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            backup_label = backup_dir.relative_to(repo_root).as_posix()
            backed_up: list[dict[str, str]] = []
            for schema in schemas:
                existing_items = [
                    key
                    for key, obj in fetched_by_schema[schema].items()
                    if obj.get("exists") is True
                ]
                if not existing_items:
                    continue
                _, backed = _fetch_schema_objects(
                    runner,
                    targets[schema],
                    tmp_path / f"backup-{schema}",
                    existing_items,
                    include_ddl=True,
                    environ=values,
                )
                for (obj_type, obj_name), obj in backed.items():
                    content = obj.get("ddl") or obj.get("source") or ""
                    if not isinstance(content, str) or not content.strip():
                        continue
                    fname = f"{schema}.{obj_type.replace(' ', '_')}.{obj_name}.sql"
                    (backup_dir / fname).write_text(content.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
                    backed_up.append({"schema": schema, "objectType": obj_type, "objectName": obj_name, "file": fname})
            _write_json_lf(backup_dir / "backup-manifest.json", {"slug": slug, "environment": env, "objects": backed_up})
        timings["backup"] = round(time.monotonic() - t1, 3)

        # Step 4: Execute all 01-ddl -> all 02-code -> all 03-ords (consecutive schema batches) + invalid delta
        t2 = time.monotonic()
        before_invalids: dict[str, set[tuple[str, str]]] = {schema: set() for schema in schemas}
        after_invalids: dict[str, set[tuple[str, str]]] = {schema: set() for schema in schemas}
        new_invalids: dict[str, list[tuple[str, str]]] = {schema: [] for schema in schemas}
        apply_schemas = [
            schema for schema in schemas
            if any(s.schema == schema and s.section in {"ddl", "code", "ords"} for s in steps)
        ]

        def _check_post_apply(schema: str, post_payload: Mapping[str, Any]) -> None:
            code_errors = post_payload.get("errors") or []
            if code_errors:
                formatted_errs = [
                    f"{e.get('objectType')} {e.get('objectName')} L{e.get('line')}:{e.get('position')}: {e.get('text')}"
                    for e in code_errors
                ]
                raise ReleaseError(f"Compilation errors in {schema}:\n  - " + "\n  - ".join(formatted_errs))

            before_set = before_invalids[schema]
            after_set = _invalid_set(post_payload.get("invalids") or [])
            delta = sorted(after_set - before_set)
            after_invalids[schema] = after_set
            new_invalids[schema] = delta
            if delta:
                names = ", ".join(f"{t} {n}" for t, n in delta)
                raise ReleaseError(f"New invalid objects appeared in {schema}: {names}")

        if len(apply_schemas) == 1:
            schema = apply_schemas[0]
            schema_apply_steps = [s for s in steps if s.schema == schema and s.section in {"ddl", "code", "ords"}]
            driver_sql = _render_apply_driver(targets[schema], schema_apply_steps)
            output = _run_driver(
                runner,
                targets[schema],
                tmp_path / f"apply-{schema}",
                "release-apply.sql",
                driver_sql,
                phase="apply",
                environ=values,
            )
            before_payload = _extract_framed_json(output, "BEFORE_INVALID")
            _verify_live_identity(before_payload.get("identity") or {}, targets[schema])
            before_invalids[schema] = _invalid_set(before_payload.get("invalids") or [])
            post_payload = _extract_framed_json(output, "POST_APPLY")
            _check_post_apply(schema, post_payload)
        elif len(apply_schemas) > 1:
            # 4a. Capture before invalid-object baselines for all touched schemas before any DDL runs.
            for schema in apply_schemas:
                driver_sql = _render_before_invalids_driver(targets[schema])
                output = _run_driver(
                    runner,
                    targets[schema],
                    tmp_path / f"before-invalids-{schema}",
                    "release-before-invalids.sql",
                    driver_sql,
                    phase="preconditions",
                    environ=values,
                )
                before_payload = _extract_framed_json(output, "BEFORE_INVALID")
                _verify_live_identity(before_payload.get("identity") or {}, targets[schema])
                before_invalids[schema] = _invalid_set(before_payload.get("invalids") or [])

            # 4b. Execute strictly by phase (ddl -> code -> ords), grouping consecutive same-schema steps.
            batch_num = 0
            for section in ("ddl", "code", "ords"):
                phase_steps = [s for s in steps if s.section == section]
                for batch_schema, batch_iter in groupby(phase_steps, key=lambda s: s.schema):
                    batch_num += 1
                    batch_steps = list(batch_iter)
                    driver_sql = _render_batch_apply_driver(targets[batch_schema], batch_steps)
                    _run_driver(
                        runner,
                        targets[batch_schema],
                        tmp_path / f"apply-{batch_num:02d}-{section}-{batch_schema}",
                        f"release-apply-{section}-{batch_schema}.sql",
                        driver_sql,
                        phase="apply",
                        environ=values,
                    )

            # 4c. After all apply phases finish, check ALL_ERRORS and invalid-object delta per schema.
            for schema in apply_schemas:
                schema_code_steps = [s for s in steps if s.schema == schema and s.section == "code"]
                driver_sql = _render_post_apply_driver(targets[schema], schema_code_steps)
                output = _run_driver(
                    runner,
                    targets[schema],
                    tmp_path / f"post-apply-{schema}",
                    "release-post-apply.sql",
                    driver_sql,
                    phase="postconditions",
                    environ=values,
                )
                post_payload = _extract_framed_json(output, "POST_APPLY")
                _check_post_apply(schema, post_payload)
        timings["apply"] = round(time.monotonic() - t2, 3)

        # Step 4b: 04-verify smoke scripts (read-only)
        t3 = time.monotonic()
        for schema in schemas:
            verify_steps = [s for s in steps if s.schema == schema and s.section == "verify"]
            if not verify_steps:
                continue
            driver_sql = _render_verify_driver(targets[schema], verify_steps)
            output = _run_driver(
                runner,
                targets[schema],
                tmp_path / f"verify-{schema}",
                "release-verify.sql",
                driver_sql,
                phase="postconditions",
                environ=values,
            )
            if "RELEASE_VERIFY_OK" not in output.splitlines():
                raise ReleaseError(f"04-verify smoke scripts did not complete cleanly for {schema}")
        timings["verify"] = round(time.monotonic() - t3, 3)

        # Step 5: Optional APEX app deploy
        t4 = time.monotonic()
        deployed_apps: list[int] = []
        for app_id in apps:
            app_deployer(repo_root, app_id, env, confirm_target, values)
            deployed_apps.append(app_id)
        timings["apps"] = round(time.monotonic() - t4, 3)

    # Step 6: Report & Prod lock
    commit_sha = _git_head_sha(repo_root)
    tag_status = "not-applicable"
    if env == "prod":
        bundle_dir = _safe_bundle_dir(repo_root, slug)
        observed_db = next(iter(identities.values()))["db_unique_name"] if identities else confirm_target
        manifest["status"] = "deployed"
        manifest["deployedAt"] = _utc_now()
        manifest["deployedTarget"] = observed_db
        manifest["deployedCommit"] = commit_sha
        _write_json_lf(bundle_dir / "release.json", manifest)
        tag_status = _git_create_release_tag(repo_root, slug)

    if report_arg:
        report_path = Path(report_arg)
        if not report_path.is_absolute():
            report_path = repo_root / report_path
    else:
        report_path = scratch_root / "release-reports" / f"{slug}-{env}-{stamp}.md"
    report_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    report_lines = [
        f"# Release Report: {slug} ({env})",
        "",
        f"- **Status:** {manifest.get('status')}",
        f"- **Commit:** `{commit_sha}`",
        f"- **Backup:** `{backup_label}`",
        f"- **Git tag:** `{tag_status}`",
        f"- **Apps deployed:** {', '.join(str(a) for a in deployed_apps) if deployed_apps else 'none'}",
        "",
        "## Target Identities",
        "",
    ]
    for schema in schemas:
        ident = identities[schema]
        report_lines.append(
            f"- `{schema}`: `DB_UNIQUE_NAME={ident['db_unique_name']}`, `CON_NAME={ident['con_name']}`, "
            f"`USER={ident['session_user']}`, before_invalid={len(before_invalids[schema])}, "
            f"after_invalid={len(after_invalids[schema])}, new_invalid={len(new_invalids[schema])}"
        )
    report_lines.extend([
        "",
        "## Phase Timings (seconds)",
        "",
        *(f"- `{phase}`: {secs:.3f}s" for phase, secs in timings.items()),
        "",
    ])
    report_path.write_text("\n".join(report_lines), encoding="utf-8", newline="\n")
    print(f"Release {slug} -> {env} succeeded. Report: {report_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/team.sh release",
        description="Lean 6-step release bundle workflow (list, new, collect, check, run).",
    )
    parser.add_argument("--repo-root", type=Path, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="subcommand", required=True, metavar="SUBCOMMAND")

    p_list = sub.add_parser("list", help="List open draft and deployed release bundles (offline)")
    p_list.add_argument("--format", choices=("text", "json"), default="text")

    p_new = sub.add_parser("new", help="Create a new draft release bundle")
    p_new.add_argument("slug", help="Lowercase slug matching [a-z0-9][a-z0-9._-]*")

    p_collect = sub.add_parser("collect", help="Collect an object from an environment into 02-code/")
    p_collect.add_argument("bundle", help="Release slug or releases/<slug>")
    p_collect.add_argument("--from", dest="from_env", choices=ENVIRONMENTS, default="dev")
    p_collect.add_argument("--schema", required=True, help="Oracle schema name")
    p_collect.add_argument("--object", dest="object_spec", required=True, help="TYPE:NAME (e.g. PACKAGE_BODY:ORDERS_PKG)")
    p_collect.add_argument("--mode", choices=sorted(CODE_MODES), required=True)
    p_collect.add_argument("--target-env", choices=ENVIRONMENTS, help="Target environment for --mode patch baseSha256")
    p_collect.add_argument("--file", dest="file_arg", help="Optional relative file path under 02-code/")

    p_check = sub.add_parser("check", help="Read-only preflight check against target environment")
    p_check.add_argument("bundle", help="Release slug or releases/<slug>")
    p_check.add_argument("--env", choices=ENVIRONMENTS, required=True)
    p_check.add_argument("--format", choices=("text", "json"), default="text")
    p_check.add_argument("--allow-existing", action="store_true")

    p_run = sub.add_parser("run", help="Execute a release bundle (zero prompts; requires --confirm-target)")
    p_run.add_argument("bundle", help="Release slug or releases/<slug>")
    p_run.add_argument("--env", choices=ENVIRONMENTS, required=True)
    p_run.add_argument("--confirm-target", required=True, help="Live DB_UNIQUE_NAME of target database")
    p_run.add_argument("--allow-existing", action="store_true")
    p_run.add_argument("--skip-backup", action="store_true")
    p_run.add_argument("--report", dest="report_arg", help="Optional custom report path")

    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    runner: Callable = run_sqlcl,
    app_deployer: Callable[[Path, int, str, str, Mapping[str, str]], None] = _invoke_app_deploy,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo_root = (args.repo_root or ROOT).resolve()
    try:
        if args.subcommand == "list":
            return cmd_list(repo_root, args.format)
        if args.subcommand == "new":
            return cmd_new(repo_root, args.slug)
        if args.subcommand == "collect":
            return cmd_collect(
                repo_root,
                args.bundle,
                from_env=args.from_env,
                schema_arg=args.schema,
                object_spec=args.object_spec,
                mode=args.mode,
                target_env=args.target_env,
                file_arg=args.file_arg,
                environ=environ,
                runner=runner,
            )
        if args.subcommand == "check":
            return cmd_check(
                repo_root,
                args.bundle,
                env=args.env,
                fmt=args.format,
                allow_existing=args.allow_existing,
                environ=environ,
                runner=runner,
            )
        if args.subcommand == "run":
            return cmd_run(
                repo_root,
                args.bundle,
                env=args.env,
                confirm_target=args.confirm_target,
                allow_existing=args.allow_existing,
                skip_backup=args.skip_backup,
                report_arg=args.report_arg,
                environ=environ,
                runner=runner,
                app_deployer=app_deployer,
            )
        raise ReleaseUsageError(f"unknown subcommand: {args.subcommand}")
    except ReleaseUsageError as exc:
        print(f"release error: {exc}", file=sys.stderr)
        return 2
    except ReleaseError as exc:
        print(f"release refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
