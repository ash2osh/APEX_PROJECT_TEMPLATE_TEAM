from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
from types import SimpleNamespace
import base64
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

    def test_parses_framed_chunked_full_definition_and_shared_owner(self):
        definition = b"CREATE TABLE SHARED.T (ID NUMBER, NOTE VARCHAR2(4000))\n"
        encoded = base64.b64encode(definition).decode()
        result = SimpleNamespace(
            stdout=(
                "TEAM_IDENTITY|SESSION_USER=DEMO|CURRENT_SCHEMA=DEMO|DB_NAME=DB|SERVICE=SERVICE|INSTANCE_ID=CI\n"
                "TEAM_IDENTITY|SESSION_USER=DEMO|CURRENT_SCHEMA=DEMO|DB_NAME=DB|SERVICE=SERVICE|INSTANCE_ID=CI\n"
                "TEAM_INVENTORY_BEGIN|version=2|topology=shared|count=1\n"
                f"TEAM_INVENTORY_CHUNK|DEMO|TABLE|T|VALID|1|1|{encoded}\n"
                "TEAM_INVENTORY_END|count=1\n"
                "TEAM_COMPLETION|operation=read\n"
            ),
        )
        inventory = inventory_from_sqlcl_result(self.target(), result, "DEMO", "DEMO")
        self.assertEqual(inventory.topology, "shared")
        self.assertIn("shared|TABLE|T", inventory.objects)

    def test_framed_inventory_rejects_missing_chunk(self):
        result = SimpleNamespace(
            stdout=(
                "TEAM_IDENTITY|SESSION_USER=DEMO|CURRENT_SCHEMA=DEMO|DB_NAME=DB|SERVICE=SERVICE|INSTANCE_ID=CI\n"
                "TEAM_IDENTITY|SESSION_USER=DEMO|CURRENT_SCHEMA=DEMO|DB_NAME=DB|SERVICE=SERVICE|INSTANCE_ID=CI\n"
                "TEAM_INVENTORY_BEGIN|version=2|topology=shared|count=1\n"
                "TEAM_INVENTORY_END|count=1\n"
            ),
        )
        with self.assertRaises(ValueError):
            inventory_from_sqlcl_result(self.target(), result, "DEMO", "DEMO")

    def test_parses_a_verified_empty_framed_inventory(self):
        result = SimpleNamespace(
            stdout=(
                "TEAM_IDENTITY|SESSION_USER=DEMO|CURRENT_SCHEMA=DEMO|DB_NAME=DB|SERVICE=SERVICE|INSTANCE_ID=CI\n"
                "TEAM_IDENTITY|SESSION_USER=DEMO|CURRENT_SCHEMA=DEMO|DB_NAME=DB|SERVICE=SERVICE|INSTANCE_ID=CI\n"
                "TEAM_INVENTORY_BEGIN|version=2|topology=shared|count=0\n"
                "TEAM_INVENTORY_END|count=0\n"
            ),
        )
        inventory = inventory_from_sqlcl_result(self.target(), result, "DEMO", "DEMO")
        self.assertEqual(inventory.objects, {})


if __name__ == "__main__":
    unittest.main()
