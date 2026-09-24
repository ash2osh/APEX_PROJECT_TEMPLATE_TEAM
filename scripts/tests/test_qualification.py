from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from teamlib.app_checks import AppCheckReport, CheckResult, build_app_check_bundle
from teamlib.config import Config, Profile, profile_target
from teamlib.evidence import (
    EvidenceError,
    canonical_json,
    validate_release_evidence_binding,
    validate_test_evidence,
)
from teamlib.qualification import QualificationError, _target_identity, qualify_target, sign_test_evidence, write_report
from teamlib.release import ApplyReport, ReleasePlan


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
        app_parsing_schemas={"employee": "APP"},
        project="team",
        role=role,
        environment=environment,
        tables_schema="APP",
        code_schema="APP_CODE",
        metadata_schema="APP_META",
        workspace_id=90001,
        ownership_mode="shared",
    )


def runtime_report():
    return SimpleNamespace(toolchain_digest="f" * 64)


def check_bundle():
    declaration = {
        "version": 1,
        "alias": "employee",
        "page_ids": [1],
        "checks": [
            {
                "id": "objects",
                "page_id": 1,
                "kind": "select",
                "verify_sql": "employee/objects.verify.sql",
                "expected_objects": ["APP.T"],
            },
            {
                "id": "home",
                "page_id": 1,
                "kind": "flow",
                "flow": "employee/home.flow.json",
                "steps": [
                    {
                        "action": "navigate",
                        "path": "/ords/r/app/employee/home",
                        "expected_visible_text": "Employee",
                    }
                ],
            },
        ],
    }
    return build_app_check_bundle(
        {
            "employee.json": json.dumps(declaration).encode("utf-8"),
            "employee/objects.verify.sql": (
                b"SELECT 'TEAM_ASSERT|' || assertion_name || '|' || status AS status "
                b"FROM (SELECT 'objects' assertion_name, 'PASS' status FROM dual);\n"
            ),
            "employee/home.flow.json": b"{}\n",
        },
        ("employee",),
    )


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="team-qualification-")
        self.root = Path(self.temp.name)
        (self.root / "ci" / "app-checks").mkdir(parents=True)
        declaration = check_bundle().declarations["employee"]
        (self.root / "ci" / "app-checks" / "employee.json").write_text(
            json.dumps(declaration),
            encoding="utf-8",
        )
        check_root = self.root / "ci" / "app-checks" / "employee"
        check_root.mkdir()
        check_root.joinpath("objects.verify.sql").write_bytes(
            check_bundle().member_bytes("employee/objects.verify.sql")
        )
        check_root.joinpath("home.flow.json").write_bytes(
            check_bundle().member_bytes("employee/home.flow.json")
        )
        self.bundle = check_bundle()
        self.flow_runner = self.root / "flow.sh"
        self.flow_runner.write_text(
            '#!/usr/bin/env bash\necho \'{"status":"PASS","diagnostic":""}\'\n',
            encoding="utf-8",
        )
        self.flow_runner.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def test_release_evidence_binding_rejects_each_independent_mismatch(self):
        valid = {
            "archive_digest": "a" * 64,
            "source_commit": "b" * 40,
            "application_checks": {"checks_digest": "c" * 64},
        }
        mutations = (
            ("archive_digest", {**valid, "archive_digest": "0" * 64}, "different release archive"),
            ("source_commit", {**valid, "source_commit": "0" * 40}, "different source commit"),
            (
                "checks_digest",
                {**valid, "application_checks": {"checks_digest": "0" * 64}},
                "application checks do not match",
            ),
        )
        for label, evidence, message in mutations:
            with self.subTest(label=label), self.assertRaisesRegex(EvidenceError, message):
                validate_release_evidence_binding(
                    evidence,
                    archive_digest="a" * 64,
                    source_commit="b" * 40,
                    checks_digest="c" * 64,
                )

    def test_release_evidence_binding_requires_exact_database_source(self):
        source = {
            "kind": "dev-database",
            "instance_id": "DEV1",
            "history_cut": 1,
            "history_digest": "c" * 64,
            "frontier_digest": "d" * 64,
        }
        evidence = {
            "version": 3,
            "source": source,
            "kind": "schema",
            "alias": None,
            "archive_digest": "a" * 64,
            "application_checks": {"checks_digest": "b" * 64},
        }
        validate_release_evidence_binding(
            evidence,
            archive_digest="a" * 64,
            source=source,
            checks_digest="b" * 64,
            kind="schema",
            alias=None,
        )
        with self.assertRaisesRegex(EvidenceError, "different database source"):
            validate_release_evidence_binding(
                evidence,
                archive_digest="a" * 64,
                source={**source, "history_cut": 2},
                checks_digest="b" * 64,
                kind="schema",
                alias=None,
            )

    def test_format3_schema_qualification_uses_database_source_and_apply_report_binding(self):
        config = config_for(role="test", environment="test")
        metadata = profile_target(config, "METADATA")
        source = {
            "kind": "dev-database",
            "instance_id": "DEV1",
            "history_cut": 3,
            "history_digest": "a" * 64,
            "frontier_digest": "b" * 64,
        }
        manifest = SimpleNamespace(
            format_version=3,
            kind="schema",
            alias=None,
            version="1.0.0",
            source_commit="",
            source=source,
            source_tree="c" * 64,
            archive_digest="e" * 64,
            toolchain={"sqlcl": "26.2.2.233.1901"},
            migrations=({
                "id": "20260924T100000__alice__one",
                "checksum": "a" * 64,
                "target": "tables",
                "destructive": False,
                "dependencies": [],
            },),
            events=(
                {"sequence": 1, "id": "20260924T100000__alice__one", "operation": "up", "checksum": "a" * 64},
                {"sequence": 2, "id": "20260924T100100__alice__two", "operation": "up", "checksum": "b" * 64},
                {"sequence": 3, "id": "20260924T100000__alice__one", "operation": "down", "checksum": "a" * 64},
            ),
        )
        apply_report = {
            "version": 1,
            "status": "applied",
            "source": source,
            "archive_digest": manifest.archive_digest,
            "target_state_key": metadata.state_key,
            "target_digest": "1" * 64,
            "history_digest": "2" * 64,
            "pending": ["20260924T100000__alice__one"],
        }
        with patch("teamlib.qualification.verify_release", return_value=manifest):
            report = qualify_target(
                self.root,
                config,
                source,
                (),
                store=FakeStore(),
                work=self.root / "format3-work",
                check_bundle=build_app_check_bundle({}, ()),
                release_archive=self.root / "format3.tar",
                apply_report=apply_report,
                runner_contract=Path("ci/runner-contract.json"),
                runtime_report=runtime_report(),
                run_identity={"run_id": "format3-test"},
                sql_runner=lambda *args, **kwargs: object(),
            )
        self.assertEqual(report["version"], 3)
        self.assertEqual(report["source"], source)
        self.assertNotIn("source_commit", report)
        validate_test_evidence(canonical_json(report) + b"\n")

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        evidence_path = self.root / "format3-evidence.json"
        write_report(report, evidence_path)
        key = Ed25519PrivateKey.generate()
        private = self.root / "format3-key.pem"
        private.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        archive = self.root / "format3.tar"
        archive.write_bytes(b"verified database archive")
        signature = self.root / "format3-evidence.sig"
        with patch("teamlib.qualification.verify_release", return_value=manifest), patch(
            "teamlib.qualification.release_app_check_bundle",
            return_value=SimpleNamespace(
                checks_digest=report["application_checks"]["checks_digest"]
            ),
        ):
            sign_test_evidence(evidence_path, archive, private, signature)
        key.public_key().verify(signature.read_bytes(), evidence_path.read_bytes())

        from teamlib.runbook import gen_runbook
        from cryptography.hazmat.primitives.serialization import PublicFormat

        public_key = self.root / "format3-key.pub.pem"
        public_key.write_bytes(key.public_key().public_bytes(
            serialization.Encoding.PEM,
            PublicFormat.SubjectPublicKeyInfo,
        ))
        runbook_plan = ReleasePlan(
            archive_digest=manifest.archive_digest,
            target_digest="1" * 64,
            pending=("20260924T100000__alice__one",),
            artifact_history_digest=source["history_digest"],
            target={"environment": "production"},
            events=(manifest.events[2],),
            replay_from=2,
        )
        with patch("teamlib.runbook.verify_release", return_value=manifest), patch(
            "teamlib.runbook.release_app_check_bundle",
            return_value=SimpleNamespace(checks_digest=report["application_checks"]["checks_digest"]),
        ), patch("teamlib.runbook.plan_release", return_value=runbook_plan):
            runbook = gen_runbook(
                archive,
                {"m1": {"status": "APPLIED", "checksum": "a" * 64, "sequence": 2}},
                {"environment": "production"},
                evidence_path,
                signature,
                public_key,
            )
        self.assertIn("3. REVERT (down): 20260924T100000__alice__one", runbook.text)
        self.assertIn("- Source history cut: 3", runbook.text)

        wrong_manifest = SimpleNamespace(
            **{**manifest.__dict__, "source": {**source, "history_cut": 2}}
        )
        with patch("teamlib.qualification.verify_release", return_value=wrong_manifest):
            with self.assertRaisesRegex(QualificationError, "archive database source"):
                qualify_target(
                    self.root,
                    config,
                    source,
                    (),
                    store=FakeStore(),
                    work=self.root / "format3-wrong-archive",
                    check_bundle=build_app_check_bundle({}, ()),
                    release_archive=archive,
                    apply_report=apply_report,
                    runner_contract=Path("ci/runner-contract.json"),
                    runtime_report=runtime_report(),
                    run_identity={"run_id": "format3-test"},
                    sql_runner=lambda *args, **kwargs: object(),
                )

        wrong_apply = {**apply_report, "source": {**source, "frontier_digest": "f" * 64}}
        with patch("teamlib.qualification.verify_release", return_value=manifest):
            with self.assertRaisesRegex(QualificationError, "apply report database source"):
                qualify_target(
                    self.root,
                    config,
                    source,
                    (),
                    store=FakeStore(),
                    work=self.root / "format3-wrong-apply",
                    check_bundle=build_app_check_bundle({}, ()),
                    release_archive=self.root / "format3.tar",
                    apply_report=wrong_apply,
                    runner_contract=Path("ci/runner-contract.json"),
                    runtime_report=runtime_report(),
                    sql_runner=lambda *args, **kwargs: object(),
                )

    def test_format3_app_qualification_signs_and_generates_local_handoff(self):
        config = config_for(role="test", environment="test")
        metadata = profile_target(config, "METADATA")
        source = {
            "kind": "dev-database",
            "instance_id": "DEV1",
            "history_cut": 0,
            "history_digest": "a" * 64,
            "frontier_digest": "b" * 64,
            "app_generation": 7,
            "app_tree_digest": "c" * 64,
            "app_checks_digest": self.bundle.checks_digest,
            "master_contract_digest": "d" * 64,
        }
        manifest = SimpleNamespace(
            format_version=3, kind="app", alias="employee", version="2.0.0",
            source_commit="", source=source, source_tree="e" * 64,
            archive_digest="f" * 64, app_tree_digests={"employee": "c" * 64},
            app_checks_digest=self.bundle.checks_digest, events=(), required_migrations=(),
            toolchain={"sqlcl": "26.2.2.233.1901"},
        )
        apply_report = {
            "version": 1, "status": "applied", "source": source,
            "archive_digest": manifest.archive_digest,
            "target_state_key": metadata.state_key,
            "target_digest": "1" * 64, "history_digest": "2" * 64, "pending": [],
        }
        fake_app = AppCheckReport(
            "", {"target_kind": "persistent"},
            (
                CheckResult("employee", "objects", 1, "select", "PASS"),
                CheckResult("employee", "home", 1, "flow", "PASS"),
            ),
            "3" * 64, self.bundle.checks_digest,
            {"apps": ["employee"], "pages": {"employee": [1]}, "checks": 2, "unknown": 0},
            "PASS", source,
        )
        archive = self.root / "format3-app.tar"
        archive.write_bytes(b"database app release archive")
        with patch("teamlib.qualification.verify_release", return_value=manifest), patch(
            "teamlib.qualification.verify_candidate_apps", return_value=fake_app,
        ):
            report = qualify_target(
                self.root, config, source, ("employee",),
                store=FakeStore(), work=self.root / "format3-app-work",
                check_bundle=self.bundle, release_archive=archive, apply_report=apply_report,
                flow_executable=str(self.flow_runner),
                runner_contract=Path("ci/runner-contract.json"), runtime_report=runtime_report(),
                run_identity={"run_id": "format3-app-test"},
                sql_runner=lambda *args, **kwargs: object(),
            )
        self.assertEqual(report["kind"], "app")
        self.assertEqual(report["alias"], "employee")
        self.assertEqual(report["source"], source)
        self.assertEqual(report["application_checks"]["coverage"]["apps"], ["employee"])
        validate_test_evidence(canonical_json(report) + b"\n")

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import PublicFormat
        from teamlib.runbook import gen_runbook

        key = Ed25519PrivateKey.generate()
        private = self.root / "app-signing-key.pem"
        private.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        evidence = self.root / "app-test-evidence.json"
        write_report(report, evidence)
        signature = self.root / "app-test-evidence.sig"
        public = self.root / "app-trust-key.pem"
        public.write_bytes(key.public_key().public_bytes(
            serialization.Encoding.PEM, PublicFormat.SubjectPublicKeyInfo,
        ))
        with patch("teamlib.qualification.verify_release", return_value=manifest), patch(
            "teamlib.qualification.release_app_check_bundle", return_value=self.bundle,
        ):
            sign_test_evidence(evidence, archive, private, signature)
        runbook_plan = ReleasePlan(
            manifest.archive_digest, "1" * 64, (), source["history_digest"],
            {"environment": "production"}, history_digest="2" * 64,
        )
        with patch("teamlib.runbook.verify_release", return_value=manifest), patch(
            "teamlib.runbook.release_app_check_bundle", return_value=self.bundle,
        ), patch("teamlib.runbook.plan_release", return_value=runbook_plan):
            runbook = gen_runbook(
                archive, {}, {"environment": "production", "app_ids": {"employee": 901}},
                evidence, signature, public,
            )
        self.assertEqual(runbook.kind, "app")
        self.assertEqual(runbook.alias, "employee")
        self.assertIn("Application generation: 7", runbook.text)
        self.assertIn(self.bundle.checks_digest, runbook.text)

    def test_format3_evidence_rejects_commit_field_and_malformed_source(self):
        config = config_for(role="test", environment="test")
        targets = {
            profile: profile_target(config, profile)
            for profile in ("TABLES", "CODE", "METADATA", "VERIFY")
        }
        source = {
            "kind": "dev-database",
            "instance_id": "DEV1",
            "history_cut": 1,
            "history_digest": "a" * 64,
            "frontier_digest": "b" * 64,
        }
        report = {
            "version": 3,
            "final_status": "PASS",
            "source": source,
            "archive_digest": "e" * 64,
            "toolchain_digest": "f" * 64,
            "kind": "schema",
            "alias": None,
            "target_identity": _target_identity(config, targets),
            "run_identity": {"run_id": "1"},
            "qualification_identity": {
                "target_kind": "persistent",
                "observation_sequence": 3,
                "observation_digest": "c" * 64,
                "history_digest": "d" * 64,
            },
            "application_checks": {
                "status": "PASS",
                "checks_digest": "0" * 64,
                "coverage": {"apps": [], "pages": {}, "checks": 0, "unknown": 0},
                "unknown": 0,
                "results": [],
            },
            "results": {
                "migrations": "PASS",
                "application_deploy": "PASS",
                "application_checks": "PASS",
            },
        }
        validate_test_evidence(canonical_json(report) + b"\n")
        with_commit = {**report, "source_commit": "a" * 40}
        with self.assertRaisesRegex(EvidenceError, "unexpected version-3 shape"):
            validate_test_evidence(canonical_json(with_commit) + b"\n")
        malformed_source = {**report, "source": {**source, "history_cut": 0}}
        with self.assertRaisesRegex(EvidenceError, "source history cut"):
            validate_test_evidence(canonical_json(malformed_source) + b"\n")

    def test_persistent_report_has_one_source_commit_and_frontier_identity(self):
        bundle = self.bundle
        fake_app = AppCheckReport(
            "a" * 40,
            {"target_kind": "persistent", "instance_id": "INSTANCE"},
            (),
            "a" * 64,
            bundle.checks_digest,
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
                check_bundle=bundle,
                flow_executable=str(self.flow_runner),
                runner_contract=Path("ci/runner-contract.json"),
                runtime_report=runtime_report(),
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

    def test_release_qualification_requires_an_explicit_check_bundle(self):
        config = config_for(role="test", environment="test")
        manifest = SimpleNamespace(source_commit="a" * 40, archive_digest="e" * 64)
        with patch("teamlib.qualification.verify_release", return_value=manifest):
            with self.assertRaisesRegex(QualificationError, "check bundle"):
                qualify_target(
                    self.root,
                    config,
                    "a" * 40,
                    ("employee",),
                    store=FakeStore(),
                    work=self.root / "missing-bundle",
                    check_bundle=None,
                    release_archive=self.root / "release.tar",
                    apply_report=self.apply_report_for(config).as_dict(),
                    runner_contract=Path("ci/runner-contract.json"),
                    runtime_report=runtime_report(),
                    sql_runner=lambda *args, **kwargs: object(),
                )

    def apply_report_for(self, config):
        metadata = profile_target(config, "METADATA")
        return ApplyReport(
            "applied", (), archive_digest="e" * 64, source_commit="a" * 40,
            target_state_key=metadata.state_key, target_digest="1" * 64,
            history_digest="2" * 64,
        )

    def test_qualify_target_accepts_an_apply_report_mapping_from_the_live_adapter(self):
        config = config_for()
        fake_app = AppCheckReport(
            "a" * 40, {"target_kind": "persistent", "instance_id": "INSTANCE"}, (),
            "a" * 64, self.bundle.checks_digest, {"apps": ["employee"], "checks": 1, "unknown": 0}, "PASS",
        )
        manifest = SimpleNamespace(source_commit="a" * 40, archive_digest="e" * 64)
        apply_report = self.apply_report_for(config)
        with patch("teamlib.qualification.verify_candidate_apps", return_value=fake_app), \
                patch("teamlib.qualification.verify_release", return_value=manifest):
            report = qualify_target(
                self.root, config, "a" * 40, ("employee",),
                store=FakeStore(), work=self.root / "work",
                release_archive=self.root / "release.tar",
                apply_report=apply_report.as_dict(),
                check_bundle=self.bundle,
                flow_executable=str(self.flow_runner),
                runner_contract=Path("ci/runner-contract.json"),
                runtime_report=runtime_report(),
                sql_runner=lambda *args, **kwargs: object(),
            )
        self.assertEqual(report["final_status"], "PASS")
        self.assertEqual(report["archive_digest"], "e" * 64)

    def test_qualify_target_accepts_an_apply_report_json_file(self):
        config = config_for()
        fake_app = AppCheckReport(
            "a" * 40, {"target_kind": "persistent", "instance_id": "INSTANCE"}, (),
            "a" * 64, self.bundle.checks_digest, {"apps": ["employee"], "checks": 1, "unknown": 0}, "PASS",
        )
        manifest = SimpleNamespace(source_commit="a" * 40, archive_digest="e" * 64)
        apply_report_path = self.root / "apply-report.json"
        apply_report_path.write_text(
            json.dumps(self.apply_report_for(config).as_dict()), encoding="utf-8",
        )
        with patch("teamlib.qualification.verify_candidate_apps", return_value=fake_app), \
                patch("teamlib.qualification.verify_release", return_value=manifest):
            report = qualify_target(
                self.root, config, "a" * 40, ("employee",),
                store=FakeStore(), work=self.root / "work2",
                release_archive=self.root / "release.tar",
                apply_report=apply_report_path,
                check_bundle=self.bundle,
                flow_executable=str(self.flow_runner),
                runner_contract=Path("ci/runner-contract.json"),
                runtime_report=runtime_report(),
                sql_runner=lambda *args, **kwargs: object(),
            )
        self.assertEqual(report["final_status"], "PASS")

    def test_qualify_target_rejects_a_malformed_apply_report_mapping(self):
        config = config_for()
        manifest = SimpleNamespace(source_commit="a" * 40, archive_digest="e" * 64)
        with patch("teamlib.qualification.verify_release", return_value=manifest):
            with self.assertRaisesRegex(QualificationError, "unexpected shape|not a successful"):
                qualify_target(
                    self.root, config, "a" * 40, ("employee",),
                    store=FakeStore(), work=self.root / "work3",
                    release_archive=self.root / "release.tar",
                    apply_report={"version": 1, "status": "pending"},
                    check_bundle=self.bundle,
                    runner_contract=Path("ci/runner-contract.json"),
                    runtime_report=runtime_report(),
                    sql_runner=lambda *args, **kwargs: object(),
                )

    def test_qualify_target_rejects_an_unreadable_apply_report_file(self):
        config = config_for()
        manifest = SimpleNamespace(source_commit="a" * 40, archive_digest="e" * 64)
        bad_path = self.root / "bad-apply-report.json"
        bad_path.write_text("not json", encoding="utf-8")
        with patch("teamlib.qualification.verify_release", return_value=manifest):
            with self.assertRaisesRegex(QualificationError, "not valid UTF-8 JSON"):
                qualify_target(
                    self.root, config, "a" * 40, ("employee",),
                    store=FakeStore(), work=self.root / "work4",
                    release_archive=self.root / "release.tar",
                    apply_report=bad_path,
                    check_bundle=self.bundle,
                    runner_contract=Path("ci/runner-contract.json"),
                    runtime_report=runtime_report(),
                    sql_runner=lambda *args, **kwargs: object(),
                )

    def test_production_and_unresolved_attempts_refuse(self):
        with self.assertRaisesRegex(QualificationError, "non-production"):
            qualify_target(
                self.root, config_for("production", "production"), "a" * 40, ("employee",),
                store=FakeStore(), work=self.root / "work",
                check_bundle=self.bundle,
                runner_contract=Path("ci/runner-contract.json"),
                runtime_report=runtime_report(),
                sql_runner=lambda *args, **kwargs: object(),
            )
        with self.assertRaisesRegex(QualificationError, "unresolved"):
            qualify_target(
                self.root, config_for(), "a" * 40, ("employee",),
                store=FakeStore(attempts={"attempt": {"state": "UNKNOWN"}}), work=self.root / "work2",
                check_bundle=self.bundle,
                runner_contract=Path("ci/runner-contract.json"),
                runtime_report=runtime_report(),
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
        archive = self.root / "release.tar"
        archive.write_bytes(b"verified release")
        manifest = SimpleNamespace(archive_digest="a" * 64, source_commit="a" * 40)
        with patch("teamlib.qualification.verify_release", return_value=manifest), patch(
            "teamlib.qualification.release_app_check_bundle",
            return_value=SimpleNamespace(checks_digest="e" * 64),
        ):
            digest = sign_test_evidence(evidence, archive, private, signature)
        self.assertEqual(digest, hashlib.sha256(evidence.read_bytes()).hexdigest())
        key.public_key().verify(signature.read_bytes(), evidence.read_bytes())

        mismatched = dict(report)
        mismatched["application_checks"] = {
            **report["application_checks"],
            "checks_digest": "0" * 64,
        }
        mismatched_path = self.root / "mismatched-evidence.json"
        write_report(mismatched, mismatched_path)
        refused_signature = self.root / "mismatched-evidence.sig"
        with patch("teamlib.qualification.verify_release", return_value=manifest), patch(
            "teamlib.qualification.release_app_check_bundle",
            return_value=SimpleNamespace(checks_digest="e" * 64),
        ):
            with self.assertRaisesRegex(QualificationError, "application checks do not match"):
                sign_test_evidence(
                    mismatched_path, archive, private, refused_signature
                )
        self.assertFalse(refused_signature.exists())

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
                    sign_test_evidence(
                        evidence,
                        self.root / "release.tar",
                        private,
                        self.root / f"invalid-{index}.sig",
                    )


if __name__ == "__main__":
    unittest.main()
