from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from teamlib.config import Target
from teamlib.migration_store import MigrationMutexHeld, MigrationSetupRequired, MigrationStore


class MigrationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-migration-store-")
        self.target = Target(
            project="team-template", role="developer", environment="development",
            connection="meta", instance_id="FREE", db_name="FREEPDB1", service="freep1",
            session_user="META", current_schema="META", alias=None, workspace_id=None,
            app_id=None, parsing_schema=None, ownership_mode="shared", binding_digest="a" * 64,
        )
        self.store = MigrationStore(Path(self.temp.name))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_bootstrap_acquire_and_release(self):
        with self.assertRaises(MigrationSetupRequired):
            self.store.acquire(self.target, "run", "worker", "host")
        self.store.bootstrap(self.target)
        self.store.acquire(self.target, "run", "worker", "host")
        with self.assertRaises(MigrationMutexHeld):
            self.store.acquire(self.target, "other", "worker2", "host2")
        self.store.release(self.target, "run")

    def test_wrong_token_cannot_release_and_history_is_exportable(self):
        self.store.bootstrap(self.target)
        self.store.acquire(self.target, "run", "worker", "host")
        with self.assertRaises(MigrationMutexHeld):
            self.store.release(self.target, "wrong")
        self.store.record_applied(self.target, "m1", "a" * 64, "tables", (), "commit", "alice", {"before": "b", "after": "a"})
        self.store.release(self.target, "run")
        history = self.store.read_history(self.target)
        self.assertEqual(history["m1"]["status"], "APPLIED")
        output = Path(self.temp.name) / "history.json"
        self.store.export_history(self.target, output)
        self.assertIn("m1", output.read_text(encoding="utf-8"))

    def test_unresolved_attempt_blocks_release(self):
        self.store.bootstrap(self.target)
        self.store.acquire(self.target, "run", "worker", "host")
        self.store.record_attempt_start(self.target, "attempt", "m1", "a" * 64, "run")
        with self.assertRaises(MigrationMutexHeld):
            self.store.release(self.target, "run")


if __name__ == "__main__":
    unittest.main()
