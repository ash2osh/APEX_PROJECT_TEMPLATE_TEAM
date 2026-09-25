from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import patch

from teamlib.config import Target
from teamlib.control_store import ControlStore
from teamlib.deploy import DeployError, deploy_app


class DeployTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-deploy-")
        self.root = Path(self.temp.name)
        self.target = Target(
            project="team", role="integration", environment="staging", connection="fake",
            instance_id="FREE", db_name="FREEPDB1", service="freep1", session_user="DEMO",
            current_schema="DEMO", alias="checkout", workspace_id=7, app_id=200,
            parsing_schema="DEMO", ownership_mode="shared", binding_digest="a" * 64,
        )
        self.store = ControlStore(self.root / ".sync-state")
        self.store.setup_state([self.target])
        self.database_tree = {"application.apx": b"old\n", ".apex/apexlang.json": b'{"format":"APEXLANG"}\n'}
        self.source_tree = {"application.apx": b"new\n", ".apex/apexlang.json": b'{"format":"APEXLANG"}\n'}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def runner(self, target, operation, driver, work, **kwargs):
        if operation == "write":
            self.database_tree = dict(self.source_tree)
        output = Path(work) / "exported"
        for path, data in self.database_tree.items():
            destination = output / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        stdout = (
            f"TEAM_RESULT_BEGIN\n"
            f"TEAM_APP_ID_APEX_VERSION|26.1.4\n"
            f"TEAM_APP_ID_WS_SCHEMA|{target.workspace_id}|{target.parsing_schema}\n"
            f"TEAM_APP_ID_APP|{target.workspace_id}|{target.app_id}|{target.parsing_schema}\n"
            f"TEAM_RESULT_END\n"
        )
        return SimpleNamespace(
            identity={"SESSION_USER":"DEMO","CURRENT_SCHEMA":"DEMO","DB_NAME":"FREEPDB1","SERVICE":"freep1","INSTANCE_ID":"FREE"},
            completion={"operation":operation},
            result_manifest={"status":"success"},
            stdout=stdout,
        )

    def test_deploy_uses_exact_source_and_verifies_reexport(self):
        report = deploy_app(self.target, self.source_tree, "abc123", repo=self.root, control_store=self.store, runner=self.runner)
        self.assertEqual(report.source_commit, "abc123")
        self.assertEqual(report.tree_digest, report.verified_tree_digest)

    def test_packaged_master_contract_replaces_the_repository_copy(self):
        (self.root / "targets").mkdir()
        (self.root / "targets" / "masters.json").write_text(json.dumps({"from": "repo"}), encoding="utf-8")
        cases = (
            ("default reads the repository", {}, [{"from": "repo"}]),
            ("packaged contract wins", {"master_contract": {"from": "release"}}, [{"from": "release"}]),
            ("release without a contract skips it", {"master_contract": None}, []),
        )
        for name, kwargs, expected in cases:
            with self.subTest(name=name):
                with patch("teamlib.deploy.validate_masters") as validate:
                    deploy_app(self.target, self.source_tree, "abc123", repo=self.root, control_store=self.store, runner=self.runner, **kwargs)
                self.assertEqual([call.args[2] for call in validate.call_args_list], expected)

    def test_production_deploy_refuses_before_runner(self):
        target = Target(**{**self.target.__dict__, "environment": "production"})
        called = []
        with self.assertRaises(DeployError):
            deploy_app(target, self.source_tree, "abc", repo=self.root, control_store=self.store, runner=lambda *args, **kwargs: called.append(1))
        self.assertFalse(called)

    def test_replay_requires_matching_proof(self):
        target = Target(**{**self.target.__dict__, "role": "replay", "environment": "test"})
        with self.assertRaises(DeployError):
            deploy_app(target, self.source_tree, "abc", repo=self.root, control_store=self.store, runner=self.runner)

    def test_deploy_refuses_when_app_id_mismatches(self):
        calls = []
        def runner(target, operation, driver, work, **kwargs):
            calls.append(operation)
            if operation == "write":
                raise AssertionError("write must not be called on identity mismatch")
            stdout = (
                f"TEAM_RESULT_BEGIN\n"
                f"TEAM_APP_ID_APEX_VERSION|26.1.4\n"
                f"TEAM_APP_ID_WS_SCHEMA|{target.workspace_id}|{target.parsing_schema}\n"
                f"TEAM_APP_ID_APP|{target.workspace_id}|9999|{target.parsing_schema}\n"
                f"TEAM_RESULT_END\n"
            )
            return SimpleNamespace(
                identity={"SESSION_USER":"DEMO","CURRENT_SCHEMA":"DEMO","DB_NAME":"FREEPDB1","SERVICE":"freep1","INSTANCE_ID":"FREE"},
                completion={"operation":operation}, result_manifest={"status":"success"},
                stdout=stdout,
            )
        with self.assertRaises(DeployError):
            deploy_app(self.target, self.source_tree, "abc", repo=self.root, control_store=self.store, runner=runner)
        self.assertNotIn("write", calls)

    def test_deploy_refuses_when_apex_version_below_26_1(self):
        calls = []
        def runner(target, operation, driver, work, **kwargs):
            calls.append(operation)
            if operation == "write":
                raise AssertionError("write must not be called on old APEX version")
            stdout = (
                f"TEAM_RESULT_BEGIN\n"
                f"TEAM_APP_ID_APEX_VERSION|26.0.0\n"
                f"TEAM_APP_ID_WS_SCHEMA|{target.workspace_id}|{target.parsing_schema}\n"
                f"TEAM_APP_ID_APP|{target.workspace_id}|{target.app_id}|{target.parsing_schema}\n"
                f"TEAM_RESULT_END\n"
            )
            return SimpleNamespace(
                identity={"SESSION_USER":"DEMO","CURRENT_SCHEMA":"DEMO","DB_NAME":"FREEPDB1","SERVICE":"freep1","INSTANCE_ID":"FREE"},
                completion={"operation":operation}, result_manifest={"status":"success"},
                stdout=stdout,
            )
        with self.assertRaises(DeployError):
            deploy_app(self.target, self.source_tree, "abc", repo=self.root, control_store=self.store, runner=runner)
        self.assertNotIn("write", calls)

    def test_deploy_allows_first_deploy_of_absent_app_with_verified_schema(self):
        calls = []
        def runner(target, operation, driver, work, **kwargs):
            calls.append(operation)
            if operation == "write":
                self.database_tree = dict(self.source_tree)
            output = Path(work) / "exported"
            for path, data in self.database_tree.items():
                destination = output / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            app_line = (
                f"TEAM_APP_ID_APP|{target.workspace_id}|{target.app_id}|{target.parsing_schema}\n"
                if "write" in calls else ""
            )
            stdout = (
                f"TEAM_RESULT_BEGIN\n"
                f"TEAM_APP_ID_APEX_VERSION|26.1.4\n"
                f"TEAM_APP_ID_WS_SCHEMA|{target.workspace_id}|{target.parsing_schema}\n"
                f"{app_line}"
                f"TEAM_RESULT_END\n"
            )
            return SimpleNamespace(
                identity={"SESSION_USER":"DEMO","CURRENT_SCHEMA":"DEMO","DB_NAME":"FREEPDB1","SERVICE":"freep1","INSTANCE_ID":"FREE"},
                completion={"operation":operation}, result_manifest={"status":"success"},
                stdout=stdout,
            )
        report = deploy_app(self.target, self.source_tree, "abc123", repo=self.root, control_store=self.store, runner=runner)
        self.assertIn("write", calls)
        self.assertEqual(report.source_commit, "abc123")

    def test_deploy_refuses_absent_app_with_unassigned_schema(self):
        calls = []
        def runner(target, operation, driver, work, **kwargs):
            calls.append(operation)
            if operation == "write":
                raise AssertionError("write must not be called with unassigned schema")
            stdout = (
                f"TEAM_RESULT_BEGIN\n"
                f"TEAM_APP_ID_APEX_VERSION|26.1.4\n"
                f"TEAM_APP_ID_WS_SCHEMA|{target.workspace_id}|OTHER_SCHEMA\n"
                f"TEAM_RESULT_END\n"
            )
            return SimpleNamespace(
                identity={"SESSION_USER":"DEMO","CURRENT_SCHEMA":"DEMO","DB_NAME":"FREEPDB1","SERVICE":"freep1","INSTANCE_ID":"FREE"},
                completion={"operation":operation}, result_manifest={"status":"success"},
                stdout=stdout,
            )
        with self.assertRaises(DeployError):
            deploy_app(self.target, self.source_tree, "abc", repo=self.root, control_store=self.store, runner=runner)
        self.assertNotIn("write", calls)


class DeployTimeoutTests(unittest.TestCase):
    def test_every_apex_operation_gets_the_application_budget(self):
        import inspect

        from teamlib import deploy
        from teamlib.sqlcl import APEX_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS

        self.assertGreater(APEX_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS)
        source = inspect.getsource(deploy)
        runner_calls = [
            line.strip()
            for line in source.splitlines()
            if "runner(target," in line
        ]
        self.assertTrue(runner_calls, "deploy.py must still drive SQLcl through runner()")
        for call in runner_calls:
            self.assertIn(
                "timeout=APEX_TIMEOUT_SECONDS",
                call,
                f"APEX operation runs on the metadata budget: {call}",
            )


if __name__ == "__main__":
    unittest.main()
