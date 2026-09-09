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

from teamlib.apex import ExportConflict, _receipt_allows_import, import_app, resolve_export, export_app
from teamlib.config import Target
from teamlib.control_store import ControlStore
from teamlib.state import load_checkpoint, save_checkpoint, save_verified_baseline, load_baseline
from teamlib.trees import read_git_tree


class RecoveryFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-recovery-flow-")
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Recovery Test"], check=True)
        (self.repo / "apps" / "checkout" / "pages").mkdir(parents=True)
        (self.repo / "apps" / "checkout" / ".apex").mkdir()
        self._write_source(b"old")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "seed"], check=True)
        self.seed = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        self.target = Target(
            project="team-template", role="developer", environment="development",
            connection="fake", instance_id="FREE", db_name="FREEPDB1", service="freep1",
            session_user="DEMO", current_schema="DEMO", alias="checkout",
            workspace_id=5402650006222933, app_id=100, parsing_schema="DEMO",
            ownership_mode="shared", binding_digest="a" * 64,
        )
        self.store = ControlStore(self.repo / ".sync-state")
        self.store.setup_state([self.target])
        self.old_tree = read_git_tree(self.repo, self.seed, "checkout")
        save_verified_baseline(self.target, self.seed, self.old_tree, root=self.repo / ".sync-state")
        save_checkpoint(self.target, self.old_tree, self.old_tree, self.seed, "seed-receipt", root=self.repo / ".sync-state")
        self.database_tree = dict(self.old_tree)
        self.selected_tree: dict[str, bytes] = {}

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_source(self, page: bytes) -> None:
        (self.repo / "apps" / "checkout" / "application.apx").write_bytes(b"application\n")
        (self.repo / "apps" / "checkout" / "pages" / "home.apx").write_bytes(page + b"\n")
        (self.repo / "apps" / "checkout" / ".apex" / "apexlang.json").write_bytes(b'{"format":"APEXLANG"}\n')

    def _runner(self, target, operation, driver, work, **kwargs):
        if operation == "write":
            self.database_tree = dict(self.selected_tree)
        exported = Path(work) / "exported-app"
        for relative, data in self.database_tree.items():
            destination = exported / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        return SimpleNamespace(
            identity={"SESSION_USER": "DEMO", "CURRENT_SCHEMA": "DEMO", "DB_NAME": "FREEPDB1", "SERVICE": "freep1", "INSTANCE_ID": "FREE"},
            completion={"operation": operation}, result_manifest={"status": "success"},
            log_path=Path(work) / "fake.log", generated_driver=Path(driver),
            stdout="", stderr="", argv=(), exit_code=0,
        )

    def test_conflict_resolve_commit_then_import_uses_resolution_receipt(self):
        self.database_tree["pages/home.apx"] = b"shared\n"
        self._write_source(b"mine")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "branch edit"], check=True)
        branch_head = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()

        with self.assertRaises(ExportConflict) as context:
            export_app(self.target, repo=self.repo, control_store=self.store, runner=self._runner)

        resolved = self.repo / "scratch" / "resolved"
        (resolved / "pages").mkdir(parents=True)
        (resolved / ".apex").mkdir()
        (resolved / "application.apx").write_bytes(b"application\n")
        (resolved / "pages" / "home.apx").write_bytes(b"resolved\n")
        (resolved / ".apex" / "apexlang.json").write_bytes(b'{"format":"APEXLANG"}\n')
        resolve_export(
            self.target, context.exception.recovery_id, resolved,
            repo=self.repo, root=self.repo / ".sync-state",
        )
        subprocess.run(["git", "-C", str(self.repo), "add", "apps/checkout"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "resolve shared export"], check=True)
        selected = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        self.selected_tree = read_git_tree(self.repo, selected, "checkout")
        self.assertTrue(
            _receipt_allows_import(
                self.target, self.database_tree, self.selected_tree, selected,
                self.repo / ".sync-state",
            )
        )

        baseline = import_app(
            self.target, selected, repo=self.repo, root=self.repo / ".sync-state",
            control_store=self.store, runner=self._runner,
            announce=lambda message: None,
        )
        self.assertEqual(baseline.source_commit, selected)
        self.assertEqual(load_baseline(self.target, root=self.repo / ".sync-state").tree, self.selected_tree)
        self.assertEqual(self.store.read_app_sync_state(self.target.physical_key).generation, 2)
        self.assertNotEqual(branch_head, selected)

    def test_resolution_carries_checkpoint_tombstones_forward(self):
        self.database_tree.pop("pages/home.apx")
        self._write_source(b"mine")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "branch edit"], check=True)

        # The checkpoint already records a deletion that must not be
        # resurrected by a later resolution or import.
        save_checkpoint(
            self.target,
            self.old_tree,
            {key: value for key, value in self.old_tree.items() if key != "pages/home.apx"},
            self.seed,
            "seed-receipt",
            required_absent={"pages/home.apx", "pages/removed.apx"},
            root=self.repo / ".sync-state",
        )
        resolved = self.repo / "scratch" / "resolved-tombstone"
        (resolved / ".apex").mkdir(parents=True)
        (resolved / "pages").mkdir()
        (resolved / "application.apx").write_bytes(b"application\n")
        (resolved / ".apex" / "apexlang.json").write_bytes(b'{"format":"APEXLANG"}\n')
        with self.assertRaises(ExportConflict) as context:
            export_app(self.target, repo=self.repo, control_store=self.store, runner=self._runner)

        # Keep the page absent in the reviewed resolution.  The resulting
        # checkpoint must retain the prior tombstone rather than resetting it
        # from an empty set.
        resolve_export(
            self.target, context.exception.recovery_id, resolved,
            repo=self.repo, root=self.repo / ".sync-state",
        )
        head = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        checkpoint = load_checkpoint(self.target, head, root=self.repo / ".sync-state")
        self.assertIn("pages/home.apx", checkpoint.required_absent)
        self.assertIn("pages/removed.apx", checkpoint.required_absent)

    def test_supplying_the_run_token_of_a_released_uncertain_target_still_recovers(self):
        key = self.target.physical_key
        run_token = "a" * 32
        self.store.register_app(self.target, "checkout-1", "host-1", "dev")
        self.store.acquire_app(key, run_token, "checkout-1", "host-1", "dev")
        self.store.mark_payload_starting(key, run_token)
        # A non-SQLcl failure after the payload started clears the owner token
        # but leaves the target uncertain.
        self.store.release_app(key, run_token, confirmed_success=False)
        state = self.store.read_app_sync_state(key)
        self.assertIsNone(state.owner_token)
        self.assertTrue(state.is_uncertain)

        evidence = self.repo / "evidence.txt"
        evidence.write_text("worker terminated; capture retained\n", encoding="utf-8")
        recovered = self.store.recover_app_lock(key, evidence=evidence, run_token=run_token)
        self.assertFalse(recovered.is_uncertain)
        self.assertIsNone(recovered.owner_token)

    def test_a_run_token_that_never_held_the_target_is_still_refused(self):
        from teamlib.control_store import MutexHeld
        key = self.target.physical_key
        self.store.register_app(self.target, "checkout-1", "host-1", "dev")
        self.store.acquire_app(key, "a" * 32, "checkout-1", "host-1", "dev")
        self.store.mark_payload_starting(key, "a" * 32)
        evidence = self.repo / "evidence.txt"
        evidence.write_text("worker terminated\n", encoding="utf-8")
        with self.assertRaises(MutexHeld):
            self.store.recover_app_lock(key, evidence=evidence, run_token="b" * 32)


if __name__ == "__main__":
    unittest.main()
