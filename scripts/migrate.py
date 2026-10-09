#!/usr/bin/env python3
"""Apply selected immutable migration folders with live checks and local receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable, Mapping, Sequence

from .db_targets import (
    Target,
    TargetResolutionError,
    batch_schema,
    flat_migrations_apply,
    looks_like_production_identity,
    resolve_target,
)
from .migration_checks import (
    CheckReport,
    PreflightReport,
    analyze_batch,
    batch_preconditions,
    compiled_units,
    preflight,
    run_checks,
)
from .migration_manifest import (
    Migration,
    MigrationFile,
    MigrationManifestError,
    assert_single_layout,
    decode_json,
    install_receipt,
    load_batch,
    load_migration,
    validate_receipt,
)
from .schema_catalog import (
    CatalogError,
    ObjectDefinition,
    ObjectKey,
    SchemaInventory,
    SchemaSnapshot,
    capture_inventory,
    capture_snapshot,
)
from .schema_normalization import normalization_coverage
from .sqlcl_session import SqlclError, run_sqlcl, safe_rmtree


ROOT = Path(__file__).resolve().parents[1]
VERIFIER_VERSION = "template-migration-v1"
STATUS_SCHEMA_VERSION = 1
RUN_MANIFEST_NAME = "run-manifest.json"
RUN_ID_RE = re.compile(r"migration-attempt-[A-Za-z0-9_-]+\Z", re.ASCII)
IDENTITY_FIELDS = (
    "session_user", "current_schema", "db_name", "db_unique_name", "service_name",
    "container_id", "container_name", "edition", "database_version",
)


class MigrationApplyError(RuntimeError):
    """Apply or fresh verification could not complete unambiguously."""


@dataclass(frozen=True)
class _FrozenMigration:
    original: Migration
    staged: Migration


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_scratch_root(repo_root: Path) -> Path:
    scratch = repo_root / "scratch"
    if scratch.is_symlink():
        raise MigrationApplyError("scratch must not be a symbolic link")
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        scratch.chmod(0o700)
    except OSError as error:
        raise MigrationApplyError(f"could not secure scratch directory: {error}") from error
    return scratch


def _new_run_dir(repo_root: Path) -> Path:
    scratch = _safe_scratch_root(repo_root)
    run_dir = Path(tempfile.mkdtemp(prefix="migration-attempt-", dir=scratch))
    try:
        run_dir.chmod(0o700)
    except OSError as error:
        safe_rmtree(run_dir)
        raise MigrationApplyError(f"could not secure migration run directory: {error}") from error
    return run_dir


def _atomic_write_json(path: Path, value: Mapping) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _canonical_payload_digest(files: Sequence[MigrationFile], checks_source: bytes) -> tuple[str, str]:
    checks_sha256 = hashlib.sha256(checks_source).hexdigest()
    payload = {
        "files": [{"name": file.name, "sequence": file.sequence, "sha256": file.sha256} for file in files],
        "checksSha256": checks_sha256,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest(), checks_sha256


def _verify_staged_payload(migration: Migration) -> str:
    observed_files: list[MigrationFile] = []
    for file in migration.files:
        try:
            source = file.path.read_bytes()
        except OSError as error:
            raise MigrationApplyError(f"frozen migration file is unavailable: {file.name}") from error
        digest = hashlib.sha256(source).hexdigest()
        if source != file.source or digest != file.sha256:
            raise MigrationApplyError(f"frozen migration file changed: {file.name}")
        observed_files.append(replace(file, source=source, sha256=digest))
    checks_path = migration.folder / "checks.json"
    try:
        checks_source = checks_path.read_bytes()
    except OSError as error:
        raise MigrationApplyError("frozen checks.json is unavailable") from error
    if checks_source != migration.checks_source:
        raise MigrationApplyError("frozen checks.json changed")
    digest, _ = _canonical_payload_digest(observed_files, checks_source)
    if digest != migration.payload_digest:
        raise MigrationApplyError("frozen payload digest changed")
    return digest


def _freeze_batch(migrations: Sequence[Migration], run_dir: Path) -> tuple[_FrozenMigration, ...]:
    payload_root = run_dir / "payload"
    payload_root.mkdir(mode=0o700)
    frozen: list[_FrozenMigration] = []
    records: list[dict] = []
    for migration in migrations:
        staged_folder = payload_root / migration.folder.name
        staged_folder.mkdir(mode=0o700)
        staged_files: list[MigrationFile] = []
        for source_file in migration.files:
            staged_path = staged_folder / source_file.name
            staged_path.write_bytes(source_file.source)
            staged_path.chmod(0o600)
            staged_source = staged_path.read_bytes()
            staged_sha = hashlib.sha256(staged_source).hexdigest()
            if staged_source != source_file.source or staged_sha != source_file.sha256:
                raise MigrationApplyError(f"staged bytes do not match validated migration file {source_file.name}")
            staged_files.append(replace(source_file, path=staged_path, source=staged_source, sha256=staged_sha))
        checks_path = staged_folder / "checks.json"
        checks_path.write_bytes(migration.checks_source)
        checks_path.chmod(0o600)
        digest, checks_sha = _canonical_payload_digest(staged_files, checks_path.read_bytes())
        if digest != migration.payload_digest or checks_sha != migration.checks_sha256:
            raise MigrationApplyError(f"staged payload digest changed for {migration.folder.name}")
        staged_migration = replace(
            migration,
            folder=staged_folder,
            files=tuple(staged_files),
            checks_source=checks_path.read_bytes(),
            checks_sha256=checks_sha,
            payload_digest=digest,
        )
        frozen.append(_FrozenMigration(migration, staged_migration))
        records.append({
            "folder": migration.folder.name,
            "payloadDigest": digest,
            "files": [{"name": file.name, "sequence": file.sequence, "sha256": file.sha256} for file in staged_files],
            "checksSha256": checks_sha,
            "writeAttempted": False,
            "state": "frozen",
        })
    return tuple(frozen)


def _identity_for_receipt(inventory: SchemaInventory | SchemaSnapshot, target: Target) -> dict:
    identity = inventory.identity
    required = IDENTITY_FIELDS
    missing = [field for field in required if not str(identity.get(field, "")).strip()]
    if missing:
        raise MigrationApplyError("observed migration target identity is incomplete: " + ", ".join(missing))
    if str(identity["session_user"]).upper() != target.expected_user or str(identity["current_schema"]).upper() != target.schema:
        raise MigrationApplyError("observed target identity does not match its configured user and schema")
    if target.environment != "prod" and looks_like_production_identity(identity["db_name"], identity["db_unique_name"], identity["service_name"]):
        raise MigrationApplyError("observed database/service identity resembles production but the selected environment is not prod")
    return {
        "environment": target.environment,
        "connection": target.connection,
        "expected_user": target.expected_user,
        **{field: str(identity[field]) for field in required},
    }


def _identity_summary(identity: Mapping[str, str]) -> dict:
    fields = (
        "environment", "connection", "expected_user", "session_user", "current_schema", "db_name",
        "db_unique_name", "service_name", "container_id", "container_name", "edition", "database_version",
    )
    return {field: identity[field] for field in fields}


def _assert_same_target(expected: Mapping[str, str], observed: Mapping[str, str]) -> None:
    required = IDENTITY_FIELDS
    if any(not str(expected.get(field, "")).strip() or not str(observed.get(field, "")).strip() for field in required):
        raise MigrationApplyError("target identity became incomplete during migration; result is unknown")
    if any(expected[field] != observed[field] for field in required):
        raise MigrationApplyError("migration connection resolved to a different database/schema identity")


def _selected_keys(operations: Sequence[Mapping]) -> tuple[tuple[str, str], ...]:
    keys: set[tuple[str, str]] = set()
    for operation in operations:
        kind = operation.get("kind")
        if kind in {"CREATE_TABLE", "CREATE_VIEW", "CREATE_SEQUENCE", "CREATE_INDEX"}:
            object_type = operation.get("object_type")
            name = operation.get("name")
            if isinstance(name, str) and isinstance(object_type, str):
                keys.add((name, object_type))
        if kind in {"ALTER_ADD_COLUMN", "CREATE_INDEX"}:
            table = operation.get("table")
            if isinstance(table, str):
                keys.add((table, "TABLE"))
    return tuple(sorted(keys))


def _snapshot_for(
    target: Target,
    inventory: SchemaInventory,
    keys: Sequence[tuple[str, str]],
    run_dir: Path,
    capture_snapshot_fn: Callable,
) -> SchemaSnapshot:
    if keys:
        return capture_snapshot_fn(target, inventory, keys, run_dir)
    return SchemaSnapshot(inventory.identity, inventory.objects, {}, inventory.coverage, inventory.started_at, inventory.completed_at)


def _explicit_checks(migrations: Sequence[Migration], phase: str) -> tuple:
    if phase == "preconditions":
        # Later folders' preconditions run at their own apply boundary, where
        # the folders before them have already been applied.
        return batch_preconditions(migrations)
    return tuple(check for migration in migrations for check in migration.postconditions)


def _check_report(target: Target, migrations: Sequence[Migration], phase: str, run_dir: Path, run_checks_fn: Callable) -> CheckReport:
    return run_checks_fn(target, _explicit_checks(migrations, phase), run_dir, phase=phase)


def _assert_check_identity(expected: Mapping[str, str], report: CheckReport) -> None:
    """Refuse check results that were observed on a different database."""
    if not report.results:
        return
    observed = report.coverage.get("identity")
    if not isinstance(observed, Mapping):
        raise MigrationApplyError(f"{report.coverage.get('phase', 'check')} session did not report its target identity")
    _assert_same_target(expected, observed)


def _verify_receipts_and_attempts(
    repo_root: Path,
    migrations: Sequence[Migration],
    target_identity: Mapping[str, str],
) -> None:
    environment = str(target_identity["environment"])
    for migration in migrations:
        path = migration.folder / f"status.{environment}.json"
        if path.is_symlink():
            raise MigrationApplyError(f"receipt path is a symbolic link and requires reconciliation: {path}")
        if path.exists():
            try:
                validate_receipt(path, migration, target_identity)
            except MigrationManifestError as error:
                raise MigrationApplyError(f"existing receipt is malformed or does not match; reconcile before applying {migration.folder.name}: {error}") from error
            raise MigrationApplyError(f"{migration.folder.name} already has a verified {environment} receipt; run read-only checks instead of reapplying")

    scratch = repo_root / "scratch"
    if not scratch.exists():
        return
    if scratch.is_symlink():
        raise MigrationApplyError("scratch must not be a symbolic link")
    selected = {migration.folder.name: migration for migration in migrations}
    for run_dir in scratch.glob("migration-attempt-*"):
        if run_dir.is_symlink() or not run_dir.is_dir() or RUN_ID_RE.fullmatch(run_dir.name) is None:
            continue
        manifest_path = run_dir / RUN_MANIFEST_NAME
        if not manifest_path.exists():
            continue
        if manifest_path.is_symlink():
            raise MigrationApplyError(f"retained migration evidence is unsafe and needs reconciliation: {manifest_path}")
        try:
            # Strict, like a receipt: a duplicate key would let the last one win and
            # turn "writeAttempted": true into false.
            manifest = decode_json(manifest_path.read_bytes(), str(manifest_path))
        except (OSError, MigrationManifestError) as error:
            raise MigrationApplyError(f"retained migration evidence cannot be read; reconcile before applying: {manifest_path} ({error})") from error
        if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1 or not isinstance(manifest.get("migrations"), list):
            raise MigrationApplyError(f"retained migration evidence is malformed; reconcile before applying: {manifest_path}")
        target_record = manifest.get("target")
        if not isinstance(target_record, dict):
            raise MigrationApplyError(f"retained migration target evidence is malformed; reconcile before applying: {manifest_path}")
        # migrations/<SCHEMA>/ may reuse a folder name per schema. An attempt
        # against another schema is not an attempt at this folder.
        recorded_schema = target_record.get("current_schema")
        if recorded_schema is not None and recorded_schema != target_identity.get("current_schema"):
            continue
        for record in manifest["migrations"]:
            if not isinstance(record, dict) or record.get("folder") not in selected:
                continue
            if target_record.get("environment") != environment or not record.get("writeAttempted"):
                continue
            if record.get("payloadDigest") != selected[record["folder"]].payload_digest:
                raise MigrationApplyError(f"a prior write attempt has different bytes for {record['folder']}; reconcile before applying")
            raise MigrationApplyError(f"a prior {environment} write attempt for {record['folder']} has no current verified receipt; inspect {run_dir} and reconcile")


def _migration_operations(migration: Migration, target_schema: str) -> tuple[dict, ...]:
    return analyze_batch((migration,), target_schema)


def _verify_structural_postconditions(
    migration: Migration,
    snapshot: SchemaSnapshot,
    target_schema: str,
) -> tuple[tuple[dict, ...], tuple[dict, ...]]:
    results: list[dict] = []
    errors: list[dict] = []
    inventory = snapshot.inventory
    for operation in _migration_operations(migration, target_schema):
        if not operation.get("supported"):
            continue
        kind = operation.get("kind")
        owner = target_schema
        name = operation.get("name")
        object_type = operation.get("object_type")
        key = ObjectKey(owner, name, object_type) if isinstance(name, str) and isinstance(object_type, str) else None
        found = key is not None and key in inventory
        details: dict = {"operation": kind, "name": name, "object_type": object_type}
        if found:
            row = inventory[key]
            if row.get("status") not in {None, "VALID"}:
                found = False
                details["status"] = row.get("status")
        if kind == "CREATE_TABLE":
            definition = snapshot.objects.get(key) if key else None
            expected_columns = set(operation.get("columns", ()))
            actual_columns = _definition_column_names(definition)
            if definition is None or actual_columns is None:
                found = False
                details["reason"] = "complete table column metadata is missing"
            elif not expected_columns.issubset(actual_columns):
                found = False
                details["missing_columns"] = sorted(expected_columns - actual_columns)
        elif kind == "ALTER_ADD_COLUMN":
            table_key = ObjectKey(owner, operation["table"], "TABLE")
            definition = snapshot.objects.get(table_key)
            actual_columns = _definition_column_names(definition)
            expected_columns = set(operation.get("columns", ()))
            found = definition is not None and actual_columns is not None and expected_columns.issubset(actual_columns)
            if not found:
                found = False
                details["reason"] = "added column is missing from fresh table metadata"
                if actual_columns is not None:
                    details["missing_columns"] = sorted(expected_columns - actual_columns)
        elif kind in {"CREATE_VIEW", "CREATE_SEQUENCE", "CREATE_INDEX"}:
            definition = snapshot.objects.get(key) if key else None
            if definition is None:
                found = False
                details["reason"] = "fresh selected definition is missing"
            elif not definition.valid:
                found = False
                details["reason"] = "object is invalid"
        passed = bool(found)
        result = {
            "id": _catalog_check_id(operation),
            "kind": "catalog",
            "passed": passed,
            "rows": 1 if passed else 0,
            "value": 1 if passed else 0,
            "observed": details,
        }
        results.append(result)
        if not passed:
            errors.append({"code": "CATALOG_POSTCONDITION_FAILED", **details, "migration": migration.folder.name})
    return tuple(results), tuple(errors)


def _definition_column_names(definition: ObjectDefinition | None) -> set[str] | None:
    if definition is None:
        return None
    columns = definition.attributes.get("columns")
    if not isinstance(columns, list):
        return None
    names: set[str] = set()
    for column in columns:
        if not isinstance(column, Mapping) or not isinstance(column.get("name"), str):
            return None
        names.add(column["name"])
    return names


def _catalog_check_id(operation: Mapping) -> str:
    kind = re.sub(r"[^a-z0-9]+", "-", str(operation.get("kind", "effect")).lower()).strip("-")
    name = re.sub(r"[^a-z0-9]+", "-", str(operation.get("name") or operation.get("table") or "object").lower()).strip("-")
    return f"catalog-{int(operation.get('sequence', 1)):03d}-{int(operation.get('statement_index', 1)):03d}-{kind}-{name}"[:100].rstrip("-")


def _apply_identity_from_output(output: str) -> dict:
    lines = output.splitlines()
    try:
        start = lines.index("MIGRATION_IDENTITY_BEGIN")
        end = lines.index("MIGRATION_IDENTITY_END", start + 1)
    except ValueError as error:
        raise MigrationApplyError("apply session did not return its verified target identity") from error
    if "MIGRATION_IDENTITY_VERIFIED" not in lines[end + 1 :]:
        raise MigrationApplyError("apply target identity frame is missing its verification sentinel")
    try:
        identity = json.loads("".join(lines[start + 1 : end]))
    except json.JSONDecodeError as error:
        raise MigrationApplyError("apply target identity JSON is malformed") from error
    required = IDENTITY_FIELDS
    if not isinstance(identity, dict) or any(not isinstance(identity.get(key), str) or not identity[key] for key in required):
        raise MigrationApplyError("apply target identity is incomplete")
    return identity


IDENTITY_SQL_EXPRESSIONS = {
    "session_user": "SYS_CONTEXT('USERENV', 'SESSION_USER')",
    "current_schema": "SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')",
    "db_name": "SYS_CONTEXT('USERENV', 'DB_NAME')",
    "db_unique_name": "SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME')",
    "service_name": "NVL(SYS_CONTEXT('USERENV', 'SERVICE_NAME'), '<NO_SERVICE>')",
    "container_id": "NVL(SYS_CONTEXT('USERENV', 'CON_ID'), '0')",
    "container_name": "NVL(SYS_CONTEXT('USERENV', 'CON_NAME'), 'NON-CDB')",
    "edition": "NVL(SYS_CONTEXT('USERENV', 'CURRENT_EDITION_NAME'), '<NONEDITIONED>')",
    "database_version": "TO_CHAR(DBMS_DB_VERSION.VERSION) || '.' || TO_CHAR(DBMS_DB_VERSION.RELEASE)",
}
IDENTITY_VALUE_RE = re.compile(r"[^\x00-\x1f'&]{1,512}\Z")


def _identity_guard_lines(expected_identity: Mapping[str, str]) -> list[str]:
    """Refuse, inside the apply session and before any payload, a different target."""
    lines = ["DECLARE", "  l_mismatch VARCHAR2(4000);", "BEGIN"]
    for field in IDENTITY_FIELDS:
        value = expected_identity.get(field)
        if not isinstance(value, str) or IDENTITY_VALUE_RE.fullmatch(value) is None:
            raise MigrationApplyError(f"preflight identity field {field} cannot be enforced in the apply session")
        lines.append(
            f"  IF NVL({IDENTITY_SQL_EXPRESSIONS[field]}, '<NULL>') != '{value}' THEN "
            f"l_mismatch := l_mismatch || ' {field}'; END IF;"
        )
    lines.extend([
        "  IF l_mismatch IS NOT NULL THEN",
        "    RAISE_APPLICATION_ERROR(-20987, 'Migration apply session differs from the preflight target:' || l_mismatch);",
        "  END IF;",
        "END;",
        "/",
    ])
    return lines


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _compile_guard_lines(units: Sequence[tuple[str, str, str, bool]]) -> list[str]:
    """Fail the apply when a unit this migration compiled is left with errors.

    SQLcl reports a PL/SQL or view compilation error as a warning and keeps
    going; WHENEVER SQLERROR does not fire. ALL_ERRORS is the reliable signal,
    checked only for the units the migration's own CREATE and ALTER ... COMPILE
    statements name, so a teammate's unrelated DDL cannot fail this apply. A
    required unit this session cannot see would pass ALL_ERRORS unseen, so it
    fails the apply too.
    """
    if not units:
        return []
    lines = [
        "DECLARE",
        "  l_failed VARCHAR2(4000);",
        "  PROCEDURE check_unit(p_owner VARCHAR2, p_type VARCHAR2, p_name VARCHAR2, p_required BOOLEAN) IS",
        "    l_count PLS_INTEGER;",
        "  BEGIN",
        "    SELECT COUNT(*) INTO l_count FROM all_objects",
        "    WHERE owner = p_owner AND object_type = p_type AND object_name = p_name;",
        "    IF l_count = 0 THEN",
        "      IF p_required THEN",
        "        l_failed := SUBSTR(l_failed || ' ' || p_type || ' ' || p_owner || '.' || p_name || ' (not found or not visible);', 1, 3000);",
        "      END IF;",
        "      RETURN;",
        "    END IF;",
        "    SELECT COUNT(*) INTO l_count FROM all_errors",
        "    WHERE owner = p_owner AND type = p_type AND name = p_name AND attribute = 'ERROR';",
        "    IF l_count > 0 THEN",
        "      l_failed := SUBSTR(l_failed || ' ' || p_type || ' ' || p_owner || '.' || p_name || ';', 1, 3000);",
        "    END IF;",
        "  END;",
        "BEGIN",
    ]
    for owner, object_type, name, required in units:
        lines.append(
            f"  check_unit({_sql_literal(owner)}, {_sql_literal(object_type)}, {_sql_literal(name)}, "
            f"{'TRUE' if required else 'FALSE'});"
        )
    lines.extend([
        "  IF l_failed IS NOT NULL THEN",
        "    RAISE_APPLICATION_ERROR(-20986, 'Migration left objects with compilation errors or that it cannot see:' || l_failed);",
        "  END IF;",
        "END;",
        "/",
    ])
    return lines


def apply_folder(
    migration: Migration,
    target: Target,
    run_dir: Path,
    *,
    expected_identity: Mapping[str, str] | None = None,
) -> dict:
    """Apply the exact staged SQL files in one SQLcl session and commit on clean exit."""
    run_dir = Path(run_dir)
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if run_dir.is_symlink():
        raise MigrationApplyError("migration SQLcl run directory cannot be a symbolic link")
    run_dir.chmod(0o700)
    script_root = Path(__file__).resolve().parent
    for trusted in ("migrate.sql", "verify_migration_access.sql"):
        shutil.copyfile(script_root / trusted, run_dir / trusted)
        (run_dir / trusted).chmod(0o600)
    for file in migration.files:
        try:
            source = file.path.read_bytes()
        except OSError as error:
            raise MigrationApplyError(f"staged migration file cannot be read: {file.name}") from error
        if source != file.source or hashlib.sha256(source).hexdigest() != file.sha256:
            raise MigrationApplyError(f"frozen migration file changed before apply: {file.name}")

    driver_lines = [
        "SET ECHO OFF",
        # SQLcl ends a plain SQL statement at a blank line unless told otherwise,
        # so an UPDATE with a blank line before its WHERE would run on every row.
        "SET SQLBLANKLINES ON",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
        "SET DEFINE ON",
        f"@@migrate.sql {target.schema} {target.environment} {target.expected_user}",
        "SET DEFINE OFF",
    ]
    if expected_identity is not None:
        driver_lines.extend(_identity_guard_lines(expected_identity))
    for file in migration.files:
        driver_lines.append(f"@@../payload/{migration.folder.name}/{file.name}")
    # The payload may have changed these settings (for example WHENEVER
    # SQLERROR CONTINUE); the compile guard's error must still stop the commit.
    driver_lines.extend((
        "SET DEFINE OFF",
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
        *_compile_guard_lines(compiled_units(migration, target.schema)),
    ))
    driver_lines.extend(("PROMPT MIGRATION_APPLY_COMPLETED", "EXIT SUCCESS COMMIT", ""))
    driver = run_dir / "migration-driver.sql"
    driver.write_text("\n".join(driver_lines), encoding="utf-8", newline="\n")
    driver.chmod(0o600)
    started = _utc_now()
    try:
        result = run_sqlcl(target, driver, run_dir)
    except SqlclError as error:
        raise MigrationApplyError(f"SQLcl apply failed for {migration.folder.name}; inspect {error.run_dir}") from error
    output = result.output
    if "MIGRATION_APPLY_COMPLETED" not in {line.strip() for line in output.splitlines()}:
        raise MigrationApplyError(f"SQLcl did not confirm all files completed for {migration.folder.name}; inspect {run_dir}")
    identity = _apply_identity_from_output(output)
    completed = _utc_now()
    return {
        "committed": True,
        "applyStartedAt": started,
        "applyCompletedAt": completed,
        "identity": identity,
        "run_dir": str(run_dir),
        "files": [{"name": file.name, "sequence": file.sequence, "sha256": file.sha256} for file in migration.files],
        "payloadDigest": migration.payload_digest,
    }


def build_receipt(
    migration: Migration,
    target_identity: Mapping[str, str],
    apply_evidence: Mapping,
    verification_snapshot: SchemaSnapshot,
    checks: CheckReport,
    catalog_checks: Sequence[Mapping],
) -> dict:
    """Build a success receipt only after committed apply and fresh verification."""
    if apply_evidence.get("committed") is not True or apply_evidence.get("payloadDigest") != migration.payload_digest:
        raise MigrationApplyError("apply evidence does not prove this frozen payload committed")
    if not checks.complete or not checks.passed:
        raise MigrationApplyError("fresh postcondition checks did not pass")
    if verification_snapshot.coverage.get("ownerComplete") is not True:
        raise MigrationApplyError("fresh schema verification has incomplete owner visibility")
    apply_identity = apply_evidence.get("identity")
    if not isinstance(apply_identity, Mapping):
        raise MigrationApplyError("apply session identity is missing")
    _assert_same_target(target_identity, apply_identity)
    _assert_same_target(target_identity, verification_snapshot.identity)
    files = [{"name": file.name, "sequence": file.sequence, "sha256": file.sha256} for file in migration.files]
    observed_checks = [
        {"id": result["id"], "passed": True, "rows": result.get("row_count", 1), "value": result.get("value", 1)}
        for result in checks.results
    ]
    observed_checks.extend(dict(result) for result in catalog_checks)
    if len({result.get("id") for result in observed_checks}) != len(observed_checks):
        raise MigrationApplyError("generated and authored verification check IDs are not unique")
    if any(result.get("passed") is not True for result in observed_checks):
        raise MigrationApplyError("one or more recorded migration verifications did not pass")
    return {
        "schemaVersion": STATUS_SCHEMA_VERSION,
        "state": "verified",
        "environment": target_identity["environment"],
        "migration": migration.folder.name,
        "createdDate": migration.date,
        "family": migration.family,
        "revision": migration.revision,
        "files": files,
        "checksSha256": migration.checks_sha256,
        "payloadDigest": migration.payload_digest,
        "target": dict(target_identity),
        "applyStartedAt": apply_evidence["applyStartedAt"],
        "applyCompletedAt": apply_evidence["applyCompletedAt"],
        "verifiedAt": _utc_now(),
        "checks": observed_checks,
        "verifier": VERIFIER_VERSION,
        "normalization": normalization_coverage()["version"],
    }


def _same_target_identity(expected: Mapping[str, str], observed: Mapping[str, str]) -> None:
    _assert_same_target(expected, observed)


def _preflight_snapshot(
    migrations: Sequence[Migration],
    target: Target,
    run_dir: Path,
    capture_inventory_fn: Callable,
    capture_snapshot_fn: Callable,
    run_checks_fn: Callable,
) -> tuple[SchemaInventory, SchemaSnapshot, CheckReport, PreflightReport]:
    operations = analyze_batch(migrations, target.schema)
    inventory = capture_inventory_fn(target, run_dir)
    keys = _selected_keys(operations)
    snapshot = _snapshot_for(target, inventory, keys, run_dir, capture_snapshot_fn)
    checks = _check_report(target, migrations, "preconditions", run_dir / "preconditions", run_checks_fn)
    report = preflight(migrations, snapshot, checks)
    return inventory, snapshot, checks, report


def _print_target_summary(target: Target, identity: Mapping[str, str], frozen: Sequence[_FrozenMigration]) -> None:
    print(f"Target {target.environment.upper()}: connection={target.connection} expected_user={target.expected_user} schema={target.schema}")
    print(
        "Observed: "
        f"session_user={identity.get('session_user')} schema={identity.get('current_schema')} "
        f"db_name={identity.get('db_name')} db_unique_name={identity.get('db_unique_name')} "
        f"service={identity.get('service_name')} container={identity.get('container_name')}/{identity.get('container_id')} "
        f"edition={identity.get('edition')}"
    )
    for item in frozen:
        migration = item.original
        print(f"Migration {migration.folder.name} payload={migration.payload_digest}")
        for file in migration.files:
            print(f"  {file.sequence:03d} {file.name} sha256={file.sha256}")


def _update_run_manifest(
    path: Path,
    target_identity: Mapping[str, str],
    frozen: Sequence[_FrozenMigration],
    records: list[dict],
    *,
    state: str,
) -> dict:
    manifest = {
        "schemaVersion": 1,
        "state": state,
        "createdAt": records[0].get("createdAt") if records else _utc_now(),
        "target": dict(target_identity),
        "migrations": records,
    }
    _atomic_write_json(path, manifest)
    return manifest


def _receipt_record(migration: Migration, run_manifest: dict) -> dict:
    return next(item for item in run_manifest["migrations"] if item["folder"] == migration.folder.name)


def apply_batch(
    repo_root: Path,
    migrations: Sequence[Migration],
    target: Target,
    confirm: Callable[[str], bool],
    *,
    capture_inventory_fn: Callable = capture_inventory,
    capture_snapshot_fn: Callable = capture_snapshot,
    run_checks_fn: Callable = run_checks,
    apply_folder_fn: Callable = apply_folder,
    receipt_installer: Callable = install_receipt,
) -> int:
    """Freeze, preflight, confirm when needed, apply in input order, and verify."""
    repo_root = Path(repo_root).resolve()
    if not migrations:
        print("migration error: select one or more folders in execution order", file=sys.stderr)
        return 2
    run_dir: Path | None = None
    attempted = False
    manifest_path: Path | None = None
    records: list[dict] = []
    run_manifest: dict | None = None
    frozen: tuple[_FrozenMigration, ...] = ()
    try:
        run_dir = _new_run_dir(repo_root)
        frozen = _freeze_batch(migrations, run_dir)
        records = [
            {
                "folder": item.original.folder.name,
                "payloadDigest": item.staged.payload_digest,
                "files": [{"name": file.name, "sequence": file.sequence, "sha256": file.sha256} for file in item.staged.files],
                "checksSha256": item.staged.checks_sha256,
                "writeAttempted": False,
                "state": "frozen",
                "createdAt": _utc_now(),
            }
            for item in frozen
        ]
        initial_inventory, initial_snapshot, initial_checks, initial_preflight = _preflight_snapshot(
            migrations, target, run_dir / "initial", capture_inventory_fn, capture_snapshot_fn, run_checks_fn,
        )
        initial_target_identity = _identity_for_receipt(initial_inventory, target)
        _assert_check_identity(initial_target_identity, initial_checks)
        _verify_receipts_and_attempts(repo_root, migrations, initial_target_identity)
        if initial_preflight.exit_code != 0:
            print(json.dumps(initial_preflight.to_dict(), ensure_ascii=False, sort_keys=True, indent=2), file=sys.stderr)
            return initial_preflight.exit_code
        run_manifest = {
            "schemaVersion": 1,
            "state": "preflight-passed",
            "createdAt": _utc_now(),
            "target": dict(initial_target_identity),
            "migrations": records,
        }
        manifest_path = run_dir / RUN_MANIFEST_NAME
        _atomic_write_json(manifest_path, run_manifest)
        if target.environment in {"staging", "prod"}:
            _print_target_summary(target, initial_target_identity, frozen)
            prompt = f"Migrating to {target.environment.upper()}. Proceed? [y/N]"
            try:
                approved = bool(confirm(prompt))
            except (EOFError, OSError):
                approved = False
            if not approved:
                print(f"Migration to {target.environment.upper()} declined; no writes were attempted.")
                return 1

        # Re-read original bytes after review and before any write.
        for item in frozen:
            relative = item.original.folder.relative_to(repo_root).as_posix()
            current = load_migration(repo_root, relative)
            if current.payload_digest != item.original.payload_digest:
                raise MigrationApplyError(f"source changed after preflight: {item.original.folder.name}")
            _verify_staged_payload(item.staged)

        for folder_index, item in enumerate(frozen, start=1):
            original = item.original
            staged = item.staged
            # Every folder boundary gets a fresh identity, complete inventory,
            # generated catalog preflight, and its explicit preconditions.
            current_inventory, current_snapshot, current_checks, current_preflight = _preflight_snapshot(
                (original,), target, run_dir / f"boundary-{folder_index:03d}",
                capture_inventory_fn, capture_snapshot_fn, run_checks_fn,
            )
            current_identity = _identity_for_receipt(current_inventory, target)
            _assert_same_target(initial_target_identity, current_identity)
            _assert_check_identity(initial_target_identity, current_checks)
            if current_preflight.exit_code != 0:
                raise MigrationApplyError(
                    f"live preflight refused {original.folder.name} at its apply boundary: "
                    + json.dumps(current_preflight.to_dict(), ensure_ascii=False, sort_keys=True)
                )
            record = _receipt_record(original, {"migrations": records})
            record["writeAttempted"] = True
            record["state"] = "write-attempted"
            record["writeStartedAt"] = _utc_now()
            attempted = True
            run_manifest = {
                "schemaVersion": 1,
                "state": "write-attempted",
                "createdAt": run_manifest["createdAt"],
                "target": dict(initial_target_identity),
                "migrations": records,
            }
            _atomic_write_json(manifest_path, run_manifest)

            folder_run_dir = run_dir / f"apply-{folder_index:03d}"
            try:
                apply_evidence = apply_folder_fn(staged, target, folder_run_dir, expected_identity=initial_target_identity)
            except KeyboardInterrupt:
                # SQLcl may have finished or stopped part-way: the outcome is unknown.
                record["state"] = "apply-failed-or-unknown"
                record["error"] = "interrupted"
                run_manifest["state"] = "apply-failed-or-unknown"
                _atomic_write_json(manifest_path, run_manifest)
                raise MigrationApplyError(f"{original.folder.name} was interrupted and may be partially applied; stop and reconcile. Evidence: {run_dir}") from None
            except (MigrationApplyError, OSError, RuntimeError) as error:
                record["state"] = "apply-failed-or-unknown"
                record["error"] = str(error)
                run_manifest["state"] = "apply-failed-or-unknown"
                _atomic_write_json(manifest_path, run_manifest)
                raise MigrationApplyError(f"{original.folder.name} may be partially applied; stop and reconcile. Evidence: {run_dir}") from error
            if apply_evidence.get("committed") is not True:
                record["state"] = "apply-failed-or-unknown"
                run_manifest["state"] = record["state"]
                _atomic_write_json(manifest_path, run_manifest)
                raise MigrationApplyError(f"{original.folder.name} did not provide commit evidence; stop and reconcile. Evidence: {run_dir}")
            record["state"] = "committed-unverified"
            record["applyStartedAt"] = apply_evidence.get("applyStartedAt")
            record["applyCompletedAt"] = apply_evidence.get("applyCompletedAt")
            record["applyPayloadDigest"] = apply_evidence.get("payloadDigest")
            run_manifest["state"] = "committed-unverified"
            _atomic_write_json(manifest_path, run_manifest)

            apply_identity = apply_evidence.get("identity")
            if not isinstance(apply_identity, Mapping):
                raise MigrationApplyError(f"{original.folder.name} committed, but apply identity is unavailable; reconcile. Evidence: {run_dir}")
            _assert_same_target(initial_target_identity, apply_identity)
            fresh_inventory = capture_inventory_fn(target, run_dir / f"verify-inventory-{folder_index:03d}")
            fresh_identity = _identity_for_receipt(fresh_inventory, target)
            _assert_same_target(initial_target_identity, fresh_identity)
            operations = analyze_batch((original,), target.schema)
            fresh_snapshot = _snapshot_for(target, fresh_inventory, _selected_keys(operations), run_dir / f"verify-snapshot-{folder_index:03d}", capture_snapshot_fn)
            post_checks = _check_report(target, (original,), "postconditions", run_dir / f"postconditions-{folder_index:03d}", run_checks_fn)
            _assert_check_identity(initial_target_identity, post_checks)
            catalog_results, catalog_errors = _verify_structural_postconditions(original, fresh_snapshot, target.schema)
            if not post_checks.complete or not post_checks.passed or catalog_errors:
                record["state"] = "committed-verification-failed"
                record["verificationErrors"] = [*post_checks.errors, *catalog_errors]
                run_manifest["state"] = record["state"]
                _atomic_write_json(manifest_path, run_manifest)
                raise MigrationApplyError(f"{original.folder.name} committed but fresh verification failed; no receipt was written. Reconcile. Evidence: {run_dir}")

            source_relative = original.folder.relative_to(repo_root).as_posix()
            current_source = load_migration(repo_root, source_relative)
            staged_digest = _verify_staged_payload(staged)
            if current_source.payload_digest != original.payload_digest or staged_digest != original.payload_digest:
                record["state"] = "committed-source-or-payload-changed"
                run_manifest["state"] = record["state"]
                _atomic_write_json(manifest_path, run_manifest)
                raise MigrationApplyError(f"{original.folder.name} committed from frozen bytes, but local source changed during apply; no receipt was written. Reconcile. Evidence: {run_dir}")

            receipt = build_receipt(original, initial_target_identity, apply_evidence, fresh_snapshot, post_checks, catalog_results)
            receipt_path = original.folder / f"status.{target.environment}.json"
            try:
                receipt_installer(receipt_path, receipt)
            except (MigrationManifestError, OSError) as error:
                record["state"] = "committed-receipt-failed"
                record["receiptError"] = str(error)
                run_manifest["state"] = record["state"]
                _atomic_write_json(manifest_path, run_manifest)
                raise MigrationApplyError(f"{original.folder.name} committed and verified, but receipt installation failed; reconcile. Evidence: {run_dir}") from error
            record["state"] = "verified"
            record["verifiedAt"] = receipt["verifiedAt"]
            record["receipt"] = receipt_path.name
            run_manifest["state"] = "verified"
            _atomic_write_json(manifest_path, run_manifest)
            print(f"Applied and verified {original.folder.name} on {target.environment}; receipt {receipt_path.name}.")

        return 0
    except KeyboardInterrupt:
        if attempted and run_dir is not None:
            print(
                "migration interrupted after a write was attempted; stop and reconcile. "
                f"Retained attempt evidence: {run_dir}",
                file=sys.stderr,
            )
        else:
            print("migration interrupted; no writes were attempted", file=sys.stderr)
        return 130
    except (MigrationApplyError, MigrationManifestError, CatalogError, TargetResolutionError, OSError, RuntimeError, ValueError) as error:
        print(f"migration error: {error}", file=sys.stderr)
        if attempted and run_dir is not None:
            print(f"Retained attempt evidence: {run_dir}", file=sys.stderr)
            if manifest_path is not None and run_manifest is not None:
                try:
                    run_manifest["state"] = run_manifest.get("state", "apply-failed-or-unknown")
                    _atomic_write_json(manifest_path, run_manifest)
                except OSError:
                    pass
        return 2
    finally:
        if run_dir is not None and not attempted:
            safe_rmtree(run_dir)


def _confirm_from_terminal(prompt: str) -> bool:
    try:
        answer = input(prompt + " ").strip().casefold()
    except (EOFError, OSError):
        return False
    return answer in {"y", "yes"}


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    repo_root: Path = ROOT,
    confirm: Callable[[str], bool] = _confirm_from_terminal,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", nargs="*")
    parser.add_argument("--env", action="append", choices=("dev", "staging", "prod"))
    parser.add_argument("--schema")
    args = parser.parse_args(argv)
    if not args.folders:
        print("usage: scripts/team.sh migrate <migration-folder> [...] --env dev|staging|prod", file=sys.stderr)
        return 2
    if len(args.env or []) != 1:
        print("migration error: specify exactly one --env dev|staging|prod", file=sys.stderr)
        return 2
    try:
        migrations = load_batch(Path(repo_root), args.folders)
        values = os.environ if environ is None else environ
        requested = args.schema or values.get("PROJECT_SCHEMA") or None
        schema = batch_schema([migration.schema for migration in migrations], requested, values, args.env[0])
        target = resolve_target(values, args.env[0], "migration", schema=schema)
        assert_single_layout(Path(repo_root), migrations, target.schema, flat_folders_apply=flat_migrations_apply(values, args.env[0]))
    except (MigrationManifestError, TargetResolutionError, OSError) as error:
        print(f"migration error: {error}", file=sys.stderr)
        return 2
    return apply_batch(Path(repo_root), migrations, target, confirm)


def _interrupt_on_sigterm() -> None:
    """Handle SIGTERM like Ctrl-C, so the run says what state it left."""
    if not hasattr(signal, "SIGTERM"):
        return

    def handler(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):  # not the main thread
        pass


def _record_status(status: int, environ: Mapping[str, str] = os.environ) -> int:
    """Also write the exit status to MIGRATE_STATUS_FILE, and return it.

    On Windows migrate.sh runs Python as a child, and Git Bash reports 130 for a child that
    ends within a moment of a Ctrl-C whatever status it chose; an interrupted apply chooses
    2 ("may be partially applied"), so migrate.sh reads the status from this file.
    """
    path = environ.get("MIGRATE_STATUS_FILE")
    if path:
        try:
            Path(path).write_text(f"{status}\n", encoding="ascii")
        except OSError:
            pass
    return status


if __name__ == "__main__":
    _interrupt_on_sigterm()
    raise SystemExit(_record_status(main()))
