from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
import tempfile
import unittest

from teamlib.fingerprints import (
    Inventory,
    diff_inventory,
    inventory_from_rows,
    load_inventory,
    save_inventory,
)


class FingerprintTests(unittest.TestCase):
    def test_added_missing_changed_and_invalid_are_distinct(self):
        expected = inventory_from_rows([
            {"owner": "APP", "object_type": "TABLE", "object_name": "T", "definition": "cols"},
            {"owner": "APP", "object_type": "VIEW", "object_name": "V", "definition": "select 1"},
        ], topology="separate")
        actual = inventory_from_rows([
            {"owner": "APP", "object_type": "TABLE", "object_name": "T", "definition": "changed"},
            {"owner": "APP", "object_type": "PACKAGE", "object_name": "P", "definition": "invalid", "status": "INVALID"},
        ], topology="separate")
        diff = diff_inventory(expected, actual)
        self.assertIn("APP|TABLE|T", diff["changed"])
        self.assertIn("APP|VIEW|V", diff["missing"])
        self.assertIn("APP|PACKAGE|P", diff["added"])
        self.assertIn("APP|PACKAGE|P", diff["invalid"])

    def test_crlf_and_volatile_fields_are_canonicalized(self):
        one = inventory_from_rows([{"owner": "APP", "object_type": "TABLE", "object_name": "T", "definition": "a\r\nb", "last_ddl_time": "one"}])
        two = inventory_from_rows([{"owner": "APP", "object_type": "TABLE", "object_name": "T", "definition": "a\nb", "last_ddl_time": "two"}])
        self.assertEqual(one.objects, two.objects)

    def test_reserved_metadata_object_and_unknown_class_fail(self):
        with self.assertRaises(ValueError):
            inventory_from_rows([{"owner": "APP", "object_type": "TABLE", "object_name": "TEAM_MIGRATION_META", "definition": "x"}])
        with self.assertRaises(ValueError):
            inventory_from_rows([{"owner": "APP", "object_type": "MATERIALIZED_VIEW", "object_name": "M", "definition": "x"}])

    def test_inventory_manifest_round_trip(self):
        inventory = inventory_from_rows([{"owner": "SHARED", "object_type": "TABLE", "object_name": "T", "definition": "x"}], topology="shared")
        with tempfile.TemporaryDirectory(prefix="team-fingerprint-") as directory:
            path = Path(directory) / "inventory.json"
            save_inventory(inventory, path)
            self.assertEqual(load_inventory(path), inventory)


if __name__ == "__main__":
    unittest.main()
