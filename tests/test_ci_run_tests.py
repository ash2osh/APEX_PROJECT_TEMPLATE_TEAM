import contextlib
import io
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from scripts.ci_run_tests import failed_test_ids, load_known, main, ordered_modules, parse_jobs, run_parallel

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

    def test_a_known_flaky_failure_is_rerun_once_and_passes(self):
        code, runner = self.run_main((1, FAIL_ONE), (0, ""))
        self.assertEqual(code, 0)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(runner.calls[1], ["discover", "-s", "tests", "-v", "-k", "test_one"])

    def test_a_known_flaky_failure_that_fails_again_fails_the_job(self):
        code, runner = self.run_main((1, FAIL_ONE), (1, FAIL_ONE))
        self.assertEqual(code, 1)
        self.assertEqual(len(runner.calls), 2)


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
            return (1, "FAIL: test_one (test_mid.C)\n") if modules == ["test_mid"] else (0, "ok\n")
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            code, output = run_parallel(2, module_runner, self.tests)
        self.assertEqual(code, 1)
        self.assertIn("FAIL: test_one (test_mid.C)", output)
        self.assertEqual(output.count("ok"), 3)
        self.assertIn("===== test_mid: exit 1", printed.getvalue())

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
        self.assertEqual(runner.calls, [["discover", "-s", "tests", "-v", "-k", "test_one"]])


if __name__ == "__main__":
    unittest.main()
