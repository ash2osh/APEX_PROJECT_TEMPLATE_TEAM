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
from collections.abc import Callable, Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import migrate, verify_checks
from scripts.db_targets import (
    Target,
    TargetResolutionError,
    batch_schema,
    looks_like_production_identity,
    resolve_target,
)
from scripts.deployment_descriptor import read_descriptor
from scripts.migration_manifest import MigrationManifestError, load_batch, validate_receipt as validate_migration_receipt
from scripts.local_config import ALLOWED_KEYS, ConfigError, read_project_env
from scripts.path_utils import relative_to_root
from scripts.ords_export import OrdsExportError, check_no_oauth, check_structure, mask_sql
from scripts.sqlcl_session import ALIAS_RE, SqlclError, run_sqlcl
from scripts.sqlcl_transport import generated_clob_assembly, lint_sqlcl_transport
from scripts.validate_app_source import validate_app_source
from scripts.validate_migration import FIRST_WORD, mask_comments_and_literals, statement_spans, validate_sql_only


ROOT = Path(__file__).resolve().parents[1]
ORACLE_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
MANIFEST_SCHEMA_VERSION = 1
REPORT_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 2
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
    schema_version: int = 1
    database_object_budget_seconds: int | None = None


@dataclass(frozen=True)
class StepResult:
    exit_code: int
    evidence_paths: tuple[str, ...] = ()
    message: str = ""
    observed_identity: dict[str, str] | None = None


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
        relative = relative_to_root(path, root)
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
        relative_to_root(resolved, root)
    except ValueError as error:
        raise RolloutError(f"{label} resolves outside the repository: {value}") from error
    if kind == "file" and not resolved.is_file():
        raise RolloutError(f"{label} must name a regular file: {value}")
    if kind == "directory" and not resolved.is_dir():
        raise RolloutError(f"{label} must name a directory: {value}")
    return relative_to_root(resolved, root).as_posix(), resolved


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


def validate_step(step: object, number: int, repo_root: Path, version: int = 1) -> dict:
    """Validate one step's strict schema and return its normalized form."""
    if type(version) is not int or version not in {1, 2}:
        raise RolloutError("rollout step validation version must be integer 1 or 2")
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
        required = {"type", "file", "schema"}
        optional: set[str] = set()
        if version == 1:
            required.update({"connection", "expectedUser"})
        if kind == "ords-import":
            optional.add("excludeModules")
        explicit_identity = {"connection", "expectedUser"} & set(step)
        if version == 2 and explicit_identity:
            raise RolloutError(
                f"step {number} schemaVersion 2 SQL/ORDS targets use a logical schema; remove connection/expectedUser "
                "and configure the selected environment's migration profile instead"
            )
        _exact_keys(step, required, optional, f"step {number}")
        relative, _path = _repo_file(step["file"], repo_root, f"step {number} file", kind="file")
        normalized["file"] = relative
        if version == 1:
            _validate_target_fields(step, number, ords=kind == "ords-import")
        elif not isinstance(step["schema"], str) or ORACLE_IDENTIFIER.fullmatch(step["schema"]) is None:
            raise RolloutError(f"step {number} schema must be an uppercase Oracle identifier")
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
    elif kind == "feature-migrate":
        if version != 2:
            raise RolloutError(f"step {number} feature-migrate requires rollout manifest schemaVersion 2")
        _exact_keys(step, {"type", "schema", "inputs"}, {"checks"}, f"step {number}")
        from scripts.feature_manifest import FeatureManifestError, load_feature_checkpoint

        try:
            checkpoint = load_feature_checkpoint(repo_root, step)
        except FeatureManifestError as error:
            raise RolloutError(f"step {number} has an invalid feature checkpoint: {error}") from error
        normalized["schema"] = checkpoint.schema
        normalized["inputs"] = [item.path for item in checkpoint.inputs]
        if "checks" in step:
            normalized["checks"] = list(step["checks"])
    else:
        raise RolloutError(f"step {number} has unsupported type {kind!r}")
    return normalized


def load_manifest(path: Path | str, repo_root: Path = ROOT, *, version: int | None = None) -> RolloutManifest:
    root = Path(repo_root).resolve(strict=True)
    if version is not None and (type(version) is not int or version not in {1, 2}):
        raise RolloutError("rollout manifest validation version must be integer 1 or 2")
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        _assert_no_symlink(candidate, root)
        manifest_path = candidate.resolve(strict=True)
        relative_to_root(manifest_path, root)
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
    if type(document.get("schemaVersion")) is not int or document["schemaVersion"] not in {1, 2}:
        raise RolloutError("rollout manifest schemaVersion must be integer 1 or 2")
    manifest_version = document["schemaVersion"]
    if version is not None and manifest_version != version:
        raise RolloutError(f"rollout manifest declares schemaVersion {manifest_version}, not requested version {version}")
    optional = {"databaseObjectBudgetSeconds"} if manifest_version == 2 else set()
    _exact_keys(document, {"schemaVersion", "steps"}, optional, "rollout manifest")
    budget = None
    if manifest_version == 2:
        budget = document.get("databaseObjectBudgetSeconds", 120)
        if type(budget) is not int or not 1 <= budget <= 300:
            raise RolloutError("rollout manifest databaseObjectBudgetSeconds must be an integer from 1 through 300")
    if not isinstance(document["steps"], list) or not document["steps"]:
        raise RolloutError("rollout manifest steps must be a nonempty ordered list")
    steps = tuple(
        validate_step(step, index, root, version=manifest_version)
        for index, step in enumerate(document["steps"], start=1)
    )
    if manifest_version == 2:
        from scripts import feature_manifest

        checkpoints = tuple(
            feature_manifest.load_feature_checkpoint(root, step)
            for step in steps
            if step["type"] == "feature-migrate"
        )
        try:
            feature_manifest.validate_release_units(checkpoints)
        except feature_manifest.FeatureManifestError as error:
            raise RolloutError(f"rollout manifest has invalid stored source identities: {error}") from error
    return RolloutManifest(
        manifest_path,
        root,
        hashlib.sha256(raw).hexdigest(),
        steps,
        schema_version=manifest_version,
        database_object_budget_seconds=budget,
    )


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
            raise RolloutError(f"deployment descriptor not found: {relative_to_root(descriptor, repo_root).as_posix()}")
        read_descriptor(descriptor, app_id)
    except (OSError, ValueError) as error:
        if isinstance(error, RolloutError):
            raise
        raise RolloutError(f"application {app_id} failed local deployment preflight: {error}") from error
    return relative_to_root(validated, repo_root).as_posix(), validated


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
        relative = relative_to_root(entry, path)
        if migration_folder and len(relative.parts) == 1 and relative.name == f"status.{environment}.json":
            # This is a local apply receipt, not migration source.
            continue
        if migration_folder and len(relative.parts) == 1 and relative.name.startswith("attempt-event."):
            # Generated attempt evidence is validated separately and is not frozen input.
            continue
        if app_source and relative.as_posix() == "apex-team-export.json":
            # This is local export baseline metadata, not imported APEX source.
            continue
        files.append(entry)
    return files


def _step_input_paths(step: dict, root: Path, environment: str) -> list[Path]:
    kind = step["type"]
    if kind == "feature-migrate":
        # Feature source and check bytes are bound to the frozen capsule later.
        return []
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
        result.append({"path": relative_to_root(path, root).as_posix(), "sha256": digest})
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


def _app_deploy_configuration(repo_root: Path) -> dict[str, str]:
    configured_path = os.environ.get("PROJECT_ENV_FILE")
    path = Path(configured_path) if configured_path else repo_root / ".env"
    if not path.is_absolute() and not path.is_file() and (repo_root / path).is_file():
        path = repo_root / path
    try:
        return read_project_env(path)
    except ConfigError as error:
        raise RolloutError(f"app-deploy child configuration is invalid: {error}") from error


def _binding_for_target(target: Target) -> dict[str, str]:
    return {
        "environment": target.environment,
        "connection": target.connection,
        "expectedUser": target.expected_user,
        "schema": target.schema,
        "classification": target.classification,
    }


def _frozen_env_text(values: Mapping[str, str]) -> str:
    if set(values) - ALLOWED_KEYS:
        raise RolloutError("app-deploy child configuration contains unsupported settings")
    lines = []
    for key in sorted(values):
        value = values[key]
        if not isinstance(value, str) or any(character in value for character in ("\x00", "\r", "\n")):
            raise RolloutError(f"app-deploy child configuration contains an unsafe value for {key}")
        # The project env parser strips only the matching outer quotes and does
        # not evaluate shell syntax, so this preserves literal values safely.
        lines.append(f'{key}="{value}"')
    return "\n".join(lines) + "\n"


def _target_for_step(step: dict, repo_root: Path, environment: str) -> Target | None:
    """Resolve the configured Oracle target represented by one rollout step."""
    kind = step["type"]
    if kind in {"pause", "feature-migrate"}:
        return None
    if kind in {"migrate", "verify"}:
        migrations = load_batch(repo_root, step["folders"])
        requested = os.environ.get("PROJECT_SCHEMA") or None
        schema = batch_schema([migration.schema for migration in migrations], requested, os.environ, environment)
        return resolve_target(os.environ, environment, "migration", schema=schema)
    if kind in {"sql-script", "ords-import"}:
        if "connection" not in step:
            target = resolve_target(os.environ, environment, "migration", schema=step["schema"])
            if kind == "ords-import" and (target.expected_user != target.schema or target.schema != step["schema"]):
                raise TargetResolutionError("ORDS rollout targets must use the actual REST owner named by the export")
            return target
        classification = {
            "dev": str(os.environ.get("DB_ENVIRONMENT", "development")).casefold(),
            "staging": "staging",
            "prod": "production",
        }[environment]
        if classification not in {"development", "test", "staging", "production"}:
            raise TargetResolutionError("DB_ENVIRONMENT must be development, test, staging, or production")
        if environment == "dev" and classification not in {"development", "test"}:
            raise TargetResolutionError("DEV SQL-script steps require DB_ENVIRONMENT=development or test")
        if environment != "prod" and looks_like_production_identity(step["connection"], step["schema"]):
            raise TargetResolutionError("SQL-script target looks like production but --env is not prod")
        return Target(environment, step["connection"], step["expectedUser"], step["schema"], classification)
    if kind == "app-deploy":
        _relative, app_dir = _read_app_inputs(repo_root, step["appId"], environment)
        descriptor = read_descriptor(app_dir / "deployments" / f"{environment}.json", step["appId"])
        schema = descriptor["app"]["databaseSession"]["parsingSchema"]
        return resolve_target(os.environ, environment, "read", schema=schema)
    raise RolloutError(f"unsupported rollout step type {kind!r}")


