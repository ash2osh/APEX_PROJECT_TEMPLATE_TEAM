"""Tests for scripts/verify_local.py: offline readiness check and schema-1 report generator.

Ensures default execution is 100% offline and read-only: no SQLcl launches,
no network calls, and no mutations to repository or skills registry.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.verify_local import build_report, main


def _hash_dir(directory: Path) -> dict[str, str]:
    hashes = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            hashes[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


class VerifyLocalTests(unittest.TestCase):
    def test_configured_graphify_with_stale_extractor_is_attention_offline(self):
        from test_graphify_project import GraphifyProjectTests
        helper=GraphifyProjectTests()
        helper.write_mock_env(self.repo/'.venv-graphify')
        self.write_valid_env(); self.create_valid_app()
        from scripts.local_config import read_project_env
        with patch('subprocess.run',side_effect=AssertionError('offline must not execute tooling')):
            report=build_report(self.repo,read_project_env(self.repo/'.env'),self.current_skills)
        check=next(row for row in report['checks'] if row['check']=='graphify')
        self.assertEqual(check['status'],'attention')
        self.assertIn('stale',check['message'])
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.temp_dir, ignore_errors=True))

        self.repo = Path(self.temp_dir) / "repo"
        self.repo.mkdir()

        # Build a valid template manifest
        self.manifest = {
            "schemaVersion": 2,
            "apexRelease": "26.2",
            "minimumSqlclVersion": "26.3.0.0",
            "upstream": "https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM.git",
            "templateOwned": [".env.example", "scripts/**"],
            "projectOwned": [".agents/rules/project.md", "apps/.gitkeep"],
            "templateOnly": []
        }
        (self.repo / "template-manifest.json").write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")
        (self.repo / "apps").mkdir()
        (self.repo / "apps" / ".gitkeep").write_text("", encoding="utf-8")
        (self.repo / ".env.example").write_text("PROJECT_NAME=DEMO\n", encoding="utf-8")

        # Mock current skills status
        self.current_skills = {
            "status": "current",
            "oldestInstalledAt": "2026-10-08T05:13:11.538803622Z",
            "installationCount": 5,
            "issues": []
        }

    def write_valid_lock(self, apex_release: str = "26.2", template_ref: str = "main") -> Path:
        lock = {
            "schemaVersion": 1,
            "templateRef": template_ref,
            "apexRelease": apex_release,
            "upstream": self.manifest["upstream"],
            "commit": "0" * 40,
            "files": {
                ".env.example": hashlib.sha256((self.repo / ".env.example").read_bytes()).hexdigest()
            }
        }
        lock_path = self.repo / ".template-lock.json"
        lock_path.write_text(json.dumps(lock, indent=2), encoding="utf-8")
        return lock_path

    def write_valid_env(self, db_env: str = "development") -> Path:
        env_content = f"""PROJECT_NAME=DEMO
