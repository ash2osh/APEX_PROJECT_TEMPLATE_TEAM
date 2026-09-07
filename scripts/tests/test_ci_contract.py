from __future__ import annotations

from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from teamlib import ci
from teamlib.ci import CIError, ci_doctor, ci_replay


class CIContractTests(unittest.TestCase):
    def _contract(self, root: Path, image: str = "oracle/free@sha256:" + "a" * 64):
        provisioner = root / "provisioner.sh"
        provisioner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        provisioner.chmod(0o755)
        return {
            "version": 1,
            "toolchain": {"sqlcl": "26.2.1", "jdk": "17", "apex": "26.1", "database": "23ai", "python": "3.10+"},
            "profiles": ["TABLES", "CODE", "APEX", "METADATA", "VERIFY"],
            "provisioner": {"path": str(provisioner), "image": image},
            "production": {"credentials": False},
        }

    def test_doctor_rejects_unpinned_or_missing_runner(self):
        with tempfile.TemporaryDirectory(prefix="team-ci-") as directory:
            root = Path(directory)
            report = ci_doctor(self._contract(root, "oracle/free:latest"))
            self.assertFalse(report.valid)
            self.assertTrue(any("digest" in issue for issue in report.issues))
            missing = self._contract(root)
            missing["provisioner"]["path"] = str(root / "gone")
            self.assertFalse(ci_doctor(missing).valid)

    def test_replay_requires_verified_disposable_create_and_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="team-ci-") as directory:
            root = Path(directory)
            calls = []

            def provisioner(argv, out):
                calls.append(tuple(argv))
                if argv[0] == "create":
                    return {"version": 1, "instance_token": "run-1", "status": "disposable", "env_path": str(out / "replay.env"), "source_commit": "abc"}
                return {"version": 1, "destroyed": True}

            def runner(ref, previous, target):
                self.assertEqual(ref, "abc")
                self.assertIsNone(previous)
                self.assertEqual(target["instance_token"], "run-1")
                return {"status": "PASS", "source_commit": ref, "fresh": "PASS", "upgrade": "NOT_APPLICABLE_INITIAL_RELEASE"}

            report = ci_replay("abc", None, contract=self._contract(root), provisioner=provisioner, runner=runner)
            self.assertEqual(report.status, "PASS")
            self.assertEqual(calls[0][0], "create")
            self.assertEqual(calls[-1][0], "destroy")

    def test_replay_retains_sanitized_runner_evidence(self):
        with tempfile.TemporaryDirectory(prefix="team-ci-evidence-keep-") as directory:
            root = Path(directory)

            def provisioner(argv, out):
                if argv[0] == "create":
                    return {
                        "version": 1, "instance_token": "run-1", "status": "disposable",
                        "env_path": str(out / "replay.env"), "instance_id": "FREE@host",
                    }
                return {"version": 1, "destroyed": True}

            report = ci_replay(
                "abc", None, contract=self._contract(root), provisioner=provisioner,
                runner=lambda ref, _previous, _target: {
                    "status": "PASS", "source_commit": ref, "fresh": "PASS",
                    "upgrade": "NOT_APPLICABLE_INITIAL_RELEASE", "application_checks": {"status": "PASS"},
                    "password": "must-not-be-retained",
                },
            )
            self.assertEqual(report.evidence["runner"]["application_checks"]["status"], "PASS")
            self.assertNotIn("password", report.evidence["runner"])

    def test_failed_runner_is_not_reported_as_pass(self):
        with tempfile.TemporaryDirectory(prefix="team-ci-") as directory:
            root = Path(directory)

            def provisioner(argv, out):
                if argv[0] == "create":
                    return {"version": 1, "instance_token": "run-1", "status": "disposable", "env_path": str(out / "replay.env")}
                return {"version": 1, "destroyed": True}

            with self.assertRaises(CIError):
                ci_replay("abc", None, contract=self._contract(root), provisioner=provisioner, runner=lambda *_: {"status": "FAIL"})

    def test_invalid_create_result_still_cleans_exact_token(self):
        with tempfile.TemporaryDirectory(prefix="team-ci-invalid-create-") as directory:
            root = Path(directory)
            calls = []

            def provisioner(argv, out):
                calls.append(tuple(argv))
                if argv[0] == "create":
                    return {"version": 1, "instance_token": "run-1", "status": "shared", "env_path": str(out / "replay.env")}
                return {"version": 1, "destroyed": True}

            with self.assertRaisesRegex(CIError, "disposable"):
                ci_replay("abc", None, contract=self._contract(root), provisioner=provisioner, runner=lambda *_: {})
            self.assertEqual(calls[-1][0], "destroy")
            self.assertEqual(calls[-1][-1], "run-1")

    def test_replay_requires_explicit_fresh_and_upgrade_results(self):
        with tempfile.TemporaryDirectory(prefix="team-ci-evidence-") as directory:
            root = Path(directory)

            def provisioner(argv, out):
                if argv[0] == "create":
                    return {"version": 1, "instance_token": "run-1", "status": "disposable", "env_path": str(out / "replay.env")}
                return {"version": 1, "destroyed": True}

            with self.assertRaisesRegex(CIError, "verified PASS"):
                ci_replay("abc", None, contract=self._contract(root), provisioner=provisioner, runner=lambda *_: {"status": "PASS", "source_commit": "abc"})

    def test_cli_resolves_runner_and_provisioner_relative_to_contract(self):
        with tempfile.TemporaryDirectory(prefix="team-ci-cli-") as directory:
            root = Path(directory)
            contract = self._contract(root)
            provisioner = root / "provisioner.sh"
            provisioner.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = create ]; then mkdir -p \"$5\"; : > \"$5/replay.env\"; "
                "printf '{\"version\":1,\"status\":\"disposable\",\"instance_token\":\"%s\",\"env_path\":\"%s/replay.env\"}\\n' \"$3\" \"$5\"; "
                "else printf '{\"version\":1,\"destroyed\":true}\\n'; fi\n",
                encoding="utf-8",
            )
            provisioner.chmod(0o755)
            runner = root / "runner.sh"
            runner.write_text("#!/bin/sh\nprintf '{\"status\":\"PASS\",\"source_commit\":\"%s\",\"fresh\":\"PASS\",\"upgrade\":\"NOT_APPLICABLE_INITIAL_RELEASE\"}\\n' \"$2\"\n", encoding="utf-8")
            runner.chmod(0o755)
            contract["provisioner"] = {"path": "provisioner.sh", "image": "oracle/free@sha256:" + "a" * 64}
            contract["runner"] = "runner.sh"
            contract_path = root / "contract.json"
            contract_path.write_text(json.dumps(contract), encoding="utf-8")
            other = root / "other"
            other.mkdir()
            with patch.object(os, "getcwd", return_value=str(other)):
                result = ci.main([
                    "ci-replay", "--ref", "abc", "--contract", str(contract_path),
                    "--provisioner", "provisioner.sh", "--runner", "runner.sh",
                    "--scratch-root", str(root / "scratch"),
                ])
            self.assertEqual(result, 0)

    def test_replay_passes_absolute_scratch_path_to_provisioner(self):
        with tempfile.TemporaryDirectory(prefix="team-ci-absolute-", dir="/tmp") as directory:
            root = Path(directory)
            observed = []

            def provisioner(argv, out):
                if argv[0] == "create":
                    observed.append(Path(argv[argv.index("--out") + 1]))
                    return {"version": 1, "instance_token": "run-1", "status": "disposable", "env_path": str(out / "replay.env")}
                return {"version": 1, "destroyed": True}

            ci_replay(
                "abc",
                None,
                contract=self._contract(root),
                provisioner=provisioner,
                runner=lambda ref, _previous, _target: {
                    "status": "PASS",
                    "source_commit": ref,
                    "fresh": "PASS",
                    "upgrade": "NOT_APPLICABLE_INITIAL_RELEASE",
                },
                scratch_root=Path("scratch") / "relative-ci",
            )
            self.assertEqual(len(observed), 1)
            self.assertTrue(observed[0].is_absolute())


if __name__ == "__main__":
    unittest.main()
