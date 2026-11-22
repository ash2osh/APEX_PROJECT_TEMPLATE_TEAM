"""Persistent SQLcl sessions (scripts/sqlcl_pool.py) against a fake SQLcl that follows the slot protocol."""
from __future__ import annotations

import _test_subprocess_safety  # noqa: F401
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

import fake_sqlcl_pool
from scripts import sqlcl_pool, sqlcl_session
from scripts.db_targets import Target
from scripts.sqlcl_session import SqlclCancelled, SqlclError, SqlclTimeout, run_sqlcl


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:  # a killed child of a dead parent may linger as a zombie until init reaps it
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().split(") ", 1)[1][:1] != "Z"
    except OSError:
        return True


@unittest.skipIf(os.name == "nt", "persistent sessions use POSIX named pipes; Windows runs one SQLcl per script")
class SqlclPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.log = self.root / "fake-events.jsonl"
        self.bin = fake_sqlcl_pool.install(self.root / "bin")
        self.environment = fake_sqlcl_pool.environment(self.bin, FAKE_POOL_LOG=str(self.log))
        self.target = Target("staging", "stage-conn", "APP", "APP", "service")
        self.count = 0

    def tearDown(self) -> None:
        pool = sqlcl_session.active_pool()
        if pool is not None:
            pool.close()
        sqlcl_session._set_active_pool(None)
        self.temporary.cleanup()

    def _driver(self, body: str, *, commit: bool = False, pooled: bool = True) -> tuple[Path, Path]:
        self.count += 1
        run_dir = self.root / "runs" / f"run-{self.count:03d}"
        run_dir.mkdir(parents=True, mode=0o700)
        ending = sqlcl_session.driver_epilogue(commit) if pooled else ("EXIT SUCCESS COMMIT" if commit else "EXIT SUCCESS ROLLBACK")
        driver = run_dir / "driver.sql"
        driver.write_text(body + "\n" + ending + "\n", encoding="utf-8")
        return driver, run_dir

    def _run(self, body: str, *, phase: str | None = None, timeout: float = 30, **kwargs):
        driver, run_dir = self._driver(body, pooled=kwargs.pop("pooled", True), commit=kwargs.pop("commit", False))
        result = run_sqlcl(self.target, driver, run_dir, environment=self.environment, phase=phase, timeout_seconds=timeout, **kwargs)
        return result, run_dir

    def _state(self, output: str) -> dict:
        line = next(line for line in output.splitlines() if line.startswith("STATE "))
        return json.loads(line[len("STATE "):])

    def _events(self, kind: str) -> list[dict]:
        return [event for event in fake_sqlcl_pool.events(self.log) if event.get("event") == kind]

    def _pool(self, **kwargs) -> sqlcl_pool.SessionPool:
        pool = sqlcl_pool.SessionPool(**kwargs)
        sqlcl_session._set_active_pool(pool)
        return pool

    def test_segments_are_framed_by_sentinel(self) -> None:
        pool = self._pool()
        first, _ = self._run("PROMPT hello")
        second, _ = self._run("PROMPT world")
        self.assertEqual(["hello"], [line for line in first.output.splitlines() if line.strip()])
        self.assertEqual(["world"], [line for line in second.output.splitlines() if line.strip()])
        self.assertEqual(1, pool.stats()["processesStarted"])
        self.assertEqual(1, len(self._events("start")), "one SQLcl process served both scripts")

    def test_loop_file_rollover_keeps_order_and_retires_before_nesting_limit(self) -> None:
        pool = self._pool(batch_size=5)
        for number in range(40):
            result, _ = self._run(f"PROMPT line {number}")
            self.assertIn(f"line {number}", result.output)
        self.assertGreaterEqual(pool.stats()["processesStarted"], 2, "a process is retired before 20 nested scripts")
        self.assertFalse(any("SP2-0309" in event.get("path", "") for event in fake_sqlcl_pool.events(self.log)))

    def test_sql_error_ends_process_and_next_call_restarts(self) -> None:
        pool = self._pool()
        with self.assertRaisesRegex(SqlclError, "SQLcl exited with status 2"):
            self._run("WHENEVER SQLERROR EXIT FAILURE ROLLBACK\nFAKE_SQL_ERROR;\nPROMPT not reached")
        result, _ = self._run("PROMPT after")
        self.assertIn("after", result.output)
        self.assertEqual(2, pool.stats()["processesStarted"])

    def test_timeout_kills_process_group(self) -> None:
        self._pool()
        with self.assertRaises(SqlclTimeout):
            self._run("FAKE_SLEEP 20;", timeout=2)
        pids = {event["pid"] for event in self._events("start")}
        deadline = time.monotonic() + 10
        while pids and time.monotonic() < deadline:
            pids = {pid for pid in pids if _alive(pid)}
            time.sleep(0.1)
        self.assertEqual(set(), pids, "the timed-out SQLcl process tree was killed")

    def test_cancel_event_kills_process(self) -> None:
        self._pool()
        event = threading.Event()
        threading.Timer(0.5, event.set).start()
        with self.assertRaises(SqlclCancelled):
            self._run("FAKE_SLEEP 20;", cancel_event=event)

    def test_output_limit_applies_to_segment(self) -> None:
        self._pool()
        with self.assertRaisesRegex(SqlclError, "byte limit"):
            self._run("\n".join(f"PROMPT {'x' * 50}" for _ in range(40)), output_limit_bytes=200)

    def test_reset_between_scripts_clears_session_and_client_state(self) -> None:
        self._pool()
        self._run("DEFINE leftover = 1\nSET HEADING OFF\nFAKE_OPEN_TXN;\nFAKE_SET_PKG;\nALTER SESSION SET CURRENT_SCHEMA = OTHER;")
        result, _ = self._run("FAKE_SHOW_STATE;")
        state = self._state(result.output)
        self.assertFalse(state["txn"])
        self.assertFalse(state["pkg"])
        self.assertIsNone(state["schema"])
        self.assertEqual({}, state["sets"])
        self.assertNotIn("LEFTOVER", state["defines"])
        self.assertEqual(1, len(self._events("start")), "the session was reused, not reconnected")
        self.assertEqual(1, len(self._events("reset")))

    def test_failed_reset_replaces_process_before_script(self) -> None:
        self.environment["FAKE_RESET_FAIL"] = "1"
        pool = self._pool()
        self._run("PROMPT first")
        result, run_dir = self._run("PROMPT second")
        self.assertIn("second", result.output)
        self.assertEqual(2, pool.stats()["processesStarted"])
        second_runs = [event for event in self._events("script") if event["path"].endswith(f"{run_dir.name}/driver.sql")]
        self.assertEqual(1, len(second_runs), "the script ran exactly once, on the new process")

    def test_lost_session_replaced_before_script_sent(self) -> None:
        self._pool()
        self._run("PROMPT first")
        for event in self._events("start"):
            os.kill(event["pid"], sqlcl_pool._SIGKILL)  # the database session is gone; SQLcl ends
        time.sleep(0.2)
        result, run_dir = self._run("PROMPT second")
        self.assertIn("second", result.output)
        runs = [event for event in self._events("script") if event["path"].endswith(f"{run_dir.name}/driver.sql")]
        self.assertEqual(1, len(runs))

    def test_writer_and_reader_are_different_processes(self) -> None:
        self._pool()
        applied, _ = self._run("FAKE_SHOW_STATE;", phase="apply", commit=True)
        checked, _ = self._run("FAKE_SHOW_STATE;", phase="postconditions")
        self.assertNotEqual(self._state(applied.output)["pid"], self._state(checked.output)["pid"])

    def test_writer_reconnects_when_compiler_baseline_unreadable(self) -> None:
        self.environment["FAKE_NO_COMPILER"] = "1"
        self._pool()
        self._run("PROMPT apply one", phase="apply", commit=True)
        self._run("PROMPT apply two", phase="apply", commit=True)
        self._run("PROMPT check one", phase="preconditions")
        self._run("PROMPT check two", phase="preconditions")
        self.assertEqual(1, len(self._events("connect")), "only the writer reconnects, once before its second apply")

    def test_driver_without_pooled_epilogue_runs_one_shot(self) -> None:
        pool = self._pool()
        result, run_dir = self._run("PROMPT legacy", pooled=False)
        self.assertIn("legacy", result.output)
        self.assertEqual(0, pool.stats()["processesStarted"])
        starts = self._events("start")
        self.assertEqual(1, len(starts))
        self.assertTrue(starts[0]["args"][-1].endswith(f"{run_dir.name}/driver.sql"))

    def test_pooled_driver_runs_safely_one_shot(self) -> None:
        pool = self._pool()
        pool.fallback_reason = "test fallback"
        result, _ = self._run("PROMPT pooled text, one-shot run", commit=True)
        self.assertEqual(0, result.returncode)
        self.assertIn("pooled text, one-shot run", result.output)
        self.assertEqual(0, pool.stats()["processesStarted"])

    def test_persistent_sessions_off_uses_one_shot(self) -> None:
        with sqlcl_pool.deployment_sessions({"SQLCL_PERSISTENT_SESSIONS": "0"}) as pool:
            self.assertIsNone(pool)
            self.assertEqual("EXIT SUCCESS ROLLBACK", sqlcl_session.driver_epilogue(False))

    def test_nested_context_reuses_outer_pool(self) -> None:
        with sqlcl_pool.deployment_sessions(self.environment) as outer:
            self.assertIsNotNone(outer)
            with sqlcl_pool.deployment_sessions(self.environment) as inner:
                self.assertIs(outer, inner)
            self.assertIs(outer, sqlcl_session.active_pool())
        self.assertIsNone(sqlcl_session.active_pool())

    def test_alias_with_unsupported_characters_refused(self) -> None:
        with self.assertRaises(sqlcl_pool.PoolUnavailable):
            sqlcl_pool.SqlclProcess("bad alias!", "reader", self.root, self.environment)

    def test_start_dir_has_empty_sqlpath(self) -> None:
        self._pool()
        self._run("PROMPT ok")
        start = self._events("start")[0]
        self.assertTrue(start["sqlpath"].endswith(".sqlcl-path"))
        self.assertEqual([], os.listdir(start["sqlpath"]) if os.path.isdir(start["sqlpath"]) else [])

    def test_evidence_records_role_and_process(self) -> None:
        self._pool()
        _, run_dir = self._run("PROMPT ok", phase="apply", commit=True)
        evidence = json.loads((run_dir / "sqlcl-session.json").read_text(encoding="utf-8"))
        self.assertEqual("writer", evidence["role"])
        self.assertEqual("pooled", evidence["mode"])
        self.assertEqual(0, evidence["returncode"])

    def test_start_failure_falls_back_to_one_shot(self) -> None:
        self.environment["FAKE_START_FAIL"] = "1"
        pool = self._pool()
        result, _ = self._run("PROMPT still runs")
        self.assertIn("still runs", result.output)
        self.assertIsNotNone(pool.stats()["fallbackReason"])
        self.assertEqual("one-shot", pool.stats()["mode"])


