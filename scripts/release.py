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
BACKUP_OBJECT_TYPES = {
    **SUPPORTED_OBJECT_TYPES,
    "TABLE": "TABLE",
    "INDEX": "INDEX",
    "SEQUENCE": "SEQUENCE",
    "SYNONYM": "SYNONYM",
    "MATERIALIZED VIEW": "MATERIALIZED VIEW",
    "MATERIALIZED_VIEW": "MATERIALIZED VIEW",
}
_DDL_IDENT_PART = r'(?:"[A-Za-z0-9_$#]+"|[A-Za-z][A-Za-z0-9_$#]*)'
_DDL_QUALIFIED_IDENT = rf"({_DDL_IDENT_PART})(?:\s*\.\s*({_DDL_IDENT_PART}))?"
_SQL_COMMENT_RE = re.compile(r"/\*.*?\*/|--[^\r\n]*", re.DOTALL)
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
    objects: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PreflightResult:
    manifest: dict[str, Any]
    steps: list[BundleStep]
    schemas: list[str]
    apps: list[int]
    targets: dict[str, Target]
    identities: dict[str, dict[str, str]]
    fetched_by_schema: dict[str, dict[tuple[str, str], dict[str, Any]]]
    failures: list[str]
    pinned_step_texts: dict[str, str] | None
    ords_backup_coverage: str
    unbacked_ords_steps: list[str]


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


def _normalize_object_spec(
    spec: str,
    *,
    allowed_types: Mapping[str, str] = SUPPORTED_OBJECT_TYPES,
) -> tuple[str, str]:
    if not isinstance(spec, str) or ":" not in spec:
        raise ReleaseUsageError("--object must use TYPE:NAME (for example PACKAGE_BODY:ORDERS_PKG)")
    raw_type, raw_name = spec.split(":", 1)
    norm_type_key = " ".join(raw_type.strip().upper().replace("_", " ").split())
    object_type = allowed_types.get(norm_type_key)
    if object_type is None:
        raise ReleaseUsageError(f"unsupported object type: {raw_type!r}")
    object_name = raw_name.strip().upper()
    if ORACLE_IDENTIFIER.fullmatch(object_name) is None:
        raise ReleaseUsageError(f"object name must be an uppercase Oracle identifier: {raw_name!r}")
    return object_type, object_name


def _parse_step_objects(raw_objects: Any, *, label: str) -> tuple[tuple[str, str], ...]:
    if raw_objects is None:
        return ()
    if not isinstance(raw_objects, list):
        raise ReleaseError(f"{label} must be a list")
    parsed: list[tuple[str, str]] = []
    for item_idx, item in enumerate(raw_objects):
        if isinstance(item, str):
            try:
                pair = _normalize_object_spec(item, allowed_types=BACKUP_OBJECT_TYPES)
            except ReleaseUsageError as exc:
                raise ReleaseError(f"{label}[{item_idx}]: {exc}") from exc
        elif isinstance(item, dict):
            raw_type = item.get("objectType")
            raw_name = item.get("objectName")
            if not isinstance(raw_type, str):
                raise ReleaseError(f"{label}[{item_idx}].objectType is invalid: {raw_type!r}")
            norm_type_key = " ".join(raw_type.strip().upper().replace("_", " ").split())
            object_type = BACKUP_OBJECT_TYPES.get(norm_type_key)
            if object_type is None:
                raise ReleaseError(f"{label}[{item_idx}].objectType is unsupported: {raw_type!r}")
            if not isinstance(raw_name, str):
                raise ReleaseError(f"{label}[{item_idx}].objectName is invalid: {raw_name!r}")
            object_name = raw_name.strip().upper()
            if ORACLE_IDENTIFIER.fullmatch(object_name) is None:
                raise ReleaseError(f"{label}[{item_idx}].objectName must be an uppercase Oracle identifier: {raw_name!r}")
            pair = (object_type, object_name)
        else:
            raise ReleaseError(f"{label}[{item_idx}] must be an object or 'TYPE:NAME' string")
        if pair not in parsed:
            parsed.append(pair)
    return tuple(parsed)


def _clean_ddl_ident(raw: str | None) -> str | None:
    if not raw:
        return None
    val = raw.strip().strip('"').strip().upper()
    return val if ORACLE_IDENTIFIER.fullmatch(val) is not None else None


def _split_ddl_schema_obj(first: str, second: str | None, default_schema: str) -> tuple[str, str] | None:
    if second is not None:
        schema = _clean_ddl_ident(first)
        obj = _clean_ddl_ident(second)
    else:
        schema = default_schema
        obj = _clean_ddl_ident(first)
    if schema is None or obj is None:
        return None
    return schema, obj


def _extract_ddl_candidate_objects(sql_text: str, default_schema: str) -> list[tuple[str, str, str]]:
    """Extract candidate existing object references from 01-ddl/ SQL for pre-apply backup."""
    cleaned = _SQL_COMMENT_RE.sub(" ", sql_text)
    candidates: list[tuple[str, str, str]] = []

    def _add(schema: str, obj_type: str, obj_name: str) -> None:
        item = (schema, obj_type, obj_name)
        if item not in candidates:
            candidates.append(item)

    for match in re.finditer(
        rf"(?is)\bALTER\s+(TABLE|VIEW|TRIGGER|INDEX|SEQUENCE|PACKAGE|PROCEDURE|FUNCTION|TYPE|MATERIALIZED\s+VIEW)\s+{_DDL_QUALIFIED_IDENT}",
        cleaned,
    ):
        obj_type = " ".join(match.group(1).upper().split())
        split = _split_ddl_schema_obj(match.group(2), match.group(3), default_schema)
        if split:
            _add(split[0], obj_type, split[1])

    for match in re.finditer(
        rf"(?is)\bDROP\s+(PACKAGE\s+BODY|TYPE\s+BODY|MATERIALIZED\s+VIEW|TABLE|TRIGGER|VIEW|INDEX|SEQUENCE|SYNONYM|PROCEDURE|FUNCTION|PACKAGE|TYPE)\s+(?:IF\s+EXISTS\s+)?{_DDL_QUALIFIED_IDENT}",
        cleaned,
    ):
        obj_type = " ".join(match.group(1).upper().split())
        split = _split_ddl_schema_obj(match.group(2), match.group(3), default_schema)
        if split:
            _add(split[0], obj_type, split[1])

    for match in re.finditer(
        rf"(?is)\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:FORCE|NOFORCE|EDITIONABLE|NONEDITIONABLE|UNIQUE|BITMAP|GLOBAL\s+TEMPORARY|PRIVATE)\s+)*(PACKAGE\s+BODY|TYPE\s+BODY|MATERIALIZED\s+VIEW|VIEW|TRIGGER|INDEX|SEQUENCE|SYNONYM|PROCEDURE|FUNCTION|PACKAGE|TYPE|TABLE)\s+(?:IF\s+NOT\s+EXISTS\s+)?{_DDL_QUALIFIED_IDENT}",
        cleaned,
    ):
        obj_type = " ".join(match.group(1).upper().split())
        split = _split_ddl_schema_obj(match.group(2), match.group(3), default_schema)
        if split:
            _add(split[0], obj_type, split[1])

    for match in re.finditer(
        rf"(?is)\bGRANT\s+([^;]+?)\s+ON\s+(?!DIRECTORY\b){_DDL_QUALIFIED_IDENT}\s+TO\b",
        cleaned,
    ):
        privs = match.group(1).upper()
        split = _split_ddl_schema_obj(match.group(2), match.group(3), default_schema)
        if not split:
            continue
        has_exec = "EXECUTE" in privs
        has_table_priv = any(k in privs for k in ("SELECT", "INSERT", "UPDATE", "DELETE", "ALTER", "INDEX", "REFERENCES", "READ", "WRITE"))
        if has_exec and not has_table_priv:
            cand_types = ("PACKAGE", "PROCEDURE", "FUNCTION", "TYPE")
        elif has_exec:
            cand_types = ("TABLE", "VIEW", "SEQUENCE", "MATERIALIZED VIEW", "PACKAGE", "PROCEDURE", "FUNCTION", "TYPE")
        else:
            cand_types = ("TABLE", "VIEW", "SEQUENCE", "MATERIALIZED VIEW")
        for cand_type in cand_types:
            _add(split[0], cand_type, split[1])

    return candidates


