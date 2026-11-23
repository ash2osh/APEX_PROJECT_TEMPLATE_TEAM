from __future__ import annotations

import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path

from scripts import release_proof_generation as generations


class ReleaseProofGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = self.root / "shared" / "release-proof-generations.json"
        self.registry.parent.mkdir(mode=0o770)
        self.identity = {
            "db_unique_name": "STAGE_A", "container_id": "3", "container_name": "APP_PDB",
        }
        self.scope = {"dbUniqueName": "STAGE_A", "containerId": "3", "schema": "APP_CODE", "edition": "ORA$BASE"}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_initialize_is_one_time_and_capture_requires_every_target(self) -> None:
        first = generations.initialize_baseline(self.registry, self.identity)
        captured = generations.capture_generations(self.registry, [self.scope])
        self.assertEqual(captured, [{
            "dbUniqueName": "STAGE_A", "containerId": "3", "generationId": first["generationId"],
        }])
        self.assertEqual(generations.assert_current_generations(self.registry, captured, [self.scope]), captured)
        with self.assertRaisesRegex(generations.ReleaseProofGenerationError, "never overwrites"):
            generations.initialize_baseline(self.registry, self.identity)
        other_scope = {**self.scope, "dbUniqueName": "STAGE_B"}
        with self.assertRaisesRegex(generations.ReleaseProofGenerationError, "no trusted restore generation"):
            generations.capture_generations(self.registry, [self.scope, other_scope])

    def test_capture_binds_each_database_and_container_in_a_multi_target_run(self) -> None:
        generations.initialize_baseline(self.registry, self.identity)
        second_identity = {"db_unique_name": "STAGE_B", "container_id": "9"}
        generations.initialize_baseline(self.registry, second_identity)
        scopes = [
            self.scope,
            {"dbUniqueName": "STAGE_B", "containerId": "9", "schema": "AUX_CODE", "edition": "ORA$BASE"},
            {"dbUniqueName": "stage_a", "containerId": "3", "schema": "APP_PARSING", "edition": "ORA$BASE"},
        ]
        captured = generations.capture_generations(self.registry, scopes)
        self.assertEqual([(row["dbUniqueName"], row["containerId"]) for row in captured], [
            ("STAGE_A", "3"), ("STAGE_B", "9"),
        ])
        generations.begin_restore(self.registry, second_identity, str(uuid.uuid4()))
        with self.assertRaisesRegex(generations.ReleaseProofGenerationError, "marked restoring"):
            generations.assert_current_generations(self.registry, captured, scopes)

    def test_restore_invalidates_old_generation_until_completion(self) -> None:
        old = generations.initialize_baseline(self.registry, self.identity)
        captured = generations.capture_generations(self.registry, [self.scope])
        operation_id = str(uuid.uuid4())

        pending = generations.begin_restore(self.registry, self.identity, operation_id)
        self.assertEqual(pending["status"], "restoring")
        self.assertIsNone(pending["generationId"])
        with self.assertRaisesRegex(generations.ReleaseProofGenerationError, "marked restoring"):
            generations.capture_generations(self.registry, [self.scope])
        with self.assertRaisesRegex(generations.ReleaseProofGenerationError, "marked restoring"):
            generations.assert_current_generations(self.registry, captured, [self.scope])

        new = generations.complete_restore(self.registry, self.identity, operation_id)
        self.assertEqual(new["status"], "ready")
        self.assertNotEqual(new["generationId"], old["generationId"])
        new_rows = generations.capture_generations(self.registry, [self.scope])
        self.assertEqual(generations.assert_current_generations(self.registry, new_rows, [self.scope]), new_rows)
        with self.assertRaisesRegex(generations.ReleaseProofGenerationError, "changed after proof capture"):
            generations.assert_current_generations(self.registry, captured, [self.scope])

    def test_restore_calls_are_idempotent_for_the_same_operation(self) -> None:
        operation_id = str(uuid.uuid4())
        first = generations.begin_restore(self.registry, self.identity, operation_id)
        before = self.registry.read_bytes()
        before_mtime = self.registry.stat().st_mtime_ns
        repeated = generations.begin_restore(self.registry, self.identity, operation_id)
        self.assertEqual(repeated, first)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.registry.stat().st_mtime_ns, before_mtime)

        completed = generations.complete_restore(self.registry, self.identity, operation_id)
        before = self.registry.read_bytes()
        before_mtime = self.registry.stat().st_mtime_ns
        repeated_complete = generations.complete_restore(self.registry, self.identity, operation_id)
        self.assertEqual(repeated_complete, completed)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.registry.stat().st_mtime_ns, before_mtime)

    def test_different_restore_cannot_replace_an_in_progress_operation(self) -> None:
        first_operation = str(uuid.uuid4())
        second_operation = str(uuid.uuid4())
        generations.begin_restore(self.registry, self.identity, first_operation)
        before = self.registry.read_bytes()
        with self.assertRaisesRegex(generations.ReleaseProofGenerationError, "another restore is already in progress"):
            generations.begin_restore(self.registry, self.identity, second_operation)
        self.assertEqual(self.registry.read_bytes(), before)
        with self.assertRaisesRegex(generations.ReleaseProofGenerationError, "no matching pending restore"):
            generations.complete_restore(self.registry, self.identity, second_operation)

    def test_generation_rows_reject_wrong_target_and_duplicates(self) -> None:
        generations.initialize_baseline(self.registry, self.identity)
        captured = generations.capture_generations(self.registry, [self.scope])
        wrong = [{**captured[0], "dbUniqueName": "STAGE_B"}]
        with self.assertRaisesRegex(generations.ReleaseProofGenerationError, "no trusted restore generation"):
            generations.assert_current_generations(self.registry, wrong)
        with self.assertRaisesRegex(generations.ReleaseProofGenerationError, "uniquely sorted"):
            generations.assert_current_generations(self.registry, captured + captured)

    @unittest.skipIf(os.name == "nt", "POSIX shared-file mode assertions do not apply on Windows")
    def test_atomic_update_preserves_preprovisioned_file_mode_and_group(self) -> None:
        generations.initialize_baseline(self.registry, self.identity)
        self.registry.chmod(0o640)
        lock = self.registry.with_name(self.registry.name + ".lock")
        lock.chmod(0o640)
        before = self.registry.stat()
        generations.begin_restore(self.registry, self.identity, str(uuid.uuid4()))
        after = self.registry.stat()
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o640)
        self.assertEqual(after.st_gid, before.st_gid)
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o640)

    def test_registry_path_requires_an_explicit_absolute_location(self) -> None:
        with self.assertRaisesRegex(generations.ReleaseProofGenerationError, "must be an absolute path"):
            generations.registry_path("relative/proof.json")
        with self.assertRaisesRegex(generations.ReleaseProofGenerationError, "must name"):
            generations.registry_path(None, environ={})


if __name__ == "__main__":
    unittest.main()
