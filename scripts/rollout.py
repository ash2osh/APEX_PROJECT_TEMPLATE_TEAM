#!/usr/bin/env python3
"""Run a frozen, ordered deployment manifest with resumable local receipts."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from collections.abc import Callable, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import migrate, verify_checks
from scripts.db_targets import Target, looks_like_production_identity
from scripts.deployment_descriptor import read_descriptor
from scripts.migration_manifest import MigrationManifestError, load_batch
from scripts.ords_export import OrdsExportError, check_no_oauth, check_structure, mask_sql
from scripts.sqlcl_session import ALIAS_RE, SqlclError, run_sqlcl
from scripts.validate_app_source import validate_app_source


ROOT = Path(__file__).resolve().parents[1]
ORACLE_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
MANIFEST_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1
FORBIDDEN_SQLCL_COMMAND = re.compile(
    r"(?im)^[ \t]*(?:@@?[^\r\n]*|![^\r\n]*|(?:START|CONNECT|DISCONNECT|EXIT|QUIT|HOST|WHENEVER|SPOOL|STORE|PASSWORD|ACCEPT|SET|PROMPT|UNDEFINE|VARIABLE|PRINT|COLUMN|TTITLE|BTITLE|BREAK|COMPUTE|CLEAR|SHOW|DESCRIBE|INFO|PAUSE)(?:[ \t]|$))"
)
ROLLOUT_MARKER_LITERAL = re.compile(r"(?i)ROLLOUT_(?:IDENTITY|TARGET|SCRIPT_COMPLETED)")
CURRENT_SCHEMA_CHANGE = re.compile(r"(?is)\bALTER\s+SESSION\s+SET\s+CURRENT_SCHEMA\b")
ORDS_CALL = re.compile(r"(?im)\b(?:ORDS(?:_ADMIN)?|ORDS_METADATA)\.[A-Z][A-Z0-9_]*[ \t]*\(")
MODULE_PARAMETER = re.compile(r"(?i)\bp_module_name[ \t]*=>")


class RolloutError(ValueError):
    """The manifest or rollout state cannot be trusted enough to continue."""


@dataclass(frozen=True)
class RolloutManifest:
    path: Path
    repo_root: Path
    sha256: str
    steps: tuple[dict, ...]


@dataclass(frozen=True)
class StepResult:
    exit_code: int
    evidence_paths: tuple[str, ...] = ()
    message: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _object_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RolloutError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_json(path: Path, label: str) -> object:
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise RolloutError(f"{label} must be UTF-8 without a byte-order mark")
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_object_no_duplicates)
    except RolloutError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RolloutError(f"cannot read {label}: {error}") from error


def _assert_no_symlink(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise RolloutError(f"path is outside the repository: {path}") from error
    current = root
    if current.is_symlink():
        raise RolloutError(f"repository root must not be a symbolic link: {current}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise RolloutError(f"symbolic links are not allowed in rollout inputs: {current}")


def _repo_file(value: object, root: Path, label: str, *, kind: str | None = None) -> tuple[str, Path]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RolloutError(f"{label} must be a nonempty repository-relative path")
    if "\\" in value or re.match(r"^[A-Za-z]:", value):
        raise RolloutError(f"{label} must use forward slashes")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise RolloutError(f"{label} must stay inside the repository and cannot contain . or .. components")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise RolloutError(f"{label} must stay inside the repository and cannot contain . or .. components")
    if relative.parts[0] in {"scratch", ".sync-state"}:
        raise RolloutError(f"{label} cannot refer to generated rollout or recovery state")
    candidate = root.joinpath(*relative.parts)
    try:
        _assert_no_symlink(candidate, root)
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise RolloutError(f"{label} does not exist: {value}") from error
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise RolloutError(f"{label} resolves outside the repository: {value}") from error
    if kind == "file" and not resolved.is_file():
        raise RolloutError(f"{label} must name a regular file: {value}")
    if kind == "directory" and not resolved.is_dir():
        raise RolloutError(f"{label} must name a directory: {value}")
    return resolved.relative_to(root).as_posix(), resolved


def _exact_keys(value: dict, required: set[str], optional: set[str], label: str) -> None:
    missing = required - value.keys()
    extra = value.keys() - required - optional
    if missing:
        raise RolloutError(f"{label} is missing required field(s): {', '.join(sorted(missing))}")
    if extra:
        raise RolloutError(f"{label} has unsupported field(s): {', '.join(sorted(extra))}")


def _validate_target_fields(step: dict, number: int, *, ords: bool = False) -> None:
    label = f"step {number}"
    for field in ("connection", "expectedUser", "schema"):
        if not isinstance(step[field], str) or not step[field]:
            raise RolloutError(f"{label} {field} must be nonempty text")
    if ALIAS_RE.fullmatch(step["connection"]) is None:
        raise RolloutError(f"{label} connection must be a supported SQLcl saved-connection alias")
    if ORACLE_IDENTIFIER.fullmatch(step["expectedUser"]) is None:
        raise RolloutError(f"{label} expectedUser must be an uppercase Oracle identifier")
    if ORACLE_IDENTIFIER.fullmatch(step["schema"]) is None:
        raise RolloutError(f"{label} schema must be an uppercase Oracle identifier")
    if step["connection"] != step["expectedUser"]:
        raise RolloutError(
            f"{label} connection must exactly equal expectedUser for a reviewed standalone SQL script"
        )
    if ords and step["schema"] != step["expectedUser"]:
        raise RolloutError(f"{label} expectedUser must equal schema for an ORDS import")


def validate_step(step: object, number: int, repo_root: Path) -> dict:
    """Validate one step's strict schema and return its normalized form."""
    if not isinstance(step, dict):
        raise RolloutError(f"step {number} must be a JSON object")
    kind = step.get("type")
    if not isinstance(kind, str):
        raise RolloutError(f"step {number} must name a type")
    normalized = dict(step)

    if kind in {"migrate", "verify"}:
        required = {"type", "folders"}
        optional = {"phase"} if kind == "verify" else set()
        if kind == "verify":
            required.add("phase")
        _exact_keys(step, required, optional, f"step {number}")
        folders = step["folders"]
        if not isinstance(folders, list) or not folders or any(not isinstance(folder, str) for folder in folders):
            raise RolloutError(f"step {number} folders must be a nonempty ordered list of paths")
        normalized_folders = []
        for folder in folders:
            relative, path = _repo_file(folder, repo_root, f"step {number} migration folder", kind="directory")
            if not relative.startswith("migrations/"):
                raise RolloutError(f"step {number} folders must be under migrations/")
            if relative in normalized_folders:
                raise RolloutError(f"step {number} repeats migration folder {relative}")
            normalized_folders.append(relative)
            try:
                load_batch(repo_root, [relative])
            except (MigrationManifestError, OSError) as error:
                raise RolloutError(f"step {number} has an invalid migration folder {relative}: {error}") from error
        normalized["folders"] = normalized_folders
        if kind == "verify" and (not isinstance(step["phase"], str) or step["phase"] not in {"pre", "post", "both"}):
            raise RolloutError(f"step {number} phase must be pre, post, or both")
    elif kind in {"sql-script", "ords-import"}:
        required = {"type", "file", "connection", "expectedUser", "schema"}
        optional = {"excludeModules"} if kind == "ords-import" else set()
        _exact_keys(step, required, optional, f"step {number}")
        relative, _path = _repo_file(step["file"], repo_root, f"step {number} file", kind="file")
        normalized["file"] = relative
        _validate_target_fields(step, number, ords=kind == "ords-import")
        if kind == "ords-import":
            exclusions = step.get("excludeModules", [])
            if not isinstance(exclusions, list) or any(not isinstance(item, str) or not item.strip() for item in exclusions):
                raise RolloutError(f"step {number} excludeModules must be a list of nonempty module names")
            if len(set(exclusions)) != len(exclusions):
                raise RolloutError(f"step {number} excludeModules must not repeat a name")
            if any(any(ord(char) < 32 for char in item) for item in exclusions):
                raise RolloutError(f"step {number} excludeModules cannot contain control characters")
            normalized["excludeModules"] = list(exclusions)
    elif kind == "app-deploy":
        _exact_keys(step, {"type", "appId"}, set(), f"step {number}")
        app_id = step["appId"]
        if type(app_id) is not int or not (1 <= app_id <= 999_999_999_999_999_999):
            raise RolloutError(f"step {number} appId must be a positive numeric ID of at most 18 digits")
    elif kind == "pause":
        _exact_keys(step, {"type", "message"}, set(), f"step {number}")
        if not isinstance(step["message"], str) or not step["message"].strip() or "\x00" in step["message"]:
            raise RolloutError(f"step {number} pause message must be nonempty text")
    else:
        raise RolloutError(f"step {number} has unsupported type {kind!r}")
    return normalized


