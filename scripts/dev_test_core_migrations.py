"""Built-in rollout and migration qualification adapters (Task 7)."""

from __future__ import annotations

import contextlib
import io
import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from scripts import baseline, dev_test_core_types as core_types, migrate, migration_attempts
from scripts import migration_checks, migration_manifest, rollout
from scripts.db_targets import ORACLE_IDENTIFIER, Target
from scripts.dev_test_bindings import RenderContext
from scripts.dev_test_evidence import RunEvidence, make_object_name
from scripts.dev_test_runtime import _same_live_target, _target_is_dev, read_rows
from scripts.migration_checks import QueryCheck
from scripts.schema_catalog import SchemaInventory, SchemaSnapshot
from scripts.sqlcl_session import run_sqlcl


_DATABASE_SCOPE_FIELDS = (
    "db_unique_name", "container_id", "container_name", "current_schema", "edition",
)


def _scenario(scenario_id: str):
    return core_types._get_builtin_scenario(scenario_id)


def _fixture_name(context: RenderContext, run: RunEvidence, binding: str, suffix: str) -> str:
    value = context.objects.get(binding) or make_object_name(run.object_prefix, suffix)
    if not isinstance(value, str) or ORACLE_IDENTIFIER.fullmatch(value) is None or len(value.encode("utf-8")) > 30:
        raise core_types._ScenarioProblem("BLOCKED", f"{binding} fixture name is not a valid 30-byte Oracle identifier")
    return value


def _object_context(context: RenderContext, **objects: str) -> RenderContext:
    return RenderContext(
        context.owner, context.run_id, {**context.objects, **objects}, context.values,
    )


def _step(
    target: Target,
    scenario: Any,
    step_id: str,
    sql: str,
    context: RenderContext,
    run: RunEvidence,
    runner: Callable,
    identity: Mapping[str, Any],
    ordinal: int,
    steps: list[dict],
) -> dict:
    result = core_types._execute_generated_step(
        target, scenario, step_id, sql, context, run, runner, identity, ordinal,
    )
    steps.append(result)
    if result.get("status") != "PASSED":
        status = result.get("status")
        if status not in {"BLOCKED", "FAILED", "UNKNOWN"}:
            status = "UNKNOWN"
        raise core_types._ScenarioProblem(
            status,
            f"{step_id} did not complete with verified target identity: {result.get('error', result.get('status'))}",
            (result,),
        )
    return result


def _capture_fixture(
    target: Target,
    run: RunEvidence,
    runner: Callable,
    identity: Mapping[str, Any],
    name: str,
    object_type: str,
):
    return core_types._capture_fixture(target, run, runner, name, object_type, identity)


def _save_fixtures(
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    runner: Callable,
    identity: Mapping[str, Any],
    scenario_id: str,
    names: Sequence[tuple[str, str]],
    *,
    details: Mapping[str, Any] | None = None,
) -> None:
    fixtures = []
    inventory: SchemaInventory | None = None
    for name, object_type in names:
        inventory, snapshot, definition = _capture_fixture(
            target, run, runner, identity, name, object_type,
        )
        fixtures.append(core_types._inventory_object(target, name, object_type, inventory, definition, snapshot))
    if inventory is None:
        raise core_types.CatalogError("built-in scenario has no fixture inventory")
    core_types._save_fixture_inventory(run, scenario_id, inventory.identity, fixtures, details=details)


def _result(
    scenario: Any,
    status: str,
    steps: Sequence[Mapping[str, Any]],
    checks: Sequence[Mapping[str, Any]] = (),
    observations: Sequence[Mapping[str, Any]] = (),
    *,
    error: str | None = None,
    eligible: bool = True,
) -> dict[str, Any]:
    value = core_types._outcome(scenario, status, steps=steps, checks=checks, error=error, eligible=eligible)
    value["observations"] = [dict(observation) for observation in observations]
    return value


def _problem(
    scenario: Any,
    error: core_types._ScenarioProblem,
    steps: Sequence[Mapping[str, Any]],
    checks: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    problem_checks = [*checks, *error.records] if error.records else list(checks)
    return _result(scenario, error.status, steps, problem_checks, observations, error=str(error))


def _unexpected(
    scenario: Any,
    error: Exception,
    steps: Sequence[Mapping[str, Any]],
    checks: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]] = (),
    *,
    write_started: bool,
) -> dict[str, Any]:
    status = "UNKNOWN" if write_started else "BLOCKED"
    return _result(
        scenario, status, steps, checks, observations,
        error=f"{type(error).__name__}: {error}", eligible=status != "BLOCKED",
    )


def _preflight(
    scenario: Any,
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    runner: Callable,
    fixture_names: Sequence[str],
) -> tuple[Mapping[str, Any], SchemaInventory, list[dict]]:
    return core_types._run_common_preflight(
        scenario, target, context, run, runner, fixture_names,
    )


def _project_root(run: RunEvidence) -> Path:
    project = run.run_dir / "project"
    if project.is_symlink() or not project.is_dir():
        raise OSError("private run project directory is missing or unsafe")
    return project


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("migration evidence log cannot be a symbolic link")
    path.write_text(text, encoding="utf-8", newline="\n")
    if os.name != "nt":
        path.chmod(0o600)


