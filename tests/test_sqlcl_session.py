import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fake_sqlcl import environment as fake_environment, install
from scripts.db_targets import Target
from scripts.sqlcl_session import SqlclError, run_sqlcl


@unittest.skipIf(os.name == "nt", "POSIX process-group cleanup")
class SqlclProcessTreeTests(unittest.TestCase):
    def make_launcher(self, root):
        child = root / "child.py"
        child.write_text(
            "import sys,time\nfrom pathlib import Path\n"
            "root=Path(sys.argv[1])\n(root/'ready').touch()\n"
            "deadline=time.monotonic()+5\n"
            "while time.monotonic()<deadline:\n"
            " if (root/'continue').exists():\n"
            "  (root/'late-write').touch();break\n"
            " time.sleep(.01)\n",
            encoding="utf-8",
        )
        launcher = root / "launcher.py"
        launcher.write_text(
            "import os,subprocess,sys,time\nfrom pathlib import Path\n"
            "(Path(sys.argv[2])/'group').write_text(str(os.getpgrp()))\n"
            "subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]],"
            "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
            "print('SQLCL_STARTED',flush=True)\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        binary = install(root / "bin", f'exec "{Path(sys.executable).as_posix()}" "{launcher.as_posix()}" "{child.as_posix()}" "{root.as_posix()}"\n')
        environment = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"]}
        run = root / "run"
        run.mkdir()
        driver = run / "driver.sql"
        driver.write_text("EXIT SUCCESS ROLLBACK\n", encoding="utf-8")
        target = Target("dev", "fake-dev", "DEMO", "DEMO", "test")
        return environment, driver, target

    def test_timeout_stops_descendants_before_returning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, driver, target = self.make_launcher(root)
            run = driver.parent
            with self.assertRaisesRegex(SqlclError, "timed out"):
                run_sqlcl(target, driver, run, environment=environment, timeout_seconds=1)
            self.assertTrue((root / "ready").exists(), "descendant never started")
            (root / "continue").touch()
            time.sleep(.3)
            self.assertFalse((root / "late-write").exists(), "SQLcl descendant continued after timeout")
            self.assertIn("SQLCL_STARTED", (run / "sqlcl-output.log").read_text())

    def test_termination_of_caller_group_stops_isolated_sqlcl_descendants(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, driver, _ = self.make_launcher(root)
            program = root / "caller.py"
            program.write_text(
                "import sys\nfrom pathlib import Path\n"
                "from scripts.db_targets import Target\nfrom scripts.sqlcl_session import run_sqlcl\n"
                "p=Path(sys.argv[1])\n"
                "run_sqlcl(Target('dev','fake-dev','DEMO','DEMO','test'),p,p.parent,timeout_seconds=30)\n",
                encoding="utf-8",
            )
            repo = Path(__file__).resolve().parents[1]
            environment["PYTHONPATH"] = str(repo)
            process = subprocess.Popen([sys.executable, str(program), str(driver)], cwd=repo,
                                       env=environment, start_new_session=True,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 5
                while not (root / "ready").exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((root / "ready").exists(), "descendant never started")
                os.killpg(process.pid, signal.SIGTERM)
                self.assertEqual(process.wait(timeout=3), -signal.SIGTERM)
                (root / "continue").touch()
                time.sleep(.3)
                self.assertFalse((root / "late-write").exists(), "SQLcl survived termination of its caller")
            finally:
                for group in (process.pid, int((root / "group").read_text()) if (root / "group").exists() else process.pid):
                    self.assertNotEqual(group, os.getpgrp())
                    try:
                        os.killpg(group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait(timeout=3)


class SqlclTimeoutConfigurationTests(unittest.TestCase):
    def test_phase_selects_the_configured_apply_or_check_timeout(self):
        target = Target("dev", "fake-dev", "DEMO", "DEMO", "test")
        for phase, setting in (
            ("apply", "MIGRATION_APPLY_TIMEOUT_SECONDS"),
            ("preconditions", "MIGRATION_CHECK_TIMEOUT_SECONDS"),
            ("postconditions", "MIGRATION_CHECK_TIMEOUT_SECONDS"),
            ("inventory", "MIGRATION_CHECK_TIMEOUT_SECONDS"),
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                run = root / "run"
                run.mkdir()
                driver = run / "driver.sql"
                driver.write_text("EXIT SUCCESS ROLLBACK\n", encoding="utf-8")
                binary = install(root / "bin", 'printf "FAKE_SQLCL_STARTED\\n"\nsleep 5\n')
                values = {
                    "MIGRATION_APPLY_TIMEOUT_SECONDS": "0.05" if setting.endswith("APPLY_TIMEOUT_SECONDS") else "2",
                    "MIGRATION_CHECK_TIMEOUT_SECONDS": "0.05" if setting.endswith("CHECK_TIMEOUT_SECONDS") else "2",
                }
                environment = fake_environment(binary, **values)

                try:
                    run_sqlcl(target, driver, run, environment=environment, phase=phase)
                except Exception as error:
                    caught = error
                else:
                    self.fail("SQLcl should have stopped at the configured phase timeout")

                self.assertEqual(type(caught).__name__, "SqlclTimeout")
                self.assertEqual(caught.phase, phase)
                self.assertEqual(caught.timeout_seconds, 0.05)
                self.assertGreater(caught.elapsed_seconds, 0)
                self.assertIn("FAKE_SQLCL_STARTED", (run / "sqlcl-output.log").read_text(encoding="utf-8"))

    def test_nonpositive_timeout_configuration_refuses_before_starting_sqlcl(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "run"
            run.mkdir()
            driver = run / "driver.sql"
            driver.write_text("EXIT SUCCESS ROLLBACK\n", encoding="utf-8")
            started = root / "started"
            binary = install(root / "bin", f'touch "{started}"\n')
            environment = fake_environment(binary, MIGRATION_CHECK_TIMEOUT_SECONDS="0")

            try:
                run_sqlcl(Target("dev", "fake-dev", "DEMO", "DEMO", "test"), driver, run,
                          environment=environment, phase="preconditions")
            except Exception as error:
                self.assertIsInstance(error, SqlclError)
                self.assertIn("MIGRATION_CHECK_TIMEOUT_SECONDS", str(error))
            else:
                self.fail("a zero SQLcl timeout must be refused")

            self.assertFalse(started.exists())


if __name__ == "__main__":
    unittest.main()
