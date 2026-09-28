import json
import unittest
from pathlib import Path

from scripts.schema_catalog import ObjectDefinition, ObjectKey
from scripts.schema_normalization import normalize_definition, normalization_coverage


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "schema_catalog" / "normalization-cases.json"


def definition(owner, name, object_type, ddl, attributes=None, dependents=(), valid=True):
    return ObjectDefinition(
        ObjectKey(owner, name, object_type),
        dict(attributes or {}),
        ddl,
        tuple(dependents),
        valid,
    )


def normalize_pair(left, right):
    return (
        normalize_definition(left, left.key.owner),
        normalize_definition(right, right.key.owner),
    )


def fixture_definition(value):
    return definition(
        value["owner"], value["name"], value["type"], value["raw_ddl"],
        value.get("attributes"), value.get("dependents", ()), value.get("valid", True),
    )


class SchemaNormalizationTests(unittest.TestCase):
    def test_owner_mapping_preserves_literals_external_owners_and_quoted_names(self) -> None:
        source = definition(
            "APP_DEV", "T", "VIEW",
            'CREATE VIEW "APP_DEV"."T" AS SELECT \'APP_DEV.T\' NOTE, '
            'b."App_Dev" FROM "APP_DEV"."BASE" b JOIN "OTHER_SCHEMA"."X" x ON b.ID=x.ID',
        )
        target = definition(
            "APP_STAGE", "T", "VIEW",
            'create view "APP_STAGE"."T" as select \'APP_DEV.T\' NOTE, '
            'b."App_Dev" from "APP_STAGE"."BASE" b join "OTHER_SCHEMA"."X" x on b.ID=x.ID',
        )

        source_norm, target_norm = normalize_pair(source, target)

        self.assertTrue(source_norm["complete"])
        self.assertEqual(source_norm, target_norm)
        self.assertTrue(any("OTHER_SCHEMA" in token for token in source_norm["ddl_tokens"]))
        self.assertEqual(source.raw_ddl.split("SELECT ", 1)[1].split(" NOTE", 1)[0], "'APP_DEV.T'")
        self.assertNotEqual(
            source_norm,
            normalize_definition(
                definition("APP_STAGE", "T", "VIEW", target.raw_ddl.replace("'APP_DEV.T'", "'APP_STAGE.T'")),
                "APP_STAGE",
            ),
        )
        self.assertNotEqual(
            source_norm,
            normalize_definition(definition("APP_STAGE", "T", "VIEW", target.raw_ddl.replace("OTHER_SCHEMA", "ANOTHER_SCHEMA")), "APP_STAGE"),
        )

    def test_storage_tablespace_ids_timestamps_and_statistics_are_excluded(self) -> None:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        left = fixture_definition(fixture["storageSource"])
        right = fixture_definition(fixture["storageTarget"])

        left_norm, right_norm = normalize_pair(left, right)

        self.assertTrue(left_norm["complete"])
        self.assertEqual(left_norm, right_norm)
        self.assertEqual(left.raw_ddl, fixture["storageSource"]["raw_ddl"])
        self.assertIn("tablespace", normalization_coverage()["exclusions"])

    def test_table_column_shape_and_order_are_kept(self) -> None:
        base = {
            "columns": [
                {"name": "CODE", "data_type": "VARCHAR2", "data_length": 40, "char_used": "C", "nullable": "N", "column_id": 1, "default_expression": "'x'", "virtual": "NO", "hidden": "NO", "identity": "NO"},
                {"name": "AMOUNT", "data_type": "NUMBER", "data_precision": 9, "data_scale": 2, "nullable": "Y", "column_id": 2, "virtual": "NO", "hidden": "NO", "identity": "NO"},
            ]
        }
        original = definition("APP_DEV", "T", "TABLE", "CREATE TABLE APP_DEV.T (CODE VARCHAR2(40), AMOUNT NUMBER)", base)
        variations = (
            {**base, "columns": [{**base["columns"][0], "data_length": 41}, base["columns"][1]]},
            {**base, "columns": [{**base["columns"][0], "char_used": "B"}, base["columns"][1]]},
            {**base, "columns": [{**base["columns"][0], "default_expression": "'y'"}, base["columns"][1]]},
            {**base, "columns": [{**base["columns"][0], "nullable": "Y"}, base["columns"][1]]},
            {**base, "columns": [{**base["columns"][0], "virtual": "YES"}, base["columns"][1]]},
            {**base, "columns": [{**base["columns"][0], "hidden": "YES"}, base["columns"][1]]},
            {**base, "columns": [{**base["columns"][0], "identity": "YES"}, base["columns"][1]]},
            {**base, "columns": [base["columns"][0], {**base["columns"][1], "data_precision": 10}]},
            {"columns": [base["columns"][1], {**base["columns"][0], "column_id": 2}]},
        )
        for attributes in variations:
            with self.subTest(attributes=attributes):
                other = definition("APP_STAGE", "T", "TABLE", "CREATE TABLE APP_STAGE.T (CODE VARCHAR2(40), AMOUNT NUMBER)", attributes)
                self.assertNotEqual(normalize_definition(original, "APP_DEV"), normalize_definition(other, "APP_STAGE"))

    def test_constraint_index_trigger_and_package_body_definitions_are_preserved(self) -> None:
        cases = (
            ("CONSTRAINT", "CHK_T", {"status": "ENABLED", "validated": "VALIDATED"}, {"status": "DISABLED", "validated": "VALIDATED"}, "ALTER TABLE APP_DEV.T ADD CONSTRAINT CHK_T CHECK (ID > 0)"),
            ("CONSTRAINT", "CHK_T", {"status": "ENABLED", "validated": "VALIDATED"}, {"status": "ENABLED", "validated": "NOT VALIDATED"}, "ALTER TABLE APP_DEV.T ADD CONSTRAINT CHK_T CHECK (ID > 0)"),
            ("INDEX", "T_IX", {"uniqueness": "NONUNIQUE", "columns": [{"name": "ID", "position": 1}]}, {"uniqueness": "UNIQUE", "columns": [{"name": "ID", "position": 1}]}, "CREATE INDEX APP_DEV.T_IX ON APP_DEV.T (ID)"),
        )
        for object_type, name, source_attrs, target_attrs, ddl in cases:
            with self.subTest(object_type=object_type):
                source = definition("APP_DEV", name, object_type, ddl, source_attrs)
                target = definition("APP_STAGE", name, object_type, ddl.replace("APP_DEV", "APP_STAGE"), target_attrs)
                self.assertNotEqual(normalize_pair(source, target)[0], normalize_pair(source, target)[1])

        source_trigger = definition("APP_DEV", "T_TRG", "TRIGGER", "CREATE TRIGGER APP_DEV.T_TRG BEFORE INSERT ON APP_DEV.T BEGIN NULL; END;")
        target_trigger = definition("APP_STAGE", "T_TRG", "TRIGGER", "CREATE TRIGGER APP_STAGE.T_TRG BEFORE INSERT ON APP_STAGE.T BEGIN INSERT INTO AUDIT_LOG VALUES ('changed'); END;")
        self.assertNotEqual(normalize_pair(source_trigger, target_trigger)[0], normalize_pair(source_trigger, target_trigger)[1])

        source_body = definition("APP_DEV", "P", "PACKAGE BODY", "CREATE PACKAGE BODY APP_DEV.P AS PROCEDURE RUN IS BEGIN NULL; END; END;")
        target_body = definition("APP_STAGE", "P", "PACKAGE BODY", "CREATE PACKAGE BODY APP_STAGE.P AS PROCEDURE RUN IS BEGIN RAISE_APPLICATION_ERROR(-20001, 'different'); END; END;")
        self.assertNotEqual(normalize_pair(source_body, target_body)[0], normalize_pair(source_body, target_body)[1])

    def test_generated_constraint_name_is_reconciled_only_with_catalog_evidence(self) -> None:
        source = definition(
            "APP_DEV", "SYS_C123", "CONSTRAINT",
            "ALTER TABLE APP_DEV.T ADD CONSTRAINT SYS_C123 CHECK (ID > 0)",
            {"generated": "GENERATED NAME", "table_name": "T", "status": "ENABLED", "validated": "VALIDATED"},
        )
        target = definition(
            "APP_STAGE", "SYS_C987", "CONSTRAINT",
            "ALTER TABLE APP_STAGE.T ADD CONSTRAINT SYS_C987 CHECK (ID > 0)",
            {"generated": "GENERATED NAME", "table_name": "T", "status": "ENABLED", "validated": "VALIDATED"},
        )

        self.assertEqual(normalize_definition(source, "APP_DEV"), normalize_definition(target, "APP_STAGE"))

    def test_runtime_sequence_position_is_excluded_but_increment_is_not(self) -> None:
        source = definition(
            "APP_DEV", "S", "SEQUENCE",
            "CREATE SEQUENCE APP_DEV.S START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 999 CYCLE CACHE 20",
            {"increment_by": 1, "min_value": 1, "max_value": 999, "cycle_flag": "Y", "order_flag": "N", "cache_size": 20, "last_number": 3},
        )
        target = definition(
            "APP_STAGE", "S", "SEQUENCE",
            "CREATE SEQUENCE APP_STAGE.S START WITH 21 INCREMENT BY 1 MINVALUE 1 MAXVALUE 999 CYCLE CACHE 20",
            {"increment_by": 1, "min_value": 1, "max_value": 999, "cycle_flag": "Y", "order_flag": "N", "cache_size": 20, "last_number": 25},
        )

        self.assertEqual(normalize_definition(source, "APP_DEV"), normalize_definition(target, "APP_STAGE"))
        self.assertEqual(source.raw_ddl, "CREATE SEQUENCE APP_DEV.S START WITH 1 INCREMENT BY 1 MINVALUE 1 MAXVALUE 999 CYCLE CACHE 20")
        changed_cache = definition("APP_STAGE", "S", "SEQUENCE", target.raw_ddl.replace("CACHE 20", "CACHE 50"), {**target.attributes, "cache_size": 50})
        self.assertNotEqual(normalize_definition(source, "APP_DEV"), normalize_definition(changed_cache, "APP_STAGE"))
        changed_cycle = definition("APP_STAGE", "S", "SEQUENCE", target.raw_ddl.replace(" CYCLE ", " NOCYCLE "), {**target.attributes, "cycle_flag": "N"})
        self.assertNotEqual(normalize_definition(source, "APP_DEV"), normalize_definition(changed_cycle, "APP_STAGE"))
        changed = definition("APP_STAGE", "S", "SEQUENCE", target.raw_ddl.replace("INCREMENT BY 1", "INCREMENT BY 2"), {**target.attributes, "increment_by": 2})
        self.assertNotEqual(normalize_definition(source, "APP_DEV"), normalize_definition(changed, "APP_STAGE"))

    def test_identity_generated_sequence_names_are_mapped_only_from_identity_relationship(self) -> None:
        source = definition(
            "APP_DEV", "T", "TABLE", "CREATE TABLE APP_DEV.T (ID NUMBER GENERATED ALWAYS AS IDENTITY)",
            {"columns": [{"name": "ID", "identity": "YES"}], "identity_columns": [{"column_name": "ID", "sequence_name": "ISEQ$$_100", "generation_type": "ALWAYS", "identity_options": "START WITH: 1 INCREMENT BY: 1"}]},
        )
        target = definition(
            "APP_STAGE", "T", "TABLE", "CREATE TABLE APP_STAGE.T (ID NUMBER GENERATED ALWAYS AS IDENTITY)",
            {"columns": [{"name": "ID", "identity": "YES"}], "identity_columns": [{"column_name": "ID", "sequence_name": "ISEQ$$_900", "generation_type": "ALWAYS", "identity_options": "START WITH: 907 INCREMENT BY: 1"}]},
        )

        self.assertEqual(normalize_definition(source, "APP_DEV"), normalize_definition(target, "APP_STAGE"))

    def test_ambiguous_generated_names_are_not_erased(self) -> None:
        source = definition("APP_DEV", "SYS_C123", "CONSTRAINT", "ALTER TABLE APP_DEV.T ADD CONSTRAINT SYS_C123 CHECK (ID > 0)", {"table_name": "T"})
        target = definition("APP_STAGE", "SYS_C987", "CONSTRAINT", "ALTER TABLE APP_STAGE.T ADD CONSTRAINT SYS_C987 CHECK (ID > 0)", {"table_name": "T"})

        self.assertNotEqual(normalize_definition(source, "APP_DEV"), normalize_definition(target, "APP_STAGE"))

    def test_unknown_types_and_malformed_ddl_are_incomplete_but_keep_raw_source(self) -> None:
        unsupported = definition("APP_DEV", "MV", "MATERIALIZED VIEW", "CREATE MATERIALIZED VIEW APP_DEV.MV AS SELECT 1 FROM dual")
        malformed = definition("APP_DEV", "T", "VIEW", "CREATE VIEW APP_DEV.T AS SELECT 'unterminated FROM dual")

        unsupported_norm = normalize_definition(unsupported, "APP_DEV")
        malformed_norm = normalize_definition(malformed, "APP_DEV")

        self.assertFalse(unsupported_norm["complete"])
        self.assertFalse(malformed_norm["complete"])
        self.assertEqual(unsupported.raw_ddl, "CREATE MATERIALIZED VIEW APP_DEV.MV AS SELECT 1 FROM dual")
        self.assertIn("reason", malformed_norm["coverage"])

    def test_optimizer_hints_and_literal_contents_are_not_stripped(self) -> None:
        source = definition("APP_DEV", "T", "VIEW", "CREATE VIEW APP_DEV.T AS SELECT /*+ INDEX(T T_IX) */ 'a  b,;\nΩ' AS TXT FROM APP_DEV.BASE T")
        formatted = definition("APP_STAGE", "T", "VIEW", "create view APP_STAGE.T as select /*+ INDEX(T T_IX) */ 'a  b,;\nΩ' AS TXT from APP_STAGE.BASE T")
        changed_literal = definition("APP_STAGE", "T", "VIEW", formatted.raw_ddl.replace("a  b,;", "a b,;"))

        self.assertEqual(normalize_pair(source, formatted)[0], normalize_pair(source, formatted)[1])
        self.assertNotEqual(normalize_definition(source, "APP_DEV"), normalize_definition(changed_literal, "APP_STAGE"))

        alternative_source = definition("APP_DEV", "Q", "VIEW", "CREATE VIEW APP_DEV.Q AS SELECT q'[APP_DEV.'x',;\nΩ]' AS TXT FROM APP_DEV.BASE")
        alternative_target = definition("APP_STAGE", "Q", "VIEW", "CREATE VIEW APP_STAGE.Q AS SELECT q'[APP_DEV.'x',;\nΩ]' AS TXT FROM APP_STAGE.BASE")
        self.assertEqual(normalize_pair(alternative_source, alternative_target)[0], normalize_pair(alternative_source, alternative_target)[1])


if __name__ == "__main__":
    unittest.main()
