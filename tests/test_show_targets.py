import contextlib
import io
import json
import subprocess
import unittest
from unittest.mock import patch

import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
from scripts import show_targets

SINGLE_OWNER = {
    "DB_ENVIRONMENT": "development",
    "CODE_SQLCL_CONNECTION": "dev_code", "CODE_EXPECTED_USER": "APP", "CODE_SCHEMA": "APP",
    "TABLES_SQLCL_CONNECTION": "dev_code", "TABLES_EXPECTED_USER": "APP", "TABLES_SCHEMA": "APP",
    "APEX_SQLCL_CONNECTION": "dev_code", "APEX_EXPECTED_USER": "APP", "APEX_PARSING_SCHEMA": "APP",
}
SPLIT_OWNER = {
    **SINGLE_OWNER,
    "CODE_SCHEMA": "APPCODE", "CODE_EXPECTED_USER": "APPCODE",
    "MIGRATION_SCHEMA": "APPDATA", "MIGRATION_SQLCL_CONNECTION": "dev_mig", "MIGRATION_EXPECTED_USER": "APPDATA",
}
ROW_KEYS = ["environment", "purpose", "schema", "connection", "expectedUser", "actsAsSchema", "classification", "status", "message"]


def _rows(report, **match):
    return [row for row in report["rows"] if all(row[key] == value for key, value in match.items())]


