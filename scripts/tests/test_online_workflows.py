from __future__ import annotations

import json
import re
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from unittest.mock import patch

import team
from teamlib.app_checks import AppCheckReport, build_app_check_bundle
from teamlib.config import Config, Profile
from teamlib.online_workflows import (
    OnlineDependencies,
    OnlineWorkflowError,
    run_integration,
    run_release_test,
)
from teamlib.qualification import qualify_target
from teamlib.release import ApplyReport, Manifest


def config_for(*, role: str = "integration", environment: str = "staging", apps: dict[str, int] | None = None) -> Config:
    profiles = {
        name: Profile(
            name, f"{name.lower()}-connection", "APP", "APP", "FREEPDB1", "service", "INSTANCE"
        )
        for name in ("TABLES", "CODE", "APEX", "METADATA", "VERIFY")
    }
    resolved_apps = apps if apps is not None else {"employee": 101}
    return Config(
        values={}, profiles=profiles, apps=resolved_apps,
        app_parsing_schemas={alias: "APP" for alias in resolved_apps},
        project="team", role=role, environment=environment,
        tables_schema="APP", code_schema="APP_CODE",
        metadata_schema="APP_META", workspace_id=90001, ownership_mode="shared",
    )


def app_check_bundle():
    declaration = {
        "version": 1,
        "alias": "employee",
        "page_ids": [1],
        "checks": [
            {
                "id": "objects",
                "page_id": 1,
                "kind": "select",
                "verify_sql": "employee/objects.verify.sql",
                "expected_objects": ["APP.T"],
            },
            {
                "id": "home",
                "page_id": 1,
                "kind": "flow",
                "flow": "employee/home.flow.json",
                "steps": [
                    {
                        "action": "navigate",
                        "path": "/ords/r/app/employee/home",
                        "expected_visible_text": "Employee",
                    }
                ],
            },
        ],
    }
    return build_app_check_bundle(
        {
            "employee.json": json.dumps(declaration).encode("utf-8"),
            "employee/objects.verify.sql": (
                b"SELECT 'TEAM_ASSERT|' || assertion_name || '|' || status AS status "
                b"FROM (SELECT 'objects' assertion_name, 'PASS' status FROM dual);\n"
            ),
            "employee/home.flow.json": b"{}\n",
        },
        ("employee",),
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
                     migration_error: Exception | None = None,
                     second_head: str | None = None,
                     later_head: str | None = None):
        store = FakeStore(events, observations=(state or {}).get("observations", []), history=history)
        inventory = SimpleNamespace(digest="b" * 64, as_dict=lambda: {"inventory_digest": "b" * 64})
        runtime = SimpleNamespace(toolchain_digest="c" * 64)
        bundle = app_check_bundle()
        source = SimpleNamespace(
            commit=head,
            canonical_inventory=inventory,
            app_trees={"employee": {"application.apx": b"committed app\n"}},
            check_bundle=bundle,
        )
        resolved = 0

        def resolve_head(_repo):
            nonlocal resolved
            resolved += 1
            if resolved > 2 and later_head is not None:
                return later_head
            return second_head if resolved > 1 and second_head is not None else head

        def load_source(_repo, commit, aliases):
            events.append("load-source")
            self.assertEqual(commit, head)
            self.assertEqual(tuple(aliases), ("employee",))
            return source

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

        def check_drift(expected, _before):
            self.assertIs(expected, source.canonical_inventory)
            events.append("check-drift")
            if drift_error is not None:
                raise drift_error

        def apply_migrations(_repo, _config, _store, before_digest, selected_source):
            self.assertEqual(before_digest, inventory.digest)
            self.assertIs(selected_source, source)
            events.append("migrate")
            if migration_error is not None:
                raise migration_error
            return migration_result or SimpleNamespace(confirmation_template=None)

        def deploy_apps(_repo, _config, selected_source):
            events.append("deploy:employee")
            self.assertIs(selected_source, source)
            if deploy_error is not None:
                raise deploy_error
            return (SimpleNamespace(source_commit=selected_source.commit),)

        def qualify_integration(*args, **kwargs):
            events.append("qualify")
            self.assertEqual(kwargs["runtime_report"], runtime)
            self.assertIs(kwargs["check_bundle"], source.check_bundle)
            return qualify_result or {
                "version": 2, "final_status": "PASS", "source_commit": head,
            }

        def write_report(report, out):
            events.append("write-report")
            Path(out).write_bytes(json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")

        return OnlineDependencies(
            resolve_head=resolve_head,
            load_source=load_source,
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
            release_app_checks=lambda *_args, **_kwargs: app_check_bundle(),
            apply_release_live=lambda *_args, **_kwargs: None,
            qualify_release=lambda *_args, **_kwargs: None,
            write_report=write_report,
            hold_release_apps=self.hold_release_apps,
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
                "load-source", "preflight", "setup-control", "bootstrap-metadata",
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

    def test_run_integration_translates_preflight_runtime_errors(self):
        for message in (
            "SQLcl executable is unavailable",
            "TEAM_FLOW_RUNNER is required for declared flow checks",
            "profile identity probe does not match TABLES",
            "observed sqlcl version 25.1 does not satisfy required 26.2.1+",
        ):
            with self.subTest(message=message):
                events: list[str] = []

                def failing_preflight(_config, _repo, _flow, _message=message, _events=events):
                    _events.append("preflight")
                    raise RuntimeError(_message)

                dependencies, _ = self.dependencies(events)
                dependencies = OnlineDependencies(**{**dependencies.__dict__, "preflight": failing_preflight})
                with self.assertRaisesRegex(OnlineWorkflowError, re.escape(message)):
                    run_integration(
                        self.root, config_for(), self.root / "out.json",
                        flow_executable=str(self.flow_runner), dependencies=dependencies,
                    )
                self.assertEqual(events, ["load-source", "preflight"])

    def test_head_change_before_first_write_refuses(self):
        events: list[str] = []
        dependencies, _ = self.dependencies(events, second_head="b" * 40)
        with self.assertRaisesRegex(OnlineWorkflowError, "HEAD changed"):
            run_integration(
                self.root,
                config_for(),
                self.root / "out.json",
                flow_executable=str(self.flow_runner),
                dependencies=dependencies,
            )
        self.assertEqual(events, ["load-source", "preflight"])

    def test_later_head_change_is_reported_without_changing_loaded_source(self):
        events: list[str] = []
        dependencies, _ = self.dependencies(events, later_head="b" * 40)

        result = run_integration(
            self.root,
            config_for(),
            self.root / "out.json",
            flow_executable=str(self.flow_runner),
            dependencies=dependencies,
        )

        self.assertEqual(result.status, "PASS")
        self.assertIn("changed after source capture", result.checkout_diagnostic)
        self.assertEqual(result.source_commit, "a" * 40)

    def test_run_release_test_translates_preflight_runtime_errors(self):
        events: list[str] = []

        def failing_preflight(_config, _repo, _flow, **_kwargs):
            events.append("preflight")
            raise RuntimeError("JDK executable is unavailable")

        dependencies, manifest = self.release_dependencies(events)
        dependencies = OnlineDependencies(**{**dependencies.__dict__, "preflight": failing_preflight})
        archive = self.root / "release.tar"
        archive.write_bytes(b"verified archive")
        target = self.root / "targets" / "test.json"
        target.parent.mkdir()
        target.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(OnlineWorkflowError, "JDK executable is unavailable"):
            run_release_test(
                self.root, config_for(role="test", environment="test"), archive, target,
                self.root / "out.json", flow_executable=str(self.flow_runner), dependencies=dependencies,
            )
        self.assertEqual(events, ["verify-archive", "load-check-bundle", "preflight"])

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

    def release_dependencies(self, events: list[str], *, manifest=None, apply_error=None, qualify_error=None):
        manifest = manifest or Manifest(
            1, "1.0.0", "a" * 40, "b" * 64, (), {"employee": "c" * 64},
            (), self.root / "release.tar", "d" * 64,
        )
        runtime = SimpleNamespace(toolchain_digest="e" * 64)
        apply_report = ApplyReport(
            "applied", (), archive_digest=manifest.archive_digest,
            source_commit=manifest.source_commit, target_state_key="f" * 64,
            target_digest="1" * 64, history_digest="2" * 64,
        )
        bundle = app_check_bundle()

        def verify_release(archive):
            events.append("verify-archive")
            return manifest

        def release_app_checks(archive):
            events.append("load-check-bundle")
            return bundle

        def preflight(_config, _repo, _flow, *, require_flow_runner=True):
            events.append("preflight")
            self.__dict__.setdefault("release_preflight_flow_requirements", []).append(
                require_flow_runner
            )
            return runtime

        def apply_live(archive, target, config, **kwargs):
            events.append("read-live-history")
            if apply_error is not None:
                raise apply_error
            return apply_report

        def qualify(*args, **kwargs):
            events.append("qualify-release")
            if qualify_error is not None:
                raise qualify_error
            self.assertEqual(kwargs["runtime_report"], runtime)
            self.assertEqual(kwargs["apply_report"], apply_report.as_dict())
            self.assertIs(kwargs["check_bundle"], bundle)
            return {"version": 2, "final_status": "PASS", "source_commit": manifest.source_commit}

        def write_report(report, out):
            events.append("write-report")
            Path(out).write_bytes(json.dumps(report, sort_keys=True, separators=(",", ":")).encode() + b"\n")

        dependencies = OnlineDependencies(
            resolve_head=lambda _repo: manifest.source_commit,
            load_source=lambda *_args: None,
            preflight=preflight,
            setup_control=lambda *_args: None,
            bootstrap_metadata=lambda *_args: None,
            read_state=lambda *_args: {"observations": []},
            adopt_frontier=lambda *_args: "",
            capture_inventory=lambda *_args: None,
            check_drift=lambda *_args: None,
            apply_migrations=lambda *_args: None,
            deploy_apps=lambda *_args: (),
            qualify_integration=lambda *_args, **_kwargs: {},
            verify_release=verify_release,
            release_app_checks=release_app_checks,
            apply_release_live=apply_live,
            qualify_release=qualify,
            write_report=write_report,
            hold_release_apps=self.hold_release_apps,
        )
        return dependencies, manifest

    @contextmanager
    def hold_release_apps(self, _repo, _config, expected, required=(), verify_history=None):
        """Record which app digests and prerequisites a release test holds during qualification."""
        holds = self.__dict__.setdefault("holds", [])
        holds.append({"expected": dict(expected), "open": True})
        self.__dict__.setdefault("held_required", []).append(tuple(required))
        self.__dict__.setdefault("held_history_checks", []).append(verify_history)
        try:
            yield
        finally:
            holds[-1]["open"] = False

    def test_release_test_reads_live_history_and_emits_evidence_without_plan_files(self):
        events: list[str] = []
        dependencies, manifest = self.release_dependencies(events)
        archive = self.root / "release.tar"
        archive.write_bytes(b"verified archive")
        target = self.root / "targets" / "test.json"
        target.parent.mkdir()
        target.write_text("{}\n", encoding="utf-8")
        out = self.root / "test-evidence.json"
        result = run_release_test(
            self.root,
            config_for(role="test", environment="test", apps={"employee": 201}),
            archive,
            target,
            out,
            flow_executable=str(self.flow_runner),
            dependencies=dependencies,
        )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(result.source_commit, manifest.source_commit)
        self.assertIn("read-live-history", events)
        self.assertNotIn("read-history-file", events)
        self.assertFalse((self.root / "plan.json").exists())
        self.assertFalse((self.root / "apply-report.json").exists())
        self.assertEqual(self.release_preflight_flow_requirements, [True])

    def test_release_test_passes_format3_app_source_and_selected_alias_to_qualification(self):
        source = {
            "kind": "dev-database",
            "instance_id": "DEV1",
            "history_cut": 1,
            "history_digest": "a" * 64,
            "frontier_digest": "b" * 64,
            "app_generation": 7,
            "app_tree_digest": "c" * 64,
            "app_checks_digest": app_check_bundle().checks_digest,
            "master_contract_digest": "e" * 64,
        }
        manifest = SimpleNamespace(
            format_version=3,
            kind="app",
            alias="employee",
            source_commit="",
            source=source,
            archive_digest="d" * 64,
            app_tree_digests={"employee": "c" * 64},
        )
        events: list[str] = []
        dependencies, _ = self.release_dependencies(events, manifest=manifest)
        forwarded = []
        forwarded_confirmation = []
        def capture_source(repo, config, source_identity, aliases, **kwargs):
            forwarded.append((source_identity, tuple(aliases)))
            return {
                "version": 3,
                "final_status": "PASS",
                "source": dict(source_identity),
            }

        original_apply = dependencies.apply_release_live

        def capture_confirmation(*args, **kwargs):
            forwarded_confirmation.append(kwargs.get("confirmation"))
            return original_apply(*args, **kwargs)

        dependencies = OnlineDependencies(
            **{
                **dependencies.__dict__,
                "qualify_release": capture_source,
                "apply_release_live": capture_confirmation,
            }
        )
        archive = self.root / "format3-release.tar"
        archive.write_bytes(b"verified database release")
        target = self.root / "targets" / "test.json"
        target.parent.mkdir()
        target.write_text("{}\n", encoding="utf-8")
        confirmation = {"version": 1, "confirmations": [{"migration_id": "m1"}]}
        result = run_release_test(
            self.root,
            config_for(role="test", environment="test", apps={"employee": 201}),
            archive,
            target,
            self.root / "format3-evidence.json",
            flow_executable=str(self.flow_runner),
            dependencies=dependencies,
            confirmation=confirmation,
        )
        self.assertEqual(forwarded, [(source, ("employee",))])
        self.assertEqual(forwarded_confirmation, [confirmation])
        self.assertEqual(result.source_commit, "")
        self.assertEqual(result.as_dict()["source"], source)
        self.assertNotIn("source_commit", result.as_dict())

    def test_release_test_qualification_validates_a_real_apply_report_through_qualify_target(self):
        from teamlib.config import profile_target

        config = config_for(role="test", environment="test")
        metadata = profile_target(config, "METADATA")
        manifest = Manifest(
            1, "1.0.0", "a" * 40, "b" * 64, (), {"employee": "c" * 64},
            (), self.root / "release.tar", "d" * 64,
        )
        real_apply_report = ApplyReport(
            "applied", (), archive_digest=manifest.archive_digest,
            source_commit=manifest.source_commit, target_state_key=metadata.state_key,
            target_digest="1" * 64, history_digest="2" * 64,
        )
        runtime = SimpleNamespace(toolchain_digest="e" * 64)
        (self.root / "ci" / "app-checks").mkdir(parents=True)
        (self.root / "ci" / "app-checks" / "employee.json").write_text(
            json.dumps({"version": 1, "alias": "employee", "page_ids": [1], "checks": [
                {"id": "objects", "page_id": 1, "kind": "select", "verify_sql": "employee/objects.verify.sql",
                 "expected_objects": ["APP.T"], "sql": "SELECT 'objects' assertion_name, 'PASS' status FROM dual"},
            ]}),
            encoding="utf-8",
        )

        class FakeMetadataStore:
            def validate_observation_chain(self, target):
                return None

            def read_history(self, target):
                return {"m1": {"status": "APPLIED", "checksum": "a" * 64, "sequence": 2}}

            def read_state(self, target):
                return {"attempts": {}, "observations": [{"sequence": 3, "after": "c" * 64}]}

        bundle = app_check_bundle()

        def real_qualify_release(repo, config, source_commit, aliases, *, release_archive, apply_report, check_bundle, flow_executable, runtime_report):
            fake_app = AppCheckReport(
                source_commit, {"target_kind": "persistent"}, (), "a" * 64, check_bundle.checks_digest,
                {"apps": list(aliases), "checks": 1, "unknown": 0}, "PASS",
            )
            with patch("teamlib.qualification.verify_candidate_apps", return_value=fake_app), \
                    patch("teamlib.qualification.verify_release", return_value=manifest):
                return qualify_target(
                    repo, config, source_commit, aliases,
                    store=FakeMetadataStore(), work=self.root / "qualify-work",
                    release_archive=release_archive, apply_report=apply_report,
                    check_bundle=check_bundle,
                    flow_executable=flow_executable, runner_contract=Path("ci/runner-contract.json"),
                    runtime_report=runtime_report, sql_runner=lambda *args, **kwargs: object(),
                )

        dependencies = OnlineDependencies(
            resolve_head=lambda _repo: manifest.source_commit,
            load_source=lambda *_args: None,
            preflight=lambda *_args, **_kwargs: runtime,
            setup_control=lambda *_args: None,
            bootstrap_metadata=lambda *_args: None,
            read_state=lambda *_args: {"observations": []},
            adopt_frontier=lambda *_args: "",
            capture_inventory=lambda *_args: None,
            check_drift=lambda *_args: None,
            apply_migrations=lambda *_args: None,
            deploy_apps=lambda *_args: (),
            qualify_integration=lambda *_args, **_kwargs: {},
            verify_release=lambda _archive: manifest,
            release_app_checks=lambda _archive: bundle,
            apply_release_live=lambda *_args, **_kwargs: real_apply_report,
            qualify_release=real_qualify_release,
            write_report=lambda report, out: Path(out).write_bytes(
                json.dumps(report, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            ),
            hold_release_apps=self.hold_release_apps,
        )
        archive = self.root / "release.tar"
        archive.write_bytes(b"verified archive")
        target = self.root / "targets" / "test.json"
        target.parent.mkdir()
        target.write_text("{}\n", encoding="utf-8")
        out = self.root / "test-evidence.json"

        result = run_release_test(
            self.root, config, archive, target, out,
            flow_executable=str(self.flow_runner), dependencies=dependencies,
        )

        self.assertEqual(result.status, "PASS")
        written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(written["archive_digest"], manifest.archive_digest)

    def test_release_test_requires_exact_test_target_and_matching_archive_aliases(self):
        events: list[str] = []
        dependencies, _ = self.release_dependencies(events)
        with self.assertRaisesRegex(OnlineWorkflowError, "protected test target"):
            run_release_test(
                self.root,
                config_for(),
                self.root / "release.tar",
                self.root / "target.json",
                self.root / "out.json",
                flow_executable=str(self.flow_runner), dependencies=dependencies,
            )
        self.assertEqual(events, [])

        mismatch_manifest = Manifest(
            1, "1.0.0", "a" * 40, "b" * 64, (), {"other": "c" * 64},
            (), self.root / "release.tar", "d" * 64,
        )
        events.clear()
        dependencies, _ = self.release_dependencies(events, manifest=mismatch_manifest)
        with self.assertRaisesRegex(OnlineWorkflowError, "application bindings differ"):
            run_release_test(
                self.root,
                config_for(role="test", environment="test", apps={"employee": 201}),
                self.root / "release.tar",
                self.root / "target.json",
                self.root / "out.json",
                flow_executable=str(self.flow_runner), dependencies=dependencies,
            )
        self.assertEqual(events, ["verify-archive", "load-check-bundle"])
        self.assertNotIn("preflight", events)

    def test_release_test_does_not_emit_pass_evidence_for_destructive_or_failed_apply(self):
        from teamlib.release_adapter import ReleaseAdapterError

        for error in (
            ReleaseAdapterError("release-test requires reviewed destructive maintenance before apply"),
            ReleaseAdapterError("release apply failed"),
        ):
            with self.subTest(error=str(error)):
                events: list[str] = []
                dependencies, _ = self.release_dependencies(events, apply_error=error)
                with self.assertRaises(ReleaseAdapterError):
                    run_release_test(
                        self.root,
                        config_for(role="test", environment="test", apps={"employee": 201}),
                        self.root / "release.tar",
                        self.root / "target.json",
                        self.root / "out.json",
                        flow_executable=str(self.flow_runner), dependencies=dependencies,
                    )
                self.assertNotIn("write-report", events)
                self.assertNotIn("qualify-release", events)

    def test_run_release_test_schema_archive_emits_schema_report_with_no_app_aliases(self):
        events: list[str] = []
        schema_manifest = Manifest(
            2, "1.0.0", "a" * 40, "b" * 64, (), {},
            (), self.root / "release.tar", "d" * 64,
            kind="schema", alias=None, required_migrations=(),
        )
        qualified_aliases: list[tuple[str, ...]] = []
        dependencies, _ = self.release_dependencies(events, manifest=schema_manifest)

        def tracking_qualify(repo, config, source_commit, aliases, **kwargs):
            qualified_aliases.append(aliases)
            return {"version": 2, "final_status": "PASS", "source_commit": source_commit}

        dependencies = OnlineDependencies(
            **{**dependencies.__dict__, "qualify_release": tracking_qualify}
        )
        archive = self.root / "release.tar"
        archive.write_bytes(b"schema archive")
        target = self.root / "targets" / "test.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        out = self.root / "schema-evidence.json"
        result = run_release_test(
            self.root,
            config_for(role="test", environment="test", apps={"employee": 101, "payroll": 201}),
            archive, target, out,
            flow_executable=str(self.flow_runner), dependencies=dependencies,
        )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(qualified_aliases, [()])
        # Schema qualification holds the migration mutex and rechecks the cut.
        self.assertIsNotNone(self.held_history_checks[-1])
        written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(written["kind"], "schema")
        self.assertIsNone(written["alias"])
        self.assertEqual(self.release_preflight_flow_requirements, [False])

    def test_schema_release_preflight_does_not_require_browser_flow_runner(self):
        events: list[str] = []
        schema_manifest = Manifest(
            2, "1.0.0", "a" * 40, "b" * 64, (), {},
            (), self.root / "release.tar", "d" * 64,
            kind="schema", alias=None, required_migrations=(),
        )
        dependencies, _ = self.release_dependencies(events, manifest=schema_manifest)
        observed_requirements: list[bool] = []

        def preflight(_config, _repo, _flow, *, require_flow_runner=True):
            observed_requirements.append(require_flow_runner)
            return SimpleNamespace(toolchain_digest="e" * 64)

        def qualify(*_args, **_kwargs):
            return {"version": 2, "final_status": "PASS", "source_commit": "a" * 40}

        dependencies = OnlineDependencies(
            **{
                **dependencies.__dict__,
                "preflight": preflight,
                "qualify_release": qualify,
            }
        )
        archive = self.root / "schema-without-flow-runner.tar"
        archive.write_bytes(b"schema archive")
        target = self.root / "targets" / "test.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")

        result = run_release_test(
            self.root,
            config_for(role="test", environment="test", apps={"employee": 101}),
            archive,
            target,
            self.root / "schema-without-flow-runner.json",
            flow_executable="",
            dependencies=dependencies,
        )

        self.assertEqual(result.status, "PASS")
        self.assertEqual(observed_requirements, [False])

    def test_run_release_test_single_app_archive_exercises_only_selected_alias(self):
        events: list[str] = []
        app_manifest = Manifest(
            2, "1.0.0", "a" * 40, "b" * 64, (), {"employee": "c" * 64},
            (), self.root / "release.tar", "d" * 64,
            kind="app", alias="employee", required_migrations=(),
        )
        qualified_aliases: list[tuple[str, ...]] = []
        dependencies, _ = self.release_dependencies(events, manifest=app_manifest)
        orig_qualify = dependencies.qualify_release

        def tracking_qualify(repo, config, source_commit, aliases, **kwargs):
            qualified_aliases.append(aliases)
            # Qualification must run while the archived app bytes are held.
            self.assertEqual(self.holds, [{"expected": {"employee": "c" * 64}, "open": True}])
            return orig_qualify(repo, config, source_commit, aliases, **kwargs)

        dependencies = OnlineDependencies(
            **{**dependencies.__dict__, "qualify_release": tracking_qualify}
        )
        archive = self.root / "release.tar"
        archive.write_bytes(b"app archive")
        target = self.root / "targets" / "test.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        out = self.root / "app-evidence.json"
        result = run_release_test(
            self.root,
            config_for(role="test", environment="test", apps={"employee": 101, "payroll": 201}),
            archive, target, out,
            flow_executable=str(self.flow_runner), dependencies=dependencies,
        )
        self.assertEqual(result.status, "PASS")
        self.assertEqual(qualified_aliases, [("employee",)])
        self.assertEqual(self.holds, [{"expected": {"employee": "c" * 64}, "open": False}])
        self.assertEqual(self.held_required, [()])
        self.assertEqual(self.held_history_checks, [None])
        written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(written["kind"], "app")
        self.assertEqual(written["alias"], "employee")

    def test_release_app_hold_rechecks_deployed_bytes_and_always_releases(self):
        from teamlib import online_workflows

        calls: list[tuple] = []
        store = SimpleNamespace(
            acquire_app=lambda key, token, *_a: calls.append(("acquire", key)),
            release_app=lambda key, token, **kw: calls.append(("release", key, kw["confirmed_success"])),
        )
        target = SimpleNamespace(alias="employee", physical_key="k-employee")
        tree = {"application.apx": b"deployed\n"}
        from teamlib.trees import tree_digest

        with patch.object(online_workflows, "SqlControlStore", return_value=store), \
             patch.object(online_workflows, "profile_target", return_value=target), \
             patch.object(online_workflows, "capture_app", return_value=SimpleNamespace(tree=tree)):
            with online_workflows._hold_release_apps(self.root, config_for(), {"employee": tree_digest(tree)}):
                calls.append(("qualify",))
            self.assertEqual(calls, [("acquire", "k-employee"), ("qualify",), ("release", "k-employee", False)])
            calls.clear()
            # Another deployment replaced the app before qualification: refuse.
            with self.assertRaisesRegex(OnlineWorkflowError, "no longer matches the release archive"):
                with online_workflows._hold_release_apps(self.root, config_for(), {"employee": "0" * 64}):
                    calls.append(("qualify",))
            self.assertEqual(calls, [("acquire", "k-employee"), ("release", "k-employee", False)])
        # A schema release holds nothing.
        with patch.object(online_workflows, "SqlControlStore", side_effect=AssertionError("no store")):
            with online_workflows._hold_release_apps(self.root, config_for(), {}):
                pass

    def test_release_prerequisites_are_rechecked_and_held_through_qualification(self):
        from teamlib import online_workflows

        calls: list[tuple] = []
        history = {"m1": {"status": "APPLIED", "checksum": "a" * 64}}
        migrations = SimpleNamespace(
            acquire=lambda *_a: calls.append(("acquire-migrations",)),
            release=lambda *_a: calls.append(("release-migrations",)),
            read_history=lambda *_a: history,
        )
        required = ({"id": "m1", "checksum": "a" * 64},)
        with patch.object(online_workflows, "SqlControlStore", return_value=SimpleNamespace()), \
             patch.object(online_workflows, "SqlMigrationStore", return_value=migrations), \
             patch.object(online_workflows, "profile_target", return_value=SimpleNamespace()):
            with online_workflows._hold_release_apps(self.root, config_for(), {}, required):
                calls.append(("qualify",))
            self.assertEqual(calls, [("acquire-migrations",), ("qualify",), ("release-migrations",)])
            calls.clear()
            # Another run undid the prerequisite after deployment: refuse, still release.
            history["m1"] = {"status": "REVERTED", "checksum": "a" * 64}
            with self.assertRaisesRegex(OnlineWorkflowError, "required migration unavailable: m1"):
                with online_workflows._hold_release_apps(self.root, config_for(), {}, required):
                    calls.append(("qualify",))
            self.assertEqual(calls, [("acquire-migrations",), ("release-migrations",)])

    def test_schema_release_history_is_rechecked_and_held_through_qualification(self):
        from teamlib import online_workflows
        from teamlib.release import ReleaseError

        calls: list[tuple] = []
        migrations = SimpleNamespace(
            acquire=lambda *_a: calls.append(("acquire-migrations",)),
            release=lambda *_a: calls.append(("release-migrations",)),
            read_history=lambda *_a: {"m9": {"status": "APPLIED"}},
        )
        seen = []

        def moved(history):
            seen.append(history)
            raise ReleaseError("test target history is not at the release cut; another migration ran, test again")

        with patch.object(online_workflows, "SqlControlStore", return_value=SimpleNamespace()), \
             patch.object(online_workflows, "SqlMigrationStore", return_value=migrations), \
             patch.object(online_workflows, "profile_target", return_value=SimpleNamespace()):
            with online_workflows._hold_release_apps(self.root, config_for(), {}, (), seen.append):
                calls.append(("qualify",))
            self.assertEqual(calls, [("acquire-migrations",), ("qualify",), ("release-migrations",)])
            calls.clear()
            with self.assertRaisesRegex(OnlineWorkflowError, "not at the release cut"):
                with online_workflows._hold_release_apps(self.root, config_for(), {}, (), moved):
                    calls.append(("qualify",))
            self.assertEqual(calls, [("acquire-migrations",), ("release-migrations",)])
        self.assertEqual(seen[-1], {"m9": {"status": "APPLIED"}})

    def test_release_applied_check_refuses_pending_replay(self):
        from teamlib import online_workflows
        from teamlib.release import ReleaseError

        target = self.root / "targets" / "t.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        for plan, refused in (
            (SimpleNamespace(pending=(), events=()), False),
            (SimpleNamespace(pending=("m2",), events=({"id": "m2"},)), True),
        ):
            with self.subTest(refused=refused), \
                 patch.object(online_workflows, "_release_target_document", return_value={}), \
                 patch.object(online_workflows, "plan_release", return_value=plan):
                if refused:
                    with self.assertRaisesRegex(ReleaseError, "not at the release cut"):
                        online_workflows._require_release_applied(self.root / "r.tar", target, {})
                else:
                    online_workflows._require_release_applied(self.root / "r.tar", target, {})

    def test_run_release_test_app_archive_refuses_when_selected_alias_not_in_config(self):
        events: list[str] = []
        app_manifest = Manifest(
            2, "1.0.0", "a" * 40, "b" * 64, (), {"other": "c" * 64},
            (), self.root / "release.tar", "d" * 64,
            kind="app", alias="other", required_migrations=(),
        )
        dependencies, _ = self.release_dependencies(events, manifest=app_manifest)
        archive = self.root / "release.tar"
        archive.write_bytes(b"app archive")
        target = self.root / "targets" / "test.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        out = self.root / "app-evidence.json"
        with self.assertRaisesRegex(OnlineWorkflowError, "not configured in test environment"):
            run_release_test(
                self.root,
                config_for(role="test", environment="test", apps={"employee": 101, "payroll": 201}),
                archive, target, out,
                flow_executable=str(self.flow_runner), dependencies=dependencies,
            )

    def test_run_release_test_schema_then_app_release_exercises_only_selected_app_and_leaves_sibling_untouched(self):
        events: list[str] = []
        schema_manifest = Manifest(
            2, "1.1.0", "a" * 40, "b" * 64,
            ({"id": "m1", "checksum": "1" * 64},),
            {},
            (), self.root / "schema.tar", "d" * 64,
            kind="schema", alias=None,
        )
        app_manifest = Manifest(
            2, "2.0.0", "c" * 40, "e" * 64,
            (),
            {"employee": "f" * 64},
            (), self.root / "app.tar", "g" * 64,
            kind="app", alias="employee",
            required_migrations=({"id": "m1", "checksum": "1" * 64},),
        )
        qualified_aliases: list[tuple[str, ...]] = []
        dependencies, _ = self.release_dependencies(events, manifest=schema_manifest)
        orig_qualify = dependencies.qualify_release

        def tracking_qualify(repo, config, source_commit, aliases, **kwargs):
            qualified_aliases.append(aliases)
            if not aliases:
                self.assertEqual(kwargs["check_bundle"].declarations, {})
                return {"version": 2, "final_status": "PASS", "source_commit": source_commit}
            return orig_qualify(repo, config, source_commit, aliases, **kwargs)

        target = self.root / "targets" / "test.json"
        target.parent.mkdir(exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")
        two_app_config = config_for(role="test", environment="test", apps={"employee": 101, "payroll": 201})

        # 1. Run release-test for schema release
        schema_archive = self.root / "schema.tar"
        schema_archive.write_bytes(b"schema archive")
        schema_out = self.root / "schema-evidence.json"
        schema_dependencies = OnlineDependencies(
            **{
                **dependencies.__dict__,
                "verify_release": lambda _arch: schema_manifest,
                "release_app_checks": lambda _arch: build_app_check_bundle({}, ()),
                "qualify_release": tracking_qualify,
            }
        )
        schema_result = run_release_test(
            self.root, two_app_config, schema_archive, target, schema_out,
            flow_executable=str(self.flow_runner), dependencies=schema_dependencies,
        )
        self.assertEqual(schema_result.status, "PASS")
        self.assertEqual(qualified_aliases, [()])
        schema_evidence = json.loads(schema_out.read_text(encoding="utf-8"))
        self.assertEqual(schema_evidence["kind"], "schema")
        self.assertIsNone(schema_evidence["alias"])

        # 2. Run release-test for app release (employee only)
        app_archive = self.root / "app.tar"
        app_archive.write_bytes(b"app archive")
        app_out = self.root / "app-evidence.json"
        app_dependencies = OnlineDependencies(
            **{
                **dependencies.__dict__,
                "verify_release": lambda _arch: app_manifest,
                "qualify_release": tracking_qualify,
            }
        )
        app_result = run_release_test(
            self.root, two_app_config, app_archive, target, app_out,
            flow_executable=str(self.flow_runner), dependencies=app_dependencies,
        )
        self.assertEqual(app_result.status, "PASS")
        self.assertEqual(qualified_aliases, [(), ("employee",)])
        app_evidence = json.loads(app_out.read_text(encoding="utf-8"))
        self.assertEqual(app_evidence["kind"], "app")
        self.assertEqual(app_evidence["alias"], "employee")
        self.assertNotIn("payroll", app_evidence.get("apps", []))


class PreflightCliTranslationTests(unittest.TestCase):
    ENV_TEMPLATE = """\
PROJECT_NAME=team-template
TARGET_ROLE={role}
DB_ENVIRONMENT={environment}
APEX_APPS=employee:101:APP
TABLES_SCHEMA=APP_DATA
CODE_SCHEMA=APP_CODE
METADATA_SCHEMA=APP_META
APP_OWNERSHIP_MODE=shared
APEX_WORKSPACE_ID=5402650006222933
{profiles}
"""
    PROFILE_BLOCK = """\
{name}_SQLCL_CONNECTION=docker-demo
{name}_EXPECTED_USER=DEMO
{name}_EXPECTED_CURRENT_SCHEMA=DEMO
{name}_EXPECTED_DB_NAME=FREEPDB1
{name}_EXPECTED_SERVICE=freep1
{name}_EXPECTED_INSTANCE_ID=FREEPDB1
"""

    def env_file(self, root: Path, *, role: str, environment: str) -> Path:
        profiles = "".join(self.PROFILE_BLOCK.format(name=name) for name in ("TABLES", "CODE", "APEX", "METADATA", "VERIFY"))
        env_path = root / ".env"
        env_path.write_text(
            self.ENV_TEMPLATE.format(role=role, environment=environment, profiles=profiles),
            encoding="utf-8",
        )
        return env_path

    def test_cli_run_integration_rejects_preflight_failure_without_a_traceback(self):
        with tempfile.TemporaryDirectory(prefix="team-preflight-cli-") as directory:
            root = Path(directory)
            env_path = self.env_file(root, role="integration", environment="staging")
            with patch("teamlib.online_workflows.preflight_online", side_effect=RuntimeError("SQLcl executable is unavailable")):
                code = team.main(["--env", str(env_path), "run-integration", "--out", str(root / "out.json")])
        self.assertEqual(code, 3)

    def test_cli_run_release_test_rejects_preflight_failure_without_a_traceback(self):
        with tempfile.TemporaryDirectory(prefix="team-preflight-cli-") as directory:
            root = Path(directory)
            env_path = self.env_file(root, role="test", environment="test")
            archive = root / "release.tar"
            archive.write_bytes(b"not a real archive")
            fake_manifest = SimpleNamespace(app_tree_digests={"employee": "c" * 64}, source_commit="a" * 40)
            with patch("teamlib.online_workflows.verify_release", return_value=fake_manifest), \
                    patch("teamlib.online_workflows.release_app_check_bundle", return_value=app_check_bundle()), \
                    patch("teamlib.online_workflows.preflight_online", side_effect=RuntimeError("JDK executable is unavailable")):
                code = team.main([
                    "--env", str(env_path), "run-release-test", str(archive),
                    "--target", str(root / "target.json"), "--out", str(root / "out.json"),
                ])
        self.assertEqual(code, 3)


if __name__ == "__main__":
    unittest.main()
