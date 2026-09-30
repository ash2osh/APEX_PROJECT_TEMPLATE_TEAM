import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check_builder_drift.py"


def observed_state(
    last_updated_on: str, database_time: str = "2026-09-26T12:00:00", version: str = "Release 1.0"
) -> str:
    # SQLcl pads the query line; the guard must ignore that padding.
    version_text = "" if last_updated_on == "NOT_FOUND" else version
    return f"{last_updated_on}|{database_time}|{version_text}   \nAPEX_DRIFT_QUERY_VERIFIED\n"


class BuilderDriftTests(unittest.TestCase):
    def run_guard(
        self,
        *,
        baseline: str | None,
        sql_output: str,
        sql_exit: str = "0",
        marker_present: bool = True,
        application_present: bool | None = True,
        version: str | None = "Release 1.0",
        legacy_marker: bool = False,
        state_out: Path | None = None,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app"
            app.mkdir()
            (app / "application.apx").write_text("app SAMPLE ()\n", encoding="utf-8")
            if marker_present:
                marker = {"applicationId": 100, "builderLastUpdatedOn": baseline}
                if not legacy_marker:
                    marker["applicationPresent"] = application_present
                    marker["version"] = version if application_present else None
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
                    *(["--state-out", str(state_out)] if state_out is not None else []),
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

    def test_approved_state_is_recorded_for_the_import_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cases = (
                ("2026-09-26T09:00:00", "2026-09-26T08:00:00", "Release 1.0", "P.2026-09-26T08:00:00." + b"Release 1.0".hex().upper()),
                (None, "NO_TIMESTAMP", "ASHARIF-2026-09-30r001 | ü", "P.NONE." + "ASHARIF-2026-09-30r001 | ü".encode().hex().upper()),
                ("2026-09-26T09:00:00", "2026-09-26T08:00:00", "Release 1.0\u00a0\t", "P.2026-09-26T08:00:00." + b"Release 1.0".hex().upper()),
            )
            for index, (baseline, live, version, expected) in enumerate(cases):
                with self.subTest(live=live):
                    state_out = Path(temporary) / f"state-{index}.txt"
                    result = self.run_guard(
                        baseline=baseline,
                        # A real export records the version as parsed, trailing whitespace dropped.
                        version=version.rstrip(),
                        sql_output=observed_state(live, version=version),
                        state_out=state_out,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(state_out.read_text(encoding="utf-8").strip(), expected)
                    # No character a Windows .cmd/.bat wrapper for SQLcl would interpret.
                    self.assertRegex(expected, r"\A[A-Z0-9.:-]+\Z")

    def test_absent_app_records_absent_state_and_drift_records_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            absent_out = Path(temporary) / "absent.txt"
            result = self.run_guard(
                baseline=None, application_present=False, sql_output=observed_state("NOT_FOUND"), state_out=absent_out,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(absent_out.read_text(encoding="utf-8").strip(), "ABSENT")
            drift_out = Path(temporary) / "drift.txt"
            result = self.run_guard(
                baseline="2026-09-26T08:00:00", sql_output=observed_state("2026-09-26T09:00:00"), state_out=drift_out,
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(drift_out.exists())

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
            application_present=False,
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
        result = self.run_guard(
            baseline=None, application_present=False, sql_output=observed_state("2026-09-26T09:00:00")
        )
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

    def test_marker_without_version_requires_a_fresh_export(self) -> None:
        result = self.run_guard(
            baseline="2026-09-26T08:00:00",
            legacy_marker=True,
            sql_output=observed_state("2026-09-26T08:00:00"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT UNKNOWN]", result.stderr)
        self.assertIn("scripts/team.sh export 100", result.stderr)

    # An import leaves no Builder timestamp; the publish tag in the version is
    # what shows that someone else imported since this export.
    def test_teammate_import_over_import_is_refused_by_version(self) -> None:
        result = self.run_guard(
            baseline=None,
            version="V2 [ASHARIF-2026-09-26r001]",
            sql_output=observed_state("NO_TIMESTAMP", version="V2 [BOB-2026-09-26r001]"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT DETECTED]", result.stdout)
        self.assertIn("V2 [BOB-2026-09-26r001]", result.stdout)
        self.assertIn("V2 [ASHARIF-2026-09-26r001]", result.stdout)

    def test_version_change_with_older_timestamp_is_refused(self) -> None:
        result = self.run_guard(
            baseline="2026-09-26T09:00:00",
            sql_output=observed_state("2026-09-26T08:00:00", version="Release 2.0"),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT DETECTED]", result.stdout)

    def test_version_containing_separator_and_spaces_matches_exactly(self) -> None:
        version = "V2 | Powered By xxx [ASHARIF-2026-09-26r002]"
        result = self.run_guard(
            baseline=None,
            version=version,
            sql_output=observed_state("NO_TIMESTAMP", version=version),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
