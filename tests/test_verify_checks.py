import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fake_sqlcl import environment as fake_environment, install
from scripts import verify_checks


ROOT = Path(__file__).resolve().parents[1]


class VerifyCommandHelpTests(unittest.TestCase):
    def test_team_help_lists_read_only_verify(self):
        result = subprocess.run(
            ["bash", str(ROOT / "scripts" / "team.sh"), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("verify <folder>", result.stdout)
        self.assertIn("--phase pre|post|both", result.stdout)

    def test_verify_help_is_available_without_loading_database_configuration(self):
        result = subprocess.run(
            ["bash", str(ROOT / "scripts" / "team.sh"), "verify", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("--only-failed", result.stdout)
        self.assertIn("--jobs JOBS", result.stdout)


class VerifyChecksIntegrationTests(unittest.TestCase):
    FAKE_SQLCL = r"""#!/usr/bin/env bash
set -eu
driver=""
for argument in "$@"; do
  case "$argument" in @*) driver="$(printf '%s\n' "$argument" | sed 's/^@//')" ;; esac
done
[ -n "$driver" ] || exit 91
phase="$(sed -n "s/.*l_payload.put('phase', '\([^']*\)').*/\1/p" "$driver" | head -n 1)"
[ -n "$phase" ] || exit 92
results=""
while IFS= read -r line; do
  check_id="$(printf '%s\n' "$line" | sed -n "s/.*l_result.put('id', '\([^']*\)').*/\1/p")"
  [ -n "$check_id" ] || continue
  case ",$FAKE_ERROR_IDS," in
    *,"$check_id",*)
      result="{\"id\":\"$check_id\",\"row_count\":0,\"column_count\":1,\"numeric\":false,\"error\":\"ORA-00942: table or view does not exist\"}"
      ;;
    *)
      value=1
      case ",$FAKE_FALSE_IDS," in *,"$check_id",*) value=0 ;; esac
      result="{\"id\":\"$check_id\",\"row_count\":1,\"column_count\":1,\"numeric\":true,\"value\":$value}"
      ;;
  esac
  if [ -n "$results" ]; then results="$results,$result"; else results="$result"; fi
done < <(grep -E "l_result.put\('id', '[a-z0-9-]+'\)" "$driver")
identity="{\"session_user\":\"$FAKE_SESSION_USER\",\"current_schema\":\"$FAKE_CURRENT_SCHEMA\",\"db_name\":\"$FAKE_DB_NAME\",\"db_unique_name\":\"$FAKE_DB_UNIQUE_NAME\",\"service_name\":\"$FAKE_SERVICE_NAME\",\"container_id\":\"3\",\"container_name\":\"APP_PDB\",\"edition\":\"ORA\$BASE\",\"database_version\":\"19.0\"}"
printf 'CHECK_PAYLOAD_BEGIN:%s\n' "$phase"
printf '{"schemaVersion":1,"phase":"%s","complete":true,"identity":%s,"results":[%s]}\n' "$phase" "$identity" "$results"
printf 'CHECK_PAYLOAD_END:%s\nCHECK_VERIFIED:%s\n' "$phase" "$phase"
"""

    IDENTITY = {
        "session_user": "APP", "current_schema": "APP", "db_name": "DEVDB",
        "db_unique_name": "DEVDB_UNIQUE", "service_name": "dev.service",
        "container_id": "3", "container_name": "APP_PDB",
        "edition": "ORA$BASE", "database_version": "19.0",
    }
    CONFIG = {
        "DB_ENVIRONMENT": "development", "CODE_SCHEMA": "APP",
        "CODE_SQLCL_CONNECTION": "dev-alias", "CODE_EXPECTED_USER": "APP",
    }

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fake_bin = install(self.root / "fake-bin", self.FAKE_SQLCL)
        self.folder = self.add_folder(
            "2026-10-09_demo-r001",
            preconditions=[{"id": "ready", "sql": "SELECT 1 FROM dual", "expected": 1}],
            postconditions=[{"id": "verified", "sql": "SELECT 1 FROM dual", "expected": 1}],
        )

    def tearDown(self):
        self.temporary.cleanup()

    def add_folder(self, name, *, schema=None, preconditions=(), postconditions=()):
        parent = self.root / "migrations"
        if schema:
            parent /= schema
        folder = parent / name
        folder.mkdir(parents=True)
        (folder / "001-change.sql").write_text(
            "CREATE TABLE VERIFY_SAMPLE (ID NUMBER);\n", encoding="utf-8"
        )
        if not postconditions:
            postconditions = [{"id": "verified", "sql": "SELECT 1 FROM dual", "expected": 1}]
        (folder / "checks.json").write_text(json.dumps({
            "schemaVersion": 1,
            "preconditions": list(preconditions),
            "postconditions": list(postconditions),
        }) + "\n", encoding="utf-8")
        return folder

    def run_verify(self, arguments, *, config=None, fake_values=None):
        defaults = {
            "FAKE_SESSION_USER": "APP",
            "FAKE_CURRENT_SCHEMA": "APP",
            "FAKE_DB_NAME": "DEVDB",
            "FAKE_DB_UNIQUE_NAME": "DEVDB_UNIQUE",
            "FAKE_SERVICE_NAME": "dev.service",
            "FAKE_FALSE_IDS": "",
            "FAKE_ERROR_IDS": "",
        }
        defaults.update(fake_values or {})
        values = dict(self.CONFIG if config is None else config)
        output, errors = io.StringIO(), io.StringIO()
        self.observed_targets = []
        identity = getattr(self, "inventory_identity", self.IDENTITY)

        def capture_target(target, _run_dir):
            self.observed_targets.append(target)
            return SimpleNamespace(identity=identity)

        with patch.dict(os.environ, fake_environment(self.fake_bin, **defaults)):
            with patch(
                "scripts.verify_checks.capture_inventory",
                side_effect=capture_target,
            ):
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    status = verify_checks.main(arguments, environ=values, repo_root=self.root)
        return status, output.getvalue(), errors.getvalue(), identity

    def test_all_requested_pre_and_post_checks_true(self):
        status, output, errors, _identity = self.run_verify([
            "migrations/2026-10-09_demo-r001", "--env", "dev",
            "--phase", "both", "--format", "json",
        ])
        report = json.loads(output)
        self.assertEqual(status, 0, errors)
        self.assertEqual([item["status"] for item in report["checks"]], ["true", "true"])
        self.assertEqual(report["summary"]["passed"], 2)
        self.assertEqual(list((self.root / "scratch").iterdir()), [])

    def test_some_false_checks_return_one(self):
        status, output, errors, _identity = self.run_verify([
            "migrations/2026-10-09_demo-r001", "--env", "dev",
            "--phase", "pre", "--format", "json",
        ], fake_values={"FAKE_FALSE_IDS": "ready"})
        report = json.loads(output)
        self.assertEqual(status, 1, errors)
        self.assertEqual(report["checks"][0]["status"], "false")
        self.assertTrue(Path(report["evidenceDir"]).is_dir())

    def test_one_check_error_returns_two(self):
        status, output, errors, _identity = self.run_verify([
            "migrations/2026-10-09_demo-r001", "--env", "dev",
            "--phase", "pre", "--format", "json",
        ], fake_values={"FAKE_ERROR_IDS": "ready"})
        report = json.loads(output)
        self.assertEqual(status, 2, errors)
        self.assertEqual(report["checks"][0]["status"], "error")
        self.assertIn("ORA-00942", report["checks"][0]["error"])

    def test_json_output_has_a_versioned_schema(self):
        status, output, errors, _identity = self.run_verify([
            "migrations/2026-10-09_demo-r001", "--env", "dev",
            "--phase", "pre", "--format", "json",
        ])
        report = json.loads(output)
        self.assertEqual(status, 0, errors)
        self.assertEqual(report["schemaVersion"], 1)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["target"], {
            "environment": "dev", "schema": "APP", "expectedUser": "APP",
        })
        self.assertEqual(
            set(report["checks"][0]),
            {"folder", "phase", "id", "expected", "observed", "status", "error"},
        )

    def test_only_failed_omits_true_rows_but_keeps_full_summary(self):
        self.add_folder(
            "2026-10-09_mixed-r001",
            preconditions=[
                {"id": "ready", "sql": "SELECT 1 FROM dual", "expected": 1},
                {"id": "missing", "sql": "SELECT 1 FROM dual", "expected": 1},
            ],
        )
        status, output, errors, _identity = self.run_verify([
            "migrations/2026-10-09_mixed-r001", "--env", "dev",
            "--phase", "pre", "--only-failed",
        ], fake_values={"FAKE_FALSE_IDS": "missing"})
        self.assertEqual(status, 1, errors)
        self.assertIn("missing", output)
        self.assertNotIn("ready", output)
        self.assertIn("2 checks", output)

    def test_three_hundred_checks_use_several_sessions(self):
        checks = [
            {"id": f"check-{index:03}", "sql": "SELECT 1 FROM dual", "expected": 1}
            for index in range(300)
        ]
        self.add_folder("2026-10-09_bulk-r001", preconditions=checks)
        status, output, errors, _identity = self.run_verify([
            "migrations/2026-10-09_bulk-r001", "--env", "dev",
            "--phase", "pre", "--format", "json", "--jobs", "2",
        ], fake_values={"MIGRATION_CHECK_BATCH_BYTES": "50000"})
        report = json.loads(output)
        self.assertEqual(status, 0, errors)
        self.assertEqual(report["summary"]["total"], 300)
        self.assertGreater(report["summary"]["sessions"], 1)
        self.assertEqual(report["summary"]["passed"], 300)

    def test_schema_selector_resolves_the_matching_staging_migration_target(self):
        self.add_folder(
            "2026-10-09_hr-demo-r001",
            schema="HR",
            preconditions=[{"id": "ready", "sql": "SELECT 1 FROM dual", "expected": 1}],
        )
        self.inventory_identity = {
            **self.IDENTITY,
            "session_user": "HR",
            "current_schema": "HR",
            "db_name": "STAGEDB",
            "db_unique_name": "STAGEDB_UNIQUE",
            "service_name": "stage.service",
        }
        config = {
            **self.CONFIG,
            "CODE_SCHEMA": "APP,HR",
            "CODE_SQLCL_CONNECTION": "dev-app,dev-hr",
            "CODE_EXPECTED_USER": "APP,HR",
            "STAGING_SCHEMA": "APP,HR",
            "STAGING_SQLCL_CONNECTION": "stage-app,stage-hr",
            "STAGING_EXPECTED_USER": "APP,HR",
            "STAGING_MIGRATION_SCHEMA": "APP,HR",
            "STAGING_MIGRATION_SQLCL_CONNECTION": "stage-migration-app,stage-migration-hr",
            "STAGING_MIGRATION_EXPECTED_USER": "APP,HR",
        }
        status, output, errors, _identity = self.run_verify([
            "migrations/HR/2026-10-09_hr-demo-r001", "--env", "staging",
            "--schema", "HR", "--phase", "pre", "--format", "json",
        ], config=config, fake_values={
            "FAKE_SESSION_USER": "HR",
            "FAKE_CURRENT_SCHEMA": "HR",
            "FAKE_DB_NAME": "STAGEDB",
            "FAKE_DB_UNIQUE_NAME": "STAGEDB_UNIQUE",
            "FAKE_SERVICE_NAME": "stage.service",
        })

        self.assertEqual(status, 0, errors + output)
        self.assertEqual(self.observed_targets[0].connection, "stage-migration-hr")
        self.assertEqual(self.observed_targets[0].schema, "HR")
        self.assertEqual(self.observed_targets[0].expected_user, "HR")

    def test_identity_mismatch_refuses_check_results(self):
        status, output, errors, _identity = self.run_verify([
            "migrations/2026-10-09_demo-r001", "--env", "dev",
            "--phase", "pre", "--format", "json",
        ], fake_values={"FAKE_DB_NAME": "OTHERDB"})
        report = json.loads(output)
        self.assertEqual(status, 2, errors)
        self.assertEqual(report["status"], "error")
        self.assertTrue(any(item["code"] == "IDENTITY_MISMATCH" for item in report["errors"]))
        self.assertTrue(Path(report["evidenceDir"]).is_dir())

    def test_missing_connection_is_reported_without_opening_sqlcl(self):
        output, errors = io.StringIO(), io.StringIO()
        config = {key: value for key, value in self.CONFIG.items() if key != "CODE_SQLCL_CONNECTION"}
        with patch(
            "scripts.verify_checks.capture_inventory",
            side_effect=AssertionError("target resolution must fail before connecting"),
        ):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                status = verify_checks.main(
                    ["migrations/2026-10-09_demo-r001", "--env", "dev"],
                    environ=config,
                    repo_root=self.root,
                )
        self.assertEqual(status, 2)
        self.assertIn("CODE_SQLCL_CONNECTION", output.getvalue())
        self.assertFalse((self.root / "scratch").exists())


if __name__ == "__main__":
    unittest.main()
