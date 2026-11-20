"""scripts/live_verify.py must never reach a database from a unit test: every run_sql call is mocked here."""

import _test_subprocess_safety  # noqa: F401
import _no_real_sqlcl  # noqa: F401
import contextlib
import importlib
import io
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import live_verify

SCENARIO_NAMES = [
    "check_replacement",
    "sequence",
    "type_owner",
    "fk_order",
    "grants_and_catalog",
    "hidden_columns",
    "invisible",
]


class LiveVerifyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.sqlcl = Path(temporary.name) / "sql"
        # A placeholder file: run_sql is always mocked here, so it is never executed.
        self.sqlcl.write_text("placeholder, never executed\n", encoding="utf-8")

    def guarded_args(self, *, schema="schema1", confirm="REALDB", extra=()):
        return ["--connection", "DEVX", "--schema", schema, "--confirm-target", confirm, "--sqlcl", str(self.sqlcl), *extra]

    def invoke(self, argv, fake=None):
        fake = fake or (lambda text, timeout=180: "")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(live_verify, "run_sql", side_effect=fake) as run, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = live_verify.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), run

    def test_list_prints_the_scenarios_without_any_sql(self):
        code, out, run = self.invoke(["--list"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), SCENARIO_NAMES)
        run.assert_not_called()

    def test_missing_confirm_target_exits_2_before_any_sql(self):
        argv = ["--connection", "DEVX", "--schema", "schema1", "--sqlcl", str(self.sqlcl)]
        code, _, run = self.invoke(argv)
        self.assertEqual(code, 2)
        run.assert_not_called()

    def test_wrong_database_unique_name_is_refused_after_one_read_only_query(self):
        code, _, run = self.invoke(self.guarded_args(confirm="OTHERDB"), fake=lambda text, timeout=180: "SCHEMA1|REALDB\n")
        self.assertEqual(code, 2)
        self.assertEqual(run.call_count, 1)
        text = run.call_args.args[0]
        self.assertIsNone(re.search(r"create|drop", text, re.IGNORECASE))

    def test_wrong_user_is_refused_after_one_read_only_query(self):
        code, _, run = self.invoke(self.guarded_args(), fake=lambda text, timeout=180: "SOMEONE|REALDB\n")
        self.assertEqual(code, 2)
        self.assertEqual(run.call_count, 1)

    def test_matching_guard_runs_only_lvf_objects_and_reports_failures(self):
        calls = []

        def fake(text, timeout=180):
            calls.append(text)
            if "sys_context('USERENV','DB_UNIQUE_NAME')" in text:
                return "SCHEMA1|REALDB\n"
            if "count(*) from user_objects" in text:
                return "0\n"
            return ""

        # Matching identity is case-insensitive; the scenario's own checks fail on these canned outputs.
        code, out, run = self.invoke(self.guarded_args(confirm="realdb", extra=("--scenario", "sequence")), fake=fake)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn("sys_context('USERENV','DB_UNIQUE_NAME')", calls[0])
        patterns = {pattern for text in calls for pattern in re.findall(r"like\s+'([^']*)'", text, re.IGNORECASE)}
        self.assertEqual(patterns, {"LVF\\_%"})
        for text in calls:
            self.assertNotIn("RVW1", text)

    def test_scenario_registry_names_are_unique_and_prefixed(self):
        names = [scenario.__name__ for scenario in live_verify.SCENARIOS]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertTrue(name.startswith("scenario_"), name)

    def test_unknown_scenario_exits_2_before_any_sql(self):
        code, _, run = self.invoke(self.guarded_args(extra=("--scenario", "nosuchthing")))
        self.assertEqual(code, 2)
        run.assert_not_called()

    def test_importing_the_module_does_no_work(self):
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess.run called at import")) as run:
            importlib.reload(live_verify)
        run.assert_not_called()

    def test_run_sql_runs_sqlcl_strips_colour_and_removes_its_script(self):
        seen = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            seen["kwargs"] = kwargs
            seen["script"] = Path(args[-1][1:]).read_text(encoding="utf-8")
            return subprocess.CompletedProcess(args, 0, stdout="\x1b[32mdone\x1b[0m\n", stderr="warn\n")

        with mock.patch.object(live_verify, "CONNECTION", "DEVX"), \
                mock.patch.object(live_verify, "SQLCL", str(self.sqlcl)), \
                mock.patch.object(live_verify.subprocess, "run", side_effect=fake_run):
            output = live_verify.run_sql("select 1 from dual;\n", timeout=5)

        self.assertEqual(output, "done\nwarn\n")
        args = seen["args"]
        self.assertEqual(args[:4], [str(self.sqlcl), "-S", "-name", "DEVX"])
        self.assertTrue(args[4].startswith("@"))
        self.assertFalse(Path(args[4][1:]).exists())
        self.assertIn("whenever sqlerror continue", seen["script"])
        self.assertTrue(seen["script"].rstrip().endswith("exit"))
        self.assertEqual(seen["kwargs"]["timeout"], 5)


if __name__ == "__main__":
    unittest.main()