@contextlib.contextmanager
def _rollout_dev_environment(target: Target):
    """Expose only the already selected DEV classification to rollout's validator."""
    previous = os.environ.get("DB_ENVIRONMENT")
    os.environ["DB_ENVIRONMENT"] = target.classification
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DB_ENVIRONMENT", None)
        else:
            os.environ["DB_ENVIRONMENT"] = previous


def _rollout_script_binding(
    project: Path, file: str, target: Target, identity: Mapping[str, Any], observation: str,
) -> dict[str, str]:
    """Resolve the guarded script alias, then prove it reaches the selected DEV scope."""
    step = {
        "type": "sql-script", "file": file,
        "connection": target.expected_user,
        "expectedUser": target.expected_user,
        "schema": target.schema,
    }
    binding = rollout._target_binding(step, project, "dev")
    if (
        binding is None
        or binding.get("environment") != target.environment
        or binding.get("expectedUser") != target.expected_user
        or binding.get("schema") != target.schema
        or binding.get("classification") != target.classification
        or binding.get("connection") != target.expected_user
    ):
        raise core_types._ScenarioProblem(
            "BLOCKED", "rollout SQL-script target does not match the selected DEV owner and classification",
        )
    alias_target = Target(
        binding["environment"], binding["connection"], binding["expectedUser"],
        binding["schema"], binding["classification"],
    )
    observed = rollout._observe_target_identity(
        alias_target, project / "sessions" / f"rollout-alias-{observation}",
    )
    if not core_types._same_scenario_identity(identity, observed):
        raise core_types._ScenarioProblem(
            "BLOCKED", "rollout SQL-script alias does not resolve to the selected DEV database scope",
        )
    return dict(binding)


