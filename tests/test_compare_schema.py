import unittest

from scripts.schema_catalog import ObjectDefinition, ObjectKey, SchemaInventory, SchemaSnapshot
from scripts.compare_schema import (
    Selection,
    compare_snapshots,
    parse_exact_selector,
    select_objects,
    validate_selector_inputs,
)


def identity(owner, db, service="service-a", container="3", edition="ORA$BASE"):
    return {
        "session_user": owner,
        "current_schema": owner,
        "db_name": db,
        "db_unique_name": db + "_UNIQUE",
        "service_name": service,
        "container_id": container,
        "container_name": "APP_PDB",
        "edition": edition,
        "database_version": "19.0",
    }


def inventory(owner, db, objects, service="service-a"):
    rows = {}
    for name, object_type, extra in objects:
        row = {"owner": owner, "name": name, "type": object_type, "status": "VALID", "last_ddl_time": "2026-09-27T00:00:00"}
        row.update(extra or {})
        rows[ObjectKey(owner, name, object_type)] = row
    return SchemaInventory(
        identity(owner, db, service), rows,
        {"ownerComplete": True, "path": "OWNER_SESSION", "catalogs": ["ALL_OBJECTS"]},
        "2026-09-28T10:00:00Z", "2026-09-28T10:00:02Z",
    )


def table(owner, name, ddl=None, *, columns=None, dependents=(), valid=True):
    return ObjectDefinition(
        ObjectKey(owner, name, "TABLE"),
        {"columns": columns if columns is not None else [{"name": "ID", "data_type": "NUMBER", "nullable": "N", "column_id": 1}]},
        ddl or f"CREATE TABLE {owner}.{name} (ID NUMBER NOT NULL)",
        tuple(dependents), valid,
    )


def snap(inv, definitions):
    return SchemaSnapshot(inv.identity, inv.objects, {item.key: item for item in definitions}, inv.coverage, inv.started_at, inv.completed_at)


class SelectionTests(unittest.TestCase):
    def test_patterns_use_union_and_literal_underscore(self) -> None:
        source = inventory("APP_DEV", "DEVDB", [
            ("HR_DEPARTMENTS", "TABLE", {}), ("HRX_EMPLOYEES", "TABLE", {}), ("GL_CODES", "TABLE", {}),
        ])
        target = inventory("APP_STAGE", "STAGEDB", [
            ("HR_EMPLOYEES", "TABLE", {}), ("HRX_EMPLOYEES", "TABLE", {}), ("GL_CODES", "TABLE", {}),
        ])

        selection = select_objects(source, target, (), ("HR_*", "GL_*"))

        self.assertNotIn(("HRX_EMPLOYEES", "TABLE"), selection.keys)
        self.assertIn(("HR_EMPLOYEES", "TABLE"), selection.keys)
        self.assertEqual(selection.keys, (("GL_CODES", "TABLE"), ("HR_DEPARTMENTS", "TABLE"), ("HR_EMPLOYEES", "TABLE")))

    def test_exact_selectors_fold_unquoted_preserve_quoted_and_qualify_type(self) -> None:
        self.assertEqual(parse_exact_selector("customers"), ("CUSTOMERS", None))
        self.assertEqual(parse_exact_selector('"Customer Details"'), ("Customer Details", None))
        self.assertEqual(parse_exact_selector("TABLE:hr_employees"), ("HR_EMPLOYEES", "TABLE"))
        self.assertEqual(parse_exact_selector('PACKAGE_BODY:"MiXeD"'), ("MiXeD", "PACKAGE BODY"))

    def test_exact_selectors_union_types_and_report_unmatched_inputs(self) -> None:
        source = inventory("APP_DEV", "DEVDB", [("API", "PACKAGE", {}), ("API", "PACKAGE BODY", {}), ("MixedCase", "TABLE", {})])
        target = inventory("APP_STAGE", "STAGEDB", [("API", "PACKAGE", {}), ("API", "PACKAGE BODY", {}), ("MixedCase", "TABLE", {})])

        selection = select_objects(source, target, ("API", '"MixedCase"', "TABLE:NOT_THERE"), ())

        self.assertIn(("API", "PACKAGE BODY"), selection.keys)
        self.assertIn(("MixedCase", "TABLE"), selection.keys)
        self.assertEqual(selection.errors[0]["code"], "NOT_FOUND")

    def test_selecting_identity_sequence_maps_to_its_table(self) -> None:
        source = inventory("APP_DEV", "DEVDB", [("ISEQ$$_100", "SEQUENCE", {"identity_sequence": True, "identity_table_name": "CUSTOMERS"}), ("CUSTOMERS", "TABLE", {})])
        target = inventory("APP_STAGE", "STAGEDB", [("ISEQ$$_900", "SEQUENCE", {"identity_sequence": True, "identity_table_name": "CUSTOMERS"}), ("CUSTOMERS", "TABLE", {})])

        selection = select_objects(source, target, (), ("ISEQ$$_*",))

        self.assertEqual(selection.keys, (("CUSTOMERS", "TABLE"),))

    def test_selector_syntax_rejects_unsafe_patterns_and_unsupported_quotes(self) -> None:
        for pattern in ("HR_%", "HR_?", "HR_[A-Z]", "APP.HR_*", '"Hr_*"', "HR-*"):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                validate_selector_inputs((), (pattern,))
        for selector in ("", '"unterminated', "APP.TABLE", "A B"):
            with self.subTest(selector=selector), self.assertRaises(ValueError):
                parse_exact_selector(selector)

    def test_unsupported_matched_types_are_reported_without_silent_skip(self) -> None:
        source = inventory("APP_DEV", "DEVDB", [("REPORT_MV", "MATERIALIZED VIEW", {})])
        target = inventory("APP_STAGE", "STAGEDB", [])

        selection = select_objects(source, target, (), ("REPORT_*",))

        self.assertIn(("REPORT_MV", "MATERIALIZED VIEW"), selection.keys)
        self.assertTrue(any(error["code"] == "UNSUPPORTED_TYPE" for error in selection.errors))


