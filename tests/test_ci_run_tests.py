import tempfile
import unittest
from pathlib import Path

from scripts.ci_run_tests import failed_test_ids, load_known, main

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


if __name__ == "__main__":
    unittest.main()
