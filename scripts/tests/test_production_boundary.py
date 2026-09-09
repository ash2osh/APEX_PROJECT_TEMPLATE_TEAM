from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import team
from teamlib.apex import ApexError, adopt_app, bootstrap_app, export_app, import_app, resolve_export
from teamlib.config import Target
from teamlib.control_store import ControlStore, ControlStoreError
from teamlib.deploy import DeployError, deploy_app
from teamlib.migrate import MigrationRunError, apply_plan
from teamlib.migration_store import MigrationStore, MigrationStoreError
from teamlib.release import ReleaseError, apply_release, build_release, plan_release


class ProductionBoundaryTests(unittest.TestCase):
    def target(self) -> Target:
        return Target(
            project="team", role="integration", environment="production", connection="prod-alias",
            instance_id="PROD", db_name="PRODDB", service="prod-service", session_user="APP",
            current_schema="APP", alias="checkout", workspace_id=1, app_id=2, parsing_schema="APP",
            ownership_mode="shared", binding_digest="a" * 64,
        )

    def test_public_application_writes_refuse_before_runner(self):
        target = self.target()
        calls = []

        def runner(*args, **kwargs):
            calls.append(args)
            raise AssertionError("production runner must not launch")

        with tempfile.TemporaryDirectory(prefix="team-prod-") as directory:
            root = Path(directory)
            store = ControlStore(root / ".sync-state")
            with self.assertRaises(ApexError):
                import_app(target, "HEAD", repo=root, control_store=store, runner=runner)
            with self.assertRaises(ApexError):
                export_app(target, repo=root, control_store=store, runner=runner)
            with self.assertRaises(ApexError):
                bootstrap_app(target, repo=root, control_store=store, runner=runner)
            with self.assertRaises(ApexError):
                resolve_export(target, "recovery", root / "resolved", repo=root, root=root / "state")
        with self.assertRaises(DeployError):
            deploy_app(target, {"application.apx": b"x", ".apex/apexlang.json": b"{}"}, "abc", runner=runner)
        self.assertEqual(calls, [])

    def test_metadata_and_release_writes_refuse_production(self):
        target = self.target()
        with tempfile.TemporaryDirectory(prefix="team-prod-meta-") as directory:
            root = Path(directory)
            with self.assertRaises(ControlStoreError):
                ControlStore(root / "control").setup_state([target])
            with self.assertRaises(MigrationStoreError):
                MigrationStore(root / "migration").bootstrap(target)
            migrations = root / "migrations"
            migrations.mkdir()
            with self.assertRaises(MigrationRunError):
                apply_plan(migrations, {"target": target, "store": MigrationStore(root / "migration2")})

    def test_release_apply_refuses_even_with_a_valid_archive(self):
        with tempfile.TemporaryDirectory(prefix="team-prod-release-") as directory:
            root = Path(directory)
            repo = root / "repo"
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "x@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "x"], check=True)
            (repo / "apps" / "a" / ".apex").mkdir(parents=True)
            (repo / "apps" / "a" / "application.apx").write_bytes(b"x")
            (repo / "apps" / "a" / ".apex" / "apexlang.json").write_bytes(b"{}")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
            commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            manifest = build_release(repo, commit, "1.0.0", root / "out")
            target = {"environment": "production", "role": "production"}
            plan = plan_release(manifest.archive_path, {}, target)
            with self.assertRaises(ReleaseError):
                apply_release(manifest.archive_path, target, plan, history={})

    def test_cli_recovery_cannot_write_a_production_control_store(self):
        env = """\
PROJECT_NAME=team-template
TARGET_ROLE=developer
DB_ENVIRONMENT=production
APEX_APPS=checkout:101
TABLES_SCHEMA=APP_DATA
CODE_SCHEMA=APP_CODE
APEX_PARSING_SCHEMA=APP
METADATA_SCHEMA=APP_META
APP_OWNERSHIP_MODE=shared
APEX_WORKSPACE_ID=5402650006222933
TABLES_SQLCL_CONNECTION=docker-demo
TABLES_EXPECTED_USER=DEMO
TABLES_EXPECTED_CURRENT_SCHEMA=DEMO
TABLES_EXPECTED_DB_NAME=FREEPDB1
TABLES_EXPECTED_SERVICE=freep1
TABLES_EXPECTED_INSTANCE_ID=FREEPDB1
CODE_SQLCL_CONNECTION=docker-demo
CODE_EXPECTED_USER=DEMO
CODE_EXPECTED_CURRENT_SCHEMA=DEMO
CODE_EXPECTED_DB_NAME=FREEPDB1
CODE_EXPECTED_SERVICE=freep1
CODE_EXPECTED_INSTANCE_ID=FREEPDB1
APEX_SQLCL_CONNECTION=docker-demo
APEX_EXPECTED_USER=DEMO
APEX_EXPECTED_CURRENT_SCHEMA=DEMO
APEX_EXPECTED_DB_NAME=FREEPDB1
APEX_EXPECTED_SERVICE=freep1
APEX_EXPECTED_INSTANCE_ID=FREEPDB1
METADATA_SQLCL_CONNECTION=docker-demo
METADATA_EXPECTED_USER=DEMO
METADATA_EXPECTED_CURRENT_SCHEMA=DEMO
METADATA_EXPECTED_DB_NAME=FREEPDB1
METADATA_EXPECTED_SERVICE=freep1
METADATA_EXPECTED_INSTANCE_ID=FREEPDB1
"""
        with tempfile.TemporaryDirectory(prefix="team-prod-recover-") as directory:
            root = Path(directory)
            env_path = root / ".env"
            evidence = root / "worker-terminated.json"
            env_path.write_text(env, encoding="utf-8")
            evidence.write_text("retained capture", encoding="utf-8")
            with patch.object(team.os, "getcwd", return_value=str(root)):
                setup_result = team.main(["--env", str(env_path), "setup-state"])
                register_result = team.main(["--env", str(env_path), "register-app", "checkout"])
                result = team.main([
                    "--env", str(env_path), "recover-app-lock", "checkout",
                    "--run-token", "run-1", "--evidence", str(evidence),
                ])
            self.assertEqual(setup_result, 2)
            self.assertEqual(register_result, 2)
            self.assertEqual(result, 2)
            self.assertFalse((root / ".sync-state").exists())


