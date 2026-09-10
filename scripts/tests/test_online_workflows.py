from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from teamlib.config import Config, Profile
from teamlib.online_workflows import OnlineDependencies, OnlineWorkflowError, run_integration


def config_for(*, role: str = "integration", environment: str = "staging", apps: dict[str, int] | None = None) -> Config:
    profiles = {
        name: Profile(
            name, f"{name.lower()}-connection", "APP", "APP", "FREEPDB1", "service", "INSTANCE"
        )
        for name in ("TABLES", "CODE", "APEX", "METADATA", "VERIFY")
    }
    return Config(
        values={}, profiles=profiles, apps=apps if apps is not None else {"employee": 101},
        project="team", role=role, environment=environment,
        tables_schema="APP", code_schema="APP_CODE", apex_parsing_schema="APP",
        metadata_schema="APP_META", workspace_id=90001, ownership_mode="shared",
    )


class FakeStore:
    def __init__(self, events: list[str], *, observations=None, history=None):
        self.events = events
        self.observations = list(observations or [])
        self.history = dict(history or {})

    def read_history(self, _target):
        return self.history


class OnlineWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-online-workflow-")
        self.root = Path(self.temp.name)
        self.flow_runner = self.root / "flow-runner"
        self.flow_runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.flow_runner.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def dependencies(self, events: list[str], *, head: str = "a" * 40, state=None, history=None,
                     drift_error: Exception | None = None, migration_result=None,
                     qualify_result=None, deploy_error: Exception | None = None,
                     migration_error: Exception | None = None):
        store = FakeStore(events, observations=(state or {}).get("observations", []), history=history)
        inventory = SimpleNamespace(digest="b" * 64, as_dict=lambda: {"inventory_digest": "b" * 64})
        runtime = SimpleNamespace(toolchain_digest="c" * 64)

        def resolve_head(_repo):
            return head

        def preflight(_config, _repo, _flow):
            events.append("preflight")
            return runtime

        def setup_control(_repo, _config):
            events.append("setup-control")

        def bootstrap_metadata(_repo, _config):
            events.append("bootstrap-metadata")
            return store

        def read_state(_store, _target):
            events.append("read-frontier")
            return state or {"observations": [], "attempts": {}}

        def adopt_frontier(_repo, _config, _store, _target, _before):
            events.append("adopt-frontier")

        def capture_inventory(_repo, _config, phase):
            events.append("capture-before" if phase == "integration-before" else f"capture:{phase}")
            return inventory

        def check_drift(_expected, _before):
            events.append("check-drift")
            if drift_error is not None:
                raise drift_error

        def apply_migrations(_repo, _config, _store, before_digest):
            self.assertEqual(before_digest, inventory.digest)
            events.append("migrate")
            if migration_error is not None:
                raise migration_error
            return migration_result or SimpleNamespace(confirmation_template=None)

        def deploy_apps(_repo, _config, source_commit):
            events.append("deploy:employee")
            if deploy_error is not None:
                raise deploy_error
            return (SimpleNamespace(source_commit=source_commit),)

        def qualify_integration(*args, **kwargs):
            events.append("qualify")
            self.assertEqual(kwargs["runtime_report"], runtime)
            return qualify_result or {
                "version": 2, "final_status": "PASS", "source_commit": head,
            }

        def write_report(report, out):
            events.append("write-report")
            Path(out).write_bytes(json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")

        return OnlineDependencies(
            resolve_head=resolve_head,
            preflight=preflight,
            setup_control=setup_control,
            bootstrap_metadata=bootstrap_metadata,
            read_state=read_state,
            adopt_frontier=adopt_frontier,
            capture_inventory=capture_inventory,
            check_drift=check_drift,
            apply_migrations=apply_migrations,
            deploy_apps=deploy_apps,
            qualify_integration=qualify_integration,
            verify_release=lambda *_args, **_kwargs: None,
            apply_release_live=lambda *_args, **_kwargs: None,
            qualify_release=lambda *_args, **_kwargs: None,
            write_report=write_report,
        ), store

    def test_run_integration_derives_head_and_aliases_and_orders_gates(self):
        events: list[str] = []
        dependencies, _ = self.dependencies(events)
        out = self.root / "qualification.json"
        result = run_integration(
            self.root, config_for(apps={"employee": 101}), out,
            flow_executable=str(self.flow_runner), dependencies=dependencies,
        )
        self.assertEqual(result.source_commit, "a" * 40)
        self.assertEqual(result.status, "PASS")
        self.assertEqual(
            events,
            [
                "preflight", "setup-control", "bootstrap-metadata",
                "capture-before", "read-frontier", "adopt-frontier",
                "check-drift", "migrate", "deploy:employee", "qualify", "write-report",
            ],
        )
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["source_commit"], "a" * 40)

    def test_production_refuses_before_preflight(self):
        events: list[str] = []
        dependencies, _ = self.dependencies(events)
        with self.assertRaisesRegex(OnlineWorkflowError, "non-production integration"):
            run_integration(
                self.root, config_for(role="production", environment="production"),
                self.root / "out.json", flow_executable=str(self.flow_runner), dependencies=dependencies,
            )
        self.assertEqual(events, [])

    def test_existing_frontier_skips_adoption(self):
        events: list[str] = []
        dependencies, _ = self.dependencies(events, state={"observations": [{"sequence": 0}], "attempts": {}})
        run_integration(self.root, config_for(), self.root / "out.json", flow_executable=str(self.flow_runner), dependencies=dependencies)
        self.assertNotIn("adopt-frontier", events)

    def test_history_without_frontier_refuses_before_drift(self):
        events: list[str] = []
        dependencies, _ = self.dependencies(events, history={"m1": {"status": "APPLIED"}})
        with self.assertRaisesRegex(OnlineWorkflowError, "without an observed frontier"):
            run_integration(self.root, config_for(), self.root / "out.json", flow_executable=str(self.flow_runner), dependencies=dependencies)
        self.assertNotIn("adopt-frontier", events)
        self.assertNotIn("check-drift", events)

    def test_drift_refuses_before_migration_and_deployment(self):
        events: list[str] = []
        dependencies, _ = self.dependencies(events, state={"observations": [{"sequence": 0}], "attempts": {}}, drift_error=OnlineWorkflowError("drift"))
        with self.assertRaisesRegex(OnlineWorkflowError, "drift"):
            run_integration(self.root, config_for(), self.root / "out.json", flow_executable=str(self.flow_runner), dependencies=dependencies)
        self.assertNotIn("migrate", events)
        self.assertNotIn("deploy:employee", events)

    def test_destructive_preview_returns_maintenance_required_without_payload(self):
        events: list[str] = []
        template = {"version": 1, "requirements": [{"migration_id": "m1", "action": "migrate"}]}
        dependencies, _ = self.dependencies(
            events, state={"observations": [{"sequence": 0}], "attempts": {}},
            migration_result=SimpleNamespace(confirmation_template=template),
        )
        result = run_integration(self.root, config_for(), self.root / "out.json", flow_executable=str(self.flow_runner), dependencies=dependencies)
        self.assertEqual(result.status, "maintenance-required")
        self.assertEqual(result.confirmation_template, template)
        self.assertNotIn("deploy:employee", events)
        self.assertNotIn("qualify", events)

    def test_failed_migration_or_deployment_stops_later_steps(self):
        for kwargs, absent in (
            ({"migration_error": OnlineWorkflowError("migration failed")}, "deploy:employee"),
            ({"deploy_error": OnlineWorkflowError("deployment failed")}, "qualify"),
        ):
            with self.subTest(absent=absent):
                events: list[str] = []
                dependencies, _ = self.dependencies(events, state={"observations": [{"sequence": 0}], "attempts": {}}, **kwargs)
                with self.assertRaises(OnlineWorkflowError):
                    run_integration(self.root, config_for(), self.root / "out.json", flow_executable=str(self.flow_runner), dependencies=dependencies)
                self.assertNotIn(absent, events)

    def test_qualification_failure_writes_structured_evidence(self):
        from teamlib.qualification import QualificationError

        events: list[str] = []
        evidence = {"version": 2, "final_status": "FAIL", "results": {"application_checks": "FAIL"}}
        error = QualificationError("application check failed", evidence)
        dependencies, _ = self.dependencies(events, state={"observations": [{"sequence": 0}], "attempts": {}}, qualify_result=None)
        dependencies = OnlineDependencies(
            **{**dependencies.__dict__, "qualify_integration": lambda *args, **kwargs: (_ for _ in ()).throw(error)}
        )
        out = self.root / "failure.json"
        with self.assertRaises(QualificationError):
            run_integration(self.root, config_for(), out, flow_executable=str(self.flow_runner), dependencies=dependencies)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8")), evidence)
        self.assertEqual(events[-1], "write-report")


if __name__ == "__main__":
    unittest.main()
