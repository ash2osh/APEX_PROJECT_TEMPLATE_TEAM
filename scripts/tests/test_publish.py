from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import json
import os
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from teamlib.apex_validate import ValidationReport
from teamlib.config import Target, load_config, profile_target
from teamlib.control_store import ControlStore
from teamlib.page_locks import LockReport, PageLock
from teamlib.publish import (
    AppPublishResult,
    Preparation,
    PublishError,
    PublishReport,
    format_publish_notice,
    load_preparation,
    prepare_publish,
    publish_prepared,
)
from teamlib.sqlcl import SqlclError
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


class PublishPreparedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-publish-prepared-test-")
        self.repo = Path(self.temp.name)
        subprocess.run(["git", "-C", str(self.repo), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Test Committer"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "committer@example.com"], check=True)

        self.env_path = self.repo / ".env"
        self.env_path.write_text(
            "PROJECT_NAME=team\n"
            "TARGET_ROLE=developer\n"
            "DB_ENVIRONMENT=development\n"
            "APEX_APPS=hr:101:HR_DATA,payroll:102:PAYROLL_DATA\n"
            "TABLES_SCHEMA=APP_DATA\n"
            "CODE_SCHEMA=APP_CODE\n"
            "METADATA_SCHEMA=APP_META\n"
            "APP_OWNERSHIP_MODE=shared\n"
            "APEX_WORKSPACE_ID=10\n"
            "TABLES_SQLCL_CONNECTION=fake\n"
            "TABLES_EXPECTED_USER=DEMO\n"
            "TABLES_EXPECTED_CURRENT_SCHEMA=DEMO\n"
            "TABLES_EXPECTED_DB_NAME=FREEPDB1\n"
            "TABLES_EXPECTED_SERVICE=freep1\n"
            "TABLES_EXPECTED_INSTANCE_ID=FREE\n"
            "CODE_SQLCL_CONNECTION=fake\n"
            "CODE_EXPECTED_USER=DEMO\n"
            "CODE_EXPECTED_CURRENT_SCHEMA=DEMO\n"
            "CODE_EXPECTED_DB_NAME=FREEPDB1\n"
            "CODE_EXPECTED_SERVICE=freep1\n"
            "CODE_EXPECTED_INSTANCE_ID=FREE\n"
            "APEX_SQLCL_CONNECTION=fake\n"
            "APEX_EXPECTED_USER=DEMO\n"
            "APEX_EXPECTED_CURRENT_SCHEMA=DEMO\n"
            "APEX_EXPECTED_DB_NAME=FREEPDB1\n"
            "APEX_EXPECTED_SERVICE=freep1\n"
            "APEX_EXPECTED_INSTANCE_ID=FREE\n"
            "METADATA_SQLCL_CONNECTION=fake\n"
            "METADATA_EXPECTED_USER=DEMO\n"
            "METADATA_EXPECTED_CURRENT_SCHEMA=DEMO\n"
            "METADATA_EXPECTED_DB_NAME=FREEPDB1\n"
            "METADATA_EXPECTED_SERVICE=freep1\n"
            "METADATA_EXPECTED_INSTANCE_ID=FREE\n"
            "VERIFY_SQLCL_CONNECTION=fake\n"
            "VERIFY_EXPECTED_USER=DEMO\n"
            "VERIFY_EXPECTED_CURRENT_SCHEMA=DEMO\n"
            "VERIFY_EXPECTED_DB_NAME=FREEPDB1\n"
            "VERIFY_EXPECTED_SERVICE=freep1\n"
            "VERIFY_EXPECTED_INSTANCE_ID=FREE\n",
            encoding="utf-8",
        )
        self.config = load_config(self.env_path)
        self.target_hr = profile_target(self.config, "APEX", alias="hr")
        self.target_payroll = profile_target(self.config, "APEX", alias="payroll")

        # Create files for both apps
        (self.repo / "apps" / "hr" / "pages").mkdir(parents=True)
        (self.repo / "apps" / "hr" / "application.apx").write_bytes(b"app hr\n")
        (self.repo / "apps" / "hr" / ".apex").mkdir(parents=True)
        (self.repo / "apps" / "hr" / ".apex" / "apexlang.json").write_bytes(b'{"format":"APEXLANG"}\n')
        (self.repo / "apps" / "hr" / "pages" / "p1.apx").write_bytes(b"p1\n")

        (self.repo / "apps" / "payroll" / "pages").mkdir(parents=True)
        (self.repo / "apps" / "payroll" / "application.apx").write_bytes(b"app payroll\n")
        (self.repo / "apps" / "payroll" / ".apex").mkdir(parents=True)
        (self.repo / "apps" / "payroll" / ".apex" / "apexlang.json").write_bytes(b'{"format":"APEXLANG"}\n')
        (self.repo / "apps" / "payroll" / "pages" / "p10.apx").write_bytes(b"p10\n")

        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial commit"], check=True)
        self.seed_commit = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()

        self.store = ControlStore(self.repo / ".sync-state")
        self.store.setup_state([self.target_hr, self.target_payroll])
        self.store.register_app(self.target_hr, "hr-uuid-1", "host-1", "alice")
        self.store.register_app(self.target_payroll, "pay-uuid-2", "host-2", "bob")

        self.hr_tree = {
            "application.apx": b"app hr\n",
            ".apex/apexlang.json": b'{"format":"APEXLANG"}\n',
            "pages/p1.apx": b"p1\n",
        }
        self.payroll_tree = {
            "application.apx": b"app payroll\n",
            ".apex/apexlang.json": b'{"format":"APEXLANG"}\n',
            "pages/p10.apx": b"p10\n",
        }

        save_verified_baseline(self.target_hr, self.seed_commit, self.hr_tree, root=self.repo / ".sync-state")
        save_verified_baseline(self.target_payroll, self.seed_commit, self.payroll_tree, root=self.repo / ".sync-state")

        self.lock_report_hr = LockReport("hr", 101, "KNOWN", (), "APEX_APPLICATION_LOCKED_PAGES")
        self.lock_report_payroll = LockReport("payroll", 102, "KNOWN", (), "APEX_APPLICATION_LOCKED_PAGES")

        self.write_calls: list[tuple[str, str]] = []
        self.timeout_on_payroll = False

        # Prepare both apps
        self.prep = prepare_publish(
            self.repo,
            (self.target_hr, self.target_payroll),
            self.seed_commit,
            {"hr": self.lock_report_hr, "payroll": self.lock_report_payroll},
            self.store,
            runner=self.fake_runner,
            validator=lambda tree, **kwargs: ValidationReport(True, "SUCCESS", "ok", ()),
        )
        self.acks = {
            "hr": ("hr-uuid-1",),
            "payroll": ("pay-uuid-2",),
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def fake_runner(self, target, operation, driver, work, **kwargs):
        if operation == "write":
            if target.alias == "payroll" and self.timeout_on_payroll:
                raise SqlclError("SQLcl timed out on payroll import; state is unknown")
            self.write_calls.append((target.alias or "", operation))
            # simulate updated database tree on write
            if target.alias == "hr":
                self.database_hr_tree = dict(self.hr_tree)
            elif target.alias == "payroll":
                self.database_payroll_tree = dict(self.payroll_tree)

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

    def lock_reader(self, target, **kwargs):
        return self.lock_report_hr if target.alias == "hr" else self.lock_report_payroll

    def test_absent_pause_confirmation_refuses_with_zero_writes(self):
        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.repo,
                self.prep.preparation_id,
                self.acks,
                confirm_pause=False,
                config=self.config,
                store=self.store,
                runner=self.fake_runner,
                lock_reader=self.lock_reader,
            )
        self.assertIn("confirm", str(ctx.exception).lower())
        self.assertEqual(len(self.write_calls), 0)

    def test_missing_registered_acknowledgement_refuses_with_zero_writes(self):
        bad_acks = {"hr": (), "payroll": ("pay-uuid-2",)}
        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.repo,
                self.prep.preparation_id,
                bad_acks,
                confirm_pause=True,
                config=self.config,
                store=self.store,
                runner=self.fake_runner,
                lock_reader=self.lock_reader,
            )
        self.assertIn("acknowledgement", str(ctx.exception).lower())
        self.assertEqual(len(self.write_calls), 0)

    def test_acknowledgement_for_unselected_alias_refuses_with_zero_writes(self):
        bad_acks = {"hr": ("hr-uuid-1",), "payroll": ("pay-uuid-2",), "billing": ("bill-uuid",)}
        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.repo,
                self.prep.preparation_id,
                bad_acks,
                confirm_pause=True,
                config=self.config,
                store=self.store,
                runner=self.fake_runner,
                lock_reader=self.lock_reader,
            )
        self.assertIn("billing", str(ctx.exception).lower())
        self.assertEqual(len(self.write_calls), 0)

    def test_edited_preparation_json_refuses_with_zero_writes(self):
        record_file = self.repo / ".sync-state" / "publish" / self.prep.preparation_id / "prepare.json"
        data = json.loads(record_file.read_text(encoding="utf-8"))
        data["source_commit"] = "f" * 40
        record_file.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.repo,
                self.prep.preparation_id,
                self.acks,
                confirm_pause=True,
                config=self.config,
                store=self.store,
                runner=self.fake_runner,
                lock_reader=self.lock_reader,
            )
        self.assertIn("tamper", str(ctx.exception).lower())
        self.assertEqual(len(self.write_calls), 0)

    def test_changed_target_binding_refuses_with_zero_writes(self):
        # Alter the workspace_id in the config
        self.env_path.write_text(self.env_path.read_text(encoding="utf-8").replace("APEX_WORKSPACE_ID=10", "APEX_WORKSPACE_ID=999"), encoding="utf-8")
        changed_config = load_config(self.env_path)
        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.repo,
                self.prep.preparation_id,
                self.acks,
                confirm_pause=True,
                config=changed_config,
                store=self.store,
                runner=self.fake_runner,
                lock_reader=self.lock_reader,
            )
        self.assertIn("binding", str(ctx.exception).lower())
        self.assertEqual(len(self.write_calls), 0)

    def test_changed_selected_commit_or_dirty_source_refuses_with_zero_writes(self):
        (self.repo / "apps" / "hr" / "pages" / "p1.apx").write_bytes(b"dirty\n")
        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.repo,
                self.prep.preparation_id,
                self.acks,
                confirm_pause=True,
                config=self.config,
                store=self.store,
                runner=self.fake_runner,
                lock_reader=self.lock_reader,
            )
        self.assertIn("clean", str(ctx.exception).lower())
        self.assertEqual(len(self.write_calls), 0)

    def test_changed_first_app_capture_refuses_all_writes(self):
        # Change hr capture in Builder
        self.hr_tree["pages/surprise.apx"] = b"new builder page\n"
        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.repo,
                self.prep.preparation_id,
                self.acks,
                confirm_pause=True,
                config=self.config,
                store=self.store,
                runner=self.fake_runner,
                lock_reader=self.lock_reader,
            )
        self.assertIn("changed", str(ctx.exception).lower())
        self.assertEqual(len(self.write_calls), 0)

    def test_changed_second_app_capture_stops_both_imports_before_any_write(self):
        # Change payroll capture in Builder before any import occurs
        self.payroll_tree["pages/surprise.apx"] = b"surprise in payroll\n"
        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.repo,
                self.prep.preparation_id,
                self.acks,
                confirm_pause=True,
                config=self.config,
                store=self.store,
                runner=self.fake_runner,
                lock_reader=self.lock_reader,
            )
        self.assertIn("changed", str(ctx.exception).lower())
        self.assertEqual(len(self.write_calls), 0)

    def test_new_checkout_after_preparation_refuses_before_any_write(self):
        self.store.register_app(self.target_payroll, "pay-uuid-3", "host-3", "carol")
        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.repo, self.prep.preparation_id, self.acks,
                confirm_pause=True, config=self.config, store=self.store,
                runner=self.fake_runner, lock_reader=self.lock_reader,
            )
        self.assertIn("roster", str(ctx.exception).lower())
        self.assertEqual(self.write_calls, [])

    def test_missing_second_app_baseline_refuses_before_any_write(self):
        baseline = self.repo / ".sync-state" / "baselines" / self.target_payroll.state_key / "baseline.json"
        baseline.unlink()
        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.repo, self.prep.preparation_id, self.acks,
                confirm_pause=True, config=self.config, store=self.store,
                runner=self.fake_runner, lock_reader=self.lock_reader,
            )
        self.assertIn("baseline", str(ctx.exception).lower())
        self.assertEqual(self.write_calls, [])

    def test_changed_second_app_generation_refuses_before_any_write(self):
        self.store.acquire_app(self.target_payroll.physical_key, "other-run", "pay-uuid-2", "host-2", "bob")
        self.store.release_app(self.target_payroll.physical_key, "other-run", confirmed_success=True)
        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.repo, self.prep.preparation_id, self.acks,
                confirm_pause=True, config=self.config, store=self.store,
                runner=self.fake_runner, lock_reader=self.lock_reader,
            )
        self.assertIn("generation", str(ctx.exception).lower())
        self.assertEqual(self.write_calls, [])

    def test_registration_during_final_lock_report_refuses_before_any_write(self):
        def registering_lock_reader(target, **kwargs):
            if target.alias == "payroll":
                self.store.register_app(self.target_payroll, "late-pay-uuid", "host-3", "carol")
            return self.lock_reader(target, **kwargs)

        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.repo, self.prep.preparation_id, self.acks,
                confirm_pause=True, config=self.config, store=self.store,
                runner=self.fake_runner, lock_reader=registering_lock_reader,
            )
        self.assertIn("roster", str(ctx.exception).lower())
        self.assertEqual(self.write_calls, [])

    def test_baseline_removed_during_final_lock_report_refuses_before_any_write(self):
        def removing_lock_reader(target, **kwargs):
            if target.alias == "payroll":
                baseline = self.repo / ".sync-state" / "baselines" / target.state_key / "baseline.json"
                baseline.unlink()
            return self.lock_reader(target, **kwargs)

        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.repo, self.prep.preparation_id, self.acks,
                confirm_pause=True, config=self.config, store=self.store,
                runner=self.fake_runner, lock_reader=removing_lock_reader,
            )
        self.assertIn("baseline", str(ctx.exception).lower())
        self.assertEqual(self.write_calls, [])

    def test_new_checkout_during_first_import_blocks_second_app_import(self):
        def registering_runner(target, operation, driver, work, **kwargs):
            result = self.fake_runner(target, operation, driver, work, **kwargs)
            if operation == "write" and target.alias == "hr":
                self.store.register_app(self.target_payroll, "late-pay-uuid", "host-3", "carol")
            return result

        with self.assertRaises(PublishError):
            publish_prepared(
                self.repo, self.prep.preparation_id, self.acks,
                confirm_pause=True, config=self.config, store=self.store,
                runner=registering_runner, lock_reader=self.lock_reader,
            )
        self.assertEqual(self.write_calls, [("hr", "write")])
        journal = self.repo / ".sync-state" / "publish" / self.prep.preparation_id / "result.json"
        data = json.loads(journal.read_text(encoding="utf-8"))
        self.assertEqual(data["overall_status"], "PARTIAL")
        self.assertEqual(data["apps"]["payroll"]["status"], "FAILED")

    def test_registration_just_before_second_import_is_rechecked_under_mutex(self):
        from teamlib.apex import import_app as real_import_app

        def register_before_import(target, *args, **kwargs):
            if target.alias == "payroll":
                self.store.register_app(self.target_payroll, "late-pay-uuid", "host-3", "carol")
            return real_import_app(target, *args, **kwargs)

        with patch("teamlib.publish.import_app", side_effect=register_before_import):
            with self.assertRaises(PublishError):
                publish_prepared(
                    self.repo, self.prep.preparation_id, self.acks,
                    confirm_pause=True, config=self.config, store=self.store,
                    runner=self.fake_runner, lock_reader=self.lock_reader,
                )
        self.assertEqual(self.write_calls, [("hr", "write")])

    def test_unknown_lock_report_refuses_with_zero_writes(self):
        def failing_lock_reader(target, **kwargs):
            return LockReport(target.alias, 101, "UNKNOWN", (), "APEX_APPLICATION_LOCKED_PAGES")

        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.repo,
                self.prep.preparation_id,
                self.acks,
                confirm_pause=True,
                config=self.config,
                store=self.store,
                runner=self.fake_runner,
                lock_reader=failing_lock_reader,
            )
        self.assertIn("unknown", str(ctx.exception).lower())
        self.assertEqual(len(self.write_calls), 0)

    def test_success_both_apps_verified_and_allows_all_clear(self):
        report = publish_prepared(
            self.repo,
            self.prep.preparation_id,
            self.acks,
            confirm_pause=True,
            config=self.config,
            store=self.store,
            runner=self.fake_runner,
            lock_reader=self.lock_reader,
        )
        self.assertIsInstance(report, PublishReport)
        self.assertEqual(report.overall_status, "VERIFIED")
        self.assertTrue(report.all_clear_allowed)
        self.assertEqual(report.app_results["hr"].status, "VERIFIED")
        self.assertEqual(report.app_results["payroll"].status, "VERIFIED")
        self.assertEqual(len(self.write_calls), 2)
        # Check journal file exists
        self.assertTrue(report.journal_path.is_file())

    def test_publish_journal_uses_each_imports_exact_recovery_operation(self):
        unrelated = self.repo / ".sync-state" / "recovery" / "unrelated-old-run" / "result.json"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text(json.dumps({
            "verified": True,
            "source_commit": self.seed_commit,
            "operation_id": "unrelated-old-run",
            "recovery_path": str(unrelated.parent),
        }), encoding="utf-8")
        future = time.time() + 3600
        os.utime(unrelated, (future, future))

        report = publish_prepared(
            self.repo, self.prep.preparation_id, self.acks,
            confirm_pause=True, config=self.config, store=self.store,
            runner=self.fake_runner, lock_reader=self.lock_reader,
        )
        self.assertEqual(report.overall_status, "VERIFIED")
        operation_ids = set()
        for alias in ("hr", "payroll"):
            result = report.app_results[alias]
            self.assertNotEqual(result.operation_id, "unrelated-old-run")
            self.assertIsNotNone(result.operation_id)
            self.assertEqual(result.recovery_path, str(self.repo / ".sync-state" / "recovery" / result.operation_id))
            operation_ids.add(result.operation_id)
        self.assertEqual(len(operation_ids), 2)

    def test_partial_failure_when_second_app_times_out(self):
        self.timeout_on_payroll = True
        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.repo,
                self.prep.preparation_id,
                self.acks,
                confirm_pause=True,
                config=self.config,
                store=self.store,
                runner=self.fake_runner,
                lock_reader=self.lock_reader,
            )
        self.assertIn("payroll", str(ctx.exception).lower())
        # Check result.json was written
        journal = self.repo / ".sync-state" / "publish" / self.prep.preparation_id / "result.json"
        self.assertTrue(journal.is_file())
        data = json.loads(journal.read_text(encoding="utf-8"))
        self.assertEqual(data["overall_status"], "UNKNOWN")
        self.assertFalse(data["all_clear_allowed"])
        self.assertEqual(data["apps"]["hr"]["status"], "VERIFIED")
        self.assertEqual(data["apps"]["payroll"]["status"], "UNKNOWN")
        operation_id = data["apps"]["payroll"]["operation_id"]
        self.assertIsNotNone(operation_id)
        recovery_path = Path(data["apps"]["payroll"]["recovery_path"])
        self.assertEqual(recovery_path.name, operation_id)
        self.assertTrue(recovery_path.is_dir())

    def test_post_import_export_timeout_keeps_unknown_recovery_evidence(self):
        payroll_written = False

        def timeout_during_verification(target, operation, driver, work, **kwargs):
            nonlocal payroll_written
            if target.alias == "payroll" and operation == "read" and payroll_written and Path(driver).name == "export.sql":
                raise SqlclError("SQLcl timed out during post-import export")
            result = self.fake_runner(target, operation, driver, work, **kwargs)
            if target.alias == "payroll" and operation == "write":
                payroll_written = True
            return result

        with self.assertRaises(PublishError):
            publish_prepared(
                self.repo, self.prep.preparation_id, self.acks,
                confirm_pause=True, config=self.config, store=self.store,
                runner=timeout_during_verification, lock_reader=self.lock_reader,
            )
        journal = self.repo / ".sync-state" / "publish" / self.prep.preparation_id / "result.json"
        data = json.loads(journal.read_text(encoding="utf-8"))
        self.assertEqual(data["overall_status"], "UNKNOWN")
        payroll = data["apps"]["payroll"]
        self.assertEqual(payroll["status"], "UNKNOWN")
        self.assertTrue(Path(payroll["recovery_path"]).is_dir())
        self.assertEqual(Path(payroll["recovery_path"]).name, payroll["operation_id"])
        self.assertIsNotNone(self.store.read_app_sync_state(self.target_payroll.physical_key).owner_token)



class RetiredRouteSafetyTests(unittest.TestCase):
    def test_retired_routes_not_dispatched_and_perform_no_sqlcl_writes(self):
        import contextlib
        import io
        from unittest.mock import patch
        import team

        sqlcl_calls = []

        def sqlcl_spy(*args, **kwargs):
            sqlcl_calls.append((args, kwargs))
            return 0

        with patch("teamlib.sqlcl.run_sqlcl", sqlcl_spy):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    team.main(["import-app", "hr"])
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("invalid choice: 'import-app'", stderr.getvalue())

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    team.main(["announce-import", "hr", "--ref", "HEAD"])
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("invalid choice: 'announce-import'", stderr.getvalue())

        self.assertEqual(sqlcl_calls, [], "no SQLcl write should have been initiated")


if __name__ == "__main__":
    unittest.main()
