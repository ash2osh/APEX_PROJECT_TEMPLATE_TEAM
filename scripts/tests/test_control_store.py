from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from teamlib.config import Target
from teamlib.control_store import (
    ControllerError,
    MutexHeld,
    SetupRequired,
    TargetUncertain,
    ControlStore,
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


if __name__ == "__main__":
    unittest.main()
