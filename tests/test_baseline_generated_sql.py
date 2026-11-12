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

import _no_real_sqlcl  # noqa: F401 (tests must never fall through to installed SQLcl)
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
