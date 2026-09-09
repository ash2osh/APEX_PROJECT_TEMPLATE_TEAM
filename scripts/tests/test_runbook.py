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
from unittest.mock import patch

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


class DependencyDiagnosticTests(unittest.TestCase):
    def test_missing_cryptography_is_reported_as_a_missing_dependency(self):
        import builtins
        from teamlib.runbook import _public_key, RunbookDependencyError

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ModuleNotFoundError("No module named 'cryptography'")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", refuse):
            with self.assertRaises(RunbookDependencyError) as caught:
                _public_key(b"-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEA\n-----END PUBLIC KEY-----\n")
        message = str(caught.exception)
        self.assertIn("cryptography", message)
        self.assertNotIn("readable PEM", message)

    def test_a_genuinely_malformed_key_still_reports_a_key_problem(self):
        from teamlib.runbook import _public_key, RunbookError
        with self.assertRaisesRegex(RunbookError, "readable PEM"):
            _public_key(b"not a key at all")


class DependencyManifestTests(unittest.TestCase):
    def test_every_third_party_dependency_is_declared(self):
        root = Path(__file__).resolve().parents[2]
        manifest = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn("[project]", manifest)
        self.assertIn("cryptography", manifest)
        self.assertIn("graphify", manifest)
        self.assertIn("tree-sitter-sql", manifest)
        self.assertIn('requires-python = ">=3.10"', manifest)


if __name__ == "__main__":
    unittest.main()
