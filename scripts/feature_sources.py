#!/usr/bin/env python3
"""Capture whole stored units (packages, types, triggers, procedures, functions) and write digest-bound feature source reviews."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from . import baseline, feature_claims, feature_compare, new_migration
from .path_utils import relative_to_root


FEATURE_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z", re.ASCII)
SCHEMA_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
PACKAGE_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_$#]{0,127}\Z", re.ASCII)
QUOTED_PACKAGE_NAME_RE = re.compile(r'"(?:[^"]|"")+"\Z')
DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
CAPTURE_SCHEMA_VERSION = 1
IDENTITY_FIELDS = (
    "session_user", "current_schema", "db_name", "db_unique_name", "service_name",
    "container_id", "container_name", "edition", "database_version",
)
# Metadata of a package capture keeps its original field set; the other groups name their group and unit.
PACKAGE_METADATA_FIELDS = frozenset({"schemaVersion", "feature", "schema", "package", "identity", "units"})
UNIT_METADATA_FIELDS = frozenset({"schemaVersion", "feature", "schema", "group", "unit", "identity", "units"})


class FeatureSourcesError(ValueError):
    """A stored-unit capture or source-review request is unsafe or incomplete."""


@dataclass(frozen=True)
class UnitGroup:
    """One capture unit: a package or type with its optional body, or a single trigger, procedure or function."""

    name: str
    directory: str
    types: tuple[tuple[str, str], ...]  # (object type, file suffix); the first type is required

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(kind for kind, _suffix in self.types)

    def suffix(self, kind: str) -> str:
        return dict(self.types)[kind]


UNIT_GROUPS: tuple[UnitGroup, ...] = (
    UnitGroup("package", "packages", (("PACKAGE", "pks"), ("PACKAGE BODY", "pkb"))),
    UnitGroup("type", "types", (("TYPE", "tps"), ("TYPE BODY", "tpb"))),
    UnitGroup("trigger", "triggers", (("TRIGGER", "trg"),)),
    UnitGroup("procedure", "procedures", (("PROCEDURE", "prc"),)),
    UnitGroup("function", "functions", (("FUNCTION", "fnc"),)),
)
UNIT_GROUP_BY_NAME = {group.name: group for group in UNIT_GROUPS}
UNIT_GROUP_BY_KIND = {kind: group for group in UNIT_GROUPS for kind in group.kinds}


def _unit_group(name: str) -> UnitGroup:
    group = UNIT_GROUP_BY_NAME.get(name) if isinstance(name, str) else None
    if group is None:
        raise FeatureSourcesError(f"unsupported unit group: {name!r}")
    return group


def _feature_scope(root: Path, feature: str, schema: str) -> tuple[Path, Path]:
    if not isinstance(feature, str) or FEATURE_RE.fullmatch(feature) is None:
        raise FeatureSourcesError("feature must be a lowercase hyphenated slug")
    if not isinstance(schema, str) or SCHEMA_RE.fullmatch(schema) is None:
        raise FeatureSourcesError("schema must be an uppercase Oracle identifier")
    feature_dir = root / "features" / feature / schema
    scratch_dir = root / "scratch" / "feature-source" / feature / schema
    return feature_dir, scratch_dir


def _is_reparse(path: Path, metadata: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)


def _ensure_directory(path: Path, root: Path) -> None:
    try:
        relative = relative_to_root(path, root)
    except ValueError as error:
        raise FeatureSourcesError(f"feature source directory is outside the repository: {path}") from error
    current = root
    try:
        metadata = current.lstat()
        if _is_reparse(current, metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise FeatureSourcesError(f"repository root is not an ordinary directory: {root}")
        for part in relative.parts:
            current = current / part
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                current.mkdir()
                metadata = current.lstat()
            if _is_reparse(current, metadata) or not stat.S_ISDIR(metadata.st_mode):
                raise FeatureSourcesError(f"feature source path contains an unsafe directory: {current}")
    except FeatureSourcesError:
        raise
    except OSError as error:
        raise FeatureSourcesError(f"cannot prepare feature source directory {path}: {error}") from error


def _ordinary_file(path: Path, root: Path) -> bytes | None:
    try:
        relative_to_root(path, root)
    except ValueError as error:
        raise FeatureSourcesError(f"feature source path is outside the repository: {path}") from error
    current = root
    try:
        if _is_reparse(current, current.lstat()):
            raise FeatureSourcesError(f"repository root cannot be a symlink or reparse point: {root}")
        relative = path.relative_to(root)
        for part in relative.parts:
            current = current / part
            metadata = current.lstat()
            if _is_reparse(current, metadata):
                raise FeatureSourcesError(f"symlink or reparse point is not allowed: {current}")
        if not stat.S_ISREG(metadata.st_mode):
            raise FeatureSourcesError(f"feature source must be an ordinary file: {path}")
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except FeatureSourcesError:
        raise
    except OSError as error:
        raise FeatureSourcesError(f"cannot read feature source {path}: {error}") from error


def _atomic_group_write(root: Path, outputs: Mapping[Path, bytes], deletes: Sequence[Path] = ()) -> None:
    """Install a small related group of files, restoring previous bytes on failure."""
    previous: dict[Path, bytes | None] = {}
    staged: dict[Path, Path] = {}
    replaced: list[Path] = []
    removed: list[Path] = []
    try:
        for path in (*outputs.keys(), *deletes):
            if path in previous:
                raise FeatureSourcesError(f"duplicate feature source transaction path: {path}")
            previous[path] = _ordinary_file(path, root)
            _ensure_directory(path.parent, root)
        for path, data in outputs.items():
            descriptor, name = tempfile.mkstemp(prefix=".feature-source-", dir=path.parent)
            temp = Path(name)
            staged[path] = temp
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        for path, temp in staged.items():
            os.replace(temp, path)
            replaced.append(path)
        for path in deletes:
            if path.exists():
                path.unlink()
                removed.append(path)
    except Exception as error:
        rollback_error = None
        for path in reversed((*replaced, *removed)):
            old = previous[path]
            try:
                if old is None:
                    path.unlink(missing_ok=True)
                else:
                    descriptor, name = tempfile.mkstemp(prefix=".feature-source-restore-", dir=path.parent)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(old)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(name, path)
            except OSError as restore_error:
                rollback_error = restore_error
                break
        if rollback_error is not None:
            raise FeatureSourcesError(f"feature source write failed and rollback was incomplete: {rollback_error}") from error
        if isinstance(error, FeatureSourcesError):
            raise
        raise FeatureSourcesError(f"feature source write failed; prior files were restored: {error}") from error
    finally:
        for temp in staged.values():
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def _unit_name(value: str, label: str = "package") -> str:
    if not isinstance(value, str) or not value:
        raise FeatureSourcesError(f"{label} must be a nonempty Oracle identifier")
    if QUOTED_PACKAGE_NAME_RE.fullmatch(value):
        name = value[1:-1].replace('""', '"')
        if not name or any(ord(char) < 32 for char in name):
            raise FeatureSourcesError(f"quoted {label} name is empty or contains a control character")
        return name
    if PACKAGE_NAME_RE.fullmatch(value) is None:
        raise FeatureSourcesError(f"{label} must be an Oracle identifier or a double-quoted identifier")
    return value.upper()


def _unit_stem(name: str) -> str:
    if name.isascii() and name.upper() == name and PACKAGE_NAME_RE.fullmatch(name) is not None:
        return name.lower()
    try:
        encoded_name = name.encode("utf-8")
    except UnicodeEncodeError as error:
        raise FeatureSourcesError("package name must be valid UTF-8") from error
    natural_stem = "quoted-" + quote(name, safe="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_$#-")
    # Preserve existing short paths, but keep percent-encoded Unicode and
    # quoted names below ordinary filesystem component limits.
    if len(natural_stem.encode("ascii")) <= 180:
        return natural_stem
    hint = re.sub(r"[^A-Za-z0-9_$#-]+", "-", name).strip("-").lower()[:32] or "unit"
    return f"quoted-{hint}-{hashlib.sha256(encoded_name).hexdigest()}"


def _unit_source_path(feature_dir: Path, name: str, object_type: str) -> Path:
    group = UNIT_GROUP_BY_KIND[object_type]
    return feature_dir / group.directory / f"{_unit_stem(name)}.{group.suffix(object_type)}.sql"


def _capture_metadata_path(scratch_dir: Path, group: str, unit: str) -> Path:
    stem = _unit_stem(unit)
    if group == "package":
        return scratch_dir / f"capture-{stem}.json"
    return scratch_dir / f"capture-{group}-{stem}.json"


def _metadata_scope(metadata: Mapping) -> tuple[str, str]:
    """The (group, unit name) a validated capture metadata document covers."""
    if "group" in metadata:
        return metadata["group"], metadata["unit"]
    return "package", metadata["package"]


def _strict_capture_metadata(path: Path, root: Path) -> dict | None:
    raw = _ordinary_file(path, root)
    if raw is None:
        return None
    try:
        text = raw.decode("utf-8")
        if "\r" in text or text.startswith("\ufeff"):
            raise ValueError("capture metadata must be UTF-8/LF without a BOM")

        def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key {key!r}")
                result[key] = value
            return result

        value = json.loads(text, object_pairs_hook=unique_pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise FeatureSourcesError(f"feature capture metadata is invalid: {error}") from error
    if not isinstance(value, dict) or set(value) not in (PACKAGE_METADATA_FIELDS, UNIT_METADATA_FIELDS):
        raise FeatureSourcesError("feature capture metadata has unsupported or missing fields")
    if set(value) == UNIT_METADATA_FIELDS and (
        value["group"] not in UNIT_GROUP_BY_NAME or value["group"] == "package" or not isinstance(value["unit"], str) or not value["unit"]
    ):
        raise FeatureSourcesError("feature capture metadata group or unit is malformed")
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != CAPTURE_SCHEMA_VERSION:
        raise FeatureSourcesError("feature capture metadata schemaVersion must be integer 1")
    if not isinstance(value["identity"], dict) or not isinstance(value["units"], list):
        raise FeatureSourcesError("feature capture metadata identity or units are malformed")
    if set(value["identity"]) != set(IDENTITY_FIELDS) or any(
        not isinstance(value["identity"].get(field), str) or not value["identity"][field]
        for field in IDENTITY_FIELDS
    ):
        raise FeatureSourcesError("feature capture metadata observed identity is incomplete")
    group, unit_name = _metadata_scope(value)
    if not isinstance(unit_name, str) or not unit_name:
        raise FeatureSourcesError("feature capture metadata names no unit")
    kinds = UNIT_GROUP_BY_NAME[group].kinds
    for unit in value["units"]:
        if isinstance(unit, dict) and (unit.get("type") not in kinds or unit.get("name") != unit_name):
            raise FeatureSourcesError("feature capture metadata lists a unit outside its own group")
    return value


def _metadata_path(value: object, *, prefix: tuple[str, ...], label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise FeatureSourcesError(f"feature capture metadata {label} is not a safe repository-relative path")
    raw_parts = value.split("/")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
        or path.parts[:len(prefix)] != prefix
        or len(path.parts) <= len(prefix)
    ):
        raise FeatureSourcesError(f"feature capture metadata {label} escapes its expected tree")
    return path


def _capture_sources(
    feature: str,
    schema: str,
    unit: str,
    catalog: Mapping,
    group: str = "package",
) -> tuple[dict, ...]:
    unit_group = _unit_group(group)
    sections = catalog.get("sections") if isinstance(catalog, Mapping) else None
    if not isinstance(sections, Mapping):
        raise FeatureSourcesError("DEV source capture has no baseline sections")
    source_rows = sections.get("baseline-source")
    settings_rows = sections.get("baseline-settings")
    if not isinstance(source_rows, list) or not isinstance(settings_rows, list):
        raise FeatureSourcesError("DEV source capture is missing source or compiler-settings rows")
    config = {"prefixes": [], "excludedObjects": []}
    source_units = baseline._source_units(catalog, config, schema)
    settings_map = baseline._settings_map(catalog, config, schema)
    selected = [(kind, unit) for kind in unit_group.kinds if (kind, unit) in source_units]
    if not selected:
        raise FeatureSourcesError(f"{unit_group.name} {unit} was not found in {schema} on DEV")
    if selected[0] != (unit_group.kinds[0], unit):
        raise FeatureSourcesError(f"{unit_group.kinds[0].lower()} specification was not found in {schema} on DEV: {unit}")

    records: list[dict] = []
    for object_type, name in selected:
        settings = settings_map.get((object_type, name))
        if settings is None:
            raise FeatureSourcesError(f"compiler settings are missing for {object_type} {name} in {schema}")
        try:
            selected_settings = baseline._setting_row([settings], name, object_type)
            sql = new_migration.step_sql(schema, object_type, name, source_units[(object_type, name)]["lines"], settings=selected_settings)
        except (baseline.BaselineError, new_migration.NewMigrationError, KeyError, TypeError, ValueError) as error:
            raise FeatureSourcesError(f"captured {object_type} {name} is incomplete: {error}") from error
        source = sql.encode("utf-8")
        relative_path = _unit_source_path(Path("features") / feature / schema, name, object_type).as_posix()
        records.append({
            "type": object_type,
            "name": name,
            "path": relative_path,
            "source": source,
            "sha256": hashlib.sha256(source).hexdigest(),
        })
    return tuple(records)


def _group_keys(schema: str, group: UnitGroup, name: str) -> tuple[tuple[str, str, str], ...]:
    return tuple((schema, kind, name) for kind in group.kinds)


def _preview(lines: Sequence[str], limit: int = 10) -> str:
    shown = "\n".join(f"    {line[:160]}" for line in lines[:limit])
    more = f"\n    ... and {len(lines) - limit} more" if len(lines) > limit else ""
    return shown + more


def _unapplied_refusal(
    action: str, label: str, comparison: feature_compare.Comparison, accepted: Mapping | None,
) -> FeatureSourcesError:
    decision = f"\n  an accepted difference is recorded for it: {accepted['decision']}" if accepted else ""
    return FeatureSourcesError(
        f"{action} refused: {label} has {len(comparison.only_feature)} line(s) that DEV does not have "
        f"and the DEV capture would erase them:\n{_preview(comparison.only_feature)}{decision}\n"
        "Apply them to DEV and repeat, pass --discard-unapplied to erase them knowingly, "
        "or (with --move-from) pass --keep-file to move the file unchanged."
    )


def _check_names_unit(sql: str, units: set[tuple[str, str]]) -> bool:
    return any(pair in units for pair in feature_claims.checked_objects(sql))


def _json_bytes(document: object) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _checks_without_units(
    root: Path, feature: str, schema: str, units: set[tuple[str, str]],
) -> tuple[bytes | None, list[str]]:
    """New bytes of a feature's checks.json without the checks that name the moved units, and the removed ids."""
    path = root / "features" / feature / schema / "checks.json"
    raw = _ordinary_file(path, root)
    if raw is None:
        return None, []
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        return None, []
    if not isinstance(document, dict):
        return None, []
    removed: list[str] = []
    for section in ("preconditions", "postconditions"):
        kept = []
        for check in document.get(section, []):
            if isinstance(check, dict) and isinstance(check.get("sql"), str) and _check_names_unit(check["sql"], units):
                removed.append(str(check.get("id")))
            else:
                kept.append(check)
        if section in document:
            document[section] = kept
    return (_json_bytes(document), removed) if removed else (None, [])


