from __future__ import annotations

import itertools
import unittest

from teamlib.reconcile import Decision, reconcile
from teamlib.trees import receipt_satisfied


class ReconcileTests(unittest.TestCase):
    def test_colleague_addition_is_preserved(self):
        d = reconcile({}, {"new": b"alice"}, {})
        self.assertEqual(d.tree, {"new": b"alice"})
        self.assertFalse(d.conflicts)

    def test_stale_existing_page_preserves_head(self):
        d = reconcile({"p": b"old"}, {"p": b"alice"}, {"p": b"old"})
        self.assertEqual(d.tree, {"p": b"alice"})

    def test_colleague_deletion_stays_deleted(self):
        self.assertEqual(reconcile({"p": b"x"}, {}, {"p": b"x"}).tree, {})

    def test_conflicting_content_is_not_applied(self):
        d = reconcile({"p": b"old"}, {"p": b"a"}, {"p": b"b"})
        self.assertEqual(d.conflicts, ("p",))
        self.assertNotIn("p", d.tree)

    def test_modify_delete_conflicts(self):
        self.assertEqual(reconcile({"p": b"old"}, {"p": b"a"}, {}).conflicts, ("p",))

    def test_successive_builder_edit_and_revert(self):
        a, b, c = ({"p": x} for x in (b"A", b"B", b"C"))
        first = reconcile(a, a, b)
        self.assertEqual(first.tree, b)
        for mine in (c, a):
            d = reconcile(b, b, mine, source_base=first.tree)
            self.assertEqual(d.tree, mine)
            self.assertFalse(d.conflicts)

    def test_checkpoint_keeps_unimported_git_change(self):
        a, b, c = ({"p": x} for x in (b"A", b"B", b"C"))
        self.assertEqual(reconcile(a, b, a, source_base=b).tree, b)
        self.assertEqual(reconcile(a, b, c, source_base=b).conflicts, ("p",))

    def test_decision_has_the_declared_shape(self):
        decision = reconcile({}, {}, {})
        self.assertIsInstance(decision, Decision)
        self.assertIsInstance(decision.tree, dict)

    def test_all_single_path_states_are_total(self):
        absent = None
        values = (absent, b"", b"A", b"B")
        for base, source, head, mine in itertools.product(values, repeat=4):
            def tree(value):
                return {} if value is absent else {"p": value}

            decision = reconcile(tree(base), tree(head), tree(mine), source_base=tree(source))
            self.assertTrue(set(decision.tree) <= {"p"})
            self.assertTrue(not decision.conflicts or decision.conflicts == ("p",))

    def test_multi_path_keeps_safe_edit_and_reports_conflict(self):
        d = reconcile(
            {"safe": b"old", "conflict": b"old"},
            {"safe": b"head", "conflict": b"alice"},
            {"safe": b"old", "conflict": b"bob"},
        )
        self.assertEqual(d.tree, {"safe": b"head"})
        self.assertEqual(d.conflicts, ("conflict",))


class ReceiptTests(unittest.TestCase):
    def test_deletion_cannot_be_resurrected(self):
        result = {"application.apx": b"app demo"}
        old = {**result, "pages/p7.apx": b"old page"}
        self.assertFalse(receipt_satisfied(result, {"pages/p7.apx"}, old))
        self.assertTrue(receipt_satisfied(result, {"pages/p7.apx"}, result))
        self.assertTrue(receipt_satisfied(result, {"pages/p7.apx"}, {**result, "pages/p8.apx": b"new"}))


if __name__ == "__main__":
    unittest.main()
