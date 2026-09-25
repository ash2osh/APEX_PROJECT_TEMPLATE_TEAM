from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import base64

from teamlib.config import Target
from teamlib.control_store import ControlStore, ControlStoreError, SqlControlStore


class PublishAcknowledgementStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-publish-ack-test-")
        self.root = Path(self.temp.name)
        self.target = Target(
            project="team", role="developer", environment="development", connection="fake",
            instance_id="FREE", db_name="FREEPDB1", service="freep1", session_user="DEMO",
            current_schema="DEMO", alias="hr", workspace_id=10, app_id=101,
            parsing_schema="HR_DATA", ownership_mode="shared", binding_digest="h" * 64,
        )
        self.store = ControlStore(self.root / "state")
        self.store.setup_state([self.target])
        self.store.register_app(self.target, "checkout-a", "host-a", "alice")

    def register_preparation(self, prep_id="prep-1", digest=None, roster=None):
        self.store.register_publish_preparation(
            prep_id,
            digest or "a" * 64,
            [(self.target, "hr", roster if roster is not None else ("checkout-a",))],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_acknowledgement_records_digest_checkout_host_user_and_time(self):
        digest = "a" * 64
        self.register_preparation("prep-1", digest)
        self.store.record_publish_acknowledgement(
            self.target,
            "prep-1",
            digest,
            "checkout-a",
            "host-a",
            "alice",
        )

        rows = self.store.list_publish_acknowledgements(
            self.target, "prep-1", digest
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].preparation_id, "prep-1")
        self.assertEqual(rows[0].preparation_digest, digest)
        self.assertEqual(rows[0].target_key, self.target.physical_key)
        self.assertEqual(rows[0].checkout_uuid, "checkout-a")
        self.assertEqual(rows[0].host, "host-a")
        self.assertEqual(rows[0].acknowledged_by_user, "alice")
        self.assertTrue(rows[0].acknowledged_at)

    def test_acknowledgement_must_match_registered_checkout_identity(self):
        self.register_preparation()
        with self.assertRaisesRegex(ControlStoreError, "registered checkout"):
            self.store.record_publish_acknowledgement(
                self.target,
                "prep-1",
                "a" * 64,
                "checkout-b",
                "host-b",
                "bob",
            )

        with self.assertRaisesRegex(ControlStoreError, "host and user must match"):
            self.store.record_publish_acknowledgement(
                self.target,
                "prep-1",
                "a" * 64,
                "checkout-a",
                "host-other",
                "alice",
            )

    def test_repeated_setup_state_preserves_registry_and_acknowledgements(self):
        digest = "b" * 64
        self.register_preparation("prep-2", digest)
        self.store.record_publish_acknowledgement(
            self.target, "prep-2", digest, "checkout-a", "host-a", "alice"
        )

        self.store.setup_state([self.target])

        self.assertEqual(
            [entry.checkout_uuid for entry in self.store.list_registry(self.target)],
            ["checkout-a"],
        )
        self.assertEqual(
            len(self.store.list_publish_acknowledgements(self.target, "prep-2", digest)),
            1,
        )
        self.assertEqual(
            self.store.list_publish_preparation("prep-2")[0].checkout_roster,
            ("checkout-a",),
        )

    def test_same_preparation_id_with_another_digest_is_not_selected(self):
        self.register_preparation("prep-3", "d" * 64)
        self.store.record_publish_acknowledgement(
            self.target, "prep-3", "d" * 64, "checkout-a", "host-a", "alice"
        )

        self.assertEqual(
            self.store.list_publish_acknowledgements(self.target, "prep-3", "a" * 64),
            [],
        )
        self.assertEqual(
            len(self.store.list_publish_acknowledgements(self.target, "prep-3", "d" * 64)),
            1,
        )

    def test_acknowledgement_refuses_unknown_or_wrong_digest_preparation(self):
        with self.assertRaisesRegex(ControlStoreError, "preparation"):
            self.store.record_publish_acknowledgement(
                self.target, "missing", "e" * 64, "checkout-a", "host-a", "alice"
            )

        self.register_preparation("prep-4", "f" * 64)
        with self.assertRaisesRegex(ControlStoreError, "preparation"):
            self.store.record_publish_acknowledgement(
                self.target, "prep-4", "e" * 64, "checkout-a", "host-a", "alice"
            )

    def test_preparation_snapshot_is_immutable_and_matches_current_roster(self):
        digest = "1" * 64
        self.register_preparation("prep-5", digest)
        self.register_preparation("prep-5", digest)
        self.assertEqual(len(self.store.list_publish_preparation("prep-5")), 1)

        with self.assertRaisesRegex(ControlStoreError, "immutable"):
            self.store.register_publish_preparation(
                "prep-5", "2" * 64,
                [(self.target, "hr", ("checkout-a",))],
            )

        with self.assertRaisesRegex(ControlStoreError, "roster changed"):
            self.store.register_publish_preparation(
                "prep-6", "3" * 64,
                [(self.target, "hr", ("late-checkout",))],
            )


