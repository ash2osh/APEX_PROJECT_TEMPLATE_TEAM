from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

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
        return SimpleNamespace(identity={"SESSION_USER":"DEMO","CURRENT_SCHEMA":"DEMO","DB_NAME":"FREEPDB1","SERVICE":"freep1","INSTANCE_ID":"FREE"}, completion={"operation":operation}, result_manifest={"status":"success"})

    def test_deploy_uses_exact_source_and_verifies_reexport(self):
        report = deploy_app(self.target, self.source_tree, "abc123", repo=self.root, control_store=self.store, runner=self.runner)
        self.assertEqual(report.source_commit, "abc123")
        self.assertEqual(report.tree_digest, report.verified_tree_digest)

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


if __name__ == "__main__":
    unittest.main()
