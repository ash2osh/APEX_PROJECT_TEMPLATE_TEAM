from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import base64
import hashlib
import tempfile
import unittest
from types import SimpleNamespace

from teamlib.config import Target
from teamlib.fingerprints import inventory_from_rows
from teamlib.migrate import MigrationRunError, adopt_members, apply_plan
from teamlib.migration_bundle import BundleError, bundle_members, checksum_from_members, load_bundles
from teamlib.migration_store import (
    MigrationMutexHeld,
    MigrationStore,
    MigrationStoreError,
    SqlMigrationStore,
)


def _target() -> Target:
    return Target(
        project="team-template", role="developer", environment="development",
        connection="meta", instance_id="FREE", db_name="FREEPDB1", service="freep1",
        session_user="META", current_schema="META", alias=None, workspace_id=None,
        app_id=None, parsing_schema=None, ownership_mode="shared", binding_digest="a" * 64,
    )


class MigrationMemberTestCase(unittest.TestCase):
    MIGRATION_ID = "20260924T100000__alice__members"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-migration-members-")
        self.root = Path(self.temp.name)
        self.migrations = self.root / "migrations"
        self.migrations.mkdir()
        self.target = _target()
        self.store = MigrationStore(self.root / ".state")
        self.schema_set_digest = "a" * 64

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_bundle(self, migration_id: str = MIGRATION_ID, *, reversible: bool = True, body: str = "CREATE TABLE M(ID NUMBER);") -> None:
        # Tabs, trailing spaces and non-ASCII text must all survive storage byte-for-byte.
        (self.migrations / f"{migration_id}.sql").write_bytes(
            f"-- migration-version: 1\n-- target: tables\n-- destructive: false\n\n{body} -- café \t \n".encode()
        )
        (self.migrations / f"{migration_id}.verify.sql").write_bytes(b"")
        if reversible:
            (self.migrations / f"{migration_id}.down.sql").write_bytes(
                b"-- migration-version: 1\n-- destructive: true\n\nDROP TABLE M;\n"
            )
            (self.migrations / f"{migration_id}.down.verify.sql").write_bytes(b"")

    def migration(self, migration_id: str = MIGRATION_ID):
        return load_bundles(self.migrations)[migration_id]

    def acquired(self, token: str = "run") -> None:
        self.store.bootstrap(self.target, schema_set_digest=self.schema_set_digest)
        self.store.acquire(self.target, token, "worker", "host")


class BundleMemberHelperTests(MigrationMemberTestCase):
    def test_members_recompute_the_bundle_checksum(self):
        self.write_bundle()
        migration = self.migration()
        members = bundle_members(migration)
        self.assertEqual(len(members), 4)
        self.assertEqual(checksum_from_members(migration.id, members), migration.checksum)

    def test_forward_only_bundle_has_two_members(self):
        self.write_bundle(reversible=False)
        migration = self.migration()
        members = bundle_members(migration)
        self.assertEqual(sorted(members), [f"{migration.id}.sql", f"{migration.id}.verify.sql"])
        self.assertEqual(checksum_from_members(migration.id, members), migration.checksum)

    def test_partial_or_foreign_member_sets_are_refused(self):
        self.write_bundle()
        members = bundle_members(self.migration())
        partial = {name: data for name, data in members.items() if not name.endswith(".down.verify.sql")}
        with self.assertRaisesRegex(BundleError, "complete bundle"):
            checksum_from_members(self.MIGRATION_ID, partial)
        with self.assertRaisesRegex(BundleError, "complete bundle"):
            checksum_from_members(self.MIGRATION_ID, {**members, "other.sql": b""})


