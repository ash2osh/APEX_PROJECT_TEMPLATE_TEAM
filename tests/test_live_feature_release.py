"""Opt-in checks of the feature-release path against one disposable Oracle schema.

These run the real `scripts/team.sh` feature and rollout commands in a copy of this
repository, against a database you name. They create and drop their own objects in
TEMPLATE_LIVE_SCHEMA, so point them only at a schema you can afford to lose. CI never
runs them: the module is skipped unless these variables are set:

    TEMPLATE_LIVE_DISPOSABLE=1       you accept that objects are created and dropped
    TEMPLATE_LIVE_CONNECTION=<name>  a saved SQLcl connection that logs in as the schema
    TEMPLATE_LIVE_SCHEMA=<SCHEMA>    the disposable schema, which must be that login's user
    TEMPLATE_LIVE_SECOND_CONNECTION  optional, with TEMPLATE_LIVE_SECOND_SCHEMA, for the
    TEMPLATE_LIVE_SECOND_SCHEMA      two-schema check
    TEMPLATE_LIVE_STAGING_CONNECTION optional, a saved connection to a separate staging
                                     database (for example Docker) for the staging profile.
                                     The production profile keeps TEMPLATE_LIVE_CONNECTION.
                                     Do not combine it with the two-schema variables.

See docs/live-feature-tests.md for the procedure and the scenarios covered.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONNECTION = os.environ.get("TEMPLATE_LIVE_CONNECTION", "")
SCHEMA = os.environ.get("TEMPLATE_LIVE_SCHEMA", "").upper()
SECOND_CONNECTION = os.environ.get("TEMPLATE_LIVE_SECOND_CONNECTION", "")
SECOND_SCHEMA = os.environ.get("TEMPLATE_LIVE_SECOND_SCHEMA", "").upper()
STAGING_CONNECTION = os.environ.get("TEMPLATE_LIVE_STAGING_CONNECTION", "")
STAGING_DB = STAGING_CONNECTION or CONNECTION
ENABLED = os.environ.get("TEMPLATE_LIVE_DISPOSABLE") == "1" and bool(CONNECTION and SCHEMA)
SKIP_REASON = (
    "live feature checks need TEMPLATE_LIVE_DISPOSABLE=1, TEMPLATE_LIVE_CONNECTION and "
    "TEMPLATE_LIVE_SCHEMA; see docs/live-feature-tests.md"
)
ATTEMPT_WAIT_SECONDS = 240
IGNORED_IN_COPY = shutil.ignore_patterns(
    ".git", "scratch", "graphify-out", "ai_generate", "__pycache__", "*.pyc", "releases", "features",
)


def _run(args, *, cwd, env, stdin=None, timeout=900) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, cwd=cwd, env=env, input=stdin, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, check=False,
    )


def _git(project: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=live-checks", "-c", "user.email=live-checks@localhost", *args],
        cwd=project, check=True, capture_output=True, text=True,
    )


def _commit(project: Path, message: str) -> None:
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "--allow-empty", "-m", message)


def _drop_table(name: str) -> str:
    return _drop_if_present("table", f"{name} purge", (-942,))


def _drop_package(name: str) -> str:
    return _drop_if_present("package", name, (-4043,))


def _drop_if_present(kind: str, target: str, missing_codes: tuple[int, ...]) -> str:
    # A scenario may fail before it creates its object, so "does not exist" is not an error here.
    codes = ", ".join(str(code) for code in missing_codes)
    return (
        "begin\n"
        f"  execute immediate 'drop {kind} {target}';\n"
        "exception\n"
        f"  when others then\n    if sqlcode not in ({codes}) then raise; end if;\n"
        "end;\n/"
    )


def _drop_synonym(name: str, *, public: bool = False) -> str:
    return _drop_if_present("public synonym" if public else "synonym", name, (-1432,) if public else (-1434,))


def _sweep_token(token: str) -> str:
    # Drops every package and table named with this run's token, on whichever database it runs against.
    return (
        "begin\n"
        "  for o in (select object_name, object_type from user_objects\n"
        "            where object_type in ('PACKAGE', 'TABLE', 'SYNONYM', 'VIEW', 'SEQUENCE') and instr(object_name, '" + token + "') > 0) loop\n"
        "    execute immediate 'drop ' || o.object_type || ' ' || o.object_name\n"
        "      || case when o.object_type = 'TABLE' then ' cascade constraints purge' end;\n"
        "  end loop;\n"
        "end;\n/"
    )


def _sqlcl(connection: str, script: str, workdir: str, env: dict, *, tolerate: bool = False) -> str:
    """Run a script in SQLcl. A failure raises unless tolerate is set (used for cleanup)."""
    header = "whenever sqlerror continue\n" if tolerate else "whenever sqlerror exit failure rollback\n"
    path = Path(workdir) / f"{uuid.uuid4().hex}.sql"
    path.write_text(header + "set define off\n" + script.strip() + "\nexit\n", encoding="utf-8")
    result = _run(["sql", "-S", "-name", connection, f"@{path}"], cwd=workdir, env=env, timeout=600)
    if result.returncode != 0 and not tolerate:
        raise RuntimeError(f"SQLcl failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def _database_name(connection: str, workdir: str, env: dict) -> str:
    """The database's db_unique_name, read through SYS_CONTEXT (USERENV needs no privilege)."""
    output = _sqlcl(connection, "set heading off feedback off\nselect sys_context('USERENV', 'DB_UNIQUE_NAME') from dual;\n", workdir, env)
    name = output.strip().splitlines()[-1].strip() if output.strip() else ""
    if not name:
        raise RuntimeError(f"connection {connection} reported no db_unique_name")
    return name


def _env_text() -> str:
    schemas = [SCHEMA, *([SECOND_SCHEMA] if SECOND_SCHEMA else [])]
    connections = [CONNECTION, *([SECOND_CONNECTION] if SECOND_SCHEMA else [])]
    staging_connections = [STAGING_CONNECTION or CONNECTION, *([SECOND_CONNECTION] if SECOND_SCHEMA else [])]
    return "\n".join([
        "PROJECT_NAME=live-feature-checks",
        "DEVELOPER_NAME=LIVECHECK",
        "DB_ENVIRONMENT=development",
        "APEX_APP_ID=100",
        f"TABLES_SCHEMA={SCHEMA}", "TABLES_PREFIXES=*",
        f"TABLES_SQLCL_CONNECTION={CONNECTION}", f"TABLES_EXPECTED_USER={SCHEMA}",
        f"CODE_SCHEMA={SCHEMA}", "CODE_PREFIXES=*",
        f"CODE_SQLCL_CONNECTION={CONNECTION}", f"CODE_EXPECTED_USER={SCHEMA}",
        f"APEX_PARSING_SCHEMA={SCHEMA}", f"APEX_SQLCL_CONNECTION={CONNECTION}", f"APEX_EXPECTED_USER={SCHEMA}",
        f"MIGRATION_SCHEMA={SCHEMA}", f"MIGRATION_SQLCL_CONNECTION={CONNECTION}", f"MIGRATION_EXPECTED_USER={SCHEMA}",
        f"STAGING_SCHEMA={','.join(schemas)}", f"STAGING_SQLCL_CONNECTION={','.join(staging_connections)}",
        f"STAGING_EXPECTED_USER={','.join(schemas)}",
        f"PROD_SCHEMA={','.join(schemas)}", f"PROD_SQLCL_CONNECTION={','.join(connections)}",
        f"PROD_EXPECTED_USER={','.join(schemas)}",
        "AGENT_STYLE=standard", "AGENT_STYLE_LEARNING=on",
    ]) + "\n"


