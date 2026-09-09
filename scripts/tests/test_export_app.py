from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
from types import SimpleNamespace
import json
import subprocess
import tempfile
import unittest

from teamlib.apex import ApexError, capture_app, export_app
from teamlib.config import Target
from teamlib.control_store import ControlStore
from teamlib.state import save_checkpoint


class ExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-export-test-")
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Export Test"], check=True)
        (self.repo / "apps" / "checkout" / "pages").mkdir(parents=True)
        (self.repo / "apps" / "checkout" / "application.apx").write_bytes(b"app old\n")
        (self.repo / "apps" / "checkout" / "pages" / "home.apx").write_bytes(b"old page\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "seed"], check=True)
        self.target = Target(
            project="team-template", role="developer", environment="development",
            connection="fake", instance_id="FREE", db_name="FREEPDB1", service="freep1",
            session_user="DEMO", current_schema="DEMO", alias="checkout",
            workspace_id=5402650006222933, app_id=100, parsing_schema="DEMO",
            ownership_mode="shared", binding_digest="a" * 64,
        )
        self.store = ControlStore(self.repo / ".sync-state")
        self.store.setup_state([self.target])
        self.database_tree = {
            "application.apx": b"app old\n",
            "pages/home.apx": b"old page\n",
            ".apex/apexlang.json": b'{"format":"APEXLANG"}\n',
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fake_runner(self, target, operation, driver, work, **kwargs):
        export = Path(work) / "exported-app"
        for path, data in self.database_tree.items():
            destination = export / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        metadata = export / ".apex" / "apexlang.json"
        metadata.parent.mkdir(parents=True, exist_ok=True)
        metadata.write_text('{"format":"APEXLANG"}\n', encoding="utf-8")
        return SimpleNamespace(
            identity={"SESSION_USER": "DEMO", "CURRENT_SCHEMA": "DEMO", "DB_NAME": "FREEPDB1", "SERVICE": "freep1", "INSTANCE_ID": "FREE"},
            completion={"operation": operation},
            result_manifest={"status": "success"},
            log_path=Path(work) / "fake.log",
            generated_driver=Path(driver), stdout="", stderr="", argv=(), exit_code=0,
        )

    def test_capture_uses_verified_read_and_finds_one_export(self):
        capture = capture_app(self.target, repo=self.repo, control_store=self.store, runner=self.fake_runner)
        self.assertEqual(capture.tree, self.database_tree)
        self.assertEqual(capture.before_sync.generation, capture.after_sync.generation)

    def test_capture_records_a_self_contained_evidence_source_marker(self):
        capture = capture_app(self.target, repo=self.repo, control_store=self.store, runner=self.fake_runner)
        marker = self.repo / ".sync-state" / "recovery" / capture.recovery_id / "evidence-source.json"
        record = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(record["authoritative"], "sync-state")
        self.assertEqual(record["scratch_work_dir"], str(capture.work_dir))

    def test_export_preserves_committed_git_only_addition(self):
        head = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        save_checkpoint(self.target, self.database_tree, self.database_tree, head, "initial", root=self.repo / ".sync-state")
        (self.repo / "apps" / "checkout" / "pages" / "new.apx").write_bytes(b"git page\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "git addition"], check=True)
        decision = export_app(self.target, repo=self.repo, control_store=self.store, runner=self.fake_runner)
        self.assertFalse(decision.conflicts)
        self.assertEqual(decision.tree["pages/new.apx"], b"git page\n")
        self.assertEqual((self.repo / "apps" / "checkout" / "pages" / "new.apx").read_bytes(), b"git page\n")

    def test_missing_checkpoint_retains_capture_and_refuses(self):
        with self.assertRaises(ApexError) as context:
            export_app(self.target, repo=self.repo, control_store=self.store, runner=self.fake_runner)
        self.assertIn("recovery", str(context.exception).lower())

    def test_torn_capture_is_refused_when_generation_changes(self):
        class TornStore(ControlStore):
            def read_app_sync_state(self, target):
                state = super().read_app_sync_state(target)
                if not hasattr(self, "read_count"):
                    self.read_count = 0
                self.read_count += 1
                if self.read_count == 2:
                    with self._locked() as data:
                        data["mutexes"][state.target_key]["generation"] = 2
                    from dataclasses import replace
                    return replace(state, generation=2)
                return state

        # This test only asserts the public refusal shape; the store subclass
        # is intentionally defensive if a future implementation changes reads.
        torn = TornStore(self.repo / "torn-state")
        torn.setup_state([self.target])
        with self.assertRaises(ApexError):
            capture_app(self.target, repo=self.repo, control_store=torn, runner=self.fake_runner)


if __name__ == "__main__":
    unittest.main()