def _target_binding(step: dict, repo_root: Path, environment: str) -> dict | None:
    try:
        target = _target_for_step(step, repo_root, environment)
    except (TargetResolutionError, MigrationManifestError, OSError, ValueError) as error:
        if isinstance(error, RolloutError):
            raise
        raise RolloutError(f"step target cannot be resolved: {error}") from error
    if target is None:
        return None
    binding = {
        "environment": target.environment,
        "connection": target.connection,
        "expectedUser": target.expected_user,
        "schema": target.schema,
        "classification": target.classification,
    }
    if step["type"] in {"sql-script", "ords-import"} and "connection" not in step:
        binding.update({"operation": "migration", "logicalSchema": step["schema"]})
    return binding


def _target_from_binding(binding: dict) -> Target:
    return Target(
        binding["environment"], binding["connection"], binding["expectedUser"],
        binding["schema"], binding["classification"],
    )


def _describe_step(step: dict, repo_root: Path, environment: str) -> str:
    kind = step["type"]
    if kind == "feature-migrate":
        return f"feature-migrate: {step['schema']} ({len(step.get('inputs', []))} frozen inputs)"
    if kind in {"migrate", "verify"}:
        result = f"{kind}: {', '.join(step['folders'])}"
        return result + (f" ({step['phase']})" if kind == "verify" else "")
    if kind in {"sql-script", "ords-import"}:
        if "connection" not in step:
            return (
                f"{kind}: {step['file']} via the {environment} migration profile "
                f"for logical schema {step['schema']}"
            )
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
        binding = _target_binding(step, manifest.repo_root, environment)
        app_deploy_config = None
        app_deploy_config_sha256 = None
        app_deploy_source_schema_selector = None
        if step["type"] == "app-deploy":
            app_deploy_config = _app_deploy_configuration(manifest.repo_root)
            _relative, app_dir = _read_app_inputs(manifest.repo_root, step["appId"], environment)
            descriptor = read_descriptor(app_dir / "deployments" / f"{environment}.json", step["appId"])
            schema = descriptor["app"]["databaseSession"]["parsingSchema"]
            try:
                child_target = resolve_target(app_deploy_config, environment, "read", schema=schema)
            except TargetResolutionError as error:
                raise RolloutError(f"app-deploy child configuration target cannot be resolved: {error}") from error
            if _binding_for_target(child_target) != binding:
                raise RolloutError("app-deploy child configuration target does not match the rollout target binding")
            app_deploy_source_schema_selector = os.environ.get("PROJECT_SCHEMA")
            if (
                app_deploy_source_schema_selector not in {None, ""}
                and not ORACLE_IDENTIFIER.fullmatch(app_deploy_source_schema_selector)
            ):
                raise RolloutError("app-deploy PROJECT_SCHEMA must be an uppercase Oracle identifier")
            app_deploy_config_sha256 = _canonical_hash(app_deploy_config)
        raw = None
        if step["type"] in {"sql-script", "ords-import"}:
            script = _read_script(manifest.repo_root / step["file"], f"step {number} script")
            raw = (manifest.repo_root / step["file"]).read_bytes()
            if step["type"] == "sql-script":
                validate_sql_script(script, number)
            else:
                validate_ords_script(script, step["schema"], number)
                if step.get("excludeModules"):
                    exclude_ords_modules(script, step["excludeModules"], step["schema"])
        hash_payload = {
            "step": step,
            "inputHashes": hashes,
            "targetBinding": binding,
        }
        if app_deploy_config_sha256 is not None:
            hash_payload["appDeployConfigSha256"] = app_deploy_config_sha256
            hash_payload["sourceSchemaSelector"] = app_deploy_source_schema_selector
        step_hash = _canonical_hash(hash_payload)
        description = _describe_step(step, manifest.repo_root, environment)
        if binding is not None:
            description += (
                f" to {binding['environment'].upper()} target {binding['schema']} via {binding['connection']} "
                f"as {binding['expectedUser']} ({binding['classification']})"
            )
        plan.append({
            "number": number,
            "type": step["type"],
            "step": step,
            "description": description,
            "sha256": step_hash,
            "inputHashes": hashes,
            "targetBinding": binding,
            "appDeployConfig": app_deploy_config,
            "appDeployConfigSha256": app_deploy_config_sha256,
            "appDeploySourceSchemaSelector": app_deploy_source_schema_selector,
        })
        if manifest.schema_version == 2 and step["type"] in {"sql-script", "ords-import"}:
            classification = classify_auxiliary_step(step, raw if raw is not None else b"")
            plan[-1]["phase"] = classification
            plan[-1]["classificationReason"] = _auxiliary_classification_reason(step["type"], classification)
        elif manifest.schema_version == 2 and step["type"] == "migrate":
            plan[-1]["phase"] = "uncovered-object"
            plan[-1]["classificationReason"] = _auxiliary_classification_reason("migrate", "uncovered-object")
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
    try:
        validate_sql_only(source, allow_transaction_control=True)
    except ValueError as error:
        raise RolloutError(f"step {number} SQL script is not safe SQL-only source: {error}") from error
    if ROLLOUT_MARKER_LITERAL.search(source):
        raise RolloutError(f"step {number} SQL script cannot emit reserved ROLLOUT_ verification markers")
    if CURRENT_SCHEMA_CHANGE.search(source):
        raise RolloutError(f"step {number} SQL script cannot change CURRENT_SCHEMA after the identity guard")


def classify_auxiliary_step(step: Mapping, frozen_source: bytes) -> str:
    """Classify frozen SQL/ORDS payloads without inferring database side effects."""
    if not isinstance(step, Mapping) or not isinstance(frozen_source, bytes):
        return "uncovered-object"
    kind = step.get("type")
    try:
        source = frozen_source.decode("utf-8", errors="strict")
    except UnicodeError:
        return "uncovered-object"
    if kind == "ords-import":
        schema = step.get("schema")
        if not isinstance(schema, str):
            return "uncovered-object"
        try:
            validate_ords_script(source, schema)
        except (RolloutError, ValueError):
            return "uncovered-object"
        return "ords"
    if kind != "sql-script":
        return "uncovered-object"
    try:
        validate_sql_script(source)
        spans = statement_spans(source, allow_transaction_control=True)
        masked = mask_comments_and_literals(source)
    except (RolloutError, ValueError):
        return "uncovered-object"
    saw_dml = False
    for start, end in spans:
        match = FIRST_WORD.match(masked, start, end)
        if match is None:
            return "uncovered-object"
        first = match.group().upper()
        statement = " ".join(masked[start:end].split()).upper()
        if first in {"COMMIT", "ROLLBACK"}:
            if re.fullmatch(r"(?:COMMIT|ROLLBACK)(?: WORK)?", statement, re.ASCII) is None:
                return "uncovered-object"
        elif first in {"INSERT", "UPDATE", "DELETE", "MERGE"}:
            saw_dml = True
        else:
            return "uncovered-object"
    return "data" if saw_dml else "uncovered-object"


