"""Regressions for the SQL that `baseline build` generates, found by tests/test_live_baseline.py against a database.

Two defects were found when the generated folders were applied for the first time: a new table's captured DDL holds
several statements, which went into one EXECUTE IMMEDIATE (ORA-00922), and the compile step declared a local function
before its variables (PLS-00103). The build modules must also import in any order.
"""

from __future__ import annotations

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

        order = baseline_bundles._new_table_order(["A_CHILD", "B_PARENT", "C_SELF"], constraints)

        self.assertEqual(["B_PARENT", "A_CHILD", "C_SELF"], order)
        self.assertEqual(["A", "B"], baseline_bundles._new_table_order(["A", "B"], {}), "without foreign keys the order stays alphabetical")

    def test_a_foreign_key_to_a_table_that_already_exists_does_not_reorder(self) -> None:
        constraints = {("A_CHILD", "A_CHILD_FK"): _fk("A_CHILD", "EXISTING")}

        self.assertEqual(["A_CHILD", "B_OTHER"], baseline_bundles._new_table_order(["A_CHILD", "B_OTHER"], constraints))

    def test_a_cycle_between_new_tables_is_refused(self) -> None:
        constraints = {("A", "A_FK"): _fk("A", "B"), ("B", "B_FK"): _fk("B", "A")}

        with self.assertRaises(baseline.BaselineError) as raised:
            baseline_bundles._new_table_order(["A", "B"], constraints)

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


class CompileStepTests(unittest.TestCase):
    def test_the_compile_block_declares_its_variables_before_the_local_function(self) -> None:
        sql = baseline._compile_all_step("APP_STAGE", ["APP_"])

        declarations = sql.split("DECLARE\n", 1)[1].split("BEGIN\n", 1)[0]
        self.assertLess(declarations.index("l_name VARCHAR2"), declarations.index("FUNCTION quoted"))
        self.assertEqual(1, declarations.count("FUNCTION"))


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


if __name__ == "__main__":
    unittest.main()
