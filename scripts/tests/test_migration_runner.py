from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from teamlib.config import Target
from teamlib.migrate import MigrationRunError, apply_plan
from teamlib.migration_store import MigrationStore


class MigrationRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-migration-runner-")
        self.root = Path(self.temp.name)
        self.migrations = self.root / "migrations"
        self.migrations.mkdir()
        self.migration_id = "20260907T100000__alice__one"
        (self.migrations / f"{self.migration_id}.sql").write_text("-- migration-version: 1\n-- target: tables\n-- destructive: false\n\nCREATE TABLE T(ID NUMBER);\n", encoding="utf-8")
        (self.migrations / f"{self.migration_id}.verify.sql").write_text("", encoding="utf-8")
        self.target = Target(
            project="team-template", role="developer", environment="development",
            connection="meta", instance_id="FREE", db_name="FREEPDB1", service="freep1",
            session_user="META", current_schema="META", alias=None, workspace_id=None,
            app_id=None, parsing_schema=None, ownership_mode="shared", binding_digest="a" * 64,
        )
        self.store = MigrationStore(self.root / ".state")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_apply_commits_attempt_and_history(self):
        calls = []
        report = apply_plan(self.migrations, {"store": self.store, "target": self.target, "execute": lambda migration: calls.append(migration.id), "verify": lambda migration: True, "bootstrap": True})
        self.assertEqual(report.applied, (self.migration_id,))
        self.assertEqual(calls, [self.migration_id])
        self.assertEqual(self.store.read_history(self.target)[self.migration_id]["status"], "APPLIED")

    def test_dry_run_does_not_bootstrap_or_execute(self):
        report = apply_plan(self.migrations, {"store": self.store, "target": self.target, "dry_run": True, "execute": lambda migration: self.fail("executed")})
        self.assertEqual(report.applied, ())
        with self.assertRaises(Exception):
            self.store.read_history(self.target)

    def test_known_failure_is_recorded_and_blocks_followup(self):
        def fail(migration):
            raise MigrationRunError("payload failed")
        with self.assertRaises(MigrationRunError):
            apply_plan(self.migrations, {"store": self.store, "target": self.target, "bootstrap": True, "execute": fail})
        self.assertTrue(self.store.read_state(self.target)["attempts"])


if __name__ == "__main__":
    unittest.main()
