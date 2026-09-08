from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