class ProductionReadAllowlistTests(unittest.TestCase):
    def test_client_and_mutation_constructs_are_refused(self):
        from teamlib.sqlcl import _assert_production_read_only, SqlclError
        for sql in (
            "HOST rm -rf /tmp/x\n",
            "!id\n",
            "SCRIPT var x = 1;\n",
            "@/tmp/other.sql\n",
            "SPOOL /tmp/out.txt\n",
            "CONNECT other/pw@db\n",
            "CALL my_proc();\n",
            "LOCK TABLE t IN EXCLUSIVE MODE;\n",
            "SAVEPOINT s1;\n",
            "SET TRANSACTION READ WRITE;\n",
            "INSERT INTO t VALUES (1);\n",
        ):
            with self.assertRaises(SqlclError, msg=sql):
                _assert_production_read_only(sql)

    def test_genuine_read_only_drivers_are_accepted(self):
        from teamlib.sqlcl import _assert_production_read_only
        for sql in (
            "SELECT 1 FROM dual;\n",
            "SET HEADING OFF\nSET PAGESIZE 0\nSELECT 1 FROM dual;\n",
            "WITH x AS (SELECT 1 a FROM dual) SELECT a FROM x;\n",
            "SELECT q'[don't drop this]' FROM dual;\n",
            "-- a comment\n/* another */\nSELECT 1 FROM dual;\n",
            "SELECT id FROM org START WITH id = 1 CONNECT BY PRIOR id = parent_id;\n",
        ):
            _assert_production_read_only(sql)

    def test_the_shipped_identity_driver_is_accepted(self):
        from teamlib.sqlcl import _assert_production_read_only
        root = Path(__file__).resolve().parents[2]
        _assert_production_read_only((root / "scripts/sql/identity.sql").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
