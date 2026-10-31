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
        f"STAGING_SCHEMA={','.join(schemas)}", f"STAGING_SQLCL_CONNECTION={','.join(connections)}",
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

    def _register_drops(self, connection: str, *statements: str) -> None:
        # Cleanup runs in reverse registration order, so later objects go first.
        self.drops.extend((connection, statement) for statement in statements)

    def _count(self, sql: str) -> int:
        output = self._sql(CONNECTION, f"set heading off feedback off\n{sql};\n")
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

    def _wait_until_settled(self, package: str, *, quiet_seconds: int = 10, limit_seconds: int = 180) -> None:
        """Wait until the package body's DDL timestamp and status stop changing for quiet_seconds."""
        query = (f"select to_char(last_ddl_time, 'YYYYMMDDHH24MISS') || ':' || status from user_objects "
                 f"where object_name = '{package}' and object_type = 'PACKAGE BODY'")
        deadline = time.monotonic() + limit_seconds
        previous, stable_since = None, time.monotonic()
        while time.monotonic() < deadline:
            current = self._sql(CONNECTION, f"set heading off feedback off\n{query};\n").strip().splitlines()[-1]
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
        self._sql(CONNECTION, f"""
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
        self.assertEqual(self._count(f"select count(*) from {log} where msg = 'ran'"), 2)

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
        self.assertEqual(self._count(f"select count(*) from user_tables where table_name = '{early}'"), 0)

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
        self._wait_until_settled(package)

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
            "and text like '%return 1;%'"), 1)


if __name__ == "__main__":
    unittest.main()
