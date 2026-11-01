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
        "            where object_type in ('PACKAGE', 'TABLE', 'SYNONYM') and instr(object_name, '" + token + "') > 0) loop\n"
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
        "INSTALL_UC_APX=false", "UC_APX_SKILLS_AGENT=universal",
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

    def _team(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
        return _run([str(self.project / "scripts" / "team.sh"), *args], cwd=self.project, env=self.env, stdin=stdin)

    def _feature(self, slug: str, schema: str = SCHEMA) -> None:
        result = self._team("feature", "init", slug, "--schema", schema)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def _capture(self, slug: str, package: str) -> list[str]:
        result = self._team("feature", "capture", slug, "--package", package, "--schema", SCHEMA, "--env", "dev")
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
                 stdin: str | None = None) -> tuple[subprocess.CompletedProcess, dict]:
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
        result = self._team(*args, stdin=stdin)
        path = self.project / report
        document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        document["reportPath"] = report
        return result, document

    def _staged(self, capsule: str) -> dict:
        """Prepare and run staging, assert success, and return the report document."""
        result, _ = self._rollout("staging", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, document = self._rollout("staging", capsule=capsule, stdin="y\n")
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
        result, production = self._rollout("prod", capsule=capsule, stage_report=staging["reportPath"], stdin="y\n")
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

    def test_production_refuses_without_a_capsule_or_stage_proof(self) -> None:
        slug = self._name("refuse")
        log = f"LIVE_{self.token}_REFUSE"
        self._sql(CONNECTION, f"create table {log} (msg varchar2(200));", drops=(_drop_table(log),))
        self._feature(slug)
        script = self._script("refuse.sql", f"insert into {log} (msg) values ('prod');\ncommit;\n")
        self._manifest([{"type": "sql-script", "file": script, "schema": SCHEMA}])
        _commit(self.project, "live checks: refusal")

        result, _ = self._rollout("prod")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires a frozen release capsule", result.stdout + result.stderr)
        self.assertEqual(self._count(f"select count(*) from {log}"), 0)

        capsule = self._freeze()
        result, _ = self._rollout("prod", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, _ = self._rollout("prod", capsule=capsule, stdin="y\n")
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
                _drop_synonym(names["alias"]),
                *((_drop_synonym(names["public"], public=True),) if public else ()),
            )
        statements = [
            f"CREATE TABLE {s}.{names['parent']} (ID NUMBER NOT NULL, CODE VARCHAR2(20) NOT NULL);",
            f"CREATE TABLE {s}.{names['child']} (ID NUMBER NOT NULL, P_ID NUMBER, FLAG VARCHAR2(1), NOTE VARCHAR2(40));",
            f"ALTER TABLE {s}.{names['parent']} ADD CONSTRAINT PK_{names['parent']} PRIMARY KEY (ID);",
            f"ALTER TABLE {s}.{names['parent']} ADD CONSTRAINT UQ_{names['parent']} UNIQUE (CODE);",
            f"ALTER TABLE {s}.{names['child']} ADD CONSTRAINT FK_{names['child']} FOREIGN KEY (P_ID) "
            f"REFERENCES {s}.{names['parent']} (ID) ON DELETE CASCADE;",
            f"ALTER TABLE {s}.{names['child']} ADD CONSTRAINT CK_{names['child']} CHECK (FLAG IN ('Y','N'));",
            f"ALTER TABLE {s}.{names['child']} ADD UNIQUE (P_ID, NOTE);",
            f"ALTER TABLE {s}.{names['existing']} ADD CONSTRAINT PK_{names['existing']} PRIMARY KEY (ID);",
            f"COMMENT ON TABLE {s}.{names['child']} IS 'children, it''s ok';",
            f"COMMENT ON COLUMN {s}.{names['child']}.FLAG IS 'Y or N';",
            f"COMMENT ON COLUMN {s}.{names['existing']}.NOTE IS '\u0645\u0648\u0631\u062f';",
            f"CREATE SYNONYM {s}.{names['alias']} FOR {s}.{names['child']};",
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

        result, _ = self._rollout("prod", capsule=capsule, prepare=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, production = self._rollout("prod", capsule=capsule, stage_report=staging["reportPath"], stdin="y\n")
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

    def _generated(self, ddl: str, tables, columns):
        """The generated before/after checks for DDL against a synthetic catalog of the named tables."""
        import test_feature_preflight as fixtures  # noqa: PLC0415 - tests/ is on sys.path under discover
        from scripts import feature_preflight  # noqa: PLC0415
        state = fixtures._state(tables=tables, columns=columns)
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
        self._sql(CONNECTION, f"""
            create table {parent} (id number not null, constraint {n('DPPK')} primary key (id));
            create table {other} (id number not null, constraint {n('DOPK')} primary key (id));
            create table {child} (id number, p_id number, a number, b number, flag varchar2(1), note varchar2(20));
            alter table {child} add constraint {n('DFK_NOACT')} foreign key (p_id) references {parent} (id);
            alter table {child} add constraint {n('DUQ_AB')} unique (a, b);
            alter table {child} add constraint {n('DCK_OFF')} check (flag in ('Y','N')) disable;
            alter table {child} add constraint {n('DCK_NOVAL')} check (id > 0) enable novalidate;
            comment on column {child}.note is 'exact text';
        """)
        columns = ((child, "ID"), (child, "P_ID"), (child, "A"), (child, "B"), (child, "FLAG"), (child, "NOTE"),
                   (parent, "ID"), (other, "ID"))
        tables = (child, parent, other)
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
        ]
        for label, ddl, expected in after_cases:
            with self.subTest(after=label):
                _, post = self._generated(ddl, tables, columns)
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
        ]
        for label, ddl, expected in before_cases:
            with self.subTest(before=label):
                if expected is None:
                    with self.assertRaises(Exception):  # noqa: B017 - the static plan refuses it first
                        self._generated(ddl, tables, columns)
                    continue
                pre, _ = self._generated(ddl, tables, columns)
                self.assertEqual(self._value(pre[0]), expected, label)

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
             "--capsule", capsule, "--report", report],
            cwd=self.project, env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )
        process.stdin.write("y\n")
        process.stdin.flush()
        attempt = self._wait_for_attempting(process)
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=120)
        # Killing the client does not stop a DDL the server is already running. Wait for the
        # package to stop changing before reconciling, or recovery captures a moving target.
        self._wait_until_settled(package, connection=STAGING_DB)

        result = self._team("rollout", "releases/current.json", "--env", "staging", "--prepare",
                            "--capsule", capsule, "--recover", attempt, stdin=f"RECONCILE {attempt}\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result, recovered = self._rollout("staging", capsule=capsule, stdin="y\n")
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


if __name__ == "__main__":
    unittest.main()
