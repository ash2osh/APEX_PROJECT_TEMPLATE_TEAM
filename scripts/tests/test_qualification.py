from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from teamlib.app_checks import AppCheckReport
from teamlib.config import Config, Profile, profile_target
from teamlib.qualification import QualificationError, _target_identity, qualify_target, sign_test_evidence, write_report


class FakeStore:
    def __init__(self, *, attempts=None, observations=None):
        self.attempts = attempts or {}
        self.observations = observations if observations is not None else [
            {"sequence": 3, "before": "b" * 64, "after": "c" * 64, "evidence": "d" * 64}
        ]

    def validate_observation_chain(self, target):
        return None

    def read_history(self, target):
        return {"m1": {"status": "APPLIED", "checksum": "a" * 64, "sequence": 2}}

    def read_state(self, target):
        return {"attempts": self.attempts, "observations": self.observations}


def config_for(role="integration", environment="staging"):
    profiles = {
        name: Profile(name, f"{name.lower()}-connection", "APP", "APP", "FREEPDB1", "service", "INSTANCE")
        for name in ("TABLES", "CODE", "APEX", "METADATA", "VERIFY")
    }
    return Config(
        values={},
        profiles=profiles,
        apps={"employee": 101},
        project="team",
        role=role,
        environment=environment,
        tables_schema="APP",
        code_schema="APP_CODE",
        apex_parsing_schema="APP",
        metadata_schema="APP_META",
        workspace_id=90001,
        ownership_mode="shared",
    )


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="team-qualification-")
        self.root = Path(self.temp.name)
        (self.root / "ci" / "app-checks").mkdir(parents=True)
        (self.root / "ci" / "app-checks" / "employee.json").write_text(
            json.dumps({"version": 1, "alias": "employee", "page_ids": [1], "checks": [
                {"id": "objects", "page_id": 1, "kind": "select", "verify_sql": "employee/objects.verify.sql",
                 "expected_objects": ["APP.T"], "sql": "SELECT 'objects' assertion_name, 'PASS' status FROM dual"},
            ]}),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_persistent_report_has_one_source_commit_and_frontier_identity(self):
        fake_app = AppCheckReport(
            "a" * 40,
            {"target_kind": "persistent", "instance_id": "INSTANCE"},
            (),
            "a" * 64,
            "b" * 64,
            {"apps": ["employee"], "checks": 1, "unknown": 0},
            "PASS",
        )
        with patch("teamlib.qualification.verify_candidate_apps", return_value=fake_app):
            report = qualify_target(
                self.root,
                config_for(),
                "a" * 40,
                ("employee",),
                store=FakeStore(),
                work=self.root / "work",
                runner_contract=Path("ci/runner-contract.json"),
                sql_runner=lambda *args, **kwargs: object(),
            )
        self.assertEqual(report["version"], 2)
        self.assertEqual(report["final_status"], "PASS")
        self.assertEqual(report["source_commit"], "a" * 40)
        self.assertNotIn("qualification_sha", report)
        self.assertNotIn("replay_identity", report)
        self.assertEqual(report["qualification_identity"]["observation_sequence"], 3)
        self.assertEqual(report["qualification_identity"]["observation_digest"], "c" * 64)
        self.assertEqual(report["application_checks"]["unknown"], 0)
        self.assertEqual(report["results"]["application_checks"], "PASS")

    def test_production_and_unresolved_attempts_refuse(self):
        with self.assertRaisesRegex(QualificationError, "non-production"):
            qualify_target(
                self.root, config_for("production", "production"), "a" * 40, ("employee",),
                store=FakeStore(), work=self.root / "work",
                runner_contract=Path("ci/runner-contract.json"),
                sql_runner=lambda *args, **kwargs: object(),
            )
        with self.assertRaisesRegex(QualificationError, "unresolved"):
            qualify_target(
                self.root, config_for(), "a" * 40, ("employee",),
                store=FakeStore(attempts={"attempt": {"state": "UNKNOWN"}}), work=self.root / "work2",
                runner_contract=Path("ci/runner-contract.json"),
                sql_runner=lambda *args, **kwargs: object(),
            )

    def test_target_identity_is_bound_to_the_metadata_owner(self):
        config = config_for(role="test", environment="test")
        targets = {
            name: profile_target(config, name, alias="employee") if name == "APEX"
            else profile_target(config, name)
            for name in ("TABLES", "CODE", "APEX", "METADATA", "VERIFY")
        }
        identity = _target_identity(config, targets)
        self.assertEqual(identity["state_key"], targets["METADATA"].state_key)
        self.assertEqual(identity["binding_digest"], targets["METADATA"].binding_digest)
        self.assertNotEqual(identity["state_key"], targets["TABLES"].state_key)
        self.assertNotEqual(identity["binding_digest"], targets["TABLES"].binding_digest)

    def test_write_report_is_canonical_and_signable(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        report = {
            "version": 2,
            "final_status": "PASS",
            "source_commit": "a" * 40,
            "archive_digest": "a" * 64,
            "toolchain_digest": "b" * 64,
            "target_identity": {
                "project": "team",
                "role": "test",
                "environment": "test",
                "target_kind": "persistent",
                "instance_id": "INSTANCE",
                "db_name": "FREEPDB1",
                "service": "service",
                "workspace_id": 90001,
                "app_ids": {"employee": 101},
                "state_key": "c" * 64,
                "binding_digest": "d" * 64,
            },
            "run_identity": {"run_id": "1"},
            "qualification_identity": {
                "target_kind": "persistent",
                "observation_sequence": 1,
                "observation_digest": "c" * 64,
                "history_digest": "d" * 64,
            },
            "application_checks": {
                "status": "PASS", "checks_digest": "e" * 64,
                "coverage": {
                    "apps": ["employee"],
                    "pages": {"employee": [1]},
                    "checks": 1,
                    "unknown": 0,
                },
                "unknown": 0,
                "results": [{
                    "alias": "employee",
                    "check_id": "objects",
                    "page_id": 1,
                    "kind": "select",
                    "status": "PASS",
                    "expected_objects": ["APP.T"],
                    "diagnostic": "",
                    "observed": {"status": "PASS"},
                }],
            },
            "results": {
                "migrations": "PASS", "application_deploy": "PASS", "application_checks": "PASS",
            },
        }
        evidence = self.root / "evidence.json"
        write_report(report, evidence)
        key = Ed25519PrivateKey.generate()
        private = self.root / "key.pem"
        private.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        signature = self.root / "evidence.sig"
        digest = sign_test_evidence(evidence, private, signature)
        self.assertEqual(digest, hashlib.sha256(evidence.read_bytes()).hexdigest())
        key.public_key().verify(signature.read_bytes(), evidence.read_bytes())

    def test_signing_rejects_non_test_or_incomplete_target_identity(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        valid = {
            "version": 2,
            "final_status": "PASS",
            "source_commit": "a" * 40,
            "archive_digest": "a" * 64,
            "toolchain_digest": "b" * 64,
            "target_identity": {
                "project": "team", "role": "test", "environment": "test",
                "target_kind": "persistent", "instance_id": "INSTANCE",
                "db_name": "FREEPDB1", "service": "service", "workspace_id": 90001,
                "app_ids": {"employee": 101}, "state_key": "c" * 64,
                "binding_digest": "d" * 64,
            },
            "run_identity": {"run_id": "1"},
            "qualification_identity": {
                "target_kind": "persistent", "observation_sequence": 1,
                "observation_digest": "c" * 64, "history_digest": "d" * 64,
            },
            "application_checks": {
                "status": "PASS", "checks_digest": "e" * 64,
                "coverage": {"apps": ["employee"], "pages": {"employee": [1]}, "checks": 1, "unknown": 0},
                "unknown": 0,
                "results": [{
                    "alias": "employee", "check_id": "objects", "page_id": 1,
                    "kind": "select", "status": "PASS", "expected_objects": ["APP.T"],
                    "diagnostic": "", "observed": {"status": "PASS"},
                }],
            },
            "results": {"migrations": "PASS", "application_deploy": "PASS", "application_checks": "PASS"},
        }
        key = Ed25519PrivateKey.generate()
        private = self.root / "identity-key.pem"
        private.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        invalid_identities = (
            {**valid["target_identity"], "role": "integration"},
            {**valid["target_identity"], "environment": "staging"},
            {**valid["target_identity"], "target_kind": "disposable"},
            {key: value for key, value in valid["target_identity"].items() if key != "state_key"},
        )
        for index, identity in enumerate(invalid_identities):
            with self.subTest(index=index):
                document = dict(valid)
                document["target_identity"] = identity
                evidence = self.root / f"invalid-{index}.json"
                write_report(document, evidence)
                with self.assertRaises(QualificationError):
                    sign_test_evidence(evidence, private, self.root / f"invalid-{index}.sig")


if __name__ == "__main__":
    unittest.main()
