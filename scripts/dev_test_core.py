"""Built-in qualification test suite and runner-owned dispatch (Tasks 6 & 7).

Public API:
    BUILTIN_SCENARIO_IDS: tuple[str, ...]
    builtin_suite() -> Suite
    run_builtin(id, target, context, run, *, runner=run_sqlcl) -> dict

Built-in scenarios are trusted, runner-owned qualification checks. A project
suite using a matching ID is still a project suite; provenance is caller-controlled.
No live SQL is executed at import time; Python standard library only.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any

from scripts.db_targets import Target
from scripts.dev_test_bindings import RenderContext
from scripts.dev_test_evidence import RunEvidence
from scripts.dev_test_manifest import Scenario, Suite
from scripts.sqlcl_session import run_sqlcl

BUILTIN_SCENARIO_IDS: tuple[str, ...] = (
    "datatype-roundtrip",
    "compiler-settings",
    "sqlcl-delivery",
    "migration-success",
    "migration-refusal",
    "invalid-view",
)

ROOT: Path = Path(__file__).resolve().parent.parent


def _build_builtin_scenario(sc_id: str) -> Scenario:
    """Build a frozen Scenario descriptor for one of the six built-in scenarios."""
    create_table_check = MappingProxyType({
        "id": "req-create-table",
        "sql": (
            "SELECT CASE WHEN EXISTS ("
            "SELECT 1 FROM SESSION_PRIVS WHERE PRIVILEGE = 'CREATE TABLE'"
            ") THEN 1 ELSE 0 END FROM DUAL"
        ),
        "expected": 1,
    })
    create_proc_check = MappingProxyType({
        "id": "req-create-procedure",
        "sql": (
            "SELECT CASE WHEN EXISTS ("
            "SELECT 1 FROM SESSION_PRIVS WHERE PRIVILEGE = 'CREATE PROCEDURE'"
            ") THEN 1 ELSE 0 END FROM DUAL"
        ),
        "expected": 1,
    })

    def metadata_access_check(view_name: str) -> MappingProxyType:
        return MappingProxyType({
            "id": f"req-metadata-{view_name.casefold().replace('_', '-')}",
            "sql": f"SELECT CASE WHEN COUNT(*) >= 0 THEN 1 ELSE 0 END FROM {view_name} WHERE ROWNUM <= 1",
            "expected": 1,
        })

    if sc_id == "datatype-roundtrip":
        desc = (
            "Create a small source fixture with BOOLEAN, NUMBER(10,2), NUMBER(*,3), "
            "FLOAT(63), VARCHAR2 CHAR semantics and RAW; capture through the production "
            "catalog path; generate and execute a second definition through baseline code."
        )
        req_checks = (create_table_check,)
        fix_objects = (
            MappingProxyType({"binding": "source_table", "suffix": "DTT_SRC", "type": "TABLE"}),
            MappingProxyType({"binding": "copy_table", "suffix": "DTT_CPY", "type": "TABLE"}),
        )
        txn = "mixed"

    elif sc_id == "compiler-settings":
        desc = (
            "Create a minimal stored unit, capture its actual settings and execute "
            "the production-generated settings query against Oracle."
        )
        req_checks = (create_proc_check,)
        fix_objects = (
            MappingProxyType({"binding": "unit", "suffix": "CS_PROC", "type": "PROCEDURE"}),
        )
        txn = "ddl"

    elif sc_id == "sqlcl-delivery":
        desc = (
            "Seed a two-row fixture, run an UPDATE containing a blank line before "
            "WHERE through the actual rollout path."
        )
        req_checks = (create_table_check,)
        fix_objects = (
            MappingProxyType({"binding": "table", "suffix": "SD_TAB", "type": "TABLE"}),
        )
        txn = "mixed"

    elif sc_id == "migration-success":
        desc = (
            "Generate a nullable-column structure change and apply it through "
            "the actual migration path."
        )
        req_checks = (create_table_check,)
        fix_objects = (
            MappingProxyType({"binding": "table", "suffix": "MS_TAB", "type": "TABLE"}),
        )
        txn = "mixed"

    elif sc_id == "migration-refusal":
        desc = (
            "Apply a deliberately incorrect fixture definition with a reviewed failing "
            "postcondition, then exercise the guarded retry."
        )
        req_checks = (create_table_check,)
        fix_objects = (
            MappingProxyType({"binding": "table", "suffix": "MR_TAB", "type": "TABLE"}),
        )
        txn = "mixed"

    elif sc_id == "invalid-view":
        desc = (
            "Create a fixture table/view, verify VALID, then mutate the fixture dependency."
        )
        req_checks = (create_table_check,)
        fix_objects = (
            MappingProxyType({"binding": "table", "suffix": "IV_TAB", "type": "TABLE"}),
            MappingProxyType({"binding": "view", "suffix": "IV_VIEW", "type": "VIEW"}),
        )
        txn = "ddl"

    else:
        raise ValueError(f"Unknown built-in scenario id: {sc_id!r}")

    if sc_id == "datatype-roundtrip":
        req_checks = (
            *req_checks,
            MappingProxyType({
                "id": "req-sql-boolean-23ai",
                "sql": "SELECT CASE WHEN DBMS_DB_VERSION.VERSION >= 23 THEN 1 ELSE 0 END FROM DUAL",
                "expected": 1,
            }),
            metadata_access_check("ALL_TAB_COLUMNS"),
        )
    elif sc_id == "compiler-settings":
        req_checks = (*req_checks, metadata_access_check("ALL_PLSQL_OBJECT_SETTINGS"))

    requirements = MappingProxyType({
        "objects": (),
        "checks": req_checks,
    })
    effects = MappingProxyType({
        "writes": True,
        "transaction": txn,
        "external": "none",
        "affectedObjects": ("@fixtures",),
        "backgroundConsumers": (),
    })
    fixtures = MappingProxyType({
        "objects": fix_objects,
        "selectors": (),
    })
    checks = (
        MappingProxyType({
            "id": f"{sc_id}-chk",
            "sql": "SELECT 1 FROM DUAL",
            "expected": 1,
        }),
    )

    return Scenario(
        id=sc_id,
        description=desc,
        dependsOn=(),
        requirements=requirements,
        effects=effects,
        fixtures=fixtures,
        steps=(),
        checks=checks,
        observations=(),
        cleanup=None,
    )


def builtin_suite() -> Suite:
    """Return the frozen Suite descriptor for all six built-in scenarios in declared order."""
    scenarios = tuple(_build_builtin_scenario(sc_id) for sc_id in BUILTIN_SCENARIO_IDS)
    return Suite(
        id="builtin",
        root=ROOT,
        scenarios=scenarios,
        schemaVersion=1,
        manifest_path=None,
    )


def run_builtin(
    id: str,
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    *,
    runner: Callable = run_sqlcl,
) -> dict[str, Any]:
    """Dispatch a runner-owned built-in scenario.

    Args:
        id: One of the six built-in scenario IDs.
        target: Target DEV environment descriptor.
        context: Render context with run/owner bindings.
        run: Current execution RunEvidence handle.
        runner: Injected SQLcl runner function.

    Returns:
        Standard scenario outcome dict (id, status, eligible, steps, checks, observations, effects).
    """
    if id not in BUILTIN_SCENARIO_IDS:
        raise ValueError(f"Unknown built-in scenario id: {id!r}")

    if id == "datatype-roundtrip":
        from scripts.dev_test_core_types import run_datatype_roundtrip
        return run_datatype_roundtrip(target, context, run, runner=runner)

    if id == "compiler-settings":
        from scripts.dev_test_core_types import run_compiler_settings
        return run_compiler_settings(target, context, run, runner=runner)

    from scripts import dev_test_core_migrations

    adapters = {
        "sqlcl-delivery": dev_test_core_migrations.run_sqlcl_delivery,
        "migration-success": dev_test_core_migrations.run_migration_success,
        "migration-refusal": dev_test_core_migrations.run_migration_refusal,
        "invalid-view": dev_test_core_migrations.run_invalid_view,
    }
    return adapters[id](target, context, run, runner=runner)
