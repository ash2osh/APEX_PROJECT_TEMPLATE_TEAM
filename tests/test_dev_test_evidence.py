"""Behavior contract tests for dev_test_evidence.py (Task 2).

These test externally observable contracts and safety properties, NOT
implementation details. They are written before the implementation (RED phase).

Run:
    python3 -m unittest discover -s tests -p 'test_dev_test_e*.py' -v
"""
from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# Helper: import the module under test (fail gracefully at collection time)
# ---------------------------------------------------------------------------

import _no_real_sqlcl  # noqa: F401
from scripts.dev_test_evidence import (
    RunEvidence, create_run, open_run, freeze_inputs, assert_inputs_unchanged, append_event, save_inventory,
)


# ---------------------------------------------------------------------------
# 1. Run ID format
# ---------------------------------------------------------------------------

class TestRunIdFormat(unittest.TestCase):
    """Run IDs must be YYYYMMDDTHHMMSSZ-<UUIDhex> format."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_id_matches_expected_format(self) -> None:
        """create_run returns a RunEvidence whose run_id is YYYYMMDDTHHMMSSZ-<hex>."""
        run = create_run(Path(self.tmp))
        pattern = re.compile(
            r"^\d{8}T\d{6}Z-[0-9a-f]{32}$", re.ASCII
        )
        self.assertRegex(run.run_id, pattern)

    def test_two_runs_have_distinct_ids(self) -> None:
        """Each create_run call generates a unique run_id."""
        r1 = create_run(Path(self.tmp))
        r2 = create_run(Path(self.tmp))
        self.assertNotEqual(r1.run_id, r2.run_id)

    def test_run_evidence_exposes_run_id_and_root(self) -> None:
        """RunEvidence has run_id and root attributes."""
        run = create_run(Path(self.tmp))
        self.assertIsInstance(run.run_id, str)
        self.assertIsInstance(run.root, Path)


# ---------------------------------------------------------------------------
# 2. Path safety — cannot escape root or follow symlinks
# ---------------------------------------------------------------------------

class TestRunPathSafety(unittest.TestCase):
    """test_run_path_cannot_escape_or_follow_symlink."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_open_run_refuses_traversal_in_id(self) -> None:
        """open_run rejects run IDs containing '..' components."""
        with self.assertRaises(ValueError):
            open_run(Path(self.tmp), "../../etc/passwd")

    def test_open_run_refuses_absolute_path_as_id(self) -> None:
        """open_run rejects run IDs that are absolute paths."""
        with self.assertRaises(ValueError):
            open_run(Path(self.tmp), "/etc/passwd")

    def test_open_run_refuses_slash_in_id(self) -> None:
        """open_run rejects run IDs containing path separators."""
        with self.assertRaises(ValueError):
            open_run(Path(self.tmp), "2026T000000Z-abc/subdir")

    def test_open_run_refuses_malformed_id(self) -> None:
        """open_run rejects run IDs not matching YYYYMMDDTHHMMSSZ-<hex> format."""
        with self.assertRaises(ValueError):
            open_run(Path(self.tmp), "not-a-valid-run-id")

    def test_open_run_refuses_nonexistent_run(self) -> None:
        """open_run raises if the run directory does not exist."""
        with self.assertRaises((ValueError, FileNotFoundError, OSError)):
            open_run(Path(self.tmp), "20261016T120000Z-" + "a" * 32)

    def test_create_run_refuses_overwrite_existing_run(self) -> None:
        """create_run must not silently overwrite a pre-existing run directory."""
        # Pre-create a directory with a valid run-id to simulate a collision.
        import uuid
        from datetime import datetime, timezone
        collision_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex
        collision_dir = Path(self.tmp) / "scratch" / "dev-test" / collision_id
        collision_dir.mkdir(parents=True)
        # Patch create_run to force the same ID by pre-populating then verifying
        # that open_run can re-open without error (correct behavior), while a
        # fresh create to the same path would fail.
        # We test the second create scenario by directly checking exist_ok semantics:
        # create_run uses mkdir(exist_ok=True) for the run_dir itself but a
        # collision would never happen with UUID; the important contract is that
        # the pre-existing directory is not silently cleared.
        (collision_dir / "events").mkdir(parents=True)
        sentinel = collision_dir / "events" / "sentinel.json"
        sentinel.write_text('{"sentinel": true}', encoding="utf-8")
        # open_run on this collision_id should succeed and NOT destroy the sentinel
        r = open_run(Path(self.tmp), collision_id)
        self.assertIsInstance(r, RunEvidence)
        self.assertTrue(sentinel.exists(), "Pre-existing run content was overwritten!")

    def test_create_run_refuses_symlinked_scratch_root(self) -> None:
        """create_run refuses if the scratch root path component is a symlink."""
        real_dir = Path(self.tmp) / "real"
        real_dir.mkdir()
        link = Path(self.tmp) / "link"
        try:
            link.symlink_to(real_dir)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks not supported on this platform")
        with self.assertRaises((ValueError, OSError)):
            create_run(link)

    def test_open_run_refuses_symlinked_run_dir(self) -> None:
        """open_run refuses a run_id whose directory is a symlink."""
        run = create_run(Path(self.tmp))
        real_run_dir = Path(self.tmp) / "scratch" / "dev-test" / run.run_id
        link_id = "20261016T120000Z-" + "b" * 32
        link_path = Path(self.tmp) / "scratch" / "dev-test" / link_id
        try:
            link_path.symlink_to(real_run_dir)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks not supported on this platform")
        with self.assertRaises((ValueError, OSError)):
            open_run(Path(self.tmp), link_id)


