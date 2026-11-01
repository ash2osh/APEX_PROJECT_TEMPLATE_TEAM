from __future__ import annotations

import unittest

from scripts import feature_ddl_forms as forms

SCHEMA = "APP_DATA"


def model(statement: str, kind: str = "OPAQUE") -> dict | None:
    return forms.model_statement(statement, {"kind": kind, "file": "001-ddl.sql", "statement_index": 1}, SCHEMA)


class CommentTests(unittest.TestCase):
    def test_table_and_column_comments_are_modelled_with_their_text(self) -> None:
        table = model("COMMENT ON TABLE \"APP_DATA\".\"T\" IS 'it''s a table; ok'")
        self.assertEqual("COMMENT_ON", table["kind"])
        self.assertEqual(("TABLE", "T", None, "it's a table; ok"),
                         (table["object_type"], table["name"], table["column"], table["comment"]))
        self.assertTrue(table["supported"])
        self.assertEqual("001-ddl.sql", table["file"], "location fields from the analyzer are kept")

        column = model("COMMENT ON COLUMN T.C IS 'x'")
        self.assertEqual(("COLUMN", "T", "C"), (column["object_type"], column["name"], column["column"]))

    def test_empty_comment_and_arabic_text_are_modelled(self) -> None:
        self.assertEqual("", model("COMMENT ON TABLE T IS ''")["comment"])
        self.assertEqual("مورد", model("COMMENT ON TABLE T IS 'مورد'")["comment"])

    def test_other_schema_is_an_error_not_a_silent_skip(self) -> None:
        for statement in ("COMMENT ON TABLE OTHER.T IS 'x'", "COMMENT ON COLUMN OTHER.T.C IS 'x'"):
            with self.subTest(statement=statement), self.assertRaises(forms.FeatureDdlError):
                model(statement)

    def test_unmodelled_comment_forms_stay_refused(self) -> None:
        for statement in (
            "COMMENT ON TABLE T IS q'[x]'",
            "COMMENT ON TABLE T IS N'x'",
            "COMMENT ON MATERIALIZED VIEW T IS 'x'",
            "COMMENT ON COLUMN T IS 'x'",
            "COMMENT ON TABLE T IS 'a' || 'b'",
            "COMMENT ON TABLE T IS 'x' DROP TABLE T",
            "COMMENT ON TABLE T.A.B.C IS 'x'",
        ):
            with self.subTest(statement=statement):
                self.assertIsNone(model(statement))

    def test_overlong_or_control_character_comment_is_an_error(self) -> None:
        with self.assertRaises(forms.FeatureDdlError):
            model("COMMENT ON TABLE T IS '" + "x" * 4001 + "'")
        with self.assertRaises(forms.FeatureDdlError):
            model("COMMENT ON TABLE T IS 'a\x01b'")


class SynonymTests(unittest.TestCase):
    def test_private_and_public_synonyms_target_the_checkpoint_schema(self) -> None:
        public = model('CREATE OR REPLACE PUBLIC SYNONYM "S" FOR "APP_DATA"."T"')
        self.assertEqual(("CREATE_SYNONYM", "PUBLIC", "S", "APP_DATA", "T"),
                         (public["kind"], public["owner"], public["name"], public["target_owner"], public["target_name"]))
        private = model("CREATE SYNONYM S FOR T")
        self.assertEqual(("APP_DATA", "T"), (private["owner"], private["target_name"]))

    def test_synonym_to_another_schema_or_through_a_link_is_an_error(self) -> None:
        for statement in (
            "CREATE SYNONYM S FOR OTHER.T",
            "CREATE PUBLIC SYNONYM S FOR OTHER.T",
            "CREATE SYNONYM OTHER.S FOR T",
            "CREATE SYNONYM S FOR T@LINK",
        ):
            with self.subTest(statement=statement), self.assertRaises(forms.FeatureDdlError):
                model(statement)