def _validate_schema_name(schema: str) -> str:
    value = schema.strip().upper()
    if ORACLE_IDENTIFIER.fullmatch(value) is None:
        raise ReleaseUsageError(f"schema must be an uppercase Oracle identifier: {schema!r}")
    return value


def _write_json_lf(path: Path, payload: Mapping[str, Any]) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def _parse_json_object_bytes(raw_bytes: bytes, label: str) -> dict[str, Any]:
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
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


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReleaseError(f"{label} is missing or not a regular file: {path}")
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ReleaseError(f"cannot read {label}: {exc}") from exc
    return _parse_json_object_bytes(raw_bytes, label)


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


def _resolve_section_file(
    bundle_dir: Path,
    subdir: str,
    raw_file: str,
    *,
    require_on_disk: bool = True,
) -> tuple[str, Path]:
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
    if candidate.is_symlink():
        raise ReleaseError(f"referenced bundle file cannot be a symlink: {subdir}/{rel.as_posix()}")
    if require_on_disk and not candidate.is_file():
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


def _resolve_env_file(repo_root: Path, environ: Mapping[str, str] | None) -> Path | None:
    if environ is not None:
        return None
    env_override = os.environ.get("PROJECT_ENV_FILE")
    return Path(env_override) if env_override else repo_root / ".env"


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


def _load_and_validate_manifest(
    bundle_dir: Path,
    expected_slug: str,
    *,
    manifest_override: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[BundleStep], list[str], list[int], list[str]]:
    manifest_path = bundle_dir / "release.json"
    if manifest_override is not None:
        manifest = manifest_override
        require_on_disk = False
    else:
        manifest = _read_json_object(manifest_path, f"releases/{expected_slug}/release.json")
        require_on_disk = True
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
    seen_files: set[str] = set()

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
            rel_file, file_path = _resolve_section_file(bundle_dir, subdir, str(raw_file or ""), require_on_disk=require_on_disk)
            if rel_file in seen_files:
                raise ReleaseError(f"duplicate bundle file reference in release.json: {rel_file}")
            seen_files.add(rel_file)
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
                step_objects: tuple[tuple[str, str], ...] = ()
                if section in {"ddl", "ords"} and "objects" in entry:
                    step_objects = _parse_step_objects(entry.get("objects"), label=f"release.json '{section}[{idx}].objects'")
                steps.append(BundleStep(
                    section=section,
                    schema=schema,
                    rel_file=rel_file,
                    path=file_path,
                    objects=step_objects,
                ))
            if schema not in inferred_schemas:
                inferred_schemas.append(schema)

    return manifest, steps, inferred_schemas, apps, forbidden_patterns


def _matches_pattern(candidate: str, pattern: str) -> bool:
    upper = candidate.upper()
    if any(ch in pattern for ch in "*?["):
        return fnmatch.fnmatchcase(upper, pattern)
    return upper.startswith(pattern)


def _check_bundle_contents(
    steps: Sequence[BundleStep],
    forbidden_patterns: Sequence[str],
    *,
    step_texts: Mapping[str, str] | None = None,
) -> None:
    for step in steps:
        if step_texts is not None:
            if step.rel_file not in step_texts:
                raise ReleaseError(f"missing commit-pinned content for {step.rel_file}")
            sql_text = step_texts[step.rel_file]
        else:
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


def _assert_matching_session_identity(
    observed: Mapping[str, str],
    expected: Mapping[str, str],
    *,
    schema: str,
    phase: str,
    confirm_target: str | None = None,
) -> None:
    for key, label in (
        ("db_unique_name", "DB_UNIQUE_NAME"),
        ("con_name", "CON_NAME"),
        ("session_user", "USER"),
        ("current_schema", "CURRENT_SCHEMA"),
    ):
        obs_val = str(observed.get(key, "")).strip()
        exp_val = str(expected.get(key, "")).strip()
        if obs_val.upper() != exp_val.upper():
            raise ReleaseError(
                f"Target {label} mismatch in {phase} session for schema {schema} "
                f"(expected {exp_val!r}, observed {obs_val!r})"
            )
    if confirm_target is not None:
        obs_db = str(observed.get("db_unique_name", "")).strip()
        if obs_db.casefold() != confirm_target.strip().casefold():
            raise ReleaseError(
                f"--confirm-target {confirm_target!r} does not match {phase} session DB_UNIQUE_NAME "
                f"{obs_db!r} for schema {schema}"
            )


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
                "      l_item.put('ddlError', SQLERRM);",
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


def _default_code_filename(schema: str, object_type: str, object_name: str) -> str:
    type_slug = object_type.lower().replace(" ", "_")
    return f"{schema.lower()}_{object_name.lower()}_{type_slug}.sql"