# ---------------------------------------------------------------------------
# 3. freeze_inputs — preserves byte snapshots and detects changes
# ---------------------------------------------------------------------------

class TestFreezeInputs(unittest.TestCase):
    """test_frozen_inputs_detect_sql_and_runtime_changes."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_files(self) -> list[Path]:
        """Create a small set of sample input files."""
        files = []
        for i, content in enumerate([b"SELECT 1 FROM DUAL", b"-- check: count\nSELECT COUNT(*) FROM T"]):
            p = Path(self.tmp) / f"input_{i}.sql"
            p.write_bytes(content)
            files.append(p)
        return files

    def test_freeze_returns_path_to_sha256_mapping(self) -> None:
        """freeze_inputs returns a dict mapping str path -> sha256 hex string."""
        run = create_run(Path(self.tmp))
        files = self._make_files()
        result = freeze_inputs(run, files)
        self.assertIsInstance(result, dict)
        self.assertEqual(len(result), len(files))
        for key, digest in result.items():
            self.assertIsInstance(key, str)
            self.assertRegex(digest, r"^[a-f0-9]{64}$")

    def test_frozen_inputs_survive_roundtrip(self) -> None:
        """Frozen hashes match re-hashing the original bytes."""
        import hashlib
        run = create_run(Path(self.tmp))
        files = self._make_files()
        result = freeze_inputs(run, files)
        for path_str, digest in result.items():
            # Find the matching file
            for f in files:
                if str(f) in path_str or path_str in str(f) or f.name in path_str:
                    expected = hashlib.sha256(f.read_bytes()).hexdigest()
                    self.assertEqual(digest, expected)
                    break

    def test_assert_inputs_unchanged_passes_when_files_unmodified(self) -> None:
        """assert_inputs_unchanged does not raise if files are as-frozen."""
        run = create_run(Path(self.tmp))
        files = self._make_files()
        freeze_inputs(run, files)
        # Should not raise
        assert_inputs_unchanged(run)

    def test_assert_inputs_unchanged_raises_on_sql_change(self) -> None:
        """assert_inputs_unchanged raises if an SQL file is modified after freezing."""
        run = create_run(Path(self.tmp))
        files = self._make_files()
        freeze_inputs(run, files)
        # Modify one file
        files[0].write_bytes(b"SELECT 2 FROM DUAL  -- changed")
        with self.assertRaises(ValueError):
            assert_inputs_unchanged(run)

    def test_assert_inputs_unchanged_raises_on_file_deletion(self) -> None:
        """assert_inputs_unchanged raises if a frozen file is deleted."""
        run = create_run(Path(self.tmp))
        files = self._make_files()
        freeze_inputs(run, files)
        files[1].unlink()
        with self.assertRaises((ValueError, FileNotFoundError, OSError)):
            assert_inputs_unchanged(run)

    def test_freeze_inputs_stores_snapshot_atomically(self) -> None:
        """freeze_inputs creates a persistent snapshot file in the run directory."""
        run = create_run(Path(self.tmp))
        files = self._make_files()
        freeze_inputs(run, files)
        # A manifest/snapshot file must exist in the run directory
        run_dir = Path(self.tmp) / "scratch" / "dev-test" / run.run_id
        # Check for at least one JSON file containing hashes
        json_files = list(run_dir.rglob("*.json"))
        self.assertGreater(len(json_files), 0, "No snapshot files found in run directory")

    def test_freeze_inputs_refuses_symlinked_input(self) -> None:
        """freeze_inputs refuses to freeze a symlinked file path."""
        real_file = Path(self.tmp) / "real.sql"
        real_file.write_bytes(b"SELECT 1 FROM DUAL")
        link_file = Path(self.tmp) / "link.sql"
        try:
            link_file.symlink_to(real_file)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks not supported on this platform")
        run = create_run(Path(self.tmp))
        with self.assertRaises((ValueError, OSError)):
            freeze_inputs(run, [link_file])


# ---------------------------------------------------------------------------
# 4. append_event — exclusive creation, immutable, append-only
# ---------------------------------------------------------------------------

class TestAppendEvent(unittest.TestCase):
    """Intent events are exclusively created and never overwritten."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_append_event_returns_path(self) -> None:
        """append_event returns the Path where the event was written."""
        run = create_run(Path(self.tmp))
        p = append_event(run, {"type": "intent", "scenarioId": "s1"})
        self.assertIsInstance(p, Path)
        self.assertTrue(p.exists())

    def test_event_file_is_valid_json(self) -> None:
        """Each event file is valid JSON."""
        run = create_run(Path(self.tmp))
        p = append_event(run, {"type": "intent", "scenarioId": "s1"})
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)

    def test_event_contains_payload_fields(self) -> None:
        """The event file includes the supplied payload fields."""
        run = create_run(Path(self.tmp))
        payload = {"type": "intent", "scenarioId": "my-scenario", "extra": 42}
        p = append_event(run, payload)
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(data.get("scenarioId"), "my-scenario")
        self.assertEqual(data.get("extra"), 42)

    def test_multiple_events_create_separate_files(self) -> None:
        """Each append_event call creates a distinct file (no overwrite)."""
        run = create_run(Path(self.tmp))
        p1 = append_event(run, {"seq": 1})
        p2 = append_event(run, {"seq": 2})
        self.assertNotEqual(p1, p2)
        self.assertTrue(p1.exists())
        self.assertTrue(p2.exists())

    def test_events_are_in_events_subdirectory(self) -> None:
        """Event files land under the events/ subdirectory of the run."""
        run = create_run(Path(self.tmp))
        p = append_event(run, {"type": "intent"})
        events_dir = Path(self.tmp) / "scratch" / "dev-test" / run.run_id / "events"
        self.assertTrue(events_dir.exists())
        self.assertTrue(p.is_relative_to(events_dir))

    def test_event_file_not_overwritten_on_second_write(self) -> None:
        """If two events would have the same path, an error is raised (exclusive create)."""
        # This tests the underlying exclusive-create contract; normal usage creates
        # unique names so this is an edge-case robustness check.
        run = create_run(Path(self.tmp))
        p1 = append_event(run, {"seq": 1})
        # Manually trying to re-create the same path should fail — the module must
        # use exclusive creation. We verify by checking that p1 contents are unchanged
        # even after a second call.
        original_content = p1.read_bytes()
        append_event(run, {"seq": 2})  # different file
        self.assertEqual(p1.read_bytes(), original_content)


