from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.release_ownership import TargetWriterBusy, target_writer_lease, target_writer_lock_path


class ReleaseOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.scope = {
            "dbUniqueName": "STAGE_UNIQUE", "containerId": "3",
            "schema": "APP_CODE", "edition": "ORA$BASE",
        }
        self.identity = {
            "payloadDigest": "a" * 64, "engineDigest": "b" * 64,
            "planDigest": "c" * 64, "checkpoint": 1,
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _child(self, *, hold: bool) -> subprocess.Popen:
        source_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(source_root))
        code = f"""
import sys, time
from pathlib import Path
from scripts.release_ownership import TargetWriterBusy, target_writer_lease
root = Path(sys.argv[1])
scope = {{"dbUniqueName":"STAGE_UNIQUE","containerId":"3","schema":"APP_CODE","edition":"ORA$BASE"}}
identity = {{"payloadDigest":"{'a' * 64}","engineDigest":"{'b' * 64}","planDigest":"{'c' * 64}","checkpoint":1}}
try:
    with target_writer_lease(root, scope, identity):
        print("ACQUIRED", flush=True)
        if sys.argv[2] == "hold":
            time.sleep(60)
except TargetWriterBusy:
    print("BUSY", flush=True)
    raise SystemExit(3)
"""
        return subprocess.Popen(
            [sys.executable, "-c", code, str(self.root), "hold" if hold else "probe"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )

    def test_active_process_blocks_normal_and_recovery_lease_attempts(self) -> None:
        owner = self._child(hold=True)
        try:
            self.assertEqual(owner.stdout.readline().strip(), "ACQUIRED")
            for recovery in (False, True):
                with self.subTest(recovery=recovery):
                    with self.assertRaises(TargetWriterBusy):
                        with target_writer_lease(
                            self.root, self.scope,
                            {**self.identity, "recoveryAttempt": "12345678-1234-4234-8234-123456789abc" if recovery else None},
                        ):
                            self.fail("active writer lease was bypassed")
        finally:
            owner.terminate()
            owner.wait(timeout=10)
            if owner.stdout is not None:
                owner.stdout.close()
            if owner.stderr is not None:
                owner.stderr.close()

    def test_terminated_owner_releases_kernel_lease(self) -> None:
        owner = self._child(hold=True)
        try:
            self.assertEqual(owner.stdout.readline().strip(), "ACQUIRED")
            owner.terminate()
            owner.wait(timeout=10)
        finally:
            if owner.poll() is None:
                owner.terminate()
                owner.wait(timeout=10)
            if owner.stdout is not None:
                owner.stdout.close()
            if owner.stderr is not None:
                owner.stderr.close()
        with target_writer_lease(self.root, self.scope, self.identity) as lease:
            self.assertTrue(lease.token)

    def test_stale_pid_metadata_does_not_block_a_new_kernel_owner(self) -> None:
        lock_dir = self.root / "releases"
        lock_dir.mkdir()
        stale = target_writer_lock_path(self.root, self.scope)
        stale.write_text('{"pid":99999999,"token":"stale","targetScope":{}}\n', encoding="utf-8")
        with target_writer_lease(self.root, self.scope, self.identity):
            self.assertTrue(stale.exists())

    def test_different_schema_scopes_use_independent_leases(self) -> None:
        other = {**self.scope, "schema": "APP_DATA"}
        with target_writer_lease(self.root, self.scope, self.identity):
            with target_writer_lease(self.root, other, self.identity):
                pass

    def test_malformed_scope_refuses_before_creating_a_lock(self) -> None:
        with self.assertRaises(ValueError):
            with target_writer_lease(self.root, {"schema": "APP_CODE"}, self.identity):
                pass
        self.assertFalse((self.root / "releases").exists())


if __name__ == "__main__":
    unittest.main()