def _accepted_list(root: Path, feature: str) -> tuple[Path, list]:
    path = root / "features" / feature / "accepted-differences.json"
    raw = _ordinary_file(path, root)
    if raw is None:
        return path, []
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as error:
        raise FeatureSourcesError(f"accepted differences of {feature} are unreadable: {error}") from error
    if not isinstance(document, list):
        raise FeatureSourcesError(f"accepted differences of {feature} must be a list")
    return path, document


def _accepted_entry(root: Path, feature: str, key: tuple[str, str, str]) -> Mapping | None:
    _path, entries = _accepted_list(root, feature)
    for entry in entries:
        if isinstance(entry, dict) and (entry.get("schema"), entry.get("type"), entry.get("name")) == key:
            return entry
    return None


def capture_package(
    repo_root: Path,
    feature: str,
    schema: str,
    package: str,
    *,
    environ: Mapping[str, str],
    refresh: bool = False,
    move_from: str | None = None,
    discard_unapplied: bool = False,
    keep_file: bool = False,
    report: list[str] | None = None,
) -> tuple[Path, ...]:
    """Capture the complete DEV package spec/body into stable feature source files (the `package` unit group)."""
    return capture_unit(
        repo_root, feature, schema, "package", package,
        environ=environ, refresh=refresh, move_from=move_from, discard_unapplied=discard_unapplied,
        keep_file=keep_file, report=report,
    )