class ComparisonTests(unittest.TestCase):
    def test_missing_extra_and_different_definition_are_actionable(self) -> None:
        source_inv = inventory("APP_DEV", "DEVDB", [("HR_DEPARTMENTS", "TABLE", {}), ("HR_COMMON", "TABLE", {}), ("HRX_EMPLOYEES", "TABLE", {})])
        target_inv = inventory("APP_STAGE", "STAGEDB", [("HR_EMPLOYEES", "TABLE", {}), ("HR_COMMON", "TABLE", {}), ("HRX_EMPLOYEES", "TABLE", {})])
        selection = select_objects(source_inv, target_inv, (), ("HR_*",))
        source_defs = [table("APP_DEV", "HR_DEPARTMENTS"), table("APP_DEV", "HR_COMMON")]
        target_defs = [table("APP_STAGE", "HR_EMPLOYEES"), table("APP_STAGE", "HR_COMMON", "CREATE TABLE APP_STAGE.HR_COMMON (ID NUMBER, STATUS VARCHAR2(8))")]

        report = compare_snapshots(snap(source_inv, source_defs), snap(target_inv, target_defs), selection)

        kinds = [item["kind"] for item in report.differences]
        self.assertIn("MISSING_ON_TARGET", kinds)
        self.assertIn("EXTRA_ON_TARGET", kinds)
        self.assertIn("DIFFERENT_DEFINITION", kinds)
        self.assertNotIn("HRX_EMPLOYEES", [item.get("name") for item in report.differences])
        self.assertEqual(report.exit_code, 1)

    def test_table_dependents_are_compared_even_when_their_names_do_not_match_selector(self) -> None:
        source_inv = inventory("APP_DEV", "DEVDB", [("ORDERS", "TABLE", {})])
        target_inv = inventory("APP_STAGE", "STAGEDB", [("ORDERS", "TABLE", {})])
        selection = select_objects(source_inv, target_inv, ("ORDERS",), ())
        source_root = table("APP_DEV", "ORDERS", dependents=(ObjectKey("APP_DEV", "ORDERS_PK", "CONSTRAINT"),))
        target_root = table("APP_STAGE", "ORDERS")
        source_child = ObjectDefinition(ObjectKey("APP_DEV", "ORDERS_PK", "CONSTRAINT"), {"status": "ENABLED", "validated": "VALIDATED"}, "ALTER TABLE APP_DEV.ORDERS ADD CONSTRAINT ORDERS_PK PRIMARY KEY (ID)", (), True)

        report = compare_snapshots(snap(source_inv, [source_root, source_child]), snap(target_inv, [target_root]), selection)

        self.assertTrue(any(item["kind"] == "MISSING_ON_TARGET" and item["object_type"] == "CONSTRAINT" for item in report.differences))
        self.assertEqual(report.exit_code, 1)

    def test_external_references_do_not_expand_selection(self) -> None:
        source_inv = inventory("APP_DEV", "DEVDB", [("V", "VIEW", {})])
        target_inv = inventory("APP_STAGE", "STAGEDB", [("V", "VIEW", {})])
        selection = select_objects(source_inv, target_inv, ("V",), ())
        self.assertEqual(selection.keys, (("V", "VIEW"),))

    def test_invalid_object_and_incomplete_coverage_use_exit_priority_two(self) -> None:
        source_inv = inventory("APP_DEV", "DEVDB", [("A", "TABLE", {}), ("P", "PROCEDURE", {})])
        target_inv = inventory("APP_STAGE", "STAGEDB", [("B", "TABLE", {}), ("P", "PROCEDURE", {})])
        selection = Selection((("A", "TABLE"), ("B", "TABLE"), ("P", "PROCEDURE")), ({"code": "UNSUPPORTED_TYPE", "name": "M", "type": "MATERIALIZED VIEW"},))
        source_proc = ObjectDefinition(ObjectKey("APP_DEV", "P", "PROCEDURE"), {}, "CREATE PROCEDURE APP_DEV.P AS BEGIN NULL; END;", (), False)
        target_proc = ObjectDefinition(ObjectKey("APP_STAGE", "P", "PROCEDURE"), {}, "CREATE PROCEDURE APP_STAGE.P AS BEGIN NULL; END;", (), True)

        report = compare_snapshots(
            snap(source_inv, [table("APP_DEV", "A"), source_proc]),
            snap(target_inv, [table("APP_STAGE", "B"), target_proc]),
            selection,
        )

        kinds = {item["kind"] for item in report.differences}
        self.assertTrue({"MISSING_ON_TARGET", "EXTRA_ON_TARGET", "INVALID_OBJECT", "UNKNOWN/UNSUPPORTED"}.issubset(kinds))
        self.assertEqual(report.exit_code, 2)

    def test_same_database_aliases_are_rejected_as_self_comparison(self) -> None:
        source_inv = inventory("APP_DEV", "DEVDB", [("T", "TABLE", {})], service="alias-one")
        target_inv = inventory("APP_DEV", "DEVDB", [("T", "TABLE", {})], service="alias-two")
        selection = select_objects(source_inv, target_inv, ("T",), ())

        report = compare_snapshots(snap(source_inv, [table("APP_DEV", "T")]), snap(target_inv, [table("APP_DEV", "T")]), selection)

        self.assertEqual(report.exit_code, 2)
        self.assertTrue(any(item["code"] == "SELF_COMPARISON" for item in report.selection["errors"]))


if __name__ == "__main__":
    unittest.main()
