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

from teamlib.release import release_app_check_bundle
from scripts.tests.release_fixtures import _build_git_release_fixture
from teamlib.release import ReleasePlan
from teamlib.runbook import RunbookError, _runbook_text, gen_runbook


class RunbookTests(unittest.TestCase):
    def test_format3_schema_runbook_preserves_database_event_order(self):
        events = (
            {"sequence": 1, "id": "20260907T100000__alice__one", "operation": "up", "checksum": "a" * 64},
            {"sequence": 2, "id": "20260907T100100__alice__two", "operation": "up", "checksum": "b" * 64},
            {"sequence": 3, "id": "20260907T100000__alice__one", "operation": "down", "checksum": "a" * 64},
        )
        manifest = type("VerifiedDatabaseManifest", (), {
            "kind": "schema",
            "format_version": 3,
            "version": "1.0.0",
            "source_commit": "",
            "source_tree": "c" * 64,
            "toolchain": {"sqlcl": "26.2"},
            "migrations": (
                {"id": "20260907T100000__alice__one", "checksum": "a" * 64, "target": "tables", "destructive": False, "dependencies": []},
                {"id": "20260907T100100__alice__two", "checksum": "b" * 64, "target": "tables", "destructive": False, "dependencies": []},
            ),
            "events": events,
        })()
        plan = ReleasePlan(
            archive_digest="d" * 64,
            target_digest="e" * 64,
            pending=("20260907T100000__alice__one", "20260907T100100__alice__two"),
            artifact_history_digest="f" * 64,
            target={"environment": "production"},
        )

        text = _runbook_text(
            manifest,
            plan,
            {"environment": "production"},
            {"target_identity": {"environment": "test"}},
            "1" * 64,
        )

        section = text.split("## Verified source ledger event order", 1)[1].split("## Planned target replay events", 1)[0]
        self.assertLess(section.index("1. APPLY (up): 20260907T100000__alice__one"), section.index("2. APPLY (up): 20260907T100100__alice__two"))
        self.assertLess(section.index("2. APPLY (up): 20260907T100100__alice__two"), section.index("3. REVERT (down): 20260907T100000__alice__one"))
        self.assertIn("## Planned target replay events", text)
        self.assertIn("including each down transition", text)
        self.assertNotIn("Apply only the listed pending migrations in dependency order", text)

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
            (repo / "app_context" / "a").mkdir(parents=True)
            (repo / "app_context" / "a" / "release.json").write_text('{"version": 1, "requires": []}\n', encoding="utf-8")
            checks = repo / "ci" / "app-checks"
            checks.mkdir(parents=True)
            checks.joinpath("a.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "alias": "a",
                        "page_ids": [1],
                        "checks": [
                            {
                                "id": "objects",
                                "page_id": 1,
                                "kind": "select",
                                "verify_sql": "a/objects.verify.sql",
                                "expected_objects": [],
                            },
                            {
                                "id": "home",
                                "page_id": 1,
                                "kind": "flow",
                                "flow": "a/home.flow.json",
                                "steps": [
                                    {
                                        "action": "navigate",
                                        "path": "/ords/r/app/a/home",
                                        "expected_visible_text": "Home",
                                    }
                                ],
                            },
                        ],
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            checks.joinpath("a").mkdir()
            checks.joinpath("a", "objects.verify.sql").write_text(
                "SELECT 'TEAM_ASSERT|' || assertion_name || '|' || status AS status "
                "FROM (SELECT 'objects' assertion_name, 'PASS' status FROM dual);\n",
                encoding="utf-8",
            )
            checks.joinpath("a", "home.flow.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
            commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            manifest = _build_git_release_fixture(repo, commit, "1.0.0", root / "out", kind="app", alias="a")
            bundle = release_app_check_bundle(manifest.archive_path)
            evidence_data = {
                "version": 2, "kind": "app", "alias": "a", "final_status": "PASS", "archive_digest": manifest.archive_digest, "source_commit": commit,
                "toolchain_digest": "b" * 64,
                "target_identity": {
                    "project": "team-template", "role": "test", "environment": "test",
                    "target_kind": "persistent", "instance_id": "TEST", "db_name": "FREEPDB1",
                    "service": "freep1", "workspace_id": 1, "app_ids": {"a": 1},
                    "state_key": "f" * 64, "binding_digest": "e" * 64,
                },
                "run_identity": {"run_id": "ci-run-1"},
                "qualification_identity": {"target_kind": "persistent", "observation_sequence": 1, "observation_digest": "d" * 64, "history_digest": "e" * 64},
                "application_checks": {
                    "status": "PASS", "checks_digest": bundle.checks_digest,
                    "coverage": {"apps": ["a"], "pages": {"a": [1]}, "checks": 1, "unknown": 0},
                    "unknown": 0,
                    "results": [{
                        "alias": "a", "check_id": "objects", "page_id": 1,
                        "kind": "select", "status": "PASS", "expected_objects": [],
                        "diagnostic": "", "observed": {"status": "PASS"},
                    }],
                },
                "results": {"migrations": "PASS", "application_deploy": "PASS", "application_checks": "PASS"},
            }
            evidence = root / "evidence.json"
            evidence.write_bytes(json.dumps(evidence_data, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
            key = Ed25519PrivateKey.generate()
            public_bytes = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            (root / "key.pem").write_bytes(public_bytes)
            (root / "sig").write_bytes(key.sign(evidence.read_bytes()))
            runbook = gen_runbook(manifest.archive_path, {}, {"environment": "production", "instance_id": "PROD", "workspace_id": 1, "app_ids": {"a": 2}, "parsing_schemas": {"a": "A_SCHEMA"}}, evidence, root / "sig", root / "key.pem")
            self.assertIn("PROD", runbook.text)

            mismatched = dict(evidence_data)
            mismatched["application_checks"] = {
                **evidence_data["application_checks"],
                "checks_digest": "c" * 64,
            }
            mismatch_path = root / "mismatched-evidence.json"
            mismatch_path.write_bytes(
                json.dumps(mismatched, sort_keys=True, separators=(",", ":")).encode("utf-8")
                + b"\n"
            )
            mismatch_signature = root / "mismatched.sig"
            mismatch_signature.write_bytes(key.sign(mismatch_path.read_bytes()))
            with self.assertRaisesRegex(RunbookError, "application checks do not match"):
                gen_runbook(
                    manifest.archive_path,
                    {},
                    {
                        "environment": "production",
                        "instance_id": "PROD",
                        "workspace_id": 1,
                        "app_ids": {"a": 2},
                        "parsing_schemas": {"a": "A_SCHEMA"},
                    },
                    mismatch_path,
                    mismatch_signature,
                    root / "key.pem",
                )
            self.assertIn(manifest.archive_digest, runbook.text)
            invalid_identities = (
                {**evidence_data["target_identity"], "role": "integration"},
                {**evidence_data["target_identity"], "environment": "staging"},
                {**evidence_data["target_identity"], "target_kind": "disposable"},
                {key: value for key, value in evidence_data["target_identity"].items() if key != "state_key"},
            )
            for index, identity in enumerate(invalid_identities):
                with self.subTest(identity=index):
                    invalid = dict(evidence_data)
                    invalid["target_identity"] = identity
                    invalid_path = root / f"invalid-{index}.json"
                    invalid_raw = json.dumps(invalid, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
                    invalid_path.write_bytes(invalid_raw)
                    invalid_signature = root / f"invalid-{index}.sig"
                    invalid_signature.write_bytes(key.sign(invalid_raw))
                    with self.assertRaises(RunbookError):
                        gen_runbook(
                            manifest.archive_path,
                            {},
                            {"environment": "production", "instance_id": "PROD", "workspace_id": 1, "app_ids": {"a": 2}, "parsing_schemas": {"a": "A_SCHEMA"}},
                            invalid_path,
                            invalid_signature,
                            root / "key.pem",
                        )

    def test_schema_runbook_contains_migrations_only(self):
        with tempfile.TemporaryDirectory(prefix="team-schema-runbook-") as directory:
            root = Path(directory)
            repo = root / "repo"
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "x@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "x"], check=True)
            m = repo / "migrations"
            m.mkdir(parents=True)
            (m / "20260907T100000__alice__one.sql").write_text("-- migration-version: 1\n-- target: tables\n-- destructive: false\n\nCREATE TABLE t (id NUMBER);\n", encoding="utf-8")
            (m / "20260907T100000__alice__one.verify.sql").write_text("", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
            commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            manifest = _build_git_release_fixture(repo, commit, "1.0.0", root / "out", kind="schema")
            bundle = release_app_check_bundle(manifest.archive_path)
            evidence_data = {
                "version": 2, "kind": "schema", "alias": None, "final_status": "PASS",
                "archive_digest": manifest.archive_digest, "source_commit": commit,
                "toolchain_digest": "b" * 64,
                "target_identity": {
                    "project": "team-template", "role": "test", "environment": "test",
                    "target_kind": "persistent", "instance_id": "TEST", "db_name": "FREEPDB1",
                    "service": "freep1", "workspace_id": 1, "app_ids": {"a": 1},
                    "state_key": "f" * 64, "binding_digest": "e" * 64,
                },
                "run_identity": {"run_id": "ci-run-1"},
                "qualification_identity": {"target_kind": "persistent", "observation_sequence": 1, "observation_digest": "d" * 64, "history_digest": "e" * 64},
                "application_checks": {
                    "status": "PASS", "checks_digest": bundle.checks_digest,
                    "coverage": {"apps": [], "pages": {}, "checks": 0, "unknown": 0},
                    "unknown": 0,
                    "results": [],
                },
                "results": {"migrations": "PASS", "application_deploy": "PASS", "application_checks": "PASS"},
            }
            evidence = root / "evidence.json"
            evidence.write_bytes(json.dumps(evidence_data, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
            key = Ed25519PrivateKey.generate()
            public_bytes = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            (root / "key.pem").write_bytes(public_bytes)
            (root / "sig").write_bytes(key.sign(evidence.read_bytes()))
            runbook = gen_runbook(
                manifest.archive_path,
                {},
                {"environment": "production", "instance_id": "PROD", "workspace_id": 1, "app_ids": {"a": 2}},
                evidence,
                root / "sig",
                root / "key.pem",
            )
            self.assertEqual(runbook.kind, "schema")
            self.assertIsNone(runbook.alias)
            self.assertIn("20260907T100000__alice__one", runbook.text)
            self.assertIn("Pending migration plan", runbook.text)
            self.assertNotIn("Exact database event replay order", runbook.text)
            self.assertIn("Apply only the listed pending migrations in dependency order", runbook.text)
            self.assertNotIn("Application and master order", runbook.text)
            self.assertNotIn("Import applications in master-before-subscriber order", runbook.text)

    def test_app_runbook_contains_one_app_and_prerequisites_only(self):
        with tempfile.TemporaryDirectory(prefix="team-app-runbook-") as directory:
            root = Path(directory)
            repo = root / "repo"
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "x@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "x"], check=True)
            m = repo / "migrations"
            m.mkdir(parents=True)
            (m / "20260907T100000__alice__one.sql").write_text("-- migration-version: 1\n-- target: tables\n-- destructive: false\n\nCREATE TABLE t (id NUMBER);\n", encoding="utf-8")
            (m / "20260907T100000__alice__one.verify.sql").write_text("", encoding="utf-8")
            for alias in ("hr", "payroll"):
                (repo / "apps" / alias / ".apex").mkdir(parents=True)
                (repo / "apps" / alias / "application.apx").write_bytes(f"app-{alias}".encode())
                (repo / "apps" / alias / ".apex" / "apexlang.json").write_bytes(b"{}")
                (repo / "app_context" / alias).mkdir(parents=True)
            (repo / "app_context" / "hr" / "release.json").write_text(
                '{"version": 1, "requires": ["20260907T100000__alice__one"]}\n', encoding="utf-8"
            )
            (repo / "app_context" / "payroll" / "release.json").write_text(
                '{"version": 1, "requires": []}\n', encoding="utf-8"
            )
            checks = repo / "ci" / "app-checks"
            checks.mkdir(parents=True)
            for alias in ("hr", "payroll"):
                checks.joinpath(f"{alias}.json").write_text(
                    json.dumps({
                        "version": 1, "alias": alias, "page_ids": [1],
                        "checks": [
                            {
                                "id": "c1", "page_id": 1, "kind": "select",
                                "verify_sql": f"{alias}/c1.verify.sql", "expected_objects": [],
                            },
                            {
                                "id": "home", "page_id": 1, "kind": "flow",
                                "flow": f"{alias}/home.flow.json",
                                "steps": [{"action": "navigate", "path": f"/ords/r/app/{alias}/home", "expected_visible_text": "Home"}],
                            },
                        ],
                    }, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                checks.joinpath(alias).mkdir()
                checks.joinpath(alias, "c1.verify.sql").write_text(
                    "SELECT 'TEAM_ASSERT|' || assertion_name || '|' || status AS status FROM (SELECT 'c1' assertion_name, 'PASS' status FROM dual);\n",
                    encoding="utf-8",
                )
                checks.joinpath(alias, "home.flow.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
            commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            manifest = _build_git_release_fixture(repo, commit, "1.0.0", root / "out", kind="app", alias="hr")
            bundle = release_app_check_bundle(manifest.archive_path)
            evidence_data = {
                "version": 2, "kind": "app", "alias": "hr", "final_status": "PASS",
                "archive_digest": manifest.archive_digest, "source_commit": commit,
                "toolchain_digest": "b" * 64,
                "target_identity": {
                    "project": "team-template", "role": "test", "environment": "test",
                    "target_kind": "persistent", "instance_id": "TEST", "db_name": "FREEPDB1",
                    "service": "freep1", "workspace_id": 1, "app_ids": {"hr": 100, "payroll": 200},
                    "state_key": "f" * 64, "binding_digest": "e" * 64,
                },
                "run_identity": {"run_id": "ci-run-1"},
                "qualification_identity": {"target_kind": "persistent", "observation_sequence": 1, "observation_digest": "d" * 64, "history_digest": "e" * 64},
                "application_checks": {
                    "status": "PASS", "checks_digest": bundle.checks_digest,
                    "coverage": {"apps": ["hr"], "pages": {"hr": [1]}, "checks": 2, "unknown": 0},
                    "unknown": 0,
                    "results": [
                        {
                            "alias": "hr", "check_id": "c1", "page_id": 1,
                            "kind": "select", "status": "PASS", "expected_objects": [],
                            "diagnostic": "", "observed": {"status": "PASS"},
                        },
                        {
                            "alias": "hr", "check_id": "home", "page_id": 1,
                            "kind": "flow", "status": "PASS", "expected_objects": [],
                            "diagnostic": "", "observed": {"status": "PASS"},
                        },
                    ],
                },
                "results": {"migrations": "PASS", "application_deploy": "PASS", "application_checks": "PASS"},
            }
            evidence = root / "evidence.json"
            evidence.write_bytes(json.dumps(evidence_data, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
            key = Ed25519PrivateKey.generate()
            public_bytes = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            (root / "key.pem").write_bytes(public_bytes)
            (root / "sig").write_bytes(key.sign(evidence.read_bytes()))
            req_checksum = manifest.required_migrations[0]["checksum"]
            history = {"20260907T100000__alice__one": {"status": "APPLIED", "checksum": req_checksum}}
            runbook = gen_runbook(
                manifest.archive_path,
                history,
                {"environment": "production", "instance_id": "PROD", "workspace_id": 1, "app_ids": {"hr": 100, "payroll": 200}, "parsing_schemas": {"hr": "HR_SCHEMA", "payroll": "PAY_SCHEMA"}},
                evidence,
                root / "sig",
                root / "key.pem",
            )
            self.assertEqual(runbook.kind, "app")
            self.assertEqual(runbook.alias, "hr")
            self.assertIn("hr", runbook.text)
            self.assertIn("20260907T100000__alice__one", runbook.text)
            self.assertNotIn("Deploy application payroll", runbook.text)
            self.assertNotIn("Import application payroll", runbook.text)
            self.assertNotIn("PAY_SCHEMA", runbook.text)
            self.assertNotIn("Pending migration plan", runbook.text)
            self.assertIn("Prerequisite migration requirements", runbook.text)

    def test_rejection_of_signed_evidence_for_different_app_or_kind(self):
        with tempfile.TemporaryDirectory(prefix="team-reject-evidence-") as directory:
            root = Path(directory)
            repo = root / "repo"
            import subprocess
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "x@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "x"], check=True)
            m = repo / "migrations"
            m.mkdir(parents=True)
            (m / "20260907T100000__alice__one.sql").write_text("-- migration-version: 1\n-- target: tables\n-- destructive: false\n\nCREATE TABLE t (id NUMBER);\n", encoding="utf-8")
            (m / "20260907T100000__alice__one.verify.sql").write_text("", encoding="utf-8")
            for alias in ("hr", "payroll"):
                (repo / "apps" / alias / ".apex").mkdir(parents=True)
                (repo / "apps" / alias / "application.apx").write_bytes(f"app-{alias}".encode())
                (repo / "apps" / alias / ".apex" / "apexlang.json").write_bytes(b"{}")
                (repo / "app_context" / alias).mkdir(parents=True)
                (repo / "app_context" / alias / "release.json").write_text('{"version": 1, "requires": []}\n', encoding="utf-8")
            checks = repo / "ci" / "app-checks"
            checks.mkdir(parents=True)
            for alias in ("hr", "payroll"):
                checks.joinpath(f"{alias}.json").write_text(
                    json.dumps({
                        "version": 1, "alias": alias, "page_ids": [1],
                        "checks": [
                            {
                                "id": "c1", "page_id": 1, "kind": "select",
                                "verify_sql": f"{alias}/c1.verify.sql", "expected_objects": [],
                            },
                            {
                                "id": "home", "page_id": 1, "kind": "flow",
                                "flow": f"{alias}/home.flow.json",
                                "steps": [{"action": "navigate", "path": f"/ords/r/app/{alias}/home", "expected_visible_text": "Home"}],
                            },
                        ],
                    }, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                checks.joinpath(alias).mkdir()
                checks.joinpath(alias, "c1.verify.sql").write_text(
                    "SELECT 'TEAM_ASSERT|' || assertion_name || '|' || status AS status FROM (SELECT 'c1' assertion_name, 'PASS' status FROM dual);\n",
                    encoding="utf-8",
                )
                checks.joinpath(alias, "home.flow.json").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "seed"], check=True)
            commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            manifest_hr = _build_git_release_fixture(repo, commit, "1.0.0", root / "out-hr", kind="app", alias="hr")
            manifest_payroll = _build_git_release_fixture(repo, commit, "1.0.0", root / "out-payroll", kind="app", alias="payroll")
            manifest_schema = _build_git_release_fixture(repo, commit, "1.0.0", root / "out-schema", kind="schema")
            bundle_hr = release_app_check_bundle(manifest_hr.archive_path)

            evidence_data_hr = {
                "version": 2, "kind": "app", "alias": "hr", "final_status": "PASS",
                "archive_digest": manifest_hr.archive_digest,
                "source_commit": commit,
                "toolchain_digest": "b" * 64,
                "target_identity": {
                    "project": "team-template", "role": "test", "environment": "test",
                    "target_kind": "persistent", "instance_id": "TEST", "db_name": "FREEPDB1",
                    "service": "freep1", "workspace_id": 1, "app_ids": {"hr": 100, "payroll": 200},
                    "state_key": "f" * 64, "binding_digest": "e" * 64,
                },
                "run_identity": {"run_id": "ci-run-1"},
                "qualification_identity": {"target_kind": "persistent", "observation_sequence": 1, "observation_digest": "d" * 64, "history_digest": "e" * 64},
                "application_checks": {
                    "status": "PASS", "checks_digest": bundle_hr.checks_digest,
                    "coverage": {"apps": ["hr"], "pages": {"hr": [1]}, "checks": 2, "unknown": 0},
                    "unknown": 0,
                    "results": [
                        {
                            "alias": "hr", "check_id": "c1", "page_id": 1,
                            "kind": "select", "status": "PASS", "expected_objects": [],
                            "diagnostic": "", "observed": {"status": "PASS"},
                        },
                        {
                            "alias": "hr", "check_id": "home", "page_id": 1,
                            "kind": "flow", "status": "PASS", "expected_objects": [],
                            "diagnostic": "", "observed": {"status": "PASS"},
                        },
                    ],
                },
                "results": {"migrations": "PASS", "application_deploy": "PASS", "application_checks": "PASS"},
            }
            evidence_hr = root / "evidence-hr.json"
            evidence_hr.write_bytes(json.dumps(evidence_data_hr, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
            key = Ed25519PrivateKey.generate()
            public_bytes = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            (root / "key.pem").write_bytes(public_bytes)
            sig_hr = root / "evidence-hr.sig"
            sig_hr.write_bytes(key.sign(evidence_hr.read_bytes()))

            target = {"environment": "production", "instance_id": "PROD", "workspace_id": 1, "app_ids": {"hr": 100, "payroll": 200}, "parsing_schemas": {"hr": "HR_S", "payroll": "PAY_S"}}
            with self.assertRaises(RunbookError):
                gen_runbook(manifest_payroll.archive_path, {}, target, evidence_hr, sig_hr, root / "key.pem")
            with self.assertRaises(RunbookError):
                gen_runbook(manifest_schema.archive_path, {}, target, evidence_hr, sig_hr, root / "key.pem")

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