def load_manifest(path: Path | str, repo_root: Path = ROOT) -> RolloutManifest:
    root = Path(repo_root).resolve(strict=True)
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        _assert_no_symlink(candidate, root)
        manifest_path = candidate.resolve(strict=True)
        manifest_path.relative_to(root)
    except (OSError, ValueError) as error:
        raise RolloutError(f"rollout manifest must be a regular file inside the repository: {path}") from error
    if not manifest_path.is_file():
        raise RolloutError(f"rollout manifest must be a regular file: {path}")
    raw = manifest_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raise RolloutError("rollout manifest must be UTF-8 without a byte-order mark")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_object_no_duplicates)
    except RolloutError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RolloutError(f"cannot parse rollout manifest: {error}") from error
    if not isinstance(document, dict):
        raise RolloutError("rollout manifest must be a JSON object")
    _exact_keys(document, {"schemaVersion", "steps"}, set(), "rollout manifest")
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != MANIFEST_SCHEMA_VERSION:
        raise RolloutError(f"rollout manifest schemaVersion must be {MANIFEST_SCHEMA_VERSION}")
    if not isinstance(document["steps"], list) or not document["steps"]:
        raise RolloutError("rollout manifest steps must be a nonempty ordered list")
    steps = tuple(validate_step(step, index, root) for index, step in enumerate(document["steps"], start=1))
    return RolloutManifest(manifest_path, root, hashlib.sha256(raw).hexdigest(), steps)


def _find_app_directory(repo_root: Path, app_id: int) -> Path:
    apps = repo_root / "apps"
    candidates = []
    if apps.exists():
        _assert_no_symlink(apps, repo_root)
    direct = apps / str(app_id)
    if direct.is_dir():
        _assert_no_symlink(direct, repo_root)
        candidates.append(direct)
    if apps.is_dir():
        for path in apps.glob(f"*/{app_id}"):
            if path.is_dir():
                _assert_no_symlink(path, repo_root)
                candidates.append(path)

    requested_schema = os.environ.get("PROJECT_SCHEMA", "").strip()
    configured_schemas = [schema.strip() for schema in os.environ.get("APEX_PARSING_SCHEMA", "").split(",") if schema.strip()]
    preferred = [requested_schema] if requested_schema else (configured_schemas if len(configured_schemas) == 1 else [])
    for schema in preferred:
        candidate = apps / schema / str(app_id)
        if candidate.is_dir():
            _assert_no_symlink(candidate, repo_root)
            return candidate.resolve(strict=True)
    unique = sorted({candidate.resolve(strict=True) for candidate in candidates}, key=lambda item: item.as_posix())
    if not unique:
        raise RolloutError(f"no application source directory found for id {app_id} under apps/<schema>/{app_id}")
    if len(unique) != 1:
        raise RolloutError(f"application id {app_id} resolves to multiple source directories; select --schema")
    return unique[0]


def _read_app_inputs(repo_root: Path, app_id: int, environment: str) -> tuple[str, Path]:
    if environment not in {"staging", "prod"}:
        raise RolloutError("app-deploy is supported only for staging and production")
    app_dir = _find_app_directory(repo_root, app_id)
    _assert_no_symlink(app_dir, repo_root)
    try:
        validated = validate_app_source(repo_root, app_dir)
        descriptor = app_dir / "deployments" / f"{environment}.json"
        _assert_no_symlink(descriptor, repo_root)
        if not descriptor.is_file():
            raise RolloutError(f"deployment descriptor not found: {descriptor.relative_to(repo_root).as_posix()}")
        read_descriptor(descriptor, app_id)
    except (OSError, ValueError) as error:
        if isinstance(error, RolloutError):
            raise
        raise RolloutError(f"application {app_id} failed local deployment preflight: {error}") from error
    return validated.relative_to(repo_root).as_posix(), validated


def _directory_input_paths(
    path: Path,
    root: Path,
    environment: str,
    *,
    migration_folder: bool = False,
    app_source: bool = False,
) -> list[Path]:
    try:
        entries = sorted(path.rglob("*"), key=lambda item: item.as_posix())
    except OSError as error:
        raise RolloutError(f"cannot enumerate rollout source directory {path}: {error}") from error
    files = []
    for entry in entries:
        if entry.is_symlink():
            raise RolloutError(f"symbolic links are not allowed in rollout inputs: {entry}")
        if entry.is_dir():
            continue
        if not entry.is_file():
            raise RolloutError(f"rollout input is not a regular file: {entry}")
        relative = entry.relative_to(path)
        if migration_folder and len(relative.parts) == 1 and relative.name == f"status.{environment}.json":
            # This is a local apply receipt, not migration source.
            continue
        if app_source and relative.as_posix() == "apex-team-export.json":
            # This is local export baseline metadata, not imported APEX source.
            continue
        files.append(entry)
    return files


def _step_input_paths(step: dict, root: Path, environment: str) -> list[Path]:
    kind = step["type"]
    if kind in {"migrate", "verify"}:
        paths = []
        for folder in step["folders"]:
            _relative, directory = _repo_file(folder, root, "migration folder", kind="directory")
            paths.extend(_directory_input_paths(directory, root, environment, migration_folder=True))
        return paths
    if kind in {"sql-script", "ords-import"}:
        _relative, path = _repo_file(step["file"], root, "script file", kind="file")
        return [path]
    if kind == "app-deploy":
        _relative, app_dir = _read_app_inputs(root, step["appId"], environment)
        return _directory_input_paths(app_dir, root, environment, app_source=True)
    return []


def _input_hashes(paths: Sequence[Path], root: Path) -> list[dict[str, str]]:
    result = []
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        _assert_no_symlink(path, root)
        if not path.is_file():
            raise RolloutError(f"rollout input is not a regular file: {path}")
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as error:
            raise RolloutError(f"cannot hash rollout input {path}: {error}") from error
        result.append({"path": path.relative_to(root).as_posix(), "sha256": digest})
    return result


def _canonical_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _app_target_details(schema: str, environment: str) -> tuple[str, str]:
    prefix = "STAGING_" if environment == "staging" else "PROD_"
    schemas = [item.strip() for item in os.environ.get(prefix + "SCHEMA", "").split(",") if item.strip()]
    index = schemas.index(schema) if schema in schemas else (0 if len(schemas) <= 1 else None)

    def configured_value(key: str) -> str:
        values = [item.strip() for item in os.environ.get(prefix + key, "").split(",") if item.strip()]
        if index is None:
            return "<schema mapping unavailable>"
        if index < len(values):
            return values[index]
        if len(values) == 1:
            return values[0]
        return "<not configured>"

    return configured_value("SQLCL_CONNECTION"), configured_value("EXPECTED_USER")