class EntryPointTests(unittest.TestCase):
    def test_entry_points_enter_pool(self) -> None:
        # Every deployment command keeps one pool for its whole run; rollout calls migrate in-process (nested).
        from scripts import compare_schema, feature_cli, migrate, release_capsule, rollout

        for function in (rollout.main, migrate.main, feature_cli.main, compare_schema.main, compare_schema.main_compare_env):
            with self.subTest(function=f"{function.__module__}.{function.__name__}"):
                self.assertTrue(hasattr(function, "__wrapped__"), "entry point is not decorated with with_deployment_sessions")
        self.assertIn("scripts/sqlcl_pool.py", release_capsule.ENGINE_HELPER_FILES)

    def test_report_records_sqlcl_mode_and_counts(self) -> None:
        from scripts import rollout

        report: dict = {}
        rollout._finish_report(report, time.perf_counter(), "success", 0)
        self.assertEqual("one-shot", report["sqlcl"]["mode"])
        pool = sqlcl_pool.SessionPool()
        sqlcl_session._set_active_pool(pool)
        try:
            rollout._finish_report(report, time.perf_counter(), "success", 0)
        finally:
            sqlcl_session._set_active_pool(None)
            pool.close()
        self.assertEqual({"mode", "fallbackReason", "processesStarted", "databaseSessions", "scripts"}, set(report["sqlcl"]))
        self.assertEqual("pooled", report["sqlcl"]["mode"])

    def test_prod_accepts_staging_proof_from_other_sqlcl_mode(self) -> None:
        # The mode is reported, not enforced: the stage proof requires only its own fields.
        from scripts import release_state

        self.assertNotIn("sqlcl", release_state.REPORT_FIELDS)


