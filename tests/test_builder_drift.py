import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check_builder_drift.py"


def observed_state(last_updated_on: str, database_time: str = "2026-09-26T12:00:00") -> str:
    return f"{last_updated_on}|{database_time}"


class BuilderDriftTests(unittest.TestCase):
    def run_guard(
        self,
        *,
        baseline: str | None,
        sql_output: str,
        sql_exit: str = "0",
        marker_present: bool = True,
        application_present: bool | None = None,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app"
            app.mkdir()
            (app / "application.apx").write_text("app SAMPLE ()\n", encoding="utf-8")
            if marker_present:
                marker = {"applicationId": 100, "builderLastUpdatedOn": baseline}
                if application_present is not None:
                    marker["applicationPresent"] = application_present
                (app / "apex-team-export.json").write_text(json.dumps(marker), encoding="utf-8")

            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_sql = fake_bin / "sql"
            fake_sql.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$FAKE_SQL_OUTPUT\"\n"
                "exit \"$FAKE_SQL_EXIT\"\n",
                encoding="utf-8",
            )
            fake_sql.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SQL_OUTPUT"] = sql_output
            environment["FAKE_SQL_EXIT"] = sql_exit
            environment["TZ"] = "Pacific/Kiritimati"
            return subprocess.run(
                [
                    "python3",
                    str(GUARD),
                    "100",
                    "docker-demo",
                    str(app),
                    "--expected-user",
                    "DEMO",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_live_builder_update_after_export_is_refused(self) -> None:
        result = self.run_guard(
            baseline="2026-09-26T08:00:00",
            sql_output=observed_state("2026-09-26T09:00:00"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT DETECTED] Live APEX App 100", result.stdout)
        self.assertIn("2026-09-26T09:00:00", result.stdout)
        self.assertIn("scripts/team.sh export 100", result.stdout)

    def test_live_state_equal_to_or_older_than_export_is_clean(self) -> None:
        result = self.run_guard(
            baseline="2026-09-26T09:00:00",
            sql_output=observed_state("2026-09-26T08:00:00"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[DRIFT OK] No uncaptured Builder edits detected.", result.stdout)

    def test_missing_database_export_baseline_fails_closed(self) -> None:
        result = self.run_guard(
            baseline=None, sql_output=observed_state("2026-09-26T09:00:00"), marker_present=False
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT UNKNOWN]", result.stderr)
        self.assertIn("scripts/team.sh export 100", result.stderr)

    def test_unavailable_sqlcl_query_fails_closed(self) -> None:
        result = self.run_guard(
            baseline="2026-09-26T08:00:00",
            sql_output="connection failed",
            sql_exit="1",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT UNKNOWN]", result.stderr)

    def test_application_not_yet_installed_is_not_builder_drift(self) -> None:
        result = self.run_guard(
            baseline=None,
            sql_output=observed_state("NOT_FOUND"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[DRIFT OK]", result.stdout)

    def test_builder_update_is_detected_independent_of_workstation_timezone(self) -> None:
        result = self.run_guard(
            baseline="2026-09-26T08:00:00",
            sql_output=observed_state("2026-09-26T09:00:00"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT DETECTED]", result.stdout)

    def test_application_created_after_export_is_refused(self) -> None:
        result = self.run_guard(baseline=None, sql_output=observed_state("2026-09-26T09:00:00"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT DETECTED]", result.stdout)
        self.assertIn("created after the local export", result.stdout)

    def test_application_removed_after_export_is_refused(self) -> None:
        result = self.run_guard(
            baseline="2026-09-26T08:00:00",
            sql_output=observed_state("NOT_FOUND"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT DETECTED]", result.stdout)
        self.assertIn("no longer exists", result.stdout)

    def test_ambiguous_same_second_timestamp_fails_closed(self) -> None:
        timestamp = "2026-09-26T09:00:00"
        result = self.run_guard(
            baseline=timestamp,
            sql_output=observed_state(timestamp, timestamp),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("current database second", result.stderr)

    # APEX does not stamp last_updated_on during an import, so an app installed
    # from APEXlang exists with a NULL revision. That is not an absent app.
    def test_imported_app_without_builder_timestamp_is_clean(self) -> None:
        result = self.run_guard(
            baseline=None,
            application_present=True,
            sql_output=observed_state("NO_TIMESTAMP"),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[DRIFT OK]", result.stdout)
        self.assertIn("no Builder edits since its last import", result.stdout)
        self.assertNotIn("absent", result.stdout)

    def test_builder_edit_after_import_baseline_is_refused(self) -> None:
        result = self.run_guard(
            baseline=None,
            application_present=True,
            sql_output=observed_state("2026-09-26T09:00:00"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT DETECTED]", result.stdout)
        self.assertIn("modified in Builder on 2026-09-26T09:00:00", result.stdout)

    def test_reimport_after_builder_baseline_is_refused(self) -> None:
        result = self.run_guard(
            baseline="2026-09-26T08:00:00",
            application_present=True,
            sql_output=observed_state("NO_TIMESTAMP"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT DETECTED]", result.stdout)
        self.assertIn("re-imported", result.stdout)

    def test_imported_app_is_not_treated_as_absent_by_legacy_marker(self) -> None:
        result = self.run_guard(baseline=None, sql_output=observed_state("NO_TIMESTAMP"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT DETECTED]", result.stdout)
        self.assertIn("created after the local export", result.stdout)

    def test_import_baseline_for_removed_app_is_refused(self) -> None:
        result = self.run_guard(
            baseline=None,
            application_present=True,
            sql_output=observed_state("NOT_FOUND"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("no longer exists", result.stdout)

    def test_inconsistent_marker_fails_closed(self) -> None:
        result = self.run_guard(
            baseline="2026-09-26T08:00:00",
            application_present=False,
            sql_output=observed_state("2026-09-26T08:00:00"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT UNKNOWN]", result.stderr)


if __name__ == "__main__":
    unittest.main()
