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


def _state(*, tables=(), columns=(), indexes=(), views=(), synonyms=(), grants=(), invalid=(), synonym_access=(), constraints=(), sequences=()) -> dict:
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
            "constraints": list(constraints), "triggers": [],
            "sequences": [{"name": name} if isinstance(name, str) else name for name in sequences],
            "stored-code": [],
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

    def test_rerun_of_an_applied_one_time_ddl_says_it_cannot_be_released_twice(self) -> None:
        # The DDL of an applied release reaches the occupied-name check when it is prepared again.
        # The refusal must say that the one-time DDL was already applied, not only that a name is taken.
        checkpoint = _checkpoint("CREATE INDEX APP_CODE.IX_T ON APP_CODE.T (ID);\n")
        state = _state(tables=("T",), columns=(("T", "ID"),), indexes=(("IX_T", "T"),))
        with self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "occupied.*one-time.*new revision"):
            feature_preflight.prepare_checkpoint(checkpoint, state, ())

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

    def test_constraints_and_comments_get_catalog_checks_and_scope(self) -> None:
        checkpoint = _checkpoint(
            "ALTER TABLE APP_CODE.C ADD CONSTRAINT FK_C_P FOREIGN KEY (P_ID) REFERENCES APP_CODE.P (ID) ON DELETE CASCADE;\n"
            "ALTER TABLE APP_CODE.C ADD UNIQUE (A, B);\n"
            "COMMENT ON COLUMN APP_CODE.C.A IS 'it''s A';\n"
            "COMMENT ON TABLE APP_CODE.C IS '';\n"
        )
        state = _state(
            tables=("C", "P"), columns=(("C", "A"), ("C", "B"), ("C", "P_ID"), ("P", "ID")),
        )
        scope = feature_preflight.required_scope(checkpoint)
        self.assertIn(ObjectKey("APP_CODE", "C", "TABLE"), scope.object_keys)
        self.assertIn(ObjectKey("APP_CODE", "P", "TABLE"), scope.prerequisite_keys)

        plan = feature_preflight.prepare_checkpoint(checkpoint, state, ())

        before = "\n".join(check.sql for check in plan.preconditions)
        after = "\n".join(check.sql for check in plan.postconditions)
        self.assertIn("C.CONSTRAINT_NAME = 'FK_C_P'", before)
        self.assertIn("NOT EXISTS (SELECT 1 FROM ALL_CONSTRAINTS C WHERE C.OWNER = :target_schema AND C.CONSTRAINT_NAME = 'FK_C_P')", before)
        self.assertIn("C.CONSTRAINT_TYPE = 'R'", after)
        self.assertIn("C.DELETE_RULE = 'CASCADE'", after)
        self.assertIn("P.TABLE_NAME = 'P'", after)
        self.assertIn("C.CONSTRAINT_TYPE IN ('P','U')", before, "an unnamed UNIQUE refuses an identical key")
        self.assertIn("ALL_COL_COMMENTS", after)
        self.assertIn("M.COMMENTS = 'it''s A'", after)
        self.assertIn("M.COMMENTS IS NULL", after, "an empty comment clears the comment")
        record = feature_preflight.plan_to_dict(plan)
        feature_preflight.validate_plan_record(record, checkpoint, state["structural"])

    def test_constraint_and_comment_targets_must_exist(self) -> None:
        cases = {
            "table": ("ALTER TABLE APP_CODE.C ADD CONSTRAINT PK PRIMARY KEY (ID);\n", _state()),
            "column": ("ALTER TABLE APP_CODE.C ADD CONSTRAINT PK PRIMARY KEY (ID);\n", _state(tables=("C",), columns=(("C", "OTHER"),))),
            "parent": (
                "ALTER TABLE APP_CODE.C ADD CONSTRAINT FK FOREIGN KEY (A) REFERENCES APP_CODE.P (ID);\n",
                _state(tables=("C",), columns=(("C", "A"),)),
            ),
            "comment table": ("COMMENT ON TABLE APP_CODE.GONE IS 'x';\n", _state()),
            "comment column": ("COMMENT ON COLUMN APP_CODE.C.GONE IS 'x';\n", _state(tables=("C",), columns=(("C", "A"),))),
        }
        for label, (source, state) in cases.items():
            with self.subTest(label=label), self.assertRaises(feature_preflight.StructuralPreflightError):
                feature_preflight.prepare_checkpoint(_checkpoint(source), state, ())

    def test_constraint_name_is_unique_and_a_key_name_also_takes_the_index_namespace(self) -> None:
        state = _state(tables=("C",), columns=(("C", "ID"),), constraints=({"name": "PK_C", "table_name": "C"},))
        with self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "PK_C"):
            feature_preflight.prepare_checkpoint(
                _checkpoint("ALTER TABLE APP_CODE.C ADD CONSTRAINT PK_C PRIMARY KEY (ID);\n"), state, (),
            )
        indexed = _state(tables=("C",), columns=(("C", "ID"),), indexes=(("UX", "C"),))
        with self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "UX"):
            feature_preflight.prepare_checkpoint(
                _checkpoint("ALTER TABLE APP_CODE.C ADD CONSTRAINT UX UNIQUE (ID);\n"), indexed, (),
            )
        with self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "CK"):
            feature_preflight.prepare_checkpoint(
                _checkpoint(
                    "ALTER TABLE APP_CODE.C ADD CONSTRAINT CK CHECK (ID > 0);\n",
                    "ALTER TABLE APP_CODE.C ADD CONSTRAINT CK CHECK (ID > 1);\n",
                ),
                _state(tables=("C",), columns=(("C", "ID"),)), (),
            )

    def test_table_and_columns_created_in_the_same_checkpoint_are_not_demanded_before_it_runs(self) -> None:
        checkpoint = _checkpoint(
            "CREATE TABLE APP_CODE.N (ID NUMBER);\n"
            "ALTER TABLE APP_CODE.E ADD (X NUMBER);\n"
            "ALTER TABLE APP_CODE.N ADD CONSTRAINT PK_N PRIMARY KEY (ID);\n"
            "ALTER TABLE APP_CODE.E ADD CONSTRAINT UX_E UNIQUE (X);\n"
            "COMMENT ON TABLE APP_CODE.N IS 'new';\n"
            "COMMENT ON COLUMN APP_CODE.E.X IS 'added here';\n"
        )
        state = _state(tables=("E",), columns=(("E", "ID"),))
        plan = feature_preflight.prepare_checkpoint(checkpoint, state, ())

        by_id = {check.id: check.sql for check in plan.preconditions}
        # Statement 2 adds a column to an existing table, so it proves the table.
        self.assertIn("ALL_TABLES", by_id["structural-before-0002-01"])
        # Statement 3 constrains a table this checkpoint creates: nothing may demand it before it runs.
        self.assertNotIn("ALL_TABLES", by_id["structural-before-0003"])
        self.assertNotIn("ALL_TAB_COLUMNS", by_id["structural-before-0003"])
        # Statement 4 constrains a column that statement 2 adds to an existing table: the table
        # is still proved, the column is not demanded yet.
        self.assertIn("ALL_TABLES", by_id["structural-before-0004"])
        self.assertNotIn("ALL_TAB_COLUMNS", by_id["structural-before-0004"])
        # The comment on the new table has nothing to prove beforehand; the one on the added
        # column proves its existing table but not the column.
        self.assertNotIn("structural-before-0005", by_id)
        self.assertIn("ALL_OBJECTS", by_id["structural-before-0006"])
        self.assertNotIn("ALL_TAB_COLUMNS", by_id["structural-before-0006"])
        # Every effect still has its after-state proof.
        after = {check.id for check in plan.postconditions}
        for number in range(3, 7):
            self.assertIn(f"structural-after-{number:04d}", after)

    def test_earlier_checkpoint_constraint_is_removed_from_a_fresh_capture(self) -> None:
        first = _checkpoint("ALTER TABLE APP_CODE.C ADD CONSTRAINT FK FOREIGN KEY (P_ID) REFERENCES APP_CODE.P (ID);\n"
                            "ALTER TABLE APP_CODE.C ADD CONSTRAINT PK_C PRIMARY KEY (ID);\n"
                            "ALTER TABLE APP_CODE.C ADD UNIQUE (A);\n")
        state = _state(tables=("C", "P"), columns=(("C", "ID"), ("C", "P_ID"), ("C", "A"), ("P", "ID")))
        first_plan = feature_preflight.prepare_checkpoint(first, state, ())
        later = _checkpoint("COMMENT ON TABLE APP_CODE.C IS 'later';\n")
        plan = feature_preflight.prepare_checkpoint(later, state, ({
            "checkpoint": 1, "schema": "APP_CODE", "effects": first_plan, "effectsSha256": first_plan.effects_sha256,
        },))
        self.assertEqual((1,), plan.prior_checkpoints)

        fresh = {
            **state["structural"],
            "constraints": [
                {"name": "FK", "table_name": "C", "constraint_type": "R", "columns": ["P_ID"]},
                {"name": "PK_C", "table_name": "C", "constraint_type": "P", "columns": ["ID"]},
                {"name": "SYS_C001", "table_name": "C", "constraint_type": "U", "columns": ["A"]},
                {"name": "KEEP", "table_name": "C", "constraint_type": "C", "columns": ["A"]},
            ],
            "indexes": [{"name": "PK_C", "table_name": "C"}, {"name": "IX_KEEP", "table_name": "C"}],
        }
        stripped = feature_preflight.strip_prior_effects(fresh, plan.prior_effects)
        self.assertEqual(["KEEP"], [row["name"] for row in stripped["constraints"]])
        self.assertEqual(["IX_KEEP"], [row["name"] for row in stripped["indexes"]])

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

    def test_view_with_cte_and_subqueries_gets_dependencies_and_checks(self) -> None:
        checkpoint = _checkpoint(
            "CREATE VIEW APP_CODE.V_CTE AS WITH CTE AS (SELECT ID FROM APP_CODE.T1) SELECT ID FROM CTE;\n"
        )
        state = _state(tables=("T1",), columns=(("T1", "ID"),))
        scope = feature_preflight.required_scope(checkpoint)
        self.assertIn(ObjectKey("APP_CODE", "V_CTE", "VIEW"), scope.object_keys)
        self.assertIn(ObjectKey("APP_CODE", "T1", "TABLE"), scope.prerequisite_keys)
        self.assertNotIn(ObjectKey("APP_CODE", "CTE", "TABLE"), scope.prerequisite_keys)

        plan = feature_preflight.prepare_checkpoint(checkpoint, state, ())
        before = "\n".join(check.sql for check in plan.preconditions)
        after = "\n".join(check.sql for check in plan.postconditions)
        self.assertIn("ALL_OBJECTS", before)
        self.assertIn("'T1'", before)
        self.assertIn("ALL_OBJECTS", after)
        self.assertIn("'VIEW'", after)
        self.assertIn("'V_CTE'", after)

    def test_alter_table_modify_column_gets_before_and_after_checks(self) -> None:
        checkpoint = _checkpoint(
            "ALTER TABLE APP_CODE.T MODIFY (NAME VARCHAR2(100 BYTE) NOT NULL, CNT NUMBER(10, 2));\n"
        )
        state = _state(tables=("T",), columns=(("T", "NAME"), ("T", "CNT")))
        scope = feature_preflight.required_scope(checkpoint)
        self.assertIn(ObjectKey("APP_CODE", "T", "TABLE"), scope.object_keys)

        plan = feature_preflight.prepare_checkpoint(checkpoint, state, ())
        before = "\n".join(check.sql for check in plan.preconditions)
        after = "\n".join(check.sql for check in plan.postconditions)

        self.assertIn("ALL_TABLES", before)
        self.assertIn("ALL_TAB_COLUMNS", before)
        self.assertIn("'NAME'", before)
        self.assertIn("'CNT'", before)

        self.assertIn("C.DATA_TYPE = 'VARCHAR2'", after)
        self.assertIn("C.CHAR_LENGTH = 100", after)
        self.assertIn("C.CHAR_USED = 'B'", after)
        self.assertIn("C.NULLABLE = 'N'", after)
        self.assertIn("C.DATA_TYPE = 'NUMBER'", after)
        self.assertIn("C.DATA_PRECISION = 10 AND C.DATA_SCALE = 2", after)

        record = feature_preflight.plan_to_dict(plan)
        feature_preflight.validate_plan_record(record, checkpoint, state["structural"])

    def test_alter_modify_missing_table_or_column_refuses(self) -> None:
        checkpoint_missing_table = _checkpoint("ALTER TABLE APP_CODE.MISSING_T MODIFY (A NUMBER);\n")
        with self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "MISSING_T"):
            feature_preflight.prepare_checkpoint(checkpoint_missing_table, _state(), ())

        checkpoint_missing_col = _checkpoint("ALTER TABLE APP_CODE.T MODIFY (MISSING_COL NUMBER);\n")
        with self.assertRaisesRegex(feature_preflight.StructuralPreflightError, "MISSING_COL"):
            feature_preflight.prepare_checkpoint(checkpoint_missing_col, _state(tables=("T",), columns=(("T", "A"),)), ())

    def test_expression_index_gets_before_and_after_checks(self) -> None:
        checkpoint = _checkpoint(
            "CREATE UNIQUE INDEX APP_CODE.IX_EXPR ON APP_CODE.T (UPPER(NAME), CNT);\n"
        )
        state = _state(tables=("T",), columns=(("T", "NAME"), ("T", "CNT")))
        scope = feature_preflight.required_scope(checkpoint)
        self.assertIn(ObjectKey("APP_CODE", "IX_EXPR", "INDEX"), scope.object_keys)

        plan = feature_preflight.prepare_checkpoint(checkpoint, state, ())
        before = "\n".join(check.sql for check in plan.preconditions)
        after = "\n".join(check.sql for check in plan.postconditions)

        self.assertIn("ALL_TABLES", before)
        self.assertIn("NOT EXISTS (SELECT 1 FROM ALL_INDEXES I WHERE I.OWNER = :target_schema AND I.INDEX_NAME = 'IX_EXPR')", before)

        self.assertIn("I.UNIQUENESS = 'UNIQUE'", after)
        self.assertIn("COUNT(*) FROM ALL_IND_COLUMNS C WHERE C.INDEX_OWNER = :target_schema AND C.INDEX_NAME = 'IX_EXPR') = 2", after)
        self.assertIn("COUNT(*) FROM ALL_IND_EXPRESSIONS E WHERE E.INDEX_OWNER = :target_schema AND E.INDEX_NAME = 'IX_EXPR') = 1", after)

        record = feature_preflight.plan_to_dict(plan)
        feature_preflight.validate_plan_record(record, checkpoint, state["structural"])

    def test_unqualified_object_and_execute_grants(self) -> None:
        checkpoint = _checkpoint(
            "GRANT EXECUTE ON PKG TO REPORTER;\n"
            "GRANT SELECT ON MY_SEQ TO REPORTER;\n"
        )
        state = _state()
        state["structural"]["stored-code"] = [{"name": "PKG", "type": "PACKAGE"}]
        state["structural"]["sequences"] = [{"name": "MY_SEQ"}]
        scope = feature_preflight.required_scope(checkpoint)
        self.assertIn(ObjectKey("APP_CODE", "PKG", "PACKAGE"), scope.prerequisite_keys)
        self.assertIn(ObjectKey("APP_CODE", "MY_SEQ", "SEQUENCE"), scope.prerequisite_keys)

        plan = feature_preflight.prepare_checkpoint(checkpoint, state, ())
        after = "\n".join(check.sql for check in plan.postconditions)
        self.assertIn("PRIVILEGE = 'EXECUTE'", after)
        self.assertIn("TABLE_NAME = 'PKG'", after)
        self.assertIn("PRIVILEGE = 'SELECT'", after)
        self.assertIn("TABLE_NAME = 'MY_SEQ'", after)


if __name__ == "__main__":
    unittest.main()
