import contextlib
import io
import os
import subprocess
import _test_subprocess_safety  # noqa: F401
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from scripts.ci_run_tests import failed_test_ids, load_known, main, ordered_modules, parse_jobs, run_parallel, run_unittest

KNOWN_TEXT = "# comment\n\nmod_a.ClassA.test_one  # reason\nmod_b.ClassB.test_two\n"
FAIL_ONE = "FAIL: test_one (mod_a.ClassA)\n----\nFAILED (failures=1)\n"


class Runner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        return self.results.pop(0)


class CiRunTestsTests(unittest.TestCase):
    def setUp(self):
        # CI sets CI_TEST_JOBS=4; without this, main() would start a parallel run of the whole suite from inside it.
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("CI_TEST_JOBS", None)
        self.temporary = tempfile.TemporaryDirectory()
        self.known = Path(self.temporary.name) / "known_flaky.txt"
        self.known.write_text(KNOWN_TEXT, encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def run_main(self, *results):
        runner = Runner(*results)
        return main([], runner=runner, known_path=self.known), runner

    def test_failed_test_ids_reads_fail_error_and_subtest_lines(self):
        output = (
            "FAIL: test_one (mod_a.ClassA)\n"
            "ERROR: test_two (mod_b.ClassB) (shell='bash', scenario='x')\n"
            "test_three (mod_c.ClassC) ... ok\n"
            "FAILED (failures=1, errors=1)\n"
        )
        self.assertEqual(failed_test_ids(output), {"mod_a.ClassA.test_one", "mod_b.ClassB.test_two"})

    def test_failed_test_ids_reads_both_unittest_name_formats(self):
        self.assertEqual(failed_test_ids("FAIL: test_one (mod_a.ClassA)\n"), {"mod_a.ClassA.test_one"})
        self.assertEqual(failed_test_ids("FAIL: test_one (mod_a.ClassA.test_one)\n"), {"mod_a.ClassA.test_one"})

    def test_load_known_ignores_comments_blank_lines_and_reasons(self):
        self.assertEqual(load_known(self.known), {"mod_a.ClassA.test_one", "mod_b.ClassB.test_two"})
        self.assertEqual(load_known(self.known.with_name("missing.txt")), set())

    def test_a_passing_run_is_not_rerun(self):
        code, runner = self.run_main((0, ""))
        self.assertEqual(code, 0)
        self.assertEqual(len(runner.calls), 1)

    def test_an_unlisted_failure_fails_without_a_rerun(self):
        code, runner = self.run_main((1, "FAIL: test_other (mod_z.ClassZ)\n"))
        self.assertEqual(code, 1)
        self.assertEqual(len(runner.calls), 1)

    def test_a_listed_and_an_unlisted_failure_together_fail_without_a_rerun(self):
        code, runner = self.run_main((1, FAIL_ONE + "FAIL: test_other (mod_z.ClassZ)\n"))
        self.assertEqual(code, 1)
        self.assertEqual(len(runner.calls), 1)

    def test_a_crash_with_no_failing_test_keeps_its_exit_code(self):
        code, runner = self.run_main((3, "Traceback ...\n"))
        self.assertEqual(code, 3)
        self.assertEqual(len(runner.calls), 1)

    def test_a_known_failure_with_incomplete_output_is_not_rerun(self):
        code, runner = self.run_main((1, "FAIL: test_one (mod_a.ClassA)\n"), (0, ""))
        self.assertEqual(code, 1)
        self.assertEqual(len(runner.calls), 1)

    def test_a_known_flaky_failure_is_rerun_once_and_passes(self):
        code, runner = self.run_main((1, FAIL_ONE), (0, ""))
        self.assertEqual(code, 0)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(runner.calls[1], ["discover", "-s", "tests", "-p", "mod_a.py", "-v", "-k", "test_one"])

    def test_a_known_flaky_failure_reruns_only_its_own_module(self):
        # A method name is not unique across modules, and -k matches substrings, so the
        # rerun must be restricted to the failing test's module file.
        both = FAIL_ONE + "FAIL: test_one (mod_b.ClassB.test_one)\n----\nFAILED (failures=2)\n"
        known = self.known
        known.write_text("mod_a.ClassA.test_one\nmod_b.ClassB.test_one\n", encoding="utf-8")
        code, runner = self.run_main((1, both), (0, ""), (0, ""))
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls[1:], [
            ["discover", "-s", "tests", "-p", "mod_a.py", "-v", "-k", "test_one"],
            ["discover", "-s", "tests", "-p", "mod_b.py", "-v", "-k", "test_one"],
        ])

    def test_a_failed_rerun_in_any_module_fails_the_job(self):
        both = FAIL_ONE + "FAIL: test_one (mod_b.ClassB.test_one)\n----\nFAILED (failures=2)\n"
        self.known.write_text("mod_a.ClassA.test_one\nmod_b.ClassB.test_one\n", encoding="utf-8")
        code, runner = self.run_main((1, both), (0, ""), (1, ""))
        self.assertEqual(code, 1)
        self.assertEqual(len(runner.calls), 3)

    def test_a_known_flaky_failure_that_fails_again_fails_the_job(self):
        code, runner = self.run_main((1, FAIL_ONE), (1, FAIL_ONE))
        self.assertEqual(code, 1)
        self.assertEqual(len(runner.calls), 2)

    def test_serial_output_is_escaped_for_a_restricted_console_encoding(self):
        captured = io.BytesIO()
        console = io.TextIOWrapper(captured, encoding="cp1252", errors="strict")
        with patch("scripts.ci_run_tests.subprocess.run", return_value=SimpleNamespace(
            returncode=0, stdout="captured: \ufffd\n",
        )):
            try:
                with contextlib.redirect_stdout(console):
                    code, output = run_unittest(["-v"])
                    console.flush()
            except UnicodeEncodeError as error:
                self.fail(f"serial runner leaked a console encoding error: {error}")

        self.assertEqual(code, 0)
        self.assertIn("captured: \ufffd", output)
        self.assertIn(b"captured: \\ufffd", captured.getvalue())


class ParallelRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.tests = Path(self.temporary.name)
        for name, size in (("test_big", 900), ("test_mid", 500), ("test_small", 400), ("test_tiny", 100)):
            (self.tests / f"{name}.py").write_text("#" * size, encoding="utf-8")
        (self.tests / "helper.py").write_text("x", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_modules_run_largest_first_and_helpers_are_left_out(self):
        self.assertEqual(ordered_modules(self.tests), ["test_big", "test_mid", "test_small", "test_tiny"])

    def test_jobs_come_from_the_option_then_the_variable_then_default_one(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(parse_jobs([]), (1, []))
            self.assertEqual(parse_jobs(["--jobs", "3", "-k", "x"]), (3, ["-k", "x"]))
            self.assertEqual(parse_jobs(["--jobs=2"]), (2, []))
        with patch.dict(os.environ, {"CI_TEST_JOBS": "4"}):
            self.assertEqual(parse_jobs([]), (4, []))
        for bad in (["--jobs", "0"], ["--jobs", "x"], ["--jobs"]):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                parse_jobs(bad)

    def test_parallel_run_fails_when_any_module_fails_and_keeps_all_output(self):
        def module_runner(modules):
            return (
                1,
                "FAIL: test_one (test_mid.C)\nRan 2 tests in 0.1s\nFAILED (failures=1)\n",
            ) if modules == ["test_mid"] else (
                0,
                "ok\nRan 3 tests in 0.1s\nOK (skipped=1)\n",
            )
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code, output = run_parallel(2, module_runner, self.tests)
        self.assertEqual(code, 1)
        self.assertIn("FAIL: test_one (test_mid.C)", output)
        self.assertEqual(output.count("ok"), 3)
        self.assertIn("===== test_mid: exit 1", printed.getvalue())
        self.assertIn("===== Combined: Ran 11 tests; skipped=3; failures=1; errors=0", printed.getvalue())

    def test_parallel_output_is_escaped_for_a_restricted_console_encoding(self):
        captured = io.BytesIO()
        console = io.TextIOWrapper(captured, encoding="cp1252", errors="strict")
        try:
            with contextlib.redirect_stdout(console):
                code, output = run_parallel(1, lambda _modules: (0, "captured: \ufffd\n"), self.tests)
                console.flush()
        except UnicodeEncodeError as error:
            self.fail(f"parallel runner leaked a console encoding error: {error}")

        self.assertEqual(code, 0)
        self.assertIn("captured: \ufffd", output)
        self.assertIn(b"captured: \\ufffd", captured.getvalue())

    def test_main_runs_in_parallel_only_for_a_whole_run_with_more_than_one_job(self):
        calls = []
        def parallel(jobs):
            calls.append(jobs)
            return 0, ""
        runner = Runner((0, ""))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(main(["--jobs", "3"], runner=runner, parallel=parallel), 0)
            self.assertEqual(calls, [3])
            self.assertEqual(runner.calls, [])
            self.assertEqual(main(["--jobs", "3", "-k", "x"], runner=runner, parallel=parallel), 0)
            self.assertEqual(runner.calls, [["discover", "-s", "tests", "-v", "-k", "x"]])

    def test_a_known_flaky_failure_in_a_parallel_run_is_rerun_once(self):
        known = self.tests / "known.txt"
        known.write_text("mod_a.ClassA.test_one\n", encoding="utf-8")
        runner = Runner((0, ""))
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--jobs", "2"], runner=runner, known_path=known, parallel=lambda jobs: (1, FAIL_ONE))
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls, [["discover", "-s", "tests", "-p", "mod_a.py", "-v", "-k", "test_one"]])


class CrashedProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tests = Path(self.temporary.name)
        self.known = self.tests / "known.txt"
        self.known.write_text("test_flaky.C.test_retry\n", encoding="utf-8")
        self.retry_calls = []

    def write_flaky(self, *, crash_at_exit=False):
        crash = "   atexit.register(lambda: os._exit(7))\n" if crash_at_exit else ""
        (self.tests / "test_flaky.py").write_text(
            "import atexit, os, unittest\nfrom pathlib import Path\n"
            "class C(unittest.TestCase):\n def test_retry(self):\n"
            "  marker = Path(__file__).with_suffix('.marker')\n"
            "  if not marker.exists():\n   marker.touch()\n" + crash
            + "   self.fail('known first-run failure')\n", encoding="utf-8")

    def run_unittest(self, args):
        result = subprocess.run([sys.executable, "-m", "unittest", *args],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8")
        return result.returncode, result.stdout

    def runner(self, args):
        self.retry_calls.append(list(args))
        args = list(args)
        args[args.index("-s") + 1] = str(self.tests)
        return self.run_unittest(args)

    def test_unrelated_parallel_crash_is_not_hidden_by_a_known_failure(self):
        self.write_flaky()
        (self.tests / "test_crash.py").write_text(
            "import os, unittest\nclass C(unittest.TestCase):\n"
            " def test_unrelated(self):\n  os._exit(7)\n", encoding="utf-8")

        def module_runner(names):
            return self.run_unittest(["discover", "-s", str(self.tests), "-p", names[0] + ".py", "-v"])

        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--jobs", "2"], runner=self.runner, known_path=self.known,
                        parallel=lambda jobs: run_parallel(jobs, module_runner, self.tests))
        self.assertEqual(code, 7)
        self.assertEqual(self.retry_calls, [])

    def test_serial_crash_after_a_known_failure_is_not_rerun(self):
        self.write_flaky(crash_at_exit=True)
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--jobs", "1"], runner=self.runner, known_path=self.known)
        self.assertEqual(code, 7)
        self.assertEqual(len(self.retry_calls), 1)

    def test_incomplete_parallel_failure_is_not_hidden_by_a_known_failure(self):
        self.write_flaky()
        (self.tests / "test_incomplete.py").write_text("# no completed unittest result\n", encoding="utf-8")

        def module_runner(names):
            if names == ["test_incomplete"]:
                return 1, "FAIL: test_retry (test_flaky.C)\n"
            return self.run_unittest(["discover", "-s", str(self.tests), "-p", names[0] + ".py", "-v"])

        with contextlib.redirect_stdout(io.StringIO()):
            code = main(["--jobs", "2"], runner=self.runner, known_path=self.known,
                        parallel=lambda jobs: run_parallel(jobs, module_runner, self.tests))
        self.assertNotEqual(code, 0)
        self.assertEqual(self.retry_calls, [])


class JobsFromEnvironmentTests(unittest.TestCase):
    def test_tests_in_a_parallel_ci_run_never_start_another_parallel_run(self):
        calls = []
        runner = Runner((0, ""))
        with patch.dict(os.environ, {"CI_TEST_JOBS": "4"}):
            code = main([], runner=runner, parallel=lambda jobs: calls.append(jobs) or (0, "x"))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [4])  # the variable alone selects parallel; the child guard is below

    def test_child_processes_get_one_job(self):
        seen = {}
        class Done:
            returncode, stdout = 0, ""
        def fake_run(*args, **kwargs):
            seen.update(kwargs["env"])
            seen["stdin"] = kwargs["stdin"]
            seen["timeout"] = kwargs["timeout"]
            return Done()
        with patch.dict(os.environ, {"CI_TEST_JOBS": "4"}), patch("scripts.ci_run_tests.subprocess.run", fake_run):
            from scripts.ci_run_tests import run_modules
            run_modules(["test_x"])
        self.assertEqual(seen["CI_TEST_JOBS"], "1")
        self.assertIs(seen["stdin"], subprocess.DEVNULL)
        self.assertEqual(seen["timeout"], 900)

    def test_each_child_loads_the_guard_that_discovery_gives_every_module(self):
        commands = []
        class Done:
            returncode, stdout = 0, ""
        with patch("scripts.ci_run_tests.subprocess.run", lambda command, **kwargs: commands.append(command) or Done()):
            from scripts.ci_run_tests import run_modules
            run_modules(["test_x", "test_y"])
        self.assertIn("import _no_real_sqlcl", commands[0][2])
        self.assertEqual(commands[0][3:], ["-v", "test_x", "test_y"])


