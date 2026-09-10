from __future__ import annotations

import json
import sys
from pathlib import Path
import tempfile
import unittest

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from teamlib.ci import ci_doctor, main


class CIContractTests(unittest.TestCase):
    def _contract(self) -> dict[str, object]:
        return {
            "version": 1,
            "toolchain": {
                "python": "3.10+",
                "sqlcl": "26.2.1+",
                "jdk": "17+",
                "apex": "26.1+",
                "database": "23ai+",
                "cryptography": "Ed25519-qualified",
            },
            "profiles": ["TABLES", "CODE", "APEX", "METADATA", "VERIFY"],
            "production": {"credentials": False, "writes": False},
        }

    def test_doctor_accepts_closed_non_secret_contract(self):
        report = ci_doctor(self._contract())
        self.assertTrue(report.valid, report.issues)
        self.assertEqual(report.issues, ())
        self.assertNotIn("provisioner", report.capabilities)
        self.assertNotIn("runner", report.capabilities)
        self.assertEqual(report.capabilities["production_credentials"], False)
        self.assertEqual(report.capabilities["production_writes"], False)

    def test_doctor_rejects_missing_toolchain_and_profile(self):
        contract = self._contract()
        del contract["toolchain"]["sqlcl"]  # type: ignore[index]
        contract["profiles"] = ["TABLES"]
        report = ci_doctor(contract)
        self.assertFalse(report.valid)
        self.assertTrue(any("sqlcl" in issue for issue in report.issues))
        self.assertTrue(any("profiles" in issue or "TABLES" in issue for issue in report.issues))

    def test_doctor_rejects_production_credentials_or_writes(self):
        for key in ("credentials", "writes"):
            contract = self._contract()
            contract["production"][key] = True  # type: ignore[index]
            report = ci_doctor(contract)
            self.assertFalse(report.valid)
            self.assertTrue(any(key in issue for issue in report.issues))

    def test_doctor_rejects_populated_secret_like_fields(self):
        contract = self._contract()
        contract["signing_private_key"] = "not-a-key"
        report = ci_doctor(contract)
        self.assertFalse(report.valid)
        self.assertTrue(any("secret-like" in issue for issue in report.issues))

    def test_doctor_rejects_malformed_contract_file(self):
        with tempfile.TemporaryDirectory(prefix="team-ci-") as directory:
            path = Path(directory) / "contract.json"
            path.write_text("[]", encoding="utf-8")
            report = ci_doctor(path)
            self.assertFalse(report.valid)
            self.assertIn("object", report.issues[0])

    def test_cli_exposes_only_ci_doctor(self):
        with tempfile.TemporaryDirectory(prefix="team-ci-cli-") as directory:
            path = Path(directory) / "contract.json"
            path.write_text(json.dumps(self._contract()), encoding="utf-8")
            self.assertEqual(main(["ci-doctor", "--contract", str(path)]), 0)


if __name__ == "__main__":
    unittest.main()
