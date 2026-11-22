"""Tests for scripts/staging_neutralize.py and its team.sh / team.ps1 dispatch.

The SQLcl runner is always replaced by a fake: no test starts a real SQLcl or reaches a database.
"""

import _test_subprocess_safety  # noqa: F401
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
import _console_ctrl_c
import contextlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import staging_neutralize as module
from scripts.sqlcl_session import SqlclError, SqlclResult
from fake_sqlcl import BASH


ROOT = Path(__file__).resolve().parents[1]
STAGING = (
    "\nSTAGING_SQLCL_CONNECTION=stage-app\nSTAGING_EXPECTED_USER=APP_STAGE\nSTAGING_SCHEMA=APP_STAGE\n"
    "STAGING_DBA_SQLCL_CONNECTION=stage-dba\n"
)
CONFIRM = "EB71POD"
IDENTITY = "SYS_CONTEXT DB_UNIQUE_NAME=EB71POD  CON_NAME=G5A5C1FE1C978D4_STAGE  SESSION_USER=DBA_OPS"
APPLY_OUTPUT = "\n".join([
    "NEUTRALIZE|IDENT|EB71POD|G5A5C1FE1C978D4_STAGE|DBA_OPS",
    "NEUTRALIZE|APEX|APEX_240200",
    "NEUTRALIZE|UNDO|JOB|RUNERP|PAY_SADAD",
    "NEUTRALIZE|UNDO|AUTOMATION|130|CUST_MAIL|Muqeem's WS",
    "NEUTRALIZE|UNDO|ACE|RUNERP|smtp.example.com|25|25|CONNECT",
    "NEUTRALIZE|UNDO|ACE|APEX_240200|api.example.com||443|HTTP",
    "NEUTRALIZE|SUMMARY|4|0",
    "",
])
EMPTY_INVENTORY = "NEUTRALIZE|IDENT|EB71POD|G5A5C1FE1C978D4_STAGE|DBA_OPS\nNEUTRALIZE|APEX|APEX_240200\nNEUTRALIZE|END\n"


class FakeSqlcl:
    """Stands in for run_sqlcl: records each driver it is given and answers with a scripted output."""

    def __init__(self, output: str = EMPTY_INVENTORY, error: Exception | None = None):
        self.output = output
        self.error = error
        self.drivers: list[str] = []
        self.connections: list[str] = []

    def __call__(self, target, driver_path, run_dir, **_):
        self.connections.append(target.connection)
        self.drivers.append(Path(driver_path).read_text(encoding="utf-8"))
        if self.error is not None:
            raise self.error
        return SqlclResult(0, self.output, Path(run_dir))


class StagingNeutralizeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()

    def write_env(self, extra: str = STAGING, base: str | None = None) -> Path:
        text = (ROOT / ".env.example").read_text(encoding="utf-8") if base is None else base
        env = self.repo / ".env"
        env.write_text(text + extra, encoding="utf-8", newline="\n")
        return env

    def run_main(self, arguments, *, sqlcl: FakeSqlcl | None = None, env_extra: str = STAGING):
        env = self.write_env(env_extra)
        fake = sqlcl if sqlcl is not None else FakeSqlcl()
        out, err = io.StringIO(), io.StringIO()
        argv = [*arguments, "--repo-root", str(self.repo), "--env-file", str(env)]
        with patch("scripts.staging_neutralize.run_sqlcl", fake), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = module.main(argv)
            except SystemExit as exit_request:
                code = exit_request.code
        return code, out.getvalue(), err.getvalue(), fake

    def restore_directory(self) -> Path:
        return self.repo / "scratch" / "staging-neutralize"

    def undo_files(self) -> list[Path]:
        return sorted(self.restore_directory().glob("*-restore.sql"))

    # ---- configuration refusals -------------------------------------------------------------

    def test_missing_dba_connection_is_refused_before_any_sql(self) -> None:
        code, _, err, fake = self.run_main(["--check"], env_extra="\nSTAGING_SQLCL_CONNECTION=stage-app\n"
                                           "STAGING_EXPECTED_USER=APP_STAGE\nSTAGING_SCHEMA=APP_STAGE\n")
        self.assertEqual(2, code, err)
        self.assertIn("STAGING_DBA_SQLCL_CONNECTION", err)
        self.assertEqual([], fake.drivers)

    def test_production_looking_dba_connection_is_refused(self) -> None:
        for alias in ("erp-prod-dba", "PRODDB_DBA", "live-dba"):
            with self.subTest(alias=alias):
                extra = STAGING.replace("stage-dba", alias)
                code, _, err, fake = self.run_main(["--capture"], env_extra=extra)
                self.assertEqual(2, code, err)
                self.assertIn("production", err)
                self.assertEqual([], fake.drivers)

    def test_principals_are_the_validated_union_in_order_without_duplicates(self) -> None:
        values = {
            "STAGING_SCHEMA": "APP_STAGE",
            "STAGING_MIGRATION_SCHEMA": "DATA_STAGE,APP_STAGE",
            "STAGING_NEUTRALIZE_PRINCIPALS": "MAIL_OWNER,DATA_STAGE",
        }
        self.assertEqual(("APP_STAGE", "DATA_STAGE", "MAIL_OWNER"), module.parse_principals(values))
        for bad in ("app_stage", "1ABC", "BAD NAME"):
            with self.subTest(bad=bad), self.assertRaises(module.NeutralizeError):
                module.parse_principals({"STAGING_NEUTRALIZE_PRINCIPALS": bad})

    def test_empty_principals_are_refused(self) -> None:
        with self.assertRaises(module.NeutralizeError):
            module.parse_principals({})
        code, _, err, fake = self.run_main(["--check"], env_extra="\nSTAGING_DBA_SQLCL_CONNECTION=stage-dba\n")
        self.assertEqual(2, code, err)
        self.assertIn("no principals", err)
        self.assertEqual([], fake.drivers)

    def test_apply_and_restore_need_confirm_target_before_any_sql(self) -> None:
        restore = self.repo / "scratch" / "staging-neutralize" / "old-restore.sql"
        restore.parent.mkdir(parents=True)
        restore.write_text("-- target db_unique_name: EB71POD\n", encoding="utf-8", newline="\n")
        for arguments in (["--apply", "--yes"], ["--restore", str(restore), "--yes"]):
            with self.subTest(arguments=arguments):
                code, _, err, fake = self.run_main(arguments)
                self.assertEqual(2, code, err)
                self.assertIn("--confirm-target", err)
                self.assertEqual([], fake.drivers)

    def test_read_only_modes_refuse_confirm_target_and_yes(self) -> None:
        with self.assertRaises(SystemExit) as refused, contextlib.redirect_stderr(io.StringIO()):
            module.main(["--check", "--confirm-target", CONFIRM, "--repo-root", str(self.repo)])
        self.assertEqual(2, refused.exception.code)

    # ---- generated SQL --------------------------------------------------------------------

    def test_apply_guard_comes_before_every_disable_and_remove_and_names_no_apex_schema(self) -> None:
        sql = module.apply_script(("APP_STAGE", "MAIL_OWNER"), CONFIRM)
        guard = sql.index("UPPER(SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME')) <> UPPER(c_confirmed)")
        self.assertIn("c_confirmed CONSTANT VARCHAR2(128) := 'EB71POD'", sql)
        for call in ("DBMS_SCHEDULER.DISABLE", "APEX_AUTOMATION.DISABLE", "REMOVE_HOST_ACE"):
            with self.subTest(call=call):
                self.assertLess(guard, sql.index(call))
        for text in (sql, module.inventory_script(("APP_STAGE",)), module.restore_script(CONFIRM, self.repo / "x-restore.sql")):
            self.assertIsNone(re.search(r"APEX_\d{6}", text))
        self.assertIn("SELECT schema INTO v_apex FROM dba_registry WHERE comp_id = 'APEX'", sql)

    def test_neutralize_sql_refuses_production_looking_observed_name(self) -> None:
        # B4: the in-database guard checks the observed names, not only the alias, before anything is disabled.
        sql = module.apply_script(("APP_STAGE",), CONFIRM)
        guard = sql.index("Refusing: the observed database name looks like production")
        for attribute in ("DB_UNIQUE_NAME", "DB_NAME", "SERVICE_NAME"):
            with self.subTest(attribute=attribute):
                self.assertIn(f"REGEXP_LIKE(REGEXP_REPLACE(NVL(SYS_CONTEXT('USERENV', '{attribute}'), ' ')", sql)
        self.assertIn("(pre|non)[-_.]?(prod|prd)", sql)
        for call in ("DBMS_SCHEDULER.DISABLE", "APEX_AUTOMATION.DISABLE", "REMOVE_HOST_ACE"):
            with self.subTest(call=call):
                self.assertLess(guard, sql.index(call))

    def test_capture_and_check_scripts_never_change_anything(self) -> None:
        sql = module.inventory_script(("APP_STAGE",))
        for call in ("DISABLE(", "ENABLE(", "REMOVE_HOST_ACE", "APPEND_HOST_ACE", "COMMIT"):
            with self.subTest(call=call):
                self.assertNotIn(call, sql)

    def test_principals_are_quoted_as_literals_in_the_generated_sql(self) -> None:
        sql = module.inventory_script(("APP_STAGE", "MAIL_OWNER"))
        self.assertIn("owner IN ('APP_STAGE', 'MAIL_OWNER')", sql)
        self.assertIn("'ORACLE_APEX_MAIL_QUEUE'", sql)
        for name in module.APEX_JOB_NAMES:
            self.assertIn(name, sql)

    # ---- apply, confirmation and the undo file -------------------------------------------

    def test_yes_skips_the_prompt_and_apply_writes_the_undo_file(self) -> None:
        fake = FakeSqlcl(APPLY_OUTPUT)
        with patch("builtins.input", side_effect=AssertionError("prompt must be skipped")):
            code, out, err, fake = self.run_main(["--apply", "--confirm-target", CONFIRM, "--yes"], sqlcl=fake)
        self.assertEqual(0, code, out + err)
        self.assertIn("Confirmed by --yes and --confirm-target EB71POD", out)
        self.assertEqual(1, len(fake.drivers))
        self.assertIn("DBMS_SCHEDULER.DISABLE", fake.drivers[0])
        files = self.undo_files()
        self.assertEqual(1, len(files))
        path = files[0]
        self.assertEqual(self.restore_directory().resolve(), path.parent.resolve())
        self.assertIn(f"Undo file: {path}", out)
        self.assertTrue(path.name.startswith("EB71POD-") and path.name.endswith("-restore.sql"))
        text = path.read_text(encoding="utf-8")
        self.assertIn("BEGIN DBMS_SCHEDULER.ENABLE(name => 'RUNERP.PAY_SADAD'); END;", text)
        self.assertIn("Muqeem''s WS", text)
        self.assertIn("APPEND_HOST_ACE(host => 'smtp.example.com', lower_port => 25, upper_port => 25", text)
        self.assertIn("lower_port => NULL, upper_port => 443", text)
        self.assertIn("'http'", text)
        self.assertEqual(4, module.validate_restore_file(path, CONFIRM))

    def test_terminal_prompt_declined_changes_nothing(self) -> None:
        with patch("scripts.staging_neutralize.terminal_confirm.may_read_yes_no", return_value=True), \
                patch("builtins.input", return_value="n"):
            code, out, err, fake = self.run_main(["--apply", "--confirm-target", CONFIRM])
        self.assertEqual(2, code, out + err)
        self.assertEqual([], fake.drivers)
        self.assertEqual([], self.undo_files())

    def test_piped_input_without_yes_is_refused_and_nothing_runs(self) -> None:
        with patch("sys.stdin", io.StringIO("y\n")), patch("builtins.input", side_effect=AssertionError("not reached")):
            code, out, err, fake = self.run_main(["--apply", "--confirm-target", CONFIRM])
        self.assertEqual(2, code, out + err)
        self.assertIn("piped or redirected answers are not accepted", err)
        self.assertEqual([], fake.drivers)
        self.assertEqual([], self.undo_files())

    def test_failed_objects_are_printed_and_give_exit_1(self) -> None:
        output = "\n".join([
            "NEUTRALIZE|IDENT|EB71POD|G5A5C1FE1C978D4_STAGE|DBA_OPS",
            "NEUTRALIZE|APEX|APEX_240200",
            "NEUTRALIZE|FAILED|JOB|APEX_240200.ORACLE_APEX_MAIL_QUEUE|ORA-01031: insufficient privileges",
            "NEUTRALIZE|UNDO|JOB|RUNERP|PAY_SADAD",
            "NEUTRALIZE|SUMMARY|1|1",
        ])
        code, out, err, _ = self.run_main(["--apply", "--confirm-target", CONFIRM, "-y"], sqlcl=FakeSqlcl(output))
        self.assertEqual(1, code, out + err)
        self.assertIn("ORA-01031", err)
        self.assertIn("Changed 1 object(s), 1 failure(s)", out)

    def test_sqlcl_refusal_without_changes_leaves_no_undo_file(self) -> None:
        refusal = SqlclError("SQLcl reported a database or client error", self.repo,
                             "ORA-20001: Refusing: DB_UNIQUE_NAME is not the confirmed target; no change was made\n")
        code, out, err, _ = self.run_main(["--apply", "--confirm-target", CONFIRM, "-y"], sqlcl=FakeSqlcl(error=refusal))
        self.assertEqual(2, code, out + err)
        self.assertEqual([], self.undo_files())
        self.assertNotIn("Undo file", out)

    def test_apply_writes_the_undo_file_even_when_sqlcl_stops_after_some_changes(self) -> None:
        partial = SqlclError("SQLcl exited with status 1", self.repo, APPLY_OUTPUT.replace("NEUTRALIZE|SUMMARY|4|0", ""))
        code, out, err, _ = self.run_main(["--apply", "--confirm-target", CONFIRM, "-y"], sqlcl=FakeSqlcl(error=partial))
        self.assertEqual(2, code, out + err)
        self.assertEqual(1, len(self.undo_files()))

    # ---- check and capture ----------------------------------------------------------------

    def test_check_exit_0_when_inert_and_prints_identity_first(self) -> None:
        code, out, err, _ = self.run_main(["--check"], sqlcl=FakeSqlcl(EMPTY_INVENTORY))
        self.assertEqual(0, code, out + err)
        self.assertEqual(IDENTITY, out.splitlines()[0])
        self.assertIn("INERT", out)
        self.assertIn("--apply would change: 0 job(s), 0 automation(s), 0 ACE(s); running jobs: 0", out)

    def test_check_exit_1_when_anything_is_still_active(self) -> None:
        output = EMPTY_INVENTORY.replace(
            "NEUTRALIZE|END",
            "NEUTRALIZE|ROW|JOB|RUNERP|PAY_SADAD|SCHEDULED\n"
            "NEUTRALIZE|ROW|AUTOMATION|130|CUST_MAIL|WS|ACTIVE\n"
            "NEUTRALIZE|ROW|ACE|RUNERP|smtp.example.com|25|25|CONNECT\n"
            "NEUTRALIZE|END",
        )
        code, out, err, _ = self.run_main(["--check"], sqlcl=FakeSqlcl(output))
        self.assertEqual(1, code, out + err)
        self.assertIn("--apply would change: 1 job(s), 1 automation(s), 1 ACE(s)", out)
        self.assertIn("ACTIVE:", out)

    def test_check_is_an_error_when_the_inventory_is_incomplete(self) -> None:
        code, out, err, _ = self.run_main(["--check"], sqlcl=FakeSqlcl("NEUTRALIZE|IDENT|EB71POD|X|Y\n"))
        self.assertEqual(2, code, out + err)
        self.assertIn("result is unknown", err)

    def test_capture_prints_the_tables_read_only_and_reminders(self) -> None:
        output = EMPTY_INVENTORY.replace("NEUTRALIZE|END", "NEUTRALIZE|ROW|JOB|RUNERP|PAY_SADAD|SCHEDULED\nNEUTRALIZE|END")
        code, out, err, fake = self.run_main(["--capture"], sqlcl=FakeSqlcl(output))
        self.assertEqual(0, code, err)
        self.assertIn("PAY_SADAD", out)
        self.assertIn("cloud access control list", out)
        self.assertIn("after every APEX application import", out)
        self.assertNotIn("DISABLE(", fake.drivers[0])
        self.assertEqual(self.undo_files(), [])

    # ---- restore --------------------------------------------------------------------------

    def make_undo_file(self, name: str = "EB71POD-20261121T100000Z-restore.sql", target: str = CONFIRM) -> Path:
        path = self.restore_directory() / name
        path.parent.mkdir(parents=True, exist_ok=True)
        undo = [("JOB", ("RUNERP", "PAY_SADAD")), ("ACE", ("RUNERP", "smtp.example.com", "25", "25", "CONNECT"))]
        path.write_text(module.render_restore_file(target, "20261121T100000Z", undo), encoding="utf-8", newline="\n")
        return path

    def test_restore_runs_the_guard_then_the_undo_file_inside_scratch(self) -> None:
        path = self.make_undo_file()
        code, out, err, fake = self.run_main(["--restore", str(path), "--confirm-target", CONFIRM, "-y"])
        self.assertEqual(0, code, out + err)
        driver = fake.drivers[0]
        self.assertLess(driver.index("RAISE_APPLICATION_ERROR"), driver.index(f'@"{path.resolve().as_posix()}"'))
        self.assertIn("c_confirmed CONSTANT VARCHAR2(128) := 'EB71POD'", driver)
        self.assertIn("cloud access control list", out)

    def test_restore_refuses_a_file_outside_scratch_or_missing_or_not_sql(self) -> None:
        outside = self.repo / "undo.sql"
        outside.write_text(module.render_restore_file(CONFIRM, "20261121T100000Z", []), encoding="utf-8", newline="\n")
        inside_text = self.restore_directory() / "notes.txt"
        inside_text.parent.mkdir(parents=True, exist_ok=True)
        inside_text.write_text("-- target db_unique_name: EB71POD\n", encoding="utf-8", newline="\n")
        for path in (outside, self.restore_directory() / "missing-restore.sql", inside_text):
            with self.subTest(path=path.name):
                code, out, err, fake = self.run_main(["--restore", str(path), "--confirm-target", CONFIRM, "-y"])
                self.assertEqual(2, code, out + err)
                self.assertEqual([], fake.drivers)

    def test_restore_refuses_a_file_for_another_target_and_any_unexpected_statement(self) -> None:
        other = self.make_undo_file("OTHERDB-20261121T100000Z-restore.sql", target="OTHERDB")
        code, out, err, fake = self.run_main(["--restore", str(other), "--confirm-target", CONFIRM, "-y"])
        self.assertEqual(2, code, err)
        self.assertEqual([], fake.drivers)
        tampered = self.make_undo_file("EB71POD-20261121T100001Z-restore.sql")
        tampered.write_text(tampered.read_text(encoding="utf-8") + "DROP TABLE RUNERP.PAYMENTS;\n", encoding="utf-8", newline="\n")
        code, out, err, fake = self.run_main(["--restore", str(tampered), "--confirm-target", CONFIRM, "-y"])
        self.assertEqual(2, code, err)
        self.assertEqual([], fake.drivers)

    def test_restore_file_path_must_be_inside_scratch_staging_neutralize(self) -> None:
        with self.assertRaises(module.NeutralizeError):
            module._check_restore_path(str(self.root / "elsewhere.sql"), self.repo)

    # ---- the wrapper's exit codes map exactly ---------------------------------------------

    def test_two_modes_at_once_is_an_argument_error(self) -> None:
        with self.assertRaises(SystemExit) as refused, contextlib.redirect_stderr(io.StringIO()):
            module.main(["--capture", "--check", "--repo-root", str(self.repo)])
        self.assertEqual(2, refused.exception.code)


