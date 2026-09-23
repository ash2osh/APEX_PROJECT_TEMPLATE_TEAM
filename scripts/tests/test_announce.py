from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import unittest

from teamlib.announce import (
    Announcement,
    draft_all_clear,
    draft_import_announcement,
    draft_publish_all_clear,
    format_publish_notice,
)
from teamlib.publish import Preparation


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

    def test_draft_publish_all_clear_requires_all_apps_verified(self):
        results = {
            "hr": {"verified": True, "recovery_path": ".sync-state/recovery/hr-123"},
            "payroll": {"verified": False, "status": "failed"},
        }
        with self.assertRaises(ValueError) as ctx:
            draft_publish_all_clear(["hr", "payroll"], results)
        self.assertIn("payroll", str(ctx.exception))

    def test_draft_publish_all_clear_success_names_all_selected_apps(self):
        results = {
            "hr": {"verified": True, "recovery_path": ".sync-state/recovery/hr-123"},
            "payroll": {"verified": True, "recovery_path": ".sync-state/recovery/pay-456"},
        }
        text = draft_publish_all_clear(["hr", "payroll"], results)
        self.assertIn("hr", text)
        self.assertIn("payroll", text)
        self.assertIn("hr-123", text)
        self.assertIn("pay-456", text)
        self.assertIn("may resume", text.lower())

    def test_format_publish_notice_scoped_to_selected_apps(self):
        prep = Preparation(
            preparation_id="test-prep-123",
            version=1,
            aliases=("hr",),
            source_commit="a" * 40,
            prepared_at_utc="2026-09-23T20:00:00Z",
            record_digest="b" * 64,
            apps={
                "hr": {
                    "target": {"alias": "hr", "app_id": 101, "workspace_id": 10},
                    "roster": ["alice-uuid"],
                    "lock_report": {"alias": "hr", "status": "KNOWN", "source": "APEX_APPLICATION_LOCKED_PAGES", "pages": []},
                }
            },
            path=Path(".sync-state/publish/test-prep-123/prepare.json"),
        )
        notice = format_publish_notice(prep)
        self.assertIn("HR", notice)
        self.assertNotIn("payroll", notice.lower())
        self.assertIn("alice-uuid", notice)


if __name__ == "__main__":
    unittest.main()