def _describe_step(step: dict, repo_root: Path, environment: str) -> str:
    kind = step["type"]
    if kind in {"migrate", "verify"}:
        result = f"{kind}: {', '.join(step['folders'])}"
        return result + (f" ({step['phase']})" if kind == "verify" else "")
    if kind in {"sql-script", "ords-import"}:
        return (
            f"{kind}: {step['file']} via {step['connection']} as {step['expectedUser']} "
            f"with current schema {step['schema']}"
        )
    if kind == "app-deploy":
        app_id = step["appId"]
        _relative, app_dir = _read_app_inputs(repo_root, app_id, environment)
        descriptor = read_descriptor(app_dir / "deployments" / f"{environment}.json", app_id)
        workspace = descriptor["workspace"]["name"]
        schema = descriptor["app"]["databaseSession"]["parsingSchema"]
        connection, expected_user = _app_target_details(schema, environment)
        return (
            f"app-deploy: application {app_id} to workspace {workspace}, parsing schema {schema}, "
            f"connection {connection}, expected user {expected_user}"
        )
    return f"pause: {step['message']}"


def _prepare_plan(manifest: RolloutManifest, environment: str) -> list[dict]:
    plan = []
    for number, step in enumerate(manifest.steps, start=1):
        if step["type"] in {"migrate", "verify"}:
            try:
                load_batch(manifest.repo_root, step["folders"])
            except (MigrationManifestError, OSError) as error:
                raise RolloutError(f"step {number} migration batch is invalid: {error}") from error
        paths = _step_input_paths(step, manifest.repo_root, environment)
        hashes = _input_hashes(paths, manifest.repo_root)
        if step["type"] in {"sql-script", "ords-import"}:
            script = _read_script(manifest.repo_root / step["file"], f"step {number} script")
            if step["type"] == "sql-script":
                validate_sql_script(script, number)
            else:
                validate_ords_script(script, step["schema"], number)
                if step.get("excludeModules"):
                    exclude_ords_modules(script, step["excludeModules"], step["schema"])
        step_hash = _canonical_hash({"step": step, "inputHashes": hashes})
        plan.append({
            "number": number,
            "type": step["type"],
            "step": step,
            "description": _describe_step(step, manifest.repo_root, environment),
            "sha256": step_hash,
            "inputHashes": hashes,
        })
    return plan


def _read_script(path: Path, label: str, *, expected_sha256: str | None = None) -> str:
    try:
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf"):
            raise RolloutError(f"{label} must be UTF-8 without a byte-order mark")
        if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise RolloutError(f"{label} does not match the confirmed SHA-256")
        return raw.decode("utf-8")
    except RolloutError:
        raise
    except (OSError, UnicodeError) as error:
        raise RolloutError(f"cannot read {label} as strict UTF-8: {error}") from error


def validate_sql_script(source: str, number: int = 1) -> None:
    if not source.strip():
        raise RolloutError(f"step {number} SQL script is empty")
    masked = mask_sql(source)
    match = FORBIDDEN_SQLCL_COMMAND.search(masked)
    if match:
        directive = match.group(0).strip().split()[0]
        raise RolloutError(
            f"step {number} SQL script contains unsupported SQLcl directive {directive}; "
            "connection changes, nested files, client exits and guard overrides are refused"
        )
    if ROLLOUT_MARKER_LITERAL.search(source):
        raise RolloutError(f"step {number} SQL script cannot emit reserved ROLLOUT_ verification markers")
    if CURRENT_SCHEMA_CHANGE.search(source):
        raise RolloutError(f"step {number} SQL script cannot change CURRENT_SCHEMA after the identity guard")


def validate_ords_script(source: str, schema: str, number: int = 1) -> None:
    try:
        check_structure(source, schema, f"step {number} ORDS import")
        check_no_oauth(source, f"step {number} ORDS import")
    except OrdsExportError as error:
        raise RolloutError(str(error)) from error
    validate_sql_script(source, number)


def _read_string_literal(source: str, position: int) -> tuple[str, int]:
    if position >= len(source) or source[position] != "'":
        raise RolloutError("ORDS module exclusion requires literal p_module_name arguments")
    position += 1
    characters = []
    while position < len(source):
        if source[position] == "'":
            if source[position + 1 : position + 2] == "'":
                characters.append("'")
                position += 2
                continue
            return "".join(characters), position + 1
        characters.append(source[position])
        position += 1
    raise RolloutError("ORDS module name has an unterminated string literal")


def _ords_module_calls(source: str) -> list[dict]:
    masked = mask_sql(source)
    calls = []
    for match in ORDS_CALL.finditer(masked):
        opening = masked.find("(", match.start(), match.end())
        depth = 0
        closing = None
        for index in range(opening, len(masked)):
            if masked[index] == "(":
                depth += 1
            elif masked[index] == ")":
                depth -= 1
                if depth == 0:
                    closing = index
                    break
        if closing is None:
            raise RolloutError("ORDS export has an unbalanced API call; module filtering is refused")
        tail = closing + 1
        while tail < len(masked) and masked[tail].isspace():
            tail += 1
        if tail >= len(masked) or masked[tail] != ";":
            raise RolloutError("ORDS export API call is not a complete PL/SQL statement; module filtering is refused")
        masked_call = masked[match.start() : closing + 1]
        parameter = MODULE_PARAMETER.search(masked_call)
        module_name = None
        if parameter:
            literal_position = match.start() + parameter.end()
            while literal_position < len(source) and source[literal_position].isspace():
                literal_position += 1
            module_name, _literal_end = _read_string_literal(source, literal_position)
        name_match = re.search(r"\.([A-Z][A-Z0-9_]*)", match.group(0), re.IGNORECASE)
        calls.append({
            "name": name_match.group(1).upper() if name_match else "",
            "module": module_name,
            "start": match.start(),
            "end": tail + 1,
        })
    return calls


def exclude_ords_modules(source: str, module_names: Sequence[str], schema: str) -> tuple[str, list[str]]:
    """Remove complete generated ORDS API call statements tied to named modules."""
    requested = list(module_names)
    if not requested:
        validate_ords_script(source, schema)
        return source, []
    validate_ords_script(source, schema)
    calls = _ords_module_calls(source)
    for module in requested:
        if not any(call["name"] == "DEFINE_MODULE" and call["module"] == module for call in calls):
            raise RolloutError(f"ORDS module exclusion {module!r} does not match an exported module")
    ranges = [
        (call["start"], call["end"])
        for call in calls
        if call["module"] in requested
    ]
    filtered = source
    for start, end in sorted(ranges, reverse=True):
        filtered = filtered[:start] + filtered[end:]
    validate_ords_script(filtered, schema)
    remaining = _ords_module_calls(filtered)
    for module in requested:
        if any(call["module"] == module for call in remaining):
            raise RolloutError(f"ORDS module exclusion {module!r} could not be applied completely")
    return filtered, requested


def _snapshot(manifest: RolloutManifest, plan: Sequence[dict], boundary: str, environment: str) -> None:
    try:
        current_manifest_hash = hashlib.sha256(manifest.path.read_bytes()).hexdigest()
    except OSError as error:
        raise RolloutError(f"cannot recheck rollout manifest {manifest.path}: {error}") from error
    if current_manifest_hash != manifest.sha256:
        raise RolloutError(f"rollout manifest changed after rollout started {boundary}")

    for item in plan:
        step = item["step"]
        try:
            current_paths = _step_input_paths(step, manifest.repo_root, environment)
            current_hashes = _input_hashes(current_paths, manifest.repo_root)
        except (OSError, RolloutError, MigrationManifestError) as error:
            raise RolloutError(f"rollout input changed after rollout started {boundary}: {error}") from error
        if current_hashes != item["inputHashes"]:
            old = {entry["path"]: entry["sha256"] for entry in item["inputHashes"]}
            new = {entry["path"]: entry["sha256"] for entry in current_hashes}
            changed = sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path))
            changed_text = ", ".join(changed[:10])
            raise RolloutError(f"rollout input changed after rollout started {boundary}: {changed_text}")


