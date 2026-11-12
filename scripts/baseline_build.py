"""Build subcommand for scripts/baseline.py, moved out of it: the build entry point and the migration folder writers.

scripts/baseline.py keeps the CLI, the exports and the helpers that the exports and the build share. Names that stay
there are read as baseline.<name> at call time, so patches on scripts.baseline still reach this code. The SQL helpers,
the reference-data readers and the bundle generators moved on to baseline_sql, baseline_reference_data and
baseline_bundles; this module re-exports their names, and baseline re-exports the entry point and the names that tests
and other scripts reference.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from .compare_schema import compare_environment_catalogs
from .db_targets import TargetResolutionError, resolve_target
from . import directory_install
from .baseline_bundle import BaselineComponent, combine_baseline_components, validate_bundle_name
from .migration_manifest import FOLDER_RE
from .path_utils import relative_to_root
from .schema_catalog import SchemaSnapshot, REQUIRED_IDENTITY, same_database_scope


def _validate_baseline_scope(source: Mapping, target: Mapping) -> None:
    identities = []
    for label, catalog in (("source", source), ("target", target)):
        identity = catalog.get("identity")
        missing = [
            field for field in REQUIRED_IDENTITY
            if not isinstance(identity, Mapping)
            or field not in identity
            or identity[field] is None
            or not str(identity[field]).strip()
        ]
        if missing:
            raise baseline.BaselineError(f"{label} identity is incomplete: {', '.join(missing)}")
        identities.append(identity)
    if same_database_scope(identities[0], identities[1]):
        raise baseline.BaselineError("baseline source and target have the same observed database/container/schema/edition scope")


def _folder_contents_match(folder: Path, expected: Mapping[str, bytes]) -> bool:
    try:
        observed = {
            path.name: path.read_bytes() for path in folder.iterdir()
            if path.is_file() and not path.is_symlink()
            and not (path.name.startswith("status.") and path.name.endswith(".json"))
            and not path.name.startswith("attempt-event.")
        }
    except OSError:
        return False
    return observed == dict(expected)


def _baseline_family_state(root: Path, parent: Path, family: str) -> tuple[dict[int, Path], tuple]:
    """Validate existing family folders and fingerprint their immutable evidence."""
    existing: dict[int, Path] = {}
    for folder in parent.iterdir():
        match = FOLDER_RE.fullmatch(folder.name)
        if match is None or match.group("family") != family:
            continue
        if folder.is_symlink():
            raise baseline.BaselineError(f"migration family contains a symbolic link: {folder.name}")
        if not folder.is_dir():
            raise baseline.BaselineError(f"migration family entry is not a folder: {folder.name}")
        revision = int(match.group("revision"))
        if revision in existing:
            raise baseline.BaselineError(f"duplicate revision r{revision:03d} in migration family {family}")
        existing[revision] = folder
    if existing and set(existing) != set(range(1, max(existing) + 1)):
        raise baseline.BaselineError(f"revision history for {family} is incomplete; revisions must be consecutive from r001")

    state = []
    for revision, folder in sorted(existing.items()):
        relative = relative_to_root(folder, root).as_posix()
        lock = baseline.inspect_migration_lock(root, relative)
        if lock.status == "unknown":
            raise baseline.BaselineError(
                f"cannot determine whether existing migration is immutable: {relative} ({'; '.join(lock.states)})"
            )
        try:
            baseline.load_migration(root, relative)
            evidence = tuple(
                (path.name, hashlib.sha256(path.read_bytes()).hexdigest())
                for path in sorted(folder.iterdir(), key=lambda item: item.name)
                if path.name.startswith("status.") or path.name.startswith("attempt-event.")
            )
        except (OSError, ValueError) as error:
            raise baseline.BaselineError(f"existing migration evidence is invalid: {relative} ({error})") from error
        state.append((revision, relative, lock.status, lock.states, evidence))
    return existing, tuple(state)


def _write_migration_stage(stage: Path, files: Mapping[str, bytes]) -> None:
    """Write one generated payload into the private directory owned by this build."""
    for name, content in sorted(files.items()):
        if Path(name).name != name or name in {".", ".."}:
            raise baseline.BaselineError(f"generated migration file name is unsafe: {name!r}")
        target = stage / name
        with target.open("xb") as stream:
            stream.write(content)
        if os.name != "nt":
            os.chmod(target, 0o600)


def _windows_system_tool(name: str) -> Path:
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        raise baseline.BaselineError("Windows system directory is unavailable; cannot secure private baseline staging")
    tool = Path(system_root) / "System32" / name
    if not tool.is_file():
        raise baseline.BaselineError(f"Windows system tool {name} is unavailable; cannot secure private baseline staging")
    return tool


def _windows_current_user_sid() -> str:
    command = baseline._windows_system_tool("whoami.exe")
    try:
        result = subprocess.run(
            [str(command), "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise baseline.BaselineError("cannot identify the Windows account for private baseline staging") from error
    matches = re.findall(r"\bS-1-\d+(?:-\d+)+\b", result.stdout)
    if result.returncode != 0 or len(matches) != 1:
        raise baseline.BaselineError("cannot identify the Windows account for private baseline staging")
    return matches[0]


def _secure_windows_private_stage(stage: Path) -> None:
    """Protect a fresh stage before writing payloads; new files inherit its owner-only DACL."""
    sid = baseline._windows_current_user_sid()
    command = baseline._windows_system_tool("icacls.exe")
    try:
        result = subprocess.run(
            [str(command), str(stage), "/inheritancelevel:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise baseline.BaselineError("could not secure private baseline staging directory") from error
    if result.returncode != 0:
        raise baseline.BaselineError("could not secure private baseline staging directory")


def _secure_private_migration_stage(stage: Path) -> None:
    if os.name == "nt":
        _secure_windows_private_stage(stage)
        return
    try:
        stage.chmod(0o700)
    except OSError as error:
        raise baseline.BaselineError("could not secure private baseline staging directory") from error


def _build_migration_folder(root: Path, schema: str, family: str, files: Mapping[str, bytes]) -> Path | None:
    root = root.resolve()
    migrations_root = root / "migrations"
    if migrations_root.is_symlink():
        raise baseline.BaselineError("migrations directory cannot be a symbolic link")
    parent = root / "migrations" / schema
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise baseline.BaselineError(f"migration schema directory is unsafe: {parent}")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    existing, initial_state = _baseline_family_state(root, parent, family)
    if existing:
        latest = existing[max(existing)]
        relative = relative_to_root(latest, root).as_posix()
        lock = baseline.inspect_migration_lock(root, relative)
        if _folder_contents_match(latest, files):
            if lock.status == "unlocked":
                print(f"Unchanged migration; left byte-identical: {relative}")
            else:
                print(f"Skipped receipted or attempted migration without changing it: {relative} ({'; '.join(lock.states)})")
            return None
    revision = max(existing, default=0) + 1
    if revision > 999:
        raise baseline.BaselineError(f"migration revisions are exhausted for {schema}/{family}")
    folder = parent / f"{dt.date.today().isoformat()}_{family}-r{revision:03d}"
    if folder.exists() or folder.is_symlink():
        raise baseline.BaselineError(f"refusing to overwrite an existing migration folder: {relative_to_root(folder, root).as_posix()}")

    scratch_root = root / "scratch"
    if scratch_root.is_symlink() or (scratch_root.exists() and not scratch_root.is_dir()):
        raise baseline.BaselineError(f"baseline staging scratch directory is unsafe: {scratch_root}")
    scratch_root.mkdir(mode=0o700, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="baseline-stage-", dir=scratch_root))
    try:
        _secure_private_migration_stage(stage)
        relative = relative_to_root(folder, root).as_posix()
        baseline._write_migration_stage(stage, files)
        try:
            stage_device = stage.stat().st_dev
            destination_device = parent.stat().st_dev
        except OSError as error:
            raise baseline.BaselineError(f"cannot verify the migration staging filesystem: {error}") from error
        if stage_device != destination_device:
            raise baseline.BaselineError("baseline staging directory and migration destination must be on the same filesystem")
        baseline.validate_staged_migration(root, stage, relative)

        # A receipt, attempt event, or new revision may arrive while the payload
        # is being assembled. Refuse and let the next invocation recapture that
        # state instead of installing against stale immutability evidence.
        current, current_state = _baseline_family_state(root, parent, family)
        if current != existing or current_state != initial_state:
            raise baseline.BaselineError(f"migration evidence changed while staging {relative}; rerun after review")
        if folder.exists() or folder.is_symlink():
            raise baseline.BaselineError(f"refusing to overwrite an existing migration folder: {relative}")
        directory_install.install_directory_exclusive(stage, folder)
        try:
            baseline.load_migration(root, relative)
        except Exception as error:
            raise baseline.BaselineError(
                f"post-install validation failed; installed migration folder remains on disk: {relative} ({error})"
            ) from error
        print(f"Created migration: {relative}")
        return folder
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _build(args, config: Mapping, values: Mapping[str, str], scratch: Path) -> None:
    if args.source_environment == args.target_environment:
        raise baseline.BaselineError("baseline source and target environments must differ")
    bundle_name = getattr(args, "bundle", None)
    if bundle_name is not None:
        try:
            bundle_name = validate_bundle_name(bundle_name)
        except ValueError as error:
            raise baseline.BaselineError(str(error)) from error
    schemas = baseline._selected_schemas(config, values, args.schema)
    sections = (
        "tables", "columns", "constraints", "indexes", "sequences", "views", "stored-code",
        "triggers", "identity-columns", "object-grants", "baseline-source", "baseline-settings", "baseline-views",
    )
    compared = sections[:-3]
    reference_specs = baseline._reference_tables(config) if getattr(args, "data", False) else []
    if getattr(args, "data_dir", None) is not None and not getattr(args, "data", False):
        raise baseline.BaselineError("--data-dir requires --data")
    if getattr(args, "data", False) and not reference_specs:
        print("No referenceData.tables allow-list is configured; no baseline-data folder will be built")
    data_root = baseline._scratch_path(
        getattr(args, "data_dir", None),
        baseline.ROOT / "scratch" / "baseline" / args.source_environment / "data",
    ) if reference_specs else None
    created: list[str] = []
    try:
        for schema in schemas:
            try:
                source_target = resolve_target(values, args.source_environment, "read", schema=schema)
                target_target = resolve_target(values, args.target_environment, "read", schema=schema)
            except TargetResolutionError as error:
                raise baseline.BaselineError(str(error)) from error
            source = baseline.capture_environment_catalog(
                source_target, args.source_environment, sections,
                scratch / f"sqlcl-{args.source_environment}-{schema}", baseline_capture=True,
                baseline_prefixes=config.get("prefixes", []), baseline_excluded=config.get("excludedObjects", []),
            )
            target_sections = sections + (("baseline-data",) if reference_specs else ())
            target = baseline.capture_environment_catalog(
                target_target, args.target_environment, target_sections,
                scratch / f"sqlcl-{args.target_environment}-{schema}", baseline_capture=True,
                baseline_prefixes=config.get("prefixes", []), baseline_excluded=config.get("excludedObjects", []),
                baseline_data_tables=reference_specs,
            )
            baseline._complete(source, sections)
            baseline._complete(target, target_sections)
            _validate_baseline_scope(source, target)
            report = compare_environment_catalogs(source, target, sections=compared, prefixes=config.get("prefixes", []))
            if report.get("exit_code") == 2:
                raise baseline.BaselineError("compare-env returned incomplete evidence; no migration folder was generated")
            keys = _snapshot_keys(source, config, source_target.schema)
            if keys:
                _inventory, source_snapshot = baseline.capture_inventory_snapshot_with_retries(
                    source_target, keys, scratch / f"snapshot-{args.source_environment}-{schema}",
                )
            else:
                source_snapshot = SchemaSnapshot(source.get("identity", {}), {}, {}, {}, "", "")
            structure_steps, structure_checks = baseline._structure_bundle(config, source_target.schema, target_target.schema, source, target, source_snapshot)
            grants_steps, grants_checks = _grant_bundle(config, source_target.schema, target_target.schema, source, target)
            code_steps, code_checks = _code_bundle(config, source_target.schema, target_target.schema, source, target)
            granular_bundles = (
                ("baseline-structure", "Structure delta generated from complete source and target catalog observations.", structure_steps, structure_checks),
                ("baseline-grants", "Grouped additive object grants generated from complete source and target grant observations.", grants_steps, grants_checks),
                ("baseline-code", "Exact stored source, per-unit compiler settings, and name-based source checks.", code_steps, code_checks),
            )
            data_bundle = None
            data_differences: Sequence[str] = ()
            if reference_specs:
                assert data_root is not None
                source_data = _read_exported_reference_data(
                    data_root, reference_specs, args.source_environment, source_target.schema, source.get("identity", {}),
                )
                target_data = _reference_data_tables(target, reference_specs, args.target_environment, target_target.schema)
                data_bundle = _reference_data_bundle(
                    config, source_target.schema, target_target.schema, source, target, source_data, target_data,
                )
                data_steps, data_checks, data_differences = data_bundle
                if data_differences:
                    print(f"Reference-data target differences for {schema} (preconditions make them visible):")
                    for difference in data_differences:
                        print(f"  {difference}")

            bundles = []
            if bundle_name is None:
                bundles.extend(granular_bundles)
            else:
                components = tuple(
                    BaselineComponent(
                        kind=family.removeprefix("baseline-"),
                        steps=tuple(steps.items()),
                        preconditions=tuple(checks["preconditions"]),
                        postconditions=tuple(checks["postconditions"]),
                    )
                    for family, _note, steps, checks in granular_bundles
                )
                composed = combine_baseline_components(components)
                if composed.steps:
                    bundles.append((
                        f"baseline-bundle-{bundle_name}",
                        "Optional composition of generated structure, grants, and exact-source changes.",
                        composed.steps,
                        composed.checks,
                    ))

            if data_bundle is not None:
                data_steps, data_checks, _data_differences = data_bundle
                bundles.append((
                    "baseline-data",
                    "Natural-key-guarded reference-data inserts with foreign keys resolved from target labels; existing rows are never updated.",
                    data_steps,
                    data_checks,
                ))

            for family, note, steps, checks in bundles:
                if not steps:
                    print(f"No {family} changes for {schema}; no migration folder created")
                    continue
                if family.startswith("baseline-bundle-"):
                    readme = _bundle_readme(
                        bundle_name or "",
                        args.source_environment,
                        source_target.schema,
                        args.target_environment,
                        target_target.schema,
                        composed.provenance,
                        has_data=bool(data_bundle and data_bundle[0]),
                    )
                else:
                    readme = (
                        f"# {family.replace('-', ' ').title()}\n\n{note}\n\n"
                        f"Source: {args.source_environment} / {source_target.schema}. Target: {args.target_environment} / {target_target.schema}.\n"
                        "Review generated SQL and checks before applying this migration."
                    )
                    if family == "baseline-data":
                        readme += (
                            "\n\nUse the ordinary `migrate <folder> --env <env> --rehearse` command to run the DML in one transaction and roll it back. "
                            "The rehearsal skips DDL, including `START WITH LIMIT VALUE`; review the identity step separately.\n"
                        )
                        if data_differences:
                            readme += "\n## Target precondition differences\n\n" + "\n".join(f"- {item}" for item in data_differences) + "\n"
                installed = baseline._build_migration_folder(baseline.ROOT, target_target.schema, family, _migration_bytes(steps, checks, readme))
                if installed is not None:
                    created.append(relative_to_root(installed, baseline.ROOT.resolve()).as_posix())
    except Exception as error:
        if created:
            created_paths = ", ".join(created)
            raise baseline.BaselineError(
                f"{error}; previously created validated migration folders remain installed: {created_paths}"
            ) from error
        raise


# Imported last: this module reads baseline.<name> only when it runs, so baseline can bind the names from this module
# at its own end without a load-time cycle.
from . import baseline


# The leaf helpers moved to baseline_sql. They are re-exported because baseline re-exports them and the build
# modules and tests reference them on scripts.baseline_build.
from .baseline_sql import (  # noqa: F401
    _Q_DELIMITERS,
    _check,
    _q_literal,
    _safe_check_slug,
    _sql_identifier,
    _sql_literal,
)

# The reference-data group moved to baseline_reference_data. The names are re-exported because _build calls them
# and baseline, tests and other scripts reference them on scripts.baseline_build and scripts.baseline.
from .baseline_reference_data import (  # noqa: F401
    DATA_STEP_BYTE_LIMIT,
    DATA_STEP_STATEMENT_LIMIT,
    DATA_TEXT_CHUNK_CHARS,
    _ISO_DATE_RE,
    _ISO_TIMESTAMP_RE,
    _REFERENCE_TIMESTAMP_TYPE_RE,
    _check_sql_value,
    _data_tuple,
    _read_exported_reference_data,
    _reference_count_check,
    _reference_data_bundle,
    _reference_data_tables,
    _reference_temporal_type,
    _text_sql_literal,
    format_sql_value,
)

# The bundle generators moved to baseline_bundles. The names are re-exported because _build calls them and baseline,
# tests and other scripts reference them on scripts.baseline_build and scripts.baseline.
from .baseline_bundles import (  # noqa: F401
    _PLSQL_SOURCE_CHUNK_CHARS,
    _bundle_readme,
    _clob_ddl_step,
    _clob_source_step,
    _code_bundle,
    _column_definition_check,
    _column_sql,
    _columns_signature,
    _compile_all_step,
    _condition_key,
    _constraint_ddl,
    _count_check,
    _ddl_without_terminator,
    _direct_source_step,
    _exact_settings_check,
    _exact_source_step,
    _grant_bundle,
    _metadata_integer,
    _migration_bytes,
    _needs_clob_assembly,
    _replace_create_header,
    _session_settings_sql,
    _setting_row,
    _snapshot_keys,
    _source_check_queries,
    _source_line_predicate,
    _structure_bundle,
    _sync_identity_sql,
    _sync_sequence_sql,
    _table_column_type,
    _view_validity_check,
    _wrap_constraint,
    _wrap_guarded_ddl,
    _wrap_not_null,
)
