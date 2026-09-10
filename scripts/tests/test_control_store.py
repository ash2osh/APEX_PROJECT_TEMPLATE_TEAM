from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import base64
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from teamlib.config import Target
from teamlib.control_store import (
    ControllerError,
    MutexHeld,
    SetupRequired,
    SyncState,
    TargetUncertain,
    ControlStore,
    SqlControlStore,
)


class ControlStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-control-test-")
        self.store = ControlStore(Path(self.temp.name))
        self.target = Target(
            project="team-template", role="developer", environment="development",
            connection="docker-demo", instance_id="FREE", db_name="FREEPDB1",
            service="freep1", session_user="DEMO", current_schema="DEMO",
            alias="checkout", workspace_id=5402650006222933, app_id=100,
            parsing_schema="DEMO", ownership_mode="shared", binding_digest="a" * 64,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_setup_seeds_and_registration_is_shared_roster(self):
        self.store.setup_state([self.target])
        self.store.register_app(self.target, "checkout-a", "host-a", "alice")
        self.store.register_app(self.target, "checkout-b", "host-b", "bob")
        self.assertEqual(len(self.store.list_registry(self.target)), 2)
        sync = self.store.read_app_sync_state(self.target.physical_key)
        self.assertEqual(sync.generation, 1)
        self.assertIsNone(sync.owner_token)

    def test_acquire_release_and_generation(self):
        self.store.setup_state([self.target])
        self.store.acquire_app(self.target.physical_key, "run-a", "checkout-a", "host", "alice")
        with self.assertRaises(MutexHeld):
            self.store.acquire_app(self.target.physical_key, "run-b", "checkout-b", "host", "bob")
        self.store.mark_payload_starting(self.target.physical_key, "run-a")
        self.store.release_app(self.target.physical_key, "run-a", confirmed_success=True)
        sync = self.store.read_app_sync_state(self.target.physical_key)
        self.assertEqual(sync.generation, 2)
        self.assertFalse(sync.is_uncertain)

    def test_failed_payload_is_uncertain_until_recovery(self):
        self.store.setup_state([self.target])
        self.store.acquire_app(self.target.physical_key, "run-a", "checkout-a", "host", "alice")
        self.store.mark_payload_starting(self.target.physical_key, "run-a")
        self.store.release_app(self.target.physical_key, "run-a", confirmed_success=False)
        with self.assertRaises(TargetUncertain):
            self.store.acquire_app(self.target.physical_key, "run-b", "checkout-b", "host", "bob")
        before = self.store.read_app_sync_state(self.target.physical_key).generation
        self.store.recover_app_lock(self.target.physical_key, evidence=Path(self.temp.name), run_token=None)
        after = self.store.read_app_sync_state(self.target.physical_key)
        self.assertEqual(after.generation, before + 1)
        self.assertFalse(after.is_uncertain)

    def test_missing_setup_is_not_silently_created_by_acquire(self):
        with self.assertRaises(SetupRequired):
            self.store.acquire_app(self.target.physical_key, "run", "checkout", "host", "user")

    def test_physical_identity_ignores_binding_details(self):
        other = Target(**{**self.target.__dict__, "connection": "other", "binding_digest": "b" * 64})
        self.assertEqual(self.target.physical_key, other.physical_key)
        self.assertNotEqual(self.target.state_key, other.state_key)

    def test_single_owner_requires_transfer_evidence(self):
        single = Target(**{**self.target.__dict__, "ownership_mode": "single"})
        self.store.setup_state([single])
        self.store.register_app(single, "checkout-a", "host-a", "alice")
        with self.assertRaises(MutexHeld):
            self.store.register_app(single, "checkout-b", "host-b", "bob")

    def test_controller_contract_is_authoritative(self):
        contract = {
            "version": 1,
            "controllers": {
                "FREE": {
                    "metadata_schema": "META",
                    "metadata_connection": "meta",
                    "expected_user": "META",
                    "current_schema": "META",
                }
            },
        }
        metadata = Target(**{
            **self.target.__dict__,
            "current_schema": "META",
            "session_user": "META",
            "alias": None,
            "workspace_id": None,
            "app_id": None,
            "parsing_schema": None,
        })
        self.store.validate_controller(metadata, contract)
        bad = Target(**{**metadata.__dict__, "current_schema": "OTHER"})
        with self.assertRaises(ControllerError):
            self.store.validate_controller(bad, contract)

    def test_store_refuses_to_open_without_a_locking_primitive(self):
        from teamlib import control_store
        from teamlib.control_store import ControlStore, ControlStoreError
        with tempfile.TemporaryDirectory(prefix="team-lockless-") as directory:
            with patch.object(control_store, "fcntl", None), patch.object(control_store, "msvcrt", None):
                with self.assertRaisesRegex(ControlStoreError, "advisory file locking"):
                    ControlStore(Path(directory) / "state")


class SqlRecoverAppLockPredicateTests(unittest.TestCase):
    """SqlControlStore needs a live SQLcl connection to run for real, so this
    captures the generated SQL predicate through a mocked runner instead of
    asserting on database state, the way test_sql_metadata_store.py does for
    SqlMigrationStore."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-sql-control-")
        self.metadata = Target(
            project="team-template", role="developer", environment="development",
            connection="meta", instance_id="FREE", db_name="FREEPDB1", service="freep1",
            session_user="META", current_schema="META", alias=None, workspace_id=None,
            app_id=None, parsing_schema=None, ownership_mode="shared", binding_digest="a" * 64,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _captured_predicate(self, run_token):
        captured: list[str] = []

        def runner(target, operation, driver, work, **kwargs):
            captured.append(Path(driver).read_text(encoding="utf-8"))
            raise RuntimeError("payload captured")

        evidence = Path(self.temp.name) / "evidence.txt"
        evidence.write_text("worker terminated; capture retained\n", encoding="utf-8")
        store = SqlControlStore(self.metadata, runner=runner, work_root=Path(self.temp.name) / "work")
        with self.assertRaises(RuntimeError):
            store.recover_app_lock("key", evidence=evidence, run_token=run_token)
        return captured[0]

    def test_a_named_run_token_also_matches_a_released_uncertain_row(self):
        # A failure after the payload started clears owner_token but leaves
        # is_uncertain set, so the predicate must match that row too -- not
        # only a row where owner_token still equals the supplied token.
        payload = self._captured_predicate("a" * 32)
        self.assertIn("owner_token IS NULL AND is_uncertain = 1", payload)
        self.assertIn(f"owner_token = '{'a' * 32}'", payload)

    def test_omitting_the_run_token_keeps_the_original_broad_predicate(self):
        payload = self._captured_predicate(None)
        self.assertIn("owner_token IS NOT NULL OR is_uncertain = 1", payload)


class SqlControlStoreContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-sql-control-")
        self.addCleanup(self.temp.cleanup)
        self.metadata = Target(
            project="team-template", role="developer", environment="development",
            connection="docker-demo", instance_id="FREE", db_name="FREEPDB1",
            service="freep1", session_user="META", current_schema="META",
            alias=None, workspace_id=None, app_id=None, parsing_schema=None,
            ownership_mode="shared", binding_digest="a" * 64,
        )

    @staticmethod
    def _mutex_stdout(target_key: str) -> str:
        # read_app_sync_state parses eight base64 fields; NULL is encoded as
        # CHR(1) by b64_sql and decoded back to "".
        fields = [target_key, "", "", "", "", "", "1", "0"]
        encoded = "|".join(
            base64.b64encode((value or "\x01").encode("utf-8")).decode("ascii")
            for value in fields
        )
        return f"TEAM_MUTEX|{encoded}\n"

    def _store(self) -> SqlControlStore:
        stdout = self._mutex_stdout("key")

        def fake_runner(target, operation, driver, work, **kwargs):
            class Result:
                pass

            result = Result()
            result.stdout = stdout
            return result

        return SqlControlStore(self.metadata, runner=fake_runner, work_root=Path(self.temp.name))

    def test_acquire_app_returns_a_sync_state(self):
        observed = self._store().acquire_app("key", "run", "checkout", "host", "user")
        self.assertIsInstance(
            observed,
            SyncState,
            "acquire_app is annotated -> SyncState; returning None diverges from "
            "ControlStore.acquire_app and breaks any caller that reads the result",
        )
        self.assertEqual(observed.target_key, "key")

    def test_every_mutex_transition_returns_a_state(self):
        import inspect

        for name in ("acquire_app", "mark_payload_starting", "release_app", "recover_app_lock"):
            source = inspect.getsource(getattr(SqlControlStore, name))
            self.assertIn(
                "return self.read_app_sync_state(target_key)",
                source,
                f"{name} is annotated -> SyncState but does not return one",
            )


if __name__ == "__main__":
    unittest.main()
