from __future__ import annotations

import csv
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from fake_sqlcl import environment as fake_environment, install
from scripts.db_targets import Target, TargetResolutionError
from scripts.read_only_query import (
    QueryError,
    build_driver,
    parse_query_output,
    prepare_query,
    resolve_query_target,
    read_query_input,
    run_query,
    validate_query,
    main,
)


def configured_values() -> dict[str, str]:
    return {
        "DB_ENVIRONMENT": "development",
        "CODE_SCHEMA": "APP,APP2",
        "CODE_SQLCL_CONNECTION": "code-dev,code-dev2",
        "CODE_EXPECTED_USER": "APP_DEPLOYER,APP2_DEPLOYER",
        "TABLES_SCHEMA": "DATA",
        "TABLES_SQLCL_CONNECTION": "tables-dev",
        "TABLES_EXPECTED_USER": "DATA_READER",
        "APEX_PARSING_SCHEMA": "APP",
        "APEX_SQLCL_CONNECTION": "apex-dev",
        "APEX_EXPECTED_USER": "APP_DEPLOYER",
        "ORDS_SCHEMA": "REST_API",
        "ORDS_SQLCL_CONNECTION": "ords-dev",
        "ORDS_EXPECTED_USER": "REST_API",
        "STAGING_SCHEMA": "APP_STAGE",
        "STAGING_SQLCL_CONNECTION": "stage-deploy",
        "STAGING_EXPECTED_USER": "APP_DEPLOYER",
        "PROD_SCHEMA": "APP_PROD",
        "PROD_SQLCL_CONNECTION": "prod-deploy",
        "PROD_EXPECTED_USER": "APP_DEPLOYER",
        "MIGRATION_SCHEMA": "APP_MIG",
        "MIGRATION_SQLCL_CONNECTION": "migration-dev",
        "MIGRATION_EXPECTED_USER": "APP_MIGRATOR",
        "STAGING_MIGRATION_SCHEMA": "APP_STAGE_MIG",
        "STAGING_MIGRATION_SQLCL_CONNECTION": "migration-stage",
        "STAGING_MIGRATION_EXPECTED_USER": "APP_MIGRATOR",
    }


