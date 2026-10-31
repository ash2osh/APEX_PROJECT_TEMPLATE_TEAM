#!/usr/bin/env python3
"""Prepare selected feature targets with read-only guards and review evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import baseline, compare_schema, deployment_descriptor, feature_manifest, feature_preflight, feature_sources, migration_attempts, migration_checks, migrate, new_migration, release_capsule, release_ownership, release_state, rollout, sqlcl_session
from .db_targets import Target, TargetResolutionError, looks_like_production_identity, resolve_target
from .feature_manifest import FeatureCheckpoint, FeatureInput
from .migration_checks import complete_observed_identity
from .migration_manifest import QueryCheck
from .path_utils import relative_to_root
from .schema_catalog import CatalogError
from .validate_migration import statement_spans


PLAN_SCHEMA_VERSION = 3
HASH_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
PLAN_ENVIRONMENTS = {"staging", "prod"}
STRUCTURAL_SECTIONS = (
    *feature_preflight.STRUCTURAL_SECTIONS,
)
SELECTED_SECTIONS = tuple(sorted((*STRUCTURAL_SECTIONS, "baseline-source", "baseline-settings")))
SETTING_FIELDS = {
    "PLSQL_OPTIMIZE_LEVEL": "plsql_optimize_level",
    "PLSQL_CODE_TYPE": "plsql_code_type",
    "PLSQL_DEBUG": "plsql_debug",
    "PLSQL_WARNINGS": "plsql_warnings",
    "PLSQL_CCFLAGS": "plsql_ccflags",
    "NLS_LENGTH_SEMANTICS": "nls_length_semantics",
    "PLSCOPE_SETTINGS": "plscope_settings",
}
REQUIRED_SETTINGS = tuple(SETTING_FIELDS.values())


class FeatureReleaseError(ValueError):
    """Target preparation is unsafe or cannot produce complete evidence."""


@dataclass(frozen=True)
class TargetPlan:
    environment: str
    payload_digest: str
    plan_digest: str
    targets: dict[str, dict]
    checkpoint_guards: tuple[dict, ...]
    review_paths: tuple[str, ...]
    recovery_attempt: str | None
    recovery_binding: dict | None = None


@dataclass(frozen=True)
class CheckpointResult:
    exit_code: int
    database_state: str
    object_seconds: float
    session_count: int
    evidence_paths: tuple[str, ...]
    message: str
    verification_id: str | None = None


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _root(repo_root: Path) -> Path:
    requested = Path(repo_root)
    if requested.is_symlink():
        raise FeatureReleaseError("repository root cannot be a symlink")
    try:
        root = requested.resolve(strict=True)
    except OSError as error:
        raise FeatureReleaseError(f"repository root is unavailable: {error}") from error
    if not root.is_dir():
        raise FeatureReleaseError("repository root must be a directory")
    return root


def _safe_repo_path(root: Path, path: Path, label: str) -> Path:
    try:
        relative = relative_to_root(path, root)
    except ValueError as error:
        raise FeatureReleaseError(f"{label} is outside the repository: {path}") from error
    current = root
    try:
        info = current.lstat()
        if current.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise FeatureReleaseError("repository root is a symlink or reparse point")
        for part in relative.parts:
            current = current / part
            info = current.lstat()
            if current.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise FeatureReleaseError(f"{label} contains a symlink or reparse point: {current}")
    except FeatureReleaseError:
        raise
    except OSError as error:
        raise FeatureReleaseError(f"{label} is unavailable: {path}") from error
    return path


def _secure_directory(path: Path, root: Path, *, private: bool = True) -> None:
    try:
        relative = relative_to_root(path, root)
    except ValueError as error:
        raise FeatureReleaseError(f"feature release directory is outside the repository: {path}") from error
    current = root
    try:
        for index, part in enumerate(relative.parts):
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                current.mkdir(mode=0o700 if private else 0o755)
                info = current.lstat()
            if current.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400) or not stat.S_ISDIR(info.st_mode):
                raise FeatureReleaseError(f"feature release directory is unsafe: {current}")
            if private and os.name != "nt" and index == len(relative.parts) - 1:
                current.chmod(0o700)
    except FeatureReleaseError:
        raise
    except OSError as error:
        raise FeatureReleaseError(f"cannot prepare feature release directory {path}: {error}") from error


def _identity(target: Target, value: object) -> dict[str, str]:
    observed = complete_observed_identity(value)
    if observed is None:
        raise FeatureReleaseError("target identity is incomplete; preparation stopped")
    if observed["session_user"].upper() != target.expected_user:
        raise FeatureReleaseError(
            f"target identity session user {observed['session_user']!r} does not match {target.expected_user}"
        )
    if observed["current_schema"].upper() != target.schema:
        raise FeatureReleaseError(
            f"target identity current schema {observed['current_schema']!r} does not match {target.schema}"
        )
    if target.environment != "prod" and looks_like_production_identity(
        observed["db_name"], observed["db_unique_name"], observed["service_name"],
    ):
        raise FeatureReleaseError("selected target identity resembles production")
    return observed


def _selected_names(checkpoint: FeatureCheckpoint) -> tuple[list[str], list[str]]:
    try:
        scope = feature_preflight.required_scope(checkpoint)
    except feature_preflight.StructuralPreflightError as error:
        raise FeatureReleaseError(f"selected feature scope is unsafe: {error}") from error
    object_names = list(dict.fromkeys(
        key.name for key in (*scope.object_keys, *scope.prerequisite_keys)
    ))
    source_names = list(dict.fromkeys(
        item.unit[2] for item in checkpoint.inputs if item.category == "stored-source" and item.unit is not None
    ))
    object_names = list(dict.fromkeys((*object_names, *source_names)))
    if not object_names and not source_names:
        raise FeatureReleaseError("feature checkpoint has no selected catalog objects")
    return object_names, source_names


def capture_selected_state(target: Target, checkpoint: FeatureCheckpoint, run_dir: Path) -> dict:
    """Read only selected object structure and stored source/settings on the chosen target."""
    if not isinstance(target, Target) or not isinstance(checkpoint, FeatureCheckpoint):
        raise FeatureReleaseError("selected-state capture requires a target and validated feature checkpoint")
    if target.environment not in PLAN_ENVIRONMENTS:
        raise FeatureReleaseError("feature target preparation supports staging or prod")
    if checkpoint.schema != target.schema:
        raise FeatureReleaseError("feature checkpoint schema does not match the selected target")
    object_names, source_names = _selected_names(checkpoint)
    sections = list(STRUCTURAL_SECTIONS)
    if source_names:
        sections.extend(("baseline-source", "baseline-settings"))
    # Dependency rows are private to feature preflight; they share the guarded
    # selected-name capture and do not assert whole-owner inventory coverage.
    if "feature-dependencies" not in sections:
        sections.append("feature-dependencies")
    try:
        catalog = compare_schema.capture_environment_catalog(
            target,
            target.environment,
            sections,
            run_dir,
            dba_capture=False,
            baseline_capture=True,
            baseline_names=source_names,
            scope_names=object_names,
        )
        baseline._complete(catalog, sections)
        identity = baseline._source_identity(catalog.get("identity"), f"{target.environment} selected feature capture")
    except (baseline.BaselineError, CatalogError, OSError, RuntimeError, ValueError) as error:
        if isinstance(error, FeatureReleaseError):
            raise
        raise FeatureReleaseError(f"selected {target.environment} capture failed: {error}") from error
    if identity["session_user"].upper() != target.expected_user or identity["current_schema"].upper() != target.schema:
        raise FeatureReleaseError("selected source capture returned a conflicting schema or session identity")

    config = {"prefixes": [], "excludedObjects": []}
    try:
        source_rows = baseline._source_units(catalog, config, target.schema)
        settings_rows = baseline._settings_map(catalog, config, target.schema)
    except baseline.BaselineError as error:
        raise FeatureReleaseError(f"selected stored-source capture is incomplete: {error}") from error
    code_status = {
        (row.get("type"), row.get("name")): str(row.get("status", "")).upper()
        for row in catalog["sections"].get("stored-code", [])
        if isinstance(row, Mapping) and isinstance(row.get("type"), str) and isinstance(row.get("name"), str)
    }
    # ASHARIF 2026-10-28: use the separate ALL_OBJECTS status for triggers, never ENABLED/DISABLED.
    for row in catalog["sections"].get("triggers", []):
        if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
            continue
        status = str(row.get("object_status", "")).upper()
        if status in {"VALID", "INVALID"}:
            code_status[("TRIGGER", row["name"])] = status
    source_units: dict[str, dict] = {}
    for item in checkpoint.inputs:
        if item.category != "stored-source" or item.unit is None:
            continue
        _owner, object_type, name = item.unit
        key = (object_type, name)
        actual = source_rows.get(key)
        setting_row = settings_rows.get(key)
        if actual is None:
            if setting_row is not None:
                raise FeatureReleaseError(f"compiler settings exist without stored source for {object_type} {name}")
            continue
        if setting_row is None:
            raise FeatureReleaseError(f"compiler settings are missing for selected {object_type} {name}")
        try:
            settings = baseline._setting_row([setting_row], name, object_type)
        except baseline.BaselineError as error:
            raise FeatureReleaseError(f"compiler settings are incomplete for {object_type} {name}: {error}") from error
        status = code_status.get(key)
        if status not in {"VALID", "INVALID"}:
            raise FeatureReleaseError(f"validity state is unavailable for selected {object_type} {name}")
        source_units[f"{object_type}:{name}"] = {
            "lines": list(actual["lines"]),
            "settings": settings,
            "valid": status == "VALID",
        }

    structural = {
        section: catalog["sections"].get(section, [])
        for section in STRUCTURAL_SECTIONS
    }
    return {
        "schema": target.schema,
        "identity": identity,
        "sourceUnits": source_units,
        "structural": structural,
        "coverage": {
            "ownerComplete": False,
            "selectedScopeComplete": True,
            "sections": {section: dict(catalog["coverage"]["sections"][section]) for section in sections},
        },
    }


def _stored_source(item: FeatureInput) -> tuple[dict, dict[str, str]]:
    if item.unit is None or item.category != "stored-source":
        raise FeatureReleaseError(f"selected feature source is not a stored unit: {item.path}")
    try:
        text = item.source.decode("utf-8")
        spans = statement_spans(text)
    except (UnicodeError, ValueError) as error:
        raise FeatureReleaseError(f"cannot parse validated stored source {item.path}: {error}") from error
    if not spans:
        raise FeatureReleaseError(f"validated stored source is empty: {item.path}")
    body_start, body_end = spans[-1]
    body = text[body_start:body_end]
    create = re.match(
        r"(?is)^\s*CREATE\s+(?:(?:OR\s+REPLACE|AND\s+(?:RESOLVE|COMPILE)|NOFORCE|EDITIONABLE|NONEDITIONABLE|FORCE)\s+)*",
        body,
    )
    if create is None:
        raise FeatureReleaseError(f"stored source has no CREATE statement: {item.path}")
    lines = body[create.end():].splitlines(keepends=True)
    if not lines:
        raise FeatureReleaseError(f"stored source body is empty: {item.path}")
    owner, object_type, name = item.unit
    identifier = r'(?:"(?:[^"]|"")*"|[A-Za-z][A-Za-z0-9_$#]*)'
    kind = r"\s+".join(re.escape(part) for part in object_type.split())
    header = re.match(
        rf"(?is)^(?P<indent>\s*)(?P<kind>{kind})\s+(?P<first>{identifier})(?:\s*\.\s*(?P<second>{identifier}))?",
        lines[0],
    )
    if header is None:
        raise FeatureReleaseError(f"stored source has no recognizable object header: {item.path}")

    def identifier_value(token: str) -> str:
        return token[1:-1].replace('""', '"') if token.startswith('"') else token.upper()

    first = identifier_value(header.group("first"))
    second = identifier_value(header.group("second")) if header.group("second") is not None else None
    actual_owner, actual_name = (first, second) if second is not None else (None, first)
    if actual_name != name or actual_owner not in {None, owner}:
        raise FeatureReleaseError(f"stored source header does not match validated unit {object_type} {name}: {item.path}")
    if actual_owner is not None:
        # ALL_SOURCE blanks the owner qualifier and keeps its columns, so the expected header does too.
        owner_start, name_start = header.start("first"), header.start("second")
        lines[0] = lines[0][:owner_start] + " " * (name_start - owner_start) + lines[0][name_start:]
    overrides: dict[str, str] = {}
    for start, end in spans[:-1]:
        statement = text[start:end].strip()
        selected = None
        for setting_name, pattern in feature_manifest.SESSION_SETTINGS:
            match = pattern.fullmatch(statement)
            if match is not None:
                selected = (setting_name, match.group(1) if match.lastindex else "")
                break
        if selected is None:
            raise FeatureReleaseError(f"stored source contains an unrecognized compiler setting: {item.path}")
        key, raw = selected
        if raw.startswith("'") and raw.endswith("'"):
            raw = raw[1:-1].replace("''", "'")
        overrides[SETTING_FIELDS[key]] = raw
    return {"type": item.unit[1], "name": item.unit[2], "lines": lines}, overrides


def _validated_settings(name: str, object_type: str, values: Mapping[str, object]) -> dict:
    if set(values) != set(REQUIRED_SETTINGS):
        raise FeatureReleaseError(f"compiler settings are incomplete for {object_type} {name}")
    row = {"name": name, "type": object_type, **dict(values)}
    try:
        return baseline._setting_row([row], name, object_type)
    except baseline.BaselineError as error:
        raise FeatureReleaseError(f"compiler settings are invalid for {object_type} {name}: {error}") from error


def _settings_prologue(name: str, object_type: str, values: Mapping[str, object]) -> tuple[str, str]:
    """Return canonical target settings bytes and their digest for one unit."""
    validated = _validated_settings(name, object_type, values)
    text = baseline._session_settings_sql(validated) + "\n"
    return text, _sha256(text.encode("utf-8"))


def _validated_source_prologue(source_guard: Mapping) -> str:
    unit = source_guard.get("unit")
    settings = source_guard.get("applySettings")
    if not isinstance(unit, Mapping) or not isinstance(settings, Mapping):
        raise FeatureReleaseError("prepared compiler settings prologue is missing its unit or settings")
    name, object_type = unit.get("name"), unit.get("type")
    if not isinstance(name, str) or not isinstance(object_type, str):
        raise FeatureReleaseError("prepared compiler settings prologue has a malformed unit")
    expected_text, expected_sha256 = _settings_prologue(name, object_type, settings)
    if source_guard.get("settingsPrologue") != expected_text or source_guard.get("settingsPrologueSha256") != expected_sha256:
        raise FeatureReleaseError(f"prepared compiler settings prologue changed for {object_type} {name}")
    return expected_text


def _guard_query(query: Mapping) -> dict:
    value = {"id": query.get("id"), "sql": query.get("sql"), "expected": query.get("expected", 1)}
    if not isinstance(value["id"], str) or not isinstance(value["sql"], str) or type(value["expected"]) is not int:
        raise FeatureReleaseError("generated source guard query is malformed")
    return value


def _header_tokens(line: str) -> list[str]:
    tokens = re.findall(r'"(?:[^"]|"")*"|[^\s"]+', line)
    return [token[1:-1].replace('""', '"') if token.startswith('"') else token.upper() for token in tokens]


def _same_stored_source(live_lines: list[str], expected_lines: list[str]) -> bool:
    """Compare captured source with the live text.

    The header names the unit, so its keywords may differ in case, its name may be quoted or
    not, and blanks may stand where an owner qualifier was. The body must match exactly.
    """
    if not live_lines or len(live_lines) != len(expected_lines):
        return live_lines == expected_lines
    return (
        _header_tokens(live_lines[0]) == _header_tokens(expected_lines[0])
        and live_lines[1:] == expected_lines[1:]
    )


def _same_settings(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    """Compare compiler settings by value. The live query returns numbers and the validator
    returns text, and an empty PLSQL_CCFLAGS is stored as NULL."""
    def text(value: object) -> str:
        return "" if value is None else str(value)

    return {key: text(value) for key, value in left.items()} == {key: text(value) for key, value in right.items()}


def _source_guards(checkpoint: FeatureCheckpoint, state: Mapping, checkpoint_number: int) -> list[dict]:
    actual_units = state["sourceUnits"]
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in checkpoint.inputs:
        if item.category != "stored-source" or item.unit is None:
            continue
        _owner, object_type, name = item.unit
        key = (object_type, name)
        if key in seen:
            continue
        seen.add(key)
        actual = actual_units.get(f"{object_type}:{name}")
        expected_unit, setting_overrides = _stored_source(item)
        expected_lines = expected_unit["lines"]
        before_settings = actual["settings"] if actual is not None else None
        if setting_overrides:
            if before_settings is None and set(setting_overrides) != set(REQUIRED_SETTINGS):
                raise FeatureReleaseError(
                    f"new {object_type} {name} needs complete compiler settings so its target state can be verified"
                )
            merged = {**(before_settings or {}), **setting_overrides}
            apply_settings = _validated_settings(name, object_type, merged)
        else:
            apply_settings = before_settings

        expected_valid = True
        if actual is None:
            preconditions = [{
                "id": f"source-absent-{checkpoint_number:03d}-{len(result) + 1:03d}",
                "sql": (
                    "SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM ALL_OBJECTS "
                    f"WHERE OWNER = '{checkpoint.schema}' AND OBJECT_NAME = {_sql_literal(name)} "
                    f"AND OBJECT_TYPE = {_sql_literal(object_type)}) THEN 1 ELSE 0 END FROM DUAL"
                ),
                "expected": 1,
            }]
            if apply_settings is None:
                raise FeatureReleaseError(f"new {object_type} {name} needs explicit compiler settings")
            before_lines: list[str] = []
        else:
            preconditions = [
                _guard_query(query)
                for query in baseline._source_check_queries(
                    {**actual, "name": name, "type": object_type}, checkpoint.schema,
                )
            ]
            preconditions.append(
                _guard_query(baseline._exact_settings_check(before_settings, name, object_type, checkpoint.schema))
            )
            validity = 1 if actual.get("valid") is True else 0
            preconditions.append({
                "id": f"validity-{checkpoint_number:03d}-{len(result) + 1:03d}",
                "sql": (
                    "SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_OBJECTS "
                    f"WHERE OWNER = '{checkpoint.schema}' AND OBJECT_NAME = {_sql_literal(name)} "
                    f"AND OBJECT_TYPE = {_sql_literal(object_type)} AND STATUS = '{'VALID' if validity else 'INVALID'}') "
                    "THEN 1 ELSE 0 END FROM DUAL"
                ),
                "expected": 1,
            })
            before_lines = list(actual["lines"])
            expected_valid = actual.get("valid") is True

        expected_matches = _same_stored_source(before_lines, expected_lines)
        settings_match = not setting_overrides or (
            before_settings is not None and _same_settings(apply_settings, before_settings)
        )
        no_op = actual is not None and expected_matches and settings_match and expected_valid
        if not isinstance(apply_settings, Mapping):
            raise FeatureReleaseError(f"compiler settings are missing for {object_type} {name}")
        settings_prologue, settings_prologue_sha256 = _settings_prologue(name, object_type, apply_settings)
        source_checks = [
            _guard_query(query)
            for query in baseline._source_check_queries({"name": name, "type": object_type, "lines": before_lines}, checkpoint.schema)
        ] if actual is not None else []
        result.append({
            "unit": {"owner": checkpoint.schema, "type": object_type, "name": name},
            "targetExists": actual is not None,
            "beforeSourceSha256": _sha256("".join(before_lines).encode("utf-8")) if actual is not None else None,
            "expectedSourceSha256": _sha256("".join(expected_lines).encode("utf-8")),
            "beforeSettings": before_settings,
            "applySettings": apply_settings,
            "settingsPrologue": settings_prologue,
            "settingsPrologueSha256": settings_prologue_sha256,
            "valid": expected_valid if actual is not None else None,
            "noOp": no_op,
            "preconditions": preconditions,
            "sourceVerification": source_checks,
        })
    return result


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _captured_dev_baseline(root: Path, item: FeatureInput) -> tuple[str, dict] | None:
    if item.unit is None:
        return None
    parts = PurePosixPath(item.path).parts
    if len(parts) < 4 or parts[0] != "features":
        return None
    feature = parts[1]
    metadata_dir = root / "scratch" / "feature-source" / feature / item.unit[0]
    # Feature source trees use the schema (item.unit owner), which is also the
    # source guard owner for every validated stored unit.
    if not metadata_dir.exists():
        return None
    candidates = sorted(metadata_dir.glob("capture-*.json"))
    for metadata_path in candidates:
        raw = feature_sources._ordinary_file(metadata_path, root)
        if raw is None:
            continue
        metadata = feature_sources._strict_capture_metadata(metadata_path, root)
        if metadata is None or metadata.get("feature") != feature or metadata.get("schema") != item.unit[0]:
            continue
        for unit in metadata["units"]:
            if not isinstance(unit, Mapping) or (unit.get("type"), unit.get("name"), unit.get("path")) != (item.unit[1], item.unit[2], item.path):
                continue
            relative = feature_sources._metadata_path(
                unit.get("capturedPath"),
                prefix=("scratch", "feature-source", feature, item.unit[0], "captured"),
                label="capturedPath",
            ).as_posix()
            path = _safe_repo_path(root, root / relative, "captured DEV source")
            try:
                source = path.read_bytes()
                text = source.decode("utf-8")
            except (OSError, UnicodeError) as error:
                raise FeatureReleaseError(f"captured DEV source is unavailable or malformed: {relative}") from error
            if _sha256(source) != unit.get("sha256"):
                raise FeatureReleaseError(f"captured DEV source hash does not match its metadata: {relative}")
            identity = baseline._source_identity(metadata.get("identity"), "captured DEV source")
            if identity["current_schema"].upper() != item.unit[0]:
                raise FeatureReleaseError("captured DEV source metadata has a conflicting schema")
            return text, identity
    return None


def _review_specs(root: Path, checkpoint: FeatureCheckpoint, state: Mapping, payload_digest: str) -> list[dict]:
    specs: list[dict] = []
    actual_units = state["sourceUnits"]
    for item in checkpoint.inputs:
        if item.category != "stored-source" or item.unit is None:
            continue
        object_type, name = item.unit[1], item.unit[2]
        actual = actual_units.get(f"{object_type}:{name}")
        before_lines = list(actual["lines"]) if actual is not None else []
        before_settings = actual["settings"] if actual is not None else None
        before = new_migration.step_sql(checkpoint.schema, object_type, name, before_lines, settings=before_settings) if actual else ""
        try:
            after = item.source.decode("utf-8")
        except UnicodeDecodeError as error:
            raise FeatureReleaseError(f"selected feature source is not UTF-8: {item.path}") from error
        baselines = [(f"{state['environment']}-target", before, state["identity"])]
        captured = _captured_dev_baseline(root, item)
        if captured is not None:
            baselines.append(("dev-capture", captured[0], captured[1]))
        for baseline_kind, old_source, old_identity in baselines:
            observed_identity = feature_sources._identity_document(old_identity)
            diff = feature_sources._review_diff(
                old_source, after, PurePosixPath(item.path).parts[1], checkpoint.schema, (object_type, name),
            )
            binding = {
                "schemaVersion": 1,
                "feature": PurePosixPath(item.path).parts[1],
                "schema": checkpoint.schema,
                "owner": checkpoint.schema,
                "unitType": object_type,
                "unitName": name,
                "observedIdentity": observed_identity,
                "beforeSha256": _sha256(old_source.encode("utf-8")),
                "afterSha256": _sha256(after.encode("utf-8")),
                "payloadDigest": payload_digest,
                "baselineKind": baseline_kind,
                "diffSha256": _sha256(diff),
            }
            specs.append({**binding, "before": old_source, "after": after, "identity": observed_identity})
    return specs


def _write_reviews(root: Path, specs: Sequence[Mapping], plan_digest: str, day: str) -> list[str]:
    paths: list[str] = []
    for spec in specs:
        diff_path, review_path = feature_sources.write_source_review(
            root,
            spec["feature"],
            spec["schema"],
            (spec["unitType"], spec["unitName"]),
            spec["before"],
            spec["after"],
            identity=spec["identity"],
            baseline_kind=spec["baselineKind"],
            payload_digest=spec["payloadDigest"],
            day=day,
            target_plan_digest=plan_digest,
        )
        paths.extend((
            relative_to_root(diff_path, root).as_posix(),
            relative_to_root(review_path, root).as_posix(),
        ))
    return list(dict.fromkeys(paths))


def _app_targets(root: Path, steps: Sequence[Mapping], environment: str, environ: Mapping[str, str]) -> dict[str, dict]:
    targets: dict[str, dict] = {}
    for step in steps:
        if step.get("type") != "app-deploy":
            continue
        app_id = step.get("appId")
        if type(app_id) is not int:
            raise FeatureReleaseError("capsule app-deploy step has an invalid app ID")
        try:
            app_dir = rollout._find_app_directory(root, app_id)
            from .validate_app_source import validate_app_source

            validate_app_source(root, app_dir)
            descriptor_path = app_dir / "deployments" / f"{environment}.json"
            _safe_repo_path(root, descriptor_path, "APEX deployment descriptor")
            descriptor = deployment_descriptor.read_descriptor(descriptor_path, app_id)
            descriptor_hash = _sha256(descriptor_path.read_bytes())
            schema = descriptor["app"]["databaseSession"]["parsingSchema"]
            target = resolve_target(environ, environment, "read", schema=schema)
        except (OSError, ValueError, TargetResolutionError) as error:
            raise FeatureReleaseError(f"application {app_id} target descriptor/profile is invalid: {error}") from error
        targets[f"app:{app_id}"] = {
            "workspace": descriptor["workspace"]["name"],
            "schema": schema,
            "connection": target.connection,
            "expectedUser": target.expected_user,
            "descriptorPath": relative_to_root(descriptor_path, root).as_posix(),
            "descriptorSha256": descriptor_hash,
        }
    return targets


def _auxiliary_targets(root: Path, steps: Sequence[Mapping], environment: str, environ: Mapping[str, str]) -> dict[str, dict]:
    """Capture read-only identities for schema-2 SQL and ORDS target bindings."""
    targets: dict[str, dict] = {}
    for number, step in enumerate(steps, start=1):
        if step.get("type") not in {"sql-script", "ords-import"}:
            continue
        logical_schema = step.get("schema")
        if not isinstance(logical_schema, str):
            raise FeatureReleaseError(f"step {number} has no logical schema for its auxiliary target")
        try:
            target = resolve_target(environ, environment, "migration", schema=logical_schema)
        except TargetResolutionError as error:
            raise FeatureReleaseError(
                f"cannot resolve {environment} migration target for step {number} schema {logical_schema}: {error}"
            ) from error
        if step["type"] == "ords-import" and (
            target.expected_user != target.schema or target.schema != logical_schema
        ):
            raise FeatureReleaseError(
                f"step {number} ORDS target must use the actual REST owner named by the frozen export"
            )
        run_dir = root / "scratch" / "feature-release-prepare" / environment / f"step-{number:03d}-identity"
        try:
            identity = _identity(target, rollout._observe_target_identity(target, run_dir))
        except (OSError, rollout.RolloutError, TargetResolutionError) as error:
            raise FeatureReleaseError(f"cannot capture complete target identity for step {number}: {error}") from error
        targets[f"step:{number}"] = {
            "environment": environment,
            "operation": "migration",
            "logicalSchema": logical_schema,
            "connection": target.connection,
            "expectedUser": target.expected_user,
            "schema": target.schema,
            "classification": target.classification,
            "observed": identity,
        }
    return targets


def _manifest_steps(capsule: release_capsule.ReleaseCapsule) -> list[dict]:
    path = capsule.root / "capsule.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        steps = document["payloadDefinition"]["steps"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        steps = []
    if not isinstance(steps, list):
        raise FeatureReleaseError("capsule rollout steps are malformed")
    feature_steps = [
        {"type": "feature-migrate", "schema": checkpoint.schema}
        for checkpoint in capsule.checkpoints
    ]
    if not steps:
        return feature_steps
    if sum(1 for step in steps if isinstance(step, dict) and step.get("type") == "feature-migrate") != len(capsule.checkpoints):
        raise FeatureReleaseError("capsule feature steps do not match its validated checkpoints")
    return steps


def _validated_capsule(root: Path, capsule: release_capsule.ReleaseCapsule) -> release_capsule.ReleaseCapsule:
    try:
        loaded = release_capsule.load_capsule(root, capsule.root)
    except release_capsule.ReleaseCapsuleError as error:
        raise FeatureReleaseError(f"release capsule validation failed before target preparation: {error}") from error
    if (
        loaded.manifest_sha256 != capsule.manifest_sha256
        or loaded.payload_digest != capsule.payload_digest
        or loaded.engine_digest != capsule.engine_digest
        or loaded.source_commit != capsule.source_commit
        or loaded.checkpoints != capsule.checkpoints
        or loaded.input_hashes != capsule.input_hashes
        or loaded.database_object_budget_seconds != capsule.database_object_budget_seconds
    ):
        raise FeatureReleaseError("release capsule changed after it was loaded; reload it before target preparation")
    return loaded


def _plan_data(
    environment: str,
    capsule: release_capsule.ReleaseCapsule,
    targets: Mapping[str, dict],
    guards: Sequence[dict],
    recovery_attempt: str | None,
    recovery_binding: Mapping | None,
) -> dict:
    payload = {
        "schemaVersion": PLAN_SCHEMA_VERSION,
        "environment": environment,
        "payloadDigest": capsule.payload_digest,
        "engineDigest": capsule.engine_digest,
        "sourceCommit": capsule.source_commit,
        "recoveryAttempt": recovery_attempt,
        "targets": dict(targets),
        "checkpointGuards": list(guards),
        "recoveryBinding": dict(recovery_binding) if recovery_binding is not None else None,
    }
    payload["planDigest"] = _sha256(_canonical_bytes(payload))
    payload["reviewPaths"] = []
    return payload


def _validate_review_artifacts(root: Path, document: Mapping, plan_digest: str) -> None:
    raw_paths = document.get("reviewPaths")
    guards = document.get("checkpointGuards")
    if not isinstance(raw_paths, list) or not isinstance(guards, list):
        raise FeatureReleaseError("target plan review evidence is malformed")
    paths: list[str] = []
    by_directory: dict[str, set[str]] = {}
    for relative in raw_paths:
        if (
            not isinstance(relative, str) or "\\" in relative
            or PurePosixPath(relative).is_absolute()
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not relative.startswith("ai_generate/")
        ):
            raise FeatureReleaseError("target plan review path is malformed")
        _safe_repo_path(root, root / PurePosixPath(relative), "target plan review")
        if relative in paths:
            raise FeatureReleaseError("target plan review path is duplicated")
        paths.append(relative)
        path = PurePosixPath(relative)
        if path.name not in {"review.json", "source.diff"}:
            raise FeatureReleaseError("target plan review path has an unexpected filename")
        by_directory.setdefault(path.parent.as_posix(), set()).add(path.name)
    if any(names != {"review.json", "source.diff"} for names in by_directory.values()):
        raise FeatureReleaseError("target plan review evidence is missing its diff or review metadata")

    fields = {
        "schemaVersion", "feature", "schema", "owner", "unitType", "unitName", "observedIdentity",
        "beforeSha256", "afterSha256", "payloadDigest", "baselineKind", "diffSha256",
    }
    expected: list[dict] = []
    for guard in guards:
        if not isinstance(guard, Mapping) or not isinstance(guard.get("reviews", []), list):
            raise FeatureReleaseError("target checkpoint review bindings are malformed")
        for binding in guard.get("reviews", []):
            if not isinstance(binding, Mapping) or set(binding) != fields:
                raise FeatureReleaseError("target checkpoint review binding has unsupported or missing fields")
            expected.append(dict(binding))

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    actual: list[dict] = []
    for directory, _names in by_directory.items():
        review_relative = f"{directory}/review.json"
        diff_relative = f"{directory}/source.diff"
        try:
            review_text = (root / review_relative).read_text(encoding="utf-8")
            review = json.loads(review_text, object_pairs_hook=unique_pairs)
            diff = (root / diff_relative).read_bytes()
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise FeatureReleaseError(f"target plan review evidence is unreadable: {directory}: {error}") from error
        if not isinstance(review, dict) or set(review) != fields | {"targetPlanDigest"}:
            raise FeatureReleaseError("target plan review metadata has unsupported or missing fields")
        if review.get("targetPlanDigest") != plan_digest or review.get("payloadDigest") != document.get("payloadDigest"):
            raise FeatureReleaseError("target plan review metadata is bound to a different plan or payload")
        if not isinstance(review.get("diffSha256"), str) or _sha256(diff) != review["diffSha256"]:
            raise FeatureReleaseError("target plan source diff does not match its review metadata")
        actual.append({key: value for key, value in review.items() if key != "targetPlanDigest"})
    if sorted(_canonical_bytes(item) for item in actual) != sorted(_canonical_bytes(item) for item in expected):
        raise FeatureReleaseError("target plan review artifacts do not match their digest-bound source evidence")


def _write_plan(root: Path, environment: str, payload_digest: str, document: dict) -> Path:
    directory = root / "scratch" / "feature-release-plans" / environment
    _secure_directory(directory, root)
    destination = directory / f"{payload_digest}.json"
    if destination.is_symlink():
        raise FeatureReleaseError("target plan destination cannot be a symlink")
    try:
        descriptor, name = tempfile.mkstemp(prefix=".target-plan-", dir=directory)
        staged = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write((json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            staged.chmod(0o600)
        os.replace(staged, destination)
        if os.name != "nt":
            destination.chmod(0o600)
    except OSError as error:
        raise FeatureReleaseError(f"cannot persist target plan: {error}") from error
    return destination


def prepare_target(
    repo_root: Path,
    capsule: release_capsule.ReleaseCapsule,
    environment: str,
    *,
    environ: Mapping[str, str],
    recovery_attempt: str | None = None,
    terminal_ack: str | None = None,
) -> TargetPlan:
    """Capture selected target state, create diffs and persist a target-specific plan."""
    root = _root(repo_root)
    if environment not in PLAN_ENVIRONMENTS:
        raise FeatureReleaseError("target preparation environment must be staging or prod")
    if not isinstance(capsule, release_capsule.ReleaseCapsule) or HASH_RE.fullmatch(capsule.payload_digest) is None or HASH_RE.fullmatch(capsule.engine_digest) is None:
        raise FeatureReleaseError("target preparation requires a validated release capsule")
    capsule = _validated_capsule(root, capsule)
    if recovery_attempt is not None and (not isinstance(recovery_attempt, str) or not recovery_attempt.strip()):
        raise FeatureReleaseError("recovery attempt must be a nonempty identifier or None")
    recovery_record = None
    if recovery_attempt is not None:
        try:
            recovery_record = release_state.recovery_attempt_details(
                root, capsule, environment, recovery_attempt,
            )
        except release_state.ReleaseStateError as error:
            raise FeatureReleaseError(f"recovery preparation refused: {error}") from error
        if terminal_ack != f"RECONCILE {recovery_attempt}":
            raise FeatureReleaseError(
                f"recovery preparation requires the exact terminal reconciliation acknowledgment: RECONCILE {recovery_attempt}"
            )
        try:
            with release_ownership.target_writer_lease(
                root,
                recovery_record["targetScope"],
                {"purpose": "recovery-preparation", "attemptId": recovery_attempt},
            ):
                pass
        except release_ownership.ReleaseOwnershipError as error:
            raise FeatureReleaseError(f"recovery preparation refused because a target writer is active: {error}") from error
    steps = _manifest_steps(capsule)
    targets = _app_targets(root, steps, environment, environ)
    targets.update(_auxiliary_targets(root, steps, environment, environ))
    guards: list[dict] = []
    review_specs: list[dict] = []
    shared_identity: dict[tuple[str, str], dict] = {}
    earlier_effects_by_schema: dict[str, list[dict]] = {}
    day = datetime.now(timezone.utc).date().isoformat()

    for checkpoint_number, checkpoint in enumerate(capsule.checkpoints, start=1):
        try:
            target = resolve_target(environ, environment, "migration", schema=checkpoint.schema)
        except TargetResolutionError as error:
            raise FeatureReleaseError(f"cannot resolve {environment} schema {checkpoint.schema}: {error}") from error
        run_dir = root / "scratch" / "feature-release-prepare" / environment / f"checkpoint-{checkpoint_number:03d}"
        try:
            state = capture_selected_state(target, checkpoint, run_dir)
        except FeatureReleaseError:
            raise
        try:
            structural_plan = feature_preflight.prepare_checkpoint(
                checkpoint,
                state,
                earlier_effects_by_schema.get(checkpoint.schema, ()),
            )
        except feature_preflight.StructuralPreflightError as error:
            raise FeatureReleaseError(f"selected structural preflight refused checkpoint {checkpoint_number}: {error}") from error
        identity = _identity(target, state.get("identity"))
        scope_key = (identity["db_unique_name"], identity["container_id"])
        prior_identity = shared_identity.get(scope_key)
        if prior_identity is not None and prior_identity["edition"] != identity["edition"]:
            raise FeatureReleaseError("selected feature checkpoints have conflicting edition identities on one physical target")
        shared_identity[scope_key] = identity
        state = dict(state, identity=identity, environment=environment)
        source_guards = _source_guards(checkpoint, state, checkpoint_number)
        selected_review_specs = _review_specs(root, checkpoint, state, capsule.payload_digest)
        review_specs.extend(selected_review_specs)
        structural = state["structural"]
        guards.append({
            "checkpoint": checkpoint_number,
            "schema": checkpoint.schema,
            "targetKey": f"schema:{checkpoint.schema}",
            "targetIdentity": identity,
            "structural": structural,
            "structuralSha256": _sha256(_canonical_bytes(structural)),
            "structuralPlan": feature_preflight.plan_to_dict(structural_plan),
            "sourceGuards": source_guards,
            "preconditions": [{"id": item.id, "sql": item.sql, "expected": item.expected} for item in checkpoint.preconditions],
            "postconditions": [{"id": item.id, "sql": item.sql, "expected": item.expected} for item in checkpoint.postconditions],
            "reviews": [{key: spec[key] for key in (
                "schemaVersion", "feature", "schema", "owner", "unitType", "unitName", "observedIdentity",
                "beforeSha256", "afterSha256", "payloadDigest", "baselineKind", "diffSha256",
            )} for spec in selected_review_specs],
        })
        target_record = targets.setdefault(f"schema:{checkpoint.schema}", {
            "environment": environment,
            "connection": target.connection,
            "expectedUser": target.expected_user,
            "schema": target.schema,
            "classification": target.classification,
            "observed": identity,
        })
        if target_record["observed"] != identity:
            raise FeatureReleaseError(f"multiple checkpoints for {checkpoint.schema} observed different target identities")
        earlier_effects_by_schema.setdefault(checkpoint.schema, []).append({
            "checkpoint": checkpoint_number,
            "schema": checkpoint.schema,
            "effects": structural_plan,
            "effectsSha256": structural_plan.effects_sha256,
        })

    recovery_binding = None
    if recovery_record is not None:
        old_checkpoint = recovery_record["checkpoint"]
        observed_guard = guards[old_checkpoint - 1]
        try:
            observed_scope = release_state.target_scope_from_identity(observed_guard["targetIdentity"])
        except (KeyError, TypeError, release_state.ReleaseStateError) as error:
            raise FeatureReleaseError(f"recovery target observation is incomplete: {error}") from error
        if observed_scope != recovery_record["targetScope"]:
            raise FeatureReleaseError("recovery plan observed a different physical target scope")
        observed_state_sha256 = _sha256(_canonical_bytes({
            "structuralSha256": observed_guard["structuralSha256"],
            "sourceGuards": observed_guard["sourceGuards"],
        }))
        recovery_binding = {
            "attemptId": recovery_attempt,
            "attemptState": recovery_record["state"],
            "checkpoint": old_checkpoint,
            "targetScope": recovery_record["targetScope"],
            "terminalAcknowledgment": terminal_ack,
            "acknowledgedAt": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "oldEvidence": recovery_record["evidence"],
            "observedStateSha256": observed_state_sha256,
        }
    payload = _plan_data(environment, capsule, targets, guards, recovery_attempt, recovery_binding)
    if recovery_attempt is not None:
        recovery_plan = TargetPlan(
            environment,
            capsule.payload_digest,
            payload["planDigest"],
            dict(targets),
            tuple(guards),
            (),
            recovery_attempt,
            recovery_binding,
        )
        try:
            release_state.validate_recovery_plan(root, capsule, recovery_plan)
        except release_state.ReleaseStateError as error:
            raise FeatureReleaseError(f"recovery preparation refused: {error}") from error
    payload["reviewPaths"] = _write_reviews(root, review_specs, payload["planDigest"], day)
    _write_plan(root, environment, capsule.payload_digest, payload)
    return TargetPlan(
        environment,
        capsule.payload_digest,
        payload["planDigest"],
        dict(targets),
        tuple(guards),
        tuple(payload["reviewPaths"]),
        recovery_attempt,
        recovery_binding,
    )


def load_target_plan(repo_root: Path, capsule: release_capsule.ReleaseCapsule, environment: str) -> TargetPlan:
    """Load only a canonical target plan bound to this capsule and environment."""
    root = _root(repo_root)
    if environment not in PLAN_ENVIRONMENTS:
        raise FeatureReleaseError("target plan environment must be staging or prod")
    path = root / "scratch" / "feature-release-plans" / environment / f"{capsule.payload_digest}.json"
    _safe_repo_path(root, path, "target plan")
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        if raw.startswith(b"\xef\xbb\xbf") or "\r" in text:
            raise FeatureReleaseError("target plan must be UTF-8/LF without a BOM")
        document = json.loads(text)
    except FeatureReleaseError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FeatureReleaseError(f"target plan is missing or invalid: {error}") from error
    if not isinstance(document, dict) or set(document) != {
        "schemaVersion", "environment", "payloadDigest", "engineDigest", "sourceCommit", "recoveryAttempt",
        "recoveryBinding", "targets", "checkpointGuards", "reviewPaths", "planDigest",
    }:
        raise FeatureReleaseError("target plan has unsupported or missing fields")
    digest = document.pop("planDigest")
    if type(document.get("schemaVersion")) is not int or document["schemaVersion"] != PLAN_SCHEMA_VERSION:
        raise FeatureReleaseError("target plan schemaVersion is unsupported")
    if document.get("environment") != environment or document.get("payloadDigest") != capsule.payload_digest or document.get("engineDigest") != capsule.engine_digest or document.get("sourceCommit") != capsule.source_commit:
        raise FeatureReleaseError("target plan does not match the selected capsule and environment")
    digest_document = {key: value for key, value in document.items() if key != "reviewPaths"}
    if digest != _sha256(_canonical_bytes(digest_document)):
        raise FeatureReleaseError("target plan digest does not match its canonical contents")
    if not isinstance(document.get("targets"), dict) or not isinstance(document.get("checkpointGuards"), list) or not isinstance(document.get("reviewPaths"), list):
        raise FeatureReleaseError("target plan records are malformed")
    _validate_review_artifacts(root, document, digest)
    loaded = TargetPlan(
        environment, capsule.payload_digest, digest,
        document["targets"], tuple(document["checkpointGuards"]), tuple(document["reviewPaths"]),
        document["recoveryAttempt"], document["recoveryBinding"],
    )
    steps = _manifest_steps(capsule)
    expected_step_targets = {
        f"step:{number}": step
        for number, step in enumerate(steps, start=1)
        if step.get("type") in {"sql-script", "ords-import"}
    }
    supplied_step_targets = {key for key in loaded.targets if isinstance(key, str) and key.startswith("step:")}
    if supplied_step_targets != set(expected_step_targets):
        raise FeatureReleaseError("target plan does not bind every SQL/ORDS step exactly once")
    target_fields = {
        "environment", "operation", "logicalSchema", "connection", "expectedUser",
        "schema", "classification", "observed",
    }
    for key, step in expected_step_targets.items():
        record = loaded.targets.get(key)
        if not isinstance(record, Mapping) or set(record) != target_fields:
            raise FeatureReleaseError(f"target plan auxiliary binding {key} is malformed")
        if (
            record.get("environment") != environment
            or record.get("operation") != "migration"
            or record.get("logicalSchema") != step.get("schema")
            or not all(isinstance(record.get(field), str) and record[field] for field in (
                "connection", "expectedUser", "schema", "classification",
            ))
        ):
            raise FeatureReleaseError(f"target plan auxiliary binding {key} differs from its frozen step")
        if step.get("type") == "ords-import" and record["expectedUser"] != record["schema"]:
            raise FeatureReleaseError(f"target plan ORDS binding {key} is not logged in as its REST owner")
        target = Target(
            environment, record["connection"], record["expectedUser"],
            record["schema"], record["classification"],
        )
        if _identity(target, record.get("observed")) != record["observed"]:
            raise FeatureReleaseError(f"target plan auxiliary identity {key} is incomplete or inconsistent")
    if len(loaded.checkpoint_guards) != len(capsule.checkpoints):
        raise FeatureReleaseError("target plan does not contain one guard per capsule checkpoint")
    for checkpoint_number, checkpoint in enumerate(capsule.checkpoints, start=1):
        _checkpoint_guard(loaded, checkpoint_number, checkpoint)
    return loaded


def _checkpoint_guard(plan: TargetPlan, checkpoint_number: int, checkpoint: FeatureCheckpoint) -> dict:
    matches = [
        row for row in plan.checkpoint_guards
        if isinstance(row, Mapping) and row.get("checkpoint") == checkpoint_number
    ]
    if len(matches) != 1:
        raise FeatureReleaseError(f"prepared target plan has no unique guard for checkpoint {checkpoint_number}")
    guard = dict(matches[0])
    expected_fields = {
        "checkpoint", "schema", "targetKey", "targetIdentity", "structural", "structuralSha256",
        "structuralPlan", "sourceGuards", "preconditions", "postconditions", "reviews",
    }
    if set(guard) != expected_fields:
        raise FeatureReleaseError("prepared target plan checkpoint guard has unsupported or missing fields")
    if guard.get("schema") != checkpoint.schema or guard.get("targetKey") != f"schema:{checkpoint.schema}":
        raise FeatureReleaseError("prepared target plan checkpoint schema or target key is stale")
    if (
        not isinstance(guard.get("targetIdentity"), Mapping)
        or not isinstance(guard.get("sourceGuards"), list)
        or not isinstance(guard.get("structural"), Mapping)
        or not all(isinstance(guard.get(key), list) for key in ("preconditions", "postconditions", "reviews"))
    ):
        raise FeatureReleaseError("prepared target plan checkpoint guards are malformed")
    if _sha256(_canonical_bytes(guard["structural"])) != guard.get("structuralSha256"):
        raise FeatureReleaseError("prepared target plan structural guard digest is invalid")
    structural_plan = guard.get("structuralPlan")
    try:
        feature_preflight.validate_plan_record(structural_plan, checkpoint, guard.get("structural"))
    except feature_preflight.StructuralPreflightError as error:
        raise FeatureReleaseError(f"prepared structural plan is invalid: {error}") from error
    if structural_plan.get("expectedBeforeSha256") != guard.get("structuralSha256"):
        raise FeatureReleaseError("prepared structural plan does not bind the selected before-state")
    source_guard_fields = {
        "unit", "targetExists", "beforeSourceSha256", "expectedSourceSha256", "beforeSettings",
        "applySettings", "settingsPrologue", "settingsPrologueSha256", "valid", "noOp",
        "preconditions", "sourceVerification",
    }
    for source_guard in guard["sourceGuards"]:
        if not isinstance(source_guard, Mapping) or set(source_guard) != source_guard_fields:
            raise FeatureReleaseError("prepared source compiler guard has unsupported or missing fields")
        unit = source_guard.get("unit")
        if not isinstance(unit, Mapping) or set(unit) != {"owner", "type", "name"}:
            raise FeatureReleaseError("prepared source compiler guard unit is malformed")
        if unit.get("owner") != checkpoint.schema or not isinstance(unit.get("type"), str) or not isinstance(unit.get("name"), str):
            raise FeatureReleaseError("prepared source compiler guard unit differs from its checkpoint")
        if type(source_guard.get("targetExists")) is not bool or type(source_guard.get("noOp")) is not bool:
            raise FeatureReleaseError("prepared source compiler guard flags are malformed")
        if not isinstance(source_guard.get("preconditions"), list) or not isinstance(source_guard.get("sourceVerification"), list):
            raise FeatureReleaseError("prepared source compiler guard checks are malformed")
        if source_guard.get("valid") is not None and type(source_guard.get("valid")) is not bool:
            raise FeatureReleaseError("prepared source compiler guard validity is malformed")
        # Only a unit that does not exist yet has no before-state digest.
        if (source_guard.get("beforeSourceSha256") is None) != (source_guard.get("targetExists") is False):
            raise FeatureReleaseError("prepared source compiler guard before-state does not match whether the unit exists")
        for digest_field in ("beforeSourceSha256", "expectedSourceSha256", "settingsPrologueSha256"):
            if digest_field == "beforeSourceSha256" and source_guard.get(digest_field) is None:
                continue
            if not isinstance(source_guard.get(digest_field), str) or HASH_RE.fullmatch(source_guard[digest_field]) is None:
                raise FeatureReleaseError(f"prepared source compiler guard {digest_field} is malformed")
        _validated_source_prologue(source_guard)
    return guard


def _runtime_check_identity(report: migration_checks.CheckReport, expected: Mapping[str, str], phase: str) -> int:
    coverage = report.coverage if isinstance(report.coverage, Mapping) else {}
    sessions = coverage.get("sessions", 0)
    if type(sessions) is not int or sessions < 0:
        raise FeatureReleaseError(f"{phase} check session coverage is malformed")
    identities = coverage.get("sessionIdentities", [])
    if sessions and (not isinstance(identities, list) or len(identities) != sessions):
        raise FeatureReleaseError(f"{phase} checks did not return the identity of every SQLcl session")
    for identity in identities if isinstance(identities, list) else []:
        complete = complete_observed_identity(identity)
        if complete is None or complete != dict(expected):
            raise FeatureReleaseError(f"{phase} check session identity differs from the prepared target")
    if not report.complete or not report.passed:
        errors = "; ".join(
            str(row.get("message", row.get("code", "check failed")))
            for row in report.errors if isinstance(row, Mapping)
        )
        raise FeatureReleaseError(f"{phase} checks failed or have incomplete coverage" + (f": {errors}" if errors else ""))
    return sessions


def _runtime_checks(checks: Sequence[QueryCheck], label: str) -> tuple[QueryCheck, ...]:
    ids: set[str] = set()
    result = []
    for check in checks:
        if not isinstance(check, QueryCheck) or check.id in ids:
            raise FeatureReleaseError(f"{label} checks contain an invalid or duplicate ID")
        ids.add(check.id)
        result.append(check)
    return tuple(result)


def _validity_check_id(object_type: str, name: str) -> str:
    # Check ids are lowercase kebab-case, so Oracle underscores and $ are slugged and hashed.
    return f"valid-{baseline._safe_check_slug(name + '-' + object_type)}"


def _checkpoint_runtime_checks(
    checkpoint: FeatureCheckpoint,
    guard: Mapping,
) -> tuple[tuple[QueryCheck, ...], tuple[QueryCheck, ...]]:
    preconditions = list(checkpoint.preconditions)
    structural_plan = guard.get("structuralPlan")
    if not isinstance(structural_plan, Mapping):
        raise FeatureReleaseError("prepared structural checks are missing")
    try:
        preconditions.extend(feature_preflight.checks_from_record(structural_plan, "preconditions"))
    except feature_preflight.StructuralPreflightError as error:
        raise FeatureReleaseError(f"prepared structural preconditions are invalid: {error}") from error
    for source_guard in guard.get("sourceGuards", []):
        for query in source_guard.get("preconditions", []):
            preconditions.append(QueryCheck(query["id"], query["sql"], query["expected"]))

    postconditions = list(checkpoint.postconditions)
    try:
        postconditions.extend(feature_preflight.checks_from_record(structural_plan, "postconditions"))
    except feature_preflight.StructuralPreflightError as error:
        raise FeatureReleaseError(f"prepared structural postconditions are invalid: {error}") from error
    for item in checkpoint.inputs:
        if item.category != "stored-source" or item.unit is None:
            continue
        _owner, object_type, name = item.unit
        source_guard = next((
            row for row in guard["sourceGuards"]
            if isinstance(row, Mapping)
            and isinstance(row.get("unit"), Mapping)
            and row["unit"].get("type") == object_type
            and row["unit"].get("name") == name
        ), None)
        if source_guard is None or not isinstance(source_guard.get("applySettings"), Mapping):
            raise FeatureReleaseError(f"prepared compiler settings are missing for {object_type} {name}")
        if source_guard.get("noOp") is True:
            # A unit that is already current keeps the text it has. Its header may be rendered
            # differently from the frozen one, so check the live text the plan captured.
            source_checks = source_guard.get("sourceVerification")
            if not isinstance(source_checks, list) or not source_checks:
                raise FeatureReleaseError(f"prepared no-op source check is missing for {object_type} {name}")
        else:
            expected_unit, _overrides = _stored_source(item)
            source_checks = baseline._source_check_queries(
                {"name": name, "type": object_type, "lines": expected_unit["lines"]}, checkpoint.schema,
            )
        for query in source_checks:
            postconditions.append(QueryCheck(query["id"], query["sql"], query["expected"]))
        setting_query = baseline._exact_settings_check(
            source_guard["applySettings"], name, object_type, checkpoint.schema,
        )
        postconditions.append(QueryCheck(setting_query["id"], setting_query["sql"], setting_query["expected"]))
        postconditions.append(QueryCheck(
            _validity_check_id(object_type, name),
            "SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_OBJECTS "
            f"WHERE OWNER = {_sql_literal(checkpoint.schema)} AND OBJECT_TYPE = {_sql_literal(object_type)} "
            f"AND OBJECT_NAME = {_sql_literal(name)} AND STATUS = 'VALID') "
            "AND NOT EXISTS (SELECT 1 FROM ALL_ERRORS "
            f"WHERE OWNER = {_sql_literal(checkpoint.schema)} AND TYPE = {_sql_literal(object_type)} "
            f"AND NAME = {_sql_literal(name)} AND ATTRIBUTE = 'ERROR') THEN 1 ELSE 0 END FROM DUAL",
            1,
        ))
    return _runtime_checks(preconditions, "precondition"), _runtime_checks(postconditions, "postcondition")


def _checkpoint_source_guard_map(guard: Mapping) -> dict[tuple[str, str], Mapping]:
    result = {}
    for row in guard["sourceGuards"]:
        unit = row.get("unit") if isinstance(row, Mapping) else None
        if not isinstance(unit, Mapping):
            raise FeatureReleaseError("prepared source guard unit is malformed")
        key = (unit.get("type"), unit.get("name"))
        if not all(isinstance(part, str) for part in key) or key in result:
            raise FeatureReleaseError("prepared source guard units are duplicated or malformed")
        result[key] = row
    return result


def _compile_units_for_apply(checkpoint: FeatureCheckpoint, source_guards: Mapping[tuple[str, str], Mapping]) -> tuple[tuple[str, str, str, bool], ...]:
    applied = {
        item.unit[1:] for item in checkpoint.inputs
        if item.category == "stored-source" and item.unit is not None
        and source_guards.get(item.unit[1:]) is not None
        and source_guards[item.unit[1:]].get("noOp") is not True
    }
    if not applied:
        return ()
    compiled = migration_checks.compiled_units(feature_manifest.analysis_migration(checkpoint), checkpoint.schema)
    selected = []
    for owner, object_type, name, required in compiled:
        key = (object_type, name)
        dependent_body = (
            object_type in {"PACKAGE BODY", "TYPE BODY"}
            and (object_type.removesuffix(" BODY"), name) in applied
        )
        if key in applied or dependent_body:
            selected.append((owner, object_type, name, required))
    return tuple(selected)


def _checkpoint_run_directory(root: Path, environment: str, payload_digest: str, number: int) -> Path:
    directory = root / "scratch" / "feature-release-runs" / environment / payload_digest / f"checkpoint-{number:03d}"
    _secure_directory(directory, root)
    run_dir = directory / str(uuid.uuid4())
    run_dir.mkdir(mode=0o700, exist_ok=False)
    return run_dir


def _runtime_evidence(root: Path, report: Mapping, run_dir: Path) -> list[str]:
    paths = [run_dir / "checkpoint-report.json"]
    for phase in ("preconditions", "postconditions"):
        coverage = report.get(phase, {}).get("coverage", {}) if isinstance(report.get(phase), Mapping) else {}
        evidence = coverage.get("evidence") if isinstance(coverage, Mapping) else None
        values = evidence if isinstance(evidence, list) else [evidence]
        for value in values:
            if isinstance(value, str) and value:
                paths.append(Path(value))
    if (run_dir / "sqlcl-output.log").is_file():
        paths.append(run_dir / "sqlcl-output.log")
    normalized = []
    for path in paths:
        if path.exists():
            try:
                normalized.append(relative_to_root(path.resolve(strict=True), root).as_posix())
            except (OSError, ValueError):
                continue
    return list(dict.fromkeys(normalized))


def _write_checkpoint_report(run_dir: Path, report: Mapping) -> None:
    destination = run_dir / "checkpoint-report.json"
    temporary = run_dir / ".checkpoint-report.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
    if os.name != "nt":
        temporary.chmod(0o600)
    os.replace(temporary, destination)
    if os.name != "nt":
        destination.chmod(0o600)


def _copy_capsule_driver(
    root: Path,
    capsule: release_capsule.ReleaseCapsule,
    run_dir: Path,
    checkpoint_number: int,
    input_number: int,
) -> tuple[str, str]:
    relative = f"drivers/checkpoint-{checkpoint_number:03d}-input-{input_number:03d}.sql"
    expected = {row["path"]: row["sha256"] for row in capsule.input_hashes if isinstance(row, Mapping)}.get(relative)
    if not isinstance(expected, str) or HASH_RE.fullmatch(expected) is None:
        raise FeatureReleaseError(f"capsule driver is missing its frozen hash: {relative}")
    source_path = _safe_repo_path(root, capsule.root / relative, "capsule checkpoint driver")
    try:
        raw = source_path.read_bytes()
        if _sha256(raw) != expected:
            raise FeatureReleaseError(f"frozen checkpoint driver changed: {relative}")
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise FeatureReleaseError(f"frozen checkpoint driver is unavailable: {relative}") from error
    from .sqlcl_transport import generated_clob_assembly, lint_sqlcl_transport

    mode = "assembled" if generated_clob_assembly(text) else "direct"
    findings = lint_sqlcl_transport(text, mode)
    for finding in findings:
        message = f"{relative}:{finding.line} ({finding.code}): {finding.message}"
        if finding.severity == "error":
            raise FeatureReleaseError(f"SQLcl transport preflight refused: {message}")
        print(f"feature release warning: SQLcl transport review: {message}", file=rollout.sys.stderr)

    staged_relative = f"payload/checkpoint-{checkpoint_number:03d}-input-{input_number:03d}.sql"
    destination = run_dir / staged_relative
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_bytes(raw)
    if os.name != "nt":
        destination.chmod(0o600)
    return staged_relative, relative


def _apply_checkpoint_session(
    root: Path,
    capsule: release_capsule.ReleaseCapsule,
    checkpoint: FeatureCheckpoint,
    guard: Mapping,
    target: Target,
    expected_identity: Mapping[str, str],
    preconditions: Sequence[QueryCheck],
    run_dir: Path,
    checkpoint_number: int,
    source_guards: Mapping[tuple[str, str], Mapping],
) -> tuple[dict[str, str], int]:
    applied_drivers: list[tuple[str, str | None, str | None]] = []
    for input_number, item in enumerate(checkpoint.inputs, start=1):
        if item.category == "stored-source" and item.unit is not None:
            source_guard = source_guards.get(item.unit[1:])
            if source_guard is None:
                raise FeatureReleaseError(f"prepared source guard is missing for {item.unit[1]} {item.unit[2]}")
            if source_guard.get("noOp") is True:
                continue
            settings_prologue = _validated_source_prologue(source_guard)
            settings_prologue_sha256 = source_guard["settingsPrologueSha256"]
        else:
            settings_prologue = None
            settings_prologue_sha256 = None
        staged_relative, _capsule_relative = _copy_capsule_driver(
            root, capsule, run_dir, checkpoint_number, input_number,
        )
        applied_drivers.append((staged_relative, settings_prologue, settings_prologue_sha256))

    if not applied_drivers:
        return {}, 0

    script_root = Path(__file__).resolve().parent
    for trusted in ("migrate.sql", "verify_migration_access.sql"):
        source = script_root / trusted
        destination = run_dir / trusted
        destination.write_bytes(source.read_bytes())
        if os.name != "nt":
            destination.chmod(0o600)

    lines = [
        "SET ECHO OFF",
        "SET SQLBLANKLINES ON",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
        "SET DEFINE ON",
        f"@@migrate.sql {target.schema} {target.environment} {target.expected_user}",
        "SET DEFINE OFF",
        *migrate._identity_guard_lines(expected_identity),
    ]
    if preconditions:
        lines.extend(migration_checks.render_in_session_check_driver(
            target, preconditions, "preconditions", fail_on_false=True,
        ).splitlines())
    for staged_relative, settings_prologue, _settings_prologue_sha256 in applied_drivers:
        if settings_prologue is not None:
            lines.extend(settings_prologue.rstrip("\n").splitlines())
        lines.append(f"PROMPT MIGRATION_PAYLOAD_STARTED:{Path(staged_relative).name}")
        lines.append(f"@@{staged_relative}")
    lines.extend((
        "SET DEFINE OFF",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
        *migrate._compile_guard_lines(_compile_units_for_apply(checkpoint, source_guards)),
        "PROMPT MIGRATION_APPLY_COMPLETED",
        "PROMPT FEATURE_CHECKPOINT_APPLY_COMPLETED",
        "EXIT SUCCESS COMMIT",
        "",
    ))
    driver = run_dir / "checkpoint-apply.sql"
    driver.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    if os.name != "nt":
        driver.chmod(0o600)
    try:
        generated_driver = driver.read_bytes()
    except OSError as error:
        raise FeatureReleaseError(f"generated checkpoint driver cannot be verified: {error}") from error
    cursor = 0
    for staged_relative, settings_prologue, settings_prologue_sha256 in applied_drivers:
        marker = f"PROMPT MIGRATION_PAYLOAD_STARTED:{Path(staged_relative).name}\n@@{staged_relative}\n".encode()
        if settings_prologue is None:
            expected_segment = marker
        else:
            prologue_bytes = settings_prologue.encode("utf-8")
            if _sha256(prologue_bytes) != settings_prologue_sha256:
                raise FeatureReleaseError("prepared compiler settings prologue digest changed before SQLcl apply")
            expected_segment = prologue_bytes + marker
        found = generated_driver.find(expected_segment, cursor)
        if found < 0:
            raise FeatureReleaseError("generated checkpoint driver does not contain its prepared compiler settings prologue")
        cursor = found + len(expected_segment)
    capsule = _validated_capsule(root, capsule)
    try:
        result = sqlcl_session.run_sqlcl(target, driver, run_dir, phase="apply")
    except (sqlcl_session.SqlclError, OSError, RuntimeError, ValueError) as error:
        output = getattr(error, "output", "")
        if output and not (run_dir / "sqlcl-output.log").exists():
            (run_dir / "sqlcl-output.log").write_text(output, encoding="utf-8")
        raise FeatureReleaseError(f"checkpoint SQLcl apply failed: {error}") from error
    output = result.output
    if result.returncode != 0:
        raise FeatureReleaseError(f"checkpoint SQLcl apply returned status {result.returncode}")
    if re.search(r"(?i)\b(?:created|altered|compiled)\s+with\s+(?:compilation\s+)?errors\b", output):
        raise FeatureReleaseError("SQLcl reported a stored unit created, altered, or compiled with errors")
    output_lines = [line.strip() for line in output.splitlines()]
    if output_lines.count("MIGRATION_APPLY_COMPLETED") != 1 or output_lines.count("FEATURE_CHECKPOINT_APPLY_COMPLETED") != 1:
        raise FeatureReleaseError("SQLcl did not confirm exactly one completed feature checkpoint")
    identity = migrate._apply_identity_from_output(output)
    try:
        migrate._assert_same_target(expected_identity, identity)
    except migrate.MigrationApplyError as error:
        raise FeatureReleaseError(str(error)) from error
    _validated_capsule(root, capsule)
    return identity, 1


def _execute_checkpoint_under_lease(
    repo_root: Path,
    capsule: release_capsule.ReleaseCapsule,
    plan: TargetPlan,
    checkpoint: int,
    *,
    run_id: str,
    lease: release_ownership.TargetWriterLease,
    environ: Mapping[str, str],
) -> CheckpointResult:
    """Revalidate and execute one frozen feature checkpoint in one apply session."""
    started = time.perf_counter()
    root = _root(repo_root)
    if type(checkpoint) is not int or checkpoint < 1:
        raise FeatureReleaseError("checkpoint number must be a positive one-based integer")
    capsule = _validated_capsule(root, capsule)
    if plan.payload_digest != capsule.payload_digest or plan.environment not in PLAN_ENVIRONMENTS:
        raise FeatureReleaseError("prepared target plan does not match the selected capsule")
    try:
        loaded_plan = load_target_plan(root, capsule, plan.environment)
    except FeatureReleaseError:
        raise
    if loaded_plan.plan_digest != plan.plan_digest:
        raise FeatureReleaseError("prepared target plan changed after it was loaded; prepare the target again")
    if (
        loaded_plan.targets != plan.targets
        or loaded_plan.checkpoint_guards != plan.checkpoint_guards
        or loaded_plan.recovery_binding != plan.recovery_binding
    ):
        raise FeatureReleaseError("prepared target plan contents changed after it was loaded")
    if plan.recovery_attempt is not None:
        try:
            release_state.validate_recovery_plan(root, capsule, plan)
        except release_state.ReleaseStateError as error:
            raise FeatureReleaseError(f"recovery plan is no longer valid: {error}") from error
    if checkpoint > len(capsule.checkpoints):
        raise FeatureReleaseError("checkpoint number is outside the selected capsule")
    selected = capsule.checkpoints[checkpoint - 1]
    guard = _checkpoint_guard(plan, checkpoint, selected)
    try:
        target = resolve_target(environ, plan.environment, "migration", schema=selected.schema)
    except TargetResolutionError as error:
        raise FeatureReleaseError(f"cannot resolve the prepared {plan.environment} write target: {error}") from error
    target_record = plan.targets.get(f"schema:{selected.schema}")
    if not isinstance(target_record, Mapping) or any(
        target_record.get(field) != expected for field, expected in (
            ("environment", plan.environment), ("connection", target.connection),
            ("expectedUser", target.expected_user), ("schema", target.schema),
            ("classification", target.classification),
        )
    ):
        raise FeatureReleaseError("resolved write target differs from the prepared target descriptor")
    expected_identity = _identity(target, guard.get("targetIdentity"))
    if target_record.get("observed") != expected_identity:
        raise FeatureReleaseError("prepared target identity differs between descriptor and checkpoint guard")

    run_dir = _checkpoint_run_directory(root, plan.environment, capsule.payload_digest, checkpoint)
    capture_started = time.perf_counter()
    fresh = capture_selected_state(target, selected, run_dir / "revalidation")
    observed_identity = _identity(target, fresh.get("identity"))
    if observed_identity != expected_identity:
        raise FeatureReleaseError("live target identity changed after preparation")
    structural = fresh.get("structural")
    structural_plan = guard.get("structuralPlan")
    if isinstance(structural, Mapping) and isinstance(structural_plan, Mapping):
        try:
            structural = feature_preflight.strip_prior_effects(structural, structural_plan.get("priorEffects", []))
        except (TypeError, ValueError) as error:
            raise FeatureReleaseError(f"selected structural revalidation is malformed: {error}") from error
    if not isinstance(structural, Mapping) or _sha256(_canonical_bytes(structural)) != guard.get("structuralSha256"):
        raise FeatureReleaseError("live selected target metadata changed after preparation")
    fresh_state = dict(fresh, identity=observed_identity, environment=plan.environment)
    fresh_source_guards = _source_guards(selected, fresh_state, checkpoint)
    if _canonical_bytes(fresh_source_guards) != _canonical_bytes(guard["sourceGuards"]):
        raise FeatureReleaseError("live selected stored source or compiler metadata changed after preparation")
    if plan.recovery_attempt is not None and checkpoint == plan.recovery_binding.get("checkpoint", checkpoint):
        recovery_fingerprint = _sha256(_canonical_bytes({
            "structuralSha256": guard["structuralSha256"],
            "sourceGuards": fresh_source_guards,
        }))
        try:
            release_state.validate_recovery_plan(
                root, capsule, plan, observed_state_sha256=recovery_fingerprint,
            )
        except release_state.ReleaseStateError as error:
            raise FeatureReleaseError(f"recovery partial state changed before execution: {error}") from error
    preparation_seconds = round(time.perf_counter() - capture_started, 3)
    source_guards = _checkpoint_source_guard_map(guard)
    preconditions, postconditions = _checkpoint_runtime_checks(selected, guard)
    pre_started = time.perf_counter()
    pre_report = migration_checks.run_checks(
        target, preconditions, run_dir / "preconditions", phase="preconditions", jobs=1,
    )
    pre_sessions = _runtime_check_identity(pre_report, expected_identity, "precondition")
    pre_seconds = round(time.perf_counter() - pre_started, 3)

    try:
        release_state.assert_checkpoint_allowed(
            root, capsule, plan, checkpoint, run_id=run_id, lease=lease,
        )
    except release_state.ReleaseStateError as error:
        raise FeatureReleaseError(f"checkpoint refused by durable occupancy state: {error}") from error

    write_required = any(
        item.category != "stored-source"
        or item.unit is None
        or source_guards.get(item.unit[1:]) is None
        or source_guards[item.unit[1:]].get("noOp") is not True
        for item in selected.inputs
    )
    attempt_id = None
    if write_required:
        _validated_capsule(root, capsule)
        try:
            attempt_id = release_state.record_attempt(
                root, capsule, plan, checkpoint, run_id=run_id, lease=lease,
            )
        except release_state.ReleaseStateError as error:
            raise FeatureReleaseError(f"checkpoint write refused by durable attempt state: {error}") from error

    report: dict = {
        "schemaVersion": 1,
        "checkpoint": checkpoint,
        "environment": plan.environment,
        "payloadDigest": capsule.payload_digest,
        "engineDigest": capsule.engine_digest,
        "targetPlanDigest": plan.plan_digest,
        "databaseState": "not-started",
        "preparationSeconds": preparation_seconds,
        "preconditionSeconds": pre_seconds,
        "applySeconds": 0.0,
        "postconditionSeconds": 0.0,
        "sessionCount": pre_sessions,
        "attemptId": attempt_id,
        "compilerSettingsPrologues": [
            {"unit": dict(source_guard["unit"]), "sha256": source_guard["settingsPrologueSha256"]}
            for source_guard in guard["sourceGuards"]
            if isinstance(source_guard, Mapping) and source_guard.get("noOp") is not True
        ],
        "error": None,
        "preconditions": pre_report.to_dict(),
        "postconditions": None,
    }
    apply_sessions = 0
    checks_verified = False
    try:
        if write_required:
            apply_started = time.perf_counter()
            _apply_checkpoint_session(
                root, capsule, selected, guard, target, expected_identity,
                preconditions, run_dir, checkpoint, source_guards,
            )
            report["applySeconds"] = round(time.perf_counter() - apply_started, 3)
            apply_sessions = 1
        post_started = time.perf_counter()
        post_report = migration_checks.run_checks(
            target, postconditions, run_dir / "postconditions", phase="postconditions", jobs=1,
        )
        post_sessions = _runtime_check_identity(post_report, expected_identity, "postcondition")
        report["postconditions"] = post_report.to_dict()
        report["postconditionSeconds"] = round(time.perf_counter() - post_started, 3)
        report["sessionCount"] += post_sessions + apply_sessions
        report["databaseState"] = "verified"
        _validated_capsule(root, capsule)
        report["objectSeconds"] = round(time.perf_counter() - started, 3)
        budget_covered = len(capsule.checkpoints) == 1
        report["objectBudget"] = {
            "seconds": capsule.database_object_budget_seconds,
            "coverageComplete": budget_covered,
            "includedCheckpoints": [checkpoint],
            "excludedObjectSteps": [],
        }
        _write_checkpoint_report(run_dir, report)
        evidence = _runtime_evidence(root, report, run_dir)
        checks_verified = True
        try:
            verification_id = release_state.record_checkpoint_verification(
                root, capsule, plan, checkpoint,
                run_id=run_id,
                attempt_id=attempt_id,
                lease=lease,
                evidence=evidence,
                metrics={
                    "objectSeconds": report["objectSeconds"],
                    "sessionCount": report["sessionCount"],
                },
                finish_attempt_metrics={
                    "preparationSeconds": preparation_seconds,
                    "objectSeconds": report["objectSeconds"],
                    "sqlclSessions": report["sessionCount"],
                    "budgetSeconds": capsule.database_object_budget_seconds,
                    "budgetCoverageComplete": budget_covered,
                    "includedCheckpoints": [checkpoint],
                    "excludedObjectSteps": [],
                } if attempt_id is not None else None,
            )
        except release_state.ReleaseStateError as error:
            raise FeatureReleaseError(f"checkpoint checks passed but durable verification could not be recorded: {error}") from error
        return CheckpointResult(
            0, "verified", report["objectSeconds"], report["sessionCount"],
            tuple(evidence), "checkpoint verified", verification_id,
        )
    except Exception as error:
        if checks_verified and attempt_id is None:
            # The database observation passed, but the local receipt could not be committed.
            # Preserve the report as diagnostic evidence and return failure to the orchestrator.
            report["error"] = str(error)
            report["objectSeconds"] = round(time.perf_counter() - started, 3)
            _write_checkpoint_report(run_dir, report)
            evidence = _runtime_evidence(root, report, run_dir)
            return CheckpointResult(
                2, "verified", report["objectSeconds"], report["sessionCount"],
                tuple(evidence), str(error),
            )
        report["databaseState"] = "unknown" if attempt_id is not None else "not-started"
        report["error"] = str(error)
        report["objectSeconds"] = round(time.perf_counter() - started, 3)
        report["sessionCount"] += apply_sessions
        report["objectBudget"] = {
            "seconds": capsule.database_object_budget_seconds,
            "coverageComplete": False,
            "includedCheckpoints": [checkpoint] if attempt_id is not None else [],
            "excludedObjectSteps": [],
        }
        log_path = run_dir / "sqlcl-output.log"
        if attempt_id is not None and not log_path.exists():
            log_path.write_text("", encoding="utf-8")
        _write_checkpoint_report(run_dir, report)
        evidence = _runtime_evidence(root, report, run_dir)
        if attempt_id is not None:
            attempt_state = "unknown"
            try:
                log_output = log_path.read_text(encoding="utf-8")
                if migration_attempts.not_started_reason(log_output) is not None:
                    attempt_state = "not-started"
                    evidence = [relative_to_root(log_path.resolve(strict=True), root).as_posix()]
            except (OSError, UnicodeError):
                pass
            release_state.finish_attempt(
                root, attempt_id, state=attempt_state, evidence=evidence,
                metrics={
                    "preparationSeconds": preparation_seconds,
                    "objectSeconds": report["objectSeconds"],
                    "sqlclSessions": report["sessionCount"],
                    "budgetSeconds": capsule.database_object_budget_seconds,
                    "budgetCoverageComplete": False,
                    "includedCheckpoints": [checkpoint],
                    "excludedObjectSteps": [],
                },
            )
        return CheckpointResult(2, report["databaseState"], report["objectSeconds"], report["sessionCount"], tuple(evidence), str(error))


def execute_checkpoint(
    repo_root: Path,
    capsule: release_capsule.ReleaseCapsule,
    plan: TargetPlan,
    checkpoint: int,
    *,
    run_id: str,
    environ: Mapping[str, str],
) -> CheckpointResult:
    """Hold a kernel-backed target lease from write occupancy through terminal state."""
    root = _root(repo_root)
    if type(checkpoint) is not int or checkpoint < 1 or checkpoint > len(capsule.checkpoints):
        raise FeatureReleaseError("checkpoint number must be inside the selected capsule")
    if not isinstance(getattr(plan, "checkpoint_guards", None), (tuple, list)):
        raise FeatureReleaseError("prepared target plan is missing checkpoint guards")
    guard = _checkpoint_guard(plan, checkpoint, capsule.checkpoints[checkpoint - 1])
    try:
        scope = release_state.target_scope_from_identity(guard["targetIdentity"])
    except (KeyError, TypeError, release_state.ReleaseStateError) as error:
        raise FeatureReleaseError(f"prepared checkpoint target scope is unavailable: {error}") from error
    execution_identity = {
        "runId": run_id,
        "environment": plan.environment,
        "payloadDigest": capsule.payload_digest,
        "engineDigest": capsule.engine_digest,
        "planDigest": plan.plan_digest,
        "checkpoint": checkpoint,
        "recoveryAttempt": plan.recovery_attempt,
    }
    try:
        with release_ownership.target_writer_lease(root, scope, execution_identity) as lease:
            return _execute_checkpoint_under_lease(
                root, capsule, plan, checkpoint, run_id=run_id, lease=lease, environ=environ,
            )
    except release_ownership.ReleaseOwnershipError as error:
        raise FeatureReleaseError(f"checkpoint target writer lease refused: {error}") from error