@unittest.skipUnless(ENABLED, SKIP_REASON)
@unittest.skipIf(os.name == "nt", "the feature path runs team.sh under bash")
class LiveFeatureReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.token = uuid.uuid4().hex[:8].upper()
        cls.workdir = tempfile.TemporaryDirectory()
        cls.project = Path(cls.workdir.name) / "project"
        shutil.copytree(ROOT, cls.project, ignore=IGNORED_IN_COPY)
        env_file = Path(cls.workdir.name) / "live.env"
        env_file.write_text(_env_text(), encoding="utf-8")
        cls.env = {**os.environ, "PROJECT_ENV_FILE": str(env_file)}
        cls.drops: list[tuple[str, str]] = []
        cls.sequence = 0
        # Production runs need the observed name: the production profile is this same database.
        cls.prod_db_name = _database_name(CONNECTION, cls.workdir.name, cls.env)
        _git(cls.project, "init", "-q")
        _commit(cls.project, "live checks: baseline")

    @classmethod
    def tearDownClass(cls) -> None:
        # Every drop is attempted, and a failure is reported rather than hidden.
        failures = []
        for connection, statement in reversed(cls.drops):
            try:
                _sqlcl(connection, statement, cls.workdir.name, cls.env)
            except RuntimeError as error:
                failures.append(f"{statement}: {error}")
        if STAGING_DB != CONNECTION:
            # Objects that staging checkpoints create on their own database never reach the drops above.
            try:
                _sqlcl(STAGING_DB, _sweep_token(cls.token), cls.workdir.name, cls.env)
            except RuntimeError as error:
                failures.append(f"sweep of {cls.token} on the staging database: {error}")
        cls.workdir.cleanup()
        if failures:
            raise AssertionError("live cleanup left objects behind:\n" + "\n".join(failures))

    # Helpers ---------------------------------------------------------------------------

    def _next_number(self) -> int:
        type(self).sequence += 1
        return type(self).sequence

    def _name(self, tag: str) -> str:
        return f"live{self.token.lower()}{tag}{self._next_number()}"

    def _sql(self, connection: str, script: str, *, drops: tuple[str, ...] = (), tolerate: bool = False) -> str:
        self._register_drops(connection, *drops)
        return _sqlcl(connection, script, self.workdir.name, self.env, tolerate=tolerate)

    def _sql_on_both(self, script: str, *, drops: tuple[str, ...] = ()) -> None:
        """Create a fixture on DEV and, when staging is a separate database, on staging too."""
        self._sql(CONNECTION, script, drops=drops)
        if STAGING_DB != CONNECTION:
            self._sql(STAGING_DB, script, drops=drops)

    def _register_drops(self, connection: str, *statements: str) -> None:
        # Cleanup runs in reverse registration order, so later objects go first.
        self.drops.extend((connection, statement) for statement in statements)

    def _count(self, sql: str, connection: str = CONNECTION) -> int:
        output = self._sql(connection, f"set heading off feedback off\n{sql};\n")
        return int(output.strip().splitlines()[-1])

    def _team(self, *args: str, stdin: str | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
        return _run([str(self.project / "scripts" / "team.sh"), *args], cwd=self.project, env=env or self.env, stdin=stdin)

    def _ack_env(self) -> dict:
        """The typed acknowledgments read redirected input only under this test-only switch; the scripts never set it."""
        return {**self.env, "TEAM_ACK_FROM_STDIN": "1"}

    def _feature(self, slug: str, schema: str = SCHEMA) -> None:
        result = self._team("feature", "init", slug, "--schema", schema)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _capture(self, slug: str, package: str) -> list[str]:
        result = self._team("feature", "capture", slug, "--package", package, "--schema", SCHEMA, "--env", "dev")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return re.findall(r"^Captured (\S+)$", result.stdout, flags=re.MULTILINE)

    def _capture_unit(self, slug: str, option: str, name: str) -> list[str]:
        result = self._team("feature", "capture", slug, f"--{option}", name, "--schema", SCHEMA, "--env", "dev")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return re.findall(r"^Captured (\S+)$", result.stdout, flags=re.MULTILINE)

    def _checks(self, slug: str, sql: str, schema: str = SCHEMA) -> None:
        path = self.project / "features" / slug / schema / "checks.json"
        path.write_text(json.dumps({
            "schemaVersion": 1, "preconditions": [],
            "postconditions": [{"id": "live-check", "sql": sql, "expected": 1}],
        }), encoding="utf-8")

    def _valid_body_check(self, package: str) -> str:
        return (f"SELECT CASE WHEN EXISTS (SELECT 1 FROM user_objects WHERE object_name = '{package}' "
                "AND object_type = 'PACKAGE BODY' AND status = 'VALID') THEN 1 ELSE 0 END FROM dual")

    def _script(self, name: str, text: str) -> str:
        path = self.project / "scripts" / "live" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return f"scripts/live/{name}"

    def _manifest(self, steps: list[dict]) -> None:
        path = self.project / "releases" / "current.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"schemaVersion": 2, "databaseObjectBudgetSeconds": 120, "steps": steps}),
                        encoding="utf-8")

    def _freeze(self) -> str:
        result = self._team("feature", "freeze", "releases/current.json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        match = re.search(r"^Capsule: (\S+)$", result.stdout, flags=re.MULTILINE)
        self.assertIsNotNone(match, result.stdout)
        return match.group(1)

    def _rollout(self, environment: str, *, capsule: str | None = None, prepare: bool = False,
                 stage_report: str | None = None, recover: str | None = None,
                 confirm: bool = True) -> tuple[subprocess.CompletedProcess, dict]:
        """Run a rollout. A run is confirmed by -y; production also names the observed database."""
        report = f"scratch/live-{environment}-{self._next_number()}.json"
        args = ["rollout", "releases/current.json", "--env", environment, "--report", report]
        if capsule is not None:
            args += ["--capsule", capsule]
        if prepare:
            args.append("--prepare")
        if recover is not None:
            args += ["--recover", recover]
        if stage_report is not None:
            args += ["--stage-report", stage_report]
        if confirm and not prepare:
            args.append("-y")
            if environment == "prod":
                args += ["--confirm-target", type(self).prod_db_name]
        result = self._team(*args)
        path = self.project / report
        document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        document["reportPath"] = report
        return result, document

    def _staged(self, capsule: str) -> dict:
        """Prepare and run staging, assert success, and return the report document."""
        result, _ = self._rollout("staging", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, document = self._rollout("staging", capsule=capsule)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(document["status"], "success")
        return document

    def _wait_until_settled(self, package: str, *, connection: str = CONNECTION,
                            quiet_seconds: int = 10, limit_seconds: int = 180) -> None:
        """Wait until the package body's DDL timestamp and status stop changing for quiet_seconds."""
        query = (f"select to_char(last_ddl_time, 'YYYYMMDDHH24MISS') || ':' || status from user_objects "
                 f"where object_name = '{package}' and object_type = 'PACKAGE BODY'")
        deadline = time.monotonic() + limit_seconds
        previous, stable_since = None, time.monotonic()
        while time.monotonic() < deadline:
            output = self._sql(connection, f"set heading off feedback off\n{query};\n").strip()
            # No row means the killed run had not yet created the package body; that is a settled state too.
            current = output.splitlines()[-1] if output else "absent"
            if current != previous:
                previous, stable_since = current, time.monotonic()
            elif time.monotonic() - stable_since >= quiet_seconds:
                return
            time.sleep(1)
        self.fail(f"{package} kept changing for {limit_seconds} seconds after the kill")

    def _wait_for_attempting(self, process: subprocess.Popen) -> str:
        index = self.project / "releases" / ".local-state.json"
        deadline = time.monotonic() + ATTEMPT_WAIT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("the rollout finished before any attempt was observed mid-write")
            with contextlib.suppress(OSError, ValueError):
                for row in json.loads(index.read_text(encoding="utf-8")).get("attempts", []):
                    if row.get("state") == "attempting":
                        return row["attemptId"]
            time.sleep(0.2)
        self.fail("no attempt reached the attempting state in time")

    # Scenarios -------------------------------------------------------------------------

    def test_capture_freeze_then_staging_and_production_with_stage_proof(self) -> None:
        slug = self._name("ping")
        package, log = f"LIVE_{self.token}_PKG", f"LIVE_{self.token}_LOG"
        self._sql_on_both(f"""
            create table {log} (id number generated by default on null as identity primary key, msg varchar2(200));
            create or replace package {package} as
              function ping return number;
            end {package};
            /
            create or replace package body {package} as
              function ping return number is begin return 1; end;
            end {package};
            /
        """, drops=(_drop_table(log), _drop_package(package)))
        self._feature(slug)
        captured = self._capture(slug, package)
        body = self.project / next(path for path in captured if path.endswith(".pkb.sql"))
        body.write_text(body.read_text(encoding="utf-8").replace("return 1;", "return 2;", 1), encoding="utf-8")
        self._checks(slug, self._valid_body_check(package))
        script = self._script("mark.sql", f"insert into {log} (msg) values ('ran');\ncommit;\n")
        self._manifest([
            {"type": "feature-migrate", "schema": SCHEMA, "inputs": captured},
            {"type": "sql-script", "file": script, "schema": SCHEMA},
        ])
        _commit(self.project, "live checks: round trip")
        capsule = self._freeze()

        staging = self._staged(capsule)
        self.assertTrue(staging["objectBudget"]["coverageComplete"])

        result, _ = self._rollout("prod", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, production = self._rollout("prod", capsule=capsule, stage_report=staging["reportPath"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(production["status"], "success")
        ran = f"select count(*) from {log} where msg = 'ran'"
        if STAGING_DB != CONNECTION:
            # Staging and production run mark.sql against different databases; each records one row.
            self.assertEqual(self._count(ran, STAGING_DB), 1)
            self.assertEqual(self._count(ran), 1)
        else:
            self.assertEqual(self._count(ran), 2)

        payload = Path(capsule).name.split("-")[0]
        cleanup = self._team("feature", "clean-capsule", payload)
        self.assertEqual(cleanup.returncode, 0, cleanup.stdout + cleanup.stderr)
        self.assertTrue(json.loads(cleanup.stdout)["eligible"])

    def test_trigger_function_and_type_with_body_ship_through_staging_and_production(self) -> None:
        slug = self._name("units")
        log, fn, ty, trg = (f"LIVE_{self.token}_{tag}" for tag in ("ULOG", "FN", "TY", "TR"))
        self._sql_on_both(f"""
            create table {log} (id number generated by default on null as identity primary key, msg varchar2(200));
            create or replace function {fn} return number is begin return 1; end;
            /
            create or replace type {ty} as object (id number, member function total return number)
            /
            create or replace type body {ty} as
              member function total return number is begin return id; end;
            end;
            /
            create or replace trigger {trg} before insert on {log} for each row
            begin null; end;
            /
        """, drops=(
            _drop_table(log),
            _drop_if_present("function", fn, (-4043,)),
            _drop_if_present("type", ty, (-4043,)),
            _drop_if_present("trigger", trg, (-4080,)),
        ))
        self._feature(slug)
        captured = [
            *self._capture_unit(slug, "function", fn),
            *self._capture_unit(slug, "type", ty),
            *self._capture_unit(slug, "trigger", trg),
        ]
        self.assertEqual([Path(path).name.rsplit(".", 2)[-2] for path in captured], ["fnc", "tps", "tpb", "trg"])
        function = self.project / next(path for path in captured if path.endswith(".fnc.sql"))
        function.write_text(function.read_text(encoding="utf-8").replace("return 1;", "return 2;", 1), encoding="utf-8")
        trigger = self.project / next(path for path in captured if path.endswith(".trg.sql"))
        trigger.write_text(trigger.read_text(encoding="utf-8").replace("begin null; end;", "begin null; null; end;", 1), encoding="utf-8")
        (self.project / "features" / slug / SCHEMA / "checks.json").write_text(json.dumps({
            "schemaVersion": 1, "preconditions": [],
            "postconditions": [
                {"id": "function-valid", "expected": 1, "sql": (
                    f"select count(*) from user_objects where object_name = '{fn}' and object_type = 'FUNCTION' and status = 'VALID'")},
                {"id": "type-body-valid", "expected": 1, "sql": (
                    f"select count(*) from user_objects where object_name = '{ty}' and object_type = 'TYPE BODY' and status = 'VALID'")},
                {"id": "trigger-enabled", "expected": 1, "sql": (
                    f"select count(*) from user_triggers where trigger_name = '{trg}' and status = 'ENABLED'")},
            ],
        }), encoding="utf-8")
        script = self._script("mark.sql", f"insert into {log} (msg) values ('ran');\ncommit;\n")
        self._manifest([
            {"type": "feature-migrate", "schema": SCHEMA, "inputs": captured},
            {"type": "sql-script", "file": script, "schema": SCHEMA},
        ])
        _commit(self.project, "live checks: stored unit kinds")
        capsule = self._freeze()

        staging = self._staged(capsule)
        self.assertTrue(staging["objectBudget"]["coverageComplete"])

        result, _ = self._rollout("prod", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, production = self._rollout("prod", capsule=capsule, stage_report=staging["reportPath"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(production["status"], "success")

        ran = f"select count(*) from {log} where msg = 'ran'"
        enabled = f"select count(*) from user_triggers where trigger_name = '{trg}' and status = 'ENABLED'"
        databases = [STAGING_DB, CONNECTION] if STAGING_DB != CONNECTION else [CONNECTION]
        for connection in databases:
            self.assertEqual(self._count(f"select {fn} from dual", connection), 2)
            self.assertEqual(self._count(enabled, connection), 1)
        if STAGING_DB != CONNECTION:
            self.assertEqual(self._count(ran, STAGING_DB), 1)
            self.assertEqual(self._count(ran), 1)
        else:
            self.assertEqual(self._count(ran), 2)

        payload = Path(capsule).name.split("-")[0]
        cleanup = self._team("feature", "clean-capsule", payload)
        self.assertEqual(cleanup.returncode, 0, cleanup.stdout + cleanup.stderr)
        self.assertTrue(json.loads(cleanup.stdout)["eligible"])

    def test_feature_table_then_migration_alter_through_staging_and_production(self) -> None:
        if STAGING_DB == CONNECTION:
            self.skipTest("set TEMPLATE_LIVE_STAGING_CONNECTION: the migration adds a column, which one database cannot take twice")
        slug = self._name("mig")
        table = f"LIVE_{self.token}_MT"
        self._register_drops(STAGING_DB, _drop_table(table))
        self._register_drops(CONNECTION, _drop_table(table))
        self._feature(slug)
        ddl = self.project / "features" / slug / SCHEMA / "ddl.sql"
        ddl.parent.mkdir(parents=True, exist_ok=True)
        ddl.write_text(f"create table {table} (id number primary key);\n", encoding="utf-8")
        self._checks(slug, f"select count(*) from user_tables where table_name = '{table}'")
        folder = f"migrations/{SCHEMA}/2026-11-11_live{self.token.lower()}-add-note-r001"
        migration = self.project / folder
        migration.mkdir(parents=True)
        (migration / "001-add-note.sql").write_text(f"alter table {table} add (note varchar2(40));\n", encoding="utf-8")
        (migration / "checks.json").write_text(json.dumps({
            "schemaVersion": 1, "preconditions": [],
            "postconditions": [{
                "id": "note-column",
                "sql": f"select count(*) from user_tab_columns where table_name = '{table}' and column_name = 'NOTE'",
                "expected": 1,
            }],
        }), encoding="utf-8")
        self._manifest([
            {"type": "feature-migrate", "schema": SCHEMA, "inputs": [f"features/{slug}/{SCHEMA}/ddl.sql"]},
            {"type": "migrate", "folders": [folder]},
        ])
        _commit(self.project, "live checks: feature table then migration")
        capsule = self._freeze()

        # The table does not exist yet when staging is prepared, so the migration preflight defers to the feature step.
        staging = self._staged(capsule)
        self.assertTrue(staging["objectBudget"]["coverageComplete"])
        self.assertEqual(staging["objectBudget"]["includedMigrationSteps"], [2])

        result, _ = self._rollout("prod", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, production = self._rollout("prod", capsule=capsule, stage_report=staging["reportPath"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(production["status"], "success")
        self.assertEqual(production["objectBudget"]["includedMigrationSteps"], [2])
        noted = f"select count(*) from user_tab_columns where table_name = '{table}' and column_name = 'NOTE'"
        self.assertEqual(self._count(noted, STAGING_DB), 1)
        self.assertEqual(self._count(noted), 1)

        payload = Path(capsule).name.split("-")[0]
        cleanup = self._team("feature", "clean-capsule", payload)
        self.assertEqual(cleanup.returncode, 0, cleanup.stdout + cleanup.stderr)
        self.assertTrue(json.loads(cleanup.stdout)["eligible"])

    def test_ownership_move_refresh_and_doctor_against_a_real_schema(self) -> None:
        first, second, third = self._name("own"), self._name("mov"), self._name("dup")
        package = f"LIVE_{self.token}_OWN"
        spec = f"create or replace package {package} as\n  function ping return number;\n{{extra}}end {package};\n/\n"
        body = (f"create or replace package body {package} as\n  function ping return number is begin return 1; end;\n{{extra}}"
                f"end {package};\n/\n")
        self._sql(CONNECTION, spec.format(extra="") + body.format(extra=""), drops=(_drop_package(package),))
        self._feature(first)
        captured = self._capture(first, package)
        capture_args = ["feature", "capture", second, "--package", package, "--schema", SCHEMA, "--env", "dev"]

        # The file equals DEV: the live doctor compares both files and finds nothing.
        result = self._team("feature", "doctor", "--live", "--feature", first)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("2 stored source file(s) compared with DEV", result.stdout)

        # A teammate compiles a new function on DEV: the file is now behind DEV.
        self._sql(CONNECTION, spec.format(extra="  function pong return number;\n")
                  + body.format(extra="  function pong return number is begin return 2; end;\n"))
        result = self._team("feature", "doctor", "--live", "--feature", first)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("BEHIND DEV", result.stdout)
        self.assertEqual(self._team("feature", "doctor", "--feature", first).returncode, 0)

        # Another feature cannot capture the package silently; a move takes the DEV version.
        self._feature(second)
        result = self._team(*capture_args)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"--move-from {first}", result.stderr)
        result = self._team(*capture_args, "--move-from", first)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for relative in captured:
            self.assertFalse((self.project / relative).exists(), relative)
        moved = self.project / "features" / second / SCHEMA / "packages" / f"{package.lower()}.pkb.sql"
        self.assertIn("function pong", moved.read_text(encoding="utf-8").lower())
        result = self._team("feature", "doctor", "--live", "--feature", second)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        # An edit DEV does not have stops a refresh and a move, and --discard-unapplied erases it knowingly.
        moved.write_text(moved.read_text(encoding="utf-8").replace("return 1;", "null; return 1;", 1), encoding="utf-8")
        result = self._team("feature", "doctor", "--live", "--feature", second)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("AHEAD OF DEV", result.stdout)
        result = self._team(*capture_args, "--refresh")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("DEV does not have", result.stderr)
        self.assertIn("null; return 1;", moved.read_text(encoding="utf-8"))
        result = self._team(*capture_args, "--refresh", "--discard-unapplied")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("null; return 1;", moved.read_text(encoding="utf-8"))

        # The same package in a second feature is reported, and a freeze that selects it stops before a capsule exists.
        shutil.copytree(self.project / "features" / second, self.project / "features" / third)
        result = self._team("feature", "doctor", "--feature", third)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("DOUBLE OWNERSHIP", result.stdout)
        inputs = [f"features/{third}/{SCHEMA}/packages/{package.lower()}.pks.sql", f"features/{third}/{SCHEMA}/packages/{package.lower()}.pkb.sql"]
        self._checks(third, self._valid_body_check(package))
        self._manifest([{"type": "feature-migrate", "schema": SCHEMA, "inputs": inputs}])
        _commit(self.project, "live checks: duplicate ownership")
        result = self._team("feature", "freeze", "releases/current.json")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("DOUBLE OWNERSHIP", result.stdout)
        self.assertNotIn("Capsule:", result.stdout)

    def test_production_refuses_without_a_capsule_or_stage_proof(self) -> None:
        slug = self._name("refuse")
        log, fresh = f"LIVE_{self.token}_REFUSE", f"LIVE_{self.token}_REFUSEF"
        self._sql(CONNECTION, f"create table {log} (msg varchar2(200));", drops=(_drop_table(log), _drop_table(fresh)))
        self._feature(slug)
        # A release needs a feature checkpoint or a migrate step (phase 2), so the release carries one DDL checkpoint.
        (self.project / "features" / slug / SCHEMA / "ddl.sql").write_text(f"create table {fresh} (id number);\n", encoding="utf-8")
        self._checks(slug, f"select count(*) from user_tables where table_name = '{fresh}'")
        script = self._script("refuse.sql", f"insert into {log} (msg) values ('prod');\ncommit;\n")
        self._manifest([
            {"type": "feature-migrate", "schema": SCHEMA, "inputs": [f"features/{slug}/{SCHEMA}/ddl.sql"]},
            {"type": "sql-script", "file": script, "schema": SCHEMA},
        ])
        _commit(self.project, "live checks: refusal")

        result, _ = self._rollout("prod")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires a frozen release capsule", result.stdout + result.stderr)
        self.assertEqual(self._count(f"select count(*) from {log}"), 0)

        capsule = self._freeze()
        result, _ = self._rollout("prod", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, _ = self._rollout("prod", capsule=capsule)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires --stage-report", result.stdout + result.stderr)
        self.assertEqual(self._count(f"select count(*) from {log}"), 0)

    def test_ddl_with_a_missing_prerequisite_is_refused_at_prepare(self) -> None:
        slug = self._name("ddl")
        early = f"LIVE_{self.token}_EARLY"
        self._feature(slug)
        (self.project / "features" / slug / SCHEMA / "ddl.sql").write_text(
            f"create table {early} (id number);\n"
            f"create index LIVE_{self.token}_IX on LIVE_{self.token}_MISSING (id);\n",
            encoding="utf-8",
        )
        self._checks(slug, f"SELECT CASE WHEN EXISTS (SELECT 1 FROM user_tables WHERE table_name = '{early}') "
                           "THEN 1 ELSE 0 END FROM dual")
        self._manifest([{"type": "feature-migrate", "schema": SCHEMA,
                         "inputs": [f"features/{slug}/{SCHEMA}/ddl.sql"]}])
        _commit(self.project, "live checks: ddl preflight")
        capsule = self._freeze()

        result, _ = self._rollout("staging", capsule=capsule, prepare=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing prerequisite table", result.stdout + result.stderr)
        self.assertEqual(self._count(f"select count(*) from user_tables where table_name = '{early}'", STAGING_DB), 0)

    def _owns_public_synonym_privilege(self, connection: str) -> bool:
        sql = "select count(*) from session_privs where privilege in ('CREATE PUBLIC SYNONYM', 'DROP PUBLIC SYNONYM')"
        return self._count(sql, connection) == 2

    def _expect_forms(self, connection: str, names: dict, public: bool) -> None:
        """Read every modelled effect back from the catalog of one database."""
        schema = SCHEMA
        checks = {
            "primary key on the new parent": f"select count(*) from user_constraints where table_name = '{names['parent']}' "
                f"and constraint_name = 'PK_{names['parent']}' and constraint_type = 'P' and status = 'ENABLED' and validated = 'VALIDATED'",
            "named unique key": f"select count(*) from user_constraints where constraint_name = 'UQ_{names['parent']}' "
                "and constraint_type = 'U' and status = 'ENABLED'",
            "named foreign key with its rule and parent": f"select count(*) from user_constraints f join user_constraints p "
                f"on p.constraint_name = f.r_constraint_name where f.constraint_name = 'FK_{names['child']}' "
                f"and f.delete_rule = 'CASCADE' and p.table_name = '{names['parent']}'",
            "named check": f"select count(*) from user_constraints where constraint_name = 'CK_{names['child']}' "
                "and constraint_type = 'C' and status = 'ENABLED'",
            "unnamed unique key on two columns": f"select count(*) from (select c.constraint_name from user_constraints c "
                f"join user_cons_columns k on k.constraint_name = c.constraint_name where c.table_name = '{names['child']}' "
                "and c.constraint_type = 'U' and c.generated = 'GENERATED NAME' group by c.constraint_name having count(*) = 2)",
            "primary key added to a table that already existed": f"select count(*) from user_constraints "
                f"where table_name = '{names['existing']}' and constraint_type = 'P' and constraint_name = 'PK_{names['existing']}'",
            "table comment with a quote": f"select count(*) from user_tab_comments where table_name = '{names['child']}' "
                "and comments = 'children, it''s ok'",
            "column comment": f"select count(*) from user_col_comments where table_name = '{names['child']}' "
                "and column_name = 'FLAG' and comments = 'Y or N'",
            "Arabic comment survives the hex check path": f"select count(*) from user_col_comments where table_name = '{names['existing']}' "
                "and column_name = 'NOTE' and comments = unistr('\\0645\\0648\\0631\\062F')",
            "private synonym": f"select count(*) from user_synonyms where synonym_name = '{names['alias']}' "
                f"and table_name = '{names['child']}' and table_owner = '{schema}'",
            "view with CTE": f"select count(*) from user_views where view_name = '{names['view']}'",
            "altered column data type and not null": f"select count(*) from user_tab_columns where table_name = '{names['existing']}' "
                "and column_name = 'CODE' and data_type = 'VARCHAR2' and char_length = 50 and nullable = 'N'",
            "altered column widened without null change": f"select count(*) from user_tab_columns where table_name = '{names['existing']}' "
                "and column_name = 'NOTE' and data_type = 'VARCHAR2' and char_length = 100",
            "expression index with expression count": f"select count(*) from user_indexes i "
                f"join user_ind_expressions e on e.index_name = i.index_name "
                f"where i.index_name = '{names['expr_ix']}' and i.table_name = '{names['child']}' and i.uniqueness = 'UNIQUE'",
            "unqualified sequence grant": f"select count(*) from user_tab_privs where table_name = '{names['seq']}' "
                "and privilege = 'SELECT' and grantee = 'PUBLIC'",
            "direct table grant": f"select count(*) from user_tab_privs where table_name = '{names['parent']}' "
                "and privilege = 'SELECT' and grantee = 'PUBLIC'",
        }
        if public:
            checks["public synonym"] = (
                f"select count(*) from all_synonyms where owner = 'PUBLIC' and synonym_name = '{names['public']}' "
                f"and table_name = '{names['child']}' and table_owner = '{schema}'"
            )
        for label, sql in checks.items():
            with self.subTest(database=connection, effect=label):
                self.assertEqual(self._count(sql, connection), 1, label)

    def test_constraints_comments_and_synonyms_ship_through_a_complete_capsule(self) -> None:
        slug = self._name("forms")
        token = self.token
        names = {
            "parent": f"LIVE_{token}_FP", "child": f"LIVE_{token}_FC", "existing": f"LIVE_{token}_FE",
            "alias": f"LIVE_{token}_FA", "public": f"LIVE_{token}_FPUB",
            "view": f"LIVE_{token}_FV", "expr_ix": f"IX_{token}_FEX", "seq": f"LIVE_{token}_FSEQ",
        }
        public = self._owns_public_synonym_privilege(CONNECTION) and self._owns_public_synonym_privilege(STAGING_DB)
        s = SCHEMA
        # A table that exists on both databases before the release, which the release then alters.
        self._sql_on_both(
            f"create table {names['existing']} (id number not null, code varchar2(20), note varchar2(40));",
            drops=(_drop_if_present("table", f"{names['existing']} cascade constraints purge", (-942,)),),
        )
        databases = [CONNECTION] if STAGING_DB == CONNECTION else [CONNECTION, STAGING_DB]
        for connection in databases:
            self._register_drops(
                connection,
                _drop_if_present("table", f"{names['parent']} cascade constraints purge", (-942,)),
                _drop_if_present("table", f"{names['child']} cascade constraints purge", (-942,)),
                _drop_if_present("view", names["view"], (-942,)),
                _drop_if_present("sequence", names["seq"], (-2289,)),
                _drop_synonym(names["alias"]),
                *((_drop_synonym(names["public"], public=True),) if public else ()),
            )
        statements = [
            f"CREATE TABLE {s}.{names['parent']} (ID NUMBER NOT NULL, CODE VARCHAR2(20) NOT NULL);",
            f"CREATE TABLE {s}.{names['child']} (ID NUMBER NOT NULL, P_ID NUMBER, FLAG VARCHAR2(1), NOTE VARCHAR2(40));",
            f"CREATE SEQUENCE {s}.{names['seq']};",
            f"ALTER TABLE {s}.{names['parent']} ADD CONSTRAINT PK_{names['parent']} PRIMARY KEY (ID);",
            f"ALTER TABLE {s}.{names['parent']} ADD CONSTRAINT UQ_{names['parent']} UNIQUE (CODE);",
            f"ALTER TABLE {s}.{names['child']} ADD CONSTRAINT FK_{names['child']} FOREIGN KEY (P_ID) "
            f"REFERENCES {s}.{names['parent']} (ID) ON DELETE CASCADE;",
            f"ALTER TABLE {s}.{names['child']} ADD CONSTRAINT CK_{names['child']} CHECK (FLAG IN ('Y','N'));",
            f"ALTER TABLE {s}.{names['child']} ADD UNIQUE (P_ID, NOTE);",
            f"ALTER TABLE {s}.{names['existing']} ADD CONSTRAINT PK_{names['existing']} PRIMARY KEY (ID);",
            f"ALTER TABLE {s}.{names['existing']} MODIFY (CODE VARCHAR2(50 BYTE) NOT NULL, NOTE VARCHAR2(100));",
            f"CREATE UNIQUE INDEX {s}.{names['expr_ix']} ON {s}.{names['child']} (UPPER(NOTE), ID);",
            f"CREATE VIEW {s}.{names['view']} AS WITH CTE AS (SELECT ID, CODE FROM {s}.{names['parent']}) SELECT ID, CODE FROM CTE;",
            f"COMMENT ON TABLE {s}.{names['child']} IS 'children, it''s ok';",
            f"COMMENT ON COLUMN {s}.{names['child']}.FLAG IS 'Y or N';",
            f"COMMENT ON COLUMN {s}.{names['existing']}.NOTE IS '\u0645\u0648\u0631\u062f';",
            f"CREATE SYNONYM {s}.{names['alias']} FOR {s}.{names['child']};",
            f"GRANT SELECT ON {names['seq']} TO PUBLIC;",
            f"GRANT SELECT ON {s}.{names['parent']} TO PUBLIC;",
        ]
        if public:
            statements.append(f"CREATE PUBLIC SYNONYM {names['public']} FOR {s}.{names['child']};")
        self._feature(slug)
        (self.project / "features" / slug / SCHEMA / "ddl.sql").write_text("\n".join(statements) + "\n", encoding="utf-8")
        self._checks(
            slug,
            "SELECT CASE WHEN (SELECT COUNT(*) FROM user_constraints WHERE constraint_name IN "
            f"('PK_{names['parent']}', 'FK_{names['child']}', 'CK_{names['child']}')) = 3 THEN 1 ELSE 0 END FROM dual",
        )
        self._manifest([{"type": "feature-migrate", "schema": SCHEMA, "inputs": [f"features/{slug}/{SCHEMA}/ddl.sql"]}])
        _commit(self.project, "live checks: modelled DDL forms")
        capsule = self._freeze()

        staging = self._staged(capsule)
        budget = staging["objectBudget"]
        self.assertTrue(budget["coverageComplete"], "every statement is inside the feature checkpoint")
        self.assertEqual(budget["excludedObjectSteps"], [])
        self._expect_forms(STAGING_DB, names, public)

        if STAGING_DB == CONNECTION:
            # One database already holds this release's one-time DDL, so production must refuse to apply it again.
            result, _ = self._rollout("prod", capsule=capsule, prepare=True)
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("object namespace is occupied", result.stdout + result.stderr)
            return

        result, _ = self._rollout("prod", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, production = self._rollout("prod", capsule=capsule, stage_report=staging["reportPath"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(production["status"], "success")
        self._expect_forms(CONNECTION, names, public)

    def test_occupied_constraint_name_and_missing_parent_are_refused_at_prepare_before_any_write(self) -> None:
        token = self.token
        table, created = f"LIVE_{token}_OZ", f"LIVE_{token}_NEW"
        self._sql_on_both(
            f"create table {table} (id number not null, flag varchar2(1));\n"
            f"alter table {table} add constraint CK_{table} check (flag in ('Y','N'));",
            drops=(_drop_if_present("table", f"{table} cascade constraints purge", (-942,)),),
        )
        for label, extra, expected in (
            ("occupied name", f"ALTER TABLE {SCHEMA}.{table} ADD CONSTRAINT CK_{table} CHECK (ID > 0);", f"CK_{table}"),
            ("missing parent",
             f"ALTER TABLE {SCHEMA}.{table} ADD CONSTRAINT FK_{table} FOREIGN KEY (ID) REFERENCES {SCHEMA}.LIVE_{token}_GONE (ID);",
             "LIVE_" + token + "_GONE"),
        ):
            with self.subTest(case=label):
                slug = self._name("refuse")
                self._feature(slug)
                (self.project / "features" / slug / SCHEMA / "ddl.sql").write_text(
                    f"CREATE TABLE {SCHEMA}.{created}_{label[:3].upper()} (ID NUMBER);\n{extra}\n", encoding="utf-8",
                )
                self._checks(slug, f"SELECT CASE WHEN EXISTS (SELECT 1 FROM user_tables WHERE table_name = '{created}') THEN 1 ELSE 0 END FROM dual")
                self._manifest([{"type": "feature-migrate", "schema": SCHEMA, "inputs": [f"features/{slug}/{SCHEMA}/ddl.sql"]}])
                _commit(self.project, f"live checks: refusal {label}")
                capsule = self._freeze()
                result, _ = self._rollout("staging", capsule=capsule, prepare=True)
                self.assertNotEqual(result.returncode, 0, label)
                self.assertIn(expected, result.stdout + result.stderr)
                self.assertEqual(
                    self._count(f"select count(*) from user_tables where table_name like '{created}%'", STAGING_DB), 0,
                    "prepare refused before any write",
                )

    def _generated(self, ddl: str, tables, columns, **kwargs):
        """The generated before/after checks for DDL against a synthetic catalog of the named tables."""
        import test_feature_preflight as fixtures  # noqa: PLC0415 - tests/ is on sys.path under discover
        from scripts import feature_preflight  # noqa: PLC0415
        state = fixtures._state(tables=tables, columns=columns, **kwargs)
        plan = feature_preflight.prepare_checkpoint(fixtures._checkpoint(ddl), state, ())
        return plan.preconditions, plan.postconditions

    def _value(self, check) -> int:
        return self._count(check.sql.replace(":target_schema", f"'{SCHEMA}'"))

    def test_generated_constraint_and_comment_checks_tell_a_match_from_a_mismatch(self) -> None:
        token = self.token
        n = lambda tag: f"LIVE_{token}_{tag}"  # noqa: E731
        parent, other, child = n("DP"), n("DO"), n("DC")
        for table in (child, other, parent):
            self._register_drops(CONNECTION, _drop_if_present("table", f"{table} cascade constraints purge", (-942,)))
        self._register_drops(CONNECTION,
            _drop_if_present("view", n("DV_CTE"), (-942,)),
            _drop_if_present("sequence", n("DSEQ"), (-2289,)),
        )
        self._sql(CONNECTION, f"""
            create table {parent} (id number not null, constraint {n('DPPK')} primary key (id));
            create table {other} (id number not null, constraint {n('DOPK')} primary key (id));
            create table {child} (id number, p_id number, a number, b number, flag varchar2(1), note varchar2(20));
            alter table {child} add constraint {n('DFK_NOACT')} foreign key (p_id) references {parent} (id);
            alter table {child} add constraint {n('DUQ_AB')} unique (a, b);
            alter table {child} add constraint {n('DCK_OFF')} check (flag in ('Y','N')) disable;
            alter table {child} add constraint {n('DCK_NOVAL')} check (id > 0) enable novalidate;
            comment on column {child}.note is 'exact text';
            alter table {child} modify (flag varchar2(10));
            create index {n('DIX_EXPR')} on {child} (upper(note));
            create view {n('DV_CTE')} as with cte as (select id from {parent}) select id from cte;
            create sequence {n('DSEQ')};
            grant select on {n('DSEQ')} to public;
        """)
        columns = ((child, "ID"), (child, "P_ID"), (child, "A"), (child, "B"), (child, "FLAG"), (child, "NOTE"),
                   (parent, "ID"), (other, "ID"))
        tables = (child, parent, other)
        sequences = (n("DSEQ"),)
        alter = f"ALTER TABLE APP_CODE.{child} ADD CONSTRAINT"
        # (what the DDL claims, what the database must answer to the generated after-check)
        after_cases = [
            ("foreign key: same rule and parent", f"{alter} {n('DFK_NOACT')} FOREIGN KEY (P_ID) REFERENCES APP_CODE.{parent} (ID);", 1),
            ("foreign key: rule differs", f"{alter} {n('DFK_NOACT')} FOREIGN KEY (P_ID) REFERENCES APP_CODE.{parent} (ID) ON DELETE CASCADE;", 0),
            ("foreign key: parent differs", f"{alter} {n('DFK_NOACT')} FOREIGN KEY (P_ID) REFERENCES APP_CODE.{other} (ID);", 0),
            ("foreign key: column differs", f"{alter} {n('DFK_NOACT')} FOREIGN KEY (ID) REFERENCES APP_CODE.{parent} (ID);", 0),
            ("unique: same ordered columns", f"{alter} {n('DUQ_AB')} UNIQUE (A, B);", 1),
            ("unique: column order differs", f"{alter} {n('DUQ_AB')} UNIQUE (B, A);", 0),
            ("unique: fewer columns", f"{alter} {n('DUQ_AB')} UNIQUE (A);", 0),
            ("type differs (name is a unique key)", f"{alter} {n('DUQ_AB')} PRIMARY KEY (A, B);", 0),
            ("disabled check claimed disabled", f"{alter} {n('DCK_OFF')} CHECK (FLAG IN ('Y','N')) DISABLE;", 1),
            ("disabled check claimed enabled", f"{alter} {n('DCK_OFF')} CHECK (FLAG IN ('Y','N'));", 0),
            ("novalidate claimed novalidate", f"{alter} {n('DCK_NOVAL')} CHECK (ID > 0) ENABLE NOVALIDATE;", 1),
            ("novalidate claimed validated", f"{alter} {n('DCK_NOVAL')} CHECK (ID > 0);", 0),
            ("absent constraint", f"{alter} {n('DNOPE')} CHECK (ID > 0);", 0),
            ("comment: same text", f"COMMENT ON COLUMN APP_CODE.{child}.NOTE IS 'exact text';", 1),
            ("comment: text differs", f"COMMENT ON COLUMN APP_CODE.{child}.NOTE IS 'exact tex';", 0),
            ("comment: clearing claimed on a commented column", f"COMMENT ON COLUMN APP_CODE.{child}.NOTE IS '';", 0),
            ("comment: clearing claimed on an uncommented column", f"COMMENT ON COLUMN APP_CODE.{child}.FLAG IS '';", 1),
            ("table comment absent", f"COMMENT ON TABLE APP_CODE.{child} IS 'x';", 0),
            ("modify column: same type, length, nullability", f"ALTER TABLE APP_CODE.{child} MODIFY (FLAG VARCHAR2(10));", 1),
            ("modify column: length differs", f"ALTER TABLE APP_CODE.{child} MODIFY (FLAG VARCHAR2(20));", 0),
            ("modify column: nullability differs", f"ALTER TABLE APP_CODE.{child} MODIFY (FLAG VARCHAR2(10) NOT NULL);", 0),
            ("expression index: same expression and uniqueness", f"CREATE INDEX APP_CODE.{n('DIX_EXPR')} ON APP_CODE.{child} (UPPER(NOTE));", 1),
            ("expression index: uniqueness differs", f"CREATE UNIQUE INDEX APP_CODE.{n('DIX_EXPR')} ON APP_CODE.{child} (UPPER(NOTE));", 0),
            ("expression index: column count differs", f"CREATE INDEX APP_CODE.{n('DIX_EXPR')} ON APP_CODE.{child} (UPPER(NOTE), ID);", 0),
            ("view with CTE: view exists and valid", f"CREATE VIEW APP_CODE.{n('DV_CTE')} AS WITH CTE AS (SELECT ID FROM APP_CODE.{parent}) SELECT ID FROM CTE;", 1),
            ("view with CTE: absent view", f"CREATE VIEW APP_CODE.{n('DNO_VIEW')} AS WITH CTE AS (SELECT ID FROM APP_CODE.{parent}) SELECT ID FROM CTE;", 0),
            ("grant sequence: match", f"GRANT SELECT ON APP_CODE.{n('DSEQ')} TO PUBLIC;", 1),
            ("grant sequence: privilege differs", f"GRANT ALTER ON APP_CODE.{n('DSEQ')} TO PUBLIC;", 0),
            ("grant sequence: grantee differs", f"GRANT SELECT ON APP_CODE.{n('DSEQ')} TO APP_CODE;", 0),
        ]
        for label, ddl, expected in after_cases:
            with self.subTest(after=label):
                _, post = self._generated(ddl, tables, columns, sequences=sequences)
                self.assertEqual(self._value(post[0]), expected, label)
        # The before-state check must refuse an occupied name or an existing identical key.
        before_cases = [
            ("free constraint name", f"{alter} {n('DFRESH')} CHECK (ID > 0);", 1),
            ("occupied constraint name", f"{alter} {n('DCK_OFF')} CHECK (ID > 5);", 0),
            ("unnamed unique repeats an existing key", f"ALTER TABLE APP_CODE.{child} ADD UNIQUE (A, B);", 0),
            ("unnamed unique on a new column set", f"ALTER TABLE APP_CODE.{child} ADD UNIQUE (A, FLAG);", 1),
            ("primary key on a keyless table", f"ALTER TABLE APP_CODE.{child} ADD CONSTRAINT {n('DNEWPK')} PRIMARY KEY (ID);", 1),
            ("primary key on a table that has one", f"ALTER TABLE APP_CODE.{parent} ADD CONSTRAINT {n('DPK2')} PRIMARY KEY (ID);", 0),
            ("comment on a missing column", f"COMMENT ON COLUMN APP_CODE.{child}.NOSUCH IS 'x';", None),
            ("modify: existing table and column", f"ALTER TABLE APP_CODE.{child} MODIFY (FLAG VARCHAR2(10));", 1),
            ("expression index: free index name", f"CREATE INDEX APP_CODE.{n('DFRESH_IX')} ON APP_CODE.{child} (UPPER(NOTE));", 1),
            ("expression index: occupied index name", f"CREATE INDEX APP_CODE.{n('DIX_EXPR')} ON APP_CODE.{child} (UPPER(NOTE));", 0),
            ("view: dependencies available", f"CREATE VIEW APP_CODE.{n('DFRESH_V')} AS WITH CTE AS (SELECT ID FROM APP_CODE.{parent}) SELECT ID FROM CTE;", 1),
        ]
        for label, ddl, expected in before_cases:
            with self.subTest(before=label):
                if expected is None:
                    with self.assertRaises(Exception):  # noqa: B017 - the static plan refuses it first
                        self._generated(ddl, tables, columns, sequences=sequences)
                    continue
                pre, _ = self._generated(ddl, tables, columns, sequences=sequences)
                self.assertEqual(self._value(pre[0]), expected, label)

    def test_view_dependencies_match_what_oracle_records(self) -> None:
        """For many view shapes the extracted dependency set equals ALL_DEPENDENCIES, so no table is missed."""
        from scripts import feature_ddl_forms as forms  # noqa: PLC0415

        token, SCHEMA_NAME = self.token, SCHEMA
        T = lambda name: f"LVT{token}_{name}"  # noqa: E731
        adv = [
         ("plain join", f"SELECT a.id FROM {T('A')} a JOIN {T('B')} b ON b.id=a.id"),
         ("comma join 3", f"SELECT 1 x FROM {T('A')} a, {T('B')} b, {T('C')} c WHERE a.id=b.id"),
         ("cte chain", f"WITH x AS (SELECT id FROM {T('A')}), y AS (SELECT x.id FROM x JOIN {T('B')} b ON b.id=x.id) SELECT id FROM y"),
         ("cte shadows a real name", f"WITH {T('A')} AS (SELECT 1 id FROM {T('B')}) SELECT id FROM {T('A')}"),
         ("inline view", f"SELECT v.id i1, b.id i2 FROM (SELECT id FROM {T('A')}) v JOIN {T('B')} b ON b.id=v.id"),
         ("scalar subquery depth1", f"SELECT (SELECT MAX(id) FROM {T('B')}) m FROM {T('A')}"),
         ("scalar subquery in NVL", f"SELECT NVL((SELECT MAX(id) FROM {T('B')}),0) m FROM {T('A')}"),
         ("subquery depth 3 in functions", f"SELECT NVL(UPPER(TRIM((SELECT MAX(name) FROM {T('B')}))),'a') m FROM {T('A')}"),
         ("subquery depth 4", f"SELECT COALESCE(LOWER(UPPER(SUBSTR((SELECT MAX(name) FROM {T('B')}),1,3))),'a') m FROM {T('A')}"),
         ("subquery in CASE in function", f"SELECT NVL(CASE WHEN EXISTS (SELECT 1 FROM {T('B')}) THEN 1 END,0) m FROM {T('A')}"),
         ("exists", f"SELECT id FROM {T('A')} a WHERE EXISTS (SELECT 1 FROM {T('B')} b WHERE b.id=a.id)"),
         ("not in nested", f"SELECT id FROM {T('A')} WHERE id NOT IN (SELECT id FROM {T('B')} WHERE id IN (SELECT id FROM {T('C')}))"),
         ("parenthesised join", f"SELECT a.id i1, b.id i2 FROM ({T('A')} a JOIN {T('B')} b ON a.id=b.id)"),
         ("parenthesised join + more", f"SELECT a.id i1, b.id i2 FROM ({T('A')} a JOIN {T('B')} b ON a.id=b.id) JOIN {T('C')} c ON c.id=a.id"),
         ("extract from", f"SELECT EXTRACT(YEAR FROM d) y FROM {T('A')}"),
         ("trim from", f"SELECT TRIM(BOTH ' ' FROM name) n FROM {T('A')}"),
         ("union all", f"SELECT id FROM {T('A')} UNION ALL SELECT id FROM {T('B')} UNION SELECT id FROM {T('C')}"),
         ("union in parens", f"SELECT id FROM ((SELECT id FROM {T('A')}) UNION ALL (SELECT id FROM {T('B')}))"),
         ("left join using", f"SELECT id, a.name n1, b.name n2 FROM {T('A')} a LEFT JOIN {T('B')} b USING (id)"),
         ("cross join", f"SELECT a.id i1, b.id i2 FROM {T('A')} a CROSS JOIN {T('B')} b"),
         ("analytic", f"SELECT id, ROW_NUMBER() OVER (PARTITION BY g ORDER BY id) rn FROM {T('A')}"),
         ("subquery in ON", f"SELECT a.id i1, b.id i2 FROM {T('A')} a JOIN {T('B')} b ON b.id=a.id AND b.k IN (SELECT k FROM {T('C')})"),
         ("subquery in select of cte", f"WITH x AS (SELECT (SELECT MAX(id) FROM {T('B')}) m FROM {T('A')}) SELECT m FROM x"),
         ("having subquery", f"SELECT g, COUNT(*) c FROM {T('A')} GROUP BY g HAVING COUNT(*) > (SELECT COUNT(*) FROM {T('B')})"),
         ("order by subquery", f"SELECT id FROM {T('A')} a ORDER BY (SELECT MAX(id) FROM {T('B')})"),
         ("comma join with inline view", f"SELECT a.id i1, b.id i2 FROM {T('A')} a, (SELECT id FROM {T('B')}) b WHERE a.id=b.id"),
         ("comma join after JOIN", f"SELECT a.id i1, b.id i2 FROM {T('A')} a JOIN {T('B')} b ON a.id=b.id, {T('C')} c"),
         ("table alias with AS", f"SELECT x.id FROM {T('A')} x"),
         ("schema qualified", f"SELECT a.id i1, b.id i2 FROM {SCHEMA_NAME}.{T('A')} a, {SCHEMA_NAME}.{T('B')} b"),
         ("only dual", "SELECT 1 x FROM dual"),
         ("subquery from dual + table", f"SELECT (SELECT 1 FROM dual) x, id FROM {T('A')}"),
         ("keep: WITH inside subquery", f"SELECT q.id i1, b.id i2 FROM (WITH z AS (SELECT id FROM {T('A')}) SELECT id FROM z) q JOIN {T('B')} b ON b.id=q.id"),
         ("paren join first table only", f"SELECT a.id FROM ({T('A')} a)"),
         ("select depth 5", f"SELECT NVL(UPPER(TRIM(LOWER(SUBSTR((SELECT MAX(name) FROM {T('B')}),1,3)))),'a') m FROM {T('A')}"),
         ("select inside case inside function inside function", f"SELECT NVL(TO_CHAR(CASE WHEN id > 0 THEN (SELECT MAX(id) FROM {T('B')}) END),'0') m FROM {T('A')}"),
         ("in-list of function with subquery", f"SELECT id FROM {T('A')} WHERE id IN (NVL((SELECT MAX(id) FROM {T('B')}),0), 1)"),
         ("nested parens select", f"SELECT * FROM {T('A')} WHERE id = ((SELECT MAX(id) FROM {T('B')}))"),
         ("listagg within group", f"SELECT LISTAGG(name, ',') WITHIN GROUP (ORDER BY id) l FROM {T('A')}"),
         ("scalar subq in function arg depth 2 wrapper", f"SELECT TO_CHAR(NVL((SELECT MAX(id) FROM {T('B')}),0)) t FROM {T('A')}"),
         ("subquery in IN list expression", f"SELECT id FROM {T('A')} WHERE (id, g) IN (SELECT id, g FROM {T('B')})"),
         ("decode with subquery", f"SELECT DECODE(g,1,(SELECT MAX(id) FROM {T('B')}),2,(SELECT MIN(id) FROM {T('C')}),0) d FROM {T('A')}"),
        ]
        tables = [T(name) for name in "ABC"]
        views = [f"LVV{token}_{number}" for number in range(len(adv))]
        for table in tables:
            self._register_drops(CONNECTION, _drop_table(table))
        for view in views:
            self._register_drops(CONNECTION, _drop_if_present("view", view, (-942,)))
        script = [f"create table {table} (id number, name varchar2(30), g number, d date, k number);" for table in tables]
        script += [f"create or replace view {view} as {query};" for view, (_, query) in zip(views, adv, strict=True)]
        self._sql(CONNECTION, "\n".join(script))
        output = self._sql(
            CONNECTION,
            "set heading off feedback off pagesize 0 linesize 400\n"
            f"select 'DEP|' || name || '|' || referenced_name from user_dependencies where name like 'LVV{token}%' "
            "and referenced_type in ('TABLE', 'VIEW', 'SYNONYM');\n"
            f"select 'OK|' || object_name from user_objects where object_name like 'LVV{token}%' and status = 'VALID';\n",
        )
        truth: dict[str, set[str]] = {}
        valid: set[str] = set()
        for line in output.splitlines():
            parts = line.strip().split("|")
            if parts[0] == "DEP" and len(parts) == 3 and parts[2] != "DUAL":
                truth.setdefault(parts[1], set()).add(parts[2])
            elif parts[0] == "OK":
                valid.add(parts[1])
        for view, (label, query) in zip(views, adv, strict=True):
            with self.subTest(shape=label):
                self.assertIn(view, valid, f"Oracle did not accept the shape: {label}")
                modelled = forms._view(f"CREATE OR REPLACE VIEW X AS {query}", SCHEMA)
                self.assertIsNotNone(modelled, f"a shape Oracle accepts was refused: {label}")
                self.assertEqual(truth.get(view, set()), {name.upper() for _, name in modelled["dependencies"]}, label)

    def test_abrupt_termination_mid_apply_is_recovered_after_reconciliation(self) -> None:
        slug = self._name("kill")
        package = f"LIVE_{self.token}_KILL"
        self._sql(CONNECTION, f"""
            create or replace package {package} as
              function ping return number;
            end {package};
            /
            create or replace package body {package} as
              function ping return number is begin return 1; end;
            end {package};
            /
        """, drops=(_drop_package(package),))
        self._feature(slug)
        captured = self._capture(slug, package)
        body = self.project / next(path for path in captured if path.endswith(".pkb.sql"))
        body.write_text(body.read_text(encoding="utf-8").replace("return 1;", "return 3;", 1), encoding="utf-8")
        self._checks(slug, self._valid_body_check(package))
        self._manifest([{"type": "feature-migrate", "schema": SCHEMA, "inputs": captured}])
        _commit(self.project, "live checks: kill and recover")
        capsule = self._freeze()
        result, _ = self._rollout("staging", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        # Start the run, then kill its whole process group once an attempt is mid-write.
        report = f"scratch/live-staging-{self._next_number()}.json"
        process = subprocess.Popen(
            [str(self.project / "scripts" / "team.sh"), "rollout", "releases/current.json", "--env", "staging",
             "--capsule", capsule, "--report", report, "-y"],
            cwd=self.project, env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        attempt = self._wait_for_attempting(process)
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=120)
        # Killing the client does not stop a DDL the server is already running. Wait for the
        # package to stop changing before reconciling, or recovery captures a moving target.
        self._wait_until_settled(package, connection=STAGING_DB)

        result = self._team("rollout", "releases/current.json", "--env", "staging", "--prepare",
                            "--capsule", capsule, "--recover", attempt, env=self._ack_env(), stdin=f"RECONCILE {attempt}\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, recovered = self._rollout("staging", capsule=capsule)
        self.assertEqual(result.returncode, 0, f"{result.stdout}{result.stderr}\nreport: {recovered}")
        self.assertEqual(recovered["status"], "success")

    def test_two_schemas_on_one_database_apply_as_two_checkpoints(self) -> None:
        if not SECOND_SCHEMA:
            self.skipTest("set TEMPLATE_LIVE_SECOND_SCHEMA and TEMPLATE_LIVE_SECOND_CONNECTION")
        slug = self._name("twoschema")
        first, second = f"LIVE_{self.token}_A", f"LIVE_{self.token}_B"
        self._feature(slug)
        self._feature(slug, SECOND_SCHEMA)
        for schema, table in ((SCHEMA, first), (SECOND_SCHEMA, second)):
            (self.project / "features" / slug / schema / "ddl.sql").write_text(
                f"create table {table} (id number);\n", encoding="utf-8")
            self._checks(slug, f"SELECT CASE WHEN EXISTS (SELECT 1 FROM user_tables WHERE table_name = '{table}') "
                               "THEN 1 ELSE 0 END FROM dual", schema)
        self._register_drops(CONNECTION, _drop_table(first))
        self._register_drops(SECOND_CONNECTION, _drop_table(second))
        self._manifest([
            {"type": "feature-migrate", "schema": SCHEMA, "inputs": [f"features/{slug}/{SCHEMA}/ddl.sql"]},
            {"type": "feature-migrate", "schema": SECOND_SCHEMA,
             "inputs": [f"features/{slug}/{SECOND_SCHEMA}/ddl.sql"]},
        ])
        _commit(self.project, "live checks: two schemas")
        capsule = self._freeze()

        staging = self._staged(capsule)
        self.assertEqual(len(staging["steps"]), 2)

    def test_noop_checkpoint_is_refused_while_an_unresolved_attempt_occupies_the_scope(self) -> None:
        # The live package already matches the frozen source, so the staging checkpoint is a
        # no-op. An attempt left unresolved on the same target must still block it at the
        # checkpoint level. The earlier live run only reached the rollout-level refusal.
        from scripts import feature_release, release_capsule, release_ownership, release_state

        slug = self._name("noop")
        package = f"LIVE_{self.token}_NOOP"
        self._sql_on_both(f"""
            create or replace package {package} as
              function ping return number;
            end {package};
            /
            create or replace package body {package} as
              function ping return number is begin return 1; end;
            end {package};
            /
        """, drops=(_drop_package(package),))
        self._feature(slug)
        captured = self._capture(slug, package)
        self._checks(slug, self._valid_body_check(package))
        self._manifest([{"type": "feature-migrate", "schema": SCHEMA, "inputs": captured}])
        _commit(self.project, "live checks: no-op checkpoint with an unresolved attempt")
        capsule = self._freeze()
        result, _ = self._rollout("staging", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        # The synthetic attempt below must not outlive this test: the other scenarios share this checkout.
        root = self.project
        index_path = root / "releases" / ".local-state.json"
        index_before = index_path.read_bytes() if index_path.exists() else None

        def restore_index() -> None:
            if index_before is None:
                index_path.unlink(missing_ok=True)
            else:
                index_path.write_bytes(index_before)

        self.addCleanup(restore_index)
        loaded = release_capsule.load_capsule(root, Path(capsule))
        plan = feature_release.load_target_plan(root, loaded, "staging")
        # Prove the premise: every stored unit already matches the frozen source on staging.
        source_guards = plan.checkpoint_guards[0]["sourceGuards"]
        self.assertTrue(source_guards)
        self.assertTrue(all(guard["noOp"] is True for guard in source_guards))
        run_id = release_state.start_rollout(root, loaded, plan)
        scope = release_state.target_scope_from_identity(plan.checkpoint_guards[0]["targetIdentity"])
        identity = {
            "runId": run_id, "environment": plan.environment,
            "payloadDigest": loaded.payload_digest, "engineDigest": loaded.engine_digest,
            "planDigest": plan.plan_digest, "checkpoint": 1,
            "recoveryAttempt": plan.recovery_attempt,
        }
        with release_ownership.target_writer_lease(root, scope, identity) as lease:
            release_state.record_attempt(root, loaded, plan, 1, run_id=run_id, lease=lease)

        # The in-process call reads the same profile values the CLI reads from PROJECT_ENV_FILE.
        profile = dict(line.split("=", 1) for line in _env_text().splitlines() if "=" in line)
        with self.assertRaisesRegex(feature_release.FeatureReleaseError, "unresolved attempt occupies this physical target"):
            feature_release.execute_checkpoint(root, loaded, plan, 1, run_id=run_id, environ=profile)

        index = release_state._load_index(root)
        self.assertEqual([row for row in index["checkpointVerifications"] if row["runId"] == run_id], [])
        self.assertEqual(self._count(
            f"select count(*) from user_source where name = '{package}' and type = 'PACKAGE BODY' "
            "and text like '%return 1;%'", STAGING_DB), 1)

    # Migrate-step, reconciliation, unit-capture, trigger and convert-plan scenarios ---------------

    def _migration(self, tag: str, steps: dict[str, str], *, preconditions: tuple = (), postconditions: tuple = ()) -> str:
        """Write a dated migration folder with its numbered steps and checks.json; return its project-relative path."""
        folder = f"migrations/{SCHEMA}/2026-11-11_live{self.token.lower()}-{tag}-r001"
        path = self.project / folder
        path.mkdir(parents=True, exist_ok=True)
        for name, text in steps.items():
            (path / name).write_text(text, encoding="utf-8")
        (path / "checks.json").write_text(json.dumps({
            "schemaVersion": 1, "preconditions": list(preconditions), "postconditions": list(postconditions),
        }), encoding="utf-8")
        return folder

    def _run_row(self, run_id: str) -> dict:
        index = json.loads((self.project / "releases" / ".local-state.json").read_text(encoding="utf-8"))
        return next(row for row in index["rollouts"] if row["runId"] == run_id)

    def _tree_digests(self) -> dict[str, str]:
        """Digest of every file in the copy, except git, the virtual environment and bytecode caches."""
        digests = {}
        for path in sorted(self.project.rglob("*")):
            relative = path.relative_to(self.project)
            if path.is_file() and not {".git", ".venv", "__pycache__"} & set(relative.parts):
                digests[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        return digests

    def test_migration_only_capsule_ships_through_staging_and_production_and_refuses_recover(self) -> None:
        if STAGING_DB == CONNECTION:
            self.skipTest("set TEMPLATE_LIVE_STAGING_CONNECTION: the migration creates a table, which one database cannot take twice")
        table = f"LIVE_{self.token}_MO"
        self._register_drops(STAGING_DB, _drop_table(table))
        self._register_drops(CONNECTION, _drop_table(table))
        folder = self._migration("mig-only", {"001-create-table.sql": f"create table {table} (id number primary key);\n"},
                                 postconditions=({"id": "table", "sql": f"select count(*) from user_tables where table_name = '{table}'",
                                                  "expected": 1},))
        self._manifest([{"type": "migrate", "folders": [folder]}])
        _commit(self.project, "live checks: migration-only capsule")
        capsule = self._freeze()
        freeze_record = json.loads((self.project / "scratch" / "freeze-checks" / f"{Path(capsule).name}.json").read_text(encoding="utf-8"))
        self.assertTrue(freeze_record["migrationOnly"])
        self.assertEqual(freeze_record["migrationSteps"], 1)

        staging = self._staged(capsule)
        budget = staging["objectBudget"]
        self.assertEqual(budget["includedCheckpoints"], [])
        self.assertEqual(budget["includedMigrationSteps"], [1])
        self.assertTrue(budget["coverageComplete"])
        self.assertEqual(self._count(f"select count(*) from user_tables where table_name = '{table}'", STAGING_DB), 1)

        # A migration-only capsule has no checkpoint attempt to recover: the refusal writes nothing.
        result, _ = self._rollout("staging", capsule=capsule, prepare=True, recover=str(uuid.uuid4()))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--recover is refused", result.stdout + result.stderr)
        self.assertEqual(self._count(f"select count(*) from user_tables where table_name = '{table}'", STAGING_DB), 1)

        result, _ = self._rollout("prod", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, production = self._rollout("prod", capsule=capsule, stage_report=staging["reportPath"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(production["status"], "success")
        self.assertEqual(production["objectBudget"]["includedCheckpoints"], [])
        self.assertEqual(production["objectBudget"]["includedMigrationSteps"], [1])
        self.assertEqual(self._count(f"select count(*) from user_tables where table_name = '{table}'"), 1)

        payload = Path(capsule).name.split("-")[0]
        cleanup = self._team("feature", "clean-capsule", payload)
        self.assertEqual(cleanup.returncode, 0, cleanup.stdout + cleanup.stderr)
        self.assertTrue(json.loads(cleanup.stdout)["eligible"])

    def test_migrate_step_refused_before_its_write_leaves_the_target_free(self) -> None:
        table, flag = f"LIVE_{self.token}_MR", f"LIVE_{self.token}_MRF"
        self._register_drops(STAGING_DB, _drop_table(table))
        self._sql(STAGING_DB, f"create table {flag} (ok varchar2(1));\ninsert into {flag} (ok) values ('Y');\ncommit;\n",
                  drops=(_drop_table(flag),))
        # The precondition holds when the release is prepared and fails only at apply time.
        folder = self._migration(
            "guard", {"001-create-table.sql": f"create table {table} (id number primary key);\n"},
            preconditions=({"id": "flag-open", "sql": f"select count(*) from {flag} where ok = 'Y'", "expected": 1},),
            postconditions=({"id": "table", "sql": f"select count(*) from user_tables where table_name = '{table}'", "expected": 1},),
        )
        self._manifest([{"type": "migrate", "folders": [folder]}])
        _commit(self.project, "live checks: migrate refused before its write")
        capsule = self._freeze()
        result, _ = self._rollout("staging", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        self._sql(STAGING_DB, f"update {flag} set ok = 'N';\ncommit;\n")
        result, failed = self._rollout("staging", capsule=capsule)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["steps"][0]["writeState"], "no-write-attempted")
        self.assertEqual(failed["databaseState"], "not-started")
        self.assertEqual(self._count(f"select count(*) from user_tables where table_name = '{table}'", STAGING_DB), 0)

        # The target is free: once the precondition holds again, the same capsule runs to success.
        self._sql(STAGING_DB, f"update {flag} set ok = 'Y';\ncommit;\n")
        result, _ = self._rollout("staging", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, retried = self._rollout("staging", capsule=capsule)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(retried["status"], "success")
        self.assertEqual(self._count(f"select count(*) from user_tables where table_name = '{table}'", STAGING_DB), 1)

    def test_unknown_run_without_a_checkpoint_is_reconciled_then_frees_the_target(self) -> None:
        slug = self._name("recon")
        package, log = f"LIVE_{self.token}_RCP", f"LIVE_{self.token}_RCLOG"
        self._sql_on_both(f"""
            create or replace package {package} as
              function ping return number;
            end {package};
            /
            create or replace package body {package} as
              function ping return number is begin return 1; end;
            end {package};
            /
            create table {log} (id number primary key, msg varchar2(200));
        """, drops=(_drop_package(package), _drop_table(log)))
        self._feature(slug)
        captured = self._capture(slug, package)
        self._checks(slug, self._valid_body_check(package))
        # The checkpoint changes nothing, so the run's only write is the script, which fails on a row another session left.
        script = self._script("reconcile.sql", f"insert into {log} (id, msg) values (1, 'ran');\ncommit;\n")
        self._manifest([
            {"type": "feature-migrate", "schema": SCHEMA, "inputs": captured},
            {"type": "sql-script", "file": script, "schema": SCHEMA},
        ])
        _commit(self.project, "live checks: reconcile an unknown run")
        capsule = self._freeze()
        payload = Path(capsule).name.split("-")[0]
        self._sql(STAGING_DB, f"insert into {log} (id, msg) values (1, 'blocker');\ncommit;\n")

        result, _ = self._rollout("staging", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, failed = self._rollout("staging", capsule=capsule)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["databaseState"], "unknown")
        run_id = failed["localRunId"]
        # An unknown database state makes the run's own state "unknown" (rollout.finish_local_run), which occupies its target.
        self.assertEqual(self._run_row(run_id)["state"], "unknown")

        # The run holds its target: the next run is refused, and a closed run is still not cleanup-eligible.
        result, _ = self._rollout("staging", capsule=capsule)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reconcile before reuse", result.stdout + result.stderr)

        result = self._team("feature", "reconcile-run", run_id, "--env", "staging", env=self._ack_env(), stdin="RECONCILE-RUN not-this-run\n")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("reconciliation", self._run_row(run_id))
        self.assertEqual(self._run_row(run_id)["state"], "unknown")

        result = self._team("feature", "reconcile-run", run_id, "--env", "staging", env=self._ack_env(), stdin=f"RECONCILE-RUN {run_id}\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        closed = self._run_row(run_id)
        self.assertEqual(closed["reconciliation"]["acknowledgment"], f"RECONCILE-RUN {run_id}")
        self.assertEqual(closed["reconciliation"]["priorState"], "unknown")
        self.assertEqual(closed["reconciliation"]["priorDatabaseState"], "unknown")
        self.assertFalse(json.loads(self._team("feature", "clean-capsule", payload).stdout)["eligible"])

        # The reconciled run is not completion or stage proof: production refuses the report it left.
        result, _ = self._rollout("prod", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, _ = self._rollout("prod", capsule=capsule, stage_report=failed["reportPath"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self._count(f"select count(*) from {log} where msg = 'ran'", CONNECTION), 0)

        # The target is free again once the blocker goes. Rollout receipts are never replaced, and step 1 of the
        # first manifest already has one for staging, so that capsule is refused before any write.
        self._sql(STAGING_DB, f"delete from {log} where id = 1;\ncommit;\n")
        result, _ = self._rollout("staging", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, _ = self._rollout("staging", capsule=capsule)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("receipt path is already occupied", result.stdout + result.stderr)
        self.assertEqual(self._count(f"select count(*) from {log}", STAGING_DB), 0)

        # A reviewed continuation manifest (a new script file, so a new manifest digest) prepares and succeeds.
        script = self._script("reconcile-continued.sql", f"insert into {log} (id, msg) values (1, 'ran');\ncommit;\n")
        self._manifest([
            {"type": "feature-migrate", "schema": SCHEMA, "inputs": captured},
            {"type": "sql-script", "file": script, "schema": SCHEMA},
        ])
        _commit(self.project, "live checks: continuation manifest after reconciliation")
        capsule = self._freeze()
        result, _ = self._rollout("staging", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, staging = self._rollout("staging", capsule=capsule)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(staging["status"], "success")
        self.assertEqual(self._count(f"select count(*) from {log} where msg = 'ran'", STAGING_DB), 1)
        if STAGING_DB != CONNECTION:
            result, _ = self._rollout("prod", capsule=capsule, prepare=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result, production = self._rollout("prod", capsule=capsule, stage_report=staging["reportPath"])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(production["status"], "success")
            self.assertEqual(self._count(f"select count(*) from {log} where msg = 'ran'", CONNECTION), 1)

    def test_standalone_procedure_and_function_capture_refresh_move_and_doctor_drift(self) -> None:
        first, second = self._name("ua"), self._name("ub")
        proc, fn = f"LIVE_{self.token}_PRC", f"LIVE_{self.token}_FNC"
        self._sql(CONNECTION, (
            f"create or replace procedure {proc} is begin null; end;\n/\n"
            f"create or replace function {fn} return number is begin return 1; end;\n/\n"
        ), drops=(_drop_if_present("procedure", proc, (-4043,)), _drop_if_present("function", fn, (-4043,))))
        self._feature(first)
        proc_paths = self._capture_unit(first, "procedure", proc)
        fn_paths = self._capture_unit(first, "function", fn)
        self.assertEqual([Path(path).name.rsplit(".", 2)[-2] for path in (*proc_paths, *fn_paths)], ["prc", "fnc"])
        proc_file, fn_file = self.project / proc_paths[0], self.project / fn_paths[0]

        # An edit DEV does not have: refresh refuses it, and only --discard-unapplied erases it.
        proc_file.write_text(proc_file.read_text(encoding="utf-8").replace("begin null; end;", "begin null; null; end;", 1), encoding="utf-8")
        refresh = ["feature", "capture", first, "--procedure", proc, "--schema", SCHEMA, "--env", "dev", "--refresh"]
        result = self._team(*refresh)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("DEV does not have", result.stderr)
        self.assertIn("null; null;", proc_file.read_text(encoding="utf-8"))
        result = self._team(*refresh, "--discard-unapplied")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("null; null;", proc_file.read_text(encoding="utf-8"))

        # DEV changes the function: the live doctor reports its file as out of step with DEV. The function file is
        # unedited (it equals the retained DEV baseline of its capture), so a refresh replaces it without
        # --discard-unapplied. The edited procedure above is still refused without the flag.
        self._sql(CONNECTION, f"create or replace function {fn} return number is begin return 2; end;\n/\n")
        result = self._team("feature", "doctor", "--live", "--feature", first)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(fn_paths[0], result.stdout)
        refresh_function = ["feature", "capture", first, "--function", fn, "--schema", SCHEMA, "--env", "dev", "--refresh"]
        self.assertIn("return 1", fn_file.read_text(encoding="utf-8").lower())
        result = self._team(*refresh_function)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("return 2", fn_file.read_text(encoding="utf-8").lower())
        self.assertEqual(self._team("feature", "doctor", "--live", "--feature", first).returncode, 0)

        # A second feature takes the unchanged procedure only with --move-from.
        self._feature(second)
        capture_second = ["feature", "capture", second, "--procedure", proc, "--schema", SCHEMA, "--env", "dev"]
        result = self._team(*capture_second)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"--move-from {first}", result.stderr)
        result = self._team(*capture_second, "--move-from", first)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(proc_file.exists())
        self.assertTrue((self.project / "features" / second / SCHEMA / "procedures" / proc_file.name).is_file())
        self.assertEqual(self._team("feature", "doctor", "--live", "--feature", second).returncode, 0)

    def test_disabled_trigger_is_refused_at_prepare_and_rechecked_before_apply(self) -> None:
        slug = self._name("disabled")
        log, trg = f"LIVE_{self.token}_DLOG", f"LIVE_{self.token}_DTRG"
        self._sql_on_both(
            f"create table {log} (id number, msg varchar2(200));\n"
            f"create or replace trigger {trg} before insert on {log} for each row\nbegin null; end;\n/\n",
            drops=(_drop_table(log), _drop_if_present("trigger", trg, (-4080,))),
        )
        self._feature(slug)
        trigger_file = self.project / self._capture_unit(slug, "trigger", trg)[0]
        trigger_file.write_text(trigger_file.read_text(encoding="utf-8").replace("begin null; end;", "begin null; null; end;", 1), encoding="utf-8")
        self._checks(slug, f"select count(*) from user_triggers where trigger_name = '{trg}' and status = 'ENABLED'")
        self._manifest([{"type": "feature-migrate", "schema": SCHEMA, "inputs": [f"features/{slug}/{SCHEMA}/triggers/{trigger_file.name}"]}])
        _commit(self.project, "live checks: disabled trigger")
        capsule = self._freeze()
        payload = Path(capsule).name.split("-")[0]
        disabled = f"select count(*) from user_triggers where trigger_name = '{trg}' and status = 'DISABLED'"
        replaced = f"select count(*) from user_source where name = '{trg}' and type = 'TRIGGER' and text like '%null; null;%'"

        # Disabled before prepare: refused, still disabled, source unchanged, no plan written.
        self._sql(STAGING_DB, f"alter trigger {trg} disable;\n")
        result, _ = self._rollout("staging", capsule=capsule, prepare=True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"trigger {trg} is DISABLED on staging", result.stdout + result.stderr)
        self.assertEqual(self._count(disabled, STAGING_DB), 1)
        self.assertEqual(self._count(replaced, STAGING_DB), 0)
        self.assertFalse((self.project / "scratch" / "feature-release-plans" / "staging" / f"{payload}.json").exists())

        # Enabled at prepare, disabled again before apply: the apply precondition refuses it and nothing is written.
        self._sql(STAGING_DB, f"alter trigger {trg} enable;\n")
        result, _ = self._rollout("staging", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._sql(STAGING_DB, f"alter trigger {trg} disable;\n")
        result, failed = self._rollout("staging", capsule=capsule)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(self._count(disabled, STAGING_DB), 1)
        self.assertEqual(self._count(replaced, STAGING_DB), 0)

    def test_convert_plan_reads_dated_migration_folders_offline(self) -> None:
        table = f"LIVE_{self.token}_CV"
        # checks.json needs at least one postcondition; convert-plan reads the file but never runs it.
        check = ({"id": "table", "sql": f"select count(*) from user_tables where table_name = '{table}'", "expected": 1},)
        folders = [
            self._migration("conv-a", {"001-create-table.sql": f"create table {table} (id number primary key);\n"}, postconditions=check),
            self._migration("conv-b", {"001-add-column.sql": f"alter table {table} add (note varchar2(40));\n"}, postconditions=check),
            self._migration("conv-c", {"001-drop.sql": f"drop table {table}_OLD purge;\n"}, postconditions=check),
        ]
        _commit(self.project, "live checks: convert-plan folders")
        before = self._tree_digests()
        env = {**self.env, "PYTHONDONTWRITEBYTECODE": "1"}
        text = _run([str(self.project / "scripts" / "team.sh"), "feature", "convert-plan", *folders], cwd=self.project, env=env)
        self.assertEqual(text.returncode, 0, text.stdout + text.stderr)
        self.assertIn("Summary: 3 folder(s)", text.stdout)
        as_json = _run([str(self.project / "scripts" / "team.sh"), "feature", "convert-plan", *folders, "--format", "json"],
                       cwd=self.project, env=env)
        self.assertEqual(as_json.returncode, 0, as_json.stdout + as_json.stderr)
        document = json.loads(as_json.stdout)
        self.assertEqual(document["exitCode"], 0)
        self.assertEqual(document["summary"]["folderCount"], 3)
        self.assertEqual(document["summary"]["notAnalysed"], 0)
        self.assertEqual(self._tree_digests(), before, "convert-plan must write nothing")


if __name__ == "__main__":
    unittest.main()