def capture_unit(
    repo_root: Path,
    feature: str,
    schema: str,
    group: str,
    unit: str,
    *,
    environ: Mapping[str, str],
    refresh: bool = False,
    move_from: str | None = None,
    discard_unapplied: bool = False,
    keep_file: bool = False,
    report: list[str] | None = None,
) -> tuple[Path, ...]:
    """Capture one DEV stored unit group (package, type, trigger, procedure or function) into feature source files.

    A unit another feature holds is refused unless `move_from` names that feature. A move or a refresh first
    compares the file it replaces with DEV and stops when the file has lines DEV lacks, unless `discard_unapplied`.
    `keep_file` moves the old bytes unchanged instead of capturing from DEV.
    """
    unit_group = _unit_group(group)
    label = unit_group.name
    requested_root = Path(repo_root)
    if requested_root.is_symlink():
        raise FeatureSourcesError("repository root cannot be a symlink")
    try:
        root = requested_root.resolve(strict=True)
    except OSError as error:
        raise FeatureSourcesError(f"repository root is unavailable: {error}") from error
    feature_dir, scratch_dir = _feature_scope(root, feature, schema)
    name = _unit_name(unit, label)
    if type(refresh) is not bool or type(discard_unapplied) is not bool or type(keep_file) is not bool:
        raise FeatureSourcesError("refresh, discard_unapplied and keep_file must be booleans")
    if not isinstance(environ, Mapping):
        raise FeatureSourcesError("environment values must be a mapping")
    if move_from is not None:
        if not isinstance(move_from, str) or FEATURE_RE.fullmatch(move_from) is None:
            raise FeatureSourcesError("move-from must be a lowercase hyphenated feature slug")
        if move_from == feature:
            raise FeatureSourcesError(f"move-from names the feature that receives the {label}")
    if keep_file and move_from is None:
        raise FeatureSourcesError("keep-file only applies to a move: pass --move-from")
    messages = report if report is not None else []

    # Ownership: a unit another feature holds is not captured a second time.
    claims, _unanalysed = feature_claims.collect_claims(root)
    keys = _group_keys(schema, unit_group, name)
    other_claims = [claim for claim in claims if claim.key in keys and claim.feature != feature]
    other_holders = sorted({claim.feature for claim in other_claims})
    if other_holders and move_from is None:
        raise FeatureSourcesError(
            f"{label} {name} is held by feature {', '.join(other_holders)}; "
            f"to take it over run the capture with --move-from {other_holders[0]}, or leave it there"
        )
    if move_from is not None:
        if move_from not in other_holders:
            raise FeatureSourcesError(f"feature {move_from} does not hold {label} {name} in {schema}")
        if set(other_holders) - {move_from}:
            raise FeatureSourcesError(
                f"{label} {name} is also held by feature {', '.join(sorted(set(other_holders) - {move_from}))}; keep one owner first"
            )
    old_files = {claim.kind: root / PurePosixPath(claim.path) for claim in other_claims if claim.feature == move_from}

    _ensure_directory(scratch_dir, root)
    metadata_path = _capture_metadata_path(scratch_dir, label, name)
    old_metadata = _strict_capture_metadata(metadata_path, root)
    if old_metadata is not None:
        if (old_metadata["feature"], old_metadata["schema"], _metadata_scope(old_metadata)) != (feature, schema, (label, name)):
            raise FeatureSourcesError(f"feature capture metadata scope does not match this {label}")

    outputs: dict[Path, bytes] = {}
    deletes: list[Path] = []

    if keep_file:
        # Move the old bytes unchanged; nothing is read from DEV.
        source_paths = []
        for object_type in unit_group.kinds:
            old_path = old_files.get(object_type)
            if old_path is None:
                continue
            raw = _ordinary_file(old_path, root)
            if raw is None:
                raise FeatureSourcesError(f"feature file to move is unreadable: {relative_to_root(old_path, root).as_posix()}")
            target = _unit_source_path(feature_dir, name, object_type)
            if _ordinary_file(target, root) is not None:
                raise FeatureSourcesError(f"feature already has a file at {relative_to_root(target, root).as_posix()}")
            outputs[target] = raw
            deletes.append(old_path)
            source_paths.append(target)
        moved_units = {(kind, name) for kind in old_files}
        _finish_move(root, feature, move_from, schema, label, name, moved_units, outputs, deletes, messages, keep_file=True)
        _atomic_group_write(root, outputs, deletes)
        messages.append(f"moved {name} from {move_from} to {feature} unchanged; DEV was not read. Run a capture with --refresh after the file equals DEV to retain a DEV baseline")
        return tuple(source_paths)

    capture_root = Path(tempfile.mkdtemp(prefix="capture-", dir=scratch_dir))
    try:
        try:
            catalog = baseline._capture(
                environ,
                "dev",
                schema,
                ("baseline-source", "baseline-settings"),
                capture_root,
                # The existing SQL filter uppercases prefixes, so it cannot select a
                # case-sensitive quoted name. In that case capture the source sections
                # completely, then retain only the exact requested unit in memory.
                baseline_prefixes=[name] if name == name.upper() else [],
            )
            baseline._complete(catalog, ("baseline-source", "baseline-settings"))
            identity = baseline._source_identity(catalog.get("identity"), f"DEV {label} capture")
        except (baseline.BaselineError, OSError, ValueError) as error:
            raise FeatureSourcesError(f"DEV {label} capture failed: {error}") from error
    finally:
        shutil.rmtree(capture_root, ignore_errors=True)
    if identity["current_schema"].upper() != schema:
        raise FeatureSourcesError(
            f"DEV {label} capture current schema {identity['current_schema']!r} does not match {schema}"
        )
    try:
        records = _capture_sources(feature, schema, name, catalog, group=label)
    except FeatureSourcesError as error:
        if move_from is not None:
            raise FeatureSourcesError(f"{error}; pass --keep-file to move the old file without DEV") from error
        raise

    snapshot_dir = scratch_dir / "captured"
    source_paths: list[Path] = []
    new_units: list[dict] = []
    for record in records:
        source_path = root / PurePosixPath(record["path"])
        snapshot_path = snapshot_dir / source_path.name
        source_paths.append(source_path)
        old_bytes = _ordinary_file(source_path, root)
        if old_bytes is not None and old_bytes != record["source"] and not refresh:
            raise FeatureSourcesError(
                f"feature source differs from the current DEV capture; review it or use refresh: {record['path']}"
            )
        if old_metadata is not None and old_bytes is None and not refresh:
            raise FeatureSourcesError(
                f"captured feature source was removed; review it or use refresh: {record['path']}"
            )
        # What the capture replaces: this feature's own file on a refresh, the old owner's file on a move.
        replaced_label, replaced_bytes, replaced_feature = record["path"], old_bytes, feature
        if move_from is not None:
            replaced_path = old_files.get(record["type"])
            replaced_bytes = _ordinary_file(replaced_path, root) if replaced_path is not None else None
            replaced_label = relative_to_root(replaced_path, root).as_posix() if replaced_path is not None else record["path"]
            replaced_feature = move_from
        if replaced_bytes is not None and replaced_bytes != record["source"] and (move_from is not None or refresh):
            comparison = feature_compare.compare(
                replaced_bytes.decode("utf-8", errors="replace"), record["source"].decode("utf-8"),
            )
            if comparison.only_feature and not discard_unapplied:
                accepted = _accepted_entry(root, replaced_feature, (schema, record["type"], record["name"]))
                raise _unapplied_refusal("move" if move_from is not None else "refresh", replaced_label, comparison, accepted)
            if move_from is not None:
                messages.append(
                    f"{record['type']} {name}: {comparison.state.lower()}, {len(comparison.only_live)} line(s) only on DEV "
                    f"are taken, {len(comparison.only_feature)} line(s) only in the old file are dropped"
                )
        baseline_bytes = _ordinary_file(snapshot_path, root)
        if old_metadata is None or refresh:
            outputs[snapshot_path] = record["source"]
        elif baseline_bytes is None:
            raise FeatureSourcesError(f"feature capture baseline is missing; use refresh to recapture: {snapshot_path}")
        outputs[source_path] = record["source"]
        new_units.append({
            "type": record["type"],
            "name": record["name"],
            "path": record["path"],
            "capturedPath": relative_to_root(snapshot_path, root).as_posix(),
            "sha256": record["sha256"],
        })

    if move_from is not None:
        for old_path in old_files.values():
            deletes.append(old_path)
        _finish_move(root, feature, move_from, schema, label, name, {(kind, name) for kind in old_files}, outputs, deletes, messages, keep_file=False)

    if old_metadata is not None:
        current_keys = {(record["type"], record["name"]) for record in records}
        prior_keys = set()
        current_hashes = {(record["type"], record["name"]): record["sha256"] for record in records}
        for unit in old_metadata["units"]:
            if not isinstance(unit, dict) or not isinstance(unit.get("type"), str) or not isinstance(unit.get("name"), str) or not isinstance(unit.get("path"), str):
                raise FeatureSourcesError("feature capture metadata has a malformed unit record")
            key = (unit["type"], unit["name"])
            prior_keys.add(key)
            source_rel = _metadata_path(
                unit["path"], prefix=("features", feature, schema), label="source path",
            )
            captured_rel = _metadata_path(
                unit.get("capturedPath"),
                prefix=("scratch", "feature-source", feature, schema, "captured"),
                label="capturedPath",
            )
            if source_rel.suffix.lower() != ".sql" or captured_rel.name != source_rel.name:
                raise FeatureSourcesError("feature capture metadata unit paths do not match a SQL source file")
            if not isinstance(unit.get("sha256"), str) or re.fullmatch(r"[0-9a-f]{64}", unit["sha256"], re.ASCII) is None:
                raise FeatureSourcesError("feature capture metadata unit hash is malformed")
            if current_hashes.get(key) != unit["sha256"] and not refresh:
                raise FeatureSourcesError(f"live {label} source changed since its last capture; review it or use refresh")
            if key not in current_keys:
                if not refresh:
                    raise FeatureSourcesError(f"live {label} unit set changed; review it or use refresh")
                deletes.append(root / source_rel)
                deletes.append(root / captured_rel)
        if prior_keys != current_keys and not refresh:
            raise FeatureSourcesError(f"live {label} unit set changed; review it or use refresh")

    scope_fields = {"package": name} if label == "package" else {"group": label, "unit": name}
    metadata = {
        "schemaVersion": CAPTURE_SCHEMA_VERSION,
        "feature": feature,
        "schema": schema,
        **scope_fields,
        "identity": identity,
        "units": new_units,
    }
    metadata_bytes = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    outputs[metadata_path] = metadata_bytes
    _atomic_group_write(root, outputs, deletes)
    return tuple(source_paths)