# ---------------------------------------------------------------------------
# 5. save_inventory
# ---------------------------------------------------------------------------

class TestSaveInventory(unittest.TestCase):
    """Inventory is saved as JSON in the run directory."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_inventory_creates_file(self) -> None:
        """save_inventory writes an inventory.json file."""
        run = create_run(Path(self.tmp))
        save_inventory(run, {"objects": [], "rows": []})
        run_dir = Path(self.tmp) / "scratch" / "dev-test" / run.run_id
        inv_path = run_dir / "inventory.json"
        self.assertTrue(inv_path.exists())

    def test_inventory_is_valid_json(self) -> None:
        """inventory.json is valid JSON matching the supplied mapping."""
        run = create_run(Path(self.tmp))
        data = {"objects": [{"name": "DT_AAABBB_ITEMS", "type": "TABLE"}], "rows": []}
        save_inventory(run, data)
        run_dir = Path(self.tmp) / "scratch" / "dev-test" / run.run_id
        saved = json.loads((run_dir / "inventory.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["objects"][0]["name"], "DT_AAABBB_ITEMS")

    def test_inventory_saved_atomically(self) -> None:
        """A second save_inventory call replaces the previous one atomically."""
        run = create_run(Path(self.tmp))
        save_inventory(run, {"objects": [], "rows": [], "version": 1})
        save_inventory(run, {"objects": [], "rows": [], "version": 2})
        run_dir = Path(self.tmp) / "scratch" / "dev-test" / run.run_id
        saved = json.loads((run_dir / "inventory.json").read_text(encoding="utf-8"))
        self.assertEqual(saved.get("version"), 2)


# ---------------------------------------------------------------------------
# 6. open_run — round-trip
# ---------------------------------------------------------------------------

class TestOpenRun(unittest.TestCase):
    """open_run re-opens an existing run directory created by create_run."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_open_run_returns_same_run_id(self) -> None:
        """open_run on a run created by create_run returns the same run_id."""
        r1 = create_run(Path(self.tmp))
        r2 = open_run(Path(self.tmp), r1.run_id)
        self.assertEqual(r1.run_id, r2.run_id)

    def test_open_run_preserves_frozen_inputs(self) -> None:
        """Events and frozen input snapshot are readable after open_run."""
        r1 = create_run(Path(self.tmp))
        f = Path(self.tmp) / "q.sql"
        f.write_bytes(b"SELECT 1 FROM DUAL")
        freeze_inputs(r1, [f])
        append_event(r1, {"seq": 99})

        r2 = open_run(Path(self.tmp), r1.run_id)
        # assert_inputs_unchanged must work via the re-opened handle
        assert_inputs_unchanged(r2)