class FileStoreMemberTests(MigrationMemberTestCase):
    def test_round_trip_is_byte_exact_and_idempotent(self):
        self.write_bundle()
        migration = self.migration()
        self.acquired()
        self.store.store_members(self.target, migration, run_token="run", stored_by="alice")
        self.store.store_members(self.target, migration, run_token="run", stored_by="bob")
        self.assertEqual(self.store.read_members(self.target, migration.checksum), bundle_members(migration))
        self.assertEqual(self.store.list_member_bundles(self.target), {migration.checksum: migration.id})

    def test_member_writes_require_the_mutex_owner(self):
        self.write_bundle()
        self.acquired("owner")
        with self.assertRaises(MigrationMutexHeld):
            self.store.store_members(self.target, self.migration(), run_token="intruder", stored_by="mallory")
        self.assertEqual(self.store.list_member_bundles(self.target), {})

    def test_same_checksum_under_another_id_is_an_immutability_violation(self):
        self.write_bundle()
        migration = self.migration()
        self.acquired()
        self.store.store_members(self.target, migration, run_token="run", stored_by="alice")
        with self.store._locked() as data:
            data["bundles"][migration.checksum]["migration_id"] = "20260924T100000__mallory__other"
        with self.assertRaisesRegex(MigrationStoreError, "immutability"):
            self.store.store_members(self.target, migration, run_token="run", stored_by="alice")

    def test_corrupt_stored_member_is_refused_on_read(self):
        self.write_bundle()
        migration = self.migration()
        self.acquired()
        self.store.store_members(self.target, migration, run_token="run", stored_by="alice")
        with self.store._locked() as data:
            member = data["bundles"][migration.checksum]["members"][f"{migration.id}.sql"]
            member["base64"] = base64.b64encode(b"DROP TABLE EVERYTHING;").decode("ascii")
        with self.assertRaisesRegex(MigrationStoreError, "corrupt"):
            self.store.read_members(self.target, migration.checksum)

    def test_unknown_checksum_is_reported_not_stored(self):
        self.acquired()
        with self.assertRaisesRegex(MigrationStoreError, "not stored"):
            self.store.read_members(self.target, "c" * 64)
        with self.assertRaisesRegex(MigrationStoreError, "malformed"):
            self.store.read_members(self.target, "not-a-checksum")

    def test_history_event_without_stored_members_is_refused(self):
        self.acquired()
        inventory = inventory_from_rows(
            [{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "x"}],
            schema_set_digest=self.schema_set_digest,
        )
        self.store.record_inventory(self.target, inventory, run_token="run")
        self.store.ensure_observation(self.target, inventory.digest, run_token="run")
        with self.assertRaisesRegex(MigrationStoreError, "MIGRATION_MEMBERS_MISSING"):
            self.store.record_applied(
                self.target, self.MIGRATION_ID, "d" * 64, "tables", (), "commit", "alice",
                {"before": inventory.digest, "after": inventory.digest}, run_token="run",
            )


class ApplyStoresMembersTests(MigrationMemberTestCase):
    def profiles(self, **overrides):
        inventory = inventory_from_rows(
            [{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "stable"}],
            schema_set_digest=self.schema_set_digest,
        )
        value = {
            "store": self.store,
            "target": self.target,
            "payload_targets": {"tables": self.target, "code": self.target},
            "schema_set_digest": self.schema_set_digest,
            "bootstrap": True,
            "observe": lambda migration, phase: inventory,
            "require_observation": True,
            "verify": lambda migration: True,
        }
        value.update(overrides)
        return value

    def test_migrate_stores_members_before_the_payload_runs(self):
        self.write_bundle()
        migration = self.migration()
        seen: list[dict[str, bytes]] = []

        def execute(applied):
            seen.append(self.store.read_members(self.target, applied.checksum))

        apply_plan(self.migrations, self.profiles(execute=execute))
        self.assertEqual(seen, [bundle_members(migration)])
        self.assertEqual(self.store.read_history(self.target)[migration.id]["status"], "APPLIED")

    def test_member_refusal_runs_no_payload_and_releases_the_mutex(self):
        self.write_bundle()

        class RefusingStore(MigrationStore):
            def store_members(self, *args, **kwargs):
                raise MigrationStoreError("metadata unavailable")

        self.store = RefusingStore(self.root / ".refusing")
        with self.assertRaisesRegex(MigrationStoreError, "metadata unavailable"):
            apply_plan(self.migrations, self.profiles(execute=lambda migration: self.fail("payload ran")))
        state = self.store.read_state(self.target)
        self.assertEqual(state["attempts"], {})
        self.store.acquire(self.target, "next-run", "worker", "host")


