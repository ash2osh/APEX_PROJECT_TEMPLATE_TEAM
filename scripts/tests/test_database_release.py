from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from dataclasses import replace
import hashlib
import json
import tempfile
import unittest

from teamlib.config import Target
from teamlib.fingerprints import inventory_from_rows
from teamlib.migrate import apply_plan, apply_undo
from teamlib.migration_store import MigrationStore, MigrationStoreError
from teamlib.release import (
    ReleaseError,
    _canonical,
    _read_archive_bytes,
    _verify_archive_members,
    build_schema_release_from_database,
    plan_release,
    verify_release,
)

A_ID = "20260924T120000__alice__accounts"
B_ID = "20260924T120100__bob__balances"
C_ID = "20260924T120200__carol__credits"


class DatabaseReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-db-release-")
        self.root = Path(self.temp.name)
        self.migrations = self.root / "migrations"
        self.migrations.mkdir()
        self.target = Target(
            project="team-template", role="developer", environment="development",
            connection="meta", instance_id="FREE@db-host", db_name="FREEPDB1", service="freep1",
            session_user="META", current_schema="META", alias=None, workspace_id=None,
            app_id=None, parsing_schema=None, ownership_mode="shared", binding_digest="a" * 64,
        )
        self.store = MigrationStore(self.root / ".state")
        self.schema_set_digest = "a" * 64
        self.step = 0

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add_migration(self, migration_id: str, table: str) -> None:
        (self.migrations / f"{migration_id}.sql").write_text(
            f"-- migration-version: 1\n-- target: tables\n-- destructive: false\n\nCREATE TABLE {table}(ID NUMBER);\n",
            encoding="utf-8",
        )
        (self.migrations / f"{migration_id}.verify.sql").write_text("", encoding="utf-8")
        (self.migrations / f"{migration_id}.down.sql").write_text(
            f"-- migration-version: 1\n-- destructive: false\n\nDROP TABLE {table};\n", encoding="utf-8",
        )
        (self.migrations / f"{migration_id}.down.verify.sql").write_text("", encoding="utf-8")

    def inventory(self, index: int):
        return inventory_from_rows(
            [{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": str(index)}],
            schema_set_digest=self.schema_set_digest,
        )

    def observe(self, _migration, phase):
        if phase == "after":
            self.step += 1
        return self.inventory(self.step)

    def profiles(self):
        return {
            "store": self.store, "target": self.target,
            "payload_targets": {"tables": self.target, "code": self.target},
            "schema_set_digest": self.schema_set_digest, "bootstrap": True,
            "observe": self.observe, "require_observation": True,
            "execute": lambda *_args: None, "verify": lambda *_args: True,
        }

    def ledger_with_a_revert(self) -> None:
        """Alice's A and Bob's B are applied, then B is reverted: three ledger events."""
        self.add_migration(A_ID, "ACCOUNTS")
        self.add_migration(B_ID, "BALANCES")
        apply_plan(self.migrations, self.profiles())
        apply_undo(self.migrations, B_ID, self.profiles())

    def build(self, name: str = "out", version: str = "1.0.0", **kwargs):
        return build_schema_release_from_database(
            self.store, self.target, version, self.root / name,
            built_by="alice", worker_identity="alice", host="host", **kwargs,
        )

    def test_release_carries_the_whole_ledger_including_the_revert(self):
        self.ledger_with_a_revert()
        manifest = self.build()
        self.assertEqual(manifest.format_version, 3)
        self.assertEqual(manifest.source_commit, "")
        self.assertEqual(
            [(event["sequence"], event["id"], event["operation"]) for event in manifest.events],
            [(1, A_ID, "up"), (2, B_ID, "up"), (3, B_ID, "down")],
        )
        self.assertEqual(manifest.source["kind"], "dev-database")
        self.assertEqual(manifest.source["instance_id"], "FREE@db-host")
        self.assertEqual(manifest.source["history_cut"], 3)
        self.assertEqual(manifest.source["history_digest"], hashlib.sha256(_canonical(list(manifest.events))).hexdigest())
        self.assertEqual(manifest.source["frontier_digest"], self.inventory(self.step).digest)
        # The reverted migration still ships, with the down pair a target will need.
        self.assertIn(f"release/migrations/{B_ID}.down.sql", manifest.payload_paths)
        self.assertEqual(verify_release(manifest.archive_path).archive_digest, manifest.archive_digest)
        ledger = self.store.read_releases(self.target)
        self.assertEqual(ledger["schema/v1.0.0"]["archive_digest"], manifest.archive_digest)
        # The mutex was released after the cut.
        self.store.acquire(self.target, "after-cut", "worker", "host")

    def test_the_same_cut_is_byte_identical_and_idempotent(self):
        self.ledger_with_a_revert()
        first = self.build("first")
        second = self.build("second")
        self.assertEqual(first.archive_digest, second.archive_digest)

    def test_a_version_cannot_be_rebound_to_a_different_cut(self):
        self.ledger_with_a_revert()
        self.build("first")
        self.add_migration(C_ID, "CREDITS")
        apply_plan(self.migrations, self.profiles())
        with self.assertRaisesRegex(ReleaseError, "already bound"):
            self.build("second")
        self.assertFalse((self.root / "second" / "release.tar").exists())
        self.assertEqual(self.build("third", version="1.1.0").source["history_cut"], 4)

    def test_unresolved_attempts_block_the_cut_and_free_the_mutex(self):
        self.ledger_with_a_revert()
        with self.store._locked() as data:
            data["attempts"]["stuck"] = {"state": "FAILED", "migration_id": C_ID, "run_token": "old"}
        with self.assertRaisesRegex(ReleaseError, "unresolved migration attempts"):
            self.build()
        self.store.acquire(self.target, "after-refusal", "worker", "host")

    def test_history_without_stored_files_names_the_backfill(self):
        self.ledger_with_a_revert()
        with self.store._locked() as data:
            data["bundles"].clear()
        with self.assertRaisesRegex(ReleaseError, "adopt-migration-members"):
            self.build()

    def test_a_frontier_that_moved_after_the_drift_check_is_refused(self):
        self.ledger_with_a_revert()
        with self.assertRaisesRegex(ReleaseError, "frontier moved"):
            self.build(expected_frontier="f" * 64)

    def test_production_metadata_is_refused(self):
        with self.assertRaisesRegex(ReleaseError, "production"):
            build_schema_release_from_database(
                self.store, replace(self.target, environment="production"), "1.0.0", self.root / "out",
                built_by="alice", worker_identity="alice", host="host",
            )

    def test_format_three_is_verified_but_not_yet_plannable(self):
        self.ledger_with_a_revert()
        manifest = self.build()
        with self.assertRaisesRegex(ReleaseError, "not implemented yet"):
            plan_release(manifest.archive_path, {}, {"role": "test", "environment": "test"})


class DatabaseReleaseVerificationTests(DatabaseReleaseTests):
    def tampered(self, mutate):
        self.ledger_with_a_revert()
        manifest = self.build()
        digest, members = _read_archive_bytes(manifest.archive_path)
        data = json.loads(members["release/MANIFEST.json"])
        mutate(data)
        members = {**members, "release/MANIFEST.json": json.dumps(data).encode("utf-8")}
        return lambda: _verify_archive_members(manifest.archive_path, digest, members)

    def test_non_contiguous_ledger_is_refused(self):
        def drop_middle(data):
            del data["events"][1]
        with self.assertRaises(ReleaseError):
            self.tampered(drop_middle)()

    def test_history_digest_must_match_the_events(self):
        def rewrite(data):
            data["events"][2]["operation"] = "up"
        with self.assertRaisesRegex(ReleaseError, "event order is invalid|history digest"):
            self.tampered(rewrite)()

    def test_digest_mismatch_alone_is_refused(self):
        def rehash(data):
            data["source"]["history_digest"] = "0" * 64
        with self.assertRaisesRegex(ReleaseError, "history digest"):
            self.tampered(rehash)()

    def test_a_git_commit_field_is_not_accepted_in_format_three(self):
        def add_commit(data):
            data["source_commit"] = "a" * 40
        with self.assertRaisesRegex(ReleaseError, "keys are not closed"):
            self.tampered(add_commit)()

    def test_packaged_bundle_without_a_ledger_event_is_refused(self):
        def forget_b(data):
            data["events"] = data["events"][:1]
            data["source"]["history_cut"] = 1
            data["source"]["history_digest"] = hashlib.sha256(_canonical(data["events"])).hexdigest()
        with self.assertRaisesRegex(ReleaseError, "no ledger event uses"):
            self.tampered(forget_b)()


class ReleaseLedgerStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-release-ledger-")
        self.target = Target(
            project="team-template", role="developer", environment="development",
            connection="meta", instance_id="FREE", db_name="FREEPDB1", service="freep1",
            session_user="META", current_schema="META", alias=None, workspace_id=None,
            app_id=None, parsing_schema=None, ownership_mode="shared", binding_digest="a" * 64,
        )
        self.store = MigrationStore(Path(self.temp.name))
        self.store.bootstrap(self.target, schema_set_digest="a" * 64)
        self.store.acquire(self.target, "run", "worker", "host")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def record(self, digest: str, **overrides):
        values = {"kind": "schema", "alias": None, "version": "1.2.3", "archive_digest": digest,
                  "source": {"history_cut": 1}, "built_by": "alice", "run_token": "run"}
        values.update(overrides)
        return self.store.record_release(self.target, **values)

    def test_release_keys_are_immutable_and_idempotent(self):
        self.assertEqual(self.record("a" * 64), "schema/v1.2.3")
        self.assertEqual(self.record("a" * 64), "schema/v1.2.3")
        with self.assertRaisesRegex(MigrationStoreError, "already bound"):
            self.record("b" * 64)
        self.assertEqual(self.record("b" * 64, kind="app", alias="hr"), "app/hr/v1.2.3")
        self.assertEqual(sorted(self.store.read_releases(self.target)), ["app/hr/v1.2.3", "schema/v1.2.3"])

    def test_malformed_release_identities_are_refused(self):
        for overrides in ({"version": "1.2"}, {"alias": "hr"}, {"kind": "app"}, {"archive_digest": "nope"}):
            with self.subTest(overrides=overrides), self.assertRaises(MigrationStoreError):
                self.record(overrides.pop("archive_digest", "a" * 64), **overrides)


class SqlReleaseLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-sql-release-ledger-")
        self.target = Target(
            project="team-template", role="developer", environment="development",
            connection="meta", instance_id="FREE", db_name="FREEPDB1", service="freep1",
            session_user="META", current_schema="META", alias=None, workspace_id=None,
            app_id=None, parsing_schema=None, ownership_mode="shared", binding_digest="a" * 64,
        )
        self.payloads: list[str] = []

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_write_is_mutex_guarded_and_immutable(self):
        from types import SimpleNamespace

        from teamlib.migration_store import SqlMigrationStore

        def runner(target, operation, driver, work, **kwargs):
            self.payloads.append(Path(driver).read_text(encoding="utf-8"))
            return SimpleNamespace(stdout="", stderr="")

        store = SqlMigrationStore(self.target, runner=runner, work_root=Path(self.temp.name))
        key = store.record_release(
            self.target, kind="schema", alias=None, version="2.0.0", archive_digest="c" * 64,
            source={"kind": "dev-database", "history_cut": 7}, built_by="alice", run_token="run",
        )
        self.assertEqual(key, "schema/v2.0.0")
        payload = self.payloads[-1]
        self.assertIn("RELEASE_WRITE_REFUSED", payload)
        self.assertIn("RELEASE_VERSION_TAKEN", payload)
        self.assertIn("INSERT INTO TEAM_RELEASE", payload)
        self.assertLess(payload.index("RELEASE_WRITE_REFUSED"), payload.index("INSERT INTO TEAM_RELEASE"))

    def test_taken_version_and_missing_table_are_explained(self):
        from teamlib.migration_store import MigrationSetupRequired, SqlMigrationStore
        from teamlib.sqlcl import SqlclError

        for detail, error, message in (
            ("ORA-20043: RELEASE_VERSION_TAKEN", MigrationStoreError, "already bound"),
            ("ORA-00942: table or view does not exist", MigrationSetupRequired, "bootstrap"),
        ):
            def runner(*args, _detail=detail, **kwargs):
                raise SqlclError(f"SQLcl reported an error ({_detail}); see /nonexistent.log")

            store = SqlMigrationStore(self.target, runner=runner, work_root=Path(self.temp.name))
            with self.subTest(detail=detail), self.assertRaisesRegex(error, message):
                store.record_release(
                    self.target, kind="schema", alias=None, version="2.0.0", archive_digest="c" * 64,
                    source={}, built_by="alice", run_token="run",
                )


if __name__ == "__main__":
    unittest.main()
