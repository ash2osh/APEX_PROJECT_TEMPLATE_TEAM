"""Public DEV test planning, execution, reporting, and optional cleanup CLI."""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.db_targets import Target, resolve_target
from scripts.dev_test_bindings import RenderContext
from scripts.dev_test_cleanup import cleanup_run
from scripts.dev_test_core import BUILTIN_SCENARIO_IDS, builtin_suite, run_builtin
from scripts.dev_test_evidence import RunEvidence, create_run, open_run
from scripts.dev_test_manifest import (
    DevTestManifestError,
    Scenario,
    Suite,
    load_suite,
    select_scenarios,
)
from scripts.dev_test_report import (
    aggregate_exit,
    new_report,
    render_report,
    save_report,
    validate_report,
)
from scripts.dev_test_runtime import execute_project_scenario
from scripts.dev_test_target import (
    DevTestTargetError,
    inspect_suite,
    verify_saved_target_identity,
)
from scripts.local_config import ConfigError, read_project_env
from scripts.sqlcl_session import run_sqlcl

ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
_READ_ONLY_RUN_EVENTS = frozenset({"plan", "read", "observation", "fixture-precreation", "fixture-observed"})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/team.sh dev-test",
        description="Plan and run explicit DEV qualification scenarios; fixtures are retained by default.",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--plan", action="store_true", help="inspect DEV and write a read-only plan")
    actions.add_argument("--run", action="store_true", help="execute the selected suite and retain its fixtures")
    actions.add_argument("--report", metavar="RUN_ID", help="render a saved run report without connecting")
    actions.add_argument("--cleanup", metavar="RUN_ID", help="explicitly clean supported artifacts from a saved run")
    parser.add_argument("--env", help="must be dev for planning and execution")
    parser.add_argument("--schema", help="select one configured Oracle schema")
    parser.add_argument("--suite", type=Path, help="project suite JSON; omitted means all six built-in scenarios")
    parser.add_argument("--scenario", action="append", default=[], help="select a scenario (repeatable)")
    parser.add_argument("--format", choices=("text", "json", "markdown"), default="text")
    return parser


def _selected_action(args: argparse.Namespace) -> str | None:
    if args.plan:
        return "plan"
    if args.run:
        return "run"
    if args.report is not None:
        return "report"
    if args.cleanup is not None:
        return "cleanup"
    return None


def _validate_action_args(parser: argparse.ArgumentParser, args: argparse.Namespace, action: str) -> None:
    if action in {"plan", "run"}:
        if args.env != "dev":
            parser.error("--plan and --run require an explicit --env dev")
        if args.schema is not None and _SCHEMA_RE.fullmatch(args.schema) is None:
            parser.error("--schema must be an uppercase Oracle identifier")
        return
    if args.env is not None or args.schema is not None or args.suite is not None or args.scenario:
        parser.error(f"--{action} uses the saved run target and does not accept --env, --schema, --suite, or --scenario")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _project_env_path() -> Path:
    configured = os.environ.get("PROJECT_ENV_FILE")
    return Path(configured) if configured else ROOT / ".env"


def _scenario_record(scenario: Scenario, planned: Mapping[str, Any], status: str) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": scenario.id,
        "status": status,
        "eligible": bool(planned.get("eligible", False)),
        "steps": [dict(item) for item in planned.get("steps", []) if isinstance(item, Mapping)],
        "checks": [dict(item) for item in planned.get("checks", []) if isinstance(item, Mapping)],
        "observations": [dict(item) for item in planned.get("observations", []) if isinstance(item, Mapping)],
        "effects": dict(planned.get("effects", {})) if isinstance(planned.get("effects"), Mapping) else {},
    }
    for key in ("plannedObjects", "collisions"):
        if key in planned:
            record[key] = copy.deepcopy(planned[key])
    return record


