from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
import contextlib
import io
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import team
from teamlib.config import Target
from teamlib.fingerprints import inventory_from_rows
from teamlib.migrate import MigrationRunError, apply_plan, apply_redo, apply_undo
from teamlib.migration_runtime import migration_callbacks
from teamlib.migration_store import (
    MigrationSetupRequired,
    MigrationStore,
    MigrationStoreError,
)
from teamlib.sqlcl import SqlclError


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
        self.schema_set_digest = "a" * 64

    def inventory(self, rows):
        return inventory_from_rows(rows, schema_set_digest=self.schema_set_digest)

    def profiles(self, **overrides):
        value = {
            "store": self.store,
            "target": self.target,
            "payload_targets": {"tables": self.target, "code": self.target},
            "schema_set_digest": self.schema_set_digest,
        }
        value.update(overrides)
        return value

    def add_migration(self, migration_id, *, destructive=False, down_destructive=False, dependency=None):
        dependency_line = f"-- depends-on: {dependency[0]} sha256:{dependency[1]}\n" if dependency else ""
        (self.migrations / f"{migration_id}.sql").write_text(
            f"-- migration-version: 1\n-- target: tables\n-- destructive: {'true' if destructive else 'false'}\n"
            f"{dependency_line}\nCREATE TABLE {migration_id[-3:].upper()}(ID NUMBER);\n",
            encoding="utf-8",
        )
        (self.migrations / f"{migration_id}.verify.sql").write_text("", encoding="utf-8")
        if down_destructive is not None:
            (self.migrations / f"{migration_id}.down.sql").write_text(
                f"-- migration-version: 1\n-- destructive: {'true' if down_destructive else 'false'}\n\nDROP TABLE {migration_id[-3:].upper()};\n",
                encoding="utf-8",
            )
            (self.migrations / f"{migration_id}.down.verify.sql").write_text("", encoding="utf-8")

    def remove_default_migration(self):
        for suffix in (".sql", ".verify.sql"):
            (self.migrations / f"{self.migration_id}{suffix}").unlink()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_apply_commits_attempt_and_history(self):
        calls = []
        inventory = self.inventory([{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "stable"}])
        report = apply_plan(self.migrations, self.profiles(
            execute=lambda migration: calls.append(migration.id),
            verify=lambda migration: True, bootstrap=True,
            observe=lambda migration, phase: inventory,
            require_observation=True,
        ))
        self.assertEqual(report.applied, (self.migration_id,))
        self.assertEqual(calls, [self.migration_id])
        self.assertEqual(self.store.read_history(self.target)[self.migration_id]["status"], "APPLIED")

    def test_cli_verification_callback_rejects_a_failed_assertion(self):
        verify_path = self.migrations / f"{self.migration_id}.verify.sql"
        verify_path.write_text(
            "SELECT 'postcondition' assertion_name, 'FAIL' status FROM dual;\n",
            encoding="utf-8",
        )
        config = SimpleNamespace()
        with patch("teamlib.migration_runtime.profile_target", return_value=self.target), patch(
            "teamlib.migration_runtime.run_sqlcl",
            return_value=SimpleNamespace(stdout="TEAM_ASSERT|postcondition|FAIL\n"),
        ):
            _, verify, _ = migration_callbacks(config, self.root, "a" * 64)
            with self.assertRaisesRegex(MigrationRunError, "postcondition"):
                apply_plan(
                    self.migrations,
                    self.profiles(
                        bootstrap=True,
                        execute=lambda *_args: None,
                        verify=verify,
                    ),
                )
        state = self.store.read_state(self.target)
        self.assertEqual(
            {attempt["state"] for attempt in state["attempts"].values()},
            {"FAILED"},
        )
        self.assertEqual(self.store.read_history(self.target), {})

    def test_dry_run_does_not_bootstrap_or_execute(self):
        report = apply_plan(self.migrations, self.profiles(dry_run=True, execute=lambda migration: self.fail("executed")))
        self.assertEqual(report.applied, ())
        with self.assertRaises(MigrationSetupRequired):
            self.store.read_history(self.target)

    def test_known_failure_is_recorded_and_blocks_followup(self):
        def fail(migration):
            raise MigrationRunError("payload failed")
        with self.assertRaises(MigrationRunError):
            apply_plan(self.migrations, self.profiles(bootstrap=True, execute=fail))
        self.assertTrue(self.store.read_state(self.target)["attempts"])

    def test_deterministic_failure_after_attempt_start_carries_recovery_context(self):
        def fail(migration):
            raise MigrationRunError("payload failed")
        try:
            apply_plan(self.migrations, self.profiles(bootstrap=True, execute=fail))
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            recovery = error.recovery
        self.assertIsNotNone(recovery)
        self.assertEqual(len(recovery["run_token"]), 32)
        self.assertEqual(recovery["migration_id"], self.migration_id)
        self.assertEqual(recovery["operation"], "migrate")
        self.assertEqual(recovery["phase"], "execute")
        self.assertEqual(recovery["result"], "FAILED")
        self.assertTrue(recovery["mutex_retained"])
        self.assertIn(recovery["attempt_id"], self.store.read_state(self.target)["attempts"])

    def test_verification_failure_wraps_without_losing_the_original_message(self):
        try:
            apply_plan(self.migrations, self.profiles(
                bootstrap=True, execute=lambda migration: None, verify=lambda migration: False,
            ))
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertIn("verification failed", str(error))
            self.assertEqual(error.recovery["phase"], "verify")
            self.assertEqual(error.recovery["result"], "FAILED")

    def test_unknown_payload_failure_reports_unknown_result(self):
        def fail(migration):
            raise SqlclError("SQLcl timed out; target state is unknown")
        try:
            apply_plan(self.migrations, self.profiles(bootstrap=True, execute=fail))
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertEqual(error.recovery["result"], "UNKNOWN")
            self.assertEqual(error.recovery["phase"], "execute")

    def test_observation_failure_after_attempt_start_carries_recovery_context(self):
        before_inventory = self.inventory(
            [{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "stable"}]
        )

        def observe(migration, phase):
            if phase == "after":
                raise MigrationRunError("boom")
            return before_inventory

        try:
            apply_plan(self.migrations, self.profiles(
                bootstrap=True, execute=lambda migration: None, verify=lambda migration: True,
                observe=observe, require_observation=True,
            ))
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertEqual(error.recovery["phase"], "observe-after")
            self.assertEqual(error.recovery["result"], "FAILED")

    def test_inventory_failure_after_attempt_start_carries_recovery_context(self):
        inventory = self.inventory(
            [{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "stable"}]
        )

        class FailSecondRecordInventoryStore(MigrationStore):
            def __init__(self, root):
                super().__init__(root)
                self._record_inventory_calls = 0

            def record_inventory(self, store_target, inventory, *, run_token):
                self._record_inventory_calls += 1
                if self._record_inventory_calls == 2:
                    raise MigrationRunError("inventory write failed")
                return super().record_inventory(store_target, inventory, run_token=run_token)

        store = FailSecondRecordInventoryStore(self.root / "inventory-failure-state")
        try:
            apply_plan(
                self.migrations,
                {
                    **self.profiles(),
                    "store": store,
                    "bootstrap": True,
                    "execute": lambda *_args: None,
                    "verify": lambda *_args: True,
                    "observe": lambda *_args: inventory,
                    "require_observation": True,
                },
            )
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertEqual(error.recovery["phase"], "record-inventory")
            self.assertEqual(error.recovery["result"], "FAILED")
            self.assertTrue(error.recovery["mutex_retained"])

    def test_pre_attempt_failure_does_not_claim_recovery_is_required(self):
        try:
            apply_plan(self.migrations, self.profiles(
                bootstrap=True, execute=lambda migration: None, verify=lambda migration: True,
                observe=lambda migration, phase: None, require_observation=True,
            ))
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertIsNone(error.recovery)

    def test_cli_prints_recovery_context_an_operator_can_feed_to_recover_migration(self):
        def fail(migration):
            raise MigrationRunError("payload failed")
        try:
            apply_plan(self.migrations, self.profiles(bootstrap=True, execute=fail))
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            real_error = error
        self.assertIsNotNone(real_error.recovery)

        stderr = io.StringIO()
        with patch("team._online", side_effect=real_error):
            with contextlib.redirect_stderr(stderr):
                code = team.main(["doctor"])
        self.assertEqual(code, 3)

        lines = [line for line in stderr.getvalue().splitlines() if line.strip()]
        self.assertEqual(lines[0], str(real_error))
        rendered = json.loads(lines[1])
        self.assertEqual(rendered["recovery"], real_error.recovery)

        parsed = team._parser().parse_args([
            "recover-migration", rendered["recovery"]["run_token"], "--evidence", "evidence.json",
        ])
        self.assertEqual(parsed.run_token, real_error.recovery["run_token"])

    def test_committed_event_with_lost_ack_is_unknown_without_failed_rewrite(self):
        inventory = self.inventory(
            [
                {
                    "owner": "tables",
                    "object_type": "TABLE",
                    "object_name": "T",
                    "definition": "stable",
                }
            ]
        )

        class CommitThenLoseEventAckStore(MigrationStore):
            def record_event(self, *args, **kwargs):
                super().record_event(*args, **kwargs)
                try:
                    raise SqlclError(
                        "SQLcl timed out; target state is unknown"
                    )
                except SqlclError as exc:
                    raise MigrationStoreError(
                        "metadata result unavailable"
                    ) from exc

        store = CommitThenLoseEventAckStore(self.root / "lost-ack-state")
        try:
            apply_plan(
                self.migrations,
                {
                    **self.profiles(),
                    "store": store,
                    "bootstrap": True,
                    "execute": lambda *_args: None,
                    "verify": lambda *_args: True,
                    "observe": lambda *_args: inventory,
                    "require_observation": True,
                },
            )
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertRegex(str(error), "unknown")
            self.assertEqual(error.recovery["phase"], "record-event")
            self.assertEqual(error.recovery["result"], "UNKNOWN")

        state = store.read_state(self.target)
        self.assertEqual(
            {attempt["state"] for attempt in state["attempts"].values()},
            {"APPLIED"},
        )
        self.assertEqual(len(store.read_history(self.target)), 1)
        self.assertEqual(
            state["mutex"]["owner_token"],
            next(iter(state["attempts"].values()))["run_token"],
        )

    def test_unknown_attempt_state_update_does_not_release_mutex(self):
        inventory = self.inventory(
            [
                {
                    "owner": "tables",
                    "object_type": "TABLE",
                    "object_name": "T",
                    "definition": "stable",
                }
            ]
        )

        class LoseAttemptStateAckStore(MigrationStore):
            def record_attempt_state(self, *args, **kwargs):
                try:
                    raise SqlclError(
                        "SQLcl timed out; target state is unknown"
                    )
                except SqlclError as exc:
                    raise MigrationStoreError(
                        "attempt state result unavailable"
                    ) from exc

        store = LoseAttemptStateAckStore(self.root / "attempt-state-lost-ack")
        try:
            apply_plan(
                self.migrations,
                {
                    **self.profiles(),
                    "store": store,
                    "bootstrap": True,
                    "execute": lambda *_args: (_ for _ in ()).throw(
                        MigrationRunError("known payload failure")
                    ),
                    "verify": lambda *_args: True,
                    "observe": lambda *_args: inventory,
                    "require_observation": True,
                },
            )
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertRegex(str(error), "attempt-state result is unknown")
            self.assertEqual(error.recovery["phase"], "record-attempt-state")
            self.assertEqual(error.recovery["result"], "UNKNOWN")
        state = store.read_state(self.target)
        self.assertEqual(
            {attempt["state"] for attempt in state["attempts"].values()},
            {"RUNNING"},
        )
        self.assertTrue(state["mutex"]["owner_token"])

    def test_unknown_mutex_release_keeps_completed_evidence(self):
        inventory = self.inventory(
            [
                {
                    "owner": "tables",
                    "object_type": "TABLE",
                    "object_name": "T",
                    "definition": "stable",
                }
            ]
        )

        class LoseReleaseAckStore(MigrationStore):
            def release(self, *args, **kwargs):
                try:
                    raise SqlclError(
                        "SQLcl timed out; target state is unknown"
                    )
                except SqlclError as exc:
                    raise MigrationStoreError(
                        "mutex release result unavailable"
                    ) from exc

        store = LoseReleaseAckStore(self.root / "release-lost-ack")
        try:
            apply_plan(
                self.migrations,
                {
                    **self.profiles(),
                    "store": store,
                    "bootstrap": True,
                    "execute": lambda *_args: None,
                    "verify": lambda *_args: True,
                    "observe": lambda *_args: inventory,
                    "require_observation": True,
                },
            )
            self.fail("expected MigrationRunError")
        except MigrationRunError as error:
            self.assertRegex(str(error), "mutex release is unknown")
            self.assertEqual(error.recovery["phase"], "release")
            self.assertEqual(error.recovery["result"], "UNKNOWN")
            self.assertTrue(error.recovery["mutex_retained"])
        state = store.read_state(self.target)
        self.assertEqual(
            {attempt["state"] for attempt in state["attempts"].values()},
            {"APPLIED"},
        )
        self.assertTrue(state["mutex"]["owner_token"])

    def test_multiple_migrations_extend_one_observed_frontier(self):
        second_id = "20260907T100001__alice__two"
        (self.migrations / f"{second_id}.sql").write_text(
            "-- migration-version: 1\n-- target: tables\n-- destructive: false\n\nCREATE TABLE T2(ID NUMBER);\n",
            encoding="utf-8",
        )
        (self.migrations / f"{second_id}.verify.sql").write_text("", encoding="utf-8")
        inventories = [
            self.inventory([{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "zero"}]),
            self.inventory([{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "one"}]),
            self.inventory([{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "two"}]),
        ]
        phase = {"index": 0}

        def observe(_migration, current_phase):
            if current_phase == "before":
                return inventories[phase["index"]]
            value = inventories[phase["index"] + 1]
            phase["index"] += 1
            return value

        report = apply_plan(
            self.migrations,
            self.profiles(
                bootstrap=True,
                execute=lambda migration: None,
                verify=lambda migration: True,
                observe=observe,
                require_observation=True,
            ),
        )
        self.assertEqual(report.applied, (self.migration_id, second_id))
        observations = self.store.read_state(self.target)["observations"]
        self.assertEqual([item["sequence"] for item in observations], [0, 1, 2])
        self.assertEqual([item["predecessor_sequence"] for item in observations], [None, 0, 1])

    def test_undo_redo_execute_directional_members_in_lifo_order(self):
        self.remove_default_migration()
        a_id = "20260907T110000__alice__aaa"
        b_id = "20260907T110001__alice__bbb"
        self.add_migration(a_id, down_destructive=False)
        from teamlib.migration_bundle import load_bundles
        a_checksum = load_bundles(self.migrations)[a_id].checksum
        self.add_migration(b_id, down_destructive=False, dependency=(a_id, a_checksum))
        inventories = [
            self.inventory([{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": str(index)}])
            for index in range(7)
        ]
        phase = {"index": 0}
        executed = []
        verified = []

        def observe(_migration, current_phase):
            if current_phase == "before":
                return inventories[phase["index"]]
            phase["index"] += 1
            return inventories[phase["index"]]

        def execute(migration, action, sql_path):
            executed.append((action, sql_path.read_bytes()))

        def verify(migration, action, verify_path):
            verified.append((action, verify_path.read_bytes() if verify_path else None))
            return True

        common = self.profiles(
            bootstrap=True, observe=observe, execute=execute, verify=verify,
            require_observation=True,
        )
        apply_plan(self.migrations, common)
        apply_undo(self.migrations, b_id, {**common, "bootstrap": False})
        apply_undo(self.migrations, a_id, {**common, "bootstrap": False})
        apply_redo(self.migrations, a_id, {**common, "bootstrap": False})
        apply_redo(self.migrations, b_id, {**common, "bootstrap": False})
        self.assertEqual([item[0] for item in executed], ["migrate", "migrate", "undo", "undo", "redo", "redo"])
        self.assertEqual(executed[0][1], load_bundles(self.migrations)[a_id].sql_bytes)
        self.assertEqual(executed[2][1], load_bundles(self.migrations)[b_id].down_sql_bytes)
        self.assertEqual(executed[4][1], load_bundles(self.migrations)[a_id].sql_bytes)
        self.assertEqual([item[0] for item in verified], ["migrate", "migrate", "undo", "undo", "redo", "redo"])
        self.assertEqual(self.store.read_history(self.target)[b_id]["status"], "APPLIED")

    def test_destructive_confirmation_is_exact_and_recorded_on_attempt(self):
        self.remove_default_migration()
        destructive_id = "20260907T120000__alice__danger"
        self.add_migration(destructive_id, destructive=True, down_destructive=True)
        from teamlib.migration_bundle import load_bundles
        migration = load_bundles(self.migrations)[destructive_id]
        inventory = self.inventory([{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "stable"}])
        common = self.profiles(
            bootstrap=True, observe=lambda _migration, _phase: inventory,
            execute=lambda *_args: None, verify=lambda *_args: True,
            require_observation=True,
        )
        with self.assertRaises(MigrationRunError):
            apply_plan(self.migrations, common, confirmation=True)
        self.assertFalse(self.store.read_state(self.target)["attempts"])
        document = {"version": 1, "confirmations": [{
            "migration_id": destructive_id, "action": "migrate", "bundle_checksum": migration.checksum,
            "payload_target_state_key": self.target.state_key, "confirmed": True,
        }]}
        report = apply_plan(self.migrations, common, confirmation=document)
        self.assertEqual(report.applied, (destructive_id,))
        attempts = self.store.read_state(self.target)["attempts"]
        attempt = next(iter(attempts.values()))
        self.assertEqual(attempt["action"], "migrate")
        self.assertRegex(attempt["confirmation_digest"], r"^[0-9a-f]{64}$")

    def test_undo_and_redo_require_the_same_confirmation_contract(self):
        self.remove_default_migration()
        migration_id = "20260907T130000__alice__danger"
        self.add_migration(migration_id, destructive=True, down_destructive=True)
        from teamlib.migration_bundle import load_bundles
        migration = load_bundles(self.migrations)[migration_id]
        inventory = self.inventory([{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "stable"}])
        common = self.profiles(
            bootstrap=True, observe=lambda _migration, _phase: inventory,
            execute=lambda *_args: None, verify=lambda *_args: True,
            require_observation=True,
        )
        migrate_doc = {"version": 1, "confirmations": [{
            "migration_id": migration_id, "action": "migrate", "bundle_checksum": migration.checksum,
            "payload_target_state_key": self.target.state_key, "confirmed": True,
        }]}
        apply_plan(self.migrations, common, confirmation=migrate_doc)
        with self.assertRaises(MigrationRunError):
            apply_undo(self.migrations, migration_id, {**common, "bootstrap": False})
        undo_doc = {"version": 1, "confirmations": [{
            "migration_id": migration_id, "action": "undo", "bundle_checksum": migration.checksum,
            "payload_target_state_key": self.target.state_key, "confirmed": True,
        }]}
        apply_undo(self.migrations, migration_id, {**common, "bootstrap": False}, confirmation=undo_doc)
        with self.assertRaises(MigrationRunError):
            apply_redo(self.migrations, migration_id, {**common, "bootstrap": False}, confirmation=undo_doc)
        redo_doc = {"version": 1, "confirmations": [{
            "migration_id": migration_id, "action": "redo", "bundle_checksum": migration.checksum,
            "payload_target_state_key": self.target.state_key, "confirmed": True,
        }]}
        apply_redo(self.migrations, migration_id, {**common, "bootstrap": False}, confirmation=redo_doc)

    def test_dry_run_returns_false_confirmation_template_without_locking(self):
        self.remove_default_migration()
        migration_id = "20260907T140000__alice__danger"
        self.add_migration(migration_id, destructive=True, down_destructive=True)
        report = apply_plan(self.migrations, self.profiles(dry_run=True))
        self.assertEqual(report.selected, (migration_id,))
        self.assertEqual(report.confirmation_template["confirmations"][0]["confirmed"], False)
        with self.assertRaises(MigrationSetupRequired):
            self.store.read_history(self.target)

    def test_known_and_unknown_payload_failures_leave_recovery_evidence(self):
        inventory = self.inventory([{"owner": "tables", "object_type": "TABLE", "object_name": "T", "definition": "stable"}])
        common = self.profiles(
            bootstrap=True, observe=lambda _migration, _phase: inventory,
            verify=lambda *_args: True, require_observation=True,
        )
        with self.assertRaises(MigrationRunError):
            apply_plan(self.migrations, {**common, "execute": lambda *_args: (_ for _ in ()).throw(MigrationRunError("known"))})
        state = self.store.read_state(self.target)
        self.assertEqual(next(iter(state["attempts"].values()))["state"], "FAILED")
        recovery_root = self.root / "second-state"
        second_store = MigrationStore(recovery_root)
        with self.assertRaises(MigrationRunError):
            apply_plan(self.migrations, {
                **common,
                "store": second_store,
                "execute": lambda *_args: (_ for _ in ()).throw(SqlclError("transport timed out")),
            })
        second_store.bootstrap(self.target, schema_set_digest=self.schema_set_digest)
        state = second_store.read_state(self.target)
        self.assertEqual(next(iter(state["attempts"].values()))["state"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