class ResetBlockTests(unittest.TestCase):
    def test_reset_block_is_one_plsql_call(self) -> None:
        baseline = sqlcl_pool.SessionBaseline(
            "ASHERIF", "ORA$BASE", {"NLS_LANGUAGE": "AMERICAN", "NLS_DATE_FORMAT": "DD-MON-RR"},
            {"plsql_optimize_level": "2", "plsql_warnings": "DISABLE:ALL", "plsql_ccflags": " "},
        )
        text = sqlcl_pool.reset_block(baseline, Path("/tmp/base.sql"), {"X"})
        lines = text.splitlines()
        self.assertEqual(1, lines.count("BEGIN"))
        self.assertEqual(1, lines.count("/"))
        body = lines[lines.index("BEGIN") + 1:lines.index("END;")]
        self.assertEqual("  DBMS_SESSION.RESET_PACKAGE;", body[-1])
        self.assertIn("  ROLLBACK;", body)
        self.assertIn("ALTER SESSION SET CURRENT_SCHEMA = \"ASHERIF\"", text)
        self.assertIn("ALTER SESSION SET plsql_optimize_level = 2", text)
        self.assertIn("ALTER SESSION SET plsql_warnings = ''DISABLE:ALL''", text)
        self.assertIn("ALTER SESSION SET NLS_DATE_FORMAT = ''DD-MON-RR''", text)
        self.assertLess(text.index("NLS_LANGUAGE"), text.index("NLS_DATE_FORMAT"))
        self.assertIn("UNDEFINE X", lines)
        self.assertEqual(f"PROMPT {sqlcl_pool.RESET_OK_MARKER}", lines[-1])

    def test_server_output_is_off_before_the_client_settings_are_replayed(self) -> None:
        # With SERVEROUTPUT on, SQLcl fetches server output after every database call, so each SET line of the
        # replay that touches the database costs a second round trip (about 9 s per reset on a remote database).
        baseline = sqlcl_pool.SessionBaseline("ASHERIF", "ORA$BASE", {}, None)
        lines = sqlcl_pool.reset_block(baseline, Path("/tmp/base.sql"), set()).splitlines()
        self.assertLess(lines.index("SET SERVEROUTPUT OFF"), lines.index('@"/tmp/base.sql"'))
        self.assertGreater(lines.index("SET SERVEROUTPUT OFF"), lines.index("/"))

    def test_reset_replays_sqlcl_ddl_settings_instead_of_defaults(self) -> None:
        # A one-shot SQLcl replays its own DDL settings on connect, and compare-env reads view DDL with them,
        # so a reused session must get the same transforms back, not DBMS_METADATA's defaults.
        ddl = sqlcl_pool.parse_show_ddl(
            f"noise\n{sqlcl_pool.DDL_MARKER}\nsetting               value\n--------------------  -----\n"
            f"collation_clause      NEVER\nstorage               OFF\nsqlterminator         ON\n{sqlcl_pool.DDL_END_MARKER}\n"
        )
        self.assertEqual({"collation_clause": "NEVER", "storage": "OFF", "sqlterminator": "ON"}, ddl)
        baseline = sqlcl_pool.SessionBaseline("APP", "ORA$BASE", {}, None, ddl)
        text = sqlcl_pool.reset_block(baseline, Path("/tmp/base.sql"), set())
        default = text.index("SESSION_TRANSFORM, 'DEFAULT');")
        for expected in ("'STORAGE', FALSE);", "'SQLTERMINATOR', TRUE);", "'COLLATION_CLAUSE', 'NEVER');"):
            self.assertLess(default, text.index(expected))
        self.assertIsNone(sqlcl_pool.parse_show_ddl("no markers here"))

    def test_unrestorable_baseline_value_refuses(self) -> None:
        baseline = sqlcl_pool.SessionBaseline("APP", "ORA$BASE", {"NLS_DATE_FORMAT": "bad\nvalue"}, None)
        with self.assertRaises(sqlcl_pool.PoolUnavailable):
            sqlcl_pool.reset_block(baseline, Path("/tmp/base.sql"), set())

    def test_script_defines_follow_included_scripts_and_positional_arguments(self) -> None:
        # Variables defined in @/@@ scripts, and the positional values an include passes, outlive the driver in a
        # reused session; the reset must undefine them too.
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            (run_dir / "inc").mkdir()
            (run_dir / "driver.sql").write_text('@@inc/a.sql X Y\n@"b.sql"\nPROMPT done\n', encoding="utf-8")
            (run_dir / "inc" / "a.sql").write_text("DEFINE alpha = 1\n@@nested\n", encoding="utf-8")
            (run_dir / "inc" / "nested.sql").write_text("DEF gamma=2\n@@a.sql\n", encoding="utf-8")
            (run_dir / "b.sql").write_text("SELECT '&&beta' FROM dual;\n@missing.sql\n", encoding="utf-8")
            self.assertEqual(
                {"1", "2", "alpha", "gamma", "beta"},
                sqlcl_pool.script_defines(run_dir / "driver.sql", run_dir),
            )

    def test_driver_defines_finds_define_and_double_ampersand(self) -> None:
        self.assertEqual({"lifecycle_user_hex", "page"}, sqlcl_pool.driver_defines(
            "DEFINE lifecycle_user_hex = 41\nSELECT &&page FROM dual;\n-- &single is not defined\n"
        ))


if __name__ == "__main__":
    unittest.main()