class SqlPublishAcknowledgementBootstrapTests(unittest.TestCase):
    def test_setup_state_adds_ack_table_without_altering_existing_metadata(self):
        temp = tempfile.TemporaryDirectory(prefix="team-sql-publish-ack-")
        self.addCleanup(temp.cleanup)
        metadata = Target(
            project="team", role="developer", environment="development", connection="meta",
            instance_id="FREE", db_name="FREEPDB1", service="freep1", session_user="META",
            current_schema="META", alias=None, workspace_id=None, app_id=None,
            parsing_schema=None, ownership_mode="shared", binding_digest="m" * 64,
        )
        target = Target(
            project="team", role="developer", environment="development", connection="fake",
            instance_id="FREE", db_name="FREEPDB1", service="freep1", session_user="DEMO",
            current_schema="DEMO", alias="hr", workspace_id=10, app_id=101,
            parsing_schema="HR_DATA", ownership_mode="shared", binding_digest="h" * 64,
        )
        statements: list[str] = []

        def runner(_target, operation, driver, _work, **_kwargs):
            self.assertEqual(operation, "write")
            statements.append(Path(driver).read_text(encoding="utf-8"))
            return SimpleNamespace(stdout="")

        store = SqlControlStore(metadata, runner=runner, work_root=Path(temp.name) / "work")
        store.setup_state([target])

        self.assertEqual(len(statements), 1)
        self.assertIn("CREATE TABLE TEAM_APP_PUBLISH_ACK", statements[0])
        self.assertIn("CREATE TABLE TEAM_APP_PUBLISH_PREP", statements[0])
        self.assertIn("TEAM_APP_PUBLISH_ACK", statements[0])
        self.assertNotIn("ALTER TABLE TEAM_CONTROL_META", statements[0])
        self.assertNotIn("DROP TABLE TEAM_CONTROL_META", statements[0])


class SqlPublishAcknowledgementStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-sql-publish-ack-store-")
        self.addCleanup(self.temp.cleanup)
        self.metadata = Target(
            project="team", role="developer", environment="development", connection="meta",
            instance_id="FREE", db_name="FREEPDB1", service="freep1", session_user="META",
            current_schema="META", alias=None, workspace_id=None, app_id=None,
            parsing_schema=None, ownership_mode="shared", binding_digest="m" * 64,
        )
        self.target = Target(
            project="team", role="developer", environment="development", connection="fake",
            instance_id="FREE", db_name="FREEPDB1", service="freep1", session_user="DEMO",
            current_schema="DEMO", alias="hr", workspace_id=10, app_id=101,
            parsing_schema="HR_DATA", ownership_mode="shared", binding_digest="h" * 64,
        )
        self.preparation_id = "prep-1"
        self.digest = "c" * 64
        self.preparation_fields = [
            self.preparation_id,
            self.digest,
            self.target.physical_key,
            "hr",
            "1",
            "checkout-a",
        ]
        self.preparation_encoded = "|".join(
            base64.b64encode(value.encode("utf-8")).decode("ascii")
            for value in self.preparation_fields
        )
        self.rows: list[str] = []
        fields = [
            self.preparation_id,
            self.digest,
            self.target.physical_key,
            "checkout-a",
            "host-a",
            "alice",
            "2026-09-24T10:15:30.000+00:00",
        ]
        encoded = "|".join(
            base64.b64encode(value.encode("utf-8")).decode("ascii") for value in fields
        )

        def runner(_target, operation, driver, _work, **_kwargs):
            sql = Path(driver).read_text(encoding="utf-8")
            self.rows.append(sql)
            if operation == "read":
                if "TEAM_APP_PUBLISH_PREP" in sql:
                    if f"preparation_id = '{self.preparation_id}'" in sql:
                        return SimpleNamespace(stdout=f"TEAM_PUBLISH_PREP|{self.preparation_encoded}\n")
                    return SimpleNamespace(stdout="")
                return SimpleNamespace(stdout=f"TEAM_PUBLISH_ACK|{encoded}\n")
            return SimpleNamespace(stdout="")

        self.store = SqlControlStore(
            self.metadata, runner=runner, work_root=Path(self.temp.name) / "work"
        )

    def test_record_acknowledgement_checks_registered_identity_and_writes_exact_digest(self):
        row = self.store.record_publish_acknowledgement(
            self.target,
            self.preparation_id,
            self.digest,
            "checkout-a",
            "host-a",
            "alice",
        )

        self.assertEqual(row.preparation_id, self.preparation_id)
        self.assertEqual(row.preparation_digest, self.digest)
        self.assertEqual(row.checkout_uuid, "checkout-a")
        self.assertEqual(row.host, "host-a")
        self.assertEqual(row.acknowledged_by_user, "alice")
        self.assertEqual(row.acknowledged_at, "2026-09-24T10:15:30.000+00:00")
        write = self.rows[0]
        self.assertIn("TEAM_APP_REGISTRY", write)
        self.assertIn("TEAM_APP_PUBLISH_PREP", write)
        self.assertIn("PUBLISH_ACK_CHECKOUT_NOT_REGISTERED", write)
        self.assertIn("PUBLISH_ACK_IDENTITY_MISMATCH", write)
        self.assertIn("MERGE INTO TEAM_APP_PUBLISH_ACK", write)
        self.assertIn(self.preparation_id, write)
        self.assertIn(self.digest, write)

    def test_acknowledgement_query_is_bound_to_preparation_id_and_digest(self):
        rows = self.store.list_publish_acknowledgements(
            self.target, self.preparation_id, self.digest
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].checkout_uuid, "checkout-a")
        self.assertIn(f"preparation_id = '{self.preparation_id}'", self.rows[0])
        self.assertIn(f"preparation_digest = '{self.digest}'", self.rows[0])

    def test_preparation_query_returns_shared_alias_and_roster(self):
        rows = self.store.list_publish_preparation(self.preparation_id)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].preparation_digest, self.digest)
        self.assertEqual(rows[0].alias, "hr")
        self.assertEqual(rows[0].checkout_roster, ("checkout-a",))
        self.assertIn(f"preparation_id = '{self.preparation_id}'", self.rows[0])

    def test_preparation_registration_writes_immutable_roster_snapshot(self):
        self.store.register_publish_preparation(
            "prep-fresh",
            self.digest,
            [(self.target, "hr", ("checkout-a",))],
        )

        write = self.rows[-1]
        self.assertIn("TEAM_APP_PUBLISH_PREP", write)
        self.assertIn("PUBLISH_PREPARATION_ID_EXISTS", write)
        self.assertIn("PUBLISH_PREP_ROSTER_CHANGED", write)
        self.assertIn("prep-fresh", write)
        self.assertIn(self.digest, write)
        self.assertIn("checkout-a", write)


if __name__ == "__main__":
    unittest.main()
