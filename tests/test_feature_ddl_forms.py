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
            "ALTER TABLE T MODIFY (A NUMBER)",
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


if __name__ == "__main__":
    unittest.main()
