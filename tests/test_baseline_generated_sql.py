"""Regressions for the SQL that `baseline build` generates, found by tests/test_live_baseline.py against a database.

Two defects were found when the generated folders were applied for the first time: a new table's captured DDL holds
several statements, which went into one EXECUTE IMMEDIATE (ORA-00922), and the compile step declared a local function
before its variables (PLS-00103). The build modules must also import in any order.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import _no_real_sqlcl  # noqa: F401, E402 (tests must never fall through to installed SQLcl)
from scripts import baseline, baseline_bundles
from scripts.schema_catalog import ObjectDefinition, ObjectKey, SchemaSnapshot
from scripts.sqlcl_transport import lint_sqlcl_transport

ROOT = Path(__file__).resolve().parents[1]

# What DBMS_METADATA.GET_DDL('TABLE') returns on Oracle Database 23ai with the capture's transforms: the CREATE TABLE
# statement, then one ALTER TABLE statement for each key and check constraint, with no terminators.
TABLE_DDL = '''CREATE TABLE "APP_DEV"."APP_ITEMS"
   (	"ITEM_ID" NUMBER,
	"COUNTRY_ID" NUMBER NOT NULL ENABLE,
	"PRICE" NUMBER(10,2) DEFAULT 0 NOT NULL ENABLE
   )
ALTER TABLE "APP_DEV"."APP_ITEMS" ADD CONSTRAINT "APP_ITEMS_PRICE_CK" CHECK (price >= 0) ENABLE
ALTER TABLE "APP_DEV"."APP_ITEMS" ADD PRIMARY KEY ("ITEM_ID")
  USING INDEX  ENABLE
ALTER TABLE "APP_DEV"."APP_ITEMS" ADD CONSTRAINT "APP_ITEMS_FK" FOREIGN KEY ("COUNTRY_ID")
	  REFERENCES "APP_DEV"."APP_COUNTRIES" ("COUNTRY_ID") ENABLE'''


def _fk(table: str, parent: str) -> dict:
    return {"name": f"{table}_FK", "table_name": table, "constraint_type": "R", "referenced_table": parent}


class DdlStatementSplitTests(unittest.TestCase):
    def test_alternative_quoted_apostrophe_and_newline_do_not_hide_next_statement(self) -> None:
        ddl = "CREATE TABLE DEV.T (C VARCHAR2(80) DEFAULT q'[it's\nALTER TABLE XX]')\nALTER TABLE DEV.T ADD PRIMARY KEY (C)"
        statements = baseline_bundles._split_ddl_statements(ddl)
        self.assertEqual(2, len(statements))
        self.assertIn("q'[it's\nALTER TABLE XX]'", statements[0])
        self.assertTrue(statements[1].startswith("ALTER TABLE DEV.T"))

    def test_a_line_comment_at_the_end_of_a_statement_does_not_hide_the_next(self) -> None:
        ddl = "CREATE TABLE DEV.T (C NUMBER) -- captured comment\nALTER TABLE DEV.T ADD PRIMARY KEY (C)"
        statements = baseline_bundles._split_ddl_statements(ddl)
        self.assertEqual(2, len(statements))
        self.assertTrue(statements[1].startswith("ALTER TABLE DEV.T"))

    def test_a_table_ddl_splits_at_each_column_zero_alter(self) -> None:
        statements = baseline_bundles._split_ddl_statements(TABLE_DDL)

        self.assertEqual(4, len(statements))
        self.assertTrue(statements[0].startswith('CREATE TABLE "APP_DEV"."APP_ITEMS"'))
        self.assertTrue(all(text.startswith('ALTER TABLE "APP_DEV"."APP_ITEMS" ADD') for text in statements[1:]))
        self.assertIn("USING INDEX  ENABLE", statements[2], "an indented continuation line stays in its statement")
        self.assertIn('REFERENCES "APP_DEV"."APP_COUNTRIES"', statements[3])
        for text in statements:
            self.assertNotIn("\nALTER ", text)

    def test_keywords_inside_literals_and_parentheses_do_not_split(self) -> None:
        ddl = (
            "CREATE TABLE \"S\".\"T\" (\"NOTE\" VARCHAR2(20) DEFAULT 'a\nALTER TABLE b' NOT NULL,\n"
            "ALTER_COUNT NUMBER CHECK (ALTER_COUNT > 0\nAND ALTER_COUNT < 9))\n"
            "ALTER TABLE \"S\".\"T\" ADD PRIMARY KEY (\"NOTE\")"
        )

        statements = baseline_bundles._split_ddl_statements(ddl)

        self.assertEqual(2, len(statements))
        self.assertIn("'a\nALTER TABLE b'", statements[0])

    def test_terminators_blank_lines_and_a_doubled_quote_are_handled(self) -> None:
        ddl = "CREATE TABLE \"S\".\"T\" (\"A\" VARCHAR2(5) DEFAULT 'it''s')\n\n\nALTER TABLE \"S\".\"T\" ADD PRIMARY KEY (\"A\");\n"

        statements = baseline_bundles._split_ddl_statements(ddl)

        self.assertEqual(
            ['CREATE TABLE "S"."T" ("A" VARCHAR2(5) DEFAULT \'it\'\'s\')', 'ALTER TABLE "S"."T" ADD PRIMARY KEY ("A")'],
            statements,
        )

    def test_a_single_statement_is_wrapped_exactly_as_before(self) -> None:
        sql = baseline_bundles._wrap_guarded_ddl(
            "ALL_INDEXES WHERE OWNER = 'APP_STAGE' AND INDEX_NAME = 'IX'",
            'CREATE INDEX "APP_STAGE"."IX" ON "APP_STAGE"."T" ("C")\n',
        )

        self.assertEqual(
            "DECLARE\n  l_count NUMBER;\nBEGIN\n"
            "  SELECT COUNT(*) INTO l_count FROM ALL_INDEXES WHERE OWNER = 'APP_STAGE' AND INDEX_NAME = 'IX';\n"
            "  IF l_count = 0 THEN\n"
            "    EXECUTE IMMEDIATE q'~CREATE INDEX \"APP_STAGE\".\"IX\" ON \"APP_STAGE\".\"T\" (\"C\")~';\n"
            "  END IF;\nEND;\n/\n",
            sql,
        )

    def test_a_multi_statement_ddl_runs_one_statement_per_execute_immediate_inside_one_guard(self) -> None:
        sql = baseline_bundles._wrap_guarded_ddl("ALL_TABLES WHERE TABLE_NAME = 'APP_ITEMS'", TABLE_DDL)

        self.assertEqual(4, sql.count("EXECUTE IMMEDIATE"))
        self.assertEqual(1, sql.count("IF l_count = 0 THEN"))
        self.assertEqual(1, sql.count("END IF;"))
        self.assertEqual(4, sql.count("~';"), "each statement is its own q-quoted literal")


class TableStatementTests(unittest.TestCase):
    config = {"schemas": ["APP_DEV"], "prefixes": ["APP_"], "excludedObjects": [], "sequenceMappings": []}

    def _bundle(self, source_schema: str, target_schema: str, ddl: str, constraints: list[dict]) -> str:
        source = {"sections": {
            "tables": [{"name": "APP_ITEMS"}], "columns": [], "constraints": constraints, "indexes": [],
            "sequences": [], "identity-columns": [],
        }}
        target = {"sections": {"tables": [], "columns": [], "constraints": [], "indexes": [], "sequences": [], "identity-columns": []}}
        key = ObjectKey(source_schema, "APP_ITEMS", "TABLE")
        snapshot = SchemaSnapshot({}, {}, {key: ObjectDefinition(key, {}, ddl, (), True)}, {}, "", "")
        steps, _checks = baseline._structure_bundle(self.config, source_schema, target_schema, source, target, snapshot)
        return steps["001-structure-delta.sql"]

    def test_a_new_table_with_constraints_is_created_one_statement_at_a_time(self) -> None:
        sql = self._bundle("APP_DEV", "APP_DEV", TABLE_DDL, [])

        self.assertEqual(4, sql.count("EXECUTE IMMEDIATE"))
        self.assertIn("q'~CREATE TABLE \"APP_DEV\".\"APP_ITEMS\"", sql)
        self.assertIn("q'~ALTER TABLE \"APP_DEV\".\"APP_ITEMS\" ADD PRIMARY KEY (\"ITEM_ID\")\n  USING INDEX  ENABLE~'", sql)

    def test_the_alter_statements_follow_the_target_schema(self) -> None:
        sql = self._bundle("APP_DEV", "APP_STAGE", TABLE_DDL, [])

        self.assertNotIn('"APP_DEV"', sql, "no statement may touch the source schema's table")
        self.assertIn('ALTER TABLE "APP_STAGE"."APP_ITEMS" ADD PRIMARY KEY', sql)
        self.assertIn('REFERENCES "APP_STAGE"."APP_COUNTRIES" ("COUNTRY_ID")', sql)

    def test_retargeting_leaves_string_literals_alone(self) -> None:
        statement = 'ALTER TABLE "APP_DEV"."T" ADD CONSTRAINT "CK" CHECK ("C" <> \'"APP_DEV".x\')'

        result = baseline_bundles._retarget_statement(statement, "APP_DEV", "APP_STAGE")

        self.assertEqual('ALTER TABLE "APP_STAGE"."T" ADD CONSTRAINT "CK" CHECK ("C" <> \'"APP_DEV".x\')', result)
        self.assertEqual(statement, baseline_bundles._retarget_statement(statement, "APP_DEV", "APP_DEV"))

    def test_retarget_preserves_quoted_identifiers_and_maps_across_comments(self) -> None:
        sql = 'CREATE VIEW "APP_STAGE"."V" AS SELECT "PRICE APP_DEV.X" FROM APP_DEV /*keep*/ .T'
        result = baseline_bundles.retarget_sql_identifiers(sql, "APP_DEV", "APP_STAGE")
        self.assertIn('"PRICE APP_DEV.X"', result)
        self.assertIn('FROM "APP_STAGE" /*keep*/ .T', result)

    def test_view_alias_named_as_source_is_not_silently_rewritten(self) -> None:
        sql = 'CREATE VIEW "APP_STAGE"."V" AS SELECT APP_DEV.ID FROM "APP_DEV"."T" APP_DEV'
        with self.assertRaisesRegex(baseline.BaselineError, "ambiguous|review"):
            baseline_bundles.retarget_sql_identifiers(sql, "APP_DEV", "APP_STAGE")

    def test_view_comma_join_retargets_all_tables(self) -> None:
        sql = 'CREATE OR REPLACE VIEW "DEV"."V" AS SELECT a.x FROM "DEV"."A" a, "DEV"."B" b WHERE a.id = b.id'
        expected = (
            'CREATE OR REPLACE VIEW "STAGE"."V" AS SELECT a.x FROM "STAGE"."A" a, "STAGE"."B" b WHERE a.id = b.id'
        )
        self.assertEqual(expected, baseline_bundles.retarget_sql_identifiers(sql, "DEV", "STAGE"))

        sql_unquoted = "CREATE OR REPLACE VIEW DEV.V AS SELECT a.x FROM DEV.A a, DEV.B b WHERE a.id = b.id"
        expected_unquoted = (
            'CREATE OR REPLACE VIEW "STAGE".V AS SELECT a.x FROM "STAGE".A a, "STAGE".B b WHERE a.id = b.id'
        )
        self.assertEqual(expected_unquoted, baseline_bundles.retarget_sql_identifiers(sql_unquoted, "DEV", "STAGE"))

    def test_view_three_part_column_retargets_body(self) -> None:
        sql = 'CREATE OR REPLACE VIEW "DEV"."V" AS SELECT "DEV"."T"."C" AS c FROM "DEV"."T"'
        expected = 'CREATE OR REPLACE VIEW "STAGE"."V" AS SELECT "STAGE"."T"."C" AS c FROM "STAGE"."T"'
        self.assertEqual(expected, baseline_bundles.retarget_sql_identifiers(sql, "DEV", "STAGE"))

        sql_unquoted = "CREATE OR REPLACE VIEW DEV.V AS SELECT DEV.T.C AS c FROM DEV.T"
        expected_unquoted = 'CREATE OR REPLACE VIEW "STAGE".V AS SELECT "STAGE".T.C AS c FROM "STAGE".T'
        self.assertEqual(expected_unquoted, baseline_bundles.retarget_sql_identifiers(sql_unquoted, "DEV", "STAGE"))

    def test_view_sequence_expression_retargets_body(self) -> None:
        sql = 'CREATE OR REPLACE VIEW "DEV"."V" AS SELECT "DEV"."SEQ".NEXTVAL AS n FROM dual'
        expected = 'CREATE OR REPLACE VIEW "STAGE"."V" AS SELECT "STAGE"."SEQ".NEXTVAL AS n FROM dual'
        self.assertEqual(expected, baseline_bundles.retarget_sql_identifiers(sql, "DEV", "STAGE"))

        sql_unquoted = "CREATE OR REPLACE VIEW DEV.V AS SELECT DEV.SEQ.NEXTVAL AS n FROM dual"
        expected_unquoted = 'CREATE OR REPLACE VIEW "STAGE".V AS SELECT "STAGE".SEQ.NEXTVAL AS n FROM dual'
        self.assertEqual(expected_unquoted, baseline_bundles.retarget_sql_identifiers(sql_unquoted, "DEV", "STAGE"))

    def test_view_alias_style_reference_in_expression_is_refused(self) -> None:
        bad_views = [
            'CREATE OR REPLACE VIEW "DEV"."V" AS SELECT "DEV".ID FROM "DEV"."T"',
            'CREATE OR REPLACE VIEW "DEV"."V" AS SELECT a.x, "DEV".ID FROM "DEV"."T" a',
            'CREATE OR REPLACE VIEW "DEV"."V" AS SELECT a.x FROM "DEV"."T" a WHERE "DEV".ID = 1',
        ]
        for view_sql in bad_views:
            with self.subTest(sql=view_sql):
                with self.assertRaisesRegex(baseline.BaselineError, "ambiguous.*qualifier"):
                    baseline_bundles.retarget_sql_identifiers(view_sql, "DEV", "STAGE")

    def test_view_retargeting_preserves_literals_and_comments(self) -> None:
        sql = (
            'CREATE OR REPLACE VIEW "DEV"."V" AS SELECT a.x, \'DEV.X\' AS c1, q\'[it\'s DEV.X]\' AS c2 '
            'FROM "DEV"."A" a, "DEV"."B" b -- DEV.Y\n'
            'WHERE a.id = b.id /* "DEV".Z */'
        )
        expected = (
            'CREATE OR REPLACE VIEW "STAGE"."V" AS SELECT a.x, \'DEV.X\' AS c1, q\'[it\'s DEV.X]\' AS c2 '
            'FROM "STAGE"."A" a, "STAGE"."B" b -- DEV.Y\n'
            'WHERE a.id = b.id /* "DEV".Z */'
        )
        result = baseline_bundles.retarget_sql_identifiers(sql, "DEV", "STAGE")
        self.assertEqual(expected, result)
        self.assertIn("'DEV.X'", result)
        self.assertIn("q'[it's DEV.X]'", result)
        self.assertIn("-- DEV.Y", result)
        self.assertIn('/* "DEV".Z */', result)

    def test_unbalanced_opening_bracket_inside_q_quote_is_literal(self) -> None:
        sql = 'ALTER TABLE APP_DEV.T ADD CHECK (C <> q\'[unbalanced [ APP_DEV.X]\')'
        self.assertEqual(
            'ALTER TABLE "APP_STAGE".T ADD CHECK (C <> q\'[unbalanced [ APP_DEV.X]\')',
            baseline_bundles.retarget_sql_identifiers(sql, "APP_DEV", "APP_STAGE"),
        )

    def test_parents_are_created_before_the_tables_that_reference_them(self) -> None:
        constraints = {("A_CHILD", "A_CHILD_FK"): _fk("A_CHILD", "B_PARENT"), ("C_SELF", "C_SELF_FK"): _fk("C_SELF", "C_SELF")}

        order = baseline_bundles._new_table_order(["A_CHILD", "B_PARENT", "C_SELF"], constraints, "APP_DEV")

        self.assertEqual(["B_PARENT", "A_CHILD", "C_SELF"], order)
        self.assertEqual(["A", "B"], baseline_bundles._new_table_order(["A", "B"], {}, "APP_DEV"), "without foreign keys the order stays alphabetical")

    def test_a_foreign_key_to_a_table_that_already_exists_does_not_reorder(self) -> None:
        constraints = {("A_CHILD", "A_CHILD_FK"): _fk("A_CHILD", "EXISTING")}

        self.assertEqual(["A_CHILD", "B_OTHER"], baseline_bundles._new_table_order(["A_CHILD", "B_OTHER"], constraints, "APP_DEV"))

    def test_a_cycle_between_new_tables_is_refused(self) -> None:
        constraints = {("A", "A_FK"): _fk("A", "B"), ("B", "B_FK"): _fk("B", "A")}

        with self.assertRaises(baseline.BaselineError) as raised:
            baseline_bundles._new_table_order(["A", "B"], constraints, "APP_DEV")

        self.assertIn("cycle", str(raised.exception))

    def test_table_with_default_sequence_retargets_to_target_schema(self) -> None:
        raw_ddl = 'CREATE TABLE "APP_DEV"."T" ("ID" NUMBER DEFAULT "APP_DEV"."SEQ".NEXTVAL, "NAME" VARCHAR2(100));'
        statements = baseline_bundles._table_statements(raw_ddl, "APP_DEV", "APP_STAGE")
        self.assertEqual(len(statements), 1)
        self.assertIn('CREATE TABLE "APP_STAGE"."T"', statements[0])
        self.assertIn('DEFAULT "APP_STAGE"."SEQ".NEXTVAL', statements[0])
        self.assertNotIn('"APP_DEV"', statements[0])

    def test_view_referencing_source_schema_retargets_body(self) -> None:
        source_ddl = 'CREATE OR REPLACE FORCE EDITIONABLE VIEW "APP_DEV"."CUSTOMERS_V" AS SELECT ID, NAME FROM "APP_DEV"."CUSTOMERS"'
        mapped_ddl = baseline_bundles._replace_create_header(source_ddl, "VIEW", "APP_DEV", "APP_STAGE")
        mapped_ddl = baseline_bundles.retarget_sql_identifiers(mapped_ddl, "APP_DEV", "APP_STAGE")
        self.assertIn('VIEW "APP_STAGE"."CUSTOMERS_V"', mapped_ddl)
        self.assertIn('FROM "APP_STAGE"."CUSTOMERS"', mapped_ddl)
        self.assertNotIn('"APP_DEV"', mapped_ddl)

    def test_create_bitmap_index_retargets_to_target_schema(self) -> None:
        ddl = 'CREATE BITMAP INDEX "APP_DEV"."IX_STATUS" ON "APP_DEV"."T" ("STATUS");'
        mapped = baseline_bundles._replace_create_header(ddl, "INDEX", "APP_DEV", "APP_STAGE")
        mapped = baseline_bundles.retarget_sql_identifiers(mapped, "APP_DEV", "APP_STAGE")
        self.assertIn('CREATE BITMAP INDEX "APP_STAGE"."IX_STATUS" ON "APP_STAGE"."T"', mapped)
        self.assertNotIn('"APP_DEV"', mapped)

    def test_q_quoted_literals_preserve_bytes_and_delimiters(self) -> None:
        cases = [
            'ALTER TABLE "APP_DEV"."T" ADD CONSTRAINT "CK" CHECK ("C" <> q\'[it\'s APP_DEV.X]\')',
            'ALTER TABLE "APP_DEV"."T" ADD CONSTRAINT "CK" CHECK ("C" <> q\'{it\'s APP_DEV.X}\')',
            'ALTER TABLE "APP_DEV"."T" ADD CONSTRAINT "CK" CHECK ("C" <> q\'<it\'s APP_DEV.X>\')',
            'ALTER TABLE "APP_DEV"."T" ADD CONSTRAINT "CK" CHECK ("C" <> q\'(it\'s APP_DEV.X)\')',
            'ALTER TABLE "APP_DEV"."T" ADD CONSTRAINT "CK" CHECK ("C" <> q\'[nested [brackets] APP_DEV.X]\')',
            'ALTER TABLE "APP_DEV"."T" ADD CONSTRAINT "CK" CHECK ("C" <> q\'~it\'s APP_DEV.X~\')',
            'ALTER TABLE "APP_DEV"."T" ADD CONSTRAINT "CK" CHECK ("C" <> q\'!it\'s APP_DEV.X!\')',
            'ALTER TABLE "APP_DEV"."T" ADD CONSTRAINT "CK" CHECK ("C" <> q\'#it\'s APP_DEV.X#\')',
            'ALTER TABLE "APP_DEV"."T" ADD CONSTRAINT "CK" CHECK ("C" <> \'it\'\'s APP_DEV.X\')',
            'ALTER TABLE "APP_DEV"."T" ADD CONSTRAINT "CK" CHECK ("C" > 0) -- APP_DEV.X note\n',
            'ALTER TABLE "APP_DEV"."T" ADD CONSTRAINT "CK" CHECK ("C" > 0) /* APP_DEV.X block */',
        ]
        for statement in cases:
            with self.subTest(statement=statement):
                result = baseline_bundles._retarget_statement(statement, "APP_DEV", "APP_STAGE")
                self.assertIn('"APP_STAGE"."T"', result)
                # The literal / comment itself must retain the original APP_DEV text verbatim
                self.assertIn("APP_DEV.X", result)

    def test_external_schema_references_preserved(self) -> None:
        statement = 'ALTER TABLE "APP_DEV"."T" ADD CONSTRAINT "FK" FOREIGN KEY ("C") REFERENCES "OTHER"."T" ("C")'
        result = baseline_bundles._retarget_statement(statement, "APP_DEV", "APP_STAGE")
        self.assertEqual('ALTER TABLE "APP_STAGE"."T" ADD CONSTRAINT "FK" FOREIGN KEY ("C") REFERENCES "OTHER"."T" ("C")', result)

    def test_unclosed_syntax_fails_closed(self) -> None:
        bad_statements = [
            'ALTER TABLE "APP_DEV"."T" ADD CONSTRAINT "CK" CHECK ("C" <> q\'[unclosed)',
            'ALTER TABLE "APP_DEV"."T" ADD CONSTRAINT "CK" CHECK ("C" <> \'unclosed)',
            'ALTER TABLE "APP_DEV"."T" /* unclosed comment',
            'ALTER TABLE "APP_DEV"."T" ADD ("C',
        ]
        for statement in bad_statements:
            with self.subTest(statement=statement):
                with self.assertRaises(baseline.BaselineError):
                    baseline_bundles._retarget_statement(statement, "APP_DEV", "APP_STAGE")


class ColumnFlagTests(unittest.TestCase):
    """Column catalog flags decide what the structure generator may add to an existing table.

    scripts/compare_env_catalog.sql writes hidden and virtual as Y/N and user_generated as YES/NO, and other producers
    use YES/NO, so both encodings are tested. A system-generated hidden column (SYS_NC...$) is skipped. A virtual column,
    or a hidden column the user created, is refused rather than added as an ordinary column. A virtual flag that
    differs between the two schemas is refused for a column that exists in both.
    """

    config = {"schemas": ["APP_DEV"], "prefixes": ["APP_"], "excludedObjects": [], "sequenceMappings": []}

    @staticmethod
    def _column(name: str, **flags: object) -> dict:
        row = {
            "table_name": "APP_ITEMS", "name": name, "data_type": "NUMBER", "data_precision": None,
            "data_scale": None, "nullable": "Y", "hidden": "N", "virtual": "N", "user_generated": "YES",
        }
        row.update(flags)
        return row

    def _structure(self, source_columns: list[dict], target_columns: list[dict]) -> tuple[dict[str, str], dict]:
        source = {"sections": {
            "tables": [{"name": "APP_ITEMS"}], "columns": source_columns, "constraints": [], "indexes": [],
            "sequences": [], "identity-columns": [],
        }}
        target = {"sections": {
            "tables": [{"name": "APP_ITEMS"}], "columns": target_columns, "constraints": [], "indexes": [],
            "sequences": [], "identity-columns": [],
        }}
        snapshot = SchemaSnapshot({}, {}, {}, {}, "", "")
        return baseline._structure_bundle(self.config, "APP_DEV", "APP_TEST", source, target, snapshot)

    def test_a_system_hidden_column_is_skipped_for_both_flag_encodings(self) -> None:
        for hidden, user_generated in (("Y", "N"), ("YES", "NO")):
            with self.subTest(hidden=hidden, user_generated=user_generated):
                system_column = self._column(
                    "SYS_NC00005$", hidden=hidden, virtual=hidden, user_generated=user_generated, nullable="N",
                )
                steps, checks = self._structure([self._column("ITEM_ID"), system_column], [self._column("ITEM_ID")])

                self.assertEqual({}, steps)
                self.assertEqual({"preconditions": [], "postconditions": []}, checks)

    def test_a_missing_virtual_column_is_refused_and_named(self) -> None:
        for flag in ("Y", "YES"):
            with self.subTest(flag=flag):
                with self.assertRaisesRegex(
                    baseline.BaselineError, r"virtual column APP_ITEMS\.TOTAL needs a reviewed migration",
                ):
                    self._structure([self._column("ITEM_ID"), self._column("TOTAL", virtual=flag)], [self._column("ITEM_ID")])

    def test_a_missing_hidden_user_column_is_refused_and_not_guessed(self) -> None:
        for hidden, user_generated in (("Y", "YES"), ("YES", "YES"), ("Y", None)):
            with self.subTest(hidden=hidden, user_generated=user_generated):
                with self.assertRaisesRegex(
                    baseline.BaselineError, r"hidden column APP_ITEMS\.NOTE_X needs a reviewed migration",
                ):
                    self._structure(
                        [self._column("ITEM_ID"), self._column("NOTE_X", hidden=hidden, user_generated=user_generated)],
                        [self._column("ITEM_ID")],
                    )

    def test_an_existing_column_whose_virtual_flag_differs_is_refused(self) -> None:
        for source_flag, target_flag in (("Y", "N"), ("NO", "YES")):
            with self.subTest(source_flag=source_flag, target_flag=target_flag):
                with self.assertRaisesRegex(
                    baseline.BaselineError, r"column virtual/stored change needs a reviewed data migration: APP_ITEMS\.TOTAL",
                ):
                    self._structure([self._column("TOTAL", virtual=source_flag)], [self._column("TOTAL", virtual=target_flag)])

    def test_an_unrecognised_virtual_flag_is_refused(self) -> None:
        with self.assertRaisesRegex(baseline.BaselineError, "unrecognised virtual flag"):
            self._structure([self._column("ITEM_ID")], [self._column("ITEM_ID", virtual="MAYBE")])

    def test_an_ordinary_missing_column_still_gets_its_add(self) -> None:
        steps, checks = self._structure(
            [self._column("ITEM_ID"), self._column("ITEM_NAME")], [self._column("ITEM_ID")],
        )

        sql = steps["001-structure-delta.sql"]
        self.assertIn('ADD ("ITEM_NAME" NUMBER)', sql)
        self.assertNotIn("SYS_NC", sql)
        self.assertTrue(any("column-def-" in check["id"] for check in checks["postconditions"]))


class CompileStepTests(unittest.TestCase):
    def test_the_compile_block_declares_its_variables_before_the_local_function(self) -> None:
        sql = baseline._compile_all_step("APP_STAGE", [("PACKAGE", "APP_PKG")])

        declarations = sql.split("DECLARE\n", 1)[1].split("BEGIN\n", 1)[0]
        self.assertLess(declarations.index("l_name VARCHAR2"), declarations.index("FUNCTION invalid_count"))
        self.assertEqual(1, declarations.count("FUNCTION"))


class CodeBeforeStateTests(unittest.TestCase):
    def test_generated_code_checks_the_reviewed_scope_and_before_state_first(self) -> None:
        settings = {
            "plsql_optimize_level": "2", "plsql_code_type": "INTERPRETED", "plsql_debug": "FALSE",
            "plsql_warnings": "DISABLE:ALL", "plsql_ccflags": None, "nls_length_semantics": "BYTE",
            "plscope_settings": "IDENTIFIERS:NONE",
        }
        spec = {"name": "Pkg'Mixed", "type": "PACKAGE"}
        body = {"name": "Pkg'Mixed", "type": "PACKAGE BODY"}
        procedure = {"name": "Added", "type": "PROCEDURE"}
        before_view = ' \nCREATE OR REPLACE VIEW "APP_STAGE"."View Mixed" AS SELECT \'x-\n'
        before_view += '  \n' + 'ħ🌺' * 10000 + "' X FROM DUAL\n "
        source = {"sections": {
            "baseline-source": [
                {**spec, "line": 1, "text": 'PACKAGE "Pkg\'Mixed" AS PROCEDURE p; END;\n'},
                {**body, "line": 1, "text": 'PACKAGE BODY "Pkg\'Mixed" AS PROCEDURE p IS BEGIN NULL; END; END;\n'},
                {**procedure, "line": 1, "text": 'PROCEDURE "Added" AS BEGIN NULL; END;\n'},
            ],
            "baseline-settings": [{**unit, **settings} for unit in (spec, body, procedure)],
            "baseline-views": [
                {"name": "View Mixed", "ddl": 'CREATE OR REPLACE VIEW "APP_DEV"."View Mixed" AS SELECT 2 X FROM DUAL'},
                {"name": "Added View", "ddl": 'CREATE VIEW "APP_DEV"."Added View" AS SELECT 1 X FROM DUAL'},
            ],
        }}
        target = {
            "identity": {
                "session_user": "CAPTURE_LOGIN", "current_schema": "APP_STAGE", "db_name": "CDB",
                "db_unique_name": "STAGE_DB", "service_name": "STAGE_SERVICE", "container_id": "5",
                "container_name": "STAGE_PDB", "edition": "RELEASE_2", "database_version": "23.0",
            },
            "sections": {
                "baseline-source": [
                    {**spec, "line": 1, "text": 'PACKAGE "Pkg\'Mixed" AS PROCEDURE old; END;\n'},
                    source["sections"]["baseline-source"][1],
                ],
                "baseline-settings": [{**spec, **settings, "plsql_optimize_level": "1"}, {**body, **settings}],
                "baseline-views": [{"name": "View Mixed", "ddl": before_view}],
            },
        }

        steps, checks = baseline_bundles._code_bundle({}, "APP_DEV", "APP_STAGE", source, target)

        self.assertEqual(next(iter(steps)), "001-verify-code-before.sql")
        guard = steps["001-verify-code-before.sql"]
        for attribute, value in (
            ("DB_UNIQUE_NAME", "STAGE_DB"), ("CON_ID", "5"), ("CON_NAME", "STAGE_PDB"),
            ("CURRENT_SCHEMA", "APP_STAGE"), ("CURRENT_EDITION_NAME", "RELEASE_2"),
        ):
            self.assertRegex(guard, rf"(?m)^.*SYS_CONTEXT\('USERENV', '{attribute}'\).*'{value}'")
        self.assertNotIn("CAPTURE_LOGIN", guard)
        self.assertNotIn("STAGE_SERVICE", guard)
        checks_sql = "\n".join(check["sql"] for check in checks["preconditions"])
        self.assertIn("PROCEDURE old", checks_sql)
        self.assertIn("PLSQL_OPTIMIZE_LEVEL = '1'", checks_sql)
        self.assertIn("PROCEDURE p IS BEGIN", checks_sql)
        self.assertRegex(checks_sql, r"(?m)COUNT\(\*\).*'Added'.* = 0 THEN 1")
        self.assertRegex(checks_sql, r"(?m)COUNT\(\*\).*'Added View'.* = 0 THEN 1")
        self.assertIn("DBMS_METADATA.GET_DDL('VIEW', 'View Mixed', 'APP_STAGE')", guard)
        self.assertIn("DBMS_LOB.COMPARE", guard)
        guard_values = re.findall(r"append_hex\('([0-9A-F]+)'\)", guard)
        self.assertIn(before_view, b"".join(bytes.fromhex(value) for value in guard_values).decode("utf-8"))
        self.assertEqual((), lint_sqlcl_transport(guard, "direct"))
        compile_step = next(sql for name, sql in steps.items() if name.endswith("compile-all.sql"))
        self.assertIn("'Pkg''Mixed', 'PACKAGE BODY'", compile_step)
        self.assertIn("REUSE SETTINGS", compile_step)
        self.assertNotIn("ALTER SESSION SET PLSQL_", compile_step)

        # Exercise the scalar before-checks against dictionary-shaped rows.
        # Oracle-specific identity and CLOB steps are inspected separately above.
        with sqlite3.connect(":memory:") as catalog:
            catalog.executescript("""
                ATTACH DATABASE ':memory:' AS SYS;
                CREATE TABLE SYS.DUAL (dummy TEXT);
                INSERT INTO SYS.DUAL VALUES ('X');
                CREATE TABLE SYS.ALL_OBJECTS (owner TEXT, object_name TEXT, object_type TEXT, subobject_name TEXT);
                INSERT INTO SYS.ALL_OBJECTS VALUES ('APP_STAGE', 'View Mixed', 'VIEW', NULL);
                CREATE TABLE SYS.ALL_SOURCE (owner TEXT, name TEXT, type TEXT, line INTEGER, text TEXT);
                CREATE TABLE SYS.ALL_PLSQL_OBJECT_SETTINGS (
                    owner TEXT, name TEXT, type TEXT, plsql_optimize_level INTEGER, plsql_code_type TEXT,
                    plsql_debug TEXT, plsql_warnings TEXT, plsql_ccflags TEXT, nls_length_semantics TEXT, plscope_settings TEXT
                );
            """)
            for row in target["sections"]["baseline-source"]:
                catalog.execute(
                    "INSERT INTO SYS.ALL_SOURCE VALUES ('APP_STAGE', ?, ?, ?, ?)",
                    (row["name"], row["type"], row["line"], row["text"]),
                )
            for row in target["sections"]["baseline-settings"]:
                catalog.execute(
                    "INSERT INTO SYS.ALL_PLSQL_OBJECT_SETTINGS VALUES ('APP_STAGE', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (row["name"], row["type"], *(row[field] for field in settings)),
                )

            def failed_checks() -> set[str]:
                return {check["id"].split("-", 2)[1] for check in checks["preconditions"] if catalog.execute(check["sql"]).fetchone() != (1,)}

            self.assertEqual(set(), failed_checks())
            catalog.execute("UPDATE SYS.ALL_SOURCE SET text = REPLACE(text, 'old', 'OLD') WHERE type = 'PACKAGE'")
            self.assertEqual({"source"}, failed_checks())
            catalog.execute("UPDATE SYS.ALL_PLSQL_OBJECT_SETTINGS SET plsql_optimize_level = 3 WHERE type = 'PACKAGE BODY'")
            self.assertEqual({"source", "settings"}, failed_checks())
            catalog.execute("INSERT INTO SYS.ALL_OBJECTS VALUES ('APP_STAGE', 'Added', 'PROCEDURE', NULL)")
            self.assertEqual({"source", "settings", "absent"}, failed_checks())


class ImportOrderTests(unittest.TestCase):
    def test_every_baseline_module_imports_first_in_a_fresh_interpreter(self) -> None:
        for module in (
            "baseline_sql", "baseline_reference_data", "baseline_bundles", "baseline_build",
            "baseline_data", "baseline_ords_filter", "baseline",
        ):
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-B", "-c", f"import scripts.{module}"],
                    cwd=ROOT, text=True, capture_output=True, timeout=60, check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr[-600:])


def _declare_order_violations(sql: str) -> list[str]:
    """Lines of each DECLARE section that follow a local subprogram without belonging to one.

    A subprogram starts at exactly two spaces with FUNCTION or PROCEDURE and ends at its own two-space END. Inside it,
    two-space BEGIN and END lines and deeper body lines are allowed. After the first subprogram, any other declaration
    (or any deeper line outside a subprogram) is a variable or type declared after a subprogram, which Oracle rejects
    with PLS-00103.
    """
    problems: list[str] = []
    lines = sql.split("\n")
    index = 0
    while index < len(lines):
        if lines[index] != "DECLARE":
            index += 1
            continue
        index += 1
        seen_subprogram = in_subprogram = False
        while index < len(lines) and lines[index] != "BEGIN":
            line = lines[index]
            index += 1
            text = line.strip()
            indent = len(line) - len(line.lstrip(" "))
            if not text:
                continue
            if indent == 2 and text.startswith(("FUNCTION ", "PROCEDURE ")):
                seen_subprogram = in_subprogram = True
            elif in_subprogram:
                if indent == 2 and text.startswith("END"):
                    in_subprogram = False
                elif indent == 2 and text != "BEGIN":
                    problems.append(line)
            elif seen_subprogram:
                problems.append(line)
    return problems


def _constraint_row(condition: str, **extra: object) -> dict:
    return {
        "table_name": "APP_ITEMS", "name": "APP_ITEMS_PRICE_CK", "constraint_type": "C", "columns": [],
        "condition": condition, "status": "ENABLED", "validated": "VALIDATED", **extra,
    }


class DeclareOrderTests(unittest.TestCase):
    def test_the_order_helper_accepts_variables_before_subprograms(self) -> None:
        good = (
            "DECLARE\n  l_x NUMBER;\n  FUNCTION f RETURN NUMBER IS\n    l_y NUMBER := 1;\n  BEGIN\n"
            "    RETURN l_y;\n  END;\nBEGIN\n  NULL;\nEND;\n/\n"
        )
        self.assertEqual([], _declare_order_violations(good))

    def test_the_order_helper_rejects_a_variable_declared_after_a_subprogram(self) -> None:
        bad = (
            "DECLARE\n  FUNCTION f RETURN NUMBER IS\n  BEGIN\n    RETURN 1;\n  END;\n"
            "  l_x NUMBER;\nBEGIN\n  NULL;\nEND;\n/\n"
        )
        self.assertEqual(["  l_x NUMBER;"], _declare_order_violations(bad))

    def test_the_order_helper_rejects_the_old_replacement_block_shape(self) -> None:
        old_shape = (
            "DECLARE\n  FUNCTION normalized(p_value VARCHAR2) RETURN VARCHAR2 IS\n    l_result VARCHAR2(4000) := '';\n"
            "  BEGIN\n    RETURN l_result;\n  END;\n  l_status VARCHAR2(8);\n  l_condition VARCHAR2(4000);\nBEGIN\n"
            "  NULL;\nEND;\n/\n"
        )
        self.assertEqual(
            ["  l_status VARCHAR2(8);", "  l_condition VARCHAR2(4000);"], _declare_order_violations(old_shape),
        )

    def test_generated_declare_blocks_declare_variables_before_subprograms(self) -> None:
        replace_sql = baseline_bundles._wrap_constraint(
            _constraint_row("price >= 5"), "APP_DEV", "APP_DEV", replace_check=True, reviewed_condition="price >= 2",
            reviewed_status="ENABLED",
        )
        plain_sql = baseline_bundles._wrap_constraint(_constraint_row("price >= 0"), "APP_DEV", "APP_DEV")
        not_null_sql = baseline_bundles._wrap_not_null("APP_DEV", "APP_ITEMS", "PRICE")
        sequence_sql = baseline_bundles._sync_sequence_sql(
            "APP_DEV", {"name": "ITEM_SEQ", "increment_by": 1}, {"table": "APP_ITEMS", "column": "ITEM_ID"},
        )
        compile_sql = baseline._compile_all_step("APP_STAGE", [("PACKAGE", "APP_PKG")])
        for label, sql in (
            ("replace-check", replace_sql), ("plain constraint", plain_sql), ("not null", not_null_sql),
            ("sequence sync", sequence_sql), ("compile all", compile_sql),
        ):
            with self.subTest(block=label):
                self.assertIn("DECLARE", sql)
                self.assertEqual([], _declare_order_violations(sql))

    def test_code_bundle_declare_blocks_declare_variables_before_subprograms(self) -> None:
        settings = {
            "plsql_optimize_level": "2", "plsql_code_type": "INTERPRETED", "plsql_debug": "FALSE",
            "plsql_warnings": "DISABLE:ALL", "plsql_ccflags": None, "nls_length_semantics": "BYTE",
            "plscope_settings": "IDENTIFIERS:NONE",
        }
        spec = {"name": "Pkg", "type": "PACKAGE"}
        body = {"name": "Pkg", "type": "PACKAGE BODY"}
        procedure = {"name": "Added", "type": "PROCEDURE"}
        source = {"sections": {
            "baseline-source": [
                {**spec, "line": 1, "text": "PACKAGE Pkg AS PROCEDURE p; END;\n"},
                {**body, "line": 1, "text": "PACKAGE BODY Pkg AS PROCEDURE p IS BEGIN NULL; END; END;\n"},
                {**procedure, "line": 1, "text": "PROCEDURE Added AS BEGIN NULL; END;\n"},
            ],
            "baseline-settings": [{**unit, **settings} for unit in (spec, body, procedure)],
            "baseline-views": [
                {"name": "View Mixed", "ddl": 'CREATE OR REPLACE VIEW "APP_DEV"."View Mixed" AS SELECT 2 X FROM DUAL'},
            ],
        }}
        target = {
            "identity": {
                "session_user": "CAPTURE_LOGIN", "current_schema": "APP_STAGE", "db_name": "CDB",
                "db_unique_name": "STAGE_DB", "service_name": "STAGE_SERVICE", "container_id": "5",
                "container_name": "STAGE_PDB", "edition": "RELEASE_2", "database_version": "23.0",
            },
            "sections": {
                "baseline-source": [
                    {**spec, "line": 1, "text": "PACKAGE Pkg AS PROCEDURE old; END;\n"},
                    source["sections"]["baseline-source"][1],
                ],
                "baseline-settings": [{**spec, **settings, "plsql_optimize_level": "1"}, {**body, **settings}],
                "baseline-views": [{"name": "View Mixed", "ddl": 'CREATE OR REPLACE VIEW "APP_STAGE"."View Mixed" AS SELECT 1 X FROM DUAL'}],
            },
        }
        steps, _checks = baseline_bundles._code_bundle({}, "APP_DEV", "APP_STAGE", source, target)

        declaring = [name for name, sql in steps.items() if "DECLARE" in sql]
        self.assertGreaterEqual(len(declaring), 3)
        for name in declaring:
            with self.subTest(step=name):
                self.assertEqual([], _declare_order_violations(steps[name]))


class CheckReplacementTests(unittest.TestCase):
    config = {"schemas": ["APP_DEV"], "prefixes": ["APP_"], "excludedObjects": [], "sequenceMappings": []}

    def _structure(self, source_row: dict, target_row: dict) -> dict[str, str]:
        key = ObjectKey("APP_DEV", source_row["name"], "CONSTRAINT")
        table = [{"name": "APP_ITEMS"}]
        source = {"sections": {
            "tables": table, "columns": [], "constraints": [source_row], "indexes": [], "sequences": [],
            "identity-columns": [],
        }}
        target = {"sections": {
            "tables": table, "columns": [], "constraints": [target_row], "indexes": [], "sequences": [],
            "identity-columns": [],
        }}
        snapshot = SchemaSnapshot({}, {}, {key: ObjectDefinition(key, {}, "", (), True)}, {}, "", "")
        steps, _checks = baseline._structure_bundle(self.config, "APP_DEV", "APP_DEV", source, target, snapshot)
        return steps

    def test_the_replacement_block_has_the_reviewed_shape(self) -> None:
        sql = baseline_bundles._wrap_constraint(
            _constraint_row("price >= 5"), "APP_DEV", "APP_DEV", replace_check=True, reviewed_condition="price >= 2",
            reviewed_status="ENABLED",
        )

        for text in ("CONSTRAINT_TYPE", "-20993", "-20994", "-20995", "'price>=5'", "'price>=2'"):
            self.assertIn(text, sql)
        self.assertLess(sql.index("l_type VARCHAR2(1);"), sql.index("FUNCTION normalized"))
        self.assertLess(sql.index("l_condition VARCHAR2(4000);"), sql.index("FUNCTION normalized"))
        self.assertEqual([], _declare_order_violations(sql))

    def test_the_replacement_block_refuses_to_guess_the_reviewed_condition(self) -> None:
        with self.assertRaises(baseline.BaselineError):
            baseline_bundles._wrap_constraint(_constraint_row("price >= 5"), "APP_DEV", "APP_DEV", replace_check=True)

    def test_generations_that_differ_only_in_the_live_condition_produce_different_sql(self) -> None:
        source = _constraint_row("price >= 5")
        at_two = self._structure(source, _constraint_row("price >= 2"))["001-structure-delta.sql"]
        at_three = self._structure(source, _constraint_row("price >= 3"))["001-structure-delta.sql"]

        self.assertNotEqual(at_two, at_three)
        self.assertIn("'price>=2'", at_two)
        self.assertIn("'price>=3'", at_three)
        self.assertNotIn("'price>=2'", at_three)

    def test_a_changed_condition_with_alternative_quoting_needs_a_reviewed_migration(self) -> None:
        for source_condition, target_condition in (
            ("S <> q'[it's a b]'", "S <> q'[it's ab]'"),
            ("S <> q'[a b]'", "S <> q'[ab]'"),
        ):
            with self.subTest(source=source_condition, target=target_condition):
                with self.assertRaisesRegex(
                    baseline.BaselineError, "same-name constraint differences need a reviewed migration",
                ):
                    self._structure(_constraint_row(source_condition), _constraint_row(target_condition))


class ConditionKeyTests(unittest.TestCase):
    def test_only_space_tab_lf_and_cr_are_stripped_outside_strings(self) -> None:
        self.assertEqual("a\x0bb", baseline_bundles._condition_key("a\x0bb"))
        self.assertEqual("a b", baseline_bundles._condition_key("a b"))
        self.assertEqual("ab", baseline_bundles._condition_key("a \t\r\nb"))
        self.assertEqual("C='a  b'", baseline_bundles._condition_key("C = 'a  b'"))

    def test_alternative_quoted_conditions_are_returned_unchanged(self) -> None:
        value = "S <> q'[a  b]'"
        self.assertEqual(value, baseline_bundles._condition_key(value))

    def test_an_apostrophe_inside_an_alternative_quote_keeps_its_whitespace(self) -> None:
        self.assertNotEqual(
            baseline_bundles._condition_key("S <> q'[it's a b]'"),
            baseline_bundles._condition_key("S <> q'[it's ab]'"),
        )

    def test_alternative_quote_detection(self) -> None:
        for value in ("q'[x]'", "Q'(x)'", "nq'{x}'", "S <> q'[a]'"):
            with self.subTest(value=value):
                self.assertTrue(baseline_bundles._has_alternative_quote(value))
        for value in ("'abq'", "'it''s'", "faq'"):
            with self.subTest(value=value):
                self.assertFalse(baseline_bundles._has_alternative_quote(value))


def _constraint_structure(source_row: dict, target_row: dict) -> tuple[dict[str, str], dict]:
    key = ObjectKey("APP_DEV", source_row["name"], "CONSTRAINT")
    table = [{"name": "APP_ITEMS"}]
    source = {"sections": {
        "tables": table, "columns": [], "constraints": [source_row], "indexes": [], "sequences": [], "identity-columns": [],
    }}
    target = {"sections": {
        "tables": table, "columns": [], "constraints": [target_row], "indexes": [], "sequences": [], "identity-columns": [],
    }}
    snapshot = SchemaSnapshot({}, {}, {key: ObjectDefinition(key, {}, "", (), True)}, {}, "", "")
    return baseline._structure_bundle(CheckReplacementTests.config, "APP_DEV", "APP_DEV", source, target, snapshot)


class CheckReplacementPreconditionTests(unittest.TestCase):
    def _checks(self, **extra: object) -> dict:
        _steps, checks = _constraint_structure(_constraint_row("price >= 5", **extra), _constraint_row("price >= 2", **extra))
        return checks

    def test_a_validated_replacement_adds_one_precondition_that_names_the_condition(self) -> None:
        found = [item for item in self._checks()["preconditions"] if item["id"].startswith("check-replacement-no-violations-")]

        self.assertEqual(1, len(found))
        self.assertIn('FROM "APP_DEV"."APP_ITEMS" WHERE NOT (price >= 5)', found[0]["sql"])
        self.assertIn(") = 0 THEN", found[0]["sql"])

    def test_a_novalidate_or_disabled_replacement_adds_no_such_precondition(self) -> None:
        for extra in ({"validated": "NOT VALIDATED"}, {"status": "DISABLED"}):
            with self.subTest(extra=extra):
                ids = [item["id"] for item in self._checks(**extra)["preconditions"]]
                self.assertFalse([item for item in ids if item.startswith("check-replacement-no-violations-")])

    def test_the_precondition_id_is_stable(self) -> None:
        first = [item["id"] for item in self._checks()["preconditions"] if item["id"].startswith("check-replacement")]
        second = [item["id"] for item in self._checks()["preconditions"] if item["id"].startswith("check-replacement")]

        self.assertEqual(first, second)
        self.assertEqual(1, len(first))


class CheckReplacementStatusTests(unittest.TestCase):
    def test_a_disabled_desired_check_is_not_enabled_by_a_rerun(self) -> None:
        sql = baseline_bundles._wrap_constraint(
            _constraint_row("price >= 5", status="DISABLED"), "APP_DEV", "APP_DEV",
            replace_check=True, reviewed_condition="price >= 2", reviewed_status="DISABLED",
        )

        self.assertNotIn("ENABLE", sql)
        self.assertIn("DISABLE", sql)

    def test_an_enabled_desired_check_still_enables_a_disabled_live_one(self) -> None:
        sql = baseline_bundles._wrap_constraint(
            _constraint_row("price >= 5"), "APP_DEV", "APP_DEV",
            replace_check=True, reviewed_condition="price >= 2", reviewed_status="ENABLED",
        )

        self.assertIn("ENABLE VALIDATE CONSTRAINT", sql)

    def test_the_drop_branch_requires_the_reviewed_status_literal(self) -> None:
        sql = baseline_bundles._wrap_constraint(
            _constraint_row("price >= 5"), "APP_DEV", "APP_DEV",
            replace_check=True, reviewed_condition="price >= 2", reviewed_status="DISABLED",
        )

        self.assertIn("ELSIF normalized(l_condition) = 'price>=2' AND l_status = 'DISABLED' THEN", sql)

    def test_a_replacement_without_a_reviewed_status_is_refused(self) -> None:
        with self.assertRaises(baseline.BaselineError):
            baseline_bundles._wrap_constraint(
                _constraint_row("price >= 5"), "APP_DEV", "APP_DEV",
                replace_check=True, reviewed_condition="price >= 2",
            )


class CatalogLengthLimitTests(unittest.TestCase):
    def test_the_replacement_refuses_a_condition_at_the_catalog_limit_before_comparing(self) -> None:
        sql = baseline_bundles._wrap_constraint(
            _constraint_row("price >= 5"), "APP_DEV", "APP_DEV",
            replace_check=True, reviewed_condition="price >= 2", reviewed_status="ENABLED",
        )

        self.assertIn("-20996", sql)
        self.assertEqual(2, sql.count("LENGTHB"))
        self.assertIn("constraint APP_ITEMS_PRICE_CK has a condition at the 4000-byte catalog limit", sql)
        self.assertLess(sql.index("LENGTHB"), sql.index("normalized(l_condition)"))
        self.assertEqual([], _declare_order_violations(sql))


class ForeignKeyCatalogOrderTests(unittest.TestCase):
    def test_catalog_shaped_references_order_the_parent_first(self) -> None:
        constraints = {("A_CHILD", "A_CHILD_FK"): _fk("A_CHILD", "<CONFIGURED_SCHEMA>.Z_PARENT")}

        self.assertEqual(
            ["Z_PARENT", "A_CHILD"], baseline_bundles._new_table_order(["A_CHILD", "Z_PARENT"], constraints, "APP_DEV"),
        )

    def test_a_cycle_written_in_catalog_format_is_refused(self) -> None:
        constraints = {
            ("A", "A_FK"): _fk("A", "<CONFIGURED_SCHEMA>.B"),
            ("B", "B_FK"): _fk("B", "<CONFIGURED_SCHEMA>.A"),
        }

        with self.assertRaisesRegex(baseline.BaselineError, "cycle"):
            baseline_bundles._new_table_order(["A", "B"], constraints, "APP_DEV")

    def test_bare_and_source_schema_qualified_names_still_order(self) -> None:
        bare = {("A_CHILD", "A_CHILD_FK"): _fk("A_CHILD", "Z_PARENT")}
        qualified = {("A_CHILD", "A_CHILD_FK"): _fk("A_CHILD", "APP_DEV.Z_PARENT")}

        self.assertEqual(["Z_PARENT", "A_CHILD"], baseline_bundles._new_table_order(["A_CHILD", "Z_PARENT"], bare, "APP_DEV"))
        self.assertEqual(["Z_PARENT", "A_CHILD"], baseline_bundles._new_table_order(["A_CHILD", "Z_PARENT"], qualified, "APP_DEV"))

    def test_a_reference_to_another_owner_is_ignored(self) -> None:
        constraints = {("A_CHILD", "A_CHILD_FK"): _fk("A_CHILD", "OTHER_OWNER.Z_PARENT")}

        self.assertEqual(["A_CHILD", "Z_PARENT"], baseline_bundles._new_table_order(["A_CHILD", "Z_PARENT"], constraints, "APP_DEV"))


class SequenceSyncTargetIncrementTests(unittest.TestCase):
    def test_the_sync_restores_the_target_increment_read_from_the_catalog(self) -> None:
        sql = baseline_bundles._sync_sequence_sql(
            "APP_STAGE", {"name": "ITEM_SEQ", "increment_by": 7}, {"table": "APP_ITEMS", "column": "ITEM_ID"},
        )

        self.assertIn("SELECT INCREMENT_BY INTO l_original_increment FROM ALL_SEQUENCES", sql)
        self.assertEqual(2, sql.count("TO_CHAR(l_original_increment)"))
        self.assertNotIn("INCREMENT BY 7", sql)
        self.assertEqual([], _declare_order_violations(sql))
        self.assertLess(sql.index("l_original_increment NUMBER;"), sql.index("BEGIN\n"))


class IndexUniquenessTests(unittest.TestCase):
    def _steps(self, source_indexes: list[dict], target_indexes: list[dict]) -> dict[str, str]:
        source = {"sections": {"tables": [{"name": "APP_ITEMS"}], "columns": [], "constraints": [], "indexes": source_indexes,
                               "sequences": [], "identity-columns": []}}
        target = {"sections": {"tables": [{"name": "APP_ITEMS"}], "columns": [], "constraints": [], "indexes": target_indexes,
                               "sequences": [], "identity-columns": []}}
        steps, _checks = baseline._structure_bundle(
            CheckReplacementTests.config, "APP_DEV", "APP_DEV", source, target, SchemaSnapshot({}, {}, {}, {}, "", ""),
        )
        return steps

    def test_a_unique_source_index_is_not_satisfied_by_a_nonunique_target_on_the_same_columns(self) -> None:
        source = [{"name": "APP_IX_PRICE", "table_name": "APP_ITEMS", "uniqueness": "UNIQUE", "columns": ["PRICE"]}]
        target = [{"name": "APP_IX_OTHER", "table_name": "APP_ITEMS", "uniqueness": "NONUNIQUE", "columns": ["PRICE"]}]

        with self.assertRaisesRegex(baseline.BaselineError, "differs in uniqueness from the target index APP_IX_OTHER"):
            self._steps(source, target)

    def test_the_same_name_with_different_uniqueness_is_refused(self) -> None:
        source = [{"name": "APP_IX_PRICE", "table_name": "APP_ITEMS", "uniqueness": "UNIQUE", "columns": ["PRICE"]}]
        target = [{"name": "APP_IX_PRICE", "table_name": "APP_ITEMS", "uniqueness": "NONUNIQUE", "columns": ["PRICE"]}]

        with self.assertRaisesRegex(baseline.BaselineError, "differs in uniqueness"):
            self._steps(source, target)

    def test_equal_uniqueness_on_the_same_columns_is_skipped(self) -> None:
        source = [{"name": "APP_IX_PRICE", "table_name": "APP_ITEMS", "uniqueness": "UNIQUE", "columns": ["PRICE"]}]
        target = [{"name": "APP_IX_OTHER", "table_name": "APP_ITEMS", "uniqueness": "UNIQUE", "columns": ["PRICE"]}]

        self.assertEqual({}, self._steps(source, target))

    def test_missing_uniqueness_on_both_sides_is_skipped(self) -> None:
        source = [{"name": "APP_IX_PRICE", "table_name": "APP_ITEMS", "columns": ["PRICE"]}]
        target = [{"name": "APP_IX_OTHER", "table_name": "APP_ITEMS", "columns": ["PRICE"]}]

        self.assertEqual({}, self._steps(source, target))


class ColumnObjectTypeOwnerTests(unittest.TestCase):
    def _steps(self, source_row: dict, target_rows: list[dict], target_schema: str) -> tuple[dict[str, str], dict]:
        source = {"sections": {"tables": [{"name": "APP_ITEMS"}], "columns": [source_row], "constraints": [], "indexes": [],
                               "sequences": [], "identity-columns": []}}
        target = {"sections": {"tables": [{"name": "APP_ITEMS"}], "columns": target_rows, "constraints": [], "indexes": [],
                               "sequences": [], "identity-columns": []}}
        return baseline._structure_bundle(
            CheckReplacementTests.config, "APP_DEV", target_schema, source, target, SchemaSnapshot({}, {}, {}, {}, "", ""),
        )

    @staticmethod
    def _row(owner: str | None, table_owner_type: str = "ADDRESS_T", **extra: object) -> dict:
        row = {"table_name": "APP_ITEMS", "name": "ADDR", "data_type": table_owner_type, "nullable": "Y", **extra}
        if owner is not None:
            row["data_type_owner"] = owner
        return row

    def test_an_object_type_column_keeps_its_owner_in_the_ddl_and_the_postcondition(self) -> None:
        steps, checks = self._steps(self._row("GEO"), [], "APP_DEV")

        ddl = steps["001-structure-delta.sql"]
        self.assertIn('ADD ("ADDR" "GEO"."ADDRESS_T")', ddl)
        column_check = [item for item in checks["postconditions"] if item["id"].startswith("column-def-")][0]
        self.assertIn("DATA_TYPE_OWNER = 'GEO'", column_check["sql"])

    def test_an_owner_equal_to_the_source_schema_is_retargeted(self) -> None:
        steps, checks = self._steps(self._row("APP_DEV"), [], "APP_STAGE")

        self.assertIn('ADD ("ADDR" "APP_STAGE"."ADDRESS_T")', steps["001-structure-delta.sql"])
        column_check = [item for item in checks["postconditions"] if item["id"].startswith("column-def-")][0]
        self.assertIn("DATA_TYPE_OWNER = 'APP_STAGE'", column_check["sql"])

    def test_two_existing_columns_differing_only_in_type_owner_are_refused(self) -> None:
        target = self._row("OTHER_OWNER")

        with self.assertRaisesRegex(baseline.BaselineError, "column type changes need a reviewed data migration"):
            self._steps(self._row("GEO"), [target], "APP_DEV")

    def test_existing_columns_with_the_same_type_owner_are_not_refused(self) -> None:
        steps, _checks = self._steps(self._row("GEO"), [self._row("GEO")], "APP_DEV")

        self.assertEqual({}, steps)

    def test_a_built_in_column_is_unchanged(self) -> None:
        row = {"table_name": "APP_ITEMS", "name": "PRICE", "data_type": "NUMBER", "data_precision": 10, "data_scale": 2}

        self.assertEqual('"PRICE" NUMBER(10,2)', baseline_bundles._column_sql(row, "APP_DEV", "APP_STAGE"))
        self.assertNotIn("DATA_TYPE_OWNER", baseline_bundles._column_definition_check("APP_STAGE", row, "APP_DEV")["sql"])


class ConditionKeyQuotedIdentifierTests(unittest.TestCase):
    def test_a_quoted_identifier_keeps_its_inner_space(self) -> None:
        self.assertEqual('"A B">0', baseline_bundles._condition_key('"A B" > 0'))

    def test_identifiers_that_differ_only_inside_quotes_get_different_keys(self) -> None:
        self.assertNotEqual(
            baseline_bundles._condition_key('"A B" + "AB" > 0'),
            baseline_bundles._condition_key('"AB" + "AB" > 0'),
        )

    def test_a_doubled_quote_and_an_apostrophe_inside_an_identifier_are_kept(self) -> None:
        self.assertEqual('"A""B">0', baseline_bundles._condition_key('"A""B" > 0'))
        self.assertEqual('"it\'s">0', baseline_bundles._condition_key('"it\'s" > 0'))

    def test_a_double_quote_inside_a_string_is_an_ordinary_character(self) -> None:
        self.assertEqual("C='a\"b'", baseline_bundles._condition_key("C = 'a\"b'"))

    def test_whitespace_outside_quotes_still_compares_equal(self) -> None:
        self.assertEqual(baseline_bundles._condition_key('"A" >  0'), baseline_bundles._condition_key('"A"> 0'))

    def test_the_generated_normalized_function_handles_quoted_identifiers(self) -> None:
        sql = "DECLARE\n" + baseline_bundles._NORMALIZED_FUNCTION + "BEGIN\n  NULL;\nEND;\n/\n"

        self.assertIn("l_in_ident BOOLEAN := FALSE;", sql)
        self.assertIn("ELSIF l_char = '\"' AND NOT l_in_string THEN", sql)
        self.assertEqual([], _declare_order_violations(sql))


if __name__ == "__main__":
    unittest.main()
