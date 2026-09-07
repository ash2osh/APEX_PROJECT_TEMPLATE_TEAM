from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from teamlib.config import Target
from teamlib.live_inventory import inventory_from_sqlcl_result, inventory_target


class LiveInventoryTests(unittest.TestCase):
    def target(self):
        return Target("team", "replay", "test", "ci", "CI", "DB", "SERVICE", "DEMO", "DEMO", None, None, None, None, "shared", "a" * 64)

    def test_parses_marked_rows_and_rejects_unqualified_identity(self):
        result = SimpleNamespace(
            stdout="TEAM_IDENTITY|SESSION_USER=DEMO|CURRENT_SCHEMA=DEMO|DB_NAME=DB|SERVICE=SERVICE|INSTANCE_ID=CI\nTEAM_IDENTITY|SESSION_USER=DEMO|CURRENT_SCHEMA=DEMO|DB_NAME=DB|SERVICE=SERVICE|INSTANCE_ID=CI\nTEAM_INVENTORY|DEMO|TABLE|EMP|VALID|" + "b" * 64 + "\nTEAM_COMPLETION|operation=read\n",
            identity={"SESSION_USER": "DEMO", "CURRENT_SCHEMA": "DEMO", "DB_NAME": "DB", "SERVICE": "SERVICE", "INSTANCE_ID": "CI"},
        )
        inventory = inventory_from_sqlcl_result(self.target(), result, "DEMO", "DEMO_META")
        self.assertIn("DEMO|TABLE|EMP", inventory.objects)
        with self.assertRaises(ValueError):
            inventory_from_sqlcl_result(self.target(), SimpleNamespace(stdout="TEAM_INVENTORY|bad", identity={}), "DEMO", "DEMO_META")

    def test_inventory_target_uses_one_read_only_runner(self):
        calls = []

        def runner(target, operation, driver, work):
            calls.append((target, operation, Path(driver).read_text(encoding="utf-8")))
            return SimpleNamespace(
                stdout="TEAM_IDENTITY|SESSION_USER=DEMO|CURRENT_SCHEMA=DEMO|DB_NAME=DB|SERVICE=SERVICE|INSTANCE_ID=CI\nTEAM_IDENTITY|SESSION_USER=DEMO|CURRENT_SCHEMA=DEMO|DB_NAME=DB|SERVICE=SERVICE|INSTANCE_ID=CI\nTEAM_INVENTORY|DEMO|TABLE|EMP|VALID|" + "b" * 64 + "\nTEAM_COMPLETION|operation=read\n",
                identity={"SESSION_USER": "DEMO", "CURRENT_SCHEMA": "DEMO", "DB_NAME": "DB", "SERVICE": "SERVICE", "INSTANCE_ID": "CI"},
            )

        with tempfile.TemporaryDirectory(prefix="team-inventory-") as directory:
            inventory = inventory_target(self.target(), "DEMO", "DEMO_META", directory, runner=runner)
            self.assertEqual(inventory.topology, "separate")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], "read")


if __name__ == "__main__":
    unittest.main()