class AdoptMembersTests(MigrationMemberTestCase):
    def seed_history(self, migration_id: str, checksum: str) -> None:
        """A history event recorded before members were stored (pre-upgrade metadata)."""
        with self.store._locked() as data:
            data["history"].append({
                "id": migration_id, "operation": "up", "checksum": checksum, "target": "tables",
                "dependencies": [], "source_commit": "c" * 40, "sequence": len(data["history"]) + 1,
                "applied_by": "alice", "observation": {},
            })

    def adopt(self, *, dry_run: bool = False):
        return adopt_members(
            self.store, self.target, load_bundles(self.migrations),
            dry_run=dry_run, actor="alice", worker_identity="alice", host="host",
        )

    def test_backfill_stores_local_files_and_reports_colleagues_missing(self):
        self.write_bundle()
        migration = self.migration()
        self.store.bootstrap(self.target, schema_set_digest=self.schema_set_digest)
        self.seed_history(migration.id, migration.checksum)
        self.seed_history("20260924T110000__bob__elsewhere", "e" * 64)

        preview = self.adopt(dry_run=True)
        self.assertEqual(preview["would_store"], [migration.id])
        self.assertEqual(preview["stored"], [])
        self.assertEqual(self.store.list_member_bundles(self.target), {})

        report = self.adopt()
        self.assertEqual(report["stored"], [migration.id])
        self.assertEqual(report["missing"], ["20260924T110000__bob__elsewhere"])
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(self.store.read_members(self.target, migration.checksum), bundle_members(migration))

        again = self.adopt()
        self.assertEqual(again["stored"], [])
        self.assertEqual(again["already_stored"], [migration.id])
        # The mutex was released after the backfill.
        self.store.acquire(self.target, "after", "worker", "host")

    def test_local_files_that_disagree_with_history_are_refused(self):
        self.write_bundle()
        self.store.bootstrap(self.target, schema_set_digest=self.schema_set_digest)
        self.seed_history(self.MIGRATION_ID, "f" * 64)
        with self.assertRaisesRegex(MigrationRunError, "checksum mismatch"):
            self.adopt()
        self.assertEqual(self.store.list_member_bundles(self.target), {})

    def test_local_files_that_disagree_with_stored_bundle_are_refused(self):
        self.write_bundle()
        migration = self.migration()
        self.store.bootstrap(self.target, schema_set_digest=self.schema_set_digest)
        self.seed_history(migration.id, migration.checksum)
        self.store.acquire(self.target, "run", "alice", "host")
        self.store.store_members(self.target, migration, run_token="run", stored_by="alice")
        self.store.release(self.target, "run")
        stored_members = self.store.read_members(self.target, migration.checksum)

        self.write_bundle(body="CREATE TABLE M(ID NUMBER, TAMPERED NUMBER);")

        with self.assertRaisesRegex(MigrationRunError, "checksum mismatch"):
            self.adopt(dry_run=True)
        self.assertEqual(self.store.list_member_bundles(self.target), {migration.checksum: migration.id})
        self.assertEqual(self.store.read_members(self.target, migration.checksum), stored_members)