def _git_run(repo_root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    return subprocess.run(
        ["git", "-C", str(repo_root), "-c", "core.quotepath=false", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        env=env,
        check=False,
        timeout=10,
    )


def _is_git_repo(repo_root: Path) -> bool:
    try:
        return _git_run(repo_root, "rev-parse", "--git-dir").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _git_head_sha(repo_root: Path) -> str:
    try:
        proc = _git_run(repo_root, "rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    out = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode == 0 and out:
        return out
    return "unknown"


def _git_cat_file_blob(repo_root: Path, commit: str, repo_rel: str) -> bytes:
    proc = _git_run(repo_root, "cat-file", "blob", f"{commit}:{repo_rel}")
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or f"exit {proc.returncode}"
        raise ReleaseError(f"cannot read {repo_rel} from Git commit {commit[:12]}: {detail}")
    return proc.stdout


def _git_ignored_bundle_paths(repo_root: Path, slug: str, rel_paths: Sequence[str] = ()) -> list[str]:
    ignored: list[str] = []
    status_proc = _git_run(
        repo_root,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignored=matching",
        "--",
        f"releases/{slug}",
    )
    if status_proc.returncode == 0:
        for line in status_proc.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("!! "):
                path_text = line[3:].strip()
                if path_text and path_text not in ignored:
                    ignored.append(path_text)
    for rel_path in rel_paths:
        chk = _git_run(repo_root, "check-ignore", "-q", "--no-index", "--", rel_path)
        if chk.returncode == 0 and rel_path not in ignored:
            ignored.append(rel_path)
    return ignored


def _verify_prod_git_clean(repo_root: Path, slug: str) -> str:
    try:
        if not _is_git_repo(repo_root):
            raise ReleaseError("--env prod requires a Git repository with a committed release bundle")
        head_sha = _git_head_sha(repo_root)
        if re.fullmatch(r"[0-9a-fA-F]{40}", head_sha) is None:
            raise ReleaseError(f"--env prod requires a valid 40-character Git HEAD commit SHA, got {head_sha!r}")
        rel_manifest = f"releases/{slug}/release.json"
        tracked = _git_run(repo_root, "ls-files", "--error-unmatch", "--", rel_manifest)
        if tracked.returncode != 0:
            raise ReleaseError(f"--env prod requires {rel_manifest} to be tracked in Git before release")
        in_commit = _git_run(repo_root, "cat-file", "-e", f"{head_sha}:{rel_manifest}")
        if in_commit.returncode != 0:
            raise ReleaseError(f"--env prod requires {rel_manifest} to be committed at HEAD ({head_sha[:12]})")
        status_proc = _git_run(
            repo_root,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignored=matching",
            "--",
            f"releases/{slug}",
        )
        if status_proc.returncode != 0:
            detail = status_proc.stderr.decode("utf-8", "replace").strip() or f"exit {status_proc.returncode}"
            raise ReleaseError(f"git status failed for releases/{slug}: {detail}")
        status_out = status_proc.stdout.decode("utf-8", "replace")
        if status_out.strip():
            raise ReleaseError(
                f"--env prod requires a clean Git working tree for releases/{slug} "
                f"(no modified, untracked, or Git-ignored files); commit or remove changes first:\n{status_out.rstrip()}"
            )
        ignored = _git_ignored_bundle_paths(repo_root, slug, [rel_manifest])
        if ignored:
            raise ReleaseError(
                f"--env prod refuses Git-ignored paths in releases/{slug}: {', '.join(ignored)}"
            )
        return head_sha
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseError(f"Git verification for --env prod failed: {exc}") from exc


def _load_pinned_prod_bundle(
    repo_root: Path,
    bundle_dir: Path,
    slug: str,
    release_commit: str,
) -> tuple[dict[str, Any], list[BundleStep], list[str], list[int], list[str], dict[str, str]]:
    rel_manifest = f"releases/{slug}/release.json"
    try:
        ignored = _git_ignored_bundle_paths(repo_root, slug, [rel_manifest])
        if ignored:
            raise ReleaseError(f"--env prod refuses Git-ignored paths in releases/{slug}: {', '.join(ignored)}")
        manifest_bytes = _git_cat_file_blob(repo_root, release_commit, rel_manifest)
        pinned_manifest = _parse_json_object_bytes(manifest_bytes, f"{release_commit[:12]}:{rel_manifest}")
        manifest, steps, schemas, apps, forbidden_patterns = _load_and_validate_manifest(
            bundle_dir, slug, manifest_override=pinned_manifest
        )
        step_repo_paths = [f"releases/{slug}/{step.rel_file}" for step in steps]
        ignored_steps = _git_ignored_bundle_paths(repo_root, slug, step_repo_paths)
        if ignored_steps:
            raise ReleaseError(
                f"--env prod refuses Git-ignored bundle files: {', '.join(ignored_steps)}"
            )
        pinned_step_texts: dict[str, str] = {}
        for step in steps:
            repo_rel = f"releases/{slug}/{step.rel_file}"
            if _git_run(repo_root, "ls-files", "--error-unmatch", "--", repo_rel).returncode != 0:
                raise ReleaseError(f"--env prod requires {repo_rel} to be tracked in Git before release")
            if _git_run(repo_root, "cat-file", "-e", f"{release_commit}:{repo_rel}").returncode != 0:
                raise ReleaseError(f"--env prod requires {repo_rel} to be committed at HEAD ({release_commit[:12]})")
            raw_bytes = _git_cat_file_blob(repo_root, release_commit, repo_rel)
            try:
                pinned_step_texts[step.rel_file] = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ReleaseError(f"cannot decode {repo_rel} at {release_commit[:12]} as UTF-8: {exc}") from exc
        return manifest, steps, schemas, apps, forbidden_patterns, pinned_step_texts
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseError(f"Git verification for --env prod failed: {exc}") from exc


def _git_create_release_tag(repo_root: Path, slug: str, target_commit: str | None = None) -> str:
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
            ["git", "-C", str(repo_root), "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            check=False,
            timeout=10,
        )
        if exists.returncode == 0:
            existing_sha = exists.stdout.strip()
            if target_commit and existing_sha.lower() != target_commit.lower():
                raise ReleaseError(
                    f"Git tag {tag} already exists on commit {existing_sha[:12]}, expected {target_commit[:12]}"
                )
            return f"already exists ({tag})"
        tag_cmd = ["git", "-C", str(repo_root), "tag", tag]
        if target_commit:
            tag_cmd.append(target_commit)
        created = subprocess.run(
            tag_cmd,
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
    raw_rel = file_arg if file_arg else _default_code_filename(schema, object_type, object_name)
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

    rel_entry_file = f"02-code/{checked_rel.as_posix()}"
    code_list = list(manifest.get("code") or [])
    for existing in code_list:
        if not isinstance(existing, dict):
            continue
        same_obj = (
            existing.get("schema") == schema
            and existing.get("objectType") == object_type
            and existing.get("objectName") == object_name
        )
        if same_obj:
            continue
        existing_file = str(existing.get("file") or "").strip().replace("\\", "/")
        while existing_file.startswith("./"):
            existing_file = existing_file[2:]
        if existing_file and not existing_file.startswith("02-code/"):
            existing_file = f"02-code/{existing_file}"
        if existing_file == rel_entry_file:
            raise ReleaseError(
                f"bundle file {rel_entry_file} is already used by "
                f"{existing.get('schema')}.{existing.get('objectName')} ({existing.get('objectType')})"
            )

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(sql_text, encoding="utf-8", newline="\n")

    new_entry: dict[str, Any] = {
        "schema": schema,
        "file": rel_entry_file,
        "objectType": object_type,
        "objectName": object_name,
        "mode": mode,
    }
    if base_sha256 is not None:
        new_entry["baseSha256"] = base_sha256

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


def _validate_basic_app_descriptor(path: Path, expected_app_id: int) -> None:
    data = _read_json_object(path, path.as_posix())
    workspace = data.get("workspace")
    ws_name = workspace.get("name") if isinstance(workspace, dict) else None
    if not isinstance(ws_name, str) or not ws_name.strip() or any(c in ws_name for c in "\t\r\n"):
        raise ReleaseError("deployment workspace.name must be a non-empty single-line string")
    app = data.get("app")
    if not isinstance(app, dict):
        raise ReleaseError("deployment app object is required")
    app_id = app.get("id")
    if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id != expected_app_id:
        raise ReleaseError(f"deployment app.id must equal {expected_app_id}, got {app_id!r}")
    db_sess = app.get("databaseSession")
    parsing_schema = db_sess.get("parsingSchema") if isinstance(db_sess, dict) else None
    if not isinstance(parsing_schema, str) or ORACLE_IDENTIFIER.fullmatch(parsing_schema) is None:
        raise ReleaseError("deployment app.databaseSession.parsingSchema must be an uppercase Oracle identifier")


def _write_preflight_env_file(work_dir: Path, values: Mapping[str, str]) -> Path:
    work_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    env_path = work_dir / "preflight.env"
    lines: list[str] = []
    for key, val in values.items():
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", str(key)) is None:
            continue
        text = str(val)
        if "\n" in text or "\r" in text:
            continue
        lines.append(f"{key}={text}")
    descriptor = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(("\n".join(lines) + "\n") if lines else "")
    return env_path


def _step_text(step: BundleStep, step_texts: Mapping[str, str] | None) -> str:
    if step_texts is not None:
        if step.rel_file not in step_texts:
            raise ReleaseError(f"missing commit-pinned content for {step.rel_file}")
        return step_texts[step.rel_file]
    return step.path.read_text(encoding="utf-8")


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
    require_deployer: bool = True,
    pinned_commit: str | None = None,
    env_file_path: Path | None = None,
) -> PreflightResult:
    bundle_dir = _safe_bundle_dir(repo_root, slug)
    if not bundle_dir.is_dir():
        raise ReleaseError(f"release bundle not found: releases/{slug}")
    if pinned_commit is not None:
        manifest, steps, schemas, apps, forbidden_patterns, pinned_step_texts = _load_pinned_prod_bundle(
            repo_root, bundle_dir, slug, pinned_commit
        )
    else:
        manifest, steps, schemas, apps, forbidden_patterns = _load_and_validate_manifest(bundle_dir, slug)
        pinned_step_texts = None
    if manifest.get("status") == "deployed":
        raise ReleaseError(f"release bundle '{slug}' is already deployed and locked")
    _check_bundle_contents(steps, forbidden_patterns, step_texts=pinned_step_texts)

    targets: dict[str, Target] = {}
    identities: dict[str, dict[str, str]] = {}
    fetched_by_schema: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    failures: list[str] = []

    if env == "prod" and pinned_commit is None and _is_git_repo(repo_root):
        rel_paths = [f"releases/{slug}/release.json", *(f"releases/{slug}/{s.rel_file}" for s in steps)]
        ignored = _git_ignored_bundle_paths(repo_root, slug, rel_paths)
        if ignored:
            failures.append(f"--env prod refuses Git-ignored paths in releases/{slug}: {', '.join(ignored)}")

    items_by_schema: dict[str, list[tuple[str, str]]] = {schema: [] for schema in schemas}
    for step in steps:
        if step.section == "code" and step.object_type and step.object_name:
            pair = (step.object_type, step.object_name)
            if pair not in items_by_schema[step.schema]:
                items_by_schema[step.schema].append(pair)
        elif step.section in {"ddl", "ords"}:
            if step.objects:
                for pair in step.objects:
                    if pair not in items_by_schema[step.schema]:
                        items_by_schema[step.schema].append(pair)
            elif step.section == "ddl":
                sql_text = _step_text(step, pinned_step_texts)
                for cand_schema, cand_type, cand_name in _extract_ddl_candidate_objects(sql_text, step.schema):
                    if cand_schema in items_by_schema:
                        pair = (cand_type, cand_name)
                        if pair not in items_by_schema[cand_schema]:
                            items_by_schema[cand_schema].append(pair)

    for schema in schemas:
        targets[schema] = _resolve_schema_target(values, env, schema, operation="migration")

    for schema in schemas:
        target = targets[schema]
        schema_code_steps = [s for s in steps if s.schema == schema and s.section == "code"]
        items = items_by_schema[schema]
        need_ddl = any(obj_type in {"VIEW", "TRIGGER"} for obj_type, _ in items)
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
                    proposed_text = _step_text(step, pinned_step_texts)
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

    ords_steps = [s for s in steps if s.section == "ords"]
    unbacked_ords_steps: list[str] = []
    for step in ords_steps:
        schema_fetched = fetched_by_schema.get(step.schema, {})
        has_backed = any(
            (schema_fetched.get(pair) or {}).get("exists") is True
            for pair in step.objects
        )
        if not has_backed:
            unbacked_ords_steps.append(step.rel_file)
    if not ords_steps:
        ords_backup_coverage = "not-applicable"
    elif unbacked_ords_steps:
        ords_backup_coverage = "unbacked"
    else:
        ords_backup_coverage = "covered"

    if apps:
        if env == "dev":
            failures.append(
                "APEX app deploy in release run supports staging and prod only; "
                "use 'scripts/team.sh publish <app_id> --env dev' for DEV"
            )
        else:
            if require_deployer and not (repo_root / "scripts" / "deploy.sh").is_file():
                failures.append(
                    "scripts/deploy.sh not found; cannot deploy APEX app(s): "
                    + ", ".join(str(a) for a in apps)
                )
            apps_dir = repo_root / "apps"
            publish_app_sh = repo_root / "scripts" / "publish_app.sh"
            describe_env_file = env_file_path
            if require_deployer and publish_app_sh.is_file() and describe_env_file is None:
                describe_env_file = _write_preflight_env_file(work_dir, values)
            if apps_dir.is_dir() or require_deployer:
                for app_id in apps:
                    matches: list[Path] = []
                    if apps_dir.is_dir():
                        matches = [
                            p
                            for p in (*apps_dir.glob(f"*/{app_id}/deployments/{env}.json"), apps_dir / str(app_id) / "deployments" / f"{env}.json")
                            if p.is_file() and not p.is_symlink()
                        ]
                    if not matches:
                        failures.append(
                            f"APEX app {app_id} deployment descriptor not found under "
                            f"apps/*/{app_id}/deployments/{env}.json"
                        )
                        continue
                    if not require_deployer:
                        continue
                    if publish_app_sh.is_file():
                        child_env = {**os.environ, **{k: str(v) for k, v in values.items()}}
                        if describe_env_file is not None:
                            child_env["PROJECT_ENV_FILE"] = str(describe_env_file)
                        proc = subprocess.run(
                            [bash_command(), publish_app_sh.as_posix(), str(app_id), "--env", env, "--describe"],
                            cwd=repo_root,
                            env=child_env,
                            stdin=subprocess.DEVNULL,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            timeout=30,
                            check=False,
                        )
                        if proc.returncode != 0:
                            detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
                            failures.append(f"APEX app {app_id} descriptor validation failed for --env {env}: {detail}")
                            continue
                        desc_lines = proc.stdout.splitlines()
                        if len(desc_lines) != 6:
                            failures.append(
                                f"APEX app {app_id} --describe returned {len(desc_lines)} lines, expected 6"
                            )
                        elif not desc_lines[3].strip() or not desc_lines[4].strip():
                            failures.append(
                                f"APEX app {app_id} target connection/user is not configured for --env {env}"
                            )
                    else:
                        for match_path in matches:
                            try:
                                _validate_basic_app_descriptor(match_path, app_id)
                            except ReleaseError as exc:
                                failures.append(
                                    f"APEX app {app_id} descriptor validation failed for --env {env}: {exc}"
                                )

    return PreflightResult(
        manifest=manifest,
        steps=steps,
        schemas=schemas,
        apps=apps,
        targets=targets,
        identities=identities,
        fetched_by_schema=fetched_by_schema,
        failures=failures,
        pinned_step_texts=pinned_step_texts,
        ords_backup_coverage=ords_backup_coverage,
        unbacked_ords_steps=unbacked_ords_steps,
    )


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
    env_file_path = _resolve_env_file(repo_root, environ)
    scratch_root = repo_root / "scratch"
    scratch_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-check-", dir=scratch_root) as tmp_dir:
        pre = _preflight_bundle(
            repo_root,
            slug,
            env,
            allow_existing=allow_existing,
            confirm_target=None,
            values=values,
            runner=runner,
            work_dir=Path(tmp_dir),
            env_file_path=env_file_path,
        )
    steps = pre.steps
    schemas = pre.schemas
    apps = pre.apps
    targets = pre.targets
    identities = pre.identities
    failures = pre.failures
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
        "ordsBackupCoverage": pre.ords_backup_coverage,
        "unbackedOrdsSteps": pre.unbacked_ords_steps,
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
        if pre.unbacked_ords_steps:
            print(
                "  WARNING: ORDS backup coverage: unbacked steps: "
                + ", ".join(pre.unbacked_ords_steps)
                + " (release run requires --allow-unbacked-ords unless covered)"
            )
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


def _target_identity_guard_lines(
    target: Target,
    *,
    confirm_target: str | None = None,
    preflight_con_name: str | None = None,
) -> list[str]:
    q_user = _sql_literal(target.expected_user)
    q_schema = _sql_literal(target.schema)
    lines = [
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "BEGIN",
    ]
    if confirm_target and confirm_target.strip():
        q_db = _sql_literal(confirm_target.strip())
        lines.extend([
            f"  IF SYS_CONTEXT('USERENV','DB_UNIQUE_NAME') IS NULL OR UPPER(SYS_CONTEXT('USERENV','DB_UNIQUE_NAME')) <> UPPER({q_db}) THEN",
            f"    RAISE_APPLICATION_ERROR(-20901, 'Target DB_UNIQUE_NAME mismatch in apply session (expected ' || {q_db} || ', observed ' || NVL(SYS_CONTEXT('USERENV','DB_UNIQUE_NAME'), '<NULL>') || ')');",
            "  END IF;",
        ])
    if preflight_con_name and preflight_con_name.strip():
        q_con = _sql_literal(preflight_con_name.strip())
        lines.extend([
            f"  IF UPPER(NVL(SYS_CONTEXT('USERENV','CON_NAME'), 'NON-CDB')) <> UPPER({q_con}) THEN",
            f"    RAISE_APPLICATION_ERROR(-20901, 'Target CON_NAME mismatch in apply session (expected ' || {q_con} || ', observed ' || NVL(SYS_CONTEXT('USERENV','CON_NAME'), 'NON-CDB') || ')');",
            "  END IF;",
        ])
    lines.extend([
        f"  IF UPPER(USER) <> UPPER({q_user}) THEN",
        f"    RAISE_APPLICATION_ERROR(-20901, 'Target USER mismatch in apply session (expected ' || {q_user} || ', observed ' || USER || ')');",
        "  END IF;",
        f"  IF UPPER(SYS_CONTEXT('USERENV','CURRENT_SCHEMA')) <> UPPER({q_schema}) THEN",
        f"    RAISE_APPLICATION_ERROR(-20901, 'Target CURRENT_SCHEMA mismatch in apply session (expected ' || {q_schema} || ', observed ' || SYS_CONTEXT('USERENV','CURRENT_SCHEMA') || ')');",
        "  END IF;",
        "END;",
        "/",
    ])
    return lines


def _patch_base_guard_lines(target: Target, step: BundleStep, expected_source: str) -> list[str]:
    assert step.object_type and step.object_name
    q_schema = _sql_literal(target.schema)
    q_type = _sql_literal(step.object_type)
    q_name = _sql_literal(step.object_name)
    q_meta_type = _sql_literal(step.object_type.replace(" ", "_"))
    expected_norm = normalize_source_for_hash(expected_source)
    expected_sha = step.base_sha256 or compute_source_sha256(expected_source)

    append_lines: list[str] = []
    if expected_norm:
        for line in expected_norm.split("\n")[:-1]:
            if not line:
                append_lines.append("  DBMS_LOB.APPEND(l_expected, TO_CLOB(CHR(10)));")
                continue
            for idx in range(0, len(line), 1500):
                chunk = line[idx : idx + 1500]
                append_lines.append(f"  DBMS_LOB.APPEND(l_expected, TO_CLOB({_sql_literal(chunk)}));")
            append_lines.append("  DBMS_LOB.APPEND(l_expected, TO_CLOB(CHR(10)));")

    lines = [
        "SET DEFINE OFF",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        f"-- RELEASE_PATCH_GUARD objectType={step.object_type} objectName={step.object_name} baseSha256={expected_sha}",
        "DECLARE",
        "  l_raw            CLOB;",
        "  l_ddl            CLOB;",
        "  l_live           CLOB;",
        "  l_expected       CLOB;",
        "  l_pos            PLS_INTEGER := 1;",
        "  l_total          PLS_INTEGER;",
        "  l_nl             PLS_INTEGER;",
        "  l_len            PLS_INTEGER;",
        "  l_next           PLS_INTEGER;",
        "  l_off            PLS_INTEGER;",
        "  l_take           PLS_INTEGER;",
        "  l_chunk          VARCHAR2(32767);",
        "  l_line           VARCHAR2(32767);",
        "  l_started        BOOLEAN := FALSE;",
        "  l_pending_blanks PLS_INTEGER := 0;",
        "  l_code_type_re   CONSTANT VARCHAR2(200) := '^[[:space:]]*ALTER[[:space:]]+SESSION[[:space:]]+SET[[:space:]]+PLSQL_' || 'CODE_TYPE[[:space:]]*=[[:space:]]*INTERPRETED[[:space:]]*;?[[:space:]]*$';",
        "  PROCEDURE cleanup_lobs IS",
        "  BEGIN",
        "    IF l_raw IS NOT NULL AND DBMS_LOB.ISTEMPORARY(l_raw) = 1 THEN DBMS_LOB.FREETEMPORARY(l_raw); END IF;",
        "    IF l_live IS NOT NULL AND DBMS_LOB.ISTEMPORARY(l_live) = 1 THEN DBMS_LOB.FREETEMPORARY(l_live); END IF;",
        "    IF l_expected IS NOT NULL AND DBMS_LOB.ISTEMPORARY(l_expected) = 1 THEN DBMS_LOB.FREETEMPORARY(l_expected); END IF;",
        "  END;",
        "BEGIN",
        "  DBMS_LOB.CREATETEMPORARY(l_raw, TRUE);",
        "  DBMS_LOB.CREATETEMPORARY(l_live, TRUE);",
        "  DBMS_LOB.CREATETEMPORARY(l_expected, TRUE);",
        *append_lines,
        "  FOR r IN (SELECT text FROM ALL_SOURCE",
        f"             WHERE OWNER = {q_schema} AND TYPE = {q_type} AND NAME = {q_name}",
        "             ORDER BY line) LOOP",
        "    DBMS_LOB.APPEND(l_raw, TO_CLOB(r.text));",
        "  END LOOP;",
    ]
    if step.object_type in {"VIEW", "TRIGGER"}:
        lines.extend([
            "  IF DBMS_LOB.GETLENGTH(l_raw) = 0 THEN",
            "    BEGIN",
            "      DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'SQLTERMINATOR', TRUE);",
            f"      l_ddl := DBMS_METADATA.GET_DDL({q_meta_type}, {q_name}, {q_schema});",
            "      IF l_ddl IS NOT NULL THEN",
            "        DBMS_LOB.APPEND(l_raw, l_ddl);",
            "      END IF;",
            "    EXCEPTION WHEN OTHERS THEN",
            "      NULL;",
            "    END;",
            "  END IF;",
        ])
    lines.extend([
        "  l_total := DBMS_LOB.GETLENGTH(l_raw);",
        "  WHILE l_pos <= l_total LOOP",
        "    l_nl := DBMS_LOB.INSTR(l_raw, CHR(10), l_pos);",
        "    IF l_nl = 0 THEN",
        "      l_len := l_total - l_pos + 1;",
        "      l_next := l_total + 1;",
        "    ELSE",
        "      l_len := l_nl - l_pos;",
        "      l_next := l_nl + 1;",
        "    END IF;",
        "    l_line := '';",
        "    IF l_len > 0 THEN",
        "      l_off := 0;",
        "      WHILE l_off < l_len LOOP",
        "        l_chunk := DBMS_LOB.SUBSTR(l_raw, 8000, l_pos + l_off);",
        "        IF l_chunk IS NULL THEN",
        "          EXIT;",
        "        END IF;",
        "        l_take := LEAST(LENGTH(l_chunk), l_len - l_off);",
        "        l_line := l_line || SUBSTR(l_chunk, 1, l_take);",
        "        l_off := l_off + l_take;",
        "      END LOOP;",
        "    END IF;",
        "    l_line := REPLACE(l_line, CHR(13), '');",
        "    IF NOT REGEXP_LIKE(l_line, l_code_type_re, 'i') THEN",
        "      l_line := REGEXP_REPLACE(l_line, '[[:space:]]+$', '');",
        "      IF l_line IS NULL THEN",
        "        IF l_started THEN",
        "          l_pending_blanks := l_pending_blanks + 1;",
        "        END IF;",
        "      ELSE",
        "        l_started := TRUE;",
        "        WHILE l_pending_blanks > 0 LOOP",
        "          DBMS_LOB.APPEND(l_live, TO_CLOB(CHR(10)));",
        "          l_pending_blanks := l_pending_blanks - 1;",
        "        END LOOP;",
        "        DBMS_LOB.APPEND(l_live, TO_CLOB(l_line || CHR(10)));",
        "      END IF;",
        "    END IF;",
        "    l_pos := l_next;",
        "  END LOOP;",
        "  IF DBMS_LOB.COMPARE(l_live, l_expected) <> 0 THEN",
        "    cleanup_lobs;",
        f"    RAISE_APPLICATION_ERROR(-20902, 'Patch base source changed between preflight and apply for ' || {q_schema} || '.' || {q_name} || ' (' || {q_type} || ')');",
        "  END IF;",
        "  cleanup_lobs;",
        "EXCEPTION",
        "  WHEN OTHERS THEN",
        "    cleanup_lobs;",
        "    RAISE;",
        "END;",
        "/",
    ])
    return lines


def _sql_step_lines(
    target: Target,
    section_steps: Sequence[BundleStep],
    *,
    patch_sources: Mapping[tuple[str, str], str] | None = None,
    step_texts: Mapping[str, str] | None = None,
) -> list[str]:
    lines: list[str] = []
    for step in section_steps:
        if step.section == "code" and step.mode == "patch" and step.object_type and step.object_name:
            if patch_sources is not None and (step.object_type, step.object_name) in patch_sources:
                lines.extend(_patch_base_guard_lines(target, step, patch_sources[(step.object_type, step.object_name)]))
        text = _step_text(step, step_texts).replace("\r\n", "\n").replace("\r", "\n")
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


def _render_apply_driver(
    target: Target,
    schema_steps: Sequence[BundleStep],
    *,
    confirm_target: str | None = None,
    preflight_con_name: str | None = None,
    patch_sources: Mapping[tuple[str, str], str] | None = None,
    step_texts: Mapping[str, str] | None = None,
) -> str:
    """Render 1 SQLcl driver for single-schema bundles: target guard, before-invalids, 01-ddl -> 02-code -> 03-ords, ALL_ERRORS, after-invalids."""
    ddl_steps = [s for s in schema_steps if s.section == "ddl"]
    code_steps = [s for s in schema_steps if s.section == "code"]
    ords_steps = [s for s in schema_steps if s.section == "ords"]

    lines = _apply_session_prelude(target)
    lines.extend(_target_identity_guard_lines(target, confirm_target=confirm_target, preflight_con_name=preflight_con_name))
    lines.extend(_before_invalids_lines(target))
    if ddl_steps:
        lines.extend(_sql_step_lines(target, ddl_steps, patch_sources=patch_sources, step_texts=step_texts))
    if code_steps:
        lines.extend(_sql_step_lines(target, code_steps, patch_sources=patch_sources, step_texts=step_texts))
    if ords_steps:
        lines.extend(_sql_step_lines(target, ords_steps, patch_sources=patch_sources, step_texts=step_texts))
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


def _render_batch_apply_driver(
    target: Target,
    batch_steps: Sequence[BundleStep],
    *,
    confirm_target: str | None = None,
    preflight_con_name: str | None = None,
    patch_sources: Mapping[tuple[str, str], str] | None = None,
    step_texts: Mapping[str, str] | None = None,
) -> str:
    """Render 1 SQLcl apply driver for one consecutive schema batch within a phase."""
    lines = _apply_session_prelude(target)
    lines.extend(_target_identity_guard_lines(target, confirm_target=confirm_target, preflight_con_name=preflight_con_name))
    lines.extend(_sql_step_lines(target, batch_steps, patch_sources=patch_sources, step_texts=step_texts))
    lines.extend(["PROMPT RELEASE_BATCH_APPLIED", driver_epilogue(True), ""])
    return "\n".join(lines)


def _render_post_apply_driver(target: Target, code_steps: Sequence[BundleStep]) -> str:
    """Render read-only ALL_ERRORS and after-invalids check driver for multi-schema bundles."""
    lines = _apply_session_prelude(target)
    lines.append("SET TRANSACTION READ ONLY;")
    lines.extend(_post_apply_lines(target, code_steps))
    lines.extend([driver_epilogue(False), ""])
    return "\n".join(lines)


def _render_verify_driver(
    target: Target,
    verify_steps: Sequence[BundleStep],
    *,
    step_texts: Mapping[str, str] | None = None,
) -> str:
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
        text = _step_text(step, step_texts).replace("\r\n", "\n").replace("\r", "\n")
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
    allow_unbacked_ords: bool = False,
    report_arg: str | None,
    environ: Mapping[str, str] | None,
    runner: Callable,
    app_deployer: Callable[[Path, int, str, str, Mapping[str, str]], None] = _invoke_app_deploy,
) -> int:
    if not isinstance(confirm_target, str) or not confirm_target.strip():
        raise ReleaseUsageError("--confirm-target <db_unique_name> is required and must be non-empty")
    slug = _parse_bundle_slug(bundle_arg)
    values = _load_env(repo_root, environ)
    env_file_path = _resolve_env_file(repo_root, environ)
    scratch_root = repo_root / "scratch"
    scratch_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = _utc_stamp()
    if report_arg:
        report_path = Path(report_arg)
        if not report_path.is_absolute():
            report_path = repo_root / report_path
    else:
        report_path = scratch_root / "release-reports" / f"{slug}-{env}-{stamp}.md"

    pinned_commit = _verify_prod_git_clean(repo_root, slug) if env == "prod" else None
    release_commit = pinned_commit if pinned_commit is not None else _git_head_sha(repo_root)

    manifest: dict[str, Any] = {"status": "draft"}
    schemas: list[str] = []
    identities: dict[str, dict[str, str]] = {}
    before_invalids: dict[str, set[tuple[str, str]]] = {}
    after_invalids: dict[str, set[tuple[str, str]]] = {}
    new_invalids: dict[str, list[tuple[str, str]]] = {}
    timings: dict[str, float] = {}
    completed_phases: list[str] = []
    backup_label = "not-started"
    ords_backup_coverage = "unknown"
    unbacked_ords_steps: list[str] = []
    tag_status = "not-applicable"
    deployed_apps: list[int] = []
    outcome = "FAILED"
    error_message: str | None = None
    preflight_started = False

    try:
        with tempfile.TemporaryDirectory(prefix="release-run-", dir=scratch_root) as tmp_dir:
            tmp_path = Path(tmp_dir)

            # Step 1-2: Preflight + target confirmation
            preflight_started = True
            t0 = time.monotonic()
            try:
                pre = _preflight_bundle(
                    repo_root,
                    slug,
                    env,
                    allow_existing=allow_existing,
                    confirm_target=confirm_target,
                    values=values,
                    runner=runner,
                    work_dir=tmp_path / "preflight",
                    require_deployer=(app_deployer is _invoke_app_deploy),
                    pinned_commit=pinned_commit,
                    env_file_path=env_file_path,
                )
            finally:
                timings["preflight"] = round(time.monotonic() - t0, 3)
            manifest = pre.manifest
            steps = pre.steps
            schemas = pre.schemas
            apps = pre.apps
            targets = pre.targets
            identities = pre.identities
            fetched_by_schema = pre.fetched_by_schema
            failures = pre.failures
            pinned_step_texts = pre.pinned_step_texts
            ords_backup_coverage = pre.ords_backup_coverage
            unbacked_ords_steps = list(pre.unbacked_ords_steps)
            before_invalids = {schema: set() for schema in schemas}
            after_invalids = {schema: set() for schema in schemas}
            new_invalids = {schema: [] for schema in schemas}
            if failures:
                raise ReleaseError("Preflight refused:\n  - " + "\n  - ".join(failures))
            completed_phases.append("preflight")

            patch_sources_by_schema: dict[str, dict[tuple[str, str], str]] = {schema: {} for schema in schemas}
            for step in steps:
                if step.section == "code" and step.mode == "patch" and step.object_type and step.object_name:
                    obj = fetched_by_schema[step.schema].get((step.object_type, step.object_name), {})
                    patch_sources_by_schema[step.schema][(step.object_type, step.object_name)] = _source_text_for_hash(
                        step.object_type, obj
                    )

            # Step 3: Backup existing target objects
            t1 = time.monotonic()
            try:
                if skip_backup:
                    backup_label = "skipped"
                    if any(s.section == "ords" for s in steps):
                        ords_backup_coverage = "skipped"
                else:
                    if unbacked_ords_steps and not allow_unbacked_ords:
                        raise ReleaseError(
                            "03-ords/ step(s) lack ORDS metadata backup coverage: "
                            + ", ".join(unbacked_ords_steps)
                            + ". Declare touched schema 'objects' in release.json if backing PL/SQL/objects, "
                            + "or pass --allow-unbacked-ords when ORDS metadata backup is not available."
                        )
                    backup_dir = scratch_root / "backups" / f"{slug}-{env}-{stamp}"
                    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                    backup_label = backup_dir.relative_to(repo_root).as_posix()
                    expected_objects: list[dict[str, str]] = []
                    backed_up: list[dict[str, str]] = []
                    try:
                        for schema in schemas:
                            existing_items = [
                                key
                                for key, obj in fetched_by_schema[schema].items()
                                if obj.get("exists") is True
                            ]
                            for obj_type, obj_name in existing_items:
                                expected_objects.append({
                                    "schema": schema,
                                    "objectType": obj_type,
                                    "objectName": obj_name,
                                })
                            if not existing_items:
                                continue
                            backup_identity, backed = _fetch_schema_objects(
                                runner,
                                targets[schema],
                                tmp_path / f"backup-{schema}",
                                existing_items,
                                include_ddl=True,
                                environ=values,
                            )
                            _assert_matching_session_identity(
                                backup_identity,
                                identities[schema],
                                schema=schema,
                                phase="backup",
                                confirm_target=confirm_target,
                            )
                            for obj_type, obj_name in existing_items:
                                obj = backed.get((obj_type, obj_name)) or {}
                                content = obj.get("ddl") or obj.get("source") or ""
                                if not isinstance(content, str) or not content.strip():
                                    ddl_err = obj.get("ddlError")
                                    detail = f" ({ddl_err})" if isinstance(ddl_err, str) and ddl_err.strip() else ""
                                    raise ReleaseError(
                                        f"Backup failed before apply: existing object {schema}.{obj_name} ({obj_type}) "
                                        f"returned empty DDL/source{detail}"
                                    )
                                fname = f"{schema}.{obj_type.replace(' ', '_')}.{obj_name}.sql"
                                (backup_dir / fname).write_text(
                                    content.replace("\r\n", "\n"), encoding="utf-8", newline="\n"
                                )
                                backed_up.append({
                                    "schema": schema,
                                    "objectType": obj_type,
                                    "objectName": obj_name,
                                    "file": fname,
                                })
                    finally:
                        _write_json_lf(
                            backup_dir / "backup-manifest.json",
                            {
                                "slug": slug,
                                "environment": env,
                                "expectedObjects": expected_objects,
                                "objects": backed_up,
                                "ordsBackupCoverage": ords_backup_coverage,
                                "unbackedOrdsSteps": unbacked_ords_steps,
                            },
                        )
                completed_phases.append("backup")
            finally:
                timings["backup"] = round(time.monotonic() - t1, 3)

            # Step 4: Execute all 01-ddl -> all 02-code -> all 03-ords (consecutive schema batches) + invalid delta
            t2 = time.monotonic()
            try:
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
                    driver_sql = _render_apply_driver(
                        targets[schema],
                        schema_apply_steps,
                        confirm_target=confirm_target,
                        preflight_con_name=identities[schema].get("con_name"),
                        patch_sources=patch_sources_by_schema[schema],
                        step_texts=pinned_step_texts,
                    )
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
                    before_identity = _verify_live_identity(before_payload.get("identity") or {}, targets[schema])
                    _assert_matching_session_identity(
                        before_identity,
                        identities[schema],
                        schema=schema,
                        phase="apply",
                        confirm_target=confirm_target,
                    )
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
                        before_identity = _verify_live_identity(before_payload.get("identity") or {}, targets[schema])
                        _assert_matching_session_identity(
                            before_identity,
                            identities[schema],
                            schema=schema,
                            phase="preconditions",
                            confirm_target=confirm_target,
                        )
                        before_invalids[schema] = _invalid_set(before_payload.get("invalids") or [])

                    # 4b. Execute strictly by phase (ddl -> code -> ords), grouping consecutive same-schema steps.
                    batch_num = 0
                    for section in ("ddl", "code", "ords"):
                        phase_steps = [s for s in steps if s.section == section]
                        for batch_schema, batch_iter in groupby(phase_steps, key=lambda s: s.schema):
                            batch_num += 1
                            batch_steps = list(batch_iter)
                            driver_sql = _render_batch_apply_driver(
                                targets[batch_schema],
                                batch_steps,
                                confirm_target=confirm_target,
                                preflight_con_name=identities[batch_schema].get("con_name"),
                                patch_sources=patch_sources_by_schema[batch_schema],
                                step_texts=pinned_step_texts,
                            )
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
                completed_phases.append("apply")
            finally:
                timings["apply"] = round(time.monotonic() - t2, 3)

            # Step 4b: 04-verify smoke scripts (read-only)
            t3 = time.monotonic()
            try:
                for schema in schemas:
                    verify_steps = [s for s in steps if s.schema == schema and s.section == "verify"]
                    if not verify_steps:
                        continue
                    driver_sql = _render_verify_driver(targets[schema], verify_steps, step_texts=pinned_step_texts)
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
                completed_phases.append("verify")
            finally:
                timings["verify"] = round(time.monotonic() - t3, 3)

            # Step 5: Optional APEX app deploy
            t4 = time.monotonic()
            try:
                for app_id in apps:
                    app_deployer(repo_root, app_id, env, confirm_target, values)
                    deployed_apps.append(app_id)
                completed_phases.append("apps")
            finally:
                timings["apps"] = round(time.monotonic() - t4, 3)

        # Step 6: Report & Prod lock
        if env == "prod":
            tag_status = _git_create_release_tag(repo_root, slug, release_commit)
            bundle_dir = _safe_bundle_dir(repo_root, slug)
            observed_db = next(iter(identities.values()))["db_unique_name"] if identities else confirm_target
            manifest["status"] = "deployed"
            manifest["deployedAt"] = _utc_now()
            manifest["deployedTarget"] = observed_db
            manifest["deployedCommit"] = release_commit
            _write_json_lf(bundle_dir / "release.json", manifest)
            completed_phases.append("prod-lock")

        outcome = "SUCCEEDED"
    except Exception as exc:
        error_message = str(exc)
        raise
    finally:
        if preflight_started:
            report_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            report_lines = [
                f"# Release Report: {slug} ({env})",
                "",
                f"- **Outcome:** {outcome}",
                f"- **Status:** {manifest.get('status')}",
            ]
            if error_message:
                report_lines.append(f"- **Error:** {error_message}")
            report_lines.extend([
                f"- **Commit:** `{release_commit}`",
                f"- **Backup:** `{backup_label}`",
                f"- **ORDS backup coverage:** `{ords_backup_coverage}`",
            ])
            if unbacked_ords_steps:
                report_lines.append(
                    "- **Unbacked ORDS steps:** " + ", ".join(f"`{s}`" for s in unbacked_ords_steps)
                )
            report_lines.extend([
                f"- **Git tag:** `{tag_status}`",
                f"- **Completed phases:** {', '.join(completed_phases) if completed_phases else 'none'}",
                f"- **Apps deployed:** {', '.join(str(a) for a in deployed_apps) if deployed_apps else 'none'}",
                "",
                "## Target Identities",
                "",
            ])
            for schema in schemas:
                ident = identities.get(schema)
                if ident is None:
                    report_lines.append(f"- `{schema}`: (preflight identity not captured)")
                    continue
                report_lines.append(
                    f"- `{schema}`: `DB_UNIQUE_NAME={ident['db_unique_name']}`, `CON_NAME={ident['con_name']}`, "
                    f"`USER={ident['session_user']}`, before_invalid={len(before_invalids.get(schema, ()))}, "
                    f"after_invalid={len(after_invalids.get(schema, ()))}, new_invalid={len(new_invalids.get(schema, ()))}"
                )
            report_lines.extend([
                "",
                "## Phase Timings (seconds)",
                "",
                *(f"- `{phase}`: {secs:.3f}s" for phase, secs in timings.items()),
                "",
            ])
            report_path.write_text("\n".join(report_lines), encoding="utf-8", newline="\n")
            if outcome == "SUCCEEDED":
                print(f"Release {slug} -> {env} succeeded. Report: {report_path}")
            else:
                print(f"Release {slug} -> {env} failed. Report: {report_path}", file=sys.stderr)

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
    p_run.add_argument("--allow-unbacked-ords", action="store_true", help="Proceed when 03-ords/ steps have no backed-up object coverage")
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
                allow_unbacked_ords=args.allow_unbacked_ords,
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