class ReadOnlyQueryGateTests(unittest.TestCase):
    def test_literal_markers_quotes_quoted_identifiers_and_comments_are_data(self) -> None:
        sql = (
            'SELECT "Order Id", N\'-- ; / @ & BEGIN UPDATE SELECT\' AS n, '
            "q'[-- ; / @ & BEGIN UPDATE SELECT]' AS q, "
            "'a''; -- / @' AS ordinary FROM demo.orders o "
            "/* hidden @script; UPDATE t */ WHERE o.note = 'x'; -- trailing comment\n"
        )

        validate_query(sql)
        prepared = prepare_query(sql)

        self.assertIn('"Order Id"', prepared)
        self.assertIn("N'-- ; / @ & BEGIN UPDATE SELECT'", prepared)
        self.assertIn("q'[-- ; / @ & BEGIN UPDATE SELECT]'", prepared)
        self.assertIn("'a''; -- / @'", prepared)
        self.assertNotIn("hidden", prepared)
        self.assertNotIn("trailing comment", prepared)
        self.assertNotIn("\n", prepared)

    def test_multiline_ordinary_national_and_q_literals_become_character_expressions(self) -> None:
        sql = "SELECT 'a\r\nb\nc\rd', N'x\ny', q'<u\rv>' FROM dual"

        prepared = prepare_query(sql)

        self.assertNotIn("\r", prepared)
        self.assertNotIn("\n", prepared)
        self.assertIn("CHR(13)", prepared)
        self.assertIn("CHR(10)", prepared)
        self.assertIn("NCHR(10)", prepared)
        self.assertIn("'a'", prepared)
        self.assertIn("'b'", prepared)
        self.assertIn("'c'", prepared)
        self.assertIn("'d'", prepared)

    def test_carriage_return_line_comments_preserve_the_following_predicate(self) -> None:
        for separator in ("\r", "\r\n", "\n"):
            with self.subTest(separator=repr(separator)):
                prepared = prepare_query(f"SELECT 1 FROM dual -- note{separator}WHERE 1=0")
                self.assertIn("where 1=0", prepared.casefold())

    def test_argument_parse_errors_do_not_echo_sql_or_invalid_values(self) -> None:
        secret = "REVIEW_SENTINEL_SECRET"
        cases = (
            ["--env", "dev", "--connection", "code", "SELECT 1 FROM dual", f"SELECT '{secret}' FROM dual"],
            ["--env", f"SELECT '{secret}' FROM dual", "--connection", "code", "SELECT 1 FROM dual"],
        )
        for argv in cases:
            stderr = io.StringIO()
            with self.subTest(argv=argv), redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
                main(argv)
            rendered = stderr.getvalue()
            self.assertEqual(error.exception.code, 2)
            self.assertIn("usage:", rendered)
            self.assertIn("values suppressed", rendered)
            self.assertNotIn(secret, rendered)

    def test_comments_and_optional_final_semicolon_do_not_create_extra_statements(self) -> None:
        for sql in (
            "SELECT 1 FROM dual;",
            "SELECT 1 /* ; UPDATE t */ FROM dual -- ; DELETE t\n;",
            "SELECT 1 FROM dual",
        ):
            with self.subTest(sql=sql):
                validate_query(sql)

    def test_conservative_select_forms_keep_ctes_joins_and_approved_builtins_available(self) -> None:
        for sql in (
            "SELECT COUNT(*), NVL(o.amount, 0), TO_CHAR(SYSDATE, 'YYYY') FROM demo.orders o WHERE o.id IN (1, 2)",
            "WITH recent AS (SELECT id FROM demo.orders WHERE status = 'OPEN') SELECT r.id FROM recent r",
            "SELECT EXISTS (SELECT 1 FROM demo.orders o WHERE o.id = 1) FROM dual",
            "SELECT SUM(amount) OVER (PARTITION BY customer_id ORDER BY created_at) FROM demo.orders",
            "SELECT LISTAGG(name, ',') WITHIN GROUP (ORDER BY name) FROM demo.orders",
        ):
            with self.subTest(sql=sql):
                validate_query(sql)

    def test_read_only_gate_rejects_dangerous_and_unrecognized_forms(self) -> None:
        invalid = (
            "UPDATE t SET x = 1",
            "DELETE FROM t",
            "INSERT INTO t VALUES (1)",
            "MERGE INTO t USING s ON (1=1) WHEN MATCHED THEN UPDATE SET x=1",
            "CREATE TABLE t (x NUMBER)",
            "BEGIN NULL; END;",
            "WITH FUNCTION f RETURN NUMBER IS BEGIN RETURN 1; END; SELECT f() FROM dual",
            "WITH PROCEDURE p IS BEGIN NULL; END; SELECT 1 FROM dual",
            "SELECT * FROM t FOR UPDATE",
            "SELECT sequence_name.NEXTVAL FROM dual",
            'SELECT sequence_name."CURRVAL" FROM dual',
            "SELECT value FROM t@remote_db",
            "SELECT DBMS_LOCK.SLEEP(1) FROM dual",
            "SELECT DBMS_RANDOM.VALUE FROM dual",
            "SELECT owner_function(value) FROM t",
            "SELECT t.user_method(value) FROM t",
            "SELECT made_up_builtin(value) FROM t",
            "SELECT AS(1) FROM dual",
            "SELECT FILTER(1) FROM dual",
            "SELECT OVER(1) FROM dual",
            "SELECT WITHIN(1) FROM dual",
            "SELECT EXISTS(1) FROM dual",
            "SELECT IN(1) FROM dual",
            "SELECT CAST(value AS custom_type) FROM t",
            "SELECT SYS.DBMS_SQL.OPEN_CURSOR FROM dual",
            "SELECT 1 FROM dual; SELECT 2 FROM dual",
            "SELECT 1 FROM dual; -- comment\n UPDATE t SET x=1",
            "SELECT 1 FROM dual\n/\n",
            "CONNECT app/password",
            "SPOOL output.txt",
            "HOST echo unsafe",
        )
        for sql in invalid:
            with self.subTest(sql=sql), self.assertRaises(QueryError):
                validate_query(sql)

    def test_malformed_and_unbounded_inputs_are_refused(self) -> None:
        invalid = (
            "",
            "SELECT 'unterminated FROM dual",
            "SELECT q'[unterminated FROM dual",
            "SELECT 1 /* unterminated",
            "SELECT (1 FROM dual",
            "SELECT 1) FROM dual",
            "SELECT 1 FROM dual\x00",
            "SELECT 1 FROM dual\x01",
        )
        for sql in invalid:
            with self.subTest(sql=repr(sql)), self.assertRaises(QueryError):
                validate_query(sql)
        with self.assertRaises(QueryError):
            validate_query("SELECT '" + ("x" * 40000) + "' FROM dual")

    def test_query_target_uses_only_named_configured_dev_profiles(self) -> None:
        values = configured_values()

        self.assertEqual(
            resolve_query_target(values, "dev", "tables"),
            Target("dev", "tables-dev", "DATA_READER", "DATA", "development"),
        )
        self.assertEqual(
            resolve_query_target(values, "dev", "code", "APP2"),
            Target("dev", "code-dev2", "APP2_DEPLOYER", "APP2", "development"),
        )
        self.assertEqual(
            resolve_query_target(values, "dev", "apex"),
            Target("dev", "apex-dev", "APP_DEPLOYER", "APP", "development"),
        )
        self.assertEqual(
            resolve_query_target(values, "dev", "ords"),
            Target("dev", "ords-dev", "REST_API", "REST_API", "development"),
        )
        self.assertEqual(
            resolve_query_target(values, "dev", "migration"),
            Target("dev", "migration-dev", "APP_MIGRATOR", "APP_MIG", "development"),
        )
        with self.assertRaises(QueryError):
            resolve_query_target(values, "dev", "tables-dev")

    def test_query_target_requires_complete_identity_and_explicit_schema_when_ambiguous(self) -> None:
        values = configured_values()
        with self.assertRaises(QueryError):
            resolve_query_target(values, "dev", "code")
        values["CODE_EXPECTED_USER"] = "APP_DEPLOYER"
        with self.assertRaises((QueryError, TargetResolutionError)):
            resolve_query_target(values, "dev", "code", "UNKNOWN")
        values.pop("ORDS_EXPECTED_USER")
        with self.assertRaises(QueryError):
            resolve_query_target(values, "dev", "ords")

    def test_staging_and_prod_use_environment_profiles_and_never_fall_back_to_dev(self) -> None:
        values = configured_values()

        self.assertEqual(
            resolve_query_target(values, "staging", "code"),
            Target("staging", "stage-deploy", "APP_DEPLOYER", "APP_STAGE", "staging"),
        )
        self.assertEqual(
            resolve_query_target(values, "prod", "migration"),
            Target("prod", "prod-deploy", "APP_DEPLOYER", "APP_PROD", "production"),
        )
        with self.assertRaises(QueryError):
            resolve_query_target(values, "prod", "ords")
        values.pop("PROD_SQLCL_CONNECTION")
        with self.assertRaises((QueryError, TargetResolutionError)):
            resolve_query_target(values, "prod", "code")

    def test_migration_profile_falls_back_only_when_the_environment_triple_is_absent(self) -> None:
        values = configured_values()
        for key in (
            "MIGRATION_SCHEMA", "MIGRATION_SQLCL_CONNECTION", "MIGRATION_EXPECTED_USER",
            "STAGING_MIGRATION_SCHEMA", "STAGING_MIGRATION_SQLCL_CONNECTION", "STAGING_MIGRATION_EXPECTED_USER",
        ):
            values.pop(key)
        self.assertEqual(
            resolve_query_target(values, "dev", "migration", "APP"),
            Target("dev", "code-dev", "APP_DEPLOYER", "APP", "development"),
        )
        self.assertEqual(
            resolve_query_target(values, "staging", "migration"),
            Target("staging", "stage-deploy", "APP_DEPLOYER", "APP_STAGE", "staging"),
        )
        values["MIGRATION_SQLCL_CONNECTION"] = "partial-profile"
        with self.assertRaises(QueryError):
            resolve_query_target(values, "dev", "migration")

    def test_driver_establishes_target_and_read_only_transaction_before_query(self) -> None:
        target = Target("dev", "code-dev", "APP_DEPLOYER", "APP", "development")
        driver = build_driver(
            target, "SELECT 'a' || CHR(10) || 'b' FROM dual", "ROQ_0123456789abcdef0123456789abcdef"
        )
        lower = driver.casefold()

        self.assertLess(lower.index("set define off"), lower.index("alter session set current_schema = app"))
        self.assertLess(
            lower.index("alter session set current_schema = app"),
            lower.index("alter session disable commit in procedure"),
        )
        self.assertLess(lower.index("alter session disable commit in procedure"), lower.index("set transaction read only"))
        self.assertLess(lower.index("set transaction read only"), lower.index("invalid_query_target"))
        self.assertLess(lower.index("invalid_query_target"), lower.index("select * from ("))
        self.assertIn("db_unique_name", lower)
        self.assertIn("service_name", lower)
        self.assertIn("regexp_replace", lower)
        self.assertIn("regexp_like", lower)
        self.assertIn("session_user')) = 'app_deployer'", lower)
        self.assertIn("rownum <= 501", lower)
        self.assertIn("exit success rollback", lower)
        self.assertNotIn("\ncommit;", "\n" + lower)
        self.assertIn("ROQ_0123456789abcdef0123456789abcdef_SUCCESS", driver)

    def test_output_requires_unique_frames_matching_identity_and_complete_row_cap(self) -> None:
        target = Target("dev", "code-dev", "APP_DEPLOYER", "APP", "development")
        marker = "ROQ_0123456789abcdef0123456789abcdef"
        output = (
            f"{marker}_IDENTITY_BEGIN\n{marker}_I:APP_DEPLOYER|APP|FREEPDB1|FREEPDB1|FREEPDB1\n{marker}_IDENTITY_END\n"
            f"{marker}_DATA_BEGIN\n1,\"one\"\n{marker}_DATA_END\n"
            f"{marker}_SUCCESS\n"
        )

        data = parse_query_output(output, marker, target)

        rows = list(csv.reader(io.StringIO(data)))
        self.assertEqual(rows, [["1", "one"]])
        with self.assertRaises(QueryError):
            parse_query_output(output.replace("APP_DEPLOYER|APP", "OTHER|APP"), marker, target)
        with self.assertRaises(QueryError):
            parse_query_output(output + f"{marker}_SUCCESS\n", marker, target)
        with self.assertRaises(QueryError):
            parse_query_output(output.replace(f"{marker}_DATA_END", "missing"), marker, target)
        with self.assertRaises(QueryError):
            parse_query_output(output.replace("FREEPDB1|FREEPDB1|FREEPDB1", "PRODDB|PROD_UNIQUE|prodsvc"), marker, target)
        with self.assertRaises(QueryError):
            parse_query_output(output.replace("APP_DEPLOYER|APP|FREEPDB1", "APP_DEPLOYER|APP|"), marker, target)

    def test_production_marker_is_allowed_only_for_a_prod_target(self) -> None:
        marker = "ROQ_0123456789abcdef0123456789abcdef"
        output = (
            f"{marker}_IDENTITY_BEGIN\n{marker}_I:APP_DEPLOYER|APP_PROD|PRODDB|PRODDB_UNIQUE|prod.service\n"
            f"{marker}_IDENTITY_END\n{marker}_DATA_BEGIN\n1\n{marker}_DATA_END\n{marker}_SUCCESS\n"
        )
        target = Target("prod", "prod-deploy", "APP_DEPLOYER", "APP_PROD", "production")

        self.assertEqual(parse_query_output(output, marker, target), "1\n")

    def test_output_refuses_a_row_count_over_the_visible_limit(self) -> None:
        target = Target("dev", "code-dev", "APP_DEPLOYER", "APP", "development")
        marker = "ROQ_0123456789abcdef0123456789abcdef"
        data = "\n".join(f"{row}" for row in range(501))
        output = (
            f"{marker}_IDENTITY_BEGIN\n{marker}_I:APP_DEPLOYER|APP|FREEPDB1|FREEPDB1|FREEPDB1\n{marker}_IDENTITY_END\n"
            f"{marker}_DATA_BEGIN\n{data}\n{marker}_DATA_END\n{marker}_SUCCESS\n"
        )
        with self.assertRaises(QueryError):
            parse_query_output(output, marker, target, max_rows=500)

    def test_query_input_requires_one_utf8_regular_file_or_inline_statement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "query.sql"
            path.write_text("SELECT 1 FROM dual", encoding="utf-8")
            self.assertEqual(read_query_input(sql=None, file=str(path)), "SELECT 1 FROM dual")
            with self.assertRaises(QueryError):
                read_query_input(sql="SELECT 1 FROM dual", file=str(path))
            path.write_bytes(b"SELECT \xff FROM dual")
            with self.assertRaises(QueryError):
                read_query_input(sql=None, file=str(path))
            if hasattr(os, "mkfifo"):
                fifo = Path(temporary) / "query.pipe"
                os.mkfifo(fifo)
                with self.assertRaises(QueryError):
                    read_query_input(sql=None, file=str(fifo))

    def test_fake_sqlcl_runs_only_after_read_only_target_guard_and_returns_framed_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seen_driver = root / "seen-driver.sql"
            fake = root / "fake_sqlcl.py"
            fake.write_text(
                "import pathlib,re,sys\n"
                "args=sys.argv[1:]\n"
                "driver=pathlib.Path(next(arg[1:] for arg in args if arg.startswith('@')))\n"
                "source=driver.read_text(encoding='utf-8')\n"
                f"pathlib.Path({str(seen_driver)!r}).write_text(source,encoding='utf-8')\n"
                "marker=re.search(r'(?m)^PROMPT (ROQ_[a-f0-9]{32})_IDENTITY_BEGIN$',source).group(1)\n"
                "user=re.search(r\"SESSION_USER'\\)\\) = '([A-Z0-9_$#]+)'\",source).group(1)\n"
                "schema=re.search(r\"CURRENT_SCHEMA'\\)\\) = '([A-Z0-9_$#]+)'\",source).group(1)\n"
                "print(marker+'_IDENTITY_BEGIN')\n"
                "print(marker+'_I:'+user+'|'+schema+'|FREEPDB1|FREEPDB1|FREEPDB1')\n"
                "print(marker+'_IDENTITY_END')\n"
                "print(marker+'_DATA_BEGIN')\n"
                "print('1,one')\n"
                "print(marker+'_DATA_END')\n"
                "print(marker+'_SUCCESS')\n",
                encoding="utf-8",
            )
            binary = install(root / "bin", f'exec "{Path(sys.executable).as_posix()}" "{fake.as_posix()}" "$@"\n')

            with patch.dict(os.environ, fake_environment(binary)):
                result = run_query(configured_values(), "dev", "code", "APP", "SELECT 1 FROM dual")

            driver = seen_driver.read_text(encoding="utf-8")
            lower = driver.casefold()
            self.assertEqual(result.strip(), "1,one")
            self.assertLess(lower.index("alter session set current_schema"), lower.index("set transaction read only"))
            self.assertLess(lower.index("alter session disable commit in procedure"), lower.index("set transaction read only"))
            self.assertLess(lower.index("set transaction read only"), lower.index("invalid_query_target"))
            self.assertLess(lower.index("invalid_query_target"), lower.index("_data_begin"))
            self.assertIn("set define off", lower)
            self.assertIn("exit success rollback", lower)
            self.assertNotIn("\ncommit;", "\n" + lower)

    def test_prepare_query_default_rejects_binds(self) -> None:
        sql = "SELECT 1 FROM dual WHERE :DEVTEST_ID = 1"
        with self.assertRaises(QueryError):
            prepare_query(sql)

    def test_prepare_query_opt_in_binds_accepts_declared_names(self) -> None:
        sql = "SELECT 1 FROM dual WHERE :DEVTEST_ID = 1 AND :DEVTEST_RUN_ID = 'x'"
        prepared = prepare_query(
            sql,
            bindings={"DEVTEST_ID": 1, "DEVTEST_RUN_ID": "x"},
        )
        self.assertIn(":DEVTEST_ID", prepared)
        self.assertIn(":DEVTEST_RUN_ID", prepared)

    def test_prepare_query_opt_in_rejects_undeclared_or_invalid_binds(self) -> None:
        # Undeclared bind name
        with self.assertRaises(QueryError):
            prepare_query(
                "SELECT 1 FROM dual WHERE :DEVTEST_UNBOUND = 1",
                bindings={"DEVTEST_ID": 1},
            )
        # Positional bind
        with self.assertRaises(QueryError):
            prepare_query(
                "SELECT 1 FROM dual WHERE :1 = 1",
                bindings={"DEVTEST_ID": 1},
            )
        # Arbitrary namespace
        with self.assertRaises(QueryError):
            prepare_query(
                "SELECT 1 FROM dual WHERE :OTHER_VAR = 1",
                bindings={"DEVTEST_ID": 1},
            )

    def test_build_driver_with_opt_in_bindings(self) -> None:
        target = Target("dev", "code-dev", "APP_DEPLOYER", "APP", "development")
        driver = build_driver(
            target,
            "SELECT 1 FROM dual WHERE :DEVTEST_ID = 1",
            "ROQ_0123456789abcdef0123456789abcdef",
            bindings={"DEVTEST_ID": 42, "DEVTEST_NAME": "test_payload"},
        )
        self.assertIn("VARIABLE DEVTEST_ID NUMBER", driver)
        self.assertIn("VARIABLE DEVTEST_NAME VARCHAR2(4000)", driver)
        self.assertIn(":DEVTEST_ID := 42;", driver)
        hex_val = b"test_payload".hex().upper()
        self.assertIn(f"HEXTORAW('{hex_val}')", driver)


if __name__ == "__main__":
    unittest.main()