class SqlStoreMemberTests(MigrationMemberTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.calls: list[tuple[str, str]] = []

    def runner(self, target, operation, driver, work, **kwargs):
        self.calls.append((operation, Path(driver).read_text(encoding="utf-8")))
        return SimpleNamespace(stdout="", stderr="", log_path=Path(work) / "fake.log", generated_driver=Path(driver))

    def sql_store(self) -> SqlMigrationStore:
        return SqlMigrationStore(self.target, runner=self.runner, work_root=self.root / "sql")

    def stored_rows(self, members: dict[str, bytes], migration_id: str, *, chunk: int = 900):
        """Rows the three member reads return, chunked the way DBMS_LOB.SUBSTR would."""
        rows = {"TEAM_BUNDLE|": [[migration_id, str(len(members))]], "TEAM_MEMBER|": [], "TEAM_MEMBER_CLOB|": []}
        for name, data in sorted(members.items()):
            rows["TEAM_MEMBER|"].append([name, str(len(data)), hashlib.sha256(data).hexdigest()])
            encoded = base64.b64encode(data).decode("ascii")
            pieces = [encoded[index:index + chunk] for index in range(0, len(encoded), chunk)] or [""]
            for part, piece in enumerate(pieces, start=1):
                rows["TEAM_MEMBER_CLOB|"].append([name, str(part), str(len(pieces)), piece])
        return rows

    def test_write_is_mutex_guarded_immutable_and_read_back(self):
        self.write_bundle(body="CREATE TABLE M(ID NUMBER);" + " -- padding" * 400)
        migration = self.migration()
        store = self.sql_store()
        rows = self.stored_rows(bundle_members(migration), migration.id)
        store._read_rows = lambda _target, _payload, prefix: rows[prefix]  # type: ignore[method-assign]
        store.store_members(self.target, migration, run_token="run", stored_by="alice")
        operation, payload = self.calls[-1]
        self.assertEqual(operation, "write")
        self.assertIn("MEMBER_WRITE_REFUSED", payload)
        self.assertIn("INSERT INTO TEAM_MIGRATION_BUNDLE", payload)
        self.assertEqual(payload.count("INSERT INTO TEAM_MIGRATION_MEMBER"), 4)
        self.assertIn("MIGRATION_MEMBER_IMMUTABILITY_VIOLATION", payload)
        self.assertLessEqual(max(len(line) for line in payload.splitlines()), 1200)

    def test_read_back_that_differs_from_the_local_bundle_is_refused(self):
        self.write_bundle()
        migration = self.migration()
        store = self.sql_store()
        tampered = dict(bundle_members(migration))
        tampered[f"{migration.id}.verify.sql"] = b"SELECT 1 FROM dual;"
        rows = self.stored_rows(tampered, migration.id)
        store._read_rows = lambda _target, _payload, prefix: rows[prefix]  # type: ignore[method-assign]
        with self.assertRaises(MigrationStoreError):
            store.store_members(self.target, migration, run_token="run", stored_by="alice")

    def test_missing_member_table_asks_for_a_metadata_upgrade(self):
        from teamlib.migration_store import MigrationSetupRequired
        from teamlib.sqlcl import SqlclError

        self.write_bundle()

        def failing_runner(*args, **kwargs):
            raise SqlclError("SQLcl reported an error (ORA-00942); see /nonexistent.log")

        store = SqlMigrationStore(self.target, runner=failing_runner, work_root=self.root / "sql")
        with self.assertRaisesRegex(MigrationSetupRequired, "bootstrap"):
            store.store_members(self.target, self.migration(), run_token="run", stored_by="alice")

    def test_history_event_payload_requires_stored_members(self):
        store = self.sql_store()
        store.record_event(
            self.target, self.MIGRATION_ID, "d" * 64, "tables", (), "commit", "alice",
            {"before": "b" * 64, "after": "c" * 64}, operation="up", run_token="run", attempt_id="attempt",
        )
        payload = self.calls[-1][1]
        self.assertIn("FROM TEAM_MIGRATION_BUNDLE", payload)
        self.assertIn("MIGRATION_MEMBERS_MISSING", payload)
        self.assertLess(payload.index("MIGRATION_MEMBERS_MISSING"), payload.index("INSERT INTO TEAM_MIGRATION_HISTORY"))


if __name__ == "__main__":
    unittest.main()