def _auxiliary_classification_reason(kind: str, classification: str) -> str:
    if classification == "data":
        return "source contains only top-level static DML and explicit transaction control; trigger, autonomous, and external effects are not inferred"
    if classification == "ords":
        return "source passed the generated ORDS structure, OAuth exclusion, and owner checks"
    if kind == "migrate":
        return "legacy migration folders are outside feature-checkpoint coverage"
    return "source is not limited to the recognized, statically validated phase"


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
        if step["type"] == "feature-migrate":
            # The capsule is the frozen source authority for feature checkpoints.
            continue
        try:
            current_binding = _target_binding(step, manifest.repo_root, environment)
        except RolloutError as error:
            raise RolloutError(f"rollout target binding changed after rollout started {boundary}: {error}") from error
        if current_binding != item["targetBinding"]:
            raise RolloutError(f"rollout target binding changed after rollout started {boundary} (step {item['number']})")
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
            relative_to_root(resolved, root)
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
        "json": relative_to_root(json_path, report["repoRoot"]).as_posix(),
        "markdown": relative_to_root(markdown_path, report["repoRoot"]).as_posix(),
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
    confirmed_steps = []
    for item in plan:
        confirmed = {
            "number": item["number"],
            "sha256": item["sha256"],
            "inputHashes": item["inputHashes"],
            "targetBinding": item["targetBinding"],
        }
        if item["appDeployConfigSha256"] is not None:
            confirmed["childConfigurationSha256"] = item["appDeployConfigSha256"]
            confirmed["sourceSchemaSelector"] = item["appDeploySourceSchemaSelector"]
        confirmed_steps.append(confirmed)
    evidence = {
        "schemaVersion": 1,
        "status": "confirmed",
        "manifestPath": relative_to_root(manifest.path, manifest.repo_root).as_posix(),
        "manifestSha256": manifest.sha256,
        "environment": environment,
        "confirmedAt": _utc_now(),
        "steps": confirmed_steps,
    }
    _write_atomic(path, json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _receipt_path(root: Path, manifest_sha256: str, environment: str, number: int) -> Path:
    return root / "scratch" / "rollout-receipts" / manifest_sha256 / environment / f"step-{number:03d}.json"


def _assert_unreceipted_execution_range(
    root: Path,
    manifest: RolloutManifest,
    environment: str,
    plan: Sequence[dict],
    from_step: int,
) -> None:
    """Refuse to execute any step whose canonical receipt path is already occupied."""
    for item in plan[from_step - 1 :]:
        path = _receipt_path(root, manifest.sha256, environment, item["number"])
        try:
            _assert_no_symlink(path, root)
        except RolloutError as error:
            raise RolloutError(
                f"step {item['number']} receipt path is unsafe or occupied; preserve existing receipts and evidence, "
                f"inspect the target and use a separately reviewed continuation manifest before further writes: {error}"
            ) from error
        if path.exists() or path.is_symlink():
            raise RolloutError(
                f"step {item['number']} receipt path is already occupied and will not be replaced: "
                f"{relative_to_root(path, root)}. Preserve its bytes and evidence, inspect the target, and use a "
                "separately reviewed continuation manifest before further writes"
            )


def _install_rollout_receipt(path: Path, receipt: dict) -> None:
    """Durably install a rollout receipt without replacing an existing path."""
    current = Path(path.anchor)
    for part in path.parent.absolute().parts[1:]:
        current = current / part
        if current.is_symlink():
            raise RolloutError(f"rollout receipt parent path contains a symbolic link: {current}")
    try:
        path.parent.resolve(strict=True)
    except OSError as error:
        raise RolloutError(f"rollout receipt directory is missing or unsafe: {error}") from error
    if not path.parent.is_dir():
        raise RolloutError("rollout receipt directory is missing or unsafe")
    if path.is_symlink() or path.exists():
        raise RolloutError(f"rollout receipt path is already occupied and will not be replaced: {path}")
    data = (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise RolloutError(f"rollout receipt path is already occupied and will not be replaced: {path}") from error
        except OSError as error:
            raise RolloutError(f"atomic no-overwrite rollout receipt installation is unavailable: {error}") from error
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            # Some supported filesystems do not allow directory fsync after the atomic link.
            pass
    except RolloutError:
        raise
    except OSError as error:
        raise RolloutError(f"atomic no-overwrite rollout receipt installation is unavailable: {error}") from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _validate_evidence_paths(paths: object, root: Path, step_number: int) -> list[str]:
    if not isinstance(paths, list) or any(not isinstance(item, str) for item in paths):
        raise RolloutError(f"step {step_number} receipt has invalid evidence paths")
    for item in paths:
        components = item.split("/")
        if not item or "\\" in item or any(part in {"", ".", ".."} for part in components) or PurePosixPath(item).is_absolute():
            raise RolloutError(f"step {step_number} receipt has invalid evidence paths")
        evidence_path = root.joinpath(*components)
        try:
            _assert_no_symlink(evidence_path, root)
        except RolloutError as error:
            raise RolloutError(f"step {step_number} receipt evidence path is unsafe: {item}") from error
        if not evidence_path.exists():
            raise RolloutError(f"step {step_number} receipt evidence path is missing: {item}")
    return paths


def _write_receipt(root: Path, environment: str, manifest: RolloutManifest, plan_item: dict, step_report: dict) -> Path:
    path = _receipt_path(root, manifest.sha256, environment, plan_item["number"])
    try:
        _validate_evidence_paths(step_report["evidencePaths"], root, plan_item["number"])
        _safe_scratch_root(root)
        _safe_directory(path.parent, root)
        receipt = {
            "schemaVersion": 1 if manifest.schema_version == 2 else RECEIPT_SCHEMA_VERSION,
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
        if manifest.schema_version == 1:
            receipt["targetBinding"] = plan_item["targetBinding"]
            receipt["observedIdentity"] = step_report["observedIdentity"]
        _install_rollout_receipt(path, receipt)
    except (OSError, RolloutError) as error:
        raise RolloutError(
            f"step {plan_item['number']} may have completed, but its rollout receipt was not installed. "
            "Preserve this report, SQLcl evidence and any existing receipt; inspect the target independently and "
            "use only a separately reviewed continuation manifest for further writes. This does not serialize "
            f"cross-process or database writes. Cause: {error}"
        ) from error
    return path


OBSERVED_IDENTITY_FIELDS = (
    "session_user", "current_schema", "db_name", "db_unique_name", "service_name",
    "container_id", "container_name", "edition", "database_version",
)


def _observe_target_identity(target: Target, run_dir: Path) -> dict[str, str]:
    """Read target identity independently through the existing schema catalog path."""
    from scripts.schema_catalog import capture_inventory

    inventory = capture_inventory(target, run_dir)
    receipt_identity = migrate._identity_for_receipt(inventory, target)
    return {field: receipt_identity[field] for field in OBSERVED_IDENTITY_FIELDS}


def _verify_migration_receipts(
    root: Path,
    step: dict,
    binding: dict,
    observed_identity: dict[str, str],
) -> None:
    if step["type"] != "migrate":
        return
    migrations = load_batch(root, step["folders"])
    target_identity = {
        "environment": binding["environment"],
        "connection": binding["connection"],
        "expected_user": binding["expectedUser"],
        **observed_identity,
    }
    for migration in migrations:
        receipt = migration.folder / f"status.{binding['environment']}.json"
        try:
            _assert_no_symlink(receipt, root)
        except RolloutError as error:
            raise RolloutError(f"migration receipt needs reconciliation: {error}") from error
        if not receipt.is_file():
            raise RolloutError(
                f"migration step {migration.folder.name} has no verified receipt; reconcile before resuming rollout"
            )
        try:
            validate_migration_receipt(receipt, migration, target_identity)
        except MigrationManifestError as error:
            raise RolloutError(
                f"migration receipt for {migration.folder.name} does not match the exact source and observed target; "
                f"reconcile before resuming rollout: {error}"
            ) from error


def _verify_receipt(root: Path, manifest: RolloutManifest, environment: str, plan_item: dict) -> tuple[dict, Path]:
    path = _receipt_path(root, manifest.sha256, environment, plan_item["number"])
    _assert_no_symlink(path, root)
    if path.is_symlink() or not path.is_file():
        raise RolloutError(f"step {plan_item['number']} has no matching successful receipt: {relative_to_root(path, root)}")
    receipt = _read_json(path, f"step {plan_item['number']} receipt")
    if manifest.schema_version == 1 and isinstance(receipt, dict) and receipt.get("schemaVersion") == 1:
        raise RolloutError(
            f"step {plan_item['number']} has a legacy receipt without independently observed target identity; "
            "inspect the completed step and reconcile before retrying"
        )
    expected = {
        "schemaVersion": 1 if manifest.schema_version == 2 else RECEIPT_SCHEMA_VERSION,
        "status": "succeeded",
        "environment": environment,
        "manifestSha256": manifest.sha256,
        "stepNumber": plan_item["number"],
        "stepType": plan_item["type"],
        "stepSha256": plan_item["sha256"],
        "inputHashes": plan_item["inputHashes"],
    }
    required_fields = set(expected) | {"startedAt", "endedAt", "durationSeconds", "evidencePaths"}
    if manifest.schema_version == 1:
        expected["targetBinding"] = plan_item["targetBinding"]
        required_fields.add("targetBinding")
        required_fields.add("observedIdentity")
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
    _validate_evidence_paths(receipt["evidencePaths"], root, plan_item["number"])
    if manifest.schema_version == 1:
        observed = receipt["observedIdentity"]
        if plan_item["targetBinding"] is None:
            if observed is not None:
                raise RolloutError(f"step {plan_item['number']} pause receipt must not claim a database identity")
        elif (
            not isinstance(observed, dict)
            or set(observed) != set(OBSERVED_IDENTITY_FIELDS)
            or any(not isinstance(observed.get(field), str) or not observed[field].strip() for field in OBSERVED_IDENTITY_FIELDS)
            or observed["session_user"].upper() != plan_item["targetBinding"]["expectedUser"]
            or observed["current_schema"].upper() != plan_item["targetBinding"]["schema"]
        ):
            raise RolloutError(f"step {plan_item['number']} receipt has invalid observed database identity")
        if (
            plan_item["targetBinding"] is not None
            and plan_item["targetBinding"]["environment"] != "prod"
            and looks_like_production_identity(observed["db_name"], observed["db_unique_name"], observed["service_name"])
        ):
            raise RolloutError(f"step {plan_item['number']} receipt records production identity for a non-production target")
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
        if item.get("targetBinding") is not None:
            lines.append("  Target binding: " + json.dumps(item["targetBinding"], sort_keys=True))
        if item.get("appDeployConfigSha256") is not None:
            lines.append("  child configuration SHA-256: " + item["appDeployConfigSha256"])
            selector = item.get("appDeploySourceSchemaSelector")
            lines.append(f"  source PROJECT_SCHEMA selector: {selector if selector else '<unset>'}")
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


def _recovery_ack_from_terminal(prompt: str) -> str:
    print(prompt, file=sys.stderr)
    try:
        return input("Reconciliation acknowledgment: ")
    except (EOFError, OSError):
        return ""


def _execution_id() -> str:
    return f"rollout-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(5)}"


def _relative_paths(paths: Sequence[Path | str], root: Path) -> tuple[str, ...]:
    result = []
    for item in paths:
        path = Path(item)
        if not path.is_absolute():
            path = root / path
        try:
            result.append(relative_to_root(path.resolve(strict=False), root).as_posix())
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
    expected_identity: Mapping[str, str] | None = None,
) -> Path:
    payload_path = run_dir / "payload.sql"
    payload_path.write_text(payload, encoding="utf-8", newline="\n")
    try:
        payload_path.chmod(0o600)
    except OSError as error:
        raise RolloutError(f"could not secure SQL rollout payload ({error})") from error
    driver = run_dir / "rollout.sql"
    identity_constants = ""
    identity_checks = ""
    identity_output = ""
    if expected_identity is not None:
        missing = [field for field in OBSERVED_IDENTITY_FIELDS if not str(expected_identity.get(field, "")).strip()]
        if missing:
            raise RolloutError("rollout expected identity is incomplete: " + ", ".join(missing))

        def sql_hex(value: str) -> str:
            return value.encode("utf-8").hex().upper()

        identity_constants = "\n".join(
            f"  c_expected_{field} CONSTANT VARCHAR2(512) := UTL_RAW.CAST_TO_VARCHAR2(HEXTORAW('{sql_hex(str(expected_identity[field]))}'));"
            for field in OBSERVED_IDENTITY_FIELDS
        )
        # The driver declares the database name as v_database_name; every other field is v_<field>.
        identity_variables = {"db_name": "database_name"}
        identity_checks = "\n".join(
            f"  IF NVL(v_{identity_variables.get(field, field)}, '{field.upper()}_MISSING') <> c_expected_{field} THEN RAISE_APPLICATION_ERROR(-20075, 'rollout writer identity mismatch'); END IF;"
            for field in OBSERVED_IDENTITY_FIELDS
        )
        identity_output = (
            "  DBMS_OUTPUT.PUT_LINE('ROLLOUT_WRITER_IDENTITY:' || "
            "RAWTOHEX(UTL_RAW.CAST_TO_RAW(v_session_user)) || ':' || "
            "RAWTOHEX(UTL_RAW.CAST_TO_RAW(v_current_schema)) || ':' || "
            "RAWTOHEX(UTL_RAW.CAST_TO_RAW(v_database_name)) || ':' || "
            "RAWTOHEX(UTL_RAW.CAST_TO_RAW(v_db_unique_name)) || ':' || "
            "RAWTOHEX(UTL_RAW.CAST_TO_RAW(v_service_name)) || ':' || "
            "RAWTOHEX(UTL_RAW.CAST_TO_RAW(v_container_id)) || ':' || "
            "RAWTOHEX(UTL_RAW.CAST_TO_RAW(v_container_name)) || ':' || "
            "RAWTOHEX(UTL_RAW.CAST_TO_RAW(v_edition)) || ':' || "
            "RAWTOHEX(UTL_RAW.CAST_TO_RAW(v_database_version)));"
        )
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
            "DECLARE",
            "  v_session_user VARCHAR2(128) := SYS_CONTEXT('USERENV', 'SESSION_USER');",
            "  v_current_schema VARCHAR2(128) := SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA');",
            "  v_database_name VARCHAR2(128) := SYS_CONTEXT('USERENV', 'DB_NAME');",
            "  v_db_unique_name VARCHAR2(128) := SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME');",
            "  v_service_name VARCHAR2(256) := NVL(SYS_CONTEXT('USERENV', 'SERVICE_NAME'), '<NO_SERVICE>');",
            "  v_container_id VARCHAR2(128) := NVL(SYS_CONTEXT('USERENV', 'CON_ID'), '0');",
            "  v_container_name VARCHAR2(128) := NVL(SYS_CONTEXT('USERENV', 'CON_NAME'), 'NON-CDB');",
            "  v_edition VARCHAR2(128) := NVL(SYS_CONTEXT('USERENV', 'CURRENT_EDITION_NAME'), '<NONEDITIONED>');",
            "  v_database_version VARCHAR2(128) := TO_CHAR(DBMS_DB_VERSION.VERSION) || '.' || TO_CHAR(DBMS_DB_VERSION.RELEASE);",
            identity_constants,
            "BEGIN",
            identity_checks,
            identity_output,
            "END;",
            "/",
            "SET SQLBLANKLINES ON",
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
    expected_identity: Mapping[str, str] | None = None,
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
        expected_identity=expected_identity,
    )
    mode = "assembled" if generated_clob_assembly(source) else "direct"
    for finding in lint_sqlcl_transport(source, mode):
        message = f"payload.sql:{finding.line} ({finding.code}): {finding.message}"
        if finding.severity == "error":
            return StepResult(2, _relative_paths((driver, evidence_dir / "payload.sql"), repo_root), f"SQLcl transport preflight refused: {message}")
        print(f"rollout warning: SQLcl transport review: {message}", file=sys.stderr)
    for finding in lint_sqlcl_transport(driver.read_text(encoding="utf-8"), "direct"):
        message = f"rollout.sql:{finding.line} ({finding.code}): {finding.message}"
        if finding.severity == "error":
            return StepResult(2, _relative_paths((driver, evidence_dir / "payload.sql"), repo_root), f"SQLcl transport preflight refused: {message}")
        print(f"rollout warning: SQLcl transport review: {message}", file=sys.stderr)
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
    writer_markers = re.findall(r"(?m)^\s*ROLLOUT_WRITER_IDENTITY:([0-9A-F]+(?::[0-9A-F]+){8})\s*$", output)
    completions = re.findall(rf"(?m)^\s*ROLLOUT_SCRIPT_COMPLETED:{step_number:04d}\s*$", output)
    if identities != [(step["expectedUser"], step["schema"])]:
        return StepResult(2, _relative_paths((driver, evidence_dir / "payload.sql", evidence_dir / "sqlcl-output.log"), repo_root), "SQLcl did not print exactly the expected session user and current schema")
    if len(completions) != 1:
        return StepResult(2, _relative_paths((driver, evidence_dir / "payload.sql", evidence_dir / "sqlcl-output.log"), repo_root), "SQLcl did not confirm exactly one completed script step")
    if len(targets) != 1 or any(value.startswith("<NO_") for value in targets[0]):
        return StepResult(2, _relative_paths((driver, evidence_dir / "payload.sql", evidence_dir / "sqlcl-output.log"), repo_root), "SQLcl did not report a complete database and service identity")
    if environment != "prod" and looks_like_production_identity(*targets[0]):
        return StepResult(2, _relative_paths((driver, evidence_dir / "payload.sql", evidence_dir / "sqlcl-output.log"), repo_root), "observed SQLcl database/service identity resembles production while the selected environment is not prod")
    observed_identity = None
    if expected_identity is not None:
        if len(writer_markers) != 1:
            return StepResult(2, _relative_paths((driver, evidence_dir / "payload.sql", evidence_dir / "sqlcl-output.log"), repo_root), "SQLcl did not report exactly one complete writer identity")
        try:
            values = [bytes.fromhex(item).decode("utf-8") for item in writer_markers[0].split(":")]
        except (UnicodeDecodeError, ValueError):
            return StepResult(2, _relative_paths((driver, evidence_dir / "payload.sql", evidence_dir / "sqlcl-output.log"), repo_root), "SQLcl writer identity marker was not valid UTF-8 hex")
        observed_identity = dict(zip(OBSERVED_IDENTITY_FIELDS, values, strict=True))
        if observed_identity != {field: str(expected_identity[field]) for field in OBSERVED_IDENTITY_FIELDS}:
            return StepResult(2, _relative_paths((driver, evidence_dir / "payload.sql", evidence_dir / "sqlcl-output.log"), repo_root), "SQLcl writer identity differed from the independently observed target")
    return StepResult(0, _relative_paths((driver, evidence_dir / "payload.sql", evidence_dir / "sqlcl-output.log"), repo_root), "script completed with the expected identity", observed_identity)


def _execute_app_deploy(
    step: dict,
    *,
    environment: str,
    repo_root: Path,
    evidence_dir: Path,
    manifest_confirmed: bool,
    expected_input_hashes: dict[str, str],
    frozen_config: Mapping[str, str],
    config_sha256: str,
    source_schema_selector: str | None,
    expected_target_binding: dict[str, str],
) -> StepResult:
    if environment not in {"staging", "prod"}:
        raise RolloutError("app-deploy is supported only for staging and production")
    if not manifest_confirmed:
        raise RolloutError("app-deploy requires the staging or production manifest confirmation")
    evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    app_relative, app_dir = _read_app_inputs(repo_root, step["appId"], environment)
    descriptor = read_descriptor(app_dir / "deployments" / f"{environment}.json", step["appId"])
    parsing_schema = descriptor["app"]["databaseSession"]["parsingSchema"]
    if _canonical_hash(dict(frozen_config)) != config_sha256:
        raise RolloutError("app-deploy child configuration changed after confirmation")
    try:
        planned_child_binding = _binding_for_target(
            resolve_target(frozen_config, environment, "read", schema=parsing_schema)
        )
    except TargetResolutionError as error:
        raise RolloutError(f"app-deploy child configuration target cannot be resolved: {error}") from error
    if planned_child_binding != expected_target_binding:
        raise RolloutError("app-deploy child configuration target does not match the confirmed target binding")

    frozen_env_path = evidence_dir / "app-deploy.env"
    try:
        _write_atomic(frozen_env_path, _frozen_env_text(frozen_config))
        frozen_config_from_disk = read_project_env(frozen_env_path)
    except (OSError, ConfigError) as error:
        raise RolloutError(f"could not secure frozen app-deploy child configuration: {error}") from error
    if frozen_config_from_disk != dict(frozen_config):
        raise RolloutError("frozen app-deploy child configuration did not retain the confirmed values")
    try:
        frozen_child_binding = _binding_for_target(
            resolve_target(frozen_config_from_disk, environment, "read", schema=parsing_schema)
        )
    except TargetResolutionError as error:
        raise RolloutError(f"frozen app-deploy child target cannot be resolved: {error}") from error
    if frozen_child_binding != expected_target_binding:
        raise RolloutError("frozen app-deploy child target does not match the confirmed target binding")

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
    for key in ALLOWED_KEYS:
        child_env.pop(key, None)
    child_env.update(frozen_config_from_disk)
    child_env["PROJECT_ENV_FILE"] = frozen_env_path.resolve().as_posix()
    if source_schema_selector is None:
        child_env.pop("PROJECT_SCHEMA", None)
    else:
        child_env["PROJECT_SCHEMA"] = source_schema_selector
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
        _relative_paths((staged_app_dir, frozen_env_path, log_path), repo_root),
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
    expected_identity: Mapping[str, str] | None = None,
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
        evidence = [relative_to_root(log_path, repo_root).as_posix()]
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
            expected_identity=expected_identity,
        )
        if removed:
            return StepResult(result.exit_code, result.evidence_paths, f"excluded ORDS modules {', '.join(removed)}; {result.message}", result.observed_identity)
        return result
    if kind == "app-deploy":
        plan_item = next((item for item in plan if item["number"] == step_number), None)
        if plan_item is None or not isinstance(plan_item.get("appDeployConfig"), dict):
            raise RolloutError("app-deploy has no frozen child configuration in the rollout plan")
        return _execute_app_deploy(
            step,
            environment=environment,
            repo_root=repo_root,
            evidence_dir=evidence_dir,
            manifest_confirmed=manifest_confirmed,
            expected_input_hashes=_planned_input_hashes(plan, step_number),
            frozen_config=plan_item["appDeployConfig"],
            config_sha256=plan_item["appDeployConfigSha256"],
            source_schema_selector=plan_item["appDeploySourceSchemaSelector"],
            expected_target_binding=plan_item["targetBinding"],
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
    result = {
        "number": plan_item["number"],
        "type": plan_item["type"],
        "description": plan_item["description"],
        "sha256": plan_item["sha256"],
        "inputHashes": plan_item["inputHashes"],
        "observedIdentity": None,
        "status": "not-run",
        "startedAt": None,
        "endedAt": None,
        "durationSeconds": None,
        "evidencePaths": [],
        "receiptPath": None,
        "message": None,
    }
    if "targetBinding" in plan_item:
        result["targetBinding"] = plan_item["targetBinding"]
    if plan_item.get("appDeployConfigSha256") is not None:
        result["childConfigurationSha256"] = plan_item["appDeployConfigSha256"]
        result["sourceSchemaSelector"] = plan_item.get("appDeploySourceSchemaSelector")
    for field in ("phase", "classificationReason"):
        if field in plan_item:
            result[field] = plan_item[field]
    return result


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
            raise RolloutError(f"report output overlaps rollout recovery state: {relative_to_root(output, root)}")
        try:
            scratch_relative = relative_to_root(output, root / "scratch")
        except ValueError:
            scratch_relative = None
        if scratch_relative and scratch_relative.parts and any(
            scratch_relative.parts[0].startswith(prefix) for prefix in reserved_scratch_prefixes
        ):
            raise RolloutError(f"report output overlaps command recovery evidence: {relative_to_root(output, root)}")
        if output in input_paths or any(source_root == output or source_root in output.parents for source_root in source_roots):
            raise RolloutError(f"report output overlaps a rollout input: {relative_to_root(output, root)}")


def _finish_report(report: dict, started: float, status: str, exit_code: int) -> None:
    report["status"] = status
    report["exitCode"] = exit_code
    report["endedAt"] = _utc_now()
    report["durationSeconds"] = round(time.perf_counter() - started, 3)


def _announce_report_paths(paths: tuple[Path, Path], root: Path) -> None:
    json_path, markdown_path = paths
    print(
        "Rollout report: JSON "
        f"{relative_to_root(json_path, root).as_posix()}; Markdown {relative_to_root(markdown_path, root).as_posix()}"
    )


def _capsule_report_inputs(root: Path, plan_item: dict, capsule, capsule_document: Mapping, checkpoint_index: int) -> list[dict]:
    capsule_root = Path(capsule.root)
    capsule_relative = relative_to_root(capsule_root, root).as_posix()
    rows: list[dict] = []
    checkpoint = capsule_document["checkpoints"][checkpoint_index]
    for item in checkpoint["inputs"]:
        rows.append({"path": item["path"], "sha256": item["sha256"]})
    for item in checkpoint["checks"]:
        rows.append({"path": item["path"], "sha256": item["sha256"]})
    source_rows = [
        row for row in capsule_document["sourceInputs"]
        if isinstance(row, Mapping) and row.get("step") == plan_item["number"]
    ]
    for row in source_rows:
        if row.get("driverPath") is None:
            continue
        rows.append({
            "path": f"{capsule_relative}/{row['driverPath']}",
            "sha256": row["driverSha256"],
        })
    rows.extend(
        {"path": f"{capsule_relative}/{row['path']}", "sha256": row["sha256"]}
        for row in capsule.input_hashes
        if row["path"].startswith("drivers/checkpoint-")
        and row["path"].startswith(f"drivers/checkpoint-{checkpoint_index + 1:03d}-")
    )
    # Preserve the stable order while rejecting duplicate path bindings.
    result: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if row["path"] not in seen:
            seen.add(row["path"])
            result.append(row)
    return result


def _capsule_live_source_check(root: Path, capsule_document: Mapping) -> None:
    """Prove any legacy/shared file still used by an existing executor equals its frozen bytes."""
    for row in capsule_document["sourceInputs"]:
        if not isinstance(row, Mapping) or not isinstance(row.get("sourcePath"), str):
            continue
        relative = row["sourcePath"].removeprefix("payload/")
        source = root / relative
        _assert_no_symlink(source, root)
        try:
            raw = source.read_bytes()
        except OSError as error:
            raise RolloutError(f"frozen rollout source is no longer available: {relative}") from error
        if hashlib.sha256(raw).hexdigest() != row.get("sourceSha256"):
            raise RolloutError(f"working source differs from the staged capsule: {relative}")

def _validate_capsule_app_targets(root: Path, target_plan, steps: Sequence[dict], environment: str) -> None:
    for step in steps:
        if step["type"] != "app-deploy":
            continue
        app_id = step["appId"]
        target = target_plan.targets.get(f"app:{app_id}")
        if not isinstance(target, Mapping):
            raise RolloutError(f"prepared target plan has no application descriptor for {app_id}")
        descriptor_path = root / target.get("descriptorPath", "")
        _assert_no_symlink(descriptor_path, root)
        try:
            raw = descriptor_path.read_bytes()
            descriptor = read_descriptor(descriptor_path, app_id)
        except (OSError, ValueError) as error:
            raise RolloutError(f"application {app_id} descriptor cannot be revalidated: {error}") from error
        if hashlib.sha256(raw).hexdigest() != target.get("descriptorSha256"):
            raise RolloutError(f"application {app_id} deployment descriptor changed after target preparation")
        if (
            descriptor["workspace"]["name"] != target.get("workspace")
            or descriptor["app"]["databaseSession"]["parsingSchema"] != target.get("schema")
        ):
            raise RolloutError(f"application {app_id} descriptor target differs from the prepared target plan")
        try:
            resolved = resolve_target(os.environ, environment, "read", schema=target["schema"])
        except (KeyError, TargetResolutionError) as error:
            raise RolloutError(f"application {app_id} target profile can no longer be resolved: {error}") from error
        if resolved.connection != target.get("connection") or resolved.expected_user != target.get("expectedUser"):
            raise RolloutError(f"application {app_id} target profile changed after preparation")


def _validate_capsule_auxiliary_targets(target_plan, steps: Sequence[dict], environment: str) -> None:
    for number, step in enumerate(steps, start=1):
        if step.get("type") not in {"sql-script", "ords-import"}:
            continue
        prepared = target_plan.targets.get(f"step:{number}")
        if not isinstance(prepared, Mapping):
            raise RolloutError(f"step {number} has no prepared SQL/ORDS target identity")
        current = _target_binding(step, Path("."), environment)
        for field in (
            "environment", "operation", "logicalSchema", "connection", "expectedUser", "schema", "classification",
        ):
            if current.get(field) != prepared.get(field):
                raise RolloutError(f"step {number} migration profile changed after target preparation")
        observed = prepared.get("observed")
        if not isinstance(observed, Mapping) or any(
            not isinstance(observed.get(field), str) or not observed[field]
            for field in OBSERVED_IDENTITY_FIELDS
        ):
            raise RolloutError(f"step {number} prepared target identity is incomplete")


def _capsule_object_budget(capsule, elapsed: float, included: Sequence[int], excluded: Sequence[str]) -> dict:
    return {
        "seconds": capsule.database_object_budget_seconds,
        "elapsedSeconds": round(elapsed, 3),
        "coverageComplete": len(included) == len(capsule.checkpoints) and not excluded,
        "withinBudget": elapsed <= capsule.database_object_budget_seconds,
        "includedCheckpoints": list(included),
        "excludedObjectSteps": list(excluded),
    }


def _selected_capsule_signature(capsule) -> tuple:
    """The capsule whose hashes were prepared and shown for confirmation."""
    return (
        capsule.manifest_sha256,
        capsule.payload_digest,
        capsule.engine_digest,
        capsule.source_commit,
        tuple((row.get("path"), row.get("sha256")) for row in capsule.input_hashes),
    )


def _assert_recovery_will_not_replay_prior_writes(plan: Sequence[dict], target_plan, parent_run: Mapping) -> None:
    """Refuse recovery when restarting the frozen order would repeat a completed write step."""
    binding = getattr(target_plan, "recovery_binding", None)
    checkpoint = binding.get("checkpoint") if isinstance(binding, Mapping) else None
    if type(checkpoint) is not int or checkpoint < 1:
        raise RolloutError("recovery plan has no valid checkpoint binding")
    checkpoint_step_number = None
    feature_checkpoint = 0
    for item in plan:
        if item["type"] == "feature-migrate":
            feature_checkpoint += 1
            if feature_checkpoint == checkpoint:
                checkpoint_step_number = item["number"]
                break
    if checkpoint_step_number is None:
        raise RolloutError("recovery checkpoint is outside the frozen rollout step list")

    # A parent killed mid-step records no outcome for that step or any later one, so only
    # the earlier writing steps must have an outcome. Those are the only ones that could
    # have completed and be replayed.
    parent_steps = parent_run.get("steps")
    if not isinstance(parent_steps, list) or len(parent_steps) > len(plan):
        raise RolloutError("recovery parent has more step outcomes than the frozen plan")
    write_types = {"migrate", "sql-script", "ords-import", "app-deploy", "feature-migrate"}
    completed_writes = []
    for item in plan[:checkpoint_step_number - 1]:
        if item["type"] not in write_types:
            continue
        if item["number"] > len(parent_steps):
            raise RolloutError(f"recovery parent has no recorded outcome for earlier writing step {item['number']}")
        outcome = parent_steps[item["number"] - 1]
        if not isinstance(outcome, Mapping) or outcome.get("step") != item["number"]:
            raise RolloutError(f"recovery parent step {item['number']} outcome is malformed")
        if outcome.get("status") != "verified":
            raise RolloutError(
                f"recovery cannot safely continue because earlier writing step {item['number']} "
                "does not have a verified receipt"
            )
        completed_writes.append(item["number"])
    if completed_writes:
        rendered = ", ".join(str(number) for number in completed_writes)
        raise RolloutError(
            "recovery refused before confirmation because restarting this capsule would replay "
            f"previously verified writing step(s): {rendered}. Preserve the receipts and use an "
            "operator-reviewed recovery path that omits those completed writes."
        )


def _run_capsule_rollout(
    manifest_path: Path | str,
    environment: str,
    *,
    from_step: int,
    dry_run: bool,
    report_path: Path | str | None,
    repo_root: Path,
    confirm: Callable[[str], bool],
    prepare_only: bool,
    capsule_path: Path | str,
    stage_report_path: Path | str | None,
    recovery_attempt: str | None,
    recovery_confirm: Callable[[str], str] | None,
) -> int:
    from scripts import feature_release, release_capsule, release_ownership, release_state

    started = time.perf_counter()
    root = Path(repo_root).resolve(strict=True)
    report = None
    report_paths = None
    run_id = None

    def finish_local_run(*, verified: bool = False) -> None:
        if run_id is None or report is None:
            return
        index = release_state._load_index(root)
        run = next((item for item in index["rollouts"] if item["runId"] == run_id), None)
        if run is None or run["state"] != "running":
            return
        step_outcomes = []
        proof_steps = []
        for plan_item, step_report in zip(plan, report.get("steps", []), strict=True):
            number = plan_item["number"]
            if step_report.get("status") == "succeeded" and isinstance(step_report.get("receiptPath"), str):
                try:
                    receipt, receipt_path = _verify_receipt(root, manifest, environment, plan_item)
                    raw = receipt_path.read_bytes()
                    relative_receipt = relative_to_root(receipt_path, root).as_posix()
                    receipt_hash = hashlib.sha256(raw).hexdigest()
                    step_outcomes.append({
                        "step": number, "status": "verified", "receiptPath": relative_receipt,
                        "receiptSha256": receipt_hash,
                    })
                    proof_steps.append({
                        "step": number, "status": "verified", "receiptPath": relative_receipt,
                        "receiptSha256": receipt_hash, "receiptJson": raw.decode("utf-8"),
                    })
                except (OSError, UnicodeError, RolloutError) as error:
                    if verified:
                        raise RolloutError(f"step {number} receipt cannot prove completion: {error}") from error
                    step_outcomes.append({"step": number, "status": "unknown", "receiptPath": None, "receiptSha256": None})
            else:
                status = step_report.get("status")
                normalized = "failed" if status == "failed" else "unknown" if status == "running" else "not-started"
                step_outcomes.append({"step": number, "status": normalized, "receiptPath": None, "receiptSha256": None})

        object_budget = report.get("objectBudget", {})
        seconds = object_budget.get("seconds", capsule.database_object_budget_seconds)
        elapsed = object_budget.get("elapsedSeconds")
        coverage = object_budget.get("coverageComplete", False)
        within = object_budget.get("withinBudget") is True and isinstance(elapsed, (int, float)) and elapsed <= seconds
        budget = {
            "seconds": seconds, "elapsedSeconds": elapsed,
            "coverageComplete": coverage is True, "passed": bool(coverage is True and within),
        }
        if verified:
            if report.get("status") != "success" or any(item["status"] != "verified" for item in step_outcomes):
                raise RolloutError("whole-rollout completion requires a successful receipt for every step")
            verification_rows = sorted(
                (row for row in index["checkpointVerifications"] if row["runId"] == run_id),
                key=lambda row: row["checkpoint"],
            )
            if [row["checkpoint"] for row in verification_rows] != list(range(1, len(capsule.checkpoints) + 1)):
                raise RolloutError("whole-rollout completion is missing a checkpoint verification")
            verification_ids = [row["verificationId"] for row in verification_rows]
            if verification_ids != run["checkpointVerificationIds"]:
                raise RolloutError("whole-rollout checkpoint verification index changed during execution")
            for row in verification_rows:
                release_state._verify_evidence_hashes(root, row["evidence"], "checkpoint verification")
            if not budget["passed"] or budget["seconds"] != capsule.database_object_budget_seconds:
                raise RolloutError("whole-rollout completion requires complete in-budget object coverage")
            if report_paths is None:
                raise RolloutError("whole-rollout completion has no final report destination")
            report["reportFiles"] = {
                "json": relative_to_root(report_paths[0], root).as_posix(),
                "markdown": relative_to_root(report_paths[1], root).as_posix(),
            }
            report["localRunId"] = run_id
            _write_reports(report, report_paths)
            final_report_path = relative_to_root(report_paths[0], root).as_posix()
            final_report_sha = _canonical_hash(report)
            recovery_links = [
                row for row in index["recoveryLinks"]
                if row["verificationId"] in verification_ids
            ]
            report["completionProof"] = {
                "schemaVersion": 1,
                "runId": run_id,
                "environment": environment,
                "sourceCommit": capsule.source_commit,
                "payloadDigest": capsule.payload_digest,
                "engineDigest": capsule.engine_digest,
                "planDigest": target_plan.plan_digest,
                "targetScopes": list(run["targetScopes"]),
                "state": "verified",
                "databaseState": "verified",
                "steps": proof_steps,
                "checkpointVerificationIds": verification_ids,
                "checkpointVerifications": verification_rows,
                "recoveryLinks": recovery_links,
                "budget": budget,
                "finalReportPath": final_report_path,
                "finalReportSha256": final_report_sha,
            }
            _write_reports(report, report_paths)
            release_state.finish_rollout(
                root, run_id,
                outcome={
                    "state": "verified", "databaseState": "verified",
                    "steps": step_outcomes,
                    "checkpointVerificationIds": verification_ids,
                    "budget": budget,
                    "finalReportPath": final_report_path,
                    "finalReportSha256": final_report_sha,
                },
            )
            return

        database_state = report.get("databaseState", "not-started")
        if database_state not in {"not-started", "verified", "unknown", "failed"}:
            database_state = "unknown"
        run_state = "unknown" if (
            report.get("exitCode") == 130
            or database_state == "unknown"
            or any(item["status"] == "unknown" for item in step_outcomes)
        ) else "failed"
        release_state.finish_rollout(
            root, run_id,
            outcome={
                "state": run_state,
                "databaseState": database_state,
                "steps": step_outcomes,
                "checkpointVerificationIds": list(run["checkpointVerificationIds"]),
                "budget": budget,
                "finalReportPath": None,
                "finalReportSha256": None,
            },
        )

    try:
        if environment not in {"staging", "prod"}:
            raise RolloutError("feature capsule rollout supports only staging or prod")
        if from_step != 1:
            raise RolloutError("feature capsule rollout starts at step 1; use its checkpoint attempt state for recovery")
        prep_started = time.perf_counter()
        capsule = release_capsule.load_capsule(root, capsule_path)
        selected_capsule_root = capsule.root
        selected_capsule_signature = _selected_capsule_signature(capsule)

        def reload_selected_capsule():
            current = release_capsule.load_capsule(root, selected_capsule_root)
            if _selected_capsule_signature(current) != selected_capsule_signature:
                raise RolloutError("selected release capsule changed after target preparation")
            return current

        capsule_document, _capsule_raw = release_capsule._read_capsule_json(capsule.root / "capsule.json")
        manifest = load_manifest(manifest_path, root, version=2)
        payload_definition = capsule_document["payloadDefinition"]
        frozen_manifest = payload_definition["rolloutManifest"]
        manifest_relative = relative_to_root(manifest.path, root).as_posix()
        if manifest_relative != frozen_manifest["path"] or manifest.sha256 != frozen_manifest["sha256"]:
            raise RolloutError("rollout manifest path or bytes do not match the selected capsule")
        if capsule.database_object_budget_seconds is None:
            raise RolloutError("feature capsule has no bounded database-object budget")
        plan = _prepare_plan(manifest, environment)
        if len(capsule.checkpoints) != sum(item["step"]["type"] == "feature-migrate" for item in plan):
            raise RolloutError("capsule feature checkpoints do not match the rollout manifest")

        checkpoint_index = 0
        for item in plan:
            if item["step"]["type"] != "feature-migrate":
                continue
            item["inputHashes"] = _capsule_report_inputs(root, item, capsule, capsule_document, checkpoint_index)
            item["sha256"] = _canonical_hash({"step": item["step"], "inputHashes": item["inputHashes"]})
            checkpoint_index += 1

        _capsule_live_source_check(root, capsule_document)

        if dry_run:
            preparation_seconds = round(time.perf_counter() - prep_started, 3)
            report_paths = _report_paths(root, report_path)
            _report_outputs_do_not_overlap(report_paths, plan, root, manifest.path, environment)
            report = {
                "schemaVersion": 2,
                "repoRoot": str(root),
                "manifestPath": manifest_relative,
                "manifestSha256": manifest.sha256,
                "environment": environment,
                "status": "planned",
                "exitCode": 0,
                "startedAt": _utc_now(),
                "endedAt": None,
                "durationSeconds": None,
                "payloadDigest": capsule.payload_digest,
                "engineDigest": capsule.engine_digest,
                "sourceCommit": capsule.source_commit,
                "targetPlanDigest": None,
                "inputHashes": list(capsule.input_hashes),
                "preparationSeconds": preparation_seconds,
                "databaseState": "not-started",
                "objectPhaseSeconds": 0.0,
                "objectBudget": {
                    "seconds": capsule.database_object_budget_seconds,
                    "elapsedSeconds": 0.0,
                    "coverageComplete": False,
                    "withinBudget": True,
                    "includedCheckpoints": [],
                    "excludedObjectSteps": [],
                },
                "applicationSeconds": 0.0,
                "ordsSeconds": 0.0,
                "dataSeconds": 0.0,
                "checkpointSqlclSessions": {},
                "steps": [dict(_report_step(item), status="planned") for item in plan],
                "reportFiles": {},
                "confirmationPath": None,
                "error": None,
            }
            _finish_report(report, started, "planned", 0)
            _write_reports(report, report_paths)
            _announce_report_paths(report_paths, root)
            return 0

        if prepare_only:
            terminal_ack = None
            if recovery_attempt is not None and recovery_confirm is not None:
                try:
                    prompt = release_state.recovery_prompt(root, capsule, environment, recovery_attempt)
                    terminal_ack = recovery_confirm(prompt)
                except release_state.ReleaseStateError as error:
                    raise RolloutError(f"recovery acknowledgment refused: {error}") from error
            target_plan = feature_release.prepare_target(
                root, capsule, environment, environ=os.environ, recovery_attempt=recovery_attempt,
                terminal_ack=terminal_ack,
            )
        else:
            target_plan = feature_release.load_target_plan(root, capsule, environment)
            if recovery_attempt is not None and recovery_attempt != target_plan.recovery_attempt:
                raise RolloutError("requested recovery attempt differs from the prepared target plan")
        if target_plan.recovery_attempt is not None:
            try:
                parent_run = release_state.recovery_parent_rollout(root, capsule, target_plan)
            except release_state.ReleaseStateError as error:
                raise RolloutError(f"recovery parent validation refused: {error}") from error
            _assert_recovery_will_not_replay_prior_writes(plan, target_plan, parent_run)

        capsule = reload_selected_capsule()

        preparation_seconds = round(time.perf_counter() - prep_started, 3)
        report_paths = _report_paths(root, report_path)
        _report_outputs_do_not_overlap(report_paths, plan, root, manifest.path, environment)
        if prepare_only:
            report = {
                "schemaVersion": 2,
                "repoRoot": str(root),
                "manifestPath": manifest_relative,
                "manifestSha256": manifest.sha256,
                "environment": environment,
                "status": "prepared",
                "exitCode": 0,
                "startedAt": _utc_now(),
                "endedAt": None,
                "durationSeconds": None,
                "payloadDigest": capsule.payload_digest,
                "engineDigest": capsule.engine_digest,
                "sourceCommit": capsule.source_commit,
                "targetPlanDigest": target_plan.plan_digest,
                "inputHashes": list(capsule.input_hashes),
                "preparationSeconds": preparation_seconds,
                "databaseState": "not-started",
                "objectBudget": {
                    "seconds": capsule.database_object_budget_seconds,
                    "elapsedSeconds": 0.0,
                    "coverageComplete": False,
                    "withinBudget": False,
                    "includedCheckpoints": [],
                    "excludedObjectSteps": [],
                },
                "objectPhaseSeconds": 0.0,
                "applicationSeconds": 0.0,
                "ordsSeconds": 0.0,
                "dataSeconds": 0.0,
                "checkpointSqlclSessions": {},
                "steps": [_report_step(item) for item in plan],
                "reportFiles": {},
                "confirmationPath": None,
                "error": None,
            }
            _finish_report(report, started, "prepared", 0)
            _write_reports(report, report_paths)
            _announce_report_paths(report_paths, root)
            return 0

        if environment == "prod":
            if stage_report_path is None:
                raise RolloutError("production capsule rollout requires --stage-report with a verified staging report")
            stage_path = Path(stage_report_path)
            if not stage_path.is_absolute():
                stage_path = root / stage_path
            _assert_no_symlink(stage_path, root)
            try:
                stage_report = json.loads(stage_path.read_text(encoding="utf-8"), object_pairs_hook=_object_no_duplicates)
            except (OSError, UnicodeError, json.JSONDecodeError, RolloutError) as error:
                raise RolloutError(f"staging report is missing or invalid: {error}") from error
            try:
                release_state.validate_stage_proof(capsule, stage_report)
            except release_state.ReleaseStateError as error:
                raise RolloutError(f"production promotion refused: {error}") from error

        _capsule_live_source_check(root, capsule_document)
        _validate_capsule_app_targets(root, target_plan, [item["step"] for item in plan], environment)
        _validate_capsule_auxiliary_targets(target_plan, [item["step"] for item in plan], environment)
        report = {
            "schemaVersion": 2,
            "repoRoot": str(root),
            "manifestPath": manifest_relative,
            "manifestSha256": manifest.sha256,
            "environment": environment,
            "status": "running",
            "exitCode": None,
            "startedAt": _utc_now(),
            "endedAt": None,
            "durationSeconds": None,
            "payloadDigest": capsule.payload_digest,
            "engineDigest": capsule.engine_digest,
            "sourceCommit": capsule.source_commit,
            "targetPlanDigest": target_plan.plan_digest,
            "inputHashes": list(capsule.input_hashes),
            "preparationSeconds": preparation_seconds,
            "databaseState": "not-started",
            "objectPhaseSeconds": 0.0,
            "objectBudget": {
                "seconds": capsule.database_object_budget_seconds,
                "elapsedSeconds": 0.0,
                "coverageComplete": False,
                "withinBudget": True,
                "includedCheckpoints": [],
                "excludedObjectSteps": [],
            },
            "applicationSeconds": 0.0,
            "ordsSeconds": 0.0,
            "dataSeconds": 0.0,
            "checkpointSqlclSessions": {},
            "steps": [_report_step(item) for item in plan],
            "reportFiles": {},
            "confirmationPath": None,
            "error": None,
        }
        if dry_run:
            report["steps"] = [dict(step, status="planned") for step in report["steps"]]
            _finish_report(report, started, "planned", 0)
            _write_reports(report, report_paths)
            _announce_report_paths(report_paths, root)
            return 0

        try:
            approved = bool(confirm(
                _confirmation_text(manifest, plan, environment, 1)
                + f"\nCapsule payload SHA-256: {capsule.payload_digest}"
                + f"\nWorkflow engine SHA-256: {capsule.engine_digest}"
                + f"\nTarget plan SHA-256: {target_plan.plan_digest}"
            ))
        except (EOFError, OSError):
            approved = False
        if not approved:
            _finish_report(report, started, "declined", 1)
            _write_reports(report, report_paths)
            _announce_report_paths(report_paths, root)
            return 1

        capsule = reload_selected_capsule()
        _validate_capsule_app_targets(root, target_plan, [item["step"] for item in plan], environment)
        _validate_capsule_auxiliary_targets(target_plan, [item["step"] for item in plan], environment)
        scratch = _safe_scratch_root(root)
        evidence_root = scratch / "rollout-runs"
        _safe_directory(evidence_root, root)
        run_evidence = evidence_root / _execution_id()
        _assert_no_symlink(run_evidence, root)
        run_evidence.mkdir(mode=0o700, exist_ok=False)
        report["evidenceDir"] = relative_to_root(run_evidence, root).as_posix()
        confirmation_path = run_evidence / "rollout-confirmation.json"
        _write_atomic(confirmation_path, json.dumps({
            "schemaVersion": 2,
            "status": "confirmed",
            "environment": environment,
            "payloadDigest": capsule.payload_digest,
            "engineDigest": capsule.engine_digest,
            "targetPlanDigest": target_plan.plan_digest,
            "confirmedAt": _utc_now(),
            "inputHashes": list(capsule.input_hashes),
        }, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        report["confirmationPath"] = relative_to_root(confirmation_path, root).as_posix()
        _write_reports(report, report_paths)

        run_id = release_state.start_rollout(root, capsule, target_plan)
        report["localRunId"] = run_id

        included_checkpoints: list[int] = []
        excluded_object_steps: list[str] = []
        object_seconds = 0.0
        checkpoint_number = 0
        for index, item in enumerate(plan):
            step = item["step"]
            step_report = report["steps"][index]
            if step["type"] == "feature-migrate" and object_seconds >= capsule.database_object_budget_seconds:
                report["objectPhaseSeconds"] = round(object_seconds, 3)
                report["objectBudget"] = _capsule_object_budget(
                    capsule, object_seconds, included_checkpoints, excluded_object_steps,
                )
                report["error"] = "database-object budget reached; stopped before the next feature checkpoint"
                report["databaseState"] = "verified" if included_checkpoints else "not-started"
                _finish_report(report, started, "failed", 2)
                break

            capsule = reload_selected_capsule()
            step_started = time.perf_counter()
            step_report["status"] = "running"
            step_report["startedAt"] = _utc_now()
            evidence_dir = run_evidence / f"step-{item['number']:03d}"
            evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            auxiliary_write_invoked = False
            try:
                if step["type"] == "feature-migrate":
                    checkpoint_number += 1
                    checkpoint_result = feature_release.execute_checkpoint(
                        root, capsule, target_plan, checkpoint_number, run_id=run_id, environ=os.environ,
                    )
                    result = StepResult(
                        checkpoint_result.exit_code,
                        checkpoint_result.evidence_paths,
                        checkpoint_result.message,
                    )
                    step_report["objectSeconds"] = checkpoint_result.object_seconds
                    step_report["sqlclSessions"] = checkpoint_result.session_count
                    step_report["checkpointVerificationId"] = getattr(checkpoint_result, "verification_id", None)
                    report["checkpointSqlclSessions"][str(checkpoint_number)] = checkpoint_result.session_count
                    object_seconds += checkpoint_result.object_seconds
                    included_checkpoints.append(checkpoint_number)
                    report["databaseState"] = checkpoint_result.database_state
                else:
                    _snapshot(manifest, plan, f"before step {item['number']}", environment)
                    expected_identity = None
                    if step["type"] in {"sql-script", "ords-import"}:
                        binding = _target_binding(step, root, environment)
                        prepared_binding = target_plan.targets.get(f"step:{item['number']}")
                        if not isinstance(prepared_binding, Mapping):
                            raise RolloutError(f"step {item['number']} has no prepared target identity")
                        if any(binding.get(field) != prepared_binding.get(field) for field in (
                            "environment", "operation", "logicalSchema", "connection", "expectedUser", "schema", "classification",
                        )):
                            raise RolloutError(f"step {item['number']} target profile no longer matches its prepared binding")
                        target = Target(
                            environment, binding["connection"], binding["expectedUser"],
                            binding["schema"], binding["classification"],
                        )
                        expected_identity = dict(prepared_binding["observed"])
                        scope = release_state.target_scope_from_identity(expected_identity)
                        lease_identity = {
                            "purpose": "rollout-auxiliary-step",
                            "runId": run_id,
                            "step": item["number"],
                            "planDigest": target_plan.plan_digest,
                        }
                        bound_step = dict(
                            step,
                            connection=binding["connection"],
                            expectedUser=binding["expectedUser"],
                            schema=binding["schema"],
                        )
                        with release_ownership.target_writer_lease(root, scope, lease_identity) as lease:
                            release_state.assert_auxiliary_step_allowed(
                                root, capsule, target_plan, item["number"], run_id=run_id, lease=lease,
                            )
                            observed = _observe_target_identity(target, evidence_dir / "target-identity")
                            if observed != expected_identity:
                                raise RolloutError(f"step {item['number']} target identity changed after preparation")
                            auxiliary_write_invoked = True
                            result = execute_step(
                                bound_step,
                                environment=environment,
                                repo_root=root,
                                evidence_dir=evidence_dir,
                                step_number=item["number"],
                                manifest=manifest,
                                plan=plan,
                                manifest_confirmed=True,
                                expected_identity=expected_identity,
                            )
                    else:
                        result = execute_step(
                            step,
                            environment=environment,
                            repo_root=root,
                            evidence_dir=evidence_dir,
                            step_number=item["number"],
                            manifest=manifest,
                            plan=plan,
                            manifest_confirmed=True,
                            expected_identity=expected_identity,
                        )
                    _snapshot(manifest, plan, f"after step {item['number']}", environment)
                    if step["type"] == "migrate" or (
                        step["type"] in {"sql-script", "ords-import"}
                        and step_report.get("phase") == "uncovered-object"
                    ):
                        excluded_object_steps.append(f"step-{item['number']:03d}:{step['type']}")
                    elapsed = time.perf_counter() - step_started
                    if step["type"] == "app-deploy":
                        report["applicationSeconds"] += elapsed
                    elif step["type"] == "ords-import":
                        report["ordsSeconds"] += elapsed
                        step_report["phaseSeconds"] = round(elapsed, 3)
                    elif step["type"] == "sql-script":
                        report["dataSeconds"] += elapsed
                        step_report["phaseSeconds"] = round(elapsed, 3)
                    if step["type"] in {"migrate", "sql-script", "ords-import"} and result.exit_code == 0:
                        report["databaseState"] = "verified"

                step_report["evidencePaths"] = list(result.evidence_paths)
                step_report["message"] = result.message
                step_report["endedAt"] = _utc_now()
                step_report["durationSeconds"] = round(time.perf_counter() - step_started, 3)
                report["objectPhaseSeconds"] = round(object_seconds, 3)
                report["objectBudget"] = _capsule_object_budget(
                    capsule, object_seconds, included_checkpoints, excluded_object_steps,
                )
                if result.exit_code != 0:
                    step_report["status"] = "failed"
                    step_report["error"] = result.message or f"step returned status {result.exit_code}"
                    report["error"] = step_report["error"]
                    if step["type"] in {"migrate", "sql-script", "ords-import"}:
                        report["databaseState"] = "unknown"
                    _finish_report(report, started, "failed", 2)
                    break
                capsule = reload_selected_capsule()
                step_report["status"] = "succeeded"
                receipt_path = _write_receipt(root, environment, manifest, item, step_report)
                step_report["receiptPath"] = relative_to_root(receipt_path, root).as_posix()
                if step["type"] == "feature-migrate" and object_seconds > capsule.database_object_budget_seconds:
                    report["error"] = "database-object budget exceeded after a verified checkpoint"
                    report["databaseState"] = "verified"
                    _finish_report(report, started, "failed", 2)
                    break
            except Exception as error:
                step_report["status"] = "failed"
                step_report["error"] = str(error)
                step_report["endedAt"] = _utc_now()
                step_report["durationSeconds"] = round(time.perf_counter() - step_started, 3)
                step_report["evidencePaths"] = _relative_paths((evidence_dir,), root)
                report["error"] = str(error)
                report["objectPhaseSeconds"] = round(object_seconds, 3)
                report["objectBudget"] = _capsule_object_budget(
                    capsule, object_seconds, included_checkpoints, excluded_object_steps,
                )
                if step["type"] == "migrate" or (
                    step["type"] in {"sql-script", "ords-import"} and auxiliary_write_invoked
                ):
                    report["databaseState"] = "unknown"
                _finish_report(report, started, "failed", 2)
                break

            _write_reports(report, report_paths)

        if report["status"] == "running":
            report["objectPhaseSeconds"] = round(object_seconds, 3)
            report["objectBudget"] = _capsule_object_budget(
                capsule, object_seconds, included_checkpoints, excluded_object_steps,
            )
            if object_seconds > capsule.database_object_budget_seconds:
                report["error"] = "database-object budget exceeded after verified work"
                report["databaseState"] = "verified"
                _finish_report(report, started, "failed", 2)
            else:
                _finish_report(report, started, "success", 0)
        _write_reports(report, report_paths)
        finish_local_run(verified=report["status"] == "success")
        _announce_report_paths(report_paths, root)
        return report["exitCode"]
    except KeyboardInterrupt:
        if report is not None and report_paths is not None:
            report["error"] = "feature capsule rollout was interrupted; inspect the last checkpoint evidence before retrying"
            _finish_report(report, started, "failed", 130)
            _write_reports(report, report_paths)
            finish_local_run()
            _announce_report_paths(report_paths, root)
        return 130
    except Exception as error:
        print(f"rollout error: {error}", file=sys.stderr)
        if report is not None and report_paths is not None:
            report["error"] = str(error)
            _finish_report(report, started, "failed", 2)
            _write_reports(report, report_paths)
            finish_local_run()
            _announce_report_paths(report_paths, root)
        return 2


def _run_legacy_rollout(
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
    prior_receipts: list[tuple[dict, dict, dict, Path]] = []
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
            "manifestPath": relative_to_root(manifest.path, root).as_posix(),
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
                prior_receipts.append((item, step_report, receipt, receipt_path))
                step_report.update({
                    "status": "pending-resume-verification",
                    "startedAt": receipt["startedAt"],
                    "endedAt": receipt["endedAt"],
                    "durationSeconds": receipt["durationSeconds"],
                    "evidencePaths": receipt["evidencePaths"],
                    "receiptPath": relative_to_root(receipt_path, root).as_posix(),
                    "targetBinding": receipt["targetBinding"],
                    "observedIdentity": receipt["observedIdentity"],
                    "message": "receipt matches; live target identity pending",
                })

        _assert_unreceipted_execution_range(root, manifest, environment, plan, from_step)

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
        report["evidenceDir"] = relative_to_root(run_evidence, root).as_posix()
        if environment in {"staging", "prod"}:
            confirmation_path = run_evidence / "rollout-confirmation.json"
            _write_confirmation_evidence(confirmation_path, manifest, plan, environment)
            report["confirmationPath"] = relative_to_root(confirmation_path, root).as_posix()

        if prior_receipts:
            _snapshot(manifest, plan, "before resuming receipted steps", environment)
            for item, step_report, receipt, _receipt_path_value in prior_receipts:
                binding = item["targetBinding"]
                if binding is not None:
                    identity_dir = run_evidence / "resume-checks" / f"step-{item['number']:03d}"
                    current_identity = _observe_target_identity(_target_from_binding(binding), identity_dir)
                    if current_identity != receipt["observedIdentity"]:
                        raise RolloutError(
                            f"step {item['number']} live database identity differs from its successful receipt; "
                            "inspect the completed step and reconcile before retrying"
                        )
                    _verify_migration_receipts(root, item["step"], binding, current_identity)
                    step_report["evidencePaths"] = [
                        *step_report["evidencePaths"],
                        relative_to_root(identity_dir, root).as_posix(),
                    ]
                step_report["status"] = "resumed"
                step_report["message"] = "verified prior receipt and live target identity"

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
                binding = item["targetBinding"]
                before_identity = None
                identity_evidence: list[str] = []
                if binding is not None:
                    before_dir = evidence_dir / "target-before"
                    before_identity = _observe_target_identity(_target_from_binding(binding), before_dir)
                    identity_evidence.append(relative_to_root(before_dir, root).as_posix())
                result = execute_step(
                    item["step"],
                    environment=environment,
                    repo_root=root,
                    evidence_dir=evidence_dir,
                    step_number=item["number"],
                    manifest=manifest,
                    plan=plan,
                    manifest_confirmed=manifest_confirmed,
                    expected_identity=before_identity,
                )
                if not isinstance(result, StepResult):
                    raise RolloutError("rollout step executor returned an invalid result")
                step_report["evidencePaths"] = list(result.evidence_paths)
                step_report["message"] = result.message
                if result.exit_code == 0:
                    _snapshot(manifest, plan, f"after step {item['number']}", environment)
                    if binding is not None:
                        if item["step"]["type"] in {"sql-script", "ords-import"} and result.observed_identity != before_identity:
                            raise RolloutError(
                                f"step {item['number']} SQLcl writer identity did not match the independently observed target"
                            )
                        after_dir = evidence_dir / "target-after"
                        after_identity = _observe_target_identity(_target_from_binding(binding), after_dir)
                        identity_evidence.append(relative_to_root(after_dir, root).as_posix())
                        if after_identity != before_identity:
                            raise RolloutError(
                                f"step {item['number']} database identity changed during execution; "
                                "the step cannot receive a successful rollout receipt"
                            )
                        _verify_migration_receipts(root, item["step"], binding, after_identity)
                        step_report["observedIdentity"] = after_identity
                    step_report["evidencePaths"] = list(dict.fromkeys([
                        *step_report["evidencePaths"], *identity_evidence,
                    ]))
                    step_report["endedAt"] = _utc_now()
                    step_report["durationSeconds"] = round(time.perf_counter() - step_started, 3)
                    receipt_path = _write_receipt(root, environment, manifest, item, {
                        **step_report,
                    })
                    step_report["receiptPath"] = relative_to_root(receipt_path, root).as_posix()
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


def run_rollout(
    manifest_path: Path | str,
    environment: str,
    *,
    from_step: int = 1,
    dry_run: bool = False,
    report_path: Path | str | None = None,
    repo_root: Path = ROOT,
    confirm: Callable[[str], bool] = _confirm_from_terminal,
    prepare_only: bool = False,
    capsule_path: Path | str | None = None,
    stage_report_path: Path | str | None = None,
    recovery_attempt: str | None = None,
    recovery_confirm: Callable[[str], str] | None = None,
) -> int:
    """Keep schema-1 rollout behavior intact and route capsule runs through v2."""
    if prepare_only or capsule_path is not None:
        if capsule_path is None:
            raise RolloutError("prepare-only requires a release capsule path")
        if prepare_only and dry_run:
            raise RolloutError("--prepare and --dry-run cannot be combined")
        if recovery_attempt is not None and not prepare_only:
            raise RolloutError("--recover is available only with --prepare")
        if stage_report_path is not None and (environment != "prod" or prepare_only or dry_run):
            raise RolloutError("--stage-report is available only for a production capsule rollout")
        return _run_capsule_rollout(
            manifest_path, environment, from_step=from_step, dry_run=dry_run,
            report_path=report_path, repo_root=repo_root, confirm=confirm,
            prepare_only=prepare_only, capsule_path=capsule_path,
            stage_report_path=stage_report_path, recovery_attempt=recovery_attempt,
            recovery_confirm=recovery_confirm,
        )
    if stage_report_path is not None or recovery_attempt is not None:
        raise RolloutError("stage report and recovery attempt require a feature capsule")
    if not dry_run:
        try:
            parsed_manifest = load_manifest(manifest_path, repo_root)
        except RolloutError:
            parsed_manifest = None
        if parsed_manifest is not None and parsed_manifest.schema_version == 2:
            manifest_label = str(manifest_path)
            print(
                "rollout error: schema-version 2 execution requires a frozen release capsule; no steps were run.\n"
                f"Freeze: scripts/team.sh feature freeze {manifest_label}\n"
                f"Prepare: scripts/team.sh rollout {manifest_label} --env {environment} --prepare --capsule <capsule-dir>\n"
                f"Run: scripts/team.sh rollout {manifest_label} --env {environment} --capsule <capsule-dir>",
                file=sys.stderr,
            )
            return 2
    return _run_legacy_rollout(
        manifest_path, environment, from_step=from_step, dry_run=dry_run,
        report_path=report_path, repo_root=repo_root, confirm=confirm,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="ordered rollout manifest JSON, relative to the repository root")
    parser.add_argument("--env", required=True, choices=("dev", "staging", "prod"))
    parser.add_argument("--from-step", type=int, default=1, help="resume at a one-based step after verifying earlier receipts")
    parser.add_argument("--dry-run", action="store_true", help="validate and print the hashed plan without running steps")
    parser.add_argument("--report", type=Path, help="report base path or .json/.md path; both formats are written")
    parser.add_argument("--prepare", action="store_true", help="prepare a feature capsule target plan without writing to the database")
    parser.add_argument("--capsule", type=Path, help="selected frozen feature release capsule")
    parser.add_argument("--stage-report", type=Path, help="verified staging report required for production promotion")
    parser.add_argument("--recover", help="prepare a reviewed recovery plan for an unresolved attempt UUID")
    args = parser.parse_args(argv)
    if args.prepare and args.dry_run:
        parser.error("--prepare and --dry-run cannot be combined")
    if args.prepare and args.capsule is None:
        parser.error("--prepare requires --capsule")
    if args.recover is not None and not args.prepare:
        parser.error("--recover requires --prepare")
    if args.stage_report is not None and args.capsule is None:
        parser.error("--stage-report requires --capsule")
    if args.stage_report is not None and args.env != "prod":
        parser.error("--stage-report requires --env prod")
    if args.stage_report is not None and (args.prepare or args.dry_run):
        parser.error("--stage-report cannot be combined with --prepare or --dry-run")
    return run_rollout(
        args.manifest,
        args.env,
        from_step=args.from_step,
        dry_run=args.dry_run,
        report_path=args.report,
        prepare_only=args.prepare,
        capsule_path=args.capsule,
        stage_report_path=args.stage_report,
        recovery_attempt=args.recover,
        recovery_confirm=_recovery_ack_from_terminal if args.recover is not None else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
