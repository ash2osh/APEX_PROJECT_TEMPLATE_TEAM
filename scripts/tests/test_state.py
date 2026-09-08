from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import json
from pathlib import Path
import tempfile
import unittest

from teamlib.config import Target
from teamlib.state import (
    StateError,
    load_baseline,
    load_checkpoint,
    load_receipt,
    required_absences,
    save_capture,
    save_checkpoint,
    save_receipt,
    save_verified_baseline,
)


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-state-test-")
        self.root = Path(self.temp.name)
        self.target = Target(
            project="team-template", role="developer", environment="development",
            connection="docker-demo", instance_id="FREE", db_name="FREEPDB1",
            service="freep1", session_user="DEMO", current_schema="DEMO",
            alias="checkout", workspace_id=5402650006222933, app_id=100,
            parsing_schema="DEMO", ownership_mode="shared", binding_digest="a" * 64,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_baseline_round_trip_preserves_empty_and_binary_bytes(self):
        tree = {"application.apx": b"app\n", "empty.bin": b"", "icon.png": b"\x00\xff"}
        save_verified_baseline(self.target, "abc123", tree, root=self.root)
        baseline = load_baseline(self.target, root=self.root)
        self.assertEqual(baseline.source_commit, "abc123")
        self.assertEqual(baseline.blobs["empty.bin"], "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        self.assertEqual(baseline.tree, tree)

    def test_corrupt_blob_fails_closed(self):
        save_verified_baseline(self.target, "abc123", {"a.apx": b"a"}, root=self.root)
        baseline_path = self.root / "baselines" / self.target.state_key / "baseline.json"
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        blob_path = self.root / "blobs" / data["files"][0]["sha256"]
        blob_path.write_bytes(b"changed")
        with self.assertRaises(StateError):
            load_baseline(self.target, root=self.root)

    def test_checkpoint_and_capture_are_durable(self):
        captured = {"application.apx": b"builder"}
        reconciled = {"application.apx": b"source"}
        save_checkpoint(self.target, captured, reconciled, "head-1", "receipt-1", root=self.root)
        checkpoint = load_checkpoint(self.target, "head-1", root=self.root)
        self.assertEqual(checkpoint.captured, captured)
        self.assertEqual(checkpoint.reconciled, reconciled)
        recovery_id = save_capture(
            self.target, {}, reconciled, "head-1", captured,
            {"reason": "test"}, root=self.root,
        )
        self.assertTrue((self.root / "recovery" / recovery_id).is_dir())

    def test_checkpoint_rejects_unrelated_head_without_ancestry(self):
        save_checkpoint(self.target, {}, {}, "missing-head", "receipt", root=self.root)
        with self.assertRaises(StateError):
            load_checkpoint(self.target, "different-head", root=self.root)

    def test_required_absences_are_cumulative_and_readds_clear_tombstones(self):
        result = {"application.apx": b"app"}
        absent = required_absences(
            {"pages/old.apx"},
            {"pages/old.apx": b"old"},
            {"application.apx": b"app"},
            {"pages/new.apx": b"new"},
            {},
            result,
        )
        self.assertEqual(absent, {"pages/old.apx", "pages/new.apx"})
        self.assertEqual(required_absences(absent, {}, {}, {}, {}, {"pages/old.apx": b"readded"}), {"pages/new.apx"})

    def test_receipt_round_trip_validates_selected_commit(self):
        rid = save_receipt(
            self.target,
            result={"application.apx": b"app"},
            selected_commit="abc123",
            required_absent={"pages/deleted.apx"},
            capture_digest="capture-digest",
            root=self.root,
        )
        receipt = load_receipt(self.target, rid, root=self.root)
        self.assertEqual(receipt.selected_commit, "abc123")
        self.assertEqual(receipt.required_absent, {"pages/deleted.apx"})
        with self.assertRaises(StateError):
            save_receipt(
                self.target, {"bad\\path": b"x"}, "abc", set(), "d", root=self.root
            )


if __name__ == "__main__":
    unittest.main()
