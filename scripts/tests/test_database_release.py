from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace
import tempfile
import unittest

from teamlib.config import Target
from teamlib.fingerprints import inventory_from_rows
from teamlib.migrate import apply_forward, apply_plan, apply_redo, apply_undo
from teamlib.migration_store import MigrationStore, MigrationStoreError
from teamlib.control_store import ControlStore
from teamlib.sqlcl import SqlclUnknownResult
from teamlib.release import (
    ReleaseError,
    _canonical,
    _read_archive_bytes,
    _verify_archive_members,
    build_schema_release_from_database,
    build_app_release_from_database,
    apply_release,
    plan_release,
    release_frontier_inventory,
    release_master_contract,
    release_migration_files,
    verify_release,
)

A_ID = "20260924T120000__alice__accounts"
B_ID = "20260924T120100__bob__balances"
C_ID = "20260924T120200__carol__credits"
D_ID = "20260924T120300__dana__debits"


class _EmptyAppReleaseMigrationStore:
    def __init__(self, target):
        self.target = target
        self.owner = None
        self.releases = {}
        self.events = []

    def acquire(self, target, run_token, worker_identity, host):
        if self.owner is not None:
            raise AssertionError("migration mutex is already held")
        self.owner = run_token

    def read_state(self, target):
        return {
            "attempts": {},
            "observations": [{"sequence": len(self.events), "after": "b" * 64}],
        }

    def read_events(self, target):
        return list(self.events)

    def read_releases(self, target):
        return dict(self.releases)

    def record_release(self, target, *, kind, alias, version, archive_digest, source, built_by, run_token):
        if run_token != self.owner:
            raise AssertionError("release record must own the migration mutex")
        key = f"app/{alias}/v{version}"
        if key in self.releases and self.releases[key]["archive_digest"] != archive_digest:
            raise MigrationStoreError("release is already bound to a different archive")
        self.releases[key] = {"archive_digest": archive_digest, "source": dict(source)}

    def release(self, target, run_token):
        if run_token != self.owner:
            raise AssertionError("wrong migration mutex owner")
        self.owner = None


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
            sqlcl_build="26.2.2.233.1901", built_by="alice", worker_identity="alice", host="host", **kwargs,
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
        self.assertEqual(manifest.toolchain.get("sqlcl"), "26.2.2.233.1901")
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

    def test_an_unknown_record_outcome_keeps_the_archive(self):
        self.ledger_with_a_revert()

        def lost(*_args, **_kwargs):
            raise MigrationStoreError("release record failed") from SqlclUnknownResult("SQLcl timed out; target state is unknown")

        self.store.record_release = lost
        with self.assertRaisesRegex(ReleaseError, "outcome is unknown; the archive was kept") as caught:
            self.build()
        kept = [path for path in (self.root / "out").rglob("*.tar")] if (self.root / "out").exists() else []
        self.assertTrue(kept, str(caught.exception))
        self.store.acquire(self.target, "after-unknown", "worker", "host")

    def test_a_refused_record_removes_the_archive(self):
        self.ledger_with_a_revert()

        def refused(*_args, **_kwargs):
            raise MigrationStoreError("release schema/v1.0.0 is already bound to a different archive")

        self.store.record_release = refused
        with self.assertRaisesRegex(ReleaseError, "already bound"):
            self.build()
        self.assertEqual(list((self.root / "out").rglob("*.tar")) if (self.root / "out").exists() else [], [])

    def test_a_frontier_that_moved_after_the_drift_check_is_refused(self):
        self.ledger_with_a_revert()
        with self.assertRaisesRegex(ReleaseError, "frontier moved"):
            self.build(expected_frontier="f" * 64)

    def test_production_metadata_is_refused(self):
        with self.assertRaisesRegex(ReleaseError, "production"):
            build_schema_release_from_database(
                self.store, replace(self.target, environment="production"), "1.0.0", self.root / "out",
                sqlcl_build="26.2.2.233.1901", built_by="alice", worker_identity="alice", host="host",
            )

    def test_format_three_replays_from_fresh_and_earlier_release_history(self):
        self.ledger_with_a_revert()
        manifest = self.build()
        target = {"role": "test", "environment": "test"}

        fresh = plan_release(manifest.archive_path, {}, target)
        self.assertEqual(
            [(event["sequence"], event["id"], event["operation"]) for event in fresh.events],
            [(1, A_ID, "up")],
        )
        self.assertEqual(fresh.pending, (A_ID,))
        executed = []
        apply_release(
            manifest.archive_path,
            target,
            fresh,
            history={},
            apply_migrations=lambda pending, reviewed: executed.extend(
                (event["id"], event["operation"]) for event in reviewed.events
            ),
        )
        self.assertEqual(executed, [(A_ID, "up")])

        def make_replay_store(name):
            replay_root = Path(self.temp.name) / name
            replay_root.mkdir()
            replay_migrations = replay_root / "migrations"
            replay_migrations.mkdir()
            for filename, data in release_migration_files(manifest.archive_path).items():
                (replay_migrations / filename).write_bytes(data)
            replay_target = replace(
                self.target, role="test", environment="test", connection=f"{name}-meta",
                instance_id=f"TEST@{name}", db_name="TEST", service="testpdb",
            )
            store = MigrationStore(replay_root / ".state")
            inventory = self.inventory(0)
            profiles = {
                "store": store,
                "target": replay_target,
                "payload_targets": {"tables": replay_target, "code": replay_target},
                "schema_set_digest": self.schema_set_digest,
                "bootstrap": True,
                "require_observation": True,
                "observe": lambda *_args: inventory,
                "execute": lambda *_args: None,
                "verify": lambda *_args: True,
                "source_commit": "db-release:" + manifest.source["history_digest"],
            }
            return replay_migrations, replay_target, store, profiles

        fresh_migrations, _fresh_target, fresh_store, fresh_profiles = make_replay_store("fresh")
        for event in fresh.events:
            apply_forward(fresh_migrations, event["id"], fresh_profiles)
        self.assertEqual(
            [(event["id"], event["operation"]) for event in fresh_store.read_events(_fresh_target)],
            [(A_ID, "up")],
        )

        # A second target that stopped at the earlier up/up cut receives the
        # remaining down transition and reaches the same database frontier.
        replay_migrations, replay_target, replay_store, replay_profiles = make_replay_store("earlier")
        for event in manifest.events[:2]:
            apply_forward(replay_migrations, event["id"], replay_profiles)
        earlier = replay_store.read_history(replay_target)
        replay = plan_release(manifest.archive_path, earlier, target)
        self.assertEqual(
            [(event["sequence"], event["id"], event["operation"]) for event in replay.events],
            [(3, B_ID, "down")],
        )
        apply_undo(replay_migrations, B_ID, replay_profiles)
        replayed_events = replay_store.read_events(replay_target)
        self.assertEqual(
            [(event["sequence"], event["id"], event["operation"]) for event in replayed_events],
            [(1, A_ID, "up"), (2, B_ID, "up"), (3, B_ID, "down")],
        )

    def test_format_three_replay_rejects_history_that_is_not_an_archive_prefix(self):
        self.ledger_with_a_revert()
        manifest = self.build()
        with self.assertRaisesRegex(ReleaseError, "not an exact archive prefix"):
            plan_release(
                manifest.archive_path,
                {A_ID: {"status": "REVERTED", "checksum": self.store.read_history(self.target)[A_ID]["checksum"], "sequence": 1}},
                {"role": "test", "environment": "test"},
            )

    def test_format_three_replay_skips_a_migration_applied_and_reverted_after_target_cut(self):
        self.add_migration(A_ID, "ACCOUNTS")
        apply_plan(self.migrations, self.profiles())
        apply_undo(self.migrations, A_ID, self.profiles())
        manifest = self.build()

        plan = plan_release(
            manifest.archive_path, {}, {"role": "test", "environment": "test"}
        )

        self.assertEqual(plan.events, ())
        self.assertEqual(plan.pending, ())
        report = apply_release(
            manifest.archive_path,
            {"role": "test", "environment": "test"},
            plan,
            history={},
        )
        self.assertEqual(report.status, "planned")

    def test_format_three_replay_can_replan_after_skipping_noop_before_later_event(self):
        self.ledger_with_a_revert()
        self.add_migration(C_ID, "CREDITS")
        apply_plan(self.migrations, self.profiles())
        manifest = self.build()
        target = {"role": "test", "environment": "test"}
        plan = plan_release(manifest.archive_path, {}, target)

        replay_root = self.root / "replay"
        replay_root.mkdir()
        replay_migrations = replay_root / "migrations"
        replay_migrations.mkdir()
        for filename, data in release_migration_files(manifest.archive_path).items():
            (replay_migrations / filename).write_bytes(data)
        replay_target = replace(
            self.target, role="test", environment="test", connection="replay-meta",
            instance_id="TEST@replay", db_name="TEST", service="testpdb",
        )
        replay_store = MigrationStore(replay_root / ".state")
        replay_step = 0

        def observe(_migration, phase):
            nonlocal replay_step
            if phase == "after":
                replay_step += 1
            return self.inventory(replay_step)

        profiles = {
            "store": replay_store,
            "target": replay_target,
            "payload_targets": {"tables": replay_target, "code": replay_target},
            "schema_set_digest": self.schema_set_digest,
            "bootstrap": True,
            "require_observation": True,
            "observe": observe,
            "execute": lambda *_args: None,
            "verify": lambda *_args: True,
        }
        for event in plan.events:
            profiles["source_commit"] = (
                f"db-release:{manifest.source['history_digest']}:{event['sequence']}:{plan.replay_from}"
            )
            if event["operation"] == "up":
                apply_forward(replay_migrations, event["id"], profiles)
            else:
                apply_undo(replay_migrations, event["id"], profiles)

        replayed_history = replay_store.read_history(replay_target)
        self.assertEqual(set(replayed_history), {A_ID, C_ID})
        self.assertEqual(replayed_history[C_ID]["sequence"], 2)
        replanned = plan_release(manifest.archive_path, replayed_history, target)
        self.assertEqual(replanned.events, ())
        self.assertEqual(replanned.replay_from, 4)

        self.add_migration(D_ID, "DEBITS")
        apply_plan(self.migrations, self.profiles())
        next_manifest = self.build("next", version="1.0.1")
        next_plan = plan_release(next_manifest.archive_path, replayed_history, target)
        self.assertEqual([(event["sequence"], event["id"]) for event in next_plan.events], [(5, D_ID)])
        for filename, data in release_migration_files(next_manifest.archive_path).items():
            (replay_migrations / filename).write_bytes(data)
        profiles["source_commit"] = (
            f"db-release:{next_manifest.source['history_digest']}:5:0"
        )
        apply_forward(replay_migrations, D_ID, profiles)
        next_history = replay_store.read_history(replay_target)
        next_replanned = plan_release(next_manifest.archive_path, next_history, target)
        self.assertEqual(next_replanned.events, ())
        self.assertEqual(next_replanned.replay_from, 5)

    def test_format_three_replay_does_not_skip_down_redo_for_target_applied_migration(self):
        self.ledger_with_a_revert()
        apply_redo(self.migrations, B_ID, self.profiles())
        self.add_migration(C_ID, "CREDITS")
        apply_plan(self.migrations, self.profiles())
        manifest = self.build()
        target = {"role": "test", "environment": "test"}

        replay_root = self.root / "applied-target"
        replay_root.mkdir()
        replay_migrations = replay_root / "migrations"
        replay_migrations.mkdir()
        for filename, data in release_migration_files(manifest.archive_path).items():
            (replay_migrations / filename).write_bytes(data)
        replay_target = replace(
            self.target, role="test", environment="test", connection="applied-meta",
            instance_id="TEST@applied", db_name="TEST", service="testpdb",
        )
        replay_store = MigrationStore(replay_root / ".state")
        replay_step = 0

        def observe(_migration, phase):
            nonlocal replay_step
            if phase == "after":
                replay_step += 1
            return self.inventory(replay_step)

        profiles = {
            "store": replay_store,
            "target": replay_target,
            "payload_targets": {"tables": replay_target, "code": replay_target},
            "schema_set_digest": self.schema_set_digest,
            "bootstrap": True,
            "require_observation": True,
            "observe": observe,
            "execute": lambda *_args: None,
            "verify": lambda *_args: True,
        }
        for migration_id in (A_ID, B_ID):
            profiles["source_commit"] = "seed-release"
            apply_forward(replay_migrations, migration_id, profiles)

        applied_history = replay_store.read_history(replay_target)
        plan = plan_release(manifest.archive_path, applied_history, target)
        self.assertEqual(
            [(event["sequence"], event["id"], event["operation"]) for event in plan.events],
            [(3, B_ID, "down"), (4, B_ID, "up"), (5, C_ID, "up")],
        )

        for event in plan.events:
            profiles["source_commit"] = (
                f"db-release:{manifest.source['history_digest']}:{event['sequence']}:{plan.replay_from}"
            )
            if event["operation"] == "down":
                apply_undo(replay_migrations, event["id"], profiles)
            elif replay_store.read_history(replay_target).get(event["id"], {}).get("status") == "REVERTED":
                apply_redo(replay_migrations, event["id"], profiles)
            else:
                apply_forward(replay_migrations, event["id"], profiles)

        replayed_history = replay_store.read_history(replay_target)
        replanned = plan_release(manifest.archive_path, replayed_history, target)
        self.assertEqual(replanned.events, ())

    def test_format_three_replay_redoes_then_undoes_target_reverted_migration(self):
        self.ledger_with_a_revert()
        apply_redo(self.migrations, B_ID, self.profiles())
        apply_undo(self.migrations, B_ID, self.profiles())
        self.add_migration(C_ID, "CREDITS")
        apply_plan(self.migrations, self.profiles())
        manifest = self.build()
        target = {"role": "test", "environment": "test"}

        replay_root = self.root / "reverted-target"
        replay_root.mkdir()
        replay_migrations = replay_root / "migrations"
        replay_migrations.mkdir()
        for filename, data in release_migration_files(manifest.archive_path).items():
            (replay_migrations / filename).write_bytes(data)
        replay_target = replace(
            self.target, role="test", environment="test", connection="reverted-meta",
            instance_id="TEST@reverted", db_name="TEST", service="testpdb",
        )
        replay_store = MigrationStore(replay_root / ".state")
        replay_step = 0

        def observe(_migration, phase):
            nonlocal replay_step
            if phase == "after":
                replay_step += 1
            return self.inventory(replay_step)

        profiles = {
            "store": replay_store,
            "target": replay_target,
            "payload_targets": {"tables": replay_target, "code": replay_target},
            "schema_set_digest": self.schema_set_digest,
            "bootstrap": True,
            "require_observation": True,
            "observe": observe,
            "execute": lambda *_args: None,
            "verify": lambda *_args: True,
        }
        for migration_id in (A_ID, B_ID):
            profiles["source_commit"] = "seed-release"
            apply_forward(replay_migrations, migration_id, profiles)
        profiles["source_commit"] = "seed-release"
        apply_undo(replay_migrations, B_ID, profiles)

        reverted_history = replay_store.read_history(replay_target)
        plan = plan_release(manifest.archive_path, reverted_history, target)
        self.assertEqual(
            [(event["sequence"], event["id"], event["operation"]) for event in plan.events],
            [(4, B_ID, "up"), (5, B_ID, "down"), (6, C_ID, "up")],
        )

        for event in plan.events:
            profiles["source_commit"] = (
                f"db-release:{manifest.source['history_digest']}:{event['sequence']}:{plan.replay_from}"
            )
            row = replay_store.read_history(replay_target).get(event["id"])
            if event["operation"] == "down":
                apply_undo(replay_migrations, event["id"], profiles)
            elif row is not None and row["status"] == "REVERTED":
                apply_redo(replay_migrations, event["id"], profiles)
            else:
                apply_forward(replay_migrations, event["id"], profiles)

        replayed_history = replay_store.read_history(replay_target)
        replanned = plan_release(manifest.archive_path, replayed_history, target)
        self.assertEqual(replanned.events, ())


class AppDatabaseReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-app-db-release-")
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "targets").mkdir()
        (self.repo / "targets" / "masters.json").write_bytes(b'{"version":1,"masters":[]}\n')
        check_root = self.repo / "ci" / "app-checks"
        (check_root / "hr").mkdir(parents=True)
        declaration = {
            "version": 1,
            "alias": "hr",
            "page_ids": [1],
            "checks": [
                {
                    "id": "objects", "page_id": 1, "kind": "select",
                    "verify_sql": "hr/objects.verify.sql", "expected_objects": ["HR"],
                },
                {
                    "id": "smoke", "page_id": 1, "kind": "flow",
                    "flow": "hr/smoke.flow.json",
                    "steps": [{
                        "action": "navigate", "path": "/ords/r/hr/home",
                        "expected_visible_text": "HR",
                    }],
                },
            ],
        }
        (check_root / "hr.json").write_text(json.dumps(declaration) + "\n", encoding="utf-8")
        (check_root / "hr" / "objects.verify.sql").write_text(
            "SELECT 'hr_objects' assertion_name, 'PASS' status FROM dual;\n", encoding="utf-8"
        )
        (check_root / "hr" / "smoke.flow.json").write_text("{}\n", encoding="utf-8")
        metadata = Target(
            project="team-template", role="developer", environment="development",
            connection="meta", instance_id="FREE@db-host", db_name="FREEPDB1", service="freep1",
            session_user="META", current_schema="META", alias=None, workspace_id=None,
            app_id=None, parsing_schema=None, ownership_mode="shared", binding_digest="a" * 64,
        )
        self.metadata = metadata
        self.app = replace(metadata, alias="hr", workspace_id=42, app_id=120, parsing_schema="HR")
        self.app_store = ControlStore(self.root / "control")
        self.app_store.setup_state([self.app])
        self.app_store.register_app(self.app, "checkout-a", "host-a", "builder")
        self.migration_store = _EmptyAppReleaseMigrationStore(metadata)
        self.tree = {
            "application.apx": b"application\n",
            ".apex/apexlang.json": b'{"format":"APEXLANG"}\n',
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _capture(self, *, second_tree=None):
        calls = {"count": 0}

        def capture(target, *, held_by, repo, control_store, persist, **_kwargs):
            calls["count"] += 1
            state = control_store.read_app_sync_state(target.physical_key)
            work = Path(repo) / "scratch" / f"fake-capture-{calls['count']}"
            work.mkdir(parents=True)
            selected_tree = second_tree if calls["count"] == 2 and second_tree is not None else self.tree
            return SimpleNamespace(tree=selected_tree, work_dir=work, before_sync=state, after_sync=state)

        return capture, calls

    def _locks(self, *, status="KNOWN", pages=()):
        def read(target):
            return SimpleNamespace(status=status, pages=pages, alias=target.alias, app_id=target.app_id)
        return read

    def test_paused_capture_builds_and_verifies_format3_app_release(self):
        capture, calls = self._capture()
        manifest = build_app_release_from_database(
            self.repo, self.app, self.metadata, self.app_store, self.migration_store,
            "hr", "1.2.3", self.root / "out", sqlcl_build="26.2.2.233.1901",
            checkout_uuid="checkout-a", built_by="builder", host="host-a",
            capture=capture, page_locks=self._locks(),
        )
        verified = verify_release(manifest.archive_path)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(verified.format_version, 3)
        self.assertEqual(verified.kind, "app")
        self.assertEqual(verified.source["app_generation"], 1)
        self.assertEqual(verified.source["app_tree_digest"], verified.app_tree_digests["hr"])
        self.assertEqual(verified.source["history_cut"], 0)
        self.assertEqual(verified.source["master_contract_digest"], hashlib.sha256((self.repo / "targets" / "masters.json").read_bytes()).hexdigest())
        self.assertEqual(verified.source["app_checks_digest"], verified.app_checks_digest)
        self.assertEqual(release_master_contract(manifest.archive_path), {"version": 1, "masters": []})
        self.assertEqual(verified.required_migrations, ())
        self.assertIn("app/hr/v1.2.3", self.migration_store.releases)
        self.assertIsNone(self.app_store.read_app_sync_state(self.app.physical_key).owner_token)

    def test_app_release_refuses_changed_capture_and_releases_mutexes(self):
        capture, calls = self._capture(second_tree={**self.tree, "application.apx": b"changed\n"})
        with self.assertRaisesRegex(ReleaseError, "changed between paused captures"):
            build_app_release_from_database(
                self.repo, self.app, self.metadata, self.app_store, self.migration_store,
                "hr", "1.2.3", self.root / "out", sqlcl_build="26.2.2.233.1901",
                checkout_uuid="checkout-a", built_by="builder", host="host-a",
                capture=capture, page_locks=self._locks(),
            )
        self.assertEqual(calls["count"], 2)
        self.assertFalse(self.migration_store.releases)
        self.assertIsNone(self.app_store.read_app_sync_state(self.app.physical_key).owner_token)

    def test_app_release_refuses_unknown_page_locks_before_capture(self):
        capture, calls = self._capture()
        with self.assertRaisesRegex(ReleaseError, "KNOWN empty page-lock report"):
            build_app_release_from_database(
                self.repo, self.app, self.metadata, self.app_store, self.migration_store,
                "hr", "1.2.3", self.root / "out", sqlcl_build="26.2.2.233.1901",
                checkout_uuid="checkout-a", built_by="builder", host="host-a",
                capture=capture, page_locks=self._locks(status="UNKNOWN"),
            )
        self.assertEqual(calls["count"], 0)
        self.assertIsNone(self.app_store.read_app_sync_state(self.app.physical_key).owner_token)

    def test_required_migrations_are_the_applied_set_at_the_locked_cut(self):
        migration_id = "20260924T120000__alice__accounts"
        self.migration_store.events = [{
            "id": migration_id, "operation": "up", "checksum": "c" * 64,
            "sequence": 1, "applied_sequence": 1, "status": "APPLIED",
        }]
        capture, _calls = self._capture()
        manifest = build_app_release_from_database(
            self.repo, self.app, self.metadata, self.app_store, self.migration_store,
            "hr", "1.2.3", self.root / "out", sqlcl_build="26.2.2.233.1901",
            checkout_uuid="checkout-a", built_by="builder", host="host-a",
            capture=capture, page_locks=self._locks(),
        )
        verified = verify_release(manifest.archive_path)
        self.assertEqual(
            verified.required_migrations,
            ({"id": migration_id, "checksum": "c" * 64},),
        )


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


    def test_schema_release_carries_its_verified_frontier_inventory(self):
        self.ledger_with_a_revert()
        manifest = self.build()
        self.assertIn("release/frontier/inventory.json", manifest.payload_paths)
        self.assertEqual(release_frontier_inventory(manifest.archive_path).digest, manifest.source["frontier_digest"])

    def frontier_members(self, mutate):
        self.ledger_with_a_revert()
        manifest = self.build()
        digest, members = _read_archive_bytes(manifest.archive_path)
        members = dict(members)
        mutate(members)
        # Reseal the payload records so only the frontier check can object.
        records = [
            {"path": path, "length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            for path, raw in sorted(members.items())
            if path != "release/MANIFEST.json"
        ]
        data = json.loads(members["release/MANIFEST.json"])
        data["payload"] = records
        data["payload_paths"] = [record["path"] for record in records]
        data["source_tree"] = hashlib.sha256(_canonical(records)).hexdigest()
        members["release/MANIFEST.json"] = json.dumps(data).encode("utf-8")
        return lambda: _verify_archive_members(manifest.archive_path, digest, members)

    def test_a_tampered_frontier_inventory_is_refused(self):
        def rewrite(members):
            data = json.loads(members["release/frontier/inventory.json"])
            data["schema_set_digest"] = "0" * 64
            members["release/frontier/inventory.json"] = json.dumps(data).encode("utf-8")
        with self.assertRaisesRegex(ReleaseError, "frontier inventory is malformed"):
            self.frontier_members(rewrite)()

    def test_a_frontier_inventory_from_another_state_is_refused(self):
        def repoint(data):
            data["source"]["frontier_digest"] = "0" * 64
        with self.assertRaisesRegex(ReleaseError, "frontier inventory does not match its frontier digest"):
            self.tampered(repoint)()

    def test_a_missing_frontier_inventory_is_refused(self):
        def drop(members):
            del members["release/frontier/inventory.json"]
        with self.assertRaisesRegex(ReleaseError, "missing its frontier inventory"):
            self.frontier_members(drop)()

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