def _has_write_started(run: RunEvidence) -> bool:
    """Read explicit write-intent evidence; uncertain or corrupt events fail closed."""
    if not run.events_dir.exists():
        return False
    for event_path in run.events_dir.glob("*.json"):
        try:
            event = json.loads(event_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return True
        if not isinstance(event, Mapping):
            return True
        if event.get("writeStarted") is True:
            return True
        if event.get("type") not in _READ_ONLY_RUN_EVENTS:
            return True
    return False


def _event_names(run: RunEvidence) -> set[str]:
    if not run.events_dir.exists():
        return set()
    return {path.name for path in run.events_dir.glob("*.json")}


def _has_new_write_started(run: RunEvidence, prior_event_names: set[str]) -> bool:
    """Classify whether this scenario added write intent after its identity check began."""
    if not run.events_dir.exists():
        return False
    for event_path in run.events_dir.glob("*.json"):
        if event_path.name in prior_event_names:
            continue
        try:
            event = json.loads(event_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return True
        if not isinstance(event, Mapping):
            return True
        if event.get("writeStarted") is True or event.get("type") not in _READ_ONLY_RUN_EVENTS:
            return True
    return False


def _read_saved_plan(run: RunEvidence) -> dict[str, Any]:
    plan_path = run.run_dir / "plan.json"
    if plan_path.is_symlink() or not plan_path.is_file():
        raise ValueError("Saved suite plan is missing or unsafe")
    try:
        value = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Saved suite plan is unreadable") from error
    validate_report(value)
    if value.get("action") != "plan" or value.get("runId") != run.run_id:
        raise ValueError("Saved suite plan identity is invalid")
    return value


def _read_fixture_inventory(run: RunEvidence, report: dict[str, Any]) -> None:
    path = run._inventory_path
    if not path.exists():
        return
    if path.is_symlink() or not path.is_file():
        report["coverage"]["complete"] = False
        report["diagnostics"]["inventory"] = "retained fixture inventory is not a regular file"
        return
    try:
        inventory = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        report["coverage"]["complete"] = False
        report["diagnostics"]["inventory"] = "retained fixture inventory could not be read"
        return
    if not isinstance(inventory, Mapping) or not isinstance(inventory.get("objects", []), list) or not isinstance(inventory.get("rows", []), list):
        report["coverage"]["complete"] = False
        report["diagnostics"]["inventory"] = "retained fixture inventory is malformed"
        return
    report["inventory"] = {
        "objects": copy.deepcopy(inventory.get("objects", [])),
        "rows": copy.deepcopy(inventory.get("rows", [])),
    }


def execute_suite(
    suite: Suite,
    target: Target,
    context: RenderContext,
    run: RunEvidence,
    *,
    runner: Callable = run_sqlcl,
) -> dict[str, Any]:
    """Execute eligible scenarios in declaration order and preserve every result.

    A fresh full identity observation precedes each eligible scenario. If the
    identity changes or becomes unverifiable, remaining writes stop. Independent
    scenarios continue after a scenario failure; dependencies require PASSED.
    Cleanup is deliberately never part of this ordinary execution path.
    """
    plan = _read_saved_plan(run)
    planned_by_id = {
        item.get("id"): item
        for item in plan.get("scenarios", [])
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    if list(planned_by_id) != [scenario.id for scenario in suite.scenarios]:
        raise ValueError("Saved plan scenario order differs from the selected suite")
    report = copy.deepcopy(plan)
    report["action"] = "run"
    report["startedAt"] = _now()
    report["finishedAt"] = None
    report["coverage"] = {"complete": True, "eligible": True}
    report["cleanup"] = {"status": "NOT_REQUESTED", "history": []}
    report["diagnostics"] = {}
    report["scenarios"] = []
    outcomes: dict[str, dict[str, Any]] = {}
    for scenario in suite.scenarios:
        planned = planned_by_id[scenario.id]
        initial_status = planned.get("status")
        if planned.get("eligible") is not True:
            initial_status = "BLOCKED" if initial_status == "BLOCKED" else "NOT_RUN"
        else:
            initial_status = "NOT_RUN"
        record = _scenario_record(scenario, planned, initial_status)
        report["scenarios"].append(record)
        outcomes[scenario.id] = record

    def finish_interrupted(
        current_id: str | None = None,
        prior_event_names: set[str] | None = None,
    ) -> dict[str, Any]:
        write_started = _has_write_started(run)
        scenario_write_started = (
            current_id is not None
            and prior_event_names is not None
            and _has_new_write_started(run, prior_event_names)
        )
        if scenario_write_started and outcomes[current_id]["status"] == "NOT_RUN":
            outcomes[current_id]["status"] = "UNKNOWN"
        report["diagnostics"] = {"interrupted": True, "writeStarted": write_started}
        report["finishedAt"] = _now()
        return report

    expected_identity = report.get("target", {}).get("observed")
    if not isinstance(expected_identity, Mapping):
        report["diagnostics"] = {"targetIdentity": "saved plan has no observed identity"}
        report["finishedAt"] = _now()
        return report

    for scenario in suite.scenarios:
        record = outcomes[scenario.id]
        if not record.get("eligible") or record["status"] == "BLOCKED":
            continue
        if any(outcomes.get(dependency, {}).get("status") != "PASSED" for dependency in scenario.dependsOn):
            record["status"] = "NOT_RUN"
            record["eligible"] = False
            record["reason"] = "a required scenario did not pass"
            continue
        prior_event_names = _event_names(run)
        try:
            verify_saved_target_identity(
                target,
                expected_identity,
                run,
                runner=runner,
            )
        except KeyboardInterrupt:
            return finish_interrupted(scenario.id, prior_event_names)
        except Exception as error:
            report["diagnostics"] = {
                "targetIdentity": "fresh identity check failed; no later scenarios were dispatched",
                "errorType": type(error).__name__,
            }
            break

        planned_objects = record.get("plannedObjects", {})
        if not isinstance(planned_objects, Mapping):
            record["status"] = "UNKNOWN"
            record["reason"] = "planned fixture object bindings are malformed"
            continue
        scenario_context = RenderContext(
            target.schema,
            run.run_id,
            planned_objects,
            context.values,
        )
        intent_before = _has_write_started(run)
        try:
            if suite.id == "builtin" and suite.manifest_path is None and scenario.id in BUILTIN_SCENARIO_IDS:
                outcome = run_builtin(scenario.id, target, scenario_context, run, runner=runner)
            else:
                outcome = execute_project_scenario(target, scenario, scenario_context, run, runner=runner)
        except KeyboardInterrupt:
            return finish_interrupted(scenario.id, prior_event_names)
        except Exception as error:
            record["status"] = "UNKNOWN" if (_has_write_started(run) and not intent_before) else "BLOCKED"
            record.setdefault("steps", []).append({
                "id": "scenario-dispatch",
                "status": record["status"],
                "errorType": type(error).__name__,
                "writeStarted": record["status"] == "UNKNOWN",
            })
            record["reason"] = "scenario dispatch ended without a verified result"
            continue
        if not isinstance(outcome, Mapping):
            record["status"] = "UNKNOWN"
            record["reason"] = "scenario returned no structured outcome"
            continue
        outcome_status = outcome.get("status")
        if outcome_status not in {"PASSED", "FAILED", "BLOCKED", "NOT_RUN", "UNKNOWN"}:
            outcome_status = "UNKNOWN"
        for key in ("steps", "checks", "observations"):
            value = outcome.get(key, [])
            if not isinstance(value, list):
                value = []
            record[key] = copy.deepcopy(value)
        record["effects"] = copy.deepcopy(outcome.get("effects", record["effects"]))
        record["status"] = outcome_status
        record["eligible"] = bool(outcome.get("eligible", record["eligible"]))
        for key, value in outcome.items():
            if key not in {"id", "status", "eligible", "steps", "checks", "observations", "effects"}:
                record[key] = copy.deepcopy(value)

    _read_fixture_inventory(run, report)
    report["finishedAt"] = _now()
    return report


def _create_refusal_report(
    run: RunEvidence,
    suite: Suite,
    action: str,
    origin: str,
    selected: Sequence[Scenario],
    error: Exception,
) -> dict[str, Any]:
    report = new_report(run, action, suite.id, [scenario.id for scenario in selected], origin)
    report["finishedAt"] = _now()
    report["diagnostics"] = {
        "refused": True,
        "errorType": type(error).__name__,
    }
    report["coverage"] = {"complete": False, "eligible": False}
    report["scenarios"] = [
        _scenario_record(scenario, {}, "NOT_RUN")
        for scenario in selected
    ]
    return report


def _render_cleanup(result: Mapping[str, Any], format: str) -> str:
    if format == "json":
        return json.dumps(dict(result), indent=2, default=str) + "\n"
    status = result.get("status", "UNKNOWN")
    run_id = result.get("runId", "unknown")
    if format == "markdown":
        lines = ["# DEV Test Cleanup", "", f"**Run ID:** `{run_id}`", f"**Status:** {status}"]
        artifacts = result.get("artifacts", [])
        if isinstance(artifacts, list) and artifacts:
            lines.extend(("", "| Artifact | Status |", "|---|---|"))
            for item in artifacts:
                if isinstance(item, Mapping):
                    label = ".".join(str(item.get(key, "")) for key in ("owner", "name", "type"))
                    lines.append(f"| {label} | {item.get('status', 'UNKNOWN')} |")
        return "\n".join(lines) + "\n"
    return f"Cleanup {run_id}: {status}\n"


def _report_saved_run(run_id: str, format: str) -> int:
    run = open_run(ROOT, run_id)
    report_path = run.run_dir / "report.json"
    if report_path.is_symlink() or not report_path.is_file():
        raise ValueError("Saved run report is missing or unsafe")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    validate_report(report)
    print(render_report(report, format), end="")
    return aggregate_exit(report)


def _cleanup_saved_run(run_id: str, format: str) -> int:
    run = open_run(ROOT, run_id)
    values = read_project_env(_project_env_path())
    result = cleanup_run(run, values)
    print(_render_cleanup(result, format), end="")
    report_path = run.run_dir / "report.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        validate_report(report)
        return aggregate_exit(report)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = list(sys.argv[1:]) if argv is None else list(argv)
    args = parser.parse_args(arguments)
    action = _selected_action(args)
    if action is None:
        if not arguments:
            parser.print_usage()
            return 0
        parser.error("choose exactly one of --plan, --run, --report, or --cleanup")
    _validate_action_args(parser, args, action)

    if action == "report":
        try:
            return _report_saved_run(args.report, args.format)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            print(f"dev-test report error: {type(error).__name__}", file=sys.stderr)
            return 2
    if action == "cleanup":
        try:
            return _cleanup_saved_run(args.cleanup, args.format)
        except (ConfigError, DevTestTargetError, OSError, ValueError, RuntimeError) as error:
            print(f"dev-test cleanup error: {type(error).__name__}", file=sys.stderr)
            return 2

    run: RunEvidence | None = None
    suite: Suite | None = None
    selected: Sequence[Scenario] = ()
    origin = "builtin"
    try:
        if args.suite is None:
            suite = builtin_suite()
        else:
            suite = load_suite(args.suite)
            origin = "project"
        selected = select_scenarios(suite, args.scenario)
        suite = Suite(
            id=suite.id,
            root=suite.root,
            scenarios=selected,
            schemaVersion=suite.schemaVersion,
            manifest_path=suite.manifest_path,
        )
        values = read_project_env(_project_env_path())
        run = create_run(ROOT)
        target = resolve_target(values, "dev", "migration", schema=args.schema)
        plan_report = inspect_suite(values, args.schema, suite, run, origin=origin)
        if action == "plan":
            save_report(run, plan_report)
            print(render_report(plan_report, args.format), end="")
            return aggregate_exit(plan_report)

        plan_copy = copy.deepcopy(plan_report)
        # The execution orchestrator consumes this persisted, run-owned plan;
        # it never trusts a plan from an earlier invocation.
        from scripts.dev_test_evidence import _atomic_write_json
        _atomic_write_json(run.run_dir / "plan.json", plan_copy)
        context = RenderContext(target.schema, run.run_id, {}, {})
        report = execute_suite(suite, target, context, run)
        save_report(run, report)
        print(render_report(report, args.format), end="")
        return aggregate_exit(report)
    except KeyboardInterrupt as error:
        if run is None or suite is None:
            print("dev-test interrupted before a run could be recorded", file=sys.stderr)
            return 130
        write_started = _has_write_started(run)
        report = _create_refusal_report(run, suite, action, origin, selected, error)
        report["diagnostics"] = {"interrupted": True, "writeStarted": write_started}
        if write_started:
            for scenario_report in report["scenarios"]:
                scenario_report["status"] = "UNKNOWN"
                scenario_report["eligible"] = True
        try:
            save_report(run, report)
            print(render_report(report, args.format), end="")
        except (OSError, ValueError, TypeError):
            print("dev-test interrupted; report persistence failed", file=sys.stderr)
            return 2
        return aggregate_exit(report)
    except (ConfigError, DevTestManifestError, DevTestTargetError, OSError, ValueError, RuntimeError) as error:
        if run is None or suite is None:
            print(f"dev-test error: {type(error).__name__}", file=sys.stderr)
            return 2
        report = _create_refusal_report(run, suite, action, origin, selected, error)
        try:
            save_report(run, report)
            print(render_report(report, args.format), end="")
        except (OSError, ValueError, TypeError):
            print("dev-test refused; report persistence failed", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - exercised through the public wrapper
    raise SystemExit(main())


__all__ = ["build_parser", "execute_suite", "main"]
