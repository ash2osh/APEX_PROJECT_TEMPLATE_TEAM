from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import json
from pathlib import Path
import os
import stat
import tempfile
import unittest

from teamlib.config import Target
from teamlib.sqlcl import SqlclError, run_sqlcl


class SqlclBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-sqlcl-test-")
        self.root = Path(self.temp.name)
        self.work = self.root / "work directory"
        self.work.mkdir()
        self.driver = self.root / "payload driver.sql"
        self.driver.write_text("SELECT 'payload✓' FROM dual;\n", encoding="utf-8")
        fixture = Path(__file__).parent / "fixtures" / "fake_sqlcl.py"
        self.fake = self.root / "fake sqlcl.py"
        self.fake.write_bytes(fixture.read_bytes())
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IXUSR)
        self.log = self.root / "boundary.log"
        self.old_env = dict(os.environ)
        os.environ["FAKE_SQLCL_LOG"] = str(self.log)
        os.environ.pop("FAKE_STARTUP_EXCEPTION", None)
        os.environ.pop("FAKE_ERROR_ZERO", None)
        os.environ.pop("FAKE_SECOND_IDENTITY", None)
        os.environ.pop("FAKE_NO_COMPLETION", None)
        os.environ.pop("FAKE_EXIT_CODE", None)
        os.environ.pop("FAKE_CRLF", None)
        os.environ.pop("FAKE_EXTRA_OUTPUT", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_env)
        self.temp.cleanup()

    def target(self, *, environment: str = "development", connection: str = "docker-demo") -> Target:
        return Target(
            project="team-template",
            role="developer",
            environment=environment,
            connection=connection,
            instance_id="FREE",
            db_name="FREEPDB1",
            service="freep1",
            session_user="DEMO",
            current_schema="DEMO",
            alias="checkout",
            workspace_id=5402650006222933,
            app_id=100,
            parsing_schema="DEMO",
            ownership_mode="shared",
            binding_digest="b" * 64,
        )

    def execute(self, *, target: Target | None = None, operation: str = "read"):
        return run_sqlcl(
            target or self.target(),
            operation,
            self.driver,
            self.work,
            executable=str(self.fake),
        )

    def test_uses_empty_regular_stdin_and_generated_driver(self):
        result = self.execute()
        self.assertEqual(result.identity["SESSION_USER"], "DEMO")
        self.assertEqual(result.completion["operation"], "read")
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("stdin_regular=True", log)
        self.assertIn("identity_count=2", log)
        self.assertEqual(result.log_path.parent, self.work)
        self.assertTrue(result.log_path.is_file())

    def test_verifies_identity_in_payload_session_and_completion(self):
        result = self.execute()
        self.assertEqual(result.result_manifest["status"], "success")
        self.assertIn("TEAM_IDENTITY", result.generated_driver.read_text(encoding="utf-8"))
        self.assertIn("TEAM_COMPLETION|operation=read", result.generated_driver.read_text(encoding="utf-8"))

    def test_refuses_production_write_before_launching_sqlcl(self):
        with self.assertRaises(SqlclError):
            self.execute(target=self.target(environment="production"), operation="write")
        self.assertFalse(self.log.exists())

    def test_wrong_identity_is_refused(self):
        os.environ["FAKE_IDENTITY"] = "OTHER|DEMO|FREEPDB1|freep1|FREE"
        with self.assertRaisesRegex(SqlclError, "SESSION_USER"):
            self.execute()

    def test_identity_change_between_observations_is_refused(self):
        os.environ["FAKE_SECOND_IDENTITY"] = "DEMO|DEMO|FREEPDB1|freep1|OTHER"
        with self.assertRaisesRegex(SqlclError, "changed"):
            self.execute()

    def test_error_zero_with_database_error_is_refused(self):
        os.environ["FAKE_ERROR_ZERO"] = "1"
        with self.assertRaisesRegex(SqlclError, "ORA-"):
            self.execute()

    def test_startup_exception_with_zero_exit_is_refused(self):
        os.environ["FAKE_STARTUP_EXCEPTION"] = "1"
        with self.assertRaisesRegex(SqlclError, "startup exception"):
            self.execute()

    def test_missing_completion_is_refused(self):
        os.environ["FAKE_NO_COMPLETION"] = "1"
        with self.assertRaisesRegex(SqlclError, "completion"):
            self.execute()

    def test_crlf_and_unicode_output_are_parsed(self):
        os.environ["FAKE_CRLF"] = "1"
        os.environ["FAKE_EXTRA_OUTPUT"] = "message✓"
        result = self.execute()
        self.assertEqual(result.identity["INSTANCE_ID"], "FREE")
        self.assertIn("message✓", result.stdout)

    def test_sqlcl_command_diagnostic_with_zero_exit_is_refused(self):
        os.environ["FAKE_EXTRA_OUTPUT"] = "Option not recognized"
        with self.assertRaisesRegex(SqlclError, "Option not recognized"):
            self.execute()

    def test_rejects_shell_metacharacters_in_connection(self):
        with self.assertRaisesRegex(SqlclError, "unsupported"):
            self.execute(target=self.target(connection="docker-demo;DROP"))

    def test_production_read_driver_rejects_mutation(self):
        self.driver.write_text("SELECT 1 FROM dual;\nDROP TABLE x;\n", encoding="utf-8")
        with self.assertRaisesRegex(SqlclError, "read-only"):
            self.execute(target=self.target(environment="production"))
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
