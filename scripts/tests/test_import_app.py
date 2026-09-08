from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest

from teamlib.apex import ApexError, import_app
from teamlib.config import Target
from teamlib.control_store import ControlStore
from teamlib.sqlcl import SqlclError
from teamlib.state import load_baseline, save_verified_baseline


class ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-import-test-")
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Import Test"], check=True)
        (self.repo / "apps" / "checkout" / ".apex").mkdir(parents=True)
        (self.repo / "apps" / "checkout" / "pages").mkdir(parents=True)
        (self.repo / "apps" / "checkout" / "application.apx").write_bytes(b"app\n")
        (self.repo / "apps" / "checkout" / "pages" / "home.apx").write_bytes(b"old\n")
        (self.repo / "apps" / "checkout" / ".apex" / "apexlang.json").write_bytes(b'{"format":"APEXLANG"}\n')
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "seed"], check=True)
        self.seed_head = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        self.target = Target(
            project="team-template", role="developer", environment="development",
            connection="fake", instance_id="FREE", db_name="FREEPDB1", service="freep1",
            session_user="DEMO", current_schema="DEMO", alias="checkout",
            workspace_id=5402650006222933, app_id=100, parsing_schema="DEMO",
            ownership_mode="shared", binding_digest="a" * 64,
        )
        self.store = ControlStore(self.repo / ".sync-state")
        self.store.setup_state([self.target])
        self.old_tree = {
            "application.apx": b"app\n",
            "pages/home.apx": b"old\n",
            ".apex/apexlang.json": b'{"format":"APEXLANG"}\n',
        }
        self.database_tree = dict(self.old_tree)
        save_verified_baseline(self.target, self.seed_head, self.old_tree, root=self.repo / ".sync-state")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fake_runner(self, target, operation, driver, work, **kwargs):
        if operation == "write":
            self.database_tree = dict(self.selected_tree)
        export = Path(work) / "exported-app"
        for path, data in self.database_tree.items():
            destination = export / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        return SimpleNamespace(
            identity={"SESSION_USER": "DEMO", "CURRENT_SCHEMA": "DEMO", "DB_NAME": "FREEPDB1", "SERVICE": "freep1", "INSTANCE_ID": "FREE"},
            completion={"operation": operation}, result_manifest={"status": "success"},
            log_path=Path(work) / "fake.log", generated_driver=Path(driver),
            stdout="", stderr="", argv=(), exit_code=0,
        )

    def commit_new_source(self) -> str:
        (self.repo / "apps" / "checkout" / "pages" / "home.apx").write_bytes(b"new\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "new source"], check=True)
        return subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()

    def test_import_stamps_baseline_only_after_verified_reexport(self):
        selected = self.commit_new_source()
        from teamlib.trees import read_git_tree
        self.selected_tree = read_git_tree(self.repo, selected, "checkout")
        baseline = import_app(self.target, selected, repo=self.repo, control_store=self.store, runner=self.fake_runner)
        self.assertEqual(baseline.source_commit, selected)
        self.assertEqual(load_baseline(self.target, root=self.repo / ".sync-state").tree, self.selected_tree)
        self.assertEqual(self.store.read_app_sync_state(self.target.physical_key).generation, 2)

    def test_uncaptured_builder_state_refuses_without_advancing_generation(self):
        selected = self.commit_new_source()
        self.database_tree["pages/uncaptured.apx"] = b"builder\n"
        self.selected_tree = self.old_tree
        with self.assertRaises(ApexError):
            import_app(self.target, selected, repo=self.repo, control_store=self.store, runner=self.fake_runner)
        self.assertEqual(self.store.read_app_sync_state(self.target.physical_key).generation, 1)

    def test_sqlcl_failure_after_payload_keeps_target_held_and_uncertain(self):
        selected = self.commit_new_source()
        from teamlib.trees import read_git_tree
        self.selected_tree = read_git_tree(self.repo, selected, "checkout")

        def failing_runner(target, operation, driver, work, **kwargs):
            if operation == "read":
                return self.fake_runner(target, operation, driver, work, **kwargs)
            raise SqlclError("SQLcl timed out; target state is unknown")

        with self.assertRaises(ApexError):
            import_app(self.target, selected, repo=self.repo, control_store=self.store, runner=failing_runner)
        state = self.store.read_app_sync_state(self.target.physical_key)
        self.assertIsNotNone(state.owner_token)
        self.assertTrue(state.is_uncertain)


if __name__ == "__main__":
    unittest.main()