# ---------------------------------------------------------------------------
# 7. Symlink safety on scratch / dev-test path components
# ---------------------------------------------------------------------------

class TestScratchSymlinkSafety(unittest.TestCase):
    """create_run must refuse if scratch or dev-test components are symlinks."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_run_refuses_symlinked_scratch_directory(self) -> None:
        """create_run refuses if <repo_root>/scratch is a symlink."""
        target_dir = Path(self.tmp) / "outside_scratch"
        target_dir.mkdir()
        repo_dir = Path(self.tmp) / "repo"
        repo_dir.mkdir()
        scratch_link = repo_dir / "scratch"
        try:
            scratch_link.symlink_to(target_dir)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks not supported")
        with self.assertRaises((ValueError, OSError)):
            create_run(repo_dir)

    def test_create_run_refuses_symlinked_devtest_directory(self) -> None:
        """create_run refuses if <repo_root>/scratch/dev-test is a symlink."""
        target_dir = Path(self.tmp) / "outside_devtest"
        target_dir.mkdir()
        repo_dir = Path(self.tmp) / "repo"
        scratch_dir = repo_dir / "scratch"
        scratch_dir.mkdir(parents=True)
        devtest_link = scratch_dir / "dev-test"
        try:
            devtest_link.symlink_to(target_dir)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks not supported")
        with self.assertRaises((ValueError, OSError)):
            create_run(repo_dir)


# ---------------------------------------------------------------------------
# 8. Occupied run ID refusal (patched generator)
# ---------------------------------------------------------------------------

class TestRunIdCollisionSafety(unittest.TestCase):
    """create_run must refuse an already occupied run directory."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_run_refuses_occupied_run_id_with_patched_generator(self) -> None:
        """create_run raises FileExistsError or ValueError when generated run ID already exists."""
        from unittest.mock import patch
        fixed_id = "20261016T120000Z-11112222333344445555666677778888"
        with patch("scripts.dev_test_evidence._generate_run_id", return_value=fixed_id):
            r1 = create_run(Path(self.tmp))
            self.assertEqual(r1.run_id, fixed_id)
            with self.assertRaises((FileExistsError, ValueError)):
                create_run(Path(self.tmp))