class ConstraintTests(unittest.TestCase):
    def test_named_foreign_key_keeps_columns_parent_and_rule(self) -> None:
        fk = model('ALTER TABLE "APP_DATA"."C" ADD CONSTRAINT "FK_C_P" FOREIGN KEY ("A","B") '
                   'REFERENCES "APP_DATA"."P" ("X","Y") ON DELETE CASCADE ENABLE NOVALIDATE', "UNKNOWN")
        self.assertEqual(
            ("ADD_CONSTRAINT", "C", "FK_C_P", "R", ("A", "B"), "P", ("X", "Y"), "CASCADE", "ENABLED", "NOT VALIDATED"),
            (fk["kind"], fk["table"], fk["constraint"], fk["constraint_type"], fk["columns"], fk["ref_table"],
             fk["ref_columns"], fk["delete_rule"], fk["status"], fk["validated"]),
        )

    def test_foreign_key_may_omit_parent_columns(self) -> None:
        fk = model("ALTER TABLE C ADD CONSTRAINT FK FOREIGN KEY (A) REFERENCES P", "UNKNOWN")
        self.assertIsNone(fk["ref_columns"])
        self.assertEqual("NO ACTION", fk["delete_rule"])

    def test_primary_unique_and_check(self) -> None:
        pk = model("ALTER TABLE T ADD CONSTRAINT PK_T PRIMARY KEY (ID)", "UNKNOWN")
        self.assertEqual(("P", ("ID",), "VALIDATED"), (pk["constraint_type"], pk["columns"], pk["validated"]))
        unique = model("ALTER TABLE T ADD UNIQUE (A, B)", "UNKNOWN")
        self.assertEqual((None, "U", ("A", "B")), (unique["constraint"], unique["constraint_type"], unique["columns"]))
        check = model("ALTER TABLE T ADD CONSTRAINT CK CHECK (A IN ('Y','N') AND (B > 0))", "UNKNOWN")
        self.assertEqual("A IN ('Y','N') AND (B > 0)", check["condition"])

    def test_disabled_constraint_has_no_validation_expectation(self) -> None:
        pk = model("ALTER TABLE T ADD CONSTRAINT PK_T PRIMARY KEY (ID) DISABLE", "UNKNOWN")
        self.assertEqual(("DISABLED", None), (pk["status"], pk["validated"]))

    def test_other_schema_table_or_parent_is_an_error(self) -> None:
        for statement in (
            "ALTER TABLE OTHER.T ADD CONSTRAINT PK PRIMARY KEY (ID)",
            "ALTER TABLE T ADD CONSTRAINT FK FOREIGN KEY (A) REFERENCES OTHER.P (X)",
        ):
            with self.subTest(statement=statement), self.assertRaises(forms.FeatureDdlError):
                model(statement, "UNKNOWN")

    def test_unmodelled_alter_forms_stay_refused(self) -> None:
        for statement in (
            "ALTER TABLE T ADD CHECK (A > 0)",  # an unnamed CHECK has no catalog identity to prove
            "ALTER TABLE T ADD CONSTRAINT PK PRIMARY KEY (ID) USING INDEX IX",
            "ALTER TABLE T ADD CONSTRAINT PK PRIMARY KEY (ID) ENABLE ENABLE",
            "ALTER TABLE T ADD CONSTRAINT PK PRIMARY KEY (ID) DEFERRABLE",
            "ALTER TABLE T ADD CONSTRAINT U UNIQUE (A, A)",
            "ALTER TABLE T ADD CONSTRAINT FK FOREIGN KEY (A, B) REFERENCES P (X)",
            "ALTER TABLE T ADD CONSTRAINT FK FOREIGN KEY (A) REFERENCES P ON DELETE RESTRICT",
            "ALTER TABLE T ADD (A NUMBER, CONSTRAINT PK PRIMARY KEY (A))",
            "ALTER TABLE T ADD CONSTRAINT PK PRIMARY KEY (ID); DROP TABLE T",
            "ALTER TABLE T DROP CONSTRAINT PK",
            "ALTER TABLE T MODIFY CONSTRAINT PK DISABLE",
            "ALTER TABLE T MODIFY (A NUMBER DEFAULT 1)",
            "ALTER TABLE T RENAME TO U",
        ):
            with self.subTest(statement=statement):
                self.assertIsNone(model(statement, "UNKNOWN"))

    def test_key_reports_constraint_name_or_table(self) -> None:
        named = model("ALTER TABLE T ADD CONSTRAINT PK_T PRIMARY KEY (ID)", "UNKNOWN")
        unnamed = model("ALTER TABLE T ADD UNIQUE (A)", "UNKNOWN")
        self.assertEqual((SCHEMA, "PK_T"), forms.operation_key(named))
        self.assertEqual((SCHEMA, "T"), forms.operation_key(unnamed))