class ShowTargetsTests(unittest.TestCase):
    def test_a_single_owner_project_resolves_every_dev_row_and_has_no_notes(self):
        report = show_targets.build_report(SINGLE_OWNER)

        self.assertEqual(1, report["schemaVersion"])
        for purpose in ("read", "migration"):
            with self.subTest(purpose=purpose):
                rows = _rows(report, environment="dev", purpose=purpose)
                self.assertEqual(["resolved"], [row["status"] for row in rows])
                self.assertEqual(("APP", "dev_code", "APP", False, "development"),
                                 (rows[0]["schema"], rows[0]["connection"], rows[0]["expectedUser"],
                                  rows[0]["actsAsSchema"], rows[0]["classification"]))
        self.assertEqual(["listed"], [row["status"] for row in _rows(report, purpose="tables")])
        self.assertEqual(["not configured"], [row["status"] for row in _rows(report, purpose="ords")])
        self.assertEqual([], report["notes"])

    def test_a_split_owner_project_shows_the_refused_code_schema_for_migrate_and_the_note(self):
        report = show_targets.build_report(SPLIT_OWNER)

        refused = _rows(report, environment="dev", purpose="migration", schema="APPCODE")
        self.assertEqual(["REFUSED"], [row["status"] for row in refused])
        self.assertIn("schema APPCODE is not listed in MIGRATION_SCHEMA for dev", refused[0]["message"])
        self.assertEqual(["resolved"], [row["status"] for row in _rows(report, environment="dev", purpose="migration", schema="APPDATA")])
        self.assertEqual(["REFUSED"], [row["status"] for row in _rows(report, environment="dev", purpose="read", schema="APPDATA")])

        self.assertEqual(2, len(report["notes"]))
        self.assertIn("APPCODE is in CODE_SCHEMA but not in the dev migration profile", report["notes"][0])
        self.assertIn("APPDATA is in the dev migration profile", report["notes"][1])
        for note in report["notes"]:
            self.assertIn("`new-migration --source` reads the code profile while `migrate` uses the migration profile.", note)

        text = show_targets.render_text(report)
        self.assertIn("REFUSED: schema APPCODE is not listed in MIGRATION_SCHEMA for dev", text)
        self.assertIn("Notes:\n  - APPCODE is in CODE_SCHEMA", text)

    def test_an_unconfigured_staging_and_prod_print_one_not_configured_row_per_purpose(self):
        report = show_targets.build_report(SINGLE_OWNER)

        for environment in ("staging", "prod"):
            for purpose in ("read", "migration"):
                with self.subTest(environment=environment, purpose=purpose):
                    rows = _rows(report, environment=environment, purpose=purpose)
                    self.assertEqual(1, len(rows))
                    self.assertEqual("not configured", rows[0]["status"])
                    self.assertIn(f"{environment.upper()}_", rows[0]["message"])
                    self.assertIsNone(rows[0]["schema"])

    def test_a_configured_staging_profile_resolves_and_is_classified_as_staging(self):
        staging = {
            **SINGLE_OWNER,
            "STAGING_SQLCL_CONNECTION": "stg_conn", "STAGING_EXPECTED_USER": "APPSTG", "STAGING_SCHEMA": "APPSTG",
        }
        report = show_targets.build_report(staging)

        rows = _rows(report, environment="staging", purpose="read")
        self.assertEqual(
            [("APPSTG", "stg_conn", "APPSTG", False, "staging", "resolved")],
            [(r["schema"], r["connection"], r["expectedUser"], r["actsAsSchema"], r["classification"], r["status"]) for r in rows],
        )

    def test_a_partial_migration_profile_is_refused_for_dev(self):
        partial = {**SINGLE_OWNER, "MIGRATION_SCHEMA": "APP"}
        report = show_targets.build_report(partial)

        rows = _rows(report, environment="dev", purpose="migration")
        self.assertEqual(["REFUSED"], [row["status"] for row in rows])
        self.assertIn("must be configured together", rows[0]["message"])

    def test_misaligned_tables_profile_is_refused_not_guessed(self):
        misaligned = {**SINGLE_OWNER, "TABLES_SCHEMA": "A,B", "TABLES_SQLCL_CONNECTION": "c1", "TABLES_EXPECTED_USER": "A,B"}
        rows = _rows(show_targets.build_report(misaligned), purpose="tables")

        self.assertEqual(["REFUSED"], [row["status"] for row in rows])
        self.assertIn("must list the same number of entries", rows[0]["message"])

    def test_the_json_report_round_trips_with_stable_key_order(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = show_targets.main(["--format", "json"], environ=SPLIT_OWNER)

        self.assertEqual(0, code)
        text = output.getvalue()
        data = json.loads(text)
        self.assertEqual(["schemaVersion", "rows", "notes"], list(data))
        self.assertEqual(1, data["schemaVersion"])
        self.assertTrue(all(list(row) == ROW_KEYS for row in data["rows"]))
        self.assertEqual(show_targets.build_report(SPLIT_OWNER), data)
        self.assertEqual(json.dumps(data, indent=2, ensure_ascii=False) + "\n", text)

    def test_the_text_report_exits_zero_even_with_refused_rows(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = show_targets.main([], environ=SPLIT_OWNER)

        self.assertEqual(0, code)
        self.assertIn("Effective database targets", output.getvalue())

    def test_a_usage_error_exits_two(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exit_status:
                show_targets.main(["--format", "xml"], environ=SINGLE_OWNER)
        self.assertEqual(2, exit_status.exception.code)

    def test_help_and_usage_name_the_prog_the_caller_passes(self):
        help_output, usage_output = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(help_output), self.assertRaises(SystemExit) as help_exit:
            show_targets.main(["--help"], environ=SINGLE_OWNER, prog="scripts/team.ps1 targets")
        self.assertEqual(0, help_exit.exception.code)
        self.assertIn("scripts/team.ps1 targets", help_output.getvalue())
        self.assertNotIn("scripts/team.sh targets", help_output.getvalue())

        with contextlib.redirect_stderr(usage_output), self.assertRaises(SystemExit) as usage_exit:
            show_targets.main(["--format", "xml"], environ=SINGLE_OWNER, prog="scripts/team.ps1 targets")
        self.assertEqual(2, usage_exit.exception.code)
        self.assertIn("usage: scripts/team.ps1 targets", usage_output.getvalue())
        self.assertNotIn("scripts/team.sh targets", usage_output.getvalue())

    def test_no_database_client_or_subprocess_is_started(self):
        def refuse(*args, **kwargs):
            raise AssertionError("show_targets must not start a process")

        with patch.object(subprocess, "run", side_effect=refuse) as run, \
                patch.object(subprocess, "Popen", side_effect=refuse) as popen, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, show_targets.main([], environ=SPLIT_OWNER))
            self.assertEqual(0, show_targets.main(["--format", "json"], environ=SPLIT_OWNER))
        self.assertFalse(run.called)
        self.assertFalse(popen.called)


if __name__ == "__main__":
    unittest.main()
