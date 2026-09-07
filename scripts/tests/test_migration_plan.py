from __future__ import annotations

import unittest

from teamlib.migration_plan import plan_migrations


class MigrationPlanTests(unittest.TestCase):
    def test_foreign_history_is_not_deleted_history(self):
        history = {"20260906T100000__alice__x": {"status": "APPLIED", "checksum": "a" * 64, "target": "tables", "dependencies": [], "sequence": 1}}
        shared = plan_migrations({}, history, "shared")
        strict = plan_migrations({}, history, "strict")
        self.assertEqual(shared.foreign_applied, ("20260906T100000__alice__x",))
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


if __name__ == "__main__":
    unittest.main()
