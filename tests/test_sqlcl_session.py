import os
import signal
import subprocess
import _test_subprocess_safety  # noqa: F401
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fake_sqlcl import environment as fake_environment, install
from scripts.db_targets import Target
from scripts import sqlcl_session, windows_job
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

    def test_termination_of_caller_group_stops_bounded_sqlcl_descendants(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment, driver, _ = self.make_launcher(root)
            program = root / "caller.py"
            program.write_text(
                "import sys\nfrom pathlib import Path\n"
                "from scripts.db_targets import Target\nfrom scripts.sqlcl_session import run_sqlcl\n"
                "p=Path(sys.argv[1])\n"
                "run_sqlcl(Target('dev','fake-dev','DEMO','DEMO','test'),p,p.parent,"
                "timeout_seconds=30,output_limit_bytes=64)\n",
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
                self.assertFalse((root / "late-write").exists(), "bounded SQLcl survived termination of its caller")
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
                binary = install(root / "bin", 'printf "FAKE_SQLCL_STARTED\\n"\nsleep 10\n')
                selected_timeout = 5 if os.name == "nt" else 0.5
                values = {
                    # Leave enough time for a loaded CI worker to start Bash and
                    # emit the banner before the timeout; phase selection is the
                    # assertion here, not a 50 ms process-start benchmark.
                    "MIGRATION_APPLY_TIMEOUT_SECONDS": str(selected_timeout) if setting.endswith("APPLY_TIMEOUT_SECONDS") else "8",
                    "MIGRATION_CHECK_TIMEOUT_SECONDS": str(selected_timeout) if setting.endswith("CHECK_TIMEOUT_SECONDS") else "8",
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
                self.assertEqual(caught.timeout_seconds, selected_timeout)
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


class SqlclBoundedOutputTests(unittest.TestCase):
    def test_posix_deadline_applies_after_child_closes_output(self):
        if os.name == "nt":
            self.skipTest("POSIX process-group implementation")
        child = "import os,time; os.close(1); os.close(2); time.sleep(0.8)"
        started = time.monotonic()

        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(subprocess.TimeoutExpired):
            sqlcl_session._run_bridge_bounded_posix(
                [sys.executable, "-c", child],
                cwd=Path(temporary),
                env=os.environ,
                timeout_seconds=0.1,
                output_limit_bytes=128,
            )

        self.assertLess(time.monotonic() - started, 0.6)

    def test_windows_algorithm_deadline_applies_after_child_closes_output(self):
        child = "import os,time; os.close(1); os.close(2); time.sleep(0.8)"

        def stop_process(process, _job):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)

        started = time.monotonic()
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(windows_job, "_Job", side_effect=OSError("unavailable"), create=True), \
                patch.object(windows_job, "_terminate_tree", side_effect=stop_process), \
                self.assertRaises(subprocess.TimeoutExpired):
            windows_job._run_bounded(
                [sys.executable, "-c", child],
                input_text="",
                timeout=0.1,
                output_limit_bytes=128,
                text=True,
                cwd=temporary,
                env=os.environ,
            )

        self.assertLess(time.monotonic() - started, 0.6)

    def test_windows_algorithm_refuses_overflow_discovered_after_process_exit(self):
        real_read = os.read

        def delayed_read(descriptor, size):
            time.sleep(0.2)
            return real_read(descriptor, size)

        def stop_process(process, _job):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(windows_job, "_Job", side_effect=OSError("unavailable"), create=True), \
                patch.object(windows_job.os, "read", side_effect=delayed_read), \
                patch.object(windows_job, "_terminate_tree", side_effect=stop_process), \
                self.assertRaises(windows_job.OutputLimitExceeded):
            windows_job._run_bounded(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x'*100); sys.stdout.flush()"],
                input_text="",
                timeout=2,
                output_limit_bytes=64,
                text=True,
                cwd=temporary,
                env=os.environ,
            )

    def test_output_cap_stops_the_process_tree_and_preserves_only_capped_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / "child.py"
            child.write_text(
                "import sys,time\nfrom pathlib import Path\n"
                "root=Path(sys.argv[1])\n"
                "deadline=time.monotonic()+5\n"
                "while time.monotonic()<deadline:\n"
                " if (root/'continue').exists():\n"
                "  (root/'late-write').touch();break\n"
                " time.sleep(.01)\n",
                encoding="utf-8",
            )
            launcher = root / "launcher.py"
            launcher.write_text(
                "import subprocess,sys,time\nfrom pathlib import Path\n"
                "root=Path(sys.argv[1])\n"
                "subprocess.Popen([sys.executable,sys.argv[2],sys.argv[1]],"
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
                "(root/'ready').touch()\n"
                "sys.stdout.write('x'*1000000);sys.stdout.flush()\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            binary = install(
                root / "bin",
                f'exec "{Path(sys.executable).as_posix()}" "{launcher.as_posix()}" "{root.as_posix()}" "{child.as_posix()}"\n',
            )
            environment = fake_environment(binary)
            run = root / "run"
            run.mkdir()
            driver = run / "driver.sql"
            driver.write_text("EXIT SUCCESS ROLLBACK\n", encoding="utf-8")
            target = Target("dev", "fake-dev", "DEMO", "DEMO", "test")

            with self.assertRaisesRegex(SqlclError, "output exceeded"):
                run_sqlcl(target, driver, run, environment=environment, output_limit_bytes=64)

            log = run / "sqlcl-output.log"
            self.assertLessEqual(log.stat().st_size, 64)
            self.assertTrue((root / "ready").exists(), "SQLcl launcher did not start")
            (root / "continue").touch()
            time.sleep(.3)
            self.assertFalse((root / "late-write").exists(), "SQLcl descendant continued after output cap")

    def test_output_capture_remains_unbounded_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = "complete-output-" + "x" * 32768
            binary = install(root / "bin", f"printf '%s\\n' '{payload}'\n")
            environment = fake_environment(binary)
            run = root / "run"
            run.mkdir()
            driver = run / "driver.sql"
            driver.write_text("EXIT SUCCESS ROLLBACK\n", encoding="utf-8")

            result = run_sqlcl(
                Target("dev", "fake-dev", "DEMO", "DEMO", "test"),
                driver,
                run,
                environment=environment,
            )

            self.assertIn(payload, result.output)
            self.assertIn(payload, (run / "sqlcl-output.log").read_text(encoding="utf-8"))

    def test_invalid_output_cap_refuses_before_starting_sqlcl(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "run"
            run.mkdir()
            driver = run / "driver.sql"
            driver.write_text("EXIT SUCCESS ROLLBACK\n", encoding="utf-8")
            started = root / "started"
            binary = install(root / "bin", f'touch "{started}"\n')

            with self.assertRaisesRegex(SqlclError, "output limit"):
                run_sqlcl(
                    Target("dev", "fake-dev", "DEMO", "DEMO", "test"),
                    driver,
                    run,
                    environment=fake_environment(binary),
                    output_limit_bytes=0,
                )

            self.assertFalse(started.exists())


if __name__ == "__main__":
    unittest.main()