class GrantAndGateTests(unittest.TestCase):
    def test_object_grant_is_still_modelled(self) -> None:
        grant = model("GRANT SELECT, INSERT ON APP_DATA.T TO APP_CODE, REPORTER", "REVIEWED_OPERATION")
        self.assertEqual(("GRANT_OBJECT", "T", "SELECT, INSERT", ("APP_CODE", "REPORTER")),
                         (grant["kind"], grant["name"], grant["privileges"], grant["grantees"]))
        with self.assertRaises(forms.FeatureDdlError):
            model("GRANT SELECT ON OTHER.T TO APP_CODE", "REVIEWED_OPERATION")

    def test_unqualified_object_grant_defaults_to_checkpoint_schema(self) -> None:
        grant = model("GRANT EXECUTE ON PKG TO APP_CODE", "REVIEWED_OPERATION")
        self.assertEqual(("GRANT_OBJECT", "PKG", "EXECUTE", ("APP_CODE",)),
                         (grant["kind"], grant["name"], grant["privileges"], grant["grantees"]))
        self.assertEqual(SCHEMA, grant["owner"])

    def test_execute_grant_and_sequence_grant(self) -> None:
        exec_grant = model("GRANT EXECUTE ON APP_DATA.MY_PROC TO RUNERP_USER", "REVIEWED_OPERATION")
        self.assertEqual(("GRANT_OBJECT", "MY_PROC", "EXECUTE", ("RUNERP_USER",)),
                         (exec_grant["kind"], exec_grant["name"], exec_grant["privileges"], exec_grant["grantees"]))
        seq_grant = model("GRANT SELECT ON APP_DATA.MY_SEQ TO RUNERP_USER", "REVIEWED_OPERATION")
        self.assertEqual(("GRANT_OBJECT", "MY_SEQ", "SELECT", ("RUNERP_USER",)),
                         (seq_grant["kind"], seq_grant["name"], seq_grant["privileges"], seq_grant["grantees"]))
        multi = model("GRANT EXECUTE ON PKG TO U1, U2 WITH GRANT OPTION", "REVIEWED_OPERATION")
        self.assertEqual(("U1", "U2"), multi["grantees"])

    def test_a_form_is_only_modelled_for_the_kind_the_analyzer_left_open(self) -> None:
        self.assertIsNone(model("COMMENT ON TABLE T IS 'x'", "REVIEWED_OPERATION"))
        self.assertIsNone(model("GRANT SELECT ON APP_DATA.T TO X", "OPAQUE"))

    def test_destructive_and_stateful_statements_are_never_modelled(self) -> None:
        for statement in (
            "DROP TABLE T", "DROP PUBLIC SYNONYM S", "TRUNCATE TABLE T", "ALTER TRIGGER TRG ENABLE",
            "ALTER PACKAGE P COMPILE", "RENAME T TO U", "REVOKE SELECT ON T FROM X",
        ):
            for kind in ("OPAQUE", "UNKNOWN", "REVIEWED_OPERATION"):
                with self.subTest(statement=statement, kind=kind):
                    self.assertIsNone(model(statement, kind))