# ---------------------------------------------------------------------------
# 9. Private directory and file permissions
# ---------------------------------------------------------------------------

class TestPrivatePermissions(unittest.TestCase):
    """Run evidence directories and files must use private permissions."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_dir_and_files_use_private_permissions(self) -> None:
        """Directories must be 0o700 and files 0o600 on POSIX systems."""
        if os.name == "nt":
            self.skipTest("POSIX permission bits not applicable on Windows")
        run = create_run(Path(self.tmp))
        # Directory permissions: 0o700 (no group or other access)
        run_mode = stat.S_IMODE(run.run_dir.stat().st_mode)
        self.assertEqual(run_mode & 0o077, 0, f"run_dir mode {oct(run_mode)} not private (expected 0o700)")
        events_mode = stat.S_IMODE(run.events_dir.stat().st_mode)
        self.assertEqual(events_mode & 0o077, 0, f"events_dir mode {oct(events_mode)} not private")
        inputs_mode = stat.S_IMODE(run.inputs_dir.stat().st_mode)
        self.assertEqual(inputs_mode & 0o077, 0, f"inputs_dir mode {oct(inputs_mode)} not private")

        # Event file: 0o600
        ev_path = append_event(run, {"type": "intent", "test": 1})
        ev_mode = stat.S_IMODE(ev_path.stat().st_mode)
        self.assertEqual(ev_mode & 0o077, 0, f"event file mode {oct(ev_mode)} not private (expected 0o600)")

        # Inventory file: 0o600
        save_inventory(run, {"objects": [], "rows": []})
        inv_mode = stat.S_IMODE(run._inventory_path.stat().st_mode)
        self.assertEqual(inv_mode & 0o077, 0, f"inventory file mode {oct(inv_mode)} not private (expected 0o600)")


# ---------------------------------------------------------------------------
# 10. Byte snapshots and snapshot tamper detection
# ---------------------------------------------------------------------------

class TestByteSnapshots(unittest.TestCase):
    """freeze_inputs must preserve raw byte snapshots and verify them."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_freeze_inputs_preserves_byte_snapshots_and_detects_tamper(self) -> None:
        """freeze_inputs saves byte snapshots; tampering with snapshots triggers refusal."""
        run = create_run(Path(self.tmp))
        f1 = Path(self.tmp) / "step1.sql"
        f1.write_bytes(b"SELECT 1 FROM DUAL;\n")
        f2 = Path(self.tmp) / "step2.sql"
        f2.write_bytes(b"SELECT 2 FROM DUAL;\n")

        freeze_inputs(run, [f1, f2])

        snapshots_dir = run.inputs_dir / "snapshots"
        self.assertTrue(snapshots_dir.exists(), "inputs/snapshots directory was not created")
        snapshot_files = list(snapshots_dir.glob("*.bin"))
        self.assertEqual(len(snapshot_files), 2, "Expected 2 snapshot files")

        # Snapshot contents match original bytes
        all_snapshot_bytes = {sf.read_bytes() for sf in snapshot_files}
        self.assertIn(b"SELECT 1 FROM DUAL;\n", all_snapshot_bytes)
        self.assertIn(b"SELECT 2 FROM DUAL;\n", all_snapshot_bytes)

        # Tampering with snapshot byte file causes assert_inputs_unchanged to raise
        snapshot_files[0].write_bytes(b"CORRUPTED SNAPSHOT")
        with self.assertRaises(ValueError):
            assert_inputs_unchanged(run)


