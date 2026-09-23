from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import json
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from teamlib.apex_validate import ValidationReport
from teamlib.config import Target
from teamlib.control_store import ControlStore
from teamlib.page_locks import LockReport, PageLock
from teamlib.publish import Preparation, PublishError, format_publish_notice, load_preparation, prepare_publish
from teamlib.state import save_capture, save_verified_baseline


class PreparePublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-publish-test-")
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test Committer"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "committer@example.com"], check=True)

        self.target_hr = Target(
            project="team", role="developer", environment="development", connection="fake-hr",
            instance_id="FREE", db_name="FREEPDB1", service="freep1", session_user="DEMO",
            current_schema="DEMO", alias="hr", workspace_id=10, app_id=101,
            parsing_schema="HR_DATA", ownership_mode="shared", binding_digest="h" * 64,
        )
        self.target_payroll = Target(
            project="team", role="developer", environment="development", connection="fake-payroll",
            instance_id="FREE", db_name="FREEPDB1", service="freep1", session_user="DEMO",
            current_schema="DEMO", alias="payroll", workspace_id=10, app_id=102,
            parsing_schema="PAYROLL_DATA", ownership_mode="shared", binding_digest="p" * 64,
        )

        # Seed repo files
        (self.repo / "apps" / "hr" / "pages").mkdir(parents=True)
        (self.repo / "apps" / "hr" / "application.apx").write_bytes(b"app hr\n")
        (self.repo / "apps" / "hr" / ".apex").mkdir(parents=True)
        (self.repo / "apps" / "hr" / ".apex" / "apexlang.json").write_bytes(b'{"format":"APEXLANG"}\n')
        (self.repo / "apps" / "hr" / "pages" / "p1.apx").write_bytes(b"page 1\n")

        (self.repo / "apps" / "payroll" / "pages").mkdir(parents=True)
        (self.repo / "apps" / "payroll" / "application.apx").write_bytes(b"app payroll\n")
        (self.repo / "apps" / "payroll" / ".apex").mkdir(parents=True)
        (self.repo / "apps" / "payroll" / ".apex" / "apexlang.json").write_bytes(b'{"format":"APEXLANG"}\n')
        (self.repo / "apps" / "payroll" / "pages" / "p10.apx").write_bytes(b"page 10\n")

        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial apps"], check=True)
        self.seed_commit = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()

        self.store = ControlStore(self.repo / ".sync-state")
        self.store.setup_state([self.target_hr, self.target_payroll])
        self.store.register_app(self.target_hr, "checkout-hr-1", "host-1", "alice")
        self.store.register_app(self.target_payroll, "checkout-payroll-1", "host-2", "bob")

        self.hr_tree = {
            "application.apx": b"app hr\n",
            ".apex/apexlang.json": b'{"format":"APEXLANG"}\n',
            "pages/p1.apx": b"page 1\n",
        }
        self.payroll_tree = {
            "application.apx": b"app payroll\n",
            ".apex/apexlang.json": b'{"format":"APEXLANG"}\n',
            "pages/p10.apx": b"page 10\n",
        }

        # Save baselines
        save_verified_baseline(self.target_hr, self.seed_commit, self.hr_tree, root=self.repo / ".sync-state")
        save_verified_baseline(self.target_payroll, self.seed_commit, self.payroll_tree, root=self.repo / ".sync-state")

        self.lock_report_hr = LockReport(
            alias="hr", app_id=101, status="KNOWN",
            pages=(PageLock(1, "Home", "alice", "2026-09-23T20:00:00Z", "Working on home"),),
            source="APEX_APPLICATION_LOCKED_PAGES",
        )
        self.lock_report_payroll = LockReport(
            alias="payroll", app_id=102, status="KNOWN",
            pages=(),
            source="APEX_APPLICATION_LOCKED_PAGES",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fake_runner(self, target, operation, driver, work, **kwargs):
        export = Path(work) / "exported-app"
        tree = self.hr_tree if target.alias == "hr" else self.payroll_tree
        for path, data in tree.items():
            dest = export / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        stdout = (
            f"TEAM_RESULT_BEGIN\n"
            f"TEAM_APP_ID_APEX_VERSION|26.1.4\n"
            f"TEAM_APP_ID_WS_SCHEMA|{target.workspace_id}|{target.parsing_schema}\n"
            f"TEAM_APP_ID_APP|{target.workspace_id}|{target.app_id}|{target.parsing_schema}\n"
            f"TEAM_RESULT_END\n"
        )
        return SimpleNamespace(
            identity={"SESSION_USER": "DEMO", "CURRENT_SCHEMA": "DEMO", "DB_NAME": "FREEPDB1", "SERVICE": "freep1", "INSTANCE_ID": "FREE"},
            completion={"operation": operation}, result_manifest={"status": "success"},
            log_path=Path(work) / "fake.log", generated_driver=Path(driver),
            stdout=stdout, stderr="", argv=(), exit_code=0,
        )

    def fake_validator(self, tree, **kwargs):
        return ValidationReport(success=True, status="SUCCESS", log="Validation successful.", errors=())

    def test_prepare_publish_success_single_app(self):
        prep = prepare_publish(
            self.repo,
            (self.target_hr,),
            self.seed_commit,
            {"hr": self.lock_report_hr},
            self.store,
            runner=self.fake_runner,
            validator=self.fake_validator,
        )
        self.assertIsInstance(prep, Preparation)
        self.assertEqual(prep.aliases, ("hr",))
        self.assertEqual(prep.source_commit, self.seed_commit)
        self.assertEqual(prep.version, 1)
        self.assertTrue(prep.record_digest)

        # Verify durable file on disk
        loaded = load_preparation(self.repo, prep.preparation_id)
        self.assertEqual(loaded.preparation_id, prep.preparation_id)
        self.assertEqual(loaded.aliases, ("hr",))
        self.assertEqual(loaded.record_digest, prep.record_digest)

    def test_prepare_hr_does_not_include_payroll_in_notice_or_record(self):
        prep = prepare_publish(
            self.repo,
            (self.target_hr,),
            self.seed_commit,
            {"hr": self.lock_report_hr},
            self.store,
            runner=self.fake_runner,
            validator=self.fake_validator,
        )
        # Check record
        self.assertNotIn("payroll", prep.aliases)
        self.assertNotIn("payroll", prep.apps)

        record_file = self.repo / ".sync-state" / "publish" / prep.preparation_id / "prepare.json"
        content = json.loads(record_file.read_text(encoding="utf-8"))
        self.assertNotIn("payroll", content["aliases"])
        self.assertNotIn("payroll", content["apps"])

        # Check notice
        notice = format_publish_notice(prep)
        self.assertIn("hr", notice.lower())
        self.assertNotIn("payroll", notice.lower())

    def test_omitted_aliases_refuses(self):
        with self.assertRaises(PublishError) as ctx:
            prepare_publish(
                self.repo,
                (),
                self.seed_commit,
                {},
                self.store,
                runner=self.fake_runner,
                validator=self.fake_validator,
            )
        self.assertIn("at least one", str(ctx.exception).lower())

    def test_duplicate_aliases_refuses(self):
        with self.assertRaises(PublishError) as ctx:
            prepare_publish(
                self.repo,
                (self.target_hr, self.target_hr),
                self.seed_commit,
                {"hr": self.lock_report_hr},
                self.store,
                runner=self.fake_runner,
                validator=self.fake_validator,
            )
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_non_development_target_refuses(self):
        prod_target = Target(
            project="team", role="production", environment="production", connection="prod",
            instance_id="PROD", db_name="PRODPDB", service="prod", session_user="PROD",
            current_schema="PROD", alias="hr", workspace_id=10, app_id=101,
            parsing_schema="HR_DATA", ownership_mode="shared", binding_digest="x" * 64,
        )
        with self.assertRaises(PublishError) as ctx:
            prepare_publish(
                self.repo,
                (prod_target,),
                self.seed_commit,
                {"hr": self.lock_report_hr},
                self.store,
                runner=self.fake_runner,
                validator=self.fake_validator,
            )
        self.assertIn("development", str(ctx.exception).lower())

    def test_dirty_selected_source_refuses(self):
        (self.repo / "apps" / "hr" / "pages" / "p1.apx").write_bytes(b"dirty changes\n")
        with self.assertRaises(PublishError) as ctx:
            prepare_publish(
                self.repo,
                (self.target_hr,),
                self.seed_commit,
                {"hr": self.lock_report_hr},
                self.store,
                runner=self.fake_runner,
                validator=self.fake_validator,
            )
        self.assertIn("clean", str(ctx.exception).lower())

    def test_stale_or_invalid_source_ref_refuses(self):
        with self.assertRaises(PublishError) as ctx:
            prepare_publish(
                self.repo,
                (self.target_hr,),
                "deadbeef" * 5,
                {"hr": self.lock_report_hr},
                self.store,
                runner=self.fake_runner,
                validator=self.fake_validator,
            )
        self.assertIn("ref", str(ctx.exception).lower())

    def test_unknown_lock_report_refuses(self):
        unknown_report = LockReport(
            alias="hr", app_id=101, status="UNKNOWN", pages=(), source="APEX_APPLICATION_LOCKED_PAGES"
        )
        with self.assertRaises(PublishError) as ctx:
            prepare_publish(
                self.repo,
                (self.target_hr,),
                self.seed_commit,
                {"hr": unknown_report},
                self.store,
                runner=self.fake_runner,
                validator=self.fake_validator,
            )
        self.assertIn("unknown", str(ctx.exception).lower())

    def test_missing_baseline_and_receipt_refuses(self):
        # Delete baseline for hr
        (self.repo / ".sync-state" / "baselines" / self.target_hr.state_key / "baseline.json").unlink()
        with self.assertRaises(PublishError) as ctx:
            prepare_publish(
                self.repo,
                (self.target_hr,),
                self.seed_commit,
                {"hr": self.lock_report_hr},
                self.store,
                runner=self.fake_runner,
                validator=self.fake_validator,
            )
        self.assertIn("baseline", str(ctx.exception).lower())

    def test_unreconciled_capture_differs_from_baseline_refuses(self):
        # Database tree differs from baseline
        self.hr_tree["pages/surprise.apx"] = b"unreconciled builder page\n"
        with self.assertRaises(PublishError) as ctx:
            prepare_publish(
                self.repo,
                (self.target_hr,),
                self.seed_commit,
                {"hr": self.lock_report_hr},
                self.store,
                runner=self.fake_runner,
                validator=self.fake_validator,
            )
        self.assertIn("differs", str(ctx.exception).lower())

    def test_replace_from_allows_app_without_baseline(self):
        # Remove baseline
        (self.repo / ".sync-state" / "baselines" / self.target_hr.state_key / "baseline.json").unlink()
        # Create a valid reviewed capture with save_capture
        rec_id = save_capture(
            self.target_hr,
            {},
            {},
            self.seed_commit,
            self.hr_tree,
            {"head_commit": self.seed_commit, "reviewed": True},
            root=self.repo / ".sync-state",
        )
        prep = prepare_publish(
            self.repo,
            (self.target_hr,),
            self.seed_commit,
            {"hr": self.lock_report_hr},
            self.store,
            runner=self.fake_runner,
            validator=self.fake_validator,
            replace_from={"hr": rec_id},
        )
        self.assertEqual(prep.aliases, ("hr",))

    def test_invalid_apexlang_refuses(self):
        def failing_validator(tree, **kwargs):
            return ValidationReport(success=False, status="FAILED", log="Error: syntax error\n", errors=("syntax error",))

        with self.assertRaises(PublishError) as ctx:
            prepare_publish(
                self.repo,
                (self.target_hr,),
                self.seed_commit,
                {"hr": self.lock_report_hr},
                self.store,
                runner=self.fake_runner,
                validator=failing_validator,
            )
        self.assertIn("validation", str(ctx.exception).lower())

    def test_prepare_multiple_apps_success(self):
        prep = prepare_publish(
            self.repo,
            (self.target_payroll, self.target_hr),  # Pass out of order
            self.seed_commit,
            {"hr": self.lock_report_hr, "payroll": self.lock_report_payroll},
            self.store,
            runner=self.fake_runner,
            validator=self.fake_validator,
        )
        self.assertEqual(prep.aliases, ("hr", "payroll"))  # Sorted
        self.assertIn("hr", prep.apps)
        self.assertIn("payroll", prep.apps)
        notice = format_publish_notice(prep)
        self.assertIn("hr", notice.lower())
        self.assertIn("payroll", notice.lower())

    def test_existing_record_directory_refuses(self):
        # Create an existing directory for prep_id
        prep_id = "test-existing-dir-id"
        existing_dir = self.repo / ".sync-state" / "publish" / prep_id
        existing_dir.mkdir(parents=True)
        with self.assertRaises(PublishError) as ctx:
            prepare_publish(
                self.repo,
                (self.target_hr,),
                self.seed_commit,
                {"hr": self.lock_report_hr},
                self.store,
                runner=self.fake_runner,
                validator=self.fake_validator,
                preparation_id=prep_id,
            )
        self.assertIn("already exists", str(ctx.exception).lower())

    def test_tampered_preparation_record_refuses(self):
        prep = prepare_publish(
            self.repo,
            (self.target_hr,),
            self.seed_commit,
            {"hr": self.lock_report_hr},
            self.store,
            runner=self.fake_runner,
            validator=self.fake_validator,
        )
        record_file = self.repo / ".sync-state" / "publish" / prep.preparation_id / "prepare.json"
        data = json.loads(record_file.read_text(encoding="utf-8"))
        data["source_commit"] = "0" * 40  # Tamper with commit without updating digest
        record_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        with self.assertRaises(PublishError) as ctx:
            load_preparation(self.repo, prep.preparation_id)
        self.assertIn("tamper", str(ctx.exception).lower())

    def test_preparation_record_contains_no_secrets(self):
        prep = prepare_publish(
            self.repo,
            (self.target_hr,),
            self.seed_commit,
            {"hr": self.lock_report_hr},
            self.store,
            runner=self.fake_runner,
            validator=self.fake_validator,
        )
        record_file = self.repo / ".sync-state" / "publish" / prep.preparation_id / "prepare.json"
        raw_text = record_file.read_text(encoding="utf-8")
        for secret_marker in ("password", "secret", "private_key", "token_value", "bearer"):
            self.assertNotIn(secret_marker, raw_text.lower())


if __name__ == "__main__":
    unittest.main()
