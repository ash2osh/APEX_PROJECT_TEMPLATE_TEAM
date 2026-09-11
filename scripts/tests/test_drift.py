from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import unittest

from teamlib.config import Target
from teamlib.drift import drift_status, observed_frontier_drift
from teamlib.fingerprints import Inventory, drift_is_clean, inventory_from_rows
from teamlib.migration_store import MigrationStoreError


def inventory(rows) -> Inventory:
    return inventory_from_rows(rows, schema_set_digest="a" * 64)


def row(name: str, definition: str = "x") -> dict[str, str]:
    return {"logical_owner": "tables", "object_type": "TABLE", "object_name": name, "definition": definition}


class DriftIsCleanTests(unittest.TestCase):
    def test_an_empty_diff_is_clean(self):
        self.assertTrue(drift_is_clean({"added": (), "missing": (), "changed": (), "invalid": (), "topology_mismatch": False}))

    def test_any_populated_list_is_drift(self):
        for key in ("added", "missing", "changed", "invalid"):
            with self.subTest(key=key):
                diff = {"added": (), "missing": (), "changed": (), "invalid": (), "topology_mismatch": False}
                diff[key] = ("T",)
                self.assertFalse(drift_is_clean(diff))

    def test_a_topology_mismatch_alone_is_not_clean(self):
        """An empty schema on a mismatched topology reports empty lists.

        diff_inventory fills `invalid` from the actual side, so comparing an
        empty live schema across a topology change produces four empty lists
        and topology_mismatch=True. A predicate that checks only the lists
        calls that clean.
        """
        self.assertFalse(
            drift_is_clean({"added": (), "missing": (), "changed": (), "invalid": (), "topology_mismatch": True})
        )


class ObservedFrontierDriftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = Target(
            project="team", role="integration", environment="development", connection="meta",
            instance_id="FREE", db_name="FREEPDB1", service="freep1", session_user="META",
            current_schema="META", alias=None, workspace_id=None, app_id=None,
            parsing_schema=None, ownership_mode="shared", binding_digest="a" * 64,
        )
        self.actual = inventory([row("T")])

    def store(self, *, observations, inventories=None, error=None):
        class Store:
            def read_state(_self, _target):
                if error is not None:
                    raise error
                return {"observations": observations}

            def read_inventories(_self, _target):
                return inventories or {}

        return Store()

    def test_no_adopted_frontier_is_unknown(self):
        result = observed_frontier_drift(self.store(observations=[]), self.metadata, self.actual)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("no observed migration frontier", result["reason"])

    def test_a_matching_frontier_is_clean(self):
        result = observed_frontier_drift(
            self.store(
                observations=[{"after": self.actual.digest}],
                inventories={self.actual.digest: self.actual.as_dict()},
            ),
            self.metadata,
            self.actual,
        )
        self.assertEqual(result["status"], "clean")
        self.assertEqual(result["digest"], self.actual.digest)

    def test_a_differing_frontier_is_drift(self):
        frontier = inventory([row("T", "changed")])
        result = observed_frontier_drift(
            self.store(
                observations=[{"after": frontier.digest}],
                inventories={frontier.digest: frontier.as_dict()},
            ),
            self.metadata,
            self.actual,
        )
        self.assertEqual(result["status"], "drift")
        self.assertEqual(result["diff"]["changed"], ("tables|TABLE|T",))

    def test_a_missing_frontier_manifest_is_unknown_not_clean(self):
        result = observed_frontier_drift(
            self.store(observations=[{"after": "b" * 64}], inventories={}),
            self.metadata,
            self.actual,
        )
        self.assertEqual(result["status"], "unknown")

    def test_an_unreadable_store_is_unknown_not_clean(self):
        result = observed_frontier_drift(
            self.store(observations=[], error=MigrationStoreError("metadata is unreachable")),
            self.metadata,
            self.actual,
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIn("unreachable", result["reason"])


class DriftStatusTests(unittest.TestCase):
    CLEAN = {"added": (), "missing": (), "changed": (), "invalid": (), "topology_mismatch": False}
    DIRTY = {"added": ("T",), "missing": (), "changed": (), "invalid": (), "topology_mismatch": False}

    def test_both_clean_is_clean(self):
        self.assertEqual(drift_status(self.CLEAN, {"status": "clean"}), "clean")

    def test_canonical_drift_is_drift(self):
        self.assertEqual(drift_status(self.DIRTY, {"status": "clean"}), "drift")

    def test_frontier_drift_is_drift_even_when_canonical_is_clean(self):
        self.assertEqual(drift_status(self.CLEAN, {"status": "drift"}), "drift")

    def test_an_unknown_frontier_is_never_reported_clean(self):
        """Unknown must not collapse to clean.

        A caller that read `unknown` as `clean` would apply migrations onto a
        schema the tooling cannot prove matches what the store last accepted.
        """
        self.assertEqual(drift_status(self.CLEAN, {"status": "unknown"}), "unknown")
        self.assertEqual(drift_status(self.CLEAN, {"status": "unavailable"}), "unknown")


if __name__ == "__main__":
    unittest.main()