DEVELOPER_NAME=ASHARIF
DB_ENVIRONMENT={db_env}
APEX_APP_ID=100
TABLES_SCHEMA=DEMO
TABLES_PREFIXES=*
TABLES_SQLCL_CONNECTION=DEV
TABLES_EXPECTED_USER=DEMO
CODE_SCHEMA=DEMO
CODE_PREFIXES=*
CODE_SQLCL_CONNECTION=DEV
CODE_EXPECTED_USER=DEMO
APEX_PARSING_SCHEMA=DEMO
APEX_SQLCL_CONNECTION=DEV
APEX_EXPECTED_USER=DEMO
"""
        env_path = self.repo / ".env"
        env_path.write_text(env_content, encoding="utf-8")
        return env_path

    def create_valid_app(self, app_id: int = 100, schema: str = "DEMO") -> Path:
        app_dir = self.repo / "apps" / schema / str(app_id)
        (app_dir / "deployments").mkdir(parents=True)
        (app_dir / "application.apx").write_text(f"app {app_id} {{\n}}\n", encoding="utf-8")
        dev_desc = {
            "workspace": {"name": "DEV_WS"},
            "app": {
                "id": app_id,
                "name": "Demo App",
                "databaseSession": {"parsingSchema": schema}
            }
        }
        (app_dir / "deployments" / "dev.json").write_text(json.dumps(dev_desc, indent=2), encoding="utf-8")
        return app_dir

    def test_default_offline_never_runs_sqlcl_or_network(self) -> None:
        self.write_valid_env()
        self.write_valid_lock()
        self.create_valid_app()

        def forbidden_launcher(*args, **kwargs):
            raise AssertionError("Default offline verify-local must not launch subprocesses!")

        with patch("subprocess.run", side_effect=forbidden_launcher), \
             patch("subprocess.Popen", side_effect=forbidden_launcher), \
             patch("scripts.verify_local.inspect_oracle_skills", return_value=self.current_skills):
            ret = main(["--project-root", str(self.repo), "--format", "json"])
            self.assertEqual(ret, 0)

    def test_valid_empty_template_checkout(self) -> None:
        # No .env, no apps (apps/ only has .gitkeep), no .template-lock.json
        # Reports attention for unconfigured .env, exit code 1
        report = build_report(self.repo, {}, self.current_skills)
        self.assertEqual(report["schemaVersion"], 1)
        self.assertEqual(report["exitCode"], 1)

        # Check statuses
        checks_by_name = {c["check"]: c for c in report["checks"]}
        self.assertEqual(checks_by_name["environment_config"]["status"], "attention")
        self.assertEqual(checks_by_name["template_lock"]["status"], "pass")
        self.assertIn("original template", checks_by_name["template_lock"]["message"])
        self.assertNotIn("apps", [c["status"] for c in report["checks"] if c["check"].startswith("app") and c["status"] == "fail"])


    def test_downstream_missing_lock_fails(self) -> None:
        # Configured downstream project with .env, but missing .template-lock.json
        self.write_valid_env()
        self.create_valid_app()
        from scripts.local_config import read_project_env
        values = read_project_env(self.repo / ".env")

        report = build_report(self.repo, values, self.current_skills)
        self.assertEqual(report["exitCode"], 2)
        checks_by_name = {c["check"]: c for c in report["checks"]}
        self.assertEqual(checks_by_name["template_lock"]["status"], "fail")
        self.assertIn(".template-lock.json is required", checks_by_name["template_lock"]["message"])

    def test_original_template_with_local_config_does_not_need_downstream_lock(self) -> None:
        self.write_valid_env()
        git=self.repo/'.git';git.mkdir(exist_ok=True)
        (git/'config').write_text('[remote "origin"]\nurl = '+self.manifest['upstream']+'\n')
        from scripts.local_config import read_project_env
        report=build_report(self.repo,read_project_env(self.repo/'.env'),self.current_skills)
        check=next(c for c in report['checks'] if c['check']=='template_lock')
        self.assertEqual(check['status'],'pass')
        self.create_valid_app()
        report=build_report(self.repo,read_project_env(self.repo/'.env'),self.current_skills)
        check=next(c for c in report['checks'] if c['check']=='template_lock')
        self.assertEqual(check['status'],'fail')

    def test_wrong_release_or_ref_in_lock_fails(self) -> None:
        self.write_valid_env()
        self.create_valid_app()
        from scripts.local_config import read_project_env
        values = read_project_env(self.repo / ".env")

        # Wrong apexRelease
        self.write_valid_lock(apex_release="26.1")
        report = build_report(self.repo, values, self.current_skills)
        self.assertEqual(report["exitCode"], 2)
        checks_by_name = {c["check"]: c for c in report["checks"]}
        self.assertEqual(checks_by_name["template_lock"]["status"], "fail")
        self.assertIn("apexRelease mismatch", checks_by_name["template_lock"]["message"])

    def test_missing_dev_descriptor_fails(self) -> None:
        self.write_valid_env()
        self.write_valid_lock()
        app_dir = self.create_valid_app()
        (app_dir / "deployments" / "dev.json").unlink()
        from scripts.local_config import read_project_env
        values = read_project_env(self.repo / ".env")

        report = build_report(self.repo, values, self.current_skills)
        self.assertEqual(report["exitCode"], 2)
        app_checks = [c for c in report["checks"] if "dev.json" in c["path"] or "dev.json" in c["message"]]
        self.assertTrue(any(c["status"] == "fail" for c in app_checks))

    def test_optional_staging_prod_absent_in_development(self) -> None:
        self.write_valid_env(db_env="development")
        self.write_valid_lock()
        self.create_valid_app()
        from scripts.local_config import read_project_env
        values = read_project_env(self.repo / ".env")

        report = build_report(self.repo, values, self.current_skills)
        self.assertEqual(report["exitCode"], 0)

    def test_staging_descriptor_required_when_selected(self) -> None:
        self.write_valid_env(db_env="staging")
        self.write_valid_lock()
        self.create_valid_app()
        from scripts.local_config import read_project_env
        values = read_project_env(self.repo / ".env")

        report = build_report(self.repo, values, self.current_skills)
        self.assertEqual(report["exitCode"], 2)
        self.assertTrue(any(c["status"] == "fail" and "staging" in c["message"].lower() for c in report["checks"]))

    def test_malformed_descriptor_fails(self) -> None:
        self.write_valid_env()
        self.write_valid_lock()
        app_dir = self.create_valid_app()
        (app_dir / "deployments" / "dev.json").write_text("{ MALFORMED }", encoding="utf-8")
        from scripts.local_config import read_project_env
        values = read_project_env(self.repo / ".env")

        report = build_report(self.repo, values, self.current_skills)
        self.assertEqual(report["exitCode"], 2)

    def test_symlink_escaping_checkout_fails(self) -> None:
        self.write_valid_env()
        self.write_valid_lock()
        app_dir = self.create_valid_app()
        # Add a symlink inside app source
        outside = Path(self.temp_dir) / "outside"
        outside.mkdir()
        (outside / "evil.sql").write_text("", encoding="utf-8")
        try:
            (app_dir / "symlink_dir").symlink_to(outside)
        except OSError:
            self.skipTest("Symlinks not supported on this filesystem")
        from scripts.local_config import read_project_env
        values = read_project_env(self.repo / ".env")

        report = build_report(self.repo, values, self.current_skills)
        self.assertEqual(report["exitCode"], 2)

    def test_unresolved_versus_released_recovery_evidence(self) -> None:
        self.write_valid_env()
        self.write_valid_lock()
        self.create_valid_app()
        from scripts.local_config import read_project_env
        values = read_project_env(self.repo / ".env")

        rec_dir = self.repo / ".sync-state" / "application-locks" / "100" / "run.test1"
        rec_dir.mkdir(parents=True)
        # Released evidence -> passes, exit 0
        (rec_dir / "recovery.json").write_text(json.dumps({"schemaVersion": 1, "status": "released"}), encoding="utf-8")
        report_released = build_report(self.repo, values, self.current_skills)
        self.assertEqual(report_released["exitCode"], 0)
        rec_checks_rel = [c for c in report_released["checks"] if c["check"] == "recovery_records"]
        self.assertEqual(rec_checks_rel[0]["status"], "pass")
        self.assertNotIn("live lock", rec_checks_rel[0]["message"].lower())

        # Held / unresolved evidence -> reports attention, exit 1
        (rec_dir / "recovery.json").write_text(json.dumps({"schemaVersion": 1, "status": "held"}), encoding="utf-8")
        report_held = build_report(self.repo, values, self.current_skills)
        self.assertEqual(report_held["exitCode"], 1)
        rec_checks = [c for c in report_held["checks"] if c["check"] == "recovery_records"]
        self.assertEqual(rec_checks[0]["status"], "attention")

    def test_upgrade_conflict_copies_report_attention(self) -> None:
        self.write_valid_env()
        self.write_valid_lock()
        self.create_valid_app()
        from scripts.local_config import read_project_env
        values = read_project_env(self.repo / ".env")

        # Conflict copy present
        (self.repo / "scripts" / "foo.py.template-new").parent.mkdir(parents=True, exist_ok=True)
        (self.repo / "scripts" / "foo.py.template-new").write_text("# new template version\n", encoding="utf-8")

        report = build_report(self.repo, values, self.current_skills)
        self.assertEqual(report["exitCode"], 1)
        conflict_checks = [c for c in report["checks"] if c["check"] == "upgrade_conflicts"]
        self.assertEqual(conflict_checks[0]["status"], "attention")

    def test_text_format_output(self) -> None:
        self.write_valid_env()
        self.write_valid_lock()
        self.create_valid_app()
        from io import StringIO
        out = StringIO()
        with patch("sys.stdout", out), \
             patch("scripts.verify_local.inspect_oracle_skills", return_value=self.current_skills):
            ret = main(["--project-root", str(self.repo), "--format", "text"])
            self.assertEqual(ret, 0)
        output = out.getvalue()
        self.assertIn("[PASS] oracle_skills:", output)
        self.assertIn("[PASS] environment_config:", output)
        self.assertIn("Readiness exit code: 0", output)

    def test_live_doctor_scenarios(self) -> None:
        self.write_valid_env()
        self.write_valid_lock()
        self.create_valid_app()

        # Doctor passes
        with patch("scripts.verify_local.run_live_doctor", return_value={"status": "pass", "message": "verified"}), \
             patch("scripts.verify_local.inspect_oracle_skills", return_value=self.current_skills):
            ret = main(["--project-root", str(self.repo), "--live", "--format", "json"])
            self.assertEqual(ret, 0)

        # Doctor unknown -> exit 1 (attention/unavailable)
        with patch("scripts.verify_local.run_live_doctor", return_value={"status": "unavailable", "message": "unknown"}), \
             patch("scripts.verify_local.inspect_oracle_skills", return_value=self.current_skills):
            ret = main(["--project-root", str(self.repo), "--live", "--format", "json"])
            self.assertEqual(ret, 1)

        # Doctor fails -> exit 2 (fail)
        with patch("scripts.verify_local.run_live_doctor", return_value={"status": "fail", "message": "failed"}), \
             patch("scripts.verify_local.inspect_oracle_skills", return_value=self.current_skills):
            ret = main(["--project-root", str(self.repo), "--live", "--format", "json"])
            self.assertEqual(ret, 2)

    def test_live_doctor_missing_sqlcl_reported_as_unavailable(self) -> None:
        from unittest.mock import MagicMock
        from scripts.verify_local import run_live_doctor
        mock_res = MagicMock()
        mock_res.returncode = 127
        mock_res.stdout = ""
        mock_res.stderr = "bash: sql: command not found\n"
        with patch("subprocess.run", return_value=mock_res):
            res = run_live_doctor(self.repo)
            self.assertEqual(res["status"], "unavailable")
            self.assertNotIn("bash:", res["message"])
            self.assertIn("unavailable", res["message"].lower())

    def test_live_doctor_exception_sanitized_without_leaks(self) -> None:
        from scripts.verify_local import run_live_doctor
        with patch("subprocess.run", side_effect=OSError("/private/secret/path/sql: permission denied")):
            res = run_live_doctor(self.repo)
            self.assertEqual(res["status"], "unavailable")
            self.assertNotIn("/private/secret", res["message"])
            self.assertEqual(res["message"], "live doctor invocation unavailable: tool launch failed")

    def test_upgrade_conflicts_ancestor_scratch_does_not_mask_conflicts(self) -> None:
        scratch_ancestor_repo = Path(self.temp_dir) / "scratch_fixture" / "my_repo"
        scratch_ancestor_repo.mkdir(parents=True)
        (scratch_ancestor_repo / "template-manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        (scratch_ancestor_repo / "scripts").mkdir()
        (scratch_ancestor_repo / "scripts" / "tool.py.template-new").write_text("conflict\n", encoding="utf-8")
        (scratch_ancestor_repo / "scratch").mkdir()
        (scratch_ancestor_repo / "scratch" / "ignored.template-new").write_text("ignored\n", encoding="utf-8")

        report = build_report(scratch_ancestor_repo, {}, self.current_skills)
        conflict_checks = [c for c in report["checks"] if c["check"] == "upgrade_conflicts"]
        self.assertEqual(conflict_checks[0]["status"], "attention")
        self.assertIn("tool.py.template-new", conflict_checks[0]["message"])
        self.assertNotIn("ignored.template-new", conflict_checks[0]["message"])

    def test_project_env_file_override_applied(self) -> None:
        self.write_valid_lock()
        self.create_valid_app(100, schema="DEMO")
        custom_env = self.repo / ".env.custom"
        custom_env.write_text("PROJECT_NAME=DEMO\nDEVELOPER_NAME=ASH\nDB_ENVIRONMENT=development\nAPEX_APP_ID=100\nTABLES_SCHEMA=DEMO\nTABLES_PREFIXES=*\nTABLES_SQLCL_CONNECTION=DEV\nTABLES_EXPECTED_USER=DEMO\nCODE_SCHEMA=DEMO\nCODE_PREFIXES=*\nCODE_SQLCL_CONNECTION=DEV\nCODE_EXPECTED_USER=DEMO\nAPEX_PARSING_SCHEMA=DEMO\nAPEX_SQLCL_CONNECTION=DEV\nAPEX_EXPECTED_USER=DEMO\n", encoding="utf-8")
        with patch.dict(os.environ, {"PROJECT_ENV_FILE": ".env.custom"}), \
             patch("scripts.verify_local.inspect_oracle_skills", return_value=self.current_skills):
            from io import StringIO
            out = StringIO()
            with patch("sys.stdout", out):
                ret = main(["--project-root", str(self.repo), "--format", "json"])
            self.assertEqual(ret, 0)
            data = json.loads(out.getvalue())
            env_check = next(c for c in data["checks"] if c["check"] == "environment_config")
            self.assertEqual(env_check["status"], "pass")
            self.assertEqual(env_check["path"], str(custom_env.resolve()))




    def test_malformed_env_via_cli_exits_2_without_leaks(self) -> None:
        secret = "SUPER_SECRET_VALUE_IN_ENV"
        (self.repo / ".env").write_text(f"INVALID LINE {secret}\n", encoding="utf-8")
        from io import StringIO
        out = StringIO()
        with patch("sys.stdout", out), \
             patch("scripts.verify_local.inspect_oracle_skills", return_value=self.current_skills):
            ret = main(["--project-root", str(self.repo), "--format", "text"])
            self.assertEqual(ret, 2)
        output = out.getvalue()
        self.assertNotIn(secret, output)
        self.assertIn("[FAIL] environment_config:", output)


    def test_non_numeric_app_dir_fails(self) -> None:
        self.write_valid_env()
        self.write_valid_lock()
        bad_dir = self.repo / "apps" / "DEMO" / "bad_name"
        bad_dir.mkdir(parents=True)
        report = build_report(self.repo, {"DB_ENVIRONMENT": "development"}, self.current_skills)
        self.assertEqual(report["exitCode"], 2)
        self.assertTrue(any(c["status"] == "fail" and "numeric ID" in c["message"] for c in report["checks"]))

    def test_dev_descriptor_parsing_schema_mismatch_fails(self) -> None:
        self.write_valid_env()
        self.write_valid_lock()
        app_dir = self.create_valid_app(100, schema="DEMO")
        # Change dev.json parsingSchema to OTHER
        dev_desc_path = app_dir / "deployments" / "dev.json"
        dev_desc = json.loads(dev_desc_path.read_text(encoding="utf-8"))
        dev_desc["app"]["databaseSession"]["parsingSchema"] = "OTHER"
        dev_desc_path.write_text(json.dumps(dev_desc, indent=2), encoding="utf-8")

        from scripts.local_config import read_project_env
        values = read_project_env(self.repo / ".env")
        report = build_report(self.repo, values, self.current_skills)
        self.assertEqual(report["exitCode"], 2)
        dev_check = next(c for c in report["checks"] if c["check"] == "app_100_descriptor_dev")
        self.assertEqual(dev_check["status"], "fail")
        self.assertIn("parsingSchema", dev_check["message"])

    def test_missing_application_apx_fails(self) -> None:
        self.write_valid_env()
        self.write_valid_lock()
        app_dir = self.create_valid_app(100, schema="DEMO")
        # Remove application.apx
        (app_dir / "application.apx").unlink()

        from scripts.local_config import read_project_env
        values = read_project_env(self.repo / ".env")
        report = build_report(self.repo, values, self.current_skills)
        self.assertEqual(report["exitCode"], 2)
        app_check = next(c for c in report["checks"] if c["check"] == "app_100_source")
        self.assertEqual(app_check["status"], "fail")
        self.assertIn("application.apx", app_check["message"])


    def test_project_and_registry_files_stay_strictly_unchanged(self) -> None:
        self.write_valid_env()
        self.write_valid_lock()
        self.create_valid_app()

        hashes_before = _hash_dir(self.repo)
        from io import StringIO
        with patch("sys.stdout", StringIO()), \
             patch("scripts.verify_local.inspect_oracle_skills", return_value=self.current_skills):
            ret = main(["--project-root", str(self.repo), "--format", "json"])

            self.assertEqual(ret, 0)

        hashes_after = _hash_dir(self.repo)
        self.assertEqual(hashes_before, hashes_after)

    def test_direct_offline_entry_creates_no_bytecode_or_project_files(self) -> None:
        source_root = Path(__file__).resolve().parents[1]
        shutil.copytree(source_root / "scripts", self.repo / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.pop("PROJECT_ENV_FILE", None)
        before = _hash_dir(self.repo)
        result = subprocess.run([sys.executable, str(self.repo / "scripts/verify_local.py"),
                                 "--repo-root", str(self.repo), "--format", "json"],
                                cwd=self.temp_dir, env=env, capture_output=True, text=True, timeout=20)
        self.assertIn(result.returncode, (1, 2), result.stderr)
        self.assertEqual(json.loads(result.stdout)["schemaVersion"], 1)
        self.assertEqual(_hash_dir(self.repo), before)


if __name__ == "__main__":
    unittest.main()