class ViewTests(unittest.TestCase):
    def test_cte_and_subquery_views_modelled_with_dependencies(self) -> None:
        # Simple CTE
        v1 = model("CREATE VIEW V1 AS WITH cte AS (SELECT ID FROM T1) SELECT ID FROM cte", "UNKNOWN")
        self.assertEqual("CREATE_VIEW", v1["kind"])
        self.assertEqual("V1", v1["name"])
        self.assertIn((SCHEMA, "T1"), v1["dependencies"])
        self.assertNotIn((SCHEMA, "CTE"), v1["dependencies"])

        # Chained CTEs
        v2 = model("CREATE VIEW V2 AS WITH c1 AS (SELECT ID FROM T1), c2 AS (SELECT ID FROM c1 JOIN T2 ON c1.ID = T2.ID) SELECT * FROM c2", "UNKNOWN")
        self.assertEqual({(SCHEMA, "T1"), (SCHEMA, "T2")}, set(v2["dependencies"]))

        # Shadowing: CTE named same as table -> CTE shadows table name in its scope
        v3 = model("CREATE VIEW V3 AS WITH T1 AS (SELECT 1 AS ID FROM DUAL) SELECT ID FROM T1", "UNKNOWN")
        self.assertEqual((), v3["dependencies"])

        # Inline view
        v4 = model("CREATE VIEW V4 AS SELECT * FROM (SELECT ID FROM T1) SUB", "UNKNOWN")
        self.assertEqual({(SCHEMA, "T1")}, set(v4["dependencies"]))

        # Comma join
        v5 = model("CREATE VIEW V5 AS SELECT A.ID, B.ID FROM T1 A, T2 B", "UNKNOWN")
        self.assertEqual({(SCHEMA, "T1"), (SCHEMA, "T2")}, set(v5["dependencies"]))

        # Subqueries in SELECT and WHERE
        v6 = model("CREATE VIEW V6 AS SELECT (SELECT VAL FROM T1) FROM T2 WHERE EXISTS (SELECT 1 FROM T3)", "UNKNOWN")
        self.assertEqual({(SCHEMA, "T1"), (SCHEMA, "T2"), (SCHEMA, "T3")}, set(v6["dependencies"]))

        # Exclusions: EXTRACT, TRIM, SUBSTRING
        v7 = model("CREATE VIEW V7 AS SELECT EXTRACT(YEAR FROM DT), TRIM('x' FROM STR) FROM T1", "UNKNOWN")
        self.assertEqual({(SCHEMA, "T1")}, set(v7["dependencies"]))

    def test_unsupported_view_features_stay_refused(self) -> None:
        for stmt in (
            "CREATE VIEW V AS SELECT * FROM T@DBLINK",
            "CREATE VIEW V AS SELECT * FROM TABLE(MY_FUNC())",
            "CREATE VIEW V AS SELECT * FROM T, LATERAL(SELECT * FROM U WHERE U.ID = T.ID)",
            "CREATE VIEW V AS SELECT * FROM T, JSON_TABLE(J, '$[*]' COLUMNS (X INT PATH '$.x'))",
            "CREATE VIEW V AS SELECT * FROM T, XMLTABLE('/a' PASSING X COLUMNS Y INT PATH 'y')",
            "CREATE VIEW V AS SELECT * FROM T AS OF TIMESTAMP SYSTIMESTAMP",
            "CREATE VIEW V AS SELECT * FROM T VERSIONS BETWEEN SCN MINVALUE AND MAXVALUE",
            "CREATE VIEW V AS SELECT * FROM T SAMPLE (10)",
            "CREATE VIEW V AS SELECT * FROM T MODEL DIMENSION BY (ID) MEASURES (VAL) RULES ()",
            "CREATE VIEW V AS SELECT * FROM T PIVOT (SUM(VAL) FOR CAT IN ('A', 'B'))",
            "CREATE VIEW V AS SELECT * FROM T UNPIVOT (VAL FOR CAT IN (A, B))",
        ):
            with self.subTest(stmt=stmt):
                self.assertIsNone(model(stmt, "UNKNOWN"))

    def test_other_schema_view_is_an_error(self) -> None:
        with self.assertRaises(forms.FeatureDdlError):
            model("CREATE VIEW OTHER.V AS SELECT * FROM T", "UNKNOWN")