class UnittestRunnerSafetyTests(unittest.TestCase):
    def test_run_unittest_closes_stdin_and_bounds_the_suite(self):
        seen = {}

        class Completed:
            returncode, stdout = 0, "suite output\n"

        def fake_run(command, **kwargs):
            seen.update(kwargs)
            return Completed()

        with patch("scripts.ci_run_tests.subprocess.Popen", side_effect=AssertionError("Popen must not be used")), \
             patch("scripts.ci_run_tests.subprocess.run", fake_run), \
             contextlib.redirect_stdout(io.StringIO()):
            code, output = run_unittest(["discover", "-s", "tests"])

        self.assertEqual(code, 0)
        self.assertEqual(output, "suite output\n")
        self.assertIs(seen["stdin"], subprocess.DEVNULL)
        self.assertEqual(seen["timeout"], 900)

    def test_run_unittest_returns_a_bounded_timeout_with_partial_output(self):
        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=b"partial output\n")

        with patch("scripts.ci_run_tests.subprocess.Popen", side_effect=AssertionError("Popen must not be used")), \
             patch("scripts.ci_run_tests.subprocess.run", fake_run), \
             contextlib.redirect_stdout(io.StringIO()):
            code, output = run_unittest(["discover", "-s", "tests"])

        self.assertEqual(code, 124)
        self.assertIn("partial output", output)
        self.assertIn("unittest timed out after 900 seconds", output)


if __name__ == "__main__":
    unittest.main()
