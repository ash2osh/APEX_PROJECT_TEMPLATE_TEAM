#!/usr/bin/env python3
"""Validate ordered, mutable feature sources without touching a database."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .migration_checks import MigrationAnalysisError, analyze_batch, compiled_units
from .migration_manifest import (
    Migration,
    MigrationFile,
    MigrationManifestError,
    QueryCheck,
    _load_check_file,
)
from .path_utils import relative_to_root
from .validate_migration import statement_spans, validate_sql_only


SCHEMA_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
FEATURE_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z", re.ASCII)
STORED_SOURCE_TYPES = {
    "PACKAGE", "PACKAGE BODY", "TRIGGER", "FUNCTION", "PROCEDURE", "TYPE", "TYPE BODY",
}
SETTING_VALUE = r"(?:'(?:[^']|'')*')"
SESSION_SETTINGS = (
    ("PLSQL_OPTIMIZE_LEVEL", re.compile(r"ALTER\s+SESSION\s+SET\s+PLSQL_OPTIMIZE_LEVEL\s*=\s*([0-3])\Z", re.I | re.ASCII)),
    ("PLSQL_CODE_TYPE", re.compile(r"ALTER\s+SESSION\s+SET\s+PLSQL_CODE_TYPE\s*=\s*(INTERPRETED|NATIVE)\Z", re.I | re.ASCII)),
    ("PLSQL_DEBUG", re.compile(r"ALTER\s+SESSION\s+SET\s+PLSQL_DEBUG\s*=\s*(TRUE|FALSE)\Z", re.I | re.ASCII)),
    ("PLSQL_WARNINGS", re.compile(rf"ALTER\s+SESSION\s+SET\s+PLSQL_WARNINGS\s*=\s*({SETTING_VALUE})\Z", re.I | re.ASCII)),
    ("PLSQL_CCFLAGS", re.compile(rf"ALTER\s+SESSION\s+SET\s+PLSQL_CCFLAGS\s*=\s*({SETTING_VALUE})\Z", re.I | re.ASCII)),
    ("NLS_LENGTH_SEMANTICS", re.compile(r"ALTER\s+SESSION\s+SET\s+NLS_LENGTH_SEMANTICS\s*=\s*(BYTE|CHAR)\Z", re.I | re.ASCII)),
    ("PLSCOPE_SETTINGS", re.compile(rf"ALTER\s+SESSION\s+SET\s+PLSCOPE_SETTINGS\s*=\s*({SETTING_VALUE})\Z", re.I | re.ASCII)),
)
GRANT_IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z][A-Za-z0-9_$#]*)'
OBJECT_GRANT_RE = re.compile(
    rf"GRANT\s+(?:ALL(?:\s+PRIVILEGES)?|[A-Za-z][A-Za-z0-9_$#]*(?:\s*,\s*[A-Za-z][A-Za-z0-9_$#]*)*)"
    rf"\s+ON\s+({GRANT_IDENT})\s*\.\s*({GRANT_IDENT})\s+TO\s+"
    rf"{GRANT_IDENT}(?:\s*,\s*{GRANT_IDENT})*"
    rf"(?:\s+WITH\s+GRANT\s+OPTION)?\Z",
    re.I | re.ASCII,
)


class FeatureManifestError(ValueError):
    """A feature checkpoint is incomplete, ambiguous, or unsafe to apply."""


@dataclass(frozen=True)
class FeatureInput:
    path: str
    source: bytes
    sha256: str
    unit: tuple[str, str, str] | None
    category: str


@dataclass(frozen=True)
class FeatureCheckpoint:
    schema: str
    inputs: tuple[FeatureInput, ...]
    preconditions: tuple[QueryCheck, ...]
    postconditions: tuple[QueryCheck, ...]
    object_keys: tuple[tuple[str, str], ...]


def _safe_parts(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise FeatureManifestError(f"{label} must be a nonempty repository-relative path")
    if "\\" in value or re.match(r"^[A-Za-z]:", value, re.ASCII):
        raise FeatureManifestError(f"{label} must use forward slashes and cannot name a drive")
    raw_parts = value.split("/")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise FeatureManifestError(f"{label} must stay inside the repository and cannot contain . or .. components")
    return tuple(raw_parts)


def _is_reparse(path: Path, metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _resolve_file(root: Path, value: object, label: str) -> tuple[str, Path]:
    parts = _safe_parts(value, label)
    candidate = root.joinpath(*parts)
    current = root
    try:
        root_metadata = current.lstat()
        if _is_reparse(current, root_metadata):
            raise FeatureManifestError("repository root cannot be a symlink or reparse point")
        for part in parts:
            current = current / part
            metadata = current.lstat()
            if _is_reparse(current, metadata):
                raise FeatureManifestError(f"symlinks and reparse points are not allowed in feature paths: {value}")
        if not stat.S_ISREG(metadata.st_mode):
            raise FeatureManifestError(f"{label} must name an ordinary file: {value}")
        resolved = candidate.resolve(strict=True)
        relative_to_root(resolved, root)
    except FeatureManifestError:
        raise
    except (OSError, ValueError) as error:
        raise FeatureManifestError(f"{label} does not resolve to an ordinary file inside the repository: {value}") from error
    return PurePosixPath(*parts).as_posix(), resolved


def _feature_path(value: object, schema: str, label: str, *, extension: str) -> tuple[str, str, Path]:
    parts = _safe_parts(value, label)
    if (
        len(parts) < 4
        or parts[0] != "features"
        or FEATURE_RE.fullmatch(parts[1]) is None
        or parts[2] != schema
        or not parts[-1].lower().endswith(extension)
    ):
        raise FeatureManifestError(
            f"{label} must be a {extension} file under features/<feature>/{schema}/"
        )
    return parts[1], PurePosixPath(*parts).as_posix(), Path(*parts)


def _read_feature_source(path: Path, relative: str) -> bytes:
    try:
        source = path.read_bytes()
    except OSError as error:
        raise FeatureManifestError(f"cannot read feature source {relative}: {error}") from error
    if source.startswith(b"\xef\xbb\xbf"):
        raise FeatureManifestError(f"feature source must be UTF-8 without a BOM: {relative}")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FeatureManifestError(f"feature source must be UTF-8: {relative}") from error
    if "\r" in text:
        raise FeatureManifestError(f"feature source must use LF line endings: {relative}")
    try:
        validate_sql_only(text)
    except ValueError as error:
        raise FeatureManifestError(f"feature source is not SQL-only ({relative}): {error}") from error
    return source


def _analysis_migration(inputs: tuple[FeatureInput, ...], schema: str) -> Migration:
    files = tuple(
        MigrationFile(
            sequence=index,
            name=f"{index:03d}-{Path(item.path).name}",
            path=Path(item.path),
            source=item.source,
            sha256=item.sha256,
        )
        for index, item in enumerate(inputs, start=1)
    )
    check_document = json.dumps(
        {"schemaVersion": 1, "preconditions": [], "postconditions": [{"id": "analysis", "sql": "SELECT 1 FROM dual", "expected": 1}]},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(b"".join(item.path.encode("utf-8") + b"\0" + item.source for item in inputs)).hexdigest()
    return Migration(
        folder=Path("features-checkpoint"),
        date="2000-01-01",
        family="features-checkpoint",
        revision=1,
        files=files,
        preconditions=(),
        postconditions=(QueryCheck("analysis", "SELECT 1 FROM dual", 1),),
        checks_source=check_document,
        checks_sha256=hashlib.sha256(check_document).hexdigest(),
        payload_digest=digest,
        schema=schema,
    )


def _setting_name(statement: str) -> str | None:
    for name, pattern in SESSION_SETTINGS:
        match = pattern.fullmatch(statement.strip())
        if match is not None:
            value = match.group(1) if match.lastindex else ""
            if any(ord(char) < 32 for char in value):
                return None
            return name
    return None


def _source_unit(item: FeatureInput, schema: str) -> tuple[str, str, str] | None:
    text = item.source.decode("utf-8")
    spans = statement_spans(text)
    statements = [text[start:end].strip() for start, end in spans]
    if not statements or not statements[-1].upper().startswith("CREATE "):
        return None
    prefix_names: set[str] = set()
    index = 0
    while index < len(statements) - 1:
        name = _setting_name(statements[index])
        if name is None:
            return None
        if name in prefix_names:
            raise FeatureManifestError(f"feature source repeats compiler setting {name}: {item.path}")
        prefix_names.add(name)
        index += 1
    if index != len(statements) - 1:
        return None

    try:
        units = compiled_units(_analysis_migration((item,), schema), schema)
    except (MigrationAnalysisError, ValueError) as error:
        raise FeatureManifestError(f"cannot identify stored source unit in {item.path}: {error}") from error
    if len(units) != 1:
        return None
    owner, object_type, name, required = units[0]
    if not required or object_type not in STORED_SOURCE_TYPES:
        return None
    if owner != schema:
        raise FeatureManifestError(
            f"stored source owner {owner!r} does not match feature checkpoint schema {schema}: {item.path}"
        )
    return owner, object_type, name


def _unquote_identifier(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('""', '"')
    return value.upper()


def _static_grant_object(statement: str, schema: str) -> tuple[str, str] | None:
    match = OBJECT_GRANT_RE.fullmatch(statement.strip())
    if match is None:
        return None
    owner, name = (_unquote_identifier(match.group(index)) for index in (1, 2))
    if owner != schema:
        raise FeatureManifestError(f"object grant owner {owner!r} does not match feature checkpoint schema {schema}")
    return owner, name


def _validate_ddl(item: FeatureInput, schema: str) -> tuple[tuple[str, str], ...]:
    text = item.source.decode("utf-8")
    migration = _analysis_migration((item,), schema)
    try:
        operations = analyze_batch((migration,), schema)
        spans = statement_spans(text)
    except (MigrationAnalysisError, ValueError) as error:
        raise FeatureManifestError(f"feature DDL cannot be analyzed safely ({item.path}): {error}") from error
    if not operations or len(operations) != len(spans):
        raise FeatureManifestError(f"feature DDL could not be classified statement by statement: {item.path}")

    keys: list[tuple[str, str]] = []
    for operation, (start, end) in zip(operations, spans, strict=True):
        if operation.get("supported") is True:
            owner = operation.get("owner")
            name = operation.get("name", operation.get("table"))
            if owner != schema:
                raise FeatureManifestError(
                    f"DDL owner {owner!r} does not match feature checkpoint schema {schema}: {item.path}"
                )
            if isinstance(name, str):
                keys.append((owner, name))
            continue
        statement = text[start:end].strip()
        if operation.get("kind") == "REVIEWED_OPERATION" and statement.upper().startswith("GRANT "):
            grant_key = _static_grant_object(statement, schema)
            if grant_key is not None:
                keys.append(grant_key)
                continue
        kind = operation.get("kind", "UNKNOWN")
        reason = operation.get("reason", "operation is not statically supported")
        raise FeatureManifestError(f"feature DDL contains unsupported {kind} in {item.path}: {reason}")
    return tuple(keys)


def _load_namespaced_checks(root: Path, paths: list[tuple[str, str, Path]]) -> tuple[tuple[QueryCheck, ...], tuple[QueryCheck, ...]]:
    preconditions: list[QueryCheck] = []
    postconditions: list[QueryCheck] = []
    seen: dict[str, set[str]] = {"preconditions": set(), "postconditions": set()}
    for feature, relative, path in paths:
        try:
            pre, post, _rehearsal_metrics, _source = _load_check_file(path)
        except (MigrationManifestError, OSError) as error:
            raise FeatureManifestError(f"feature checks are invalid ({relative}): {error}") from error
        parts = PurePosixPath(relative).parts
        check_tail = parts[3:]
        name_parts = list(check_tail[:-1])
        if name_parts and name_parts[0] == "checks":
            name_parts = name_parts[1:]
        if check_tail[-1] == "checks.json":
            name_parts.append("checks")
        else:
            name_parts.append(Path(check_tail[-1]).stem)
        namespace = "-".join([feature, *[part for part in name_parts if part]])
        for field, values, output in (
            ("preconditions", pre, preconditions),
            ("postconditions", post, postconditions),
        ):
            for check in values:
                check_id = f"{namespace}-{check.id}"
                if check_id in seen[field]:
                    raise FeatureManifestError(f"feature checks contain duplicate namespaced {field} id {check_id!r}")
                seen[field].add(check_id)
                output.append(QueryCheck(check_id, check.sql, check.expected))
    return tuple(preconditions), tuple(postconditions)


def load_feature_checkpoint(repo_root: Path, step: Mapping) -> FeatureCheckpoint:
    """Load and statically validate one ordered feature checkpoint."""
    if not isinstance(step, Mapping):
        raise FeatureManifestError("feature-migrate step must be an object")
    if set(step) - {"type", "schema", "inputs", "checks"} or not {"type", "schema", "inputs"}.issubset(step):
        raise FeatureManifestError("feature-migrate step must contain only type, schema, inputs, and optional checks")
    if step.get("type") != "feature-migrate":
        raise FeatureManifestError("feature checkpoint type must be feature-migrate")
    schema = step.get("schema")
    if not isinstance(schema, str) or SCHEMA_RE.fullmatch(schema) is None:
        raise FeatureManifestError("feature-migrate schema must be an uppercase Oracle identifier")
    raw_inputs = step.get("inputs")
    if not isinstance(raw_inputs, list) or not raw_inputs or any(not isinstance(value, str) for value in raw_inputs):
        raise FeatureManifestError("feature-migrate inputs must be a nonempty ordered list of paths")

    requested_root = Path(repo_root)
    if requested_root.is_symlink():
        raise FeatureManifestError("repository root cannot be a symlink")
    try:
        root = requested_root.resolve(strict=True)
    except OSError as error:
        raise FeatureManifestError(f"feature repository root is unavailable: {error}") from error
    if len(set(raw_inputs)) != len(raw_inputs):
        raise FeatureManifestError("feature-migrate inputs cannot repeat a path")

    features: list[str] = []
    input_rows: list[tuple[str, Path, bytes]] = []
    for raw_path in raw_inputs:
        feature, relative, _parts = _feature_path(raw_path, schema, "feature input", extension=".sql")
        _safe_relative, path = _resolve_file(root, relative, "feature input")
        source = _read_feature_source(path, relative)
        if feature not in features:
            features.append(feature)
        input_rows.append((relative, path, source))

    default_paths: list[tuple[str, str, Path]] = []
    for feature in features:
        relative = f"features/{feature}/{schema}/checks.json"
        safe_relative, path = _resolve_file(root, relative, "feature schema checks")
        default_paths.append((feature, safe_relative, path))

    explicit = "checks" in step
    if explicit:
        raw_checks = step["checks"]
        if not isinstance(raw_checks, list) or not raw_checks or any(not isinstance(value, str) for value in raw_checks):
            raise FeatureManifestError("feature-migrate checks must be a nonempty ordered list of paths")
        if len(set(raw_checks)) != len(raw_checks):
            raise FeatureManifestError("feature-migrate checks cannot repeat a path")
        check_paths: list[tuple[str, str, Path]] = []
        for raw_path in raw_checks:
            feature, relative, _parts = _feature_path(raw_path, schema, "feature checks", extension=".json")
            if feature not in features:
                raise FeatureManifestError(f"explicit checks must belong to a selected feature: {relative}")
            tail = PurePosixPath(relative).parts[3:]
            if tail != ("checks.json",) and (not tail or tail[0] != "checks"):
                raise FeatureManifestError(f"explicit checks must be checks.json or live under checks/: {relative}")
            safe_relative, path = _resolve_file(root, relative, "feature checks")
            check_paths.append((feature, safe_relative, path))
    else:
        check_paths = default_paths

    inputs: list[FeatureInput] = []
    unit_identities: set[tuple[str, str, str]] = set()
    object_keys: list[tuple[str, str]] = []
    for relative, _path, source in input_rows:
        item = FeatureInput(relative, source, hashlib.sha256(source).hexdigest(), None, "ddl")
        unit = _source_unit(item, schema)
        if unit is not None:
            if unit in unit_identities:
                raise FeatureManifestError(f"duplicate stored unit identity across selected feature inputs: {unit}")
            unit_identities.add(unit)
            object_keys.append((unit[0], unit[2]))
            item = FeatureInput(relative, source, item.sha256, unit, "stored-source")
        else:
            object_keys.extend(_validate_ddl(item, schema))
        inputs.append(item)

    preconditions, postconditions = _load_namespaced_checks(root, check_paths)
    if not postconditions:
        raise FeatureManifestError("feature checkpoint must contain at least one postcondition")
    checkpoint = FeatureCheckpoint(
        schema=schema,
        inputs=tuple(inputs),
        preconditions=preconditions,
        postconditions=postconditions,
        object_keys=tuple(dict.fromkeys(object_keys)),
    )
    return checkpoint


def validate_release_units(checkpoints: Sequence[FeatureCheckpoint]) -> None:
    """Reject duplicate or forged stored-unit identities across one release."""
    if not isinstance(checkpoints, Sequence):
        raise FeatureManifestError("release checkpoints must be an ordered sequence")
    seen: dict[tuple[str, str, str], str] = {}
    for checkpoint_number, checkpoint in enumerate(checkpoints, start=1):
        if not isinstance(checkpoint, FeatureCheckpoint):
            raise FeatureManifestError("release contains an unvalidated feature checkpoint")
        for item in checkpoint.inputs:
            if not isinstance(item, FeatureInput):
                raise FeatureManifestError(f"checkpoint {checkpoint_number} contains an invalid feature input")
            try:
                derived_unit = _source_unit(item, checkpoint.schema)
            except (UnicodeError, ValueError) as error:
                raise FeatureManifestError(
                    f"checkpoint {checkpoint_number} cannot verify the stored unit identity for {item.path}: {error}"
                ) from error
            if item.category == "stored-source":
                if item.unit is None or derived_unit != item.unit:
                    raise FeatureManifestError(
                        f"stored unit identity does not match its source bytes in checkpoint {checkpoint_number}: {item.path}"
                    )
                previous_path = seen.get(item.unit)
                if previous_path is not None:
                    raise FeatureManifestError(
                        f"duplicate stored unit identity across selected release checkpoints: "
                        f"{item.unit} ({previous_path}, {item.path})"
                    )
                seen[item.unit] = item.path
            elif item.category != "ddl" or item.unit is not None or derived_unit is not None:
                raise FeatureManifestError(
                    f"feature input category or stored unit identity is inconsistent with its source: {item.path}"
                )


def analysis_migration(checkpoint: FeatureCheckpoint) -> Migration:
    """Adapt the validated checkpoint to existing analysis APIs, without writing files."""
    if not isinstance(checkpoint, FeatureCheckpoint):
        raise FeatureManifestError("analysis_migration requires a validated FeatureCheckpoint")
    migration = _analysis_migration(checkpoint.inputs, checkpoint.schema)
    return Migration(
        folder=migration.folder,
        date=migration.date,
        family=migration.family,
        revision=migration.revision,
        files=migration.files,
        preconditions=checkpoint.preconditions,
        postconditions=checkpoint.postconditions,
        checks_source=migration.checks_source,
        checks_sha256=migration.checks_sha256,
        payload_digest=migration.payload_digest,
        schema=checkpoint.schema,
    )