def run_sqlcl_delivery(
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    *,
    runner: Callable = run_sqlcl,
) -> dict[str, Any]:
    """Run a two-row blank-line UPDATE through the production rollout pipeline."""
    scenario = _scenario("sqlcl-delivery")
    steps: list[dict] = []
    checks: list[dict] = []
    observations: list[dict] = []
    write_started = False
    try:
        table = _fixture_name(context, run, "table", "SD_TAB")
        identity, _initial, required = _preflight(scenario, target, context, run, runner, (table,))
        checks.extend(required)
        scenario_context = _object_context(context, table=table)
        project = _project_root(run)
        sql_file = project / "delivery.sql"
        sql_file.write_text(
            f'UPDATE "{target.schema}"."{table}"\n'
            'SET "STATE" = \'UPDATED\'\n\n'
            'WHERE "ID" = 1;\n',
            encoding="utf-8", newline="\n",
        )
        manifest_path = project / "rollout.json"
        manifest_path.write_text(json.dumps({
            "schemaVersion": 1,
            "steps": [{
                "type": "sql-script", "file": sql_file.name,
                "connection": target.expected_user,
                "expectedUser": target.expected_user,
                "schema": target.schema,
            }],
        }, indent=2) + "\n", encoding="utf-8", newline="\n")
        with _rollout_dev_environment(target):
            binding = _rollout_script_binding(project, sql_file.name, target, identity, "pre-fixture")
        steps.append({
            "id": "rollout-alias-preflight", "status": "PASSED",
            "targetBinding": binding,
            "evidence": (project / "sessions" / "rollout-alias-pre-fixture").relative_to(run.run_dir).as_posix(),
            "writeStarted": False,
        })

        # Prove the alias mandated by the guarded SQL-script path is the same
        # selected database before creating any fixture objects or rows.
        create = (
            f'CREATE TABLE "{target.schema}"."{table}" ('
            '"ID" NUMBER PRIMARY KEY, "STATE" VARCHAR2(20 CHAR));\n'
        )
        _step(target, scenario, "create-delivery-fixture", create, scenario_context, run, runner, identity, 1, steps)
        write_started = True
        seed = (
            f'INSERT INTO "{target.schema}"."{table}" ("ID", "STATE") VALUES (1, \'UNCHANGED\');\n'
            f'INSERT INTO "{target.schema}"."{table}" ("ID", "STATE") VALUES (2, \'UNCHANGED\');\n'
            "COMMIT;\n"
        )
        _step(target, scenario, "seed-delivery-fixture", seed, scenario_context, run, runner, identity, 2, steps)
        _save_fixtures(target, scenario_context, run, runner, identity, scenario.id, ((table, "TABLE"),), details={"rowCount": 2, "retained": True})

        report_base = project / "rollout-report"
        with _rollout_dev_environment(target):
            try:
                latest_binding = _rollout_script_binding(
                    project, sql_file.name, target, identity, "before-execute",
                )
            except core_types._ScenarioProblem as error:
                steps.append({
                    "id": "rollout-alias-recheck", "status": "UNKNOWN",
                    "targetBinding": binding,
                    "evidence": (project / "sessions" / "rollout-alias-before-execute").relative_to(run.run_dir).as_posix(),
                    "writeStarted": True,
                })
                raise core_types._ScenarioProblem(
                    "UNKNOWN", "rollout alias identity changed after fixture setup; preserve and reconcile the run",
                ) from error
            if latest_binding != binding:
                steps.append({
                    "id": "rollout-alias-recheck", "status": "UNKNOWN",
                    "targetBinding": latest_binding,
                    "evidence": (project / "sessions" / "rollout-alias-before-execute").relative_to(run.run_dir).as_posix(),
                    "writeStarted": True,
                })
                raise core_types._ScenarioProblem(
                    "UNKNOWN", "rollout target binding changed after fixture setup; preserve and reconcile the run",
                )
            steps.append({
                "id": "rollout-alias-recheck", "status": "PASSED",
                "targetBinding": latest_binding,
                "evidence": (project / "sessions" / "rollout-alias-before-execute").relative_to(run.run_dir).as_posix(),
                "writeStarted": True,
            })
            exit_code = rollout.run_rollout(
                manifest_path, "dev", repo_root=project, report_path=report_base,
                confirm=lambda _prompt: (
                    target.environment == "dev" and context.run_id == run.run_id
                ),
            )
        report_path = report_base.with_suffix(".json")
        report_evidence = report_path.relative_to(run.run_dir).as_posix()
        if not report_path.is_file() or report_path.is_symlink():
            steps.append({
                "id": "rollout-update", "status": "UNKNOWN", "report": report_evidence,
                "exitCode": exit_code, "writeStarted": True,
            })
            raise core_types._ScenarioProblem(
                "UNKNOWN", "rollout did not produce a successful, complete report; inspect private run evidence",
            )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            steps.append({
                "id": "rollout-update", "status": "UNKNOWN", "report": report_evidence,
                "exitCode": exit_code, "writeStarted": True,
            })
            raise core_types._ScenarioProblem("UNKNOWN", "rollout report is malformed; inspect private run evidence")
        report_steps = report.get("steps")
        report_step = report_steps[0] if isinstance(report_steps, list) and len(report_steps) == 1 else None
        rollout_record: dict[str, Any] = {
            "id": "rollout-update",
            "status": "OBSERVED" if exit_code == 0 else "REPORTED_FAILURE",
            "report": report_evidence,
            "exitCode": exit_code,
            "reportStatus": report.get("status"),
            "writeStarted": True,
        }
        steps.append(rollout_record)
        if (
            exit_code != 0 or report.get("status") != "succeeded"
            or report.get("exitCode") != 0 or not isinstance(report_step, dict)
            or report_step.get("status") != "succeeded"
            or report_step.get("targetBinding") != binding
            or report_step.get("observedIdentity") is None
            or not core_types._same_scenario_identity(identity, report_step.get("observedIdentity", {}))
        ):
            raise core_types._ScenarioProblem("UNKNOWN", "rollout report lacks successful step and target identity evidence")
        receipt_relative = report_step.get("receiptPath")
        if not isinstance(receipt_relative, str):
            raise core_types._ScenarioProblem("UNKNOWN", "successful rollout report has no receipt path")
        receipt_path = project / receipt_relative
        if (
            receipt_path.is_symlink() or not receipt_path.is_file()
            or not receipt_path.resolve().is_relative_to(project.resolve())
        ):
            raise core_types._ScenarioProblem("UNKNOWN", "successful rollout receipt is missing or outside the private run project")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (
            not isinstance(receipt, dict) or receipt.get("status") != "succeeded"
            or receipt.get("stepSha256") != report_step.get("sha256")
            or receipt.get("targetBinding") != binding
            or receipt.get("observedIdentity") != report_step.get("observedIdentity")
        ):
            raise core_types._ScenarioProblem("UNKNOWN", "rollout receipt does not match the verified report and target")
        rollout_record.update({
            "status": "PASSED", "sourceSha256": report_step["sha256"],
            "observedIdentity": dict(report_step["observedIdentity"]),
            "targetBinding": binding, "receipt": receipt_relative,
        })

        query = 'SELECT "ID", "STATE" FROM ${object.table} ORDER BY "ID"'
        readback = read_rows(target, query, scenario_context, run, max_rows=3, runner=runner)
        if not _same_live_target(identity, readback.get("identity", {})):
            raise core_types._ScenarioProblem("UNKNOWN", "fresh rollout readback observed a different database identity")
        rows = [dict(row) for row in readback.get("rows", [])]
        observation = {
            "id": "delivery-rows-after-rollout", "count": readback.get("count"),
            "rows": rows, "truncated": readback.get("truncated"),
            "evidence": readback.get("evidence"),
        }
        observations.append(observation)
        expected = [{"ID": "1", "STATE": "UPDATED"}, {"ID": "2", "STATE": "UNCHANGED"}]
        if readback.get("truncated") is not False or rows != expected:
            raise core_types._ScenarioProblem(
                "FAILED", "fresh rollout readback did not prove the selected row changed and its neighbor stayed unchanged",
                (observation,),
            )
        _save_fixtures(target, scenario_context, run, runner, identity, scenario.id, ((table, "TABLE"),), details={"rowCount": len(rows), "retained": True})
        checks.append({"id": "selected-row-only", "expected": expected, "actual": rows, "passed": True})
        return _result(scenario, "PASSED", steps, checks, observations)
    except core_types._ScenarioProblem as error:
        return _problem(scenario, error, steps, checks, observations)
    except Exception as error:
        return _unexpected(scenario, error, steps, checks, observations, write_started=write_started)


