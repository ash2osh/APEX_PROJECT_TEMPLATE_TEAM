from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.qualify_synonym_namespaces import (
    QualificationError,
    _preflight_view,
    guarded_fixture_write,
    guarded_owned_cleanup,
    report_interruption,
)
from scripts.schema_catalog import SchemaSnapshot, SynonymDefinition, ObjectGrant


class QualifySynonymNamespacesTests(unittest.TestCase):
    def test_wrong_target_creates_no_fixture(self) -> None:
        writes: list[str] = []

        with self.assertRaisesRegex(QualificationError, "identity"):
            guarded_fixture_write(
                {"session_user": "DEMO", "current_schema": "DEMO", "server_host": "wrong", "service_name": "freepdb1"},
                {"session_user": "DEMO2", "current_schema": "DEMO2", "server_host": "b72393fa20fa", "service_name": "freepdb1"},
                {"owner": False, "synonym": False},
                lambda: writes.append("write"),
            )

        self.assertEqual(writes, [])

    def test_preexisting_name_refuses_before_fixture_write(self) -> None:
        writes: list[str] = []
        identity = {"session_user": "DEMO2", "current_schema": "DEMO2", "server_host": "b72393fa20fa", "service_name": "freepdb1"}

        with self.assertRaisesRegex(QualificationError, "already exists"):
            guarded_fixture_write(
                identity,
                identity,
                {"owner": False, "synonym": True},
                lambda: writes.append("write"),
            )

        self.assertEqual(writes, [])

    def test_cleanup_requires_exact_owned_inventory(self) -> None:
        drops: list[str] = []

        with self.assertRaisesRegex(QualificationError, "unexpected"):
            guarded_owned_cleanup(
                expected_owner="QSN_FIXTURE",
                observed_owner="QSN_FIXTURE",
                expected_objects={("QSN_FIXTURE", "TABLE", "QSN_TABLE")},
                observed_objects={
                    ("QSN_FIXTURE", "TABLE", "QSN_TABLE"),
                    ("QSN_FIXTURE", "TABLE", "UNRELATED_TABLE"),
                },
                cleanup=lambda: drops.append("drop"),
            )

        self.assertEqual(drops, [])

    def test_cleanup_after_interruption_before_owner_creation_can_finish_without_drop(self) -> None:
        calls: list[str] = []

        result = guarded_owned_cleanup(
            expected_owner="QSN_FIXTURE",
            observed_owner=None,
            expected_objects={("QSN_FIXTURE", "TABLE", "QSN_TABLE")},
            observed_objects=set(),
            cleanup=lambda: calls.append("cleanup") or "clean",
        )

        self.assertEqual(result, "clean")
        self.assertEqual(calls, ["cleanup"])

    def test_cleanup_refuses_objects_without_the_recorded_owner(self) -> None:
        drops: list[str] = []

        with self.assertRaisesRegex(QualificationError, "without their recorded owner"):
            guarded_owned_cleanup(
                expected_owner="QSN_FIXTURE",
                observed_owner=None,
                expected_objects={("QSN_FIXTURE", "TABLE", "QSN_TABLE")},
                observed_objects={("QSN_FIXTURE", "TABLE", "QSN_TABLE")},
                cleanup=lambda: drops.append("drop"),
            )

        self.assertEqual(drops, [])

    def test_interruption_reports_cleanup_pending(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "qualification.json"
            report_interruption(path, {
                "schemaVersion": 1,
                "status": "unknown",
                "cleanupRequired": True,
                "fixtureCleanup": "pending",
                "fixtureOwnership": {"attempted": True, "owner": "QSN_FIXTURE"},
            })

            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(saved["status"], "unknown")
        self.assertTrue(saved["cleanupRequired"])
        self.assertEqual(saved["fixtureCleanup"], "pending")

    def test_view_preflight_accepts_directly_granted_table_synonym(self) -> None:
        snapshot = SchemaSnapshot(
            {"session_user": "APP", "current_schema": "APP"},
            {},
            {},
            {"ownerComplete": True, "catalogs": ["ALL_OBJECTS"]},
            "2026-10-12T00:00:00Z",
            "2026-10-12T00:00:01Z",
            (SynonymDefinition("APP", "SYN_T", "DATA_OWNER", "T", target_exists=True, target_type="TABLE"),),
            (ObjectGrant("DATA_OWNER", "T", "APP", "SELECT"),),
        )

        with tempfile.TemporaryDirectory() as temporary:
            result = _preflight_view(snapshot, "APP", "SYN_T", "qsn-unit-table", Path(temporary))

        self.assertEqual(result["exit_code"], 0, result)

    def test_view_preflight_refuses_select_granted_sequence_synonym(self) -> None:
        snapshot = SchemaSnapshot(
            {"session_user": "APP", "current_schema": "APP"},
            {},
            {},
            {"ownerComplete": True, "catalogs": ["ALL_OBJECTS"]},
            "2026-10-12T00:00:00Z",
            "2026-10-12T00:00:01Z",
            (SynonymDefinition("APP", "SYN_S", "DATA_OWNER", "S", target_exists=True, target_type="SEQUENCE"),),
            (ObjectGrant("DATA_OWNER", "S", "APP", "SELECT"),),
        )

        with tempfile.TemporaryDirectory() as temporary:
            result = _preflight_view(snapshot, "APP", "SYN_S", "qsn-unit-sequence", Path(temporary))

        self.assertNotEqual(result["exit_code"], 0, result)
        self.assertIn("MISSING_PREREQUISITE", {item.get("code") for item in result["conflicts"]})


if __name__ == "__main__":
    unittest.main()
