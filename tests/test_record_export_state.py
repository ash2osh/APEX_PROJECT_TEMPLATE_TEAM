import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RECORDER = ROOT / "scripts" / "record_export_state.py"


class RecordExportStateTests(unittest.TestCase):
    def run_recorder(
        self,
        before: str,
        after: str,
        timezone: str = "UTC",
        before_version: str = "Release 1.0",
        after_version: str = "Release 1.0",
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before_path = root / "before.txt"
            after_path = root / "after.txt"
            marker_path = root / "app" / "apex-team-export.json"
            marker_path.parent.mkdir()
            before_text = "" if before == "NOT_FOUND" else before_version
            after_text = "" if after == "NOT_FOUND" else after_version
            before_path.write_text(f"{before}|2026-09-26T09:00:00|{before_text}\n", encoding="utf-8")
            after_path.write_text(f"{after}|2026-09-26T09:00:02|{after_text}\n", encoding="utf-8")
            environment = os.environ.copy()
            environment["TZ"] = timezone
            result = subprocess.run(
                [
                    "python3",
                    str(RECORDER),
                    "100",
                    str(before_path),
                    str(after_path),
                    str(marker_path),
                ],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            marker = json.loads(marker_path.read_text(encoding="utf-8")) if marker_path.exists() else None
            return result, marker

    def test_marker_uses_database_baseline_not_workstation_clock(self) -> None:
        result, marker = self.run_recorder(
            "2026-09-26T08:00:00", "2026-09-26T08:00:00", "Pacific/Kiritimati"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            marker,
            {
                "applicationId": 100,
                "applicationPresent": True,
                "builderLastUpdatedOn": "2026-09-26T08:00:00",
                "version": "Release 1.0",
            },
        )

    def test_export_fails_if_builder_changes_during_export(self) -> None:
        result, marker = self.run_recorder("2026-09-26T08:00:00", "2026-09-26T08:00:01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("while export was running", result.stderr)
        self.assertIsNone(marker)

    def test_export_fails_when_builder_timestamp_is_in_the_same_database_second(self) -> None:
        result, marker = self.run_recorder("2026-09-26T09:00:00", "2026-09-26T09:00:00")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("same database second", result.stderr)
        self.assertIsNone(marker)

    def test_not_installed_baseline_is_recorded_explicitly(self) -> None:
        result, marker = self.run_recorder("NOT_FOUND", "NOT_FOUND")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker["builderLastUpdatedOn"], None)
        self.assertIs(marker["applicationPresent"], False)
        self.assertIsNone(marker["version"])

    def test_import_during_export_fails_closed(self) -> None:
        result, marker = self.run_recorder(
            "NO_TIMESTAMP", "NO_TIMESTAMP", after_version="V2 [BOB-2026-09-26r001]"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed", result.stderr)
        self.assertIsNone(marker)

    def test_state_without_version_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "before.txt").write_text("NO_TIMESTAMP|2026-09-26T09:00:00\n", encoding="utf-8")
            (root / "after.txt").write_text("NO_TIMESTAMP|2026-09-26T09:00:02\n", encoding="utf-8")
            result = subprocess.run(
                ["python3", str(RECORDER), "100", str(root / "before.txt"), str(root / "after.txt"), str(root / "m.json")],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "m.json").exists())

    def test_imported_app_without_builder_timestamp_is_recorded_as_present(self) -> None:
        result, marker = self.run_recorder("NO_TIMESTAMP", "NO_TIMESTAMP")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            marker,
            {
                "applicationId": 100,
                "applicationPresent": True,
                "builderLastUpdatedOn": None,
                "version": "Release 1.0",
            },
        )

    def test_builder_edit_during_export_of_imported_app_fails_closed(self) -> None:
        result, marker = self.run_recorder("NO_TIMESTAMP", "2026-09-26T09:00:01")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("while export was running", result.stderr)
        self.assertIsNone(marker)

    def test_invalid_or_mismatched_export_state_fails_closed(self) -> None:
        result, marker = self.run_recorder("NOT_FOUND", "2026-09-26T08:00:00")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("while export was running", result.stderr)
        self.assertIsNone(marker)


if __name__ == "__main__":
    unittest.main()
