"""Contract tests for composing eligible generated baseline components."""

from __future__ import annotations

import hashlib
import importlib
import unittest


def _bundle_api(test: unittest.TestCase):
    try:
        return importlib.import_module("scripts.baseline_bundle")
    except ModuleNotFoundError as error:
        if error.name != "scripts.baseline_bundle":
            raise
        test.fail("the pure baseline bundle interface is not implemented")


def _component(api, kind, steps=(), preconditions=(), postconditions=()):
    return api.BaselineComponent(
        kind=kind,
        steps=tuple(steps),
        preconditions=tuple(preconditions),
        postconditions=tuple(postconditions),
    )


class BaselineBundleTests(unittest.TestCase):
    def test_three_components_become_one_ordered_payload(self) -> None:
        api = _bundle_api(self)
        structure_sql = "CREATE TABLE APP_CUSTOMER (ID NUMBER);\n"
        grants_sql = "GRANT SELECT ON APP_CUSTOMER TO APP_READER;\n"
        code_sql = "CREATE OR REPLACE VIEW APP_CUSTOMER_V AS SELECT ID FROM APP_CUSTOMER;\n"

        result = api.combine_baseline_components(
            [
                _component(api, "code", [("001-package.sql", code_sql)]),
                _component(api, "grants", [("001-permissions.sql", grants_sql)]),
                _component(api, "structure", [("001-table.sql", structure_sql)]),
            ]
        )

        self.assertEqual(
            list(result.steps),
            [
                "001-structure-table.sql",
                "002-grants-permissions.sql",
                "003-code-package.sql",
            ],
        )
        self.assertEqual(
            result.steps,
            {
                "001-structure-table.sql": structure_sql,
                "002-grants-permissions.sql": grants_sql,
                "003-code-package.sql": code_sql,
            },
        )
        self.assertEqual(
            result.provenance,
            (
                {
                    "kind": "structure",
                    "originalName": "001-table.sql",
                    "generatedName": "001-structure-table.sql",
                    "sqlSha256": hashlib.sha256(structure_sql.encode("utf-8")).hexdigest(),
                },
                {
                    "kind": "grants",
                    "originalName": "001-permissions.sql",
                    "generatedName": "002-grants-permissions.sql",
                    "sqlSha256": hashlib.sha256(grants_sql.encode("utf-8")).hexdigest(),
                },
                {
                    "kind": "code",
                    "originalName": "001-package.sql",
                    "generatedName": "003-code-package.sql",
                    "sqlSha256": hashlib.sha256(code_sql.encode("utf-8")).hexdigest(),
                },
            ),
        )

    def test_all_checks_survive_with_namespaced_ids(self) -> None:
        api = _bundle_api(self)
        ready_query = "SELECT 1 FROM DUAL"
        components = [
            _component(
                api,
                kind,
                [("001-change.sql", "SELECT 1 FROM DUAL;\n")],
                postconditions=({"id": "ready", "sql": ready_query, "expected": 1},),
            )
            for kind in ("code", "structure", "grants")
        ]

        result = api.combine_baseline_components(components)

        self.assertEqual(
            result.checks["postconditions"],
            [
                {"id": "structure-ready", "sql": ready_query, "expected": 1},
                {"id": "grants-ready", "sql": ready_query, "expected": 1},
                {"id": "code-ready", "sql": ready_query, "expected": 1},
            ],
        )
        self.assertEqual(result.checks["preconditions"], [])

    def test_duplicate_check_ids_inside_one_component_are_refused(self) -> None:
        api = _bundle_api(self)
        check = {"id": "ready", "sql": "SELECT 1 FROM DUAL", "expected": 1}
        component = _component(api, "structure", postconditions=(check, check))

        with self.assertRaises(api.BaselineBundleError):
            api.combine_baseline_components([component])

    def test_existing_check_query_allowlist_is_enforced(self) -> None:
        api = _bundle_api(self)
        supported = {"id": "ready", "sql": "SELECT COUNT(*) FROM ALL_TABLES", "expected": 1}
        result = api.combine_baseline_components(
            [_component(api, "structure", postconditions=(supported,))]
        )
        self.assertEqual(result.checks["postconditions"][0]["sql"], supported["sql"])

        unsupported = {"id": "ready", "sql": "SELECT NEW_FUNCTION(1) FROM DUAL", "expected": 1}
        with self.assertRaises(api.BaselineBundleError):
            api.combine_baseline_components(
                [_component(api, "structure", postconditions=(unsupported,))]
            )

    def test_data_and_unknown_categories_are_refused(self) -> None:
        api = _bundle_api(self)
        for kind in ("data", "unknown"):
            with self.subTest(kind=kind), self.assertRaises(api.BaselineBundleError):
                api.combine_baseline_components([_component(api, kind)])

    def test_grants_and_code_preconditions_are_refused(self) -> None:
        api = _bundle_api(self)
        precondition = {"id": "ready", "sql": "SELECT 1 FROM DUAL", "expected": 1}
        for kind in ("grants", "code"):
            with self.subTest(kind=kind), self.assertRaises(api.BaselineBundleError):
                api.combine_baseline_components(
                    [_component(api, kind, preconditions=(precondition,))]
                )

    def test_structure_preconditions_remain_at_the_initial_boundary(self) -> None:
        api = _bundle_api(self)
        precondition = {"id": "ready", "sql": "SELECT 1 FROM DUAL", "expected": 1}
        result = api.combine_baseline_components(
            [_component(api, "structure", preconditions=(precondition,))]
        )

        self.assertEqual(
            result.checks["preconditions"],
            [{"id": "structure-ready", "sql": precondition["sql"], "expected": 1}],
        )

    def test_nonconsecutive_original_filenames_are_refused(self) -> None:
        api = _bundle_api(self)
        component = _component(
            api,
            "structure",
            [
                ("001-first.sql", "SELECT 1 FROM DUAL;\n"),
                ("003-third.sql", "SELECT 3 FROM DUAL;\n"),
            ],
        )

        with self.assertRaises(api.BaselineBundleError):
            api.combine_baseline_components([component])

    def test_code_compile_step_remains_last_in_code_category(self) -> None:
        api = _bundle_api(self)
        component = _component(
            api,
            "code",
            [
                ("001-source-package.sql", "SELECT 1 FROM DUAL;\n"),
                ("002-view-customer.sql", "SELECT 2 FROM DUAL;\n"),
                ("003-compile-all.sql", "SELECT 3 FROM DUAL;\n"),
            ],
        )

        result = api.combine_baseline_components([component])

        self.assertEqual(
            list(result.steps),
            [
                "001-code-source-package.sql",
                "002-code-view-customer.sql",
                "003-code-compile-all.sql",
            ],
        )

    def test_duplicate_and_missing_categories_are_handled_deterministically(self) -> None:
        api = _bundle_api(self)
        with self.assertRaises(api.BaselineBundleError):
            api.combine_baseline_components(
                [_component(api, "structure"), _component(api, "structure")]
            )

        result = api.combine_baseline_components(
            [
                _component(api, "code", [("001-code.sql", "SELECT 3 FROM DUAL;\n")]),
                _component(api, "structure"),
            ]
        )
        self.assertEqual(list(result.steps), ["001-code-code.sql"])

    def test_empty_components_and_all_empty_composition_add_no_steps(self) -> None:
        api = _bundle_api(self)
        components = [_component(api, kind) for kind in ("code", "structure", "grants")]

        result = api.combine_baseline_components(components)

        self.assertEqual(result.steps, {})
        self.assertEqual(result.provenance, ())
        self.assertEqual(result.checks, {"preconditions": [], "postconditions": []})

    def test_999_steps_are_accepted(self) -> None:
        api = _bundle_api(self)
        steps = tuple(
            (f"{number:03d}-step.sql", "SELECT 1 FROM DUAL;\n")
            for number in range(1, 1000)
        )

        result = api.combine_baseline_components([_component(api, "code", steps)])

        self.assertEqual(len(result.steps), 999)
        self.assertIn("001-code-step.sql", result.steps)
        self.assertIn("999-code-step.sql", result.steps)

    def test_1000_combined_steps_are_refused(self) -> None:
        api = _bundle_api(self)
        structure_steps = tuple(
            (f"{number:03d}-step.sql", "SELECT 1 FROM DUAL;\n")
            for number in range(1, 1000)
        )
        grants_steps = (("001-step.sql", "SELECT 1 FROM DUAL;\n"),)

        with self.assertRaises(api.BaselineBundleError):
            api.combine_baseline_components(
                [
                    _component(api, "structure", structure_steps),
                    _component(api, "grants", grants_steps),
                ]
            )

    def test_bundle_name_enforces_ascii_slug_and_64_character_limit(self) -> None:
        api = _bundle_api(self)
        valid = "a" + "b" * 63
        self.assertEqual(api.validate_bundle_name(valid), valid)

        for invalid in (
            "Customer-status",
            "a/b",
            "a\nb",
            "a--b",
            "a" + "b" * 64,
        ):
            with self.subTest(name=invalid), self.assertRaises(api.BaselineBundleError):
                api.validate_bundle_name(invalid)

    def test_bundle_combines_nullable_column_postconditions(self):
        api = _bundle_api(self)
        col_check = {"id": "column-def-t-c", "sql": "SELECT CASE WHEN EXISTS (SELECT 1 FROM ALL_TAB_COLS WHERE OWNER = 'S' AND TABLE_NAME = 'T' AND COLUMN_NAME = 'C' AND DATA_TYPE = 'VARCHAR2' AND CHAR_USED = 'C' AND CHAR_LENGTH = 100) THEN 1 ELSE 0 END FROM DUAL", "expected": 1}
        component = _component(
            api,
            "structure",
            steps=(("001-add-column.sql", "ALTER TABLE S.T ADD (C VARCHAR2(100 CHAR));\n"),),
            postconditions=(col_check,),
        )
        combined = api.combine_baseline_components((component,))
        self.assertEqual(len(combined.checks["postconditions"]), 1)
        self.assertEqual(combined.checks["postconditions"][0]["id"], "structure-column-def-t-c")


if __name__ == "__main__":
    unittest.main()