class StagingNeutralizeConfigTests(unittest.TestCase):
    def test_principals_key_is_accepted_by_the_env_reader(self) -> None:
        from scripts.local_config import read_project_env

        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / ".env"
            env.write_text((ROOT / ".env.example").read_text(encoding="utf-8") + "\nSTAGING_NEUTRALIZE_PRINCIPALS=MAIL_OWNER\n",
                           encoding="utf-8", newline="\n")
            self.assertEqual("MAIL_OWNER", read_project_env(env)["STAGING_NEUTRALIZE_PRINCIPALS"])


def _wrapper_checkout(parent: Path) -> Path:
    """Copy only the two wrappers and the PowerShell resolver into a checkout with spaces and brackets in its path."""
    root = parent / "neutralize checkout [wrappers]"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in ("team.sh", "team.ps1", "resolve_python.ps1"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    shutil.copy2(ROOT / "template-manifest.json", root / "template-manifest.json")
    (root / ".env").write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    return root


def _fake_python(directory: Path, record: Path) -> None:
    """A python3 that records its arguments and PROJECT_ENV_FILE, one per line, and exits 0."""
    if os.name == "nt":
        source = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "if sys.argv[1:2] == ['-c']:\n"
            "    raise SystemExit(0)\n"
            f"Path({str(record)!r}).write_text('\\n'.join(sys.argv[1:] + ['ENV:' + os.environ.get('PROJECT_ENV_FILE', '')]) + '\\n', encoding='utf-8')\n"
        )
        _console_ctrl_c.write_python_command(directory, "python3", source)
        return
    directory.mkdir(parents=True, exist_ok=True)
    fake = directory / "python3"
    fake.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = -c ]; then exit 0; fi\n"
        f"for argument in \"$@\"; do printf '%s\\n' \"$argument\"; done > '{record}'\n"
        f"printf 'ENV:%s\\n' \"$PROJECT_ENV_FILE\" >> '{record}'\n",
        encoding="utf-8",
        newline="\n",
    )
    fake.chmod(0o755)


