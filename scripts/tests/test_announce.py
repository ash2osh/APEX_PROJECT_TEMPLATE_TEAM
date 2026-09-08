from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import unittest

from teamlib.announce import Announcement, draft_all_clear, draft_import_announcement


class AnnounceTests(unittest.TestCase):
    def test_announcement_names_target_and_unknown_duration(self):
        announcement = draft_import_announcement(
            "checkout", "abc123", target={"app_id": 100, "workspace_id": 7, "instance_id": "FREE", "project": "team"},
            roster=["alice", "bob"], changed_paths=["pages/p1.apx"],
        )
        self.assertIsInstance(announcement, Announcement)
        self.assertIn("application 100", announcement.text)
        self.assertIn("pages/p1.apx", announcement.text)
        self.assertIn("unknown", announcement.text.lower())
        self.assertIn("may resume", draft_all_clear("checkout", {"verified": True}).lower())


if __name__ == "__main__":
    unittest.main()