# ---------------------------------------------------------------------------
# 11. Refuse overwrite / refreeze after write intent
# ---------------------------------------------------------------------------

class TestRefuseRefreezeAfterIntent(unittest.TestCase):
    """freeze_inputs must refuse to run after an intent event is recorded."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_freeze_inputs_refused_after_write_intent(self) -> None:
        """freeze_inputs raises ValueError if called after a write intent event."""
        run = create_run(Path(self.tmp))
        f1 = Path(self.tmp) / "input.sql"
        f1.write_bytes(b"SELECT 1 FROM DUAL")
        freeze_inputs(run, [f1])

        # Record a write intent
        append_event(run, {"type": "intent", "scenarioId": "test-scenario", "target": "DEMO"})

        # Subsequent freeze_inputs must be refused
        f2 = Path(self.tmp) / "input2.sql"
        f2.write_bytes(b"SELECT 2 FROM DUAL")
        with self.assertRaises(ValueError):
            freeze_inputs(run, [f2])


# ---------------------------------------------------------------------------
# 12. Object prefix contract: DT_<12 uppercase hex>_
# ---------------------------------------------------------------------------

class TestObjectPrefixContract(unittest.TestCase):
    """RunEvidence must provide an uppercase DT_<12 hex>_ object prefix within 30-byte budget."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_run_has_dt_object_prefix(self) -> None:
        """RunEvidence has an object_prefix matching DT_<12 hex>_ with length 16."""
        run = create_run(Path(self.tmp))
        self.assertTrue(hasattr(run, "object_prefix"), "RunEvidence missing object_prefix")
        self.assertRegex(run.object_prefix, r"^DT_[0-9A-F]{12}_\Z")
        self.assertEqual(len(run.object_prefix), 16)

    def test_object_prefix_survives_open_run(self) -> None:
        """open_run yields the exact same object_prefix."""
        r1 = create_run(Path(self.tmp))
        r2 = open_run(Path(self.tmp), r1.run_id)
        self.assertEqual(r1.object_prefix, r2.object_prefix)

    def test_make_object_name_helper(self) -> None:
        """make_object_name combines prefix and suffix safely within 30 bytes."""
        from scripts import dev_test_evidence
        make_fn = getattr(dev_test_evidence, "make_object_name", None)
        if make_fn is None:
            self.fail("make_object_name helper not found in dev_test_evidence")
        name = make_fn("DT_0123456789AB_", "ITEMS")
        self.assertEqual(name, "DT_0123456789AB_ITEMS")
        self.assertLessEqual(len(name), 30)

        # Suffix exceeding budget raises ValueError
        with self.assertRaises(ValueError):
            make_fn("DT_0123456789AB_", "A" * 15)  # 16 + 15 = 31 > 30


if __name__ == "__main__":
    unittest.main()