def _finish_move(
    root: Path,
    feature: str,
    old_feature: str,
    schema: str,
    group: str,
    name: str,
    units: set[tuple[str, str]],
    outputs: dict[Path, bytes],
    deletes: list[Path],
    messages: list[str],
    *,
    keep_file: bool,
) -> None:
    """Everything else a move changes, written in the same atomic group: the old owner's checks, retained
    baseline and accepted differences, and (for keep-file) the receiving feature's accepted differences."""
    old_checks, removed = _checks_without_units(root, old_feature, schema, units)
    if old_checks is not None:
        outputs[root / "features" / old_feature / schema / "checks.json"] = old_checks
        messages.append(f"removed {len(removed)} check(s) of {old_feature} that named {name}: {', '.join(removed)}")
    messages.append(f"add a check for {name} to features/{feature}/{schema}/checks.json if the feature has none for it")
    # Retained DEV baseline of the old owner.
    old_scratch = root / "scratch" / "feature-source" / old_feature / schema
    old_meta = _capture_metadata_path(old_scratch, group, name)
    old_meta_data = _strict_capture_metadata(old_meta, root)
    if old_meta_data is not None:
        for unit in old_meta_data["units"]:
            captured = old_scratch / "captured" / Path(str(unit.get("path", ""))).name
            if _ordinary_file(captured, root) is not None:
                deletes.append(captured)
        deletes.append(old_meta)
    # Accepted differences recorded for the moved units.
    old_accepted_path, old_entries = _accepted_list(root, old_feature)
    moving = [
        entry for entry in old_entries
        if isinstance(entry, dict) and entry.get("schema") == schema and entry.get("name") == name and entry.get("type") in {kind for kind, _ in units}
    ]
    if moving:
        remaining = [entry for entry in old_entries if entry not in moving]
        if remaining:
            outputs[old_accepted_path] = _json_bytes(remaining)
        else:
            deletes.append(old_accepted_path)
        if keep_file:
            new_path, new_entries = _accepted_list(root, feature)
            outputs[new_path] = _json_bytes([*new_entries, *moving])
            messages.append(f"moved {len(moving)} accepted difference(s) of {name} to {feature}")
        else:
            messages.append(f"dropped {len(moving)} accepted difference(s) of {name}: the DEV capture replaces the file")


