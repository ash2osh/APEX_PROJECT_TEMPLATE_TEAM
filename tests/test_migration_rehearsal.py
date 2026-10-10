from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fake_sqlcl import environment as fake_environment, install
from scripts.db_targets import Target
from scripts.migrate import main as migrate_main, rehearse_batch
from scripts.migration_checks import CheckReport, render_in_session_check_driver
from scripts.migration_manifest import MigrationManifestError, QueryCheck, load_batch
from scripts.sqlcl_session import run_sqlcl


class MigrationRehearsalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()
        self.target = Target("dev", "fake-profile", "APP", "APP", "development")
        self.identity = {
            "session_user": "APP",
            "current_schema": "APP",
            "db_name": "DEVDB",
            "db_unique_name": "DEVDB_UNIQUE",
            "service_name": "dev.service",
            "container_id": "3",
            "container_name": "APP_PDB",
            "edition": "ORA$BASE",
            "database_version": "19.0",
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_folder(self, name: str, files: dict[str, str], *, pre_id: str, post_id: str) -> Path:
        folder = self.root / "migrations" / name
        folder.mkdir()
        for filename, content in files.items():
            (folder / filename).write_text(content, encoding="utf-8", newline="\n")
        checks = {
            "schemaVersion": 1,
            "preconditions": [{"id": pre_id, "sql": "SELECT 1 FROM dual", "expected": 1}],
            "postconditions": [{"id": post_id, "sql": "SELECT 1 FROM dual", "expected": 1}],
        }
        (folder / "checks.json").write_text(json.dumps(checks) + "\n", encoding="utf-8")
        return folder

    def load(self, *folders):
        return load_batch(self.root, [f"migrations/{folder}" for folder in folders])

    def frame(self, phase: str, check_id: str, value: int = 1) -> str:
        payload = {
            "schemaVersion": 1,
            "phase": phase,
            "complete": True,
            "identity": self.identity,
            "results": [{
                "id": check_id,
                "row_count": 1,
                "column_count": 1,
                "numeric": True,
                "value": value,
            }],
        }
        return "\n".join((
            f"CHECK_PAYLOAD_BEGIN:{phase}",
            json.dumps(payload, separators=(",", ":")),
            f"CHECK_PAYLOAD_END:{phase}",
            f"CHECK_VERIFIED:{phase}",
        ))

    def identity_frame(self) -> str:
        return "\n".join((
            "MIGRATION_IDENTITY_BEGIN",
            json.dumps(self.identity, separators=(",", ":")),
            "MIGRATION_IDENTITY_END",
            "MIGRATION_IDENTITY_VERIFIED",
        ))

    def fake_sqlcl(self, output: str, *, status: int = 0, autocommit: str = "OFF") -> Path:
        script = (
            'for argument in "$@"; do\n'
            '  case "$argument" in\n'
            '    *autocommit-probe.sql*)\n'
            '      printf "%s\\n" MIGRATION_REHEARSAL_AUTOCOMMIT_BEGIN "AUTOCOMMIT is ' + autocommit + '" MIGRATION_REHEARSAL_AUTOCOMMIT_END\n'
            '      exit 0\n'
            '      ;;\n'
            '  esac\n'
            'done\n'
            "cat <<'FAKE_SQLCL_OUTPUT'\n"
            + output + "\nFAKE_SQLCL_OUTPUT\n"
        )
        if status:
            script += f"exit {status}\n"
        return install(self.root / "fake-bin", script)

    def inventory(self, target, run_dir):
        return SimpleNamespace(identity=self.identity)

    def invoke_rehearsal(self, migrations, *, report_path=None, confirm=None, run_checks_fn=None):
        captured_drivers: list[str] = []
        self.check_drivers: list[str] = []

        def capture_and_run(target, driver, run_dir, **kwargs):
            if driver.name == "rehearsal-driver.sql":
                captured_drivers.append(driver.read_text(encoding="utf-8"))
            self.check_drivers.extend(
                path.read_text(encoding="utf-8")
                for path in sorted(driver.parent.glob("*.sql"))
                if path != driver and driver.name == "rehearsal-driver.sql"
            )
            return run_sqlcl(target, driver, run_dir, **kwargs)

        output = io.StringIO()
        with patch.dict(os.environ, fake_environment(self.root / "fake-bin")):
            with contextlib.redirect_stdout(output):
                status = rehearse_batch(
                    self.root,
                    migrations,
                    self.target,
                    confirm or (lambda _prompt: True),
                    capture_inventory_fn=self.inventory,
                    run_session_fn=capture_and_run,
                    run_checks_fn=run_checks_fn,
                    report_path=report_path,
                )
        return status, output.getvalue(), captured_drivers

    def test_two_folders_rehearse_in_order_with_same_session_postchecks_and_rollback_proof(self) -> None:
        first = self.add_folder(
            "2026-10-01_first-r001",
            {"001-seed.sql": "INSERT INTO ITEMS (ID) VALUES (1);\n"},
            pre_id="first-before",
            post_id="first-after",
        )
        second = self.add_folder(
            "2026-10-02_second-r001",
            {"001-worker.sql": "UPDATE ITEMS SET CLAIMED = 'Y' WHERE ID = 1;\n"},
            pre_id="second-before",
            post_id="second-after",
        )
        output_lines = [
            "AUTOCOMMIT is OFF",
            self.identity_frame(),
            self.frame("preconditions", "first-before"),
            "MIGRATION_REHEARSAL_FILE_BEGIN:001:001",
            "1 row inserted.",
            "MIGRATION_REHEARSAL_FILE_END:001:001",
            self.frame("postconditions", "first-after"),
            self.frame("preconditions", "second-before"),
            "MIGRATION_REHEARSAL_FILE_BEGIN:002:001",
            "1 row updated.",
            "MIGRATION_REHEARSAL_FILE_END:002:001",
            self.frame("postconditions", "second-after"),
            "MIGRATION_REHEARSAL_ROLLBACK_COMPLETED",
            self.frame("preconditions", "first-before"),
            self.frame("preconditions", "second-before"),
        ]
        self.fake_sqlcl("\n".join(output_lines))
        report_path = self.root / "rehearsal.json"

        status, _stdout, drivers = self.invoke_rehearsal(self.load(first.name, second.name), report_path=report_path)

        self.assertEqual(status, 0)
        self.assertEqual(len(drivers), 1, "all selected folders must use one SQLcl transaction")
        driver = drivers[0]
        self.assertLess(driver.index("@@pre-001.sql"), driver.index("001-seed.sql"))
        self.assertLess(driver.index("001-seed.sql"), driver.index("@@post-001.sql"))
        self.assertLess(driver.index("@@post-001.sql"), driver.index("@@pre-002.sql"))
        self.assertLess(driver.index("@@pre-002.sql"), driver.index("001-worker.sql"))
        self.assertLess(driver.index("001-worker.sql"), driver.index("@@post-002.sql"))
        self.assertLess(driver.index("@@post-002.sql"), driver.index("ROLLBACK;"))
        self.assertLess(driver.index("ROLLBACK;"), driver.index("@@rollback-pre-001.sql"))
        self.assertLess(driver.index("@@rollback-pre-001.sql"), driver.index("@@rollback-pre-002.sql"))
        self.assertIn("SET AUTOCOMMIT OFF", driver)
        self.assertIn("WHENEVER SQLERROR EXIT FAILURE ROLLBACK", driver)
        self.assertNotIn("SET TRANSACTION READ ONLY", driver)
        self.assertTrue(self.check_drivers)
        self.assertTrue(all("SET TRANSACTION READ ONLY" not in check for check in self.check_drivers))
        self.assertTrue(all("EXIT SUCCESS ROLLBACK" not in check for check in self.check_drivers))
        self.assertEqual(report_path.stat().st_mode & 0o777, 0o600)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["rollbackProof"]["status"], "true")
        self.assertEqual([folder["postconditions"]["status"] for folder in report["folders"]], ["true", "true"])
        self.assertEqual(report["folders"][0]["statements"][0]["rowsAffected"], 1)
        self.assertEqual(report["folders"][1]["statements"][0]["rowsAffected"], 1)
        self.assertFalse((first / "status.dev.json").exists())
        self.assertFalse((second / "status.dev.json").exists())
        scratch = self.root / "scratch"
        self.assertFalse(any(scratch.rglob("run-manifest.json")) if scratch.exists() else False)

    def test_ddl_file_is_reported_not_rehearsable_and_never_included_in_driver(self) -> None:
        folder = self.add_folder(
            "2026-10-03_mixed-r001",
            {
                "001-seed.sql": "INSERT INTO ITEMS (ID) VALUES (1);\n",
                "002-index.sql": "CREATE UNIQUE INDEX ITEMS_U1 ON ITEMS (ID);\n",
            },
            pre_id="before",
            post_id="after",
        )
        output = "\n".join((
            "AUTOCOMMIT is OFF",
            self.identity_frame(),
            self.frame("preconditions", "before"),
            "MIGRATION_REHEARSAL_FILE_BEGIN:001:001",
            "1 row inserted.",
            "MIGRATION_REHEARSAL_FILE_END:001:001",
            self.frame("postconditions", "after"),
            "MIGRATION_REHEARSAL_ROLLBACK_COMPLETED",
            self.frame("preconditions", "before"),
        ))
        self.fake_sqlcl(output)
        report_path = self.root / "partial.json"

        status, _stdout, drivers = self.invoke_rehearsal(self.load(folder.name), report_path=report_path)

        self.assertEqual(status, 1)
        self.assertNotIn("002-index.sql", drivers[0])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        index = next(file for file in report["folders"][0]["files"] if file["file"] == "002-index.sql")
        self.assertEqual(index["status"], "not rehearsable")
        self.assertIn("CREATE_INDEX", index["reason"])
        self.assertEqual(report["folders"][0]["statements"][0]["rowsAffected"], 1)
        self.assertFalse((folder / "status.dev.json").exists())

    def test_zero_rows_are_reported_and_a_false_postcondition_does_not_hide_rollback(self) -> None:
        folder = self.add_folder(
            "2026-10-04_worker-r001",
            {"001-claim.sql": "UPDATE WORKERS SET CLAIMED = 'Y' WHERE ACTIVE = 'Y';\n"},
            pre_id="workers-exist",
            post_id="one-worker-claimed",
        )
        output = "\n".join((
            "AUTOCOMMIT is OFF",
            self.identity_frame(),
            self.frame("preconditions", "workers-exist"),
            "MIGRATION_REHEARSAL_FILE_BEGIN:001:001",
            "0 rows updated.",
            "MIGRATION_REHEARSAL_FILE_END:001:001",
            self.frame("postconditions", "one-worker-claimed", 0),
            "MIGRATION_REHEARSAL_ROLLBACK_COMPLETED",
            self.frame("preconditions", "workers-exist"),
        ))
        self.fake_sqlcl(output)
        report_path = self.root / "zero-rows.json"

        status, _stdout, _drivers = self.invoke_rehearsal(self.load(folder.name), report_path=report_path)

        self.assertEqual(status, 1)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["folders"][0]["statements"][0]["rowsAffected"], 0)
        self.assertEqual(report["folders"][0]["postconditions"]["status"], "false")
        self.assertEqual(report["rollbackProof"]["status"], "true")

    def test_false_precondition_stops_the_batch_before_any_data_file(self) -> None:
        folder = self.add_folder(
            "2026-10-04_precondition-r001",
            {"001-seed.sql": "INSERT INTO ITEMS (ID) VALUES (1);\n"},
            pre_id="service-ready",
            post_id="seed-present",
        )
        self.fake_sqlcl("\n".join((
            "AUTOCOMMIT is OFF",
            self.identity_frame(),
            self.frame("preconditions", "service-ready", 0),
            "CHECK_REHEARSAL_FAILED:preconditions",
            "ORA-20985: Migration rehearsal precondition failed",
        )), status=1)

        def passing_checks(target, checks, run_dir, *, phase):
            results = tuple({"id": check.id, "passed": True, "row_count": 1, "column_count": 1, "numeric": True, "value": 1} for check in checks)
            return CheckReport(True, True, results, (), {"phase": phase, "identity": self.identity})

        report_path = self.root / "false-precondition.json"
        status, _stdout, _drivers = self.invoke_rehearsal(
            self.load(folder.name), report_path=report_path, run_checks_fn=passing_checks,
        )

        self.assertEqual(status, 1)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["folders"][0]["preconditions"]["status"], "false")
        self.assertEqual(report["folders"][0]["statements"][0]["status"], "not run")
        self.assertEqual(report["rollbackProof"]["status"], "true")
        self.assertEqual(report["folders"][0]["rowsAffected"], None)
        self.assertFalse((folder / "status.dev.json").exists())

    def test_sql_error_reports_rows_and_rechecks_preconditions_after_rollback(self) -> None:
        folder = self.add_folder(
            "2026-10-05_unique-seed-r001",
            {"001-seed.sql": "INSERT INTO SERVICE_AREAS (SERVICE_AREA_ID, PRIORITY) VALUES (4, 1);\n"},
            pre_id="service-area-ready",
            post_id="seed-present",
        )
        self.fake_sqlcl("\n".join((
            "AUTOCOMMIT is OFF",
            self.identity_frame(),
            self.frame("preconditions", "service-area-ready"),
            "MIGRATION_REHEARSAL_FILE_BEGIN:001:001",
            "ORA-00001: unique constraint (APP.SERVICE_AREA_U1) violated",
        )), status=1)
        proof_calls = []

        def passing_checks(target, checks, run_dir, *, phase):
            proof_calls.append((phase, tuple(check.id for check in checks)))
            results = tuple({"id": check.id, "passed": True, "row_count": 1, "column_count": 1, "numeric": True, "value": 1} for check in checks)
            return CheckReport(True, True, results, (), {"phase": phase, "identity": self.identity})

        report_path = self.root / "failed.json"

        status, stdout, _drivers = self.invoke_rehearsal(self.load(folder.name), report_path=report_path, run_checks_fn=passing_checks)

        self.assertEqual(status, 2)
        self.assertEqual(proof_calls, [("preconditions", ("service-area-ready",))])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(any("ORA-00001" in error for error in report["errors"]))
        self.assertEqual(report["rollbackProof"]["status"], "true")
        self.assertIn("preconditions after SQLcl exit", report["rollbackProof"]["method"])
        self.assertEqual(report["folders"][0]["statements"][0]["rowsAffected"], None)
        self.assertEqual(report["folders"][0]["postconditions"]["status"], "not run")
        self.assertFalse((folder / "status.dev.json").exists())

    def test_rollback_proof_rejects_preconditions_observed_on_a_different_target(self) -> None:
        folder = self.add_folder(
            "2026-10-05_wrong-proof-target-r001",
            {"001-seed.sql": "INSERT INTO ITEMS (ID) VALUES (1);\n"},
            pre_id="before",
            post_id="after",
        )
        self.fake_sqlcl("\n".join((
            "AUTOCOMMIT is OFF",
            self.identity_frame(),
            self.frame("preconditions", "before"),
            "MIGRATION_REHEARSAL_FILE_BEGIN:001:001",
            "ORA-00001: unique constraint (APP.ITEMS_U1) violated",
        )), status=1)

        def wrong_target_checks(target, checks, run_dir, *, phase):
            results = tuple({"id": check.id, "passed": True, "row_count": 1, "column_count": 1, "numeric": True, "value": 1} for check in checks)
            wrong_identity = {**self.identity, "db_name": "OTHERDB"}
            return CheckReport(True, True, results, (), {"phase": phase, "identity": wrong_identity})

        report_path = self.root / "wrong-proof-target.json"
        status, _stdout, _drivers = self.invoke_rehearsal(
            self.load(folder.name), report_path=report_path, run_checks_fn=wrong_target_checks,
        )

        self.assertEqual(status, 2)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["rollbackProof"]["status"], "error")
        self.assertIn("different database/schema identity", report["folders"][0]["rollbackProof"]["error"])
        self.assertFalse((folder / "status.dev.json").exists())

    def test_staging_confirmation_is_still_required_for_rehearsal(self) -> None:
        folder = self.add_folder(
            "2026-10-06_confirm-r001",
            {"001-seed.sql": "INSERT INTO ITEMS (ID) VALUES (1);\n"},
            pre_id="before",
            post_id="after",
        )
        staging = Target("staging", "fake-profile", "APP", "APP", "staging")
        prompts = []
        calls = []

        def no_session(*args, **kwargs):
            calls.append(args)
            raise AssertionError("declined rehearsal must not start SQLcl")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = rehearse_batch(
                self.root,
                self.load(folder.name),
                staging,
                lambda prompt: prompts.append(prompt) or False,
                capture_inventory_fn=self.inventory,
                run_session_fn=no_session,
            )

        self.assertEqual(status, 1)
        self.assertEqual(len(prompts), 1)
        self.assertIn("STAGING", prompts[0])
        self.assertEqual(calls, [])
        self.assertFalse((folder / "status.staging.json").exists())

    def test_autocommit_must_be_confirmed_off_before_payload_session(self) -> None:
        folder = self.add_folder(
            "2026-10-09_autocommit-r001",
            {"001-seed.sql": "INSERT INTO ITEMS (ID) VALUES (1);\n"},
            pre_id="before",
            post_id="after",
        )
        self.fake_sqlcl("", autocommit="ON")
        report_path = self.root / "autocommit.json"

        status, _stdout, drivers = self.invoke_rehearsal(self.load(folder.name), report_path=report_path)

        self.assertEqual(status, 2)
        self.assertEqual(drivers, [])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(any("autocommit is not confirmed off" in error for error in report["errors"]))
        self.assertFalse((folder / "status.dev.json").exists())

    def test_in_session_checks_skip_readonly_transaction_but_keep_safe_function_validation(self) -> None:
        driver = render_in_session_check_driver(
            self.target,
            [QueryCheck("before", "SELECT 1 FROM dual", 1)],
            "preconditions",
            fail_on_false=True,
        )
        self.assertNotIn("SET TRANSACTION READ ONLY", driver)
        self.assertNotIn("EXIT SUCCESS ROLLBACK", driver)
        self.assertIn("CHECK_REHEARSAL_FAILED:preconditions", driver)
        with self.assertRaises(MigrationManifestError):
            render_in_session_check_driver(
                self.target,
                [QueryCheck("unsafe", "SELECT UTL_HTTP.REQUEST('https://example.invalid') FROM dual", 1)],
                "postconditions",
            )

    def test_report_is_rejected_without_rehearsal(self) -> None:
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = migrate_main(
                ["migrations/2026-10-07_cli-r001", "--env", "dev", "--report", "out.json"],
                repo_root=self.root,
            )
        self.assertEqual(status, 2)
        self.assertIn("--report requires --rehearse", stderr.getvalue())

    def test_migrate_cli_forwards_rehearsal_and_report(self) -> None:
        folder = self.add_folder(
            "2026-10-08_cli-r001",
            {"001-seed.sql": "INSERT INTO ITEMS (ID) VALUES (1);\n"},
            pre_id="before",
            post_id="after",
        )
        report_path = self.root / "rehearsal.json"
        with patch("scripts.migrate.resolve_target", return_value=self.target), patch(
            "scripts.migrate.rehearse_batch", return_value=0
        ) as rehearse:
            status = migrate_main(
                [f"migrations/{folder.name}", "--env", "dev", "--rehearse", "--report", str(report_path)],
                repo_root=self.root,
                environ={"CODE_SCHEMA": "APP", "DB_ENVIRONMENT": "development"},
            )
        self.assertEqual(status, 0)
        self.assertEqual(rehearse.call_args.kwargs["report_path"], report_path)


if __name__ == "__main__":
    unittest.main()
