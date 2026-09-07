from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from teamlib.release import build_release
from teamlib.runbook import RunbookError, gen_runbook


class RunbookTests(unittest.TestCase):
    def test_signed_pass_evidence_is_required_for_production_handoff(self):
        with tempfile.TemporaryDirectory(prefix="team-runbook-") as directory:
            root = Path(directory)
            repo = root / "repo"
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "x@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "x"], check=True)
            (repo / "apps" / "a" / ".apex").mkdir(parents=True)
            (repo / "apps" / "a" / "application.apx").write_bytes(b"app")
            (repo / "apps" / "a" / ".apex" / "apexlang.json").write_bytes(b"{}")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
            commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            manifest = build_release(repo, commit, "1.0.0", root / "out")
            evidence_data = {
                "version": 1, "archive_digest": manifest.archive_digest, "source_commit": commit,
                "qualification_sha": commit, "toolchain_digest": "b" * 64,
                "target_identity": {"instance_id": "TEST", "workspace_id": 1},
                "run_identity": "ci-run-1", "replay_identity": {"instance_token": "ci-1"},
                "application_checks": {"status": "PASS", "checks_digest": "c" * 64, "coverage": {"apps": []}, "unknown": 0},
                "final_status": "PASS",
                "results": {"replay": "PASS", "apex": "PASS", "subscription": "PASS", "application_checks": "PASS"},
            }
            evidence = root / "evidence.json"
            evidence.write_text(json.dumps(evidence_data, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            key = Ed25519PrivateKey.generate()
            private_bytes = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
            public_bytes = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            (root / "key.pem").write_bytes(public_bytes)
            (root / "sig").write_bytes(key.sign(evidence.read_bytes()))
            runbook = gen_runbook(manifest.archive_path, {}, {"environment": "production", "instance_id": "PROD", "workspace_id": 1, "app_ids": {"a": 2}}, evidence, root / "sig", root / "key.pem")
            self.assertIn("PROD", runbook.text)
            self.assertIn(manifest.archive_digest, runbook.text)

    def test_failed_evidence_refuses(self):
        with tempfile.TemporaryDirectory(prefix="team-runbook-fail-") as directory:
            path = Path(directory)
            with self.assertRaises(RunbookError):
                gen_runbook(path / "missing.tar", {}, {"environment": "production"}, path / "evidence", path / "sig", path / "key")


if __name__ == "__main__":
    unittest.main()