class ModifyColumnTests(unittest.TestCase):
    def test_modify_column_data_types_and_constraints(self) -> None:
        stmt = "ALTER TABLE T MODIFY (A VARCHAR2(100 BYTE) NOT NULL, B NUMBER(10, 2), C DATE NULL, D CLOB)"
        m = model(stmt, "UNKNOWN")
        self.assertEqual("ALTER_MODIFY_COLUMN", m["kind"])
        self.assertEqual("T", m["table"])
        self.assertEqual(("A", "B", "C", "D"), m["columns"])
        specs = {s["name"]: s for s in m["column_specs"]}

        self.assertEqual("VARCHAR2", specs["A"]["data_type"])
        self.assertEqual(100, specs["A"]["char_length"])
        self.assertEqual("B", specs["A"]["char_used"])
        self.assertFalse(specs["A"]["nullable"])

        self.assertEqual("NUMBER", specs["B"]["data_type"])
        self.assertEqual(10, specs["B"]["precision"])
        self.assertEqual(2, specs["B"]["scale"])
        self.assertIsNone(specs["B"]["nullable"])

        self.assertEqual("DATE", specs["C"]["data_type"])
        self.assertTrue(specs["C"]["nullable"])

        self.assertEqual("CLOB", specs["D"]["data_type"])

    def test_modify_column_without_parentheses(self) -> None:
        m = model("ALTER TABLE T MODIFY A NUMBER", "UNKNOWN")
        self.assertEqual("ALTER_MODIFY_COLUMN", m["kind"])
        self.assertEqual(("A",), m["columns"])
        self.assertEqual("NUMBER", m["column_specs"][0]["data_type"])

    def test_unmodelled_modify_forms_stay_refused(self) -> None:
        for stmt in (
            "ALTER TABLE T MODIFY (A NUMBER DEFAULT 1)",
            "ALTER TABLE T MODIFY (A INVISIBLE)",
            "ALTER TABLE T MODIFY (A NUMBER, CONSTRAINT CK CHECK (A > 0))",
            "ALTER TABLE T MODIFY (A RAW(100))",
            "ALTER TABLE T MODIFY (A VARCHAR2(50) COLLATE BINARY_CI)",
            "ALTER TABLE T MODIFY (A NUMBER GENERATED ALWAYS AS IDENTITY)",
        ):
            with self.subTest(stmt=stmt):
                self.assertIsNone(model(stmt, "UNKNOWN"))

    def test_other_schema_modify_is_an_error(self) -> None:
        with self.assertRaises(forms.FeatureDdlError):
            model("ALTER TABLE OTHER.T MODIFY (A NUMBER)", "UNKNOWN")


class ExpressionIndexTests(unittest.TestCase):
    def test_function_based_and_expression_indexes(self) -> None:
        ix1 = model("CREATE INDEX IX_T ON T (UPPER(NAME))", "UNKNOWN")
        self.assertEqual("CREATE_INDEX", ix1["kind"])
        self.assertEqual("IX_T", ix1["name"])
        self.assertEqual("T", ix1["table"])
        self.assertTrue(ix1["has_expressions"])
        self.assertEqual(1, ix1["total_columns"])
        self.assertEqual(1, ix1["expression_count"])
        self.assertFalse(ix1["unique"])

        ix2 = model("CREATE UNIQUE INDEX IX_U ON T (CASE WHEN A = 1 THEN B END, C)", "UNKNOWN")
        self.assertEqual("CREATE_INDEX", ix2["kind"])
        self.assertEqual("IX_U", ix2["name"])
        self.assertTrue(ix2["has_expressions"])
        self.assertTrue(ix2["unique"])
        self.assertEqual(2, ix2["total_columns"])
        self.assertEqual(1, ix2["expression_count"])

        ix3 = model("CREATE BITMAP INDEX IX_B ON T (TRUNC(DT))", "UNKNOWN")
        self.assertEqual("CREATE_INDEX", ix3["kind"])
        self.assertTrue(ix3["bitmap"])
        self.assertTrue(ix3["has_expressions"])

    def test_unmodelled_index_forms_stay_refused(self) -> None:
        with self.assertRaises(forms.FeatureDdlError):
            model("CREATE INDEX OTHER.IX ON T (UPPER(A))", "UNKNOWN")
        with self.assertRaises(forms.FeatureDdlError):
            model("CREATE INDEX IX ON OTHER.T (UPPER(A))", "UNKNOWN")
        self.assertIsNone(model("CREATE INDEX IX ON T (A) GLOBAL PARTITION BY RANGE (A)", "UNKNOWN"))


if __name__ == "__main__":
    unittest.main()
