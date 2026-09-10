from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
import tempfile
import unittest

from teamlib.config import Target
from teamlib.fingerprints import inventory_from_rows
from teamlib.migration_store import (
    MigrationMutexHeld,
    MigrationSetupRequired,
    MigrationStore,
    MigrationStoreError,
)


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
        self.schema_set_digest = "a" * 64

    def inventory(self, rows):
        return inventory_from_rows(rows, schema_set_digest=self.schema_set_digest)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_bootstrap_acquire_and_release(self):
        with self.assertRaises(MigrationSetupRequired):
            self.store.acquire(self.target, "run", "worker", "host")
        self.store.bootstrap(self.target, schema_set_digest=self.schema_set_digest)
        self.store.acquire(self.target, "run", "worker", "host")
        with self.assertRaises(MigrationMutexHeld):
            self.store.acquire(self.target, "other", "worker2", "host2")
        self.store.release(self.target, "run")

    def test_wrong_token_cannot_release_and_history_is_exportable(self):
        self.store.bootstrap(self.target, schema_set_digest=self.schema_set_digest)
        self.store.acquire(self.target, "run", "worker", "host")
        with self.assertRaises(MigrationMutexHeld):
            self.store.release(self.target, "wrong")
        before = self.inventory([{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "before"}])
        after = self.inventory([{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "after"}])
        self.store.record_inventory(self.target, before, run_token="run")
        self.store.ensure_observation(self.target, before.digest, run_token="run")
        self.store.record_inventory(self.target, after, run_token="run")
        self.store.record_applied(self.target, "m1", "a" * 64, "tables", (), "commit", "alice", {"before": before.digest, "after": after.digest})
        self.store.release(self.target, "run")
        history = self.store.read_history(self.target)
        self.assertEqual(history["m1"]["status"], "APPLIED")
        output = Path(self.temp.name) / "history.json"
        self.store.export_history(self.target, output)
        self.assertIn("m1", output.read_text(encoding="utf-8"))

    def test_unresolved_attempt_blocks_release(self):
        self.store.bootstrap(self.target, schema_set_digest=self.schema_set_digest)
        self.store.acquire(self.target, "run", "worker", "host")
        self.store.record_attempt_start(self.target, "attempt", "m1", "a" * 64, "run")
        with self.assertRaises(MigrationMutexHeld):
            self.store.release(self.target, "run")

    def test_new_store_uses_version_two_event_history(self):
        data = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["history"], [])

    def test_bootstrap_refuses_schema_set_rebinding(self):
        first = self.schema_set_digest
        self.store.bootstrap(self.target, schema_set_digest=first)
        self.store.bootstrap(self.target, schema_set_digest=first)
        with self.assertRaisesRegex(
            MigrationStoreError, "different project/schema set"
        ):
            self.store.bootstrap(self.target, schema_set_digest="b" * 64)

    def test_bootstrap_requires_a_lowercase_schema_set_digest(self):
        with self.assertRaisesRegex(
            MigrationStoreError, "lowercase schema-set SHA-256"
        ):
            self.store.bootstrap(self.target, schema_set_digest="not-a-digest")

    def test_bootstrap_adopts_empty_v1_digest_only_with_preserved_backup(self):
        self.store.path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "meta": {
                        "state_key": self.target.state_key,
                        "project_id": self.target.project,
                        "schema_set_digest": "",
                    },
                    "mutex": None,
                    "history": [],
                    "attempts": {},
                    "inventories": {},
                    "observations": [],
                }
            ),
            encoding="utf-8",
        )
        self.store._backup_path.write_bytes(b"preserved v1 source")
        self.store.bootstrap(self.target, schema_set_digest=self.schema_set_digest)
        self.assertEqual(
            json.loads(self.store.path.read_text(encoding="utf-8"))["meta"]["schema_set_digest"],
            self.schema_set_digest,
        )

    def test_bootstrap_refuses_unowned_empty_digest(self):
        self.store.path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "meta": {
                        "state_key": self.target.state_key,
                        "project_id": self.target.project,
                        "schema_set_digest": "",
                    },
                    "mutex": None,
                    "history": [],
                    "attempts": {},
                    "inventories": {},
                    "observations": [],
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(MigrationStoreError, "unowned schema set"):
            self.store.bootstrap(self.target, schema_set_digest=self.schema_set_digest)

    def test_v1_store_is_upgraded_and_original_bytes_are_preserved(self):
        before = self.inventory([{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "before"}])
        entry = {
            "status": "APPLIED",
            "checksum": "a" * 64,
            "target": "tables",
            "dependencies": [],
            "source_commit": "commit",
            "sequence": 1,
            "applied_sequence": 1,
            "applied_at": "now",
            "applied_by": "alice",
            "run_token": "run",
            "attempt_id": "attempt",
            "observation": {"before": before.digest, "after": before.digest, "evidence": "e" * 64},
        }
        v1 = {
            "version": 1,
            "meta": {"state_key": self.target.state_key, "project_id": self.target.project, "schema_set_digest": ""},
            "mutex": {"owner_token": None, "worker_identity": None, "host": None, "acquired_at": None},
            "history": {"m1": entry},
            "attempts": {"attempt": {"attempt_id": "attempt", "migration_id": "m1", "checksum": "a" * 64, "state": "APPLIED", "run_token": "run"}},
            "inventories": {before.digest: before.as_dict()},
            "observations": [{"sequence": 0, "migration_id": None, "attempt_id": None, "predecessor_sequence": None, "before": before.digest, "after": before.digest, "evidence": "e" * 64}],
        }
        original = json.dumps(v1, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        self.store.path.write_bytes(original)

        upgraded = MigrationStore(Path(self.temp.name))
        data = json.loads(upgraded.path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 2)
        self.assertEqual([event["operation"] for event in data["history"]], ["up"])
        self.assertEqual(data["attempts"]["attempt"]["action"], "migrate")
        self.assertEqual(data["attempts"]["attempt"]["confirmation_digest"], "")
        self.assertEqual(upgraded._backup_path.read_bytes(), original)

    def test_event_ledger_appends_and_collapses_current_state(self):
        self.store.bootstrap(self.target, schema_set_digest=self.schema_set_digest)
        self.store.acquire(self.target, "run", "worker", "host")
        first = self.inventory([{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "one"}])
        second = self.inventory([{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "two"}])
        third = self.inventory([{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "three"}])
        for inventory in (first, second, third):
            self.store.record_inventory(self.target, inventory, run_token="run")
        self.store.ensure_observation(self.target, first.digest, run_token="run")

        self.store.record_attempt_start(self.target, "up-a", "a", "a" * 64, "run")
        self.store.record_event(
            self.target, "a", "a" * 64, "tables", (), "commit", "alice",
            {"before": first.digest, "after": second.digest, "evidence": "b" * 64},
            operation="up", run_token="run", attempt_id="up-a",
        )
        self.store.record_attempt_start(self.target, "up-b", "b", "b" * 64, "run")
        self.store.record_event(
            self.target, "b", "b" * 64, "tables", (("a", "a" * 64),), "commit", "alice",
            {"before": second.digest, "after": third.digest, "evidence": "c" * 64},
            operation="up", run_token="run", attempt_id="up-b",
        )
        self.store.record_attempt_start(self.target, "down-b", "b", "b" * 64, "run", action="undo", confirmation_digest="d" * 64)
        self.store.record_event(
            self.target, "b", "b" * 64, "tables", (("a", "a" * 64),), "commit", "alice",
            {"before": third.digest, "after": second.digest, "evidence": "e" * 64},
            operation="down", run_token="run", attempt_id="down-b",
        )

        raw = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual([event["sequence"] for event in raw["history"]], [1, 2, 3])
        history = self.store.read_history(self.target)
        self.assertEqual(history["a"]["status"], "APPLIED")
        self.assertEqual(history["b"]["status"], "REVERTED")
        self.assertEqual(history["b"]["operation"], "down")
        self.assertEqual(history["b"]["sequence"], 3)

    def test_malformed_event_histories_are_rejected(self):
        invalid_histories = (
            [{"id": "m1", "operation": "sideways", "sequence": 1}],
            [{"id": "m1", "operation": "up", "sequence": 2}],
            [{"id": "m1", "operation": "down", "sequence": 1}],
            [
                {"id": "m1", "operation": "up", "sequence": 1},
                {"id": "m1", "operation": "up", "sequence": 2},
            ],
        )
        for history in invalid_histories:
            with self.subTest(history=history):
                root = Path(tempfile.mkdtemp(prefix="team-malformed-store-"))
                try:
                    (root / "migration-store.json").write_text(
                        json.dumps({"version": 2, "meta": None, "mutex": None, "history": history, "attempts": {}, "inventories": {}, "observations": []}),
                        encoding="utf-8",
                    )
                    with self.assertRaises(MigrationStoreError):
                        MigrationStore(root)
                finally:
                    for child in root.iterdir():
                        child.unlink()
                    root.rmdir()

    def test_attempt_records_action_and_confirmation_digest(self):
        self.store.bootstrap(self.target, schema_set_digest=self.schema_set_digest)
        self.store.acquire(self.target, "run", "worker", "host")
        with self.assertRaises(MigrationStoreError):
            self.store.record_attempt_start(self.target, "bad-action", "m1", "a" * 64, "run", action="pause")
        with self.assertRaises(MigrationStoreError):
            self.store.record_attempt_start(self.target, "bad-digest", "m1", "a" * 64, "run", action="undo", confirmation_digest="not-a-digest")
        self.store.record_attempt_start(self.target, "undo-attempt", "m1", "a" * 64, "run", action="undo", confirmation_digest="a" * 64)
        attempt = self.store.read_state(self.target)["attempts"]["undo-attempt"]
        self.assertEqual(attempt["action"], "undo")
        self.assertEqual(attempt["confirmation_digest"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