def _validate_day(day: str) -> None:
    if not isinstance(day, str) or DAY_RE.fullmatch(day) is None:
        raise FeatureSourcesError("day must use YYYY-MM-DD")
    try:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("noncanonical date")
    except ValueError as error:
        raise FeatureSourcesError("day must be a valid YYYY-MM-DD date") from error


def _identity_document(identity: Mapping) -> dict[str, str]:
    if not isinstance(identity, Mapping):
        raise FeatureSourcesError("observed identity must be a mapping")
    result: dict[str, str] = {}
    for key, value in identity.items():
        if not isinstance(key, str) or not key or not isinstance(value, str) or not value or any(ord(char) < 32 for char in key + value):
            raise FeatureSourcesError("observed identity must contain nonempty text fields")
        result[key] = value
    if not result:
        raise FeatureSourcesError("observed identity cannot be empty")
    return dict(sorted(result.items()))


def _review_diff(before: str, after: str, feature: str, schema: str, unit: tuple[str, str]) -> bytes:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{unit[0]} {unit[1]} (captured)",
        tofile=f"b/features/{feature}/{schema}/{unit[0]} {unit[1]}",
        n=3,
    )
    result = "".join(lines)
    if not result:
        result = "# no source difference\n"
    return result.encode("utf-8")


