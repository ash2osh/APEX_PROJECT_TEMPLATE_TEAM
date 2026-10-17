"""The published project-suite example stays valid, explicit, and offline-testable."""
from __future__ import annotations

import unittest
from pathlib import Path

from scripts.dev_test_bindings import RenderContext, render_object_tokens
from scripts.dev_test_manifest import load_suite
from scripts.read_only_query import prepare_query
from scripts.rollout import validate_sql_script


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "docs/dev-test-examples/example.json"


class DevTestExampleTests(unittest.TestCase):
    def test_example_sql_and_bindings_are_valid_without_database_access(self) -> None:
        suite = load_suite(EXAMPLE)
        self.assertEqual("example-dev-fixture", suite.id)
        self.assertEqual(1, len(suite.scenarios))
        scenario = suite.scenarios[0]
        self.assertEqual("fixture-insert", scenario.id)
        self.assertIsNone(scenario.cleanup)

        fixture = scenario.fixtures["objects"][0]
        self.assertEqual("items", fixture["binding"])
        self.assertEqual("TABLE", fixture["type"])
        run_id = "20261017T010203Z-" + "a" * 32
        context = RenderContext(
            "APP",
            run_id,
            {"items": "DT_ABCDEF012345_ITEMS"},
            {},
        )

        step = scenario.steps[0]
        rendered_sql = render_object_tokens(step["source"].decode("utf-8"), context)
        validate_sql_script(rendered_sql)
        self.assertIn("DT_ABCDEF012345_ITEMS", rendered_sql)
        self.assertIn(":DEVTEST_RUN_ID", rendered_sql)
        self.assertEqual(run_id, context.values["DEVTEST_RUN_ID"])

        check = scenario.checks[0]
        rendered_check = render_object_tokens(check["sql"], context)
        prepared_check = prepare_query(rendered_check, bindings=context.values)
        self.assertIn("DEVTEST_RUN_ID", prepared_check)
        self.assertEqual(2, check["expected"])


if __name__ == "__main__":
    unittest.main()