def _column_snapshot_sections(table: str, columns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [dict(row, table_name=table) for row in columns]
    return {
        "tables": [{"name": table}], "columns": rows,
        "constraints": [], "indexes": [], "identity-columns": [],
        "sequences": [], "views": [],
    }


def _migration_folder(
    project: Path,
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    table: str,
    current_columns: Sequence[Mapping[str, Any]],
    *,
    family_suffix: str,
    wrong_definition: bool = False,
) -> tuple[migration_manifest.Migration, dict[str, Any]]:
    desired_columns = [dict(row) for row in current_columns]
    desired_columns.append({
        "table_name": table, "name": "NOTE", "data_type": "VARCHAR2",
        "char_length": 20, "char_used": "C", "data_length": 80,
        "nullable": "Y", "hidden": "NO", "virtual": "NO", "identity": "NO",
    })
    source = {"sections": _column_snapshot_sections(table, desired_columns)}
    target_snapshot = {"sections": _column_snapshot_sections(table, current_columns)}
    steps, checks = baseline._structure_bundle(
        {"prefixes": []}, target.schema, target.schema, source, target_snapshot,
        SchemaSnapshot(dict(), {}, {}, {}, "", ""),
    )
    if not steps:
        raise baseline.BaselineError("baseline generator returned no nullable-column change")
    checks = dict(checks)
    checks["preconditions"] = [{
        "id": "pre-note-column-absent",
        "sql": (
            "SELECT CASE WHEN COUNT(*) = 0 THEN 1 ELSE 0 END FROM ALL_TAB_COLS "
            f"WHERE OWNER = '{target.schema}' AND TABLE_NAME = '{table}' AND COLUMN_NAME = 'NOTE'"
        ),
        "expected": 1,
    }]
    if wrong_definition:
        changed = {}
        for name, sql in steps.items():
            if "VARCHAR2(20 CHAR)" not in sql:
                raise baseline.BaselineError("generated nullable-column SQL did not contain the expected definition")
            changed[name] = sql.replace("VARCHAR2(20 CHAR)", "VARCHAR2(21 CHAR)")
        steps = changed
    files = baseline._migration_bytes(
        steps, checks,
        f"Private DEV test migration for run {run.run_id}; retain for inspection.\n",
    )
    run_fragment = run.run_id.split("-")[-1][:10]
    family = f"devtest-{family_suffix}-{run_fragment}"
    folder = baseline._build_migration_folder(project, target.schema, family, files)
    if folder is None:
        raise baseline.BaselineError("baseline generator did not create a fresh run-owned migration folder")
    relative = folder.relative_to(project).as_posix()
    migration = migration_manifest.load_migration(project, relative)
    if not migration.folder.resolve().is_relative_to(project.resolve()):
        raise baseline.BaselineError("generated migration escaped the private run project")
    return migration, checks


def _apply_callbacks(
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    runner: Callable,
    identity: Mapping[str, Any],
    apply_count: list[int],
):
    def capture_inventory_fn(selected_target: Target, run_dir: Path):
        return core_types._capture_inventory(selected_target, run, runner, identity)

    def capture_snapshot_fn(
        selected_target: Target,
        inventory: SchemaInventory,
        keys: Sequence[tuple[str, str]],
        run_dir: Path,
    ):
        snapshot = core_types.capture_snapshot(
            selected_target, inventory, keys, run_dir, _runner=runner,
        )
        core_types._verify_catalog_identity(snapshot.identity, selected_target, identity)
        if (
            not isinstance(snapshot.identity, Mapping)
            or not all(snapshot.identity.get(field) == identity.get(field) for field in _DATABASE_SCOPE_FIELDS)
            or snapshot.identity.get("database_version") != identity.get("database_version")
        ):
            raise core_types.CatalogError("migration snapshot changed container, edition or database version")
        return snapshot

    def run_checks_fn(
        selected_target: Target,
        checks: Sequence[QueryCheck],
        run_dir: Path,
        *,
        phase: str,
    ):
        return core_types.run_checks(
            selected_target, checks, run_dir, phase=phase, _runner=runner,
        )

    def apply_folder_fn(
        migration: migration_manifest.Migration,
        selected_target: Target,
        run_dir: Path,
        *,
        expected_identity: Mapping[str, str] | None = None,
    ):
        apply_count[0] += 1
        return migrate.apply_folder(
            migration, selected_target, run_dir, expected_identity=expected_identity,
        )

    def confirm_dev_scratch(_prompt: str) -> bool:
        return (
            target.environment == "dev" and _target_is_dev(target)
            and context.run_id == run.run_id and context.owner == target.schema
        )

    return {
        "confirm": confirm_dev_scratch,
        "capture_inventory_fn": capture_inventory_fn,
        "capture_snapshot_fn": capture_snapshot_fn,
        "run_checks_fn": run_checks_fn,
        "apply_folder_fn": apply_folder_fn,
    }


def _apply_batch(
    scenario_id: str,
    project: Path,
    migration: migration_manifest.Migration,
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    runner: Callable,
    identity: Mapping[str, Any],
    apply_count: list[int],
) -> tuple[int, Path]:
    log_path = run.run_dir / "sessions" / f"{scenario_id}-migration-output-{apply_count[0] + 1}.log"
    stdout, stderr = io.StringIO(), io.StringIO()
    callbacks = _apply_callbacks(target, context, run, runner, identity, apply_count)
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        status = migrate.apply_batch(
            project, (migration,), target, callbacks["confirm"],
            capture_inventory_fn=callbacks["capture_inventory_fn"],
            capture_snapshot_fn=callbacks["capture_snapshot_fn"],
            run_checks_fn=callbacks["run_checks_fn"],
            apply_folder_fn=callbacks["apply_folder_fn"],
        )
    _write_private(log_path, stdout.getvalue() + stderr.getvalue())
    return status, log_path


def _fresh_target_identity(target: Target, identity: Mapping[str, Any]) -> dict[str, str]:
    return {
        "environment": target.environment,
        "connection": target.connection,
        "expected_user": target.expected_user,
        **{field: str(identity[field]) for field in migration_checks.OBSERVED_IDENTITY_FIELDS},
    }


def _postcondition_observation(
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    runner: Callable,
    identity: Mapping[str, Any],
    table: str,
) -> tuple[SchemaInventory, SchemaSnapshot, Any, dict[str, Any]]:
    inventory, snapshot, definition = _capture_fixture(
        target, run, runner, identity, table, "TABLE",
    )
    return inventory, snapshot, definition, {
        "name": table,
        "columns": core_types._column_rows(definition, table),
        "identity": dict(snapshot.identity),
    }


def run_migration_success(
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    *,
    runner: Callable = run_sqlcl,
) -> dict[str, Any]:
    """Generate and apply a nullable-column change through the migration pipeline."""
    scenario = _scenario("migration-success")
    steps: list[dict] = []
    checks: list[dict] = []
    write_started = False
    try:
        table = _fixture_name(context, run, "table", "MS_TAB")
        identity, _initial, requirements = _preflight(scenario, target, context, run, runner, (table,))
        checks.extend(requirements)
        scenario_context = _object_context(context, table=table)
        create = f'CREATE TABLE "{target.schema}"."{table}" ("ID" NUMBER NOT NULL);\n'
        _step(target, scenario, "create-migration-fixture", create, scenario_context, run, runner, identity, 1, steps)
        write_started = True
        project = _project_root(run)
        inventory, snapshot, definition = _capture_fixture(target, run, runner, identity, table, "TABLE")
        current_columns = core_types._column_rows(definition, table)
        migration, generated = _migration_folder(
            project, target, context, run, table, current_columns,
            family_suffix="success",
        )
        apply_count = [0]
        status, log_path = _apply_batch(
            scenario.id, project, migration, target, context, run, runner, identity, apply_count,
        )
        write_started = True
        receipt_path = migration.folder / f"status.{target.environment}.json"
        if status != 0 or not receipt_path.is_file():
            raise core_types._ScenarioProblem(
                "UNKNOWN", "migration did not produce a verified success receipt; reconcile private attempt evidence",
            )
        receipt = migration_manifest.validate_receipt(
            receipt_path, migration, _fresh_target_identity(target, identity),
        )
        _, _, _, actual = _postcondition_observation(target, scenario_context, run, runner, identity, table)
        expected_check = baseline._column_definition_check(target.schema, {
            "table_name": table, "name": "NOTE", "data_type": "VARCHAR2",
            "char_length": 20, "char_used": "C", "data_length": 80,
            "nullable": "Y",
        })
        post = core_types._run_checked_phase(
            target, scenario, (QueryCheck(expected_check["id"], expected_check["sql"], 1),),
            run, runner, phase="migration-success-readback", expected_identity=identity,
            failure_status="FAILED",
        )
        checks.extend(post)
        note = next((row for row in actual["columns"] if row.get("name") == "NOTE"), None)
        if note is None or note.get("char_length") != 20 or note.get("nullable") != "Y":
            raise core_types._ScenarioProblem("FAILED", "fresh catalog readback did not confirm the nullable VARCHAR2(20 CHAR) column")
        relative_folder = migration.folder.relative_to(run.run_dir).as_posix()
        steps.append({
            "id": "migration-apply", "status": "PASSED", "folder": relative_folder,
            "receipt": receipt_path.relative_to(run.run_dir).as_posix(),
            "payloadDigest": receipt["payloadDigest"], "applyLog": log_path.relative_to(run.run_dir).as_posix(),
            "applyCalls": apply_count[0],
        })
        checks.append({
            "id": "migration-receipt", "passed": True,
            "payloadDigest": receipt["payloadDigest"], "checksSha256": receipt["checksSha256"],
        })
        _save_fixtures(
            target, scenario_context, run, runner, identity, scenario.id, ((table, "TABLE"),),
            details={"migrationFolder": relative_folder, "receipt": receipt_path.relative_to(run.run_dir).as_posix(), "retained": True},
        )
        return _result(scenario, "PASSED", steps, checks)
    except core_types._ScenarioProblem as error:
        return _problem(scenario, error, steps, checks)
    except Exception as error:
        return _unexpected(scenario, error, steps, checks, write_started=write_started)


def _expected_refusal(
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    runner: Callable,
    scenario: Any,
    identity: Mapping[str, Any],
    migration: migration_manifest.Migration,
    table: str,
    project: Path,
    apply_count: list[int],
    scenario_id: str,
) -> tuple[list[dict], dict[str, Any], tuple[Mapping[str, Any], ...]]:
    status, log_path = _apply_batch(
        scenario_id, project, migration, target, context, run, runner, identity, apply_count,
    )
    if status == 0:
        raise core_types._ScenarioProblem("FAILED", "deliberately incorrect migration unexpectedly passed verification")
    receipt_path = migration.folder / f"status.{target.environment}.json"
    if receipt_path.exists() or receipt_path.is_symlink():
        raise core_types._ScenarioProblem("FAILED", "refused migration unexpectedly installed a success receipt")

    _inventory, _snapshot, definition, actual = _postcondition_observation(
        target, context, run, runner, identity, table,
    )
    note = next((row for row in actual["columns"] if row.get("name") == "NOTE"), None)
    if note is None or note.get("char_length") != 21:
        raise core_types._ScenarioProblem("UNKNOWN", "wrong-definition fixture was not independently observed after apply")
    generated = baseline._column_definition_check(target.schema, {
        "table_name": table, "name": "NOTE", "data_type": "VARCHAR2",
        "char_length": 20, "char_used": "C", "data_length": 80,
    })
    try:
        core_types._run_checked_phase(
            target, scenario, (QueryCheck(generated["id"], generated["sql"], 1),),
            run, runner, phase=f"{scenario_id}-failed-postcondition", expected_identity=identity,
            failure_status="FAILED",
        )
    except core_types._ScenarioProblem as mismatch:
        if mismatch.status != "FAILED" or len(mismatch.records) != 1:
            raise
        if mismatch.records[0].get("actual") != 0 or mismatch.records[0].get("passed") is not False:
            raise core_types._ScenarioProblem(
                "UNKNOWN", "failed migration check did not retain its observed mismatch",
            ) from mismatch
        check_records = tuple(mismatch.records)
    else:
        raise core_types._ScenarioProblem("FAILED", "wrong column definition passed its independent postcondition")

    events = migration_attempts.read_attempt_events(migration.folder)
    reduced = migration_attempts.reduce_attempt_events(events)
    pending = [event for event in events if event.get("state") == "pending" and event.get("writeAttempted") is True]
    if len(pending) != 1 or reduced.status != "locked":
        raise core_types._ScenarioProblem("UNKNOWN", "refused migration did not retain one validated pending lock event")
    step = {
        "id": "migration-refusal", "status": "EXPECTED_REFUSAL",
        "folder": migration.folder.relative_to(run.run_dir).as_posix(),
        "applyLog": log_path.relative_to(run.run_dir).as_posix(),
        "receiptAbsent": True,
        "pendingEvent": pending[0]["eventId"],
        "retryLock": reduced.status,
        "applyCalls": apply_count[0],
        "actualNote": dict(note),
    }
    return [step], {"receiptPath": receipt_path, "attemptState": reduced}, check_records


def run_migration_refusal(
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    *,
    runner: Callable = run_sqlcl,
) -> dict[str, Any]:
    """Prove a wrong definition fails verification, leaves a pending lock, and cannot retry."""
    scenario = _scenario("migration-refusal")
    steps: list[dict] = []
    checks: list[dict] = []
    write_started = False
    try:
        table = _fixture_name(context, run, "table", "MR_TAB")
        identity, _initial, requirements = _preflight(scenario, target, context, run, runner, (table,))
        checks.extend(requirements)
        scenario_context = _object_context(context, table=table)
        create = f'CREATE TABLE "{target.schema}"."{table}" ("ID" NUMBER NOT NULL);\n'
        _step(target, scenario, "create-refusal-fixture", create, scenario_context, run, runner, identity, 1, steps)
        write_started = True
        project = _project_root(run)
        _inventory, _snapshot, definition = _capture_fixture(target, run, runner, identity, table, "TABLE")
        migration, _generated = _migration_folder(
            project, target, context, run, table, core_types._column_rows(definition, table),
            family_suffix="refusal", wrong_definition=True,
        )
        payload = migration.files[0].source.decode("utf-8")
        if "VARCHAR2(21 CHAR)" not in payload or migration.postconditions[0].expected != 1:
            raise core_types._ScenarioProblem("BLOCKED", "refusal fixture does not preserve the generated expected definition check")
        apply_count = [0]
        refusal_steps, receipt, failed_check_records = _expected_refusal(
            target, scenario_context, run, runner, scenario, identity, migration,
            table, project, apply_count, scenario.id,
        )
        steps.extend(refusal_steps)
        checks.extend(failed_check_records)
        before_retry = apply_count[0]
        retry_status, retry_log = _apply_batch(
            scenario.id, project, migration, target, context, run, runner, identity, apply_count,
        )
        if retry_status == 0 or apply_count[0] != before_retry:
            raise core_types._ScenarioProblem("FAILED", "guarded migration retry was not refused before another payload execution")
        events = migration_attempts.read_attempt_events(migration.folder)
        reduced = migration_attempts.reduce_attempt_events(events)
        if reduced.status != "locked":
            raise core_types._ScenarioProblem("UNKNOWN", "pending attempt lock was lost during retry refusal")
        steps.append({
            "id": "guarded-retry", "status": "PASSED", "exitCode": retry_status,
            "applyLog": retry_log.relative_to(run.run_dir).as_posix(),
            "payloadApplyCount": apply_count[0], "pendingLockRetained": True,
        })
        _save_fixtures(
            target, scenario_context, run, runner, identity, scenario.id, ((table, "TABLE"),),
            details={"migrationFolder": migration.folder.relative_to(run.run_dir).as_posix(), "receiptAbsent": True, "retained": True},
        )
        checks.append({
            "id": "no-success-receipt", "passed": not receipt["receiptPath"].exists(),
            "expected": "absent", "actual": "absent",
        })
        return _result(scenario, "PASSED", steps, checks)
    except core_types._ScenarioProblem as error:
        return _problem(scenario, error, steps, checks)
    except Exception as error:
        return _unexpected(scenario, error, steps, checks, write_started=write_started)


def _build_invalid_view_migration(
    project: Path,
    target: Target,
    run: RunEvidence,
    table: str,
    view: str,
) -> migration_manifest.Migration:
    validity = baseline._view_validity_check(target.schema, view)
    precondition = {**validity, "id": f"pre-{validity['id']}"}
    checks = {"preconditions": [precondition], "postconditions": [validity]}
    ddl = f'ALTER TABLE "{target.schema}"."{table}" DROP COLUMN "DEPENDENCY";\n'
    files = baseline._migration_bytes(
        {"001-invalidate-view.sql": ddl}, checks,
        f"Private invalid-view qualification migration for run {run.run_id}.\n",
    )
    run_fragment = run.run_id.split("-")[-1][:10]
    family = f"devtest-invalid-view-{run_fragment}"
    folder = baseline._build_migration_folder(project, target.schema, family, files)
    if folder is None:
        raise baseline.BaselineError("invalid-view migration generator did not create a fresh run-owned folder")
    migration = migration_manifest.load_migration(project, folder.relative_to(project).as_posix())
    if not migration.folder.resolve().is_relative_to(project.resolve()):
        raise baseline.BaselineError("invalid-view migration escaped the private run project")
    return migration


def _observe_invalid_view(
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    runner: Callable,
    identity: Mapping[str, Any],
    view: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    values = dict(context.values)
    for key, value in (("DEVTEST_OWNER", target.schema), ("DEVTEST_VIEW", view)):
        if key in values and values[key] != value:
            raise core_types._ScenarioProblem("BLOCKED", f"{key} conflicts with the generated fixture binding")
        values[key] = value
    bound_context = RenderContext(context.owner, context.run_id, dict(context.objects), values)
    status_query = (
        "SELECT STATUS FROM ALL_OBJECTS WHERE OWNER = :DEVTEST_OWNER "
        "AND OBJECT_NAME = :DEVTEST_VIEW AND OBJECT_TYPE = 'VIEW'"
    )
    errors_query = (
        "SELECT LINE, POSITION, ATTRIBUTE, TEXT FROM ALL_ERRORS WHERE OWNER = :DEVTEST_OWNER "
        "AND NAME = :DEVTEST_VIEW AND TYPE = 'VIEW' AND ATTRIBUTE = 'ERROR' ORDER BY SEQUENCE"
    )
    status = read_rows(target, status_query, bound_context, run, max_rows=1, runner=runner)
    errors = read_rows(target, errors_query, bound_context, run, max_rows=20, runner=runner)
    for observation in (status, errors):
        if not _same_live_target(identity, observation.get("identity", {})):
            raise core_types._ScenarioProblem("UNKNOWN", "invalid-view observation was read from a different target")
    if status.get("truncated") is not False or status.get("count") != 1:
        raise core_types._ScenarioProblem("UNKNOWN", "view status observation is missing, duplicated or truncated")
    error_rows = errors.get("rows")
    error_count = errors.get("count")
    if (
        errors.get("truncated") is not False
        or not isinstance(error_rows, list)
        or type(error_count) is not int
        or error_count < 0
        or error_count > 20
        or error_count != len(error_rows)
    ):
        raise core_types._ScenarioProblem("UNKNOWN", "ALL_ERRORS observation exceeded its safe row limit")
    status_record = {
        "id": "invalid-view-status", "count": status.get("count"),
        "rows": [dict(row) for row in status.get("rows", [])],
        "truncated": status.get("truncated"), "evidence": status.get("evidence"),
    }
    errors_record = {
        "id": "invalid-view-all-errors", "count": errors.get("count"),
        "rows": [dict(row) for row in error_rows],
        "truncated": errors.get("truncated"), "maxRows": 20,
        "evidence": errors.get("evidence"),
    }
    return status_record, errors_record


def run_invalid_view(
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    *,
    runner: Callable = run_sqlcl,
) -> dict[str, Any]:
    """Invalidate only the run-owned view and record bounded Oracle observations."""
    scenario = _scenario("invalid-view")
    steps: list[dict] = []
    checks: list[dict] = []
    observations: list[dict] = []
    write_started = False
    try:
        table = _fixture_name(context, run, "table", "IV_TAB")
        view = _fixture_name(context, run, "view", "IV_VIEW")
        identity, _initial, requirements = _preflight(scenario, target, context, run, runner, (table, view))
        checks.extend(requirements)
        scenario_context = _object_context(context, table=table, view=view)
        create_table = (
            f'CREATE TABLE "{target.schema}"."{table}" '
            '("ID" NUMBER NOT NULL, "DEPENDENCY" VARCHAR2(20 CHAR));\n'
        )
        _step(target, scenario, "create-view-dependency-table", create_table, scenario_context, run, runner, identity, 1, steps)
        write_started = True
        create_view = (
            f'CREATE VIEW "{target.schema}"."{view}" AS '
            f'SELECT "DEPENDENCY" FROM "{target.schema}"."{table}";\n'
        )
        _step(target, scenario, "create-dependent-view", create_view, scenario_context, run, runner, identity, 2, steps)
        validity = baseline._view_validity_check(target.schema, view)
        before = core_types._run_checked_phase(
            target, scenario, (QueryCheck(validity["id"], validity["sql"], 1),),
            run, runner, phase="invalid-view-before", expected_identity=identity,
            failure_status="FAILED",
        )
        checks.extend(before)
        project = _project_root(run)
        migration = _build_invalid_view_migration(project, target, run, table, view)
        apply_count = [0]
        apply_status, apply_log = _apply_batch(
            scenario.id, project, migration, target, context, run, runner, identity, apply_count,
        )
        write_started = True
        receipt_path = migration.folder / f"status.{target.environment}.json"
        if apply_status == 0 or receipt_path.exists() or receipt_path.is_symlink():
            raise core_types._ScenarioProblem("FAILED", "invalid view migration was not refused without a success receipt")
        after = core_types._run_checked_phase(
            target, scenario, (QueryCheck(validity["id"], validity["sql"], 0),),
            run, runner, phase="invalid-view-after", expected_identity=identity,
            failure_status="FAILED",
        )
        checks.extend(after)
        status_observation, errors_observation = _observe_invalid_view(
            target, scenario_context, run, runner, identity, view,
        )
        observations.extend((status_observation, errors_observation))
        status_rows = status_observation["rows"]
        if len(status_rows) != 1 or str(status_rows[0].get("STATUS", "")).upper() != "INVALID":
            raise core_types._ScenarioProblem("FAILED", "fresh ALL_OBJECTS readback did not observe the fixture view as INVALID")
        events = migration_attempts.read_attempt_events(migration.folder)
        pending = [event for event in events if event.get("state") == "pending" and event.get("writeAttempted") is True]
        attempt_state = migration_attempts.reduce_attempt_events(events)
        if len(pending) != 1 or attempt_state.status != "locked":
            raise core_types._ScenarioProblem("UNKNOWN", "invalid-view migration refusal did not retain a validated pending attempt")
        steps.append({
            "id": "invalidate-dependent-view", "status": "EXPECTED_REFUSAL",
            "folder": migration.folder.relative_to(run.run_dir).as_posix(),
            "receiptAbsent": True, "pendingEvent": pending[0]["eventId"],
            "applyLog": apply_log.relative_to(run.run_dir).as_posix(),
            "attemptState": attempt_state.status, "applyCalls": apply_count[0],
        })
        _save_fixtures(
            target, scenario_context, run, runner, identity, scenario.id,
            ((table, "TABLE"), (view, "VIEW")),
            details={
                "viewStatus": "INVALID", "allErrorsCount": errors_observation["count"],
                "migrationFolder": migration.folder.relative_to(run.run_dir).as_posix(),
                "receiptAbsent": True, "retained": True,
            },
        )
        checks.append({"id": "view-invalidity-detected", "expected": 0, "actual": 0, "passed": True})
        checks.append({"id": "migration-receipt-refused", "expected": "absent", "actual": "absent", "passed": True})
        return _result(scenario, "PASSED", steps, checks, observations)
    except core_types._ScenarioProblem as error:
        return _problem(scenario, error, steps, checks, observations)
    except Exception as error:
        return _unexpected(scenario, error, steps, checks, observations, write_started=write_started)