class StagingNeutralizeWrapperTests(unittest.TestCase):
    def test_both_wrappers_dispatch_staging_neutralize_to_the_python_module(self) -> None:
        pwsh = _test_subprocess_safety.working_pwsh()
        entries = [("team.sh", [BASH])]
        if pwsh:
            entries.append(("team.ps1", [pwsh, "-NoProfile", "-File"]))
        for name, launcher in entries:
            with self.subTest(wrapper=name), tempfile.TemporaryDirectory() as temporary:
                root = _wrapper_checkout(Path(temporary))
                record = root / "python-argv.txt"
                fake_bin = root / "bin"
                _fake_python(fake_bin, record)
                environment = dict(
                    os.environ,
                    PATH=f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    PROJECT_ENV_FILE=str(root / ".env"),
                )
                result = subprocess.run(
                    [*launcher, str(root / "scripts" / name), "staging-neutralize", "--check"],
                    cwd=root, env=environment, capture_output=True, text=True, check=False,
                    stdin=subprocess.DEVNULL, timeout=120,
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                lines = record.read_text(encoding="utf-8").splitlines()
                self.assertEqual(["-m", "scripts.staging_neutralize", "--check"], lines[:-1])
                # Git Bash hands Python the path with "/" (cygpath -m), so compare paths, not strings.
                self.assertTrue(lines[-1].startswith("ENV:"), lines[-1])
                self.assertEqual(Path(lines[-1][len("ENV:"):]), root / ".env")

    def test_both_wrappers_list_staging_neutralize_and_release_plan_in_usage(self) -> None:
        pwsh = _test_subprocess_safety.working_pwsh()
        entries = [[BASH, str(ROOT / "scripts" / "team.sh")]]
        if pwsh:
            entries.append([pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1")])
        for command in entries:
            with self.subTest(entry=command[-1]):
                result = subprocess.run([*command, "--help"], text=True, capture_output=True, check=False, cwd=ROOT)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("staging-neutralize --capture|--check|--apply|--restore FILE", result.stdout)
                self.assertIn("convert-plan|release-plan>", result.stdout)


if __name__ == "__main__":
    unittest.main()