def _unit_artifact_stem(unit: tuple[str, str], *, review_content: bytes = b"") -> str:
    """Return a bounded ASCII path component bound to identity and review bytes."""
    if not isinstance(unit, tuple) or len(unit) != 2 or any(not isinstance(value, str) or not value for value in unit):
        raise FeatureSourcesError("unit must be an (object type, object name) pair")
    if not isinstance(review_content, bytes):
        raise FeatureSourcesError("review content must be bytes")
    try:
        identity = unit[0].encode("utf-8") + b"\0" + unit[1].encode("utf-8")
    except UnicodeEncodeError as error:
        raise FeatureSourcesError("unit identity must be valid UTF-8") from error
    type_hint = re.sub(r"[^a-z0-9]+", "-", unit[0].lower()).strip("-")[:8] or "unit"
    name_hint = re.sub(r"[^a-z0-9]+", "-", unit[1].lower()).strip("-")[:16] or "unit"
    identity_hash = hashlib.sha256(identity).hexdigest()[:32]
    content_hash = hashlib.sha256(review_content).hexdigest()[:32]
    return f"{type_hint}-{name_hint}-{identity_hash}-{content_hash}"


def write_source_review(
    repo_root: Path,
    feature: str,
    schema: str,
    unit: tuple[str, str],
    before: str,
    after: str,
    *,
    identity: Mapping[str, str],
    baseline_kind: str,
    payload_digest: str | None,
    day: str,
    target_plan_digest: str | None = None,
) -> tuple[Path, Path]:
    """Write a non-overwriting unified source diff and its provenance manifest."""
    requested_root = Path(repo_root)
    if requested_root.is_symlink():
        raise FeatureSourcesError("repository root cannot be a symlink")
    root = requested_root.resolve(strict=True)
    _feature_dir, _scratch_dir = _feature_scope(root, feature, schema)
    _validate_day(day)
    if not isinstance(unit, tuple) or len(unit) != 2 or any(not isinstance(value, str) or not value for value in unit):
        raise FeatureSourcesError("unit must be an (object type, object name) pair")
    if not isinstance(baseline_kind, str) or re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", baseline_kind, re.ASCII) is None:
        raise FeatureSourcesError("baseline_kind must be a lowercase slug")
    if payload_digest is not None and (not isinstance(payload_digest, str) or re.fullmatch(r"[0-9a-f]{64}", payload_digest, re.ASCII) is None):
        raise FeatureSourcesError("payload_digest must be a SHA-256 hex digest or None")
    if target_plan_digest is not None and (not isinstance(target_plan_digest, str) or re.fullmatch(r"[0-9a-f]{64}", target_plan_digest, re.ASCII) is None):
        raise FeatureSourcesError("target_plan_digest must be a SHA-256 hex digest or None")
    if not isinstance(before, str) or not isinstance(after, str):
        raise FeatureSourcesError("before and after source must be text")
    try:
        before_bytes, after_bytes = before.encode("utf-8"), after.encode("utf-8")
    except UnicodeEncodeError as error:
        raise FeatureSourcesError("source review text must be valid UTF-8") from error
    observed_identity = _identity_document(identity)
    before_sha = hashlib.sha256(before_bytes).hexdigest()
    after_sha = hashlib.sha256(after_bytes).hexdigest()
    diff_bytes = _review_diff(before, after, feature, schema, unit)
    metadata = {
        "schemaVersion": 1,
        "feature": feature,
        "schema": schema,
        "owner": schema,
        "unitType": unit[0],
        "unitName": unit[1],
        "observedIdentity": observed_identity,
        "beforeSha256": before_sha,
        "afterSha256": after_sha,
        "payloadDigest": payload_digest,
        "baselineKind": baseline_kind,
        "diffSha256": hashlib.sha256(diff_bytes).hexdigest(),
        "targetPlanDigest": target_plan_digest,
    }
    metadata_bytes = (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    artifact_content = metadata_bytes + b"\0" + diff_bytes
    unit_stem = _unit_artifact_stem(unit, review_content=artifact_content)
    directory = root / "ai_generate" / day / "feature-diffs" / feature / schema / baseline_kind / unit_stem
    diff_path = directory / "source.diff"
    review_path = directory / "review.json"
    _ensure_directory(directory, root)
    old_diff = _ordinary_file(diff_path, root)
    old_review = _ordinary_file(review_path, root)
    if old_diff is not None or old_review is not None:
        if old_diff == diff_bytes and old_review == metadata_bytes:
            return diff_path, review_path
        raise FeatureSourcesError(f"refusing to overwrite an existing feature source review: {directory}")
    _atomic_group_write(root, {diff_path: diff_bytes, review_path: metadata_bytes})
    return diff_path, review_path


def _read_text(path: Path, root: Path, label: str) -> str:
    raw = _ordinary_file(path, root)
    if raw is None:
        raise FeatureSourcesError(f"{label} is missing: {path}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FeatureSourcesError(f"{label} is not UTF-8: {path}") from error


def diff_captured_package(repo_root: Path, feature: str, schema: str, *, day: str) -> tuple[Path, ...]:
    """Compare edited feature package files with their captured DEV source."""
    requested_root = Path(repo_root)
    if requested_root.is_symlink():
        raise FeatureSourcesError("repository root cannot be a symlink")
    try:
        root = requested_root.resolve(strict=True)
    except OSError as error:
        raise FeatureSourcesError(f"repository root is unavailable: {error}") from error
    _validate_day(day)
    feature_dir, scratch_dir = _feature_scope(root, feature, schema)
    try:
        metadata_paths = sorted(scratch_dir.glob("capture-*.json"), key=lambda path: path.name)
    except OSError as error:
        raise FeatureSourcesError(f"cannot enumerate feature capture baselines: {error}") from error
    metadata_documents = [(_strict_capture_metadata(path, root), path) for path in metadata_paths]
    metadata_documents = [(metadata, path) for metadata, path in metadata_documents if metadata is not None]
    if not metadata_documents:
        raise FeatureSourcesError("feature capture baseline is missing; run feature capture first")
    artifacts: list[Path] = []
    for metadata, _metadata_file in metadata_documents:
        if (metadata["feature"], metadata["schema"]) != (feature, schema):
            continue
        identity = _identity_document(metadata["identity"])
        for unit in metadata["units"]:
            if not isinstance(unit, dict) or set(unit) != {"type", "name", "path", "capturedPath", "sha256"}:
                raise FeatureSourcesError("feature capture metadata has a malformed unit record")
            source_rel = _metadata_path(
                unit["path"], prefix=("features", feature, schema), label="source path",
            )
            captured_rel = _metadata_path(
                unit["capturedPath"],
                prefix=("scratch", "feature-source", feature, schema, "captured"),
                label="capturedPath",
            )
            if source_rel.suffix.lower() != ".sql" or captured_rel.name != source_rel.name:
                raise FeatureSourcesError("feature capture metadata unit paths do not match a SQL source file")
            if not isinstance(unit["sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", unit["sha256"], re.ASCII) is None:
                raise FeatureSourcesError("feature capture metadata unit hash is malformed")
            source_path = root / source_rel
            captured_path = root / captured_rel
            before = _read_text(captured_path, root, "captured DEV source").encode("utf-8")
            if hashlib.sha256(before).hexdigest() != unit["sha256"]:
                raise FeatureSourcesError(f"captured DEV source does not match its stored hash: {unit['capturedPath']}")
            after_text = _read_text(source_path, root, "feature source")
            diff_path, review_path = write_source_review(
                root,
                feature,
                schema,
                (unit["type"], unit["name"]),
                before.decode("utf-8"),
                after_text,
                identity=identity,
                baseline_kind="dev-capture",
                payload_digest=None,
                day=day,
            )
            artifacts.extend((diff_path, review_path))
    if not artifacts:
        raise FeatureSourcesError(f"no package source units are recorded for {feature}/{schema}")
    return tuple(artifacts)
