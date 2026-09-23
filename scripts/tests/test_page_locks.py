from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from datetime import datetime, timezone, timedelta
import json
from types import SimpleNamespace
import tempfile
import unittest

from teamlib.config import Target
from teamlib.page_locks import (
    LockReport,
    PageLock,
    PageLockError,
    format_lock_report,
    load_manual_page_locks,
    read_page_locks,
)
from teamlib.sqlcl import SqlclError


class PageLocksReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = Target(
            project="team", role="developer", environment="development", connection="fake",
            instance_id="FREE", db_name="FREEPDB1", service="freep1", session_user="DEMO",
            current_schema="DEMO", alias="hr", workspace_id=10, app_id=100,
            parsing_schema="HR_SCHEMA", ownership_mode="shared", binding_digest="a" * 64,
        )

    def test_two_locks_with_distinct_owners_including_current_user(self):
        # Even if locked by target.session_user ("DEMO"), the lock must NOT be filtered out
        stdout = """\
TEAM_RESULT_BEGIN
TEAM_PAGE_LOCK|1|Home|DEMO|2026-09-23T20:00:00Z|Editing home
TEAM_PAGE_LOCK|2|Employees|ALICE|2026-09-23T20:05:00Z|Updating grid
TEAM_RESULT_END
"""
        def runner(target, operation, driver, work, **kwargs):
            return SimpleNamespace(
                stdout=stdout, stderr="", exit_code=0,
                identity={"SESSION_USER": "DEMO", "CURRENT_SCHEMA": "DEMO", "DB_NAME": "FREEPDB1", "SERVICE": "freep1", "INSTANCE_ID": "FREE"},
                completion={"operation": operation}, result_manifest={"status": "success"},
            )

        report = read_page_locks(self.target, runner=runner)
        self.assertEqual(report.alias, "hr")
        self.assertEqual(report.app_id, 100)
        self.assertEqual(report.status, "KNOWN")
        self.assertEqual(len(report.pages), 2)
        self.assertEqual(report.pages[0].page_id, 1)
        self.assertEqual(report.pages[0].locked_by, "DEMO")
        self.assertEqual(report.pages[0].page_name, "Home")
        self.assertEqual(report.pages[1].page_id, 2)
        self.assertEqual(report.pages[1].locked_by, "ALICE")
        self.assertEqual(report.source, "APEX_APPLICATION_LOCKED_PAGES")

    def test_zero_rows_reported_as_known_empty(self):
        stdout = """\
TEAM_RESULT_BEGIN
TEAM_RESULT_END
"""
        def runner(target, operation, driver, work, **kwargs):
            return SimpleNamespace(
                stdout=stdout, stderr="", exit_code=0,
                identity={"SESSION_USER": "DEMO", "CURRENT_SCHEMA": "DEMO", "DB_NAME": "FREEPDB1", "SERVICE": "freep1", "INSTANCE_ID": "FREE"},
                completion={"operation": operation}, result_manifest={"status": "success"},
            )

        report = read_page_locks(self.target, runner=runner)
        self.assertEqual(report.status, "KNOWN")
        self.assertEqual(report.pages, ())
        rendered = format_lock_report(report)
        self.assertIn("No locked pages reported", rendered)

    def test_missing_view_or_sqlcl_error_returns_unknown(self):
        def failing_runner(target, operation, driver, work, **kwargs):
            raise SqlclError("ORA-00942: table or view does not exist")

        report = read_page_locks(self.target, runner=failing_runner)
        self.assertEqual(report.status, "UNKNOWN")
        self.assertEqual(report.pages, ())

    def test_malformed_rows_or_duplicate_page_ids_returns_unknown(self):
        for bad_stdout in (
            "TEAM_RESULT_BEGIN\nTEAM_PAGE_LOCK|not-a-number|Home|DEMO|-|-\nTEAM_RESULT_END\n",
            "TEAM_RESULT_BEGIN\nTEAM_PAGE_LOCK|-5|Home|DEMO|-|-\nTEAM_RESULT_END\n",
            "TEAM_RESULT_BEGIN\nTEAM_PAGE_LOCK|1|Home|DEMO|-|-\nTEAM_PAGE_LOCK|1|Home Dup|BOB|-|-\nTEAM_RESULT_END\n",
            "TEAM_RESULT_BEGIN\nTEAM_PAGE_LOCK|1|Home\x00Control|DEMO|-|-\nTEAM_RESULT_END\n",
        ):
            with self.subTest(stdout=bad_stdout):
                def runner(target, operation, driver, work, out=bad_stdout, **kwargs):
                    return SimpleNamespace(
                        stdout=out, stderr="", exit_code=0,
                        identity={"SESSION_USER": "DEMO", "CURRENT_SCHEMA": "DEMO", "DB_NAME": "FREEPDB1", "SERVICE": "freep1", "INSTANCE_ID": "FREE"},
                        completion={"operation": operation}, result_manifest={"status": "success"},
                    )

                report = read_page_locks(self.target, runner=runner)
                self.assertEqual(report.status, "UNKNOWN")


class PageLocksManualFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-page-locks-")
        self.root = Path(self.temp.name)
        self.target = Target(
            project="team", role="developer", environment="development", connection="fake",
            instance_id="FREE", db_name="FREEPDB1", service="freep1", session_user="DEMO",
            current_schema="DEMO", alias="hr", workspace_id=10, app_id=100,
            parsing_schema="HR_SCHEMA", ownership_mode="shared", binding_digest="a" * 64,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def valid_manual_payload(self) -> dict:
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "version": 1,
            "target_state_key": self.target.state_key,
            "alias": "hr",
            "app_id": 100,
            "captured_at_utc": now_utc,
            "reviewed_by": "operator-alice",
            "pages": [
                {
                    "page_id": 42,
                    "page_name": "Employees",
                    "locked_by": "BOB",
                    "locked_on": "2026-09-23T20:00:00Z",
                    "comment": "Working on salary",
                }
            ],
        }

    def test_valid_manual_report_is_accepted_as_manual_builder_review(self):
        path = self.root / "locks.json"
        path.write_text(json.dumps(self.valid_manual_payload()), encoding="utf-8")
        report = load_manual_page_locks(path, self.target)
        self.assertEqual(report.status, "KNOWN")
        self.assertEqual(report.source, "MANUAL BUILDER REVIEW")
        self.assertEqual(len(report.pages), 1)
        self.assertEqual(report.pages[0].page_id, 42)
        self.assertEqual(report.pages[0].locked_by, "BOB")

    def test_stale_manual_report_older_than_five_minutes_is_rejected(self):
        stale_time = (datetime.now(timezone.utc) - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = self.valid_manual_payload()
        payload["captured_at_utc"] = stale_time
        path = self.root / "stale.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(PageLockError, "stale"):
            load_manual_page_locks(path, self.target)

    def test_mismatched_target_or_alias_is_rejected(self):
        payload = self.valid_manual_payload()
        payload["alias"] = "other"
        path = self.root / "mismatch.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(PageLockError, "alias"):
            load_manual_page_locks(path, self.target)

        payload2 = self.valid_manual_payload()
        payload2["target_state_key"] = "wrong-key"
        path2 = self.root / "mismatch_key.json"
        path2.write_text(json.dumps(payload2), encoding="utf-8")
        with self.assertRaisesRegex(PageLockError, "target_state_key"):
            load_manual_page_locks(path2, self.target)

    def test_unknown_fields_are_rejected(self):
        payload = self.valid_manual_payload()
        payload["unknown_extra_field"] = "bad"
        path = self.root / "extra.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(PageLockError, "unsupported"):
            load_manual_page_locks(path, self.target)


if __name__ == "__main__":
    unittest.main()
