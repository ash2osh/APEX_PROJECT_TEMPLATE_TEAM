from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
import json
import tempfile
import unittest
from types import SimpleNamespace

from teamlib.config import Target
from teamlib.migration_store import MigrationMutexHeld, SqlMigrationStore
from teamlib.sqlcl import SqlclError


class SqlMetadataStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-sql-metadata-")
        self.root = Path(self.temp.name)
        self.target = Target(
            project="team-template", role="developer", environment="development",
            connection="meta", instance_id="FREE", db_name="FREEPDB1", service="freep1",
            session_user="META", current_schema="META", alias=None, workspace_id=None,
            app_id=None, parsing_schema=None, ownership_mode="shared", binding_digest="a" * 64,
        )
        self.calls: list[tuple[str, str]] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def runner(self, target, operation, driver, work, **kwargs):
        payload = Path(driver).read_text(encoding="utf-8")
        self.calls.append((operation, payload))
        return SimpleNamespace(
            identity={"SESSION_USER": "META", "CURRENT_SCHEMA": "META", "DB_NAME": "FREEPDB1", "SERVICE": "freep1", "INSTANCE_ID": "FREE"},
            completion={"operation": operation}, result_manifest={"status": "success"},
            stdout="", stderr="", log_path=Path(work) / "fake.log", generated_driver=Path(driver),
            argv=(), exit_code=0,
        )

    def test_bootstrap_and_mutex_use_sqlcl_and_nowait_transition(self):
        store = SqlMigrationStore(self.target, runner=self.runner, work_root=self.root)
        store.bootstrap(self.target, schema_set_digest="digest")
        store.acquire(self.target, "run-token", "worker", "host")
        store.release(self.target, "run-token")
        self.assertEqual([operation for operation, _ in self.calls], ["write", "write", "write"])
        self.assertIn("FOR UPDATE NOWAIT", self.calls[1][1])
        self.assertIn("COMMIT", self.calls[1][1])
        self.assertNotIn("password", "\n".join(payload for _, payload in self.calls).lower())

    def test_mutex_error_is_classified_from_sanitized_log(self):
        def held_runner(target, operation, driver, work, **kwargs):
            log = Path(work) / "fake.log"
            log.write_text("ORA-20001: MUTEX_HELD:run-token\n", encoding="utf-8")
            raise SqlclError(f"SQLcl failed; see {log}")

        store = SqlMigrationStore(self.target, runner=held_runner, work_root=self.root)
        with self.assertRaises(MigrationMutexHeld) as caught:
            store.acquire(self.target, "other", "worker", "host")
        self.assertEqual(caught.exception.owner_token, "run-token")

    def test_embedded_and_reference_ddl_define_metadata_v2(self):
        import teamlib.migration_store as migration_store

        reference = (Path(__file__).resolve().parents[2] / "scripts" / "sql" / "migration_metadata.sql").read_text(encoding="utf-8")
        embedded = migration_store._MIGRATION_BOOTSTRAP_SQL
        for ddl in (embedded, reference):
            self.assertIn("operation VARCHAR2(4) NOT NULL", ddl)
            self.assertIn("CONSTRAINT team_migration_history_operation_ck CHECK (operation IN ('up','down'))", ddl)
            self.assertIn("CREATE INDEX team_migration_history_id_ix ON TEAM_MIGRATION_HISTORY (id, applied_sequence)", ddl)
            self.assertIn("action VARCHAR2(16) NOT NULL", ddl)
            self.assertIn("CONSTRAINT team_migration_attempt_action_ck CHECK (action IN ('migrate','undo','redo'))", ddl)
            self.assertIn("confirmation_digest VARCHAR2(64)", ddl)
            self.assertIn("PRIMARY KEY (applied_sequence)", ddl)
            self.assertNotIn("PRIMARY KEY (id)", ddl)

    def test_sql_history_reads_repeated_ids_by_sequence_and_collapses(self):
        store = SqlMigrationStore(self.target, runner=self.runner, work_root=self.root)
        scalar_rows = [
            ["m1", "up", "a" * 64, "tables", "commit", "1", "now", "alice", "run", "up-attempt"],
            ["m1", "down", "a" * 64, "tables", "commit", "2", "now", "alice", "run", "down-attempt"],
        ]
        clob_rows = [
            ["m1", "1", "dependencies", "1", "2", "["],
            ["m1", "1", "dependencies", "2", "2", "]"],
            ["m1", "1", "observation", "1", "1", "{}"],
            ["m1", "2", "dependencies", "1", "1", "[]"],
            ["m1", "2", "observation", "1", "1", "{}"],
        ]

        def rows(_target, _payload, prefix):
            return scalar_rows if prefix == "TEAM_HISTORY|" else clob_rows

        store._read_rows = rows  # type: ignore[method-assign]
        history = store.read_history(self.target)
        self.assertEqual(history["m1"]["status"], "REVERTED")
        self.assertEqual(history["m1"]["operation"], "down")
        self.assertEqual(history["m1"]["sequence"], 2)


class GeneratedSqlLineLengthTests(unittest.TestCase):
    MAX_LINE = 1200

    def _capture_payload(self, call):
        seen: list[str] = []

        def runner(target, operation, driver, work, **kwargs):
            seen.append(Path(driver).read_text(encoding="utf-8"))
            raise RuntimeError("payload captured")

        try:
            call(runner)
        except Exception:
            pass
        return seen

    def test_a_large_inventory_manifest_emits_only_bounded_lines(self):
        from teamlib.sql_text import clob_builder
        manifest = json.dumps(
            {"version": 1, "objects": [
                {"name": f"TBL_{i:04d}", "type": "TABLE", "sha256": "a" * 64} for i in range(400)
            ]},
            sort_keys=True, separators=(",", ":"),
        )
        built = clob_builder("v_manifest", manifest)
        self.assertGreater(len(manifest), 40000)
        self.assertLess(max(len(line) for line in built.splitlines()), self.MAX_LINE)
        # The value is emitted once, not once per reference.
        self.assertEqual(built.count("CREATETEMPORARY"), 1)

    def test_clob_literal_is_gone(self):
        import teamlib.migration_store as store
        self.assertFalse(hasattr(store, "_clob_literal"))


class ManifestEncodingTests(unittest.TestCase):
    def test_manifest_json_is_ascii_so_900_characters_is_900_bytes(self):
        import inspect

        from teamlib.migration_store import SqlMigrationStore

        source = inspect.getsource(SqlMigrationStore.record_inventory)
        self.assertIn("manifest_json = json.dumps(", source)
        self.assertNotIn(
            "ensure_ascii=False",
            source,
            "read_inventories chunks the manifest 900 characters at a time and "
            "base64-encodes each chunk through SQL RAW, which is capped at 2000 "
            "bytes; non-ASCII characters overflow it",
        )

    def test_ascii_escaped_manifest_round_trips(self):
        import json

        from teamlib.fingerprints import inventory_from_manifest, inventory_from_rows

        inventory = inventory_from_rows(
            [{"logical_owner": "tables", "object_type": "TABLE", "object_name": "MITARBEITER_Ü", "definition": "x"}],
            schema_set_digest="a" * 64,
        )
        encoded = json.dumps(inventory.as_dict(), sort_keys=True, separators=(",", ":"))
        self.assertTrue(all(ord(char) < 128 for char in encoded))
        self.assertEqual(inventory_from_manifest(json.loads(encoded)).digest, inventory.digest)


if __name__ == "__main__":
    unittest.main()
