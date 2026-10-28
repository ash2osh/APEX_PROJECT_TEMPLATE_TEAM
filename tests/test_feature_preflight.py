from __future__ import annotations

import hashlib
import unittest

from scripts import feature_preflight
from scripts.feature_manifest import FeatureCheckpoint, FeatureInput
from scripts.migration_manifest import QueryCheck
from scripts.schema_catalog import ObjectKey


def _input(path: str, source: str) -> FeatureInput:
    raw = source.encode("utf-8")
    return FeatureInput(path, raw, hashlib.sha256(raw).hexdigest(), None, "ddl")


def _checkpoint(*sources: str, schema: str = "APP_CODE") -> FeatureCheckpoint:
    return FeatureCheckpoint(
        schema,
        tuple(_input(f"features/demo/{schema}/ddl-{index:02d}.sql", source) for index, source in enumerate(sources, 1)),
        (),
        (QueryCheck("always-true", "SELECT 1 FROM dual", 1),),
        (),
    )


def _state(*, tables=(), columns=(), indexes=(), views=(), synonyms=(), grants=(), invalid=(), synonym_access=()) -> dict:
    return {
        "schema": "APP_CODE",
        "structural": {
            "tables": [{"name": name} for name in tables],
            "columns": [{"table_name": table, "name": column} for table, column in columns],
            "indexes": [{"name": name, "table_name": table} for name, table in indexes],
            "views": [{"name": name, "status": "VALID"} for name in views],
            "synonyms": list(synonyms),
            "object-grants": list(grants),
            "invalid-objects": list(invalid),
            "feature-synonym-access": list(synonym_access),
            "constraints": [], "triggers": [], "sequences": [], "stored-code": [],
            "identity-columns": [], "feature-dependencies": [],
        },
        "coverage": {"ownerComplete": False, "selectedScopeComplete": True},
    }


