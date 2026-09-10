from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import unittest

from teamlib.migration_bundle import BundleError
from teamlib.migration_plan import plan_migrations, plan_redo, plan_undo


class MigrationPlanTests(unittest.TestCase):
    class Bundle:
        def __init__(self, migration_id, checksum=None, dependencies=(), reversible=True):
            self.id = migration_id
            self.checksum = checksum or (migration_id[-1] * 64)
            self.target = "tables"
            self.dependencies = tuple(dependencies)
            self.stamp = migration_id[:15]
            self.destructive = False
            self.reversible = reversible

    def test_foreign_history_is_not_deleted_history(self):
        history = {
            "20260906T100000__alice__x": {"status": "APPLIED", "checksum": "a" * 64, "target": "tables", "dependencies": [], "sequence": 1},
            "20260906T100001__alice__y": {"status": "REVERTED", "checksum": "b" * 64, "target": "tables", "dependencies": [], "sequence": 2},
        }
        shared = plan_migrations({}, history, "shared")
        strict = plan_migrations({}, history, "strict")
        self.assertEqual(shared.foreign_applied, ("20260906T100000__alice__x",))
        self.assertEqual(shared.foreign_reverted, ("20260906T100001__alice__y",))
        self.assertFalse(shared.errors)
        self.assertTrue(strict.errors)

    def test_checksum_conflict_and_unresolved_attempt_block(self):
        class Bundle:
            id = "20260906T100000__alice__x"
            checksum = "b" * 64
            target = "tables"
            dependencies = ()
            stamp = "20260906T100000"

        history = {Bundle.id: {"status": "APPLIED", "checksum": "a" * 64}}
        plan = plan_migrations({Bundle.id: Bundle()}, history, "shared")
        self.assertTrue(plan.errors)
        history[Bundle.id] = {"status": "RUNNING", "checksum": Bundle.checksum}
        self.assertTrue(plan_migrations({Bundle.id: Bundle()}, history, "shared").errors)

    def test_reverted_local_migrations_are_not_pending_and_block_dependents(self):
        reverted_id = "20260906T100000__alice__a"
        pending_id = "20260906T100001__alice__b"
        applied_id = "20260906T100002__alice__c"
        reverted = self.Bundle(reverted_id, "a" * 64)
        pending = self.Bundle(pending_id, "b" * 64, ((reverted_id, reverted.checksum),))
        applied = self.Bundle(applied_id, "c" * 64, ((reverted_id, reverted.checksum),))
        history = {
            reverted_id: {"status": "REVERTED", "checksum": reverted.checksum, "sequence": 2},
            applied_id: {"status": "APPLIED", "checksum": applied.checksum, "sequence": 3},
        }
        plan = plan_migrations({reverted_id: reverted, pending_id: pending, applied_id: applied}, history, "shared")
        self.assertNotIn(reverted_id, plan.pending)
        self.assertTrue(any("REVERTED" in error for error in plan.errors))

    def test_undo_requires_the_current_global_lifo_top(self):
        first_id = "20260906T100000__alice__a"
        second_id = "20260906T100001__alice__b"
        bundles = {first_id: self.Bundle(first_id), second_id: self.Bundle(second_id)}
        history = {
            first_id: {"status": "APPLIED", "checksum": bundles[first_id].checksum, "sequence": 1},
            second_id: {"status": "APPLIED", "checksum": bundles[second_id].checksum, "sequence": 2},
        }
        with self.assertRaisesRegex(BundleError, "current top"):
            plan_undo(bundles, history, first_id)
        self.assertEqual(plan_undo(bundles, history, second_id), second_id)
        history[second_id] = {"status": "REVERTED", "checksum": bundles[second_id].checksum, "sequence": 3}
        self.assertEqual(plan_undo(bundles, history, first_id), first_id)

    def test_undo_requires_a_reversible_top(self):
        migration_id = "20260906T100000__alice__a"
        bundle = self.Bundle(migration_id, reversible=False)
        history = {migration_id: {"status": "APPLIED", "checksum": bundle.checksum, "sequence": 1}}
        with self.assertRaisesRegex(BundleError, "not reversible"):
            plan_undo({migration_id: bundle}, history, migration_id)

    def test_redo_requires_reverted_checksum_and_applied_dependencies(self):
        dependency_id = "20260906T100000__alice__a"
        migration_id = "20260906T100001__alice__b"
        dependency = self.Bundle(dependency_id)
        migration = self.Bundle(migration_id, dependencies=((dependency_id, dependency.checksum),))
        bundles = {dependency_id: dependency, migration_id: migration}
        history = {
            dependency_id: {"status": "APPLIED", "checksum": dependency.checksum, "sequence": 1},
            migration_id: {"status": "REVERTED", "checksum": migration.checksum, "sequence": 2},
        }
        self.assertEqual(plan_redo(bundles, history, migration_id), migration_id)
        history[dependency_id]["status"] = "REVERTED"
        with self.assertRaisesRegex(BundleError, "dependency"):
            plan_redo(bundles, history, migration_id)

    def test_independent_reverted_migrations_can_be_redone_in_either_order(self):
        first_id = "20260906T100000__alice__a"
        second_id = "20260906T100001__alice__b"
        first = self.Bundle(first_id)
        second = self.Bundle(second_id)
        bundles = {first_id: first, second_id: second}
        history = {
            first_id: {"status": "REVERTED", "checksum": first.checksum, "sequence": 3},
            second_id: {"status": "REVERTED", "checksum": second.checksum, "sequence": 4},
        }
        self.assertEqual(plan_redo(bundles, history, first_id), first_id)
        self.assertEqual(plan_redo(bundles, history, second_id), second_id)


if __name__ == "__main__":
    unittest.main()