def _report_paths(root: Path, report_path: str | Path | None) -> tuple[Path, Path]:
    if report_path is None:
        stem = root / "scratch" / f"rollout-report-{secrets.token_hex(6)}"
    else:
        requested = Path(report_path)
        if not requested.is_absolute():
            requested = root / requested
        suffix = requested.suffix.lower()
        stem = requested.with_suffix("") if suffix in {".json", ".md"} else requested
    json_path = stem.with_suffix(".json")
    markdown_path = stem.with_suffix(".md")
    canonical = []
    for path in (json_path, markdown_path):
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError) as error:
            raise RolloutError("report files must be written inside the repository") from error
        _assert_no_symlink(path.parent, root)
        if path.exists() and path.is_symlink():
            raise RolloutError(f"report output must not be a symbolic link: {path}")
        canonical.append(resolved)
    if json_path == markdown_path:
        raise RolloutError("JSON and Markdown report paths must be different")
    return tuple(canonical)


def _safe_scratch_root(root: Path) -> Path:
    scratch = root / "scratch"
    if scratch.is_symlink():
        raise RolloutError("scratch must not be a symbolic link")
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        scratch.chmod(0o700)
    except OSError as error:
        raise RolloutError(f"could not secure scratch directory: {error}") from error
    return scratch


def _safe_directory(path: Path, root: Path) -> None:
    _assert_no_symlink(path, root)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _assert_no_symlink(path, root)
    try:
        path.chmod(0o700)
    except OSError as error:
        raise RolloutError(f"could not secure rollout directory {path}: {error}") from error


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _markdown_report(report: dict) -> str:
    lines = [
        "# Rollout report",
        "",
        f"- Environment: `{report['environment']}`",
        f"- Status: **{report['status']}**",
        f"- Manifest: `{report['manifestPath']}`",
        f"- Manifest SHA-256: `{report['manifestSha256']}`",
        f"- Started: `{report['startedAt']}`",
        f"- Ended: `{report.get('endedAt') or 'not finished'}`",
        f"- Duration: `{report.get('durationSeconds')}` seconds",
        "",
        "| Step | Type | Status | SHA-256 | Start | End | Seconds | Evidence |",
        "| ---: | --- | --- | --- | --- | --- | ---: | --- |",
    ]
    for step in report.get("steps", []):
        evidence = ", ".join(f"`{path}`" for path in step.get("evidencePaths", [])) or "—"
        lines.append(
            f"| {step['number']} | `{step['type']}` | {step['status']} | `{step['sha256']}` | "
            f"{step.get('startedAt') or '—'} | {step.get('endedAt') or '—'} | "
            f"{step.get('durationSeconds') if step.get('durationSeconds') is not None else '—'} | {evidence} |"
        )
    if report.get("error"):
        lines.extend(("", "## Error", "", report["error"]))
    if report.get("confirmationPath"):
        lines.extend(("", f"Confirmation evidence: `{report['confirmationPath']}`"))
    lines.append("")
    return "\n".join(lines)


def _write_reports(report: dict, paths: tuple[Path, Path]) -> None:
    json_path, markdown_path = paths
    report["reportFiles"] = {
        "json": json_path.relative_to(report["repoRoot"]).as_posix(),
        "markdown": markdown_path.relative_to(report["repoRoot"]).as_posix(),
    }
    json_text = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    markdown_text = _markdown_report(report)
    _write_atomic(json_path, json_text)
    _write_atomic(markdown_path, markdown_text)