class FeaturePreflightTests(unittest.TestCase):
    def test_later_missing_index_table_refuses_before_first_create(self) -> None:
        checkpoint = _checkpoint(
            "CREATE TABLE APP_CODE.EARLY (ID NUMBER);\n",
            "CREATE INDEX APP_CODE.IX_MISSING ON APP_CODE.MISSING_TABLE (ID);\n",
        )

        with self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "MISSING_TABLE"):
            feature_preflight.prepare_checkpoint(checkpoint, _state(), ())

    def test_scope_includes_created_objects_and_index_table_and_columns(self) -> None:
        scope = feature_preflight.required_scope(_checkpoint(
            "CREATE TABLE APP_CODE.EARLY (ID NUMBER);\n",
            "CREATE INDEX APP_CODE.IX_EARLY ON APP_CODE.EARLY (ID);\n",
        ))

        self.assertIn(ObjectKey("APP_CODE", "EARLY", "TABLE"), scope.object_keys)
        self.assertIn(ObjectKey("APP_CODE", "IX_EARLY", "INDEX"), scope.object_keys)
        self.assertIn(ObjectKey("APP_CODE", "EARLY", "TABLE"), scope.prerequisite_keys)
        self.assertIn("columns", scope.sections)
        self.assertIn("invalid-objects", scope.sections)

    def test_structural_preflight_preserves_quoted_identifier_case(self) -> None:
        checkpoint = _checkpoint(
            'CREATE TABLE APP_CODE."MiXeD" ("Id" NUMBER);\n',
            'CREATE UNIQUE INDEX APP_CODE."IxMixed" ON APP_CODE."MiXeD" ("Id");\n',
        )
        scope = feature_preflight.required_scope(checkpoint)
        self.assertIn(ObjectKey("APP_CODE", "MiXeD", "TABLE"), scope.object_keys)
        self.assertIn(ObjectKey("APP_CODE", "IxMixed", "INDEX"), scope.object_keys)
        plan = feature_preflight.prepare_checkpoint(checkpoint, _state(), ())
        sql = "\n".join(check.sql for check in plan.postconditions)
        self.assertIn("'MiXeD'", sql)
        self.assertIn("'IxMixed'", sql)
        self.assertIn("'Id'", sql)

    def test_index_missing_column_refuses(self) -> None:
        checkpoint = _checkpoint("CREATE INDEX APP_CODE.IX_T ON APP_CODE.T (MISSING);\n")
        with self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "MISSING"):
            feature_preflight.prepare_checkpoint(checkpoint, _state(tables=("T",), columns=(("T", "ID"),)), ())

    def test_occupied_common_namespace_refuses_even_with_always_true_postcondition(self) -> None:
        checkpoint = _checkpoint("CREATE TABLE APP_CODE.T (ID NUMBER);\n")
        with self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "occupied"):
            feature_preflight.prepare_checkpoint(checkpoint, _state(views=("T",)), ())
        with self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "occupied"):
            feature_preflight.prepare_checkpoint(checkpoint, _state(indexes=(("T", "OTHER"),)), ())

    def test_external_view_dependency_is_not_treated_as_selected_coverage(self) -> None:
        checkpoint = _checkpoint("CREATE VIEW APP_CODE.V AS SELECT ID FROM OTHER_SCHEMA.T;\n")
        with self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "dependency"):
            feature_preflight.prepare_checkpoint(checkpoint, _state(), ())

    def test_reviewed_create_or_replace_view_keeps_its_live_view_guard(self) -> None:
        base = _checkpoint("CREATE OR REPLACE VIEW APP_CODE.V AS SELECT 1 AS ID FROM DUAL;\n")
        checkpoint = FeatureCheckpoint(
            base.schema, base.inputs, (QueryCheck("review-replace", "SELECT 1 FROM dual", 1),),
            base.postconditions, base.object_keys,
        )
        plan = feature_preflight.prepare_checkpoint(checkpoint, _state(views=("V",)), ())
        self.assertIn("O.OBJECT_TYPE = 'VIEW'", plan.preconditions[0].sql)

    def test_missing_direct_grant_target_and_synonym_target_refuse(self) -> None:
        grants = _checkpoint("GRANT SELECT ON APP_CODE.MISSING TO REPORTER;\n")
        synonym = _checkpoint("CREATE SYNONYM APP_CODE.MISSING_ALIAS FOR APP_CODE.MISSING;\n")
        with self.subTest(kind="grant"), self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "MISSING"):
            feature_preflight.prepare_checkpoint(grants, _state(), ())
        with self.subTest(kind="synonym"), self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "MISSING"):
            feature_preflight.prepare_checkpoint(synonym, _state(), ())

    def test_invalid_required_dependent_refuses(self) -> None:
        checkpoint = _checkpoint("CREATE TABLE APP_CODE.T (ID NUMBER);\n")
        state = _state(invalid=({"name": "V_T", "type": "VIEW", "status": "INVALID"},))
        state["structural"]["feature-dependencies"] = [{
            "name": "V_T", "type": "VIEW", "referenced_name": "T", "referenced_owner": "APP_CODE",
        }]

        with self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "invalid"):
            feature_preflight.prepare_checkpoint(checkpoint, state, ())

    def test_scope_includes_existing_dependents_of_selected_table(self) -> None:
        checkpoint = _checkpoint("ALTER TABLE APP_CODE.T ADD (NEW_ID NUMBER);\n")
        state = _state(tables=("T",), columns=(("T", "ID"),), views=("V_T",))
        state["structural"]["feature-dependencies"] = [{
            "name": "V_T", "type": "VIEW", "referenced_name": "T", "referenced_owner": "APP_CODE",
        }]

        plan = feature_preflight.prepare_checkpoint(checkpoint, state, ())

        self.assertIn(ObjectKey("APP_CODE", "V_T", "VIEW"), plan.scope.object_keys)
        record = feature_preflight.plan_to_dict(plan)
        feature_preflight.validate_plan_record(record, checkpoint, state["structural"])
        record["preconditions"][0]["sql"] = "DELETE FROM APP_CODE.T"
        with self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "invalid query"):
            feature_preflight.validate_plan_record(record, checkpoint, state["structural"])

    def test_ordered_same_checkpoint_and_verified_earlier_checkpoint_effects_are_accepted(self) -> None:
        first = _checkpoint("CREATE TABLE APP_CODE.T (ID NUMBER);\n")
        same_checkpoint = _checkpoint(
            "CREATE TABLE APP_CODE.T (ID NUMBER);\n",
            "CREATE INDEX APP_CODE.IX_T ON APP_CODE.T (ID);\n",
        )
        within = feature_preflight.prepare_checkpoint(same_checkpoint, _state(), ())
        self.assertTrue(within.effects_sha256)
        self.assertEqual((), within.prior_checkpoints)

        first_plan = feature_preflight.prepare_checkpoint(first, _state(), ())
        later = _checkpoint("CREATE INDEX APP_CODE.IX_T ON APP_CODE.T (ID);\n")
        across = feature_preflight.prepare_checkpoint(later, _state(), ({
            "checkpoint": 1,
            "schema": "APP_CODE",
            "effects": first_plan,
            "effectsSha256": first_plan.effects_sha256,
        },))
        self.assertEqual((1,), across.prior_checkpoints)

    def test_same_schema_view_dependency_created_earlier_is_accepted(self) -> None:
        checkpoint = _checkpoint(
            "CREATE TABLE APP_CODE.T (ID NUMBER);\n",
            "CREATE VIEW APP_CODE.V AS SELECT ID FROM APP_CODE.T;\n",
        )
        plan = feature_preflight.prepare_checkpoint(checkpoint, _state(), ())
        self.assertTrue(plan.postconditions)

    def test_existing_synonym_dependency_requires_a_selectable_target_or_direct_grant(self) -> None:
        checkpoint = _checkpoint("CREATE VIEW APP_CODE.V AS SELECT ID FROM APP_CODE.ALIAS;\n")
        local_synonym = {
            "owner": "APP_CODE", "name": "ALIAS", "table_owner": "APP_CODE",
            "table_name": "T", "db_link": "",
        }
        plan = feature_preflight.prepare_checkpoint(
            checkpoint, _state(tables=("T",), synonyms=(local_synonym,)), (),
        )
        self.assertTrue(plan.postconditions)

        external_synonym = {
            "owner": "APP_CODE", "name": "ALIAS", "table_owner": "OTHER_SCHEMA",
            "table_name": "T", "db_link": "",
        }
        with self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "dependency"):
            feature_preflight.prepare_checkpoint(checkpoint, _state(synonyms=(external_synonym,)), ())

        access = {
            "owner": "OTHER_SCHEMA", "object_name": "T", "grantee": "APP_CODE",
            "privilege": "SELECT", "grantable": "NO",
        }
        granted = feature_preflight.prepare_checkpoint(
            checkpoint, _state(synonyms=(external_synonym,), synonym_access=(access,)), (),
        )
        self.assertTrue(granted.postconditions)


if __name__ == "__main__":
    unittest.main()