def _write_confirmation_evidence(
    path: Path,
    manifest: RolloutManifest,
    plan: Sequence[dict],
    environment: str,
) -> None:
    evidence = {
        "schemaVersion": 1,
        "status": "confirmed",
        "manifestPath": manifest.path.relative_to(manifest.repo_root).as_posix(),
        "manifestSha256": manifest.sha256,
        "environment": environment,
        "confirmedAt": _utc_now(),
        "steps": [
            {
                "number": item["number"],
                "sha256": item["sha256"],
                "inputHashes": item["inputHashes"],
            }
            for item in plan
        ],
    }
    _write_atomic(path, json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _receipt_path(root: Path, manifest_sha256: str, environment: str, number: int) -> Path:
    return root / "scratch" / "rollout-receipts" / manifest_sha256 / environment / f"step-{number:03d}.json"


def _write_receipt(root: Path, environment: str, manifest: RolloutManifest, plan_item: dict, step_report: dict) -> Path:
    path = _receipt_path(root, manifest.sha256, environment, plan_item["number"])
    _safe_scratch_root(root)
    _safe_directory(path.parent, root)
    receipt = {
        "schemaVersion": RECEIPT_SCHEMA_VERSION,
        "status": "succeeded",
        "environment": environment,
        "manifestSha256": manifest.sha256,
        "stepNumber": plan_item["number"],
        "stepType": plan_item["type"],
        "stepSha256": plan_item["sha256"],
        "inputHashes": plan_item["inputHashes"],
        "startedAt": step_report["startedAt"],
        "endedAt": step_report["endedAt"],
        "durationSeconds": step_report["durationSeconds"],
        "evidencePaths": step_report["evidencePaths"],
    }
    _write_atomic(path, json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return path


def _verify_receipt(root: Path, manifest: RolloutManifest, environment: str, plan_item: dict) -> tuple[dict, Path]:
    path = _receipt_path(root, manifest.sha256, environment, plan_item["number"])
    _assert_no_symlink(path, root)
    if path.is_symlink() or not path.is_file():
        raise RolloutError(f"step {plan_item['number']} has no matching successful receipt: {path.relative_to(root)}")
    receipt = _read_json(path, f"step {plan_item['number']} receipt")
    expected = {
        "schemaVersion": RECEIPT_SCHEMA_VERSION,
        "status": "succeeded",
        "environment": environment,
        "manifestSha256": manifest.sha256,
        "stepNumber": plan_item["number"],
        "stepType": plan_item["type"],
        "stepSha256": plan_item["sha256"],
        "inputHashes": plan_item["inputHashes"],
    }
    required_fields = set(expected) | {"startedAt", "endedAt", "durationSeconds", "evidencePaths"}
    if (
        not isinstance(receipt, dict)
        or set(receipt) != required_fields
        or any(receipt.get(key) != value for key, value in expected.items())
    ):
        raise RolloutError(f"step {plan_item['number']} receipt does not match this manifest, environment and input hashes")
    try:
        started_at = datetime.fromisoformat(receipt["startedAt"].replace("Z", "+00:00"))
        ended_at = datetime.fromisoformat(receipt["endedAt"].replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise RolloutError(f"step {plan_item['number']} receipt has invalid execution times") from error
    if started_at.tzinfo is None or ended_at.tzinfo is None or ended_at < started_at:
        raise RolloutError(f"step {plan_item['number']} receipt has invalid execution times")
    duration = receipt["durationSeconds"]
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        raise RolloutError(f"step {plan_item['number']} receipt has an invalid duration")
    evidence_paths = receipt["evidencePaths"]
    if not isinstance(evidence_paths, list) or any(not isinstance(item, str) for item in evidence_paths):
        raise RolloutError(f"step {plan_item['number']} receipt has invalid evidence paths")
    for item in evidence_paths:
        components = item.split("/")
        if not item or "\\" in item or any(part in {"", ".", ".."} for part in components) or PurePosixPath(item).is_absolute():
            raise RolloutError(f"step {plan_item['number']} receipt has invalid evidence paths")
    return receipt, path


def _confirmation_text(manifest: RolloutManifest, plan: Sequence[dict], environment: str, from_step: int) -> str:
    lines = [
        f"Rollout manifest SHA-256: {manifest.sha256}",
        f"Target environment: {environment.upper()}",
        "Every step and source file is frozen by the hashes below:",
    ]
    for item in plan:
        state = "already receipted" if item["number"] < from_step else "to run"
        lines.append(f"Step {item['number']:02d} [{state}] {item['description']} — SHA-256 {item['sha256']}")
        for file in item["inputHashes"]:
            lines.append(f"  {file['path']} — SHA-256 {file['sha256']}")
    lines.append("Continue with this rollout? [y/N]")
    return "\n".join(lines)


def _confirm_from_terminal(prompt: str) -> bool:
    try:
        print(prompt)
        return input().strip().casefold() in {"y", "yes"}
    except (EOFError, OSError):
        return False


def _execution_id() -> str:
    return f"rollout-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(5)}"


def _relative_paths(paths: Sequence[Path | str], root: Path) -> tuple[str, ...]:
    result = []
    for item in paths:
        path = Path(item)
        if not path.is_absolute():
            path = root / path
        try:
            result.append(path.resolve(strict=False).relative_to(root).as_posix())
        except ValueError:
            result.append(str(item))
    return tuple(result)


def _capture_call(log_path: Path, action: Callable[[], int]) -> tuple[int, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = int(action())
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt):
            status = 130
        else:
            status = 2
            stderr.write(f"rollout step error: {error}\n")
    output = stdout.getvalue() + stderr.getvalue()
    _write_atomic(log_path, output)
    return status, output


def _output_summary(output: str, *, limit: int = 800) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    summary = " | ".join(lines[-3:])
    if len(summary) > limit:
        summary = summary[-limit:]
    return summary


def _json_report_from_output(output: str) -> dict | None:
    json_start = output.find("{")
    if json_start < 0:
        return None
    try:
        value, _consumed = json.JSONDecoder().raw_decode(output[json_start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _verify_output_summary(output: str, *, limit: int = 800) -> str:
    report = _json_report_from_output(output)
    if report is None:
        return _output_summary(output, limit=limit)

    details: list[str] = []
    checks = report.get("checks")
    if isinstance(checks, list):
        failed_checks = [
            item for item in checks
            if isinstance(item, dict) and item.get("status") in {"false", "error"}
        ]
        for item in failed_checks[:5]:
            location = "/".join(
                str(item[key]) for key in ("folder", "phase", "id") if item.get(key)
            )
            status = item.get("status", "failed")
            detail = f"{location}={status}" if location else str(status)
            if item.get("error"):
                detail += f" ({item['error']})"
            elif status == "false":
                detail += f" (expected {item.get('expected')!s}, observed {item.get('observed')!s})"
            details.append(detail)
        if len(failed_checks) > 5:
            details.append(f"and {len(failed_checks) - 5} more failed checks")

    errors = report.get("errors")
    if isinstance(errors, list):
        for item in errors[:5]:
            if not isinstance(item, dict):
                continue
            location = "/".join(
                str(item[key]) for key in ("folder", "phase", "id") if item.get(key)
            )
            message = str(item.get("message", item.get("code", "verification error")))
            details.append(f"{location}: {message}" if location else message)
        if len(errors) > 5:
            details.append(f"and {len(errors) - 5} more verifier errors")

    summary = "; ".join(details) if details else _output_summary(output, limit=limit)
    if len(summary) > limit:
        summary = summary[: limit - 3].rstrip() + "..."
    return summary


def _planned_input_hashes(plan: Sequence[dict], step_number: int) -> dict[str, str]:
    item = next((entry for entry in plan if entry["number"] == step_number), None)
    if item is None:
        raise RolloutError(f"step {step_number} is missing from the frozen rollout plan")
    return {entry["path"]: entry["sha256"] for entry in item["inputHashes"]}


def _script_driver(
    run_dir: Path,
    payload: str,
    *,
    environment: str,
    schema: str,
    expected_user: str,
    step_number: int,
) -> Path:
    payload_path = run_dir / "payload.sql"
    payload_path.write_text(payload, encoding="utf-8", newline="\n")
    try:
        payload_path.chmod(0o600)
    except OSError as error:
        raise RolloutError(f"could not secure SQL rollout payload ({error})") from error
    driver = run_dir / "rollout.sql"
    driver.write_text(
        "\n".join((
            "SET DEFINE OFF",
            "SET SERVEROUTPUT ON SIZE UNLIMITED",
            "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
            "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
            "DECLARE",
            f"  c_target_environment CONSTANT VARCHAR2(16) := '{environment}';",
            "  v_session_user VARCHAR2(128) := SYS_CONTEXT('USERENV', 'SESSION_USER');",
            "  v_database_name VARCHAR2(128) := SYS_CONTEXT('USERENV', 'DB_NAME');",
            "  v_db_unique_name VARCHAR2(128) := SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME');",
            "  v_service_name VARCHAR2(256) := SYS_CONTEXT('USERENV', 'SERVICE_NAME');",
            "  v_owner_count PLS_INTEGER;",
            "  c_production_marker CONSTANT VARCHAR2(256) :=",
            "    '(^|[^[:alnum:]])(production|live)[[:digit:]]*([^[:alnum:]]|$)|(prod|prd)[[:digit:]]*([^[:alnum:]]|$)|(^|[^[:alnum:]])(prod|prd)(db|[[:digit:]])';",
            "  c_non_production_marker CONSTANT VARCHAR2(64) := '(pre|non)[-_.]?(prod|prd)';",
            "  FUNCTION resembles_production(p_name VARCHAR2) RETURN BOOLEAN IS",
            "  BEGIN",
            "    RETURN REGEXP_LIKE(REGEXP_REPLACE(p_name, c_non_production_marker, ' ', 1, 0, 'i'), c_production_marker, 'i');",
            "  END;",
            "BEGIN",
            f"  IF UPPER(v_session_user) <> '{expected_user}' THEN",
            "    RAISE_APPLICATION_ERROR(-20071, 'rollout identity mismatch');",
            "  END IF;",
            "  IF v_database_name IS NULL OR v_db_unique_name IS NULL THEN",
            "    RAISE_APPLICATION_ERROR(-20072, 'rollout database identity is unavailable');",
            "  END IF;",
            "  SELECT COUNT(*) INTO v_owner_count FROM all_users WHERE username = '" + schema + "';",
            "  IF v_owner_count <> 1 THEN",
            "    RAISE_APPLICATION_ERROR(-20073, 'rollout target schema is not visible');",
            "  END IF;",
            "  IF c_target_environment <> 'prod' AND (",
            "       resembles_production(v_database_name)",
            "       OR resembles_production(v_db_unique_name)",
            "       OR resembles_production(v_service_name)) THEN",
            "    RAISE_APPLICATION_ERROR(-20074, 'rollout target identity resembles production');",
            "  END IF;",
            "END;",
            "/",
            f"ALTER SESSION SET CURRENT_SCHEMA = {schema};",
            "DECLARE",
            "  v_session_user VARCHAR2(128) := SYS_CONTEXT('USERENV', 'SESSION_USER');",
            "  v_current_schema VARCHAR2(128) := SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA');",
            "  v_database_name VARCHAR2(128) := SYS_CONTEXT('USERENV', 'DB_NAME');",
            "  v_db_unique_name VARCHAR2(128) := SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME');",
            "  v_service_name VARCHAR2(256) := SYS_CONTEXT('USERENV', 'SERVICE_NAME');",
            "BEGIN",
            f"  IF UPPER(v_session_user) <> '{expected_user}' OR UPPER(v_current_schema) <> '{schema}' THEN",
            "    RAISE_APPLICATION_ERROR(-20071, 'rollout identity mismatch');",
            "  END IF;",
            "  DBMS_OUTPUT.PUT_LINE('ROLLOUT_IDENTITY:' || UPPER(v_session_user) || ':' || UPPER(v_current_schema));",
            "  DBMS_OUTPUT.PUT_LINE('ROLLOUT_TARGET:' || UPPER(NVL(v_database_name, '<NO_DB_NAME>')) || ':' || UPPER(NVL(v_db_unique_name, '<NO_DB_UNIQUE_NAME>')) || ':' || UPPER(NVL(v_service_name, '<NO_SERVICE>')));",
            "END;",
            "/",
            "@@payload.sql",
            f"PROMPT ROLLOUT_SCRIPT_COMPLETED:{step_number:04d}",
            "EXIT SUCCESS ROLLBACK",
            "",
        )),
        encoding="utf-8",
        newline="\n",
    )
    try:
        driver.chmod(0o600)
    except OSError as error:
        raise RolloutError(f"could not secure SQL rollout driver ({error})") from error
    return driver


def _execute_sql_payload(
    step: dict,
    *,
    environment: str,
    repo_root: Path,
    evidence_dir: Path,
    step_number: int,
    source: str,
    ords: bool = False,
) -> StepResult:
    validate_sql_script(source, step_number)
    if ords:
        validate_ords_script(source, step["schema"], step_number)
    env_classification = {
        "dev": str(os.environ.get("DB_ENVIRONMENT", "development")).casefold(),
        "staging": "staging",
        "prod": "production",
    }[environment]
    if environment == "dev" and env_classification not in {"development", "test"}:
        raise RolloutError("DEV SQL-script steps require DB_ENVIRONMENT=development or test")
    if looks_like_production_identity(step["connection"], step["schema"]) and environment != "prod":
        raise RolloutError("SQL-script target looks like production but --env is not prod")

    evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        evidence_dir.chmod(0o700)
    except OSError as error:
        raise RolloutError(f"could not secure SQL rollout evidence directory ({error})") from error
    driver = _script_driver(
        evidence_dir,
        source,
        environment=environment,
        schema=step["schema"],
        expected_user=step["expectedUser"],
        step_number=step_number,
    )
    target = Target(environment, step["connection"], step["expectedUser"], step["schema"], env_classification)
    try:
        result = run_sqlcl(target, driver, evidence_dir, phase="apply")
    except SqlclError as error:
        summary = _output_summary(error.output)
        message = str(error) + (f": {summary}" if summary else "")
        return StepResult(2, _relative_paths((driver, evidence_dir / "payload.sql", evidence_dir / "sqlcl-output.log"), repo_root), message)

    output = result.output
    identities = re.findall(r"(?m)^\s*ROLLOUT_IDENTITY:([A-Z][A-Z0-9_$#]*):([A-Z][A-Z0-9_$#]*)\s*$", output)
    targets = re.findall(r"(?m)^\s*ROLLOUT_TARGET:([A-Z0-9_.<>-]+):([A-Z0-9_.<>-]+):([A-Z0-9_.<>-]+)\s*$", output)
    completions = re.findall(rf"(?m)^\s*ROLLOUT_SCRIPT_COMPLETED:{step_number:04d}\s*$", output)
    if identities != [(step["expectedUser"], step["schema"])]:
        return StepResult(2, _relative_paths((driver, evidence_dir / "payload.sql", evidence_dir / "sqlcl-output.log"), repo_root), "SQLcl did not print exactly the expected session user and current schema")
    if len(completions) != 1:
        return StepResult(2, _relative_paths((driver, evidence_dir / "payload.sql", evidence_dir / "sqlcl-output.log"), repo_root), "SQLcl did not confirm exactly one completed script step")
    if len(targets) != 1 or any(value.startswith("<NO_") for value in targets[0]):
        return StepResult(2, _relative_paths((driver, evidence_dir / "payload.sql", evidence_dir / "sqlcl-output.log"), repo_root), "SQLcl did not report a complete database and service identity")
    if environment != "prod" and looks_like_production_identity(*targets[0]):
        return StepResult(2, _relative_paths((driver, evidence_dir / "payload.sql", evidence_dir / "sqlcl-output.log"), repo_root), "observed SQLcl database/service identity resembles production while the selected environment is not prod")
    return StepResult(0, _relative_paths((driver, evidence_dir / "payload.sql", evidence_dir / "sqlcl-output.log"), repo_root), "script completed with the expected identity")


def _execute_app_deploy(
    step: dict,
    *,
    environment: str,
    repo_root: Path,
    evidence_dir: Path,
    manifest_confirmed: bool,
    expected_input_hashes: dict[str, str],
) -> StepResult:
    if environment not in {"staging", "prod"}:
        raise RolloutError("app-deploy is supported only for staging and production")
    if not manifest_confirmed:
        raise RolloutError("app-deploy requires the staging or production manifest confirmation")
    evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    app_relative, _app_dir = _read_app_inputs(repo_root, step["appId"], environment)
    staged_app_dir = evidence_dir / "app-source" / Path(app_relative)
    staged_app_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    app_prefix = app_relative + "/"
    staged_files = 0
    for relative, expected_sha256 in sorted(expected_input_hashes.items()):
        if not relative.startswith(app_prefix):
            raise RolloutError(f"application input is outside its source directory: {relative}")
        source_path = repo_root / relative
        relative_inside_app = Path(relative[len(app_prefix):])
        try:
            raw = source_path.read_bytes()
        except OSError as error:
            raise RolloutError(f"cannot freeze application input {relative}: {error}") from error
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise RolloutError(f"application input does not match the confirmed SHA-256: {relative}")
        staged_path = staged_app_dir / relative_inside_app
        staged_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staged_path.write_bytes(raw)
        try:
            staged_path.chmod(0o600)
        except OSError as error:
            raise RolloutError(f"could not secure frozen application input {relative}: {error}") from error
        if hashlib.sha256(staged_path.read_bytes()).hexdigest() != expected_sha256:
            raise RolloutError(f"frozen application input did not retain the confirmed bytes: {relative}")
        staged_files += 1
    if staged_files == 0:
        raise RolloutError("app-deploy has no frozen application source files")

    child_env = dict(os.environ)
    log_path = evidence_dir / "deploy-output.log"
    # deploy.sh is a Bash entry point. On Windows, `sys.executable` is Python, so
    # launch Git Bash by its resolved path through the project's shared helper.
    from scripts.sqlcl_session import bash_command
    command = [
        bash_command(), (repo_root / "scripts" / "deploy.sh").as_posix(), str(step["appId"]),
        "--env", environment, "--app-source-dir", staged_app_dir.as_posix(),
    ]
    try:
        # The rollout's single manifest confirmation is already held in memory;
        # feed it to deploy.sh's existing target prompt for this child run.
        completed = subprocess.run(
            command,
            cwd=repo_root,
            env=child_env,
            input="y\n",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        _write_atomic(log_path, f"unable to start the existing deploy path: {error}\n")
        return StepResult(2, _relative_paths((log_path,), repo_root), str(error))
    _write_atomic(log_path, completed.stdout or "")
    return StepResult(
        completed.returncode,
        _relative_paths((staged_app_dir, log_path), repo_root),
        "existing deployment path completed" if completed.returncode == 0 else (
            f"deploy.sh exited with status {completed.returncode}"
            + (f": {_output_summary(completed.stdout or '')}" if _output_summary(completed.stdout or "") else "")
        ),
    )


def execute_step(
    step: dict,
    *,
    environment: str,
    repo_root: Path,
    evidence_dir: Path,
    step_number: int,
    manifest: RolloutManifest,
    plan: Sequence[dict],
    manifest_confirmed: bool,
) -> StepResult:
    """Execute one step using existing command or SQLcl paths."""
    kind = step["type"]
    log_path = evidence_dir / "step-output.log"
    if kind in {"migrate", "verify"}:
        arguments = [*step["folders"], "--env", environment]
        expected_input_hashes = _planned_input_hashes(plan, step_number)
        if kind == "migrate":
            def action() -> int:
                def require_rollout_confirmation(_prompt: str) -> bool:
                    if environment not in {"staging", "prod"}:
                        return False
                    if not manifest_confirmed:
                        return False
                    _snapshot(manifest, plan, "before migration write confirmation", environment)
                    return True

                return migrate.main(
                    arguments,
                    environ=os.environ,
                    repo_root=repo_root,
                    confirm=require_rollout_confirmation,
                    expected_input_hashes=expected_input_hashes,
                )
        else:
            arguments.extend(("--phase", step["phase"], "--format", "json"))

            def action() -> int:
                return verify_checks.main(
                    arguments,
                    environ=os.environ,
                    repo_root=repo_root,
                    expected_input_hashes=expected_input_hashes,
                )

        status, output = _capture_call(log_path, action)
        evidence = [log_path.relative_to(repo_root).as_posix()]
        if kind == "verify":
            verifier_report = _json_report_from_output(output)
            if verifier_report and verifier_report.get("evidenceDir"):
                evidence.extend(_relative_paths((verifier_report["evidenceDir"],), repo_root))
        if status == 0:
            message = f"{kind} step passed"
        else:
            summary = _verify_output_summary(output) if kind == "verify" else _output_summary(output)
            message = f"{kind} returned status {status}" + (f": {summary}" if summary else "")
        return StepResult(status, tuple(evidence), message)
    if kind in {"sql-script", "ords-import"}:
        if environment in {"staging", "prod"}:
            if not manifest_confirmed:
                raise RolloutError("staging and production SQL steps require the manifest-level confirmation")
            _snapshot(manifest, plan, f"before step {step_number}", environment)
        expected_hashes = _planned_input_hashes(plan, step_number)
        expected_script_hash = expected_hashes.get(step["file"])
        if expected_script_hash is None:
            raise RolloutError(f"step {step_number} script is missing from the frozen hash set")
        source = _read_script(
            repo_root / step["file"],
            f"step {step_number} script",
            expected_sha256=expected_script_hash,
        )
        removed = []
        if kind == "ords-import":
            validate_ords_script(source, step["schema"], step_number)
            source, removed = exclude_ords_modules(source, step.get("excludeModules", []), step["schema"])
        result = _execute_sql_payload(
            step,
            environment=environment,
            repo_root=repo_root,
            evidence_dir=evidence_dir,
            step_number=step_number,
            source=source,
            ords=kind == "ords-import",
        )
        if removed:
            return StepResult(result.exit_code, result.evidence_paths, f"excluded ORDS modules {', '.join(removed)}; {result.message}")
        return result
    if kind == "app-deploy":
        return _execute_app_deploy(
            step,
            environment=environment,
            repo_root=repo_root,
            evidence_dir=evidence_dir,
            manifest_confirmed=manifest_confirmed,
            expected_input_hashes=_planned_input_hashes(plan, step_number),
        )
    if kind == "pause":
        print(step["message"])
        try:
            input("Press Enter to continue the rollout...")
        except (EOFError, OSError):
            return StepResult(1, (), "pause was not acknowledged")
        return StepResult(0, (), "pause acknowledged")
    raise RolloutError(f"unsupported rollout step: {kind}")


def _report_step(plan_item: dict) -> dict:
    return {
        "number": plan_item["number"],
        "type": plan_item["type"],
        "description": plan_item["description"],
        "sha256": plan_item["sha256"],
        "inputHashes": plan_item["inputHashes"],
        "status": "not-run",
        "startedAt": None,
        "endedAt": None,
        "durationSeconds": None,
        "evidencePaths": [],
        "receiptPath": None,
        "message": None,
    }


def _append_unique_inputs(plan: Sequence[dict]) -> list[dict]:
    by_path = {}
    for step in plan:
        for item in step["inputHashes"]:
            by_path[item["path"]] = item["sha256"]
    return [{"path": path, "sha256": by_path[path]} for path in sorted(by_path)]


def _report_outputs_do_not_overlap(
    paths: tuple[Path, Path], plan: Sequence[dict], root: Path, manifest_path: Path, environment: str,
) -> None:
    output_paths = {path.resolve(strict=False) for path in paths}
    reserved_state_roots = (
        root / "scratch" / "rollout-receipts",
        root / "scratch" / "rollout-runs",
    )
    reserved_scratch_prefixes = (
        "migration-attempt-",
        "migration-verify-",
        "apex-publish.",
        "apex-lookup.",
        "sqlcl-doctor.",
        "db-backup.",
        "apex-export.",
        "apexlang-upgrade.",
    )
    input_paths = {root / item["path"] for plan_item in plan for item in plan_item["inputHashes"]}
    input_paths.add(manifest_path)
    source_roots = []
    for plan_item in plan:
        step = plan_item["step"]
        if step["type"] in {"migrate", "verify"}:
            source_roots.extend(root / folder for folder in step["folders"])
        elif step["type"] == "app-deploy":
            _relative, app_dir = _read_app_inputs(root, step["appId"], environment)
            source_roots.append(app_dir)
    for output in output_paths:
        if any(state_root == output or state_root in output.parents for state_root in reserved_state_roots):
            raise RolloutError(f"report output overlaps rollout recovery state: {output.relative_to(root)}")
        try:
            scratch_relative = output.relative_to(root / "scratch")
        except ValueError:
            scratch_relative = None
        if scratch_relative and scratch_relative.parts and any(
            scratch_relative.parts[0].startswith(prefix) for prefix in reserved_scratch_prefixes
        ):
            raise RolloutError(f"report output overlaps command recovery evidence: {output.relative_to(root)}")
        if output in input_paths or any(source_root == output or source_root in output.parents for source_root in source_roots):
            raise RolloutError(f"report output overlaps a rollout input: {output.relative_to(root)}")


def _finish_report(report: dict, started: float, status: str, exit_code: int) -> None:
    report["status"] = status
    report["exitCode"] = exit_code
    report["endedAt"] = _utc_now()
    report["durationSeconds"] = round(time.perf_counter() - started, 3)


def _announce_report_paths(paths: tuple[Path, Path], root: Path) -> None:
    json_path, markdown_path = paths
    print(
        "Rollout report: JSON "
        f"{json_path.relative_to(root).as_posix()}; Markdown {markdown_path.relative_to(root).as_posix()}"
    )


def run_rollout(
    manifest_path: Path | str,
    environment: str,
    *,
    from_step: int = 1,
    dry_run: bool = False,
    report_path: Path | str | None = None,
    repo_root: Path = ROOT,
    confirm: Callable[[str], bool] = _confirm_from_terminal,
) -> int:
    """Validate, hash, confirm, execute and report one rollout manifest."""
    started = time.perf_counter()
    started_at = _utc_now()
    root = Path(repo_root).resolve()
    manifest: RolloutManifest | None = None
    plan: list[dict] = []
    report_paths: tuple[Path, Path] | None = None
    report: dict | None = None
    try:
        if environment not in {"dev", "staging", "prod"}:
            raise RolloutError("--env must be dev, staging, or prod")
        if type(from_step) is not int or from_step < 1:
            raise RolloutError("--from-step must be a positive one-based step number")
        manifest = load_manifest(manifest_path, root)
        plan = _prepare_plan(manifest, environment)
        if from_step > len(plan):
            raise RolloutError(f"--from-step must be between 1 and {len(plan)}")
        report_paths = _report_paths(root, report_path)
        _report_outputs_do_not_overlap(report_paths, plan, root, manifest.path, environment)
        report = {
            "schemaVersion": REPORT_SCHEMA_VERSION,
            "repoRoot": str(root),
            "manifestPath": manifest.path.relative_to(root).as_posix(),
            "manifestSha256": manifest.sha256,
            "environment": environment,
            "status": "running",
            "exitCode": None,
            "startedAt": started_at,
            "endedAt": None,
            "durationSeconds": None,
            "inputHashes": _append_unique_inputs(plan),
            "steps": [_report_step(item) for item in plan],
            "reportFiles": {},
            "confirmationPath": None,
            "error": None,
        }
        if from_step > 1:
            _safe_scratch_root(root)
            for item, step_report in zip(plan[: from_step - 1], report["steps"][: from_step - 1], strict=False):
                receipt, receipt_path = _verify_receipt(root, manifest, environment, item)
                step_report.update({
                    "status": "resumed",
                    "startedAt": receipt["startedAt"],
                    "endedAt": receipt["endedAt"],
                    "durationSeconds": receipt["durationSeconds"],
                    "evidencePaths": receipt["evidencePaths"],
                    "receiptPath": receipt_path.relative_to(root).as_posix(),
                    "message": "verified prior rollout receipt",
                })

        if dry_run:
            report["steps"] = [dict(step, status="planned") for step in report["steps"]]
            report["error"] = None
            _finish_report(report, started, "planned", 0)
            print(f"Rollout dry run for {environment.upper()} (manifest SHA-256 {manifest.sha256})")
            for item in plan:
                print(f"Step {item['number']:02d}: {item['description']} — SHA-256 {item['sha256']}")
                for source in item["inputHashes"]:
                    print(f"  {source['path']} — SHA-256 {source['sha256']}")
            _write_reports(report, report_paths)
            _announce_report_paths(report_paths, root)
            return 0

        manifest_confirmed = False
        if environment in {"staging", "prod"}:
            try:
                approved = bool(confirm(_confirmation_text(manifest, plan, environment, from_step)))
            except (EOFError, OSError):
                approved = False
            if not approved:
                _finish_report(report, started, "declined", 1)
                _write_reports(report, report_paths)
                _announce_report_paths(report_paths, root)
                return 1
            manifest_confirmed = True
        scratch = _safe_scratch_root(root)
        rollout_runs = scratch / "rollout-runs"
        _safe_directory(rollout_runs, root)
        run_evidence = rollout_runs / _execution_id()
        _assert_no_symlink(run_evidence, root)
        run_evidence.mkdir(mode=0o700, exist_ok=False)
        _assert_no_symlink(run_evidence, root)
        try:
            run_evidence.chmod(0o700)
        except OSError as error:
            raise RolloutError(f"could not secure rollout directory: {error}") from error
        report["evidenceDir"] = run_evidence.relative_to(root).as_posix()
        if environment in {"staging", "prod"}:
            confirmation_path = run_evidence / "rollout-confirmation.json"
            _write_confirmation_evidence(confirmation_path, manifest, plan, environment)
            report["confirmationPath"] = confirmation_path.relative_to(root).as_posix()

        _write_reports(report, report_paths)
        for index in range(from_step - 1, len(plan)):
            item = plan[index]
            step_report = report["steps"][index]
            step_started = time.perf_counter()
            step_report["status"] = "running"
            step_report["startedAt"] = _utc_now()
            try:
                evidence_dir = run_evidence / f"step-{item['number']:03d}"
                _snapshot(manifest, plan, f"before step {item['number']}", environment)
                evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
                result = execute_step(
                    item["step"],
                    environment=environment,
                    repo_root=root,
                    evidence_dir=evidence_dir,
                    step_number=item["number"],
                    manifest=manifest,
                    plan=plan,
                    manifest_confirmed=manifest_confirmed,
                )
                if not isinstance(result, StepResult):
                    raise RolloutError("rollout step executor returned an invalid result")
                step_report["evidencePaths"] = list(result.evidence_paths)
                step_report["message"] = result.message
                if result.exit_code == 0:
                    _snapshot(manifest, plan, f"after step {item['number']}", environment)
                    step_report["endedAt"] = _utc_now()
                    step_report["durationSeconds"] = round(time.perf_counter() - step_started, 3)
                    receipt_path = _write_receipt(root, environment, manifest, item, {
                        **step_report,
                    })
                    step_report["receiptPath"] = receipt_path.relative_to(root).as_posix()
                    step_report["status"] = "succeeded"
                else:
                    step_report["status"] = "failed"
                    step_report["error"] = result.message or f"step returned status {result.exit_code}"
                    report["error"] = step_report["error"]
                    _finish_report(report, started, "failed", result.exit_code if result.exit_code in {1, 2, 130, 143} else 2)
                    step_report["endedAt"] = report["endedAt"]
                    step_report["durationSeconds"] = round(time.perf_counter() - step_started, 3)
                    _write_reports(report, report_paths)
                    print(f"Rollout step {item['number']:02d} failed: {step_report['error']}", file=sys.stderr)
                    _announce_report_paths(report_paths, root)
                    return report["exitCode"]
            except KeyboardInterrupt:
                step_report["status"] = "failed"
                step_report["error"] = "rollout was interrupted; inspect the step evidence before retrying"
                step_report["evidencePaths"] = _relative_paths((run_evidence / f"step-{item['number']:03d}",), root)
                report["error"] = step_report["error"]
                _finish_report(report, started, "failed", 130)
                step_report["endedAt"] = report["endedAt"]
                step_report["durationSeconds"] = round(time.perf_counter() - step_started, 3)
                _write_reports(report, report_paths)
                print(f"Rollout step {item['number']:02d} failed: {step_report['error']}", file=sys.stderr)
                _announce_report_paths(report_paths, root)
                return 130
            except Exception as error:
                step_report["status"] = "failed"
                step_report["error"] = str(error)
                step_report["evidencePaths"] = _relative_paths((run_evidence / f"step-{item['number']:03d}",), root)
                report["error"] = str(error)
                _finish_report(report, started, "failed", 2)
                step_report["endedAt"] = report["endedAt"]
                step_report["durationSeconds"] = round(time.perf_counter() - step_started, 3)
                _write_reports(report, report_paths)
                print(f"Rollout step {item['number']:02d} failed: {step_report['error']}", file=sys.stderr)
                _announce_report_paths(report_paths, root)
                return 2
            if step_report["endedAt"] is None:
                step_report["endedAt"] = _utc_now()
                step_report["durationSeconds"] = round(time.perf_counter() - step_started, 3)
            _write_reports(report, report_paths)

        _finish_report(report, started, "succeeded", 0)
        _write_reports(report, report_paths)
        _announce_report_paths(report_paths, root)
        return 0
    except KeyboardInterrupt:
        print("rollout error: interrupted; inspect the last step evidence before retrying", file=sys.stderr)
        if report is not None and report_paths is not None:
            report["error"] = "rollout interrupted; inspect the last step evidence before retrying"
            _finish_report(report, started, "failed", 130)
            _write_reports(report, report_paths)
            _announce_report_paths(report_paths, root)
        return 130
    except Exception as error:
        message = str(error)
        print(f"rollout error: {message}", file=sys.stderr)
        if report is not None and report_paths is not None:
            report["error"] = message
            _finish_report(report, started, "failed", 2)
            _write_reports(report, report_paths)
            _announce_report_paths(report_paths, root)
        return 2
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="ordered rollout manifest JSON, relative to the repository root")
    parser.add_argument("--env", required=True, choices=("dev", "staging", "prod"))
    parser.add_argument("--from-step", type=int, default=1, help="resume at a one-based step after verifying earlier receipts")
    parser.add_argument("--dry-run", action="store_true", help="validate and print the hashed plan without running steps")
    parser.add_argument("--report", type=Path, help="report base path or .json/.md path; both formats are written")
    args = parser.parse_args(argv)
    return run_rollout(
        args.manifest,
        args.env,
        from_step=args.from_step,
        dry_run=args.dry_run,
        report_path=args.report,
    )


if __name__ == "__main__":
    raise SystemExit(main())
