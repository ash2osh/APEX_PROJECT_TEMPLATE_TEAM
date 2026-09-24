from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
import json
import subprocess
import tempfile
import unittest

from teamlib.config import Target
from teamlib.masters import MasterError, validate_masters
from teamlib.migration_bundle import bundle_checksum
from teamlib.release import (
    ReleaseError,
    apply_release,
    build_release,
    plan_release,
    verify_release,
)


ROOT = Path(__file__).resolve().parents[2]


class WorkflowContractTests(unittest.TestCase):
    def test_workflows_use_exact_sha_and_never_pull_request_target(self):
        database = (ROOT / ".github" / "workflows" / "database-checks.yml").read_text(encoding="utf-8")
        integration = (ROOT / ".github" / "workflows" / "integration.yml").read_text(encoding="utf-8")
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        for workflow in (database, integration, release):
            self.assertNotIn("pull_request_target", workflow)
            self.assertIn("github.sha", workflow)
        self.assertEqual(database.count("\n  offline:"), 1)
        self.assertNotIn("ci-" + "replay", database)
        self.assertNotIn("docker", database.lower())
        self.assertNotIn("oracle/free", database.lower())
        self.assertNotIn("command -v sql", database)
        self.assertNotIn("upload-artifact", database)
        self.assertIn("verify-release", release)
        self.assertIn("release.tar", release)
        self.assertIn("runs-on: [self-hosted, team-apex, test]", release)
        self.assertEqual(release.count("run-release-test"), 1)
        self.assertNotIn("TEAM_TEST_HISTORY_JSON", release)
        self.assertNotIn("apply_release.sh", release)
        self.assertNotIn("test-plan.json", release)
        self.assertNotIn("apply-report.json", release)
        self.assertIn("sign-test-evidence", release)
        self.assertIn("--archive scratch/release/release.tar", release)
        self.assertIn("TEAM_TEST_ENV_CONTENT: ${{ secrets.TEAM_TEST_ENV_CONTENT }}", release)
        self.assertIn("gen-runbook", release)
        self.assertIn("TEAM_FLOW_RUNNER: ${{ vars.TEAM_FLOW_RUNNER }}", release)
        self.assertIn("cancel-in-progress: false", integration)
        self.assertIn("concurrency:", release)
        self.assertIn("group: example-team-apex-test", release)
        self.assertIn("cancel-in-progress: false", release)
        self.assertNotIn("id-token: write", release)
        self.assertNotIn("production-secrets", release)
        self.assertIn("schema/v[0-9]+.[0-9]+.[0-9]+", release)
        self.assertIn("app/**/v[0-9]+.[0-9]+.[0-9]+", release)
        self.assertIn("--kind", release)

    def test_release_generates_and_signs_test_evidence_in_order(self):
        release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        ordered = [release.index(token) for token in ("run-release-test", "sign-test-evidence", "gen-runbook")]
        self.assertEqual(ordered, sorted(ordered))
        self.assertIn("TEAM_TEST_SIGNING_KEY_CONTENT: ${{ secrets.TEAM_TEST_SIGNING_KEY_CONTENT }}", release)
        self.assertIn("$RUNNER_TEMP/test-signing-key.pem", release)
        self.assertIn("umask 077", release)
        self.assertIn("chmod 600", release)
        self.assertIn("if: always()", release)
        self.assertIn("find \"$RUNNER_TEMP\" -maxdepth 1 -type f", release)
        self.assertIn("-name test-signing-key.pem", release)
        self.assertNotIn("TEAM_TEST_EVIDENCE", release)
        self.assertNotIn("TEAM_TEST_SIGNATURE", release)
        self.assertIn("$RUNNER_TEMP/test-evidence.json", release)
        self.assertIn("$RUNNER_TEMP/test-evidence.sig", release)
        self.assertIn("if-no-files-found: error", release)
        self.assertIn("test.env", release)
        self.assertIn("test-trust-key.pem", release)
        self.assertIn("production-history.json", release)
        self.assertIn("Remove protected handoff inputs", release)

    def test_release_tags_and_namespacing(self):
        from teamlib.release import ReleaseError, validate_release_identity
        commit_a = "a" * 40
        commit_b = "b" * 40
        # Valid schema tag
        validate_release_identity("schema/v1.0.0", "1.0.0", commit_a, {}, kind="schema")
        validate_release_identity("refs/tags/schema/v1.0.0", "1.0.0", commit_a, {}, kind="schema")
        # Valid app tag
        validate_release_identity("app/hr/v1.0.0", "1.0.0", commit_a, {}, kind="app", alias="hr")
        validate_release_identity("refs/tags/app/hr/v1.0.0", "1.0.0", commit_a, {}, kind="app", alias="hr")

        # Tag / version mismatch
        with self.assertRaises(ReleaseError):
            validate_release_identity("schema/v1.0.1", "1.0.0", commit_a, {}, kind="schema")
        with self.assertRaises(ReleaseError):
            validate_release_identity("app/hr/v1.0.1", "1.0.0", commit_a, {}, kind="app", alias="hr")

        # Tag / alias mismatch
        with self.assertRaises(ReleaseError):
            validate_release_identity("app/payroll/v1.0.0", "1.0.0", commit_a, {}, kind="app", alias="hr")

        # Tag / kind mismatch
        with self.assertRaises(ReleaseError):
            validate_release_identity("schema/v1.0.0", "1.0.0", commit_a, {}, kind="app", alias="hr")
        with self.assertRaises(ReleaseError):
            validate_release_identity("app/hr/v1.0.0", "1.0.0", commit_a, {}, kind="schema")

        # Namespacing in release records: schema/v1.0.0 and app/hr/v1.0.0 do not collide
        records = {
            "schema/v1.0.0": {"source_commit": commit_a, "archive_digest": "1" * 64},
            "app/hr/v1.0.0": {"source_commit": commit_b, "archive_digest": "2" * 64},
        }
        validate_release_identity("schema/v1.0.0", "1.0.0", commit_a, records, kind="schema")
        validate_release_identity("app/hr/v1.0.0", "1.0.0", commit_b, records, kind="app", alias="hr")
        with self.assertRaises(ReleaseError):
            validate_release_identity("schema/v1.0.0", "1.0.0", commit_b, records, kind="schema")
        with self.assertRaises(ReleaseError):
            validate_release_identity("app/hr/v1.0.0", "1.0.0", commit_a, records, kind="app", alias="hr")


class IndependentReleaseTwoAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-two-app-release-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Release Test"], check=True)

        # Two apps: HR and Payroll
        (self.repo / "apps" / "hr" / ".apex").mkdir(parents=True)
        (self.repo / "apps" / "hr" / ".apex" / "apexlang.json").write_text('{"format":"APEXLANG"}\n', encoding="utf-8")
        (self.repo / "apps" / "hr" / "application.apx").write_text('app "HR" { version: 1 }\n', encoding="utf-8")

        (self.repo / "apps" / "payroll" / ".apex").mkdir(parents=True)
        (self.repo / "apps" / "payroll" / ".apex" / "apexlang.json").write_text('{"format":"APEXLANG"}\n', encoding="utf-8")
        (self.repo / "apps" / "payroll" / "application.apx").write_text('app "Payroll" { version: 1 }\n', encoding="utf-8")

        # Shared migrations stream
        (self.repo / "migrations").mkdir()
        self.init_mid = "20260901T000000__alice__init"
        (self.repo / "migrations" / f"{self.init_mid}.sql").write_text(
            "-- migration-version: 1\n-- target: tables\n-- destructive: false\n\nCREATE TABLE EMPLOYEE (ID NUMBER);\n",
            encoding="utf-8",
        )
        (self.repo / "migrations" / f"{self.init_mid}.verify.sql").write_text("", encoding="utf-8")

        # App contexts
        (self.repo / "app_context" / "hr").mkdir(parents=True)
        (self.repo / "app_context" / "hr" / "release.json").write_text(
            json.dumps({"version": 1, "requires": [self.init_mid]}) + "\n",
            encoding="utf-8",
        )
        (self.repo / "app_context" / "payroll").mkdir(parents=True)
        (self.repo / "app_context" / "payroll" / "release.json").write_text(
            json.dumps({"version": 1, "requires": [self.init_mid]}) + "\n",
            encoding="utf-8",
        )

        # Targets
        (self.repo / "targets").mkdir()
        self.target_contract = {
            "version": 2,
            "role": "test",
            "environment": "test",
            "apps": {
                "hr": {"id": 101, "parsing_schema": "APP"},
                "payroll": {"id": 201, "parsing_schema": "APP"},
            },
        }
        (self.repo / "targets" / "test.json").write_text(
            json.dumps(self.target_contract, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.repo / "targets" / "masters.json").write_text(
            '{"version": 1, "masters": []}\n',
            encoding="utf-8",
        )

        # CI app-checks
        (self.repo / "ci" / "app-checks").mkdir(parents=True)
        (self.repo / "ci" / "app-checks" / "hr.json").write_text(
            json.dumps({"version": 1, "alias": "hr", "page_ids": [1], "checks": []}) + "\n",
            encoding="utf-8",
        )
        (self.repo / "ci" / "app-checks" / "payroll.json").write_text(
            json.dumps({"version": 1, "alias": "payroll", "page_ids": [1], "checks": []}) + "\n",
            encoding="utf-8",
        )

        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "Initial commit"], check=True)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_schema_release_does_not_force_sibling_app_deployment(self):
        # 1. Add optional column migration: EMPLOYEE.PRONOUNS
        pronouns_mid = "20260910T120000__bob__add-pronouns"
        (self.repo / "migrations" / f"{pronouns_mid}.sql").write_text(
            "-- migration-version: 1\n-- target: tables\n-- destructive: false\n\nALTER TABLE EMPLOYEE ADD (PRONOUNS VARCHAR2(50));\n",
            encoding="utf-8",
        )
        (self.repo / "migrations" / f"{pronouns_mid}.verify.sql").write_text("", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "Add pronouns migration"], check=True)

        # Build schema archive
        scratch = Path(self.temp.name) / "scratch"
        scratch.mkdir()
        schema_manifest = build_release(self.repo, "HEAD", "1.1.0", scratch / "schema", kind="schema")
        schema_archive = scratch / "schema" / "release.tar"
        verified_schema_manifest = verify_release(schema_archive)
        self.assertEqual(verified_schema_manifest.kind, "schema")
        self.assertIsNone(verified_schema_manifest.alias)
        self.assertEqual(verified_schema_manifest.app_tree_digests, {})
        self.assertEqual(len(verified_schema_manifest.migrations), 2)

        # 2. Update HR to v2 requiring the new migration. Payroll is untouched.
        (self.repo / "apps" / "hr" / "application.apx").write_text(
            'app "HR" { version: 2, pronouns: true }\n', encoding="utf-8"
        )
        (self.repo / "app_context" / "hr" / "release.json").write_text(
            json.dumps({"version": 1, "requires": [self.init_mid, pronouns_mid]}) + "\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "HR v2 requires pronouns"], check=True)

        # Build HR v2 app archive
        hr_manifest = build_release(self.repo, "HEAD", "2.0.0", scratch / "hr", kind="app", alias="hr")
        hr_archive = scratch / "hr" / "release.tar"
        verified_hr_manifest = verify_release(hr_archive)
        self.assertEqual(verified_hr_manifest.kind, "app")
        self.assertEqual(hr_manifest.alias, "hr")
        self.assertEqual(set(hr_manifest.app_tree_digests.keys()), {"hr"})
        self.assertNotIn("payroll", hr_manifest.app_tree_digests)
        self.assertEqual(hr_manifest.migrations, ())
        self.assertEqual(len(hr_manifest.required_migrations), 2)

        # Compute checksums
        init_checksum = bundle_checksum(self.repo / "migrations", self.init_mid)
        pronouns_checksum = bundle_checksum(self.repo / "migrations", pronouns_mid)
        history = {
            self.init_mid: {"status": "APPLIED", "checksum": init_checksum},
        }

        # 3. Refusal case: HR v2 refuses when pronouns migration has not been applied
        plan_hr_refused = plan_release(hr_archive, history, self.target_contract)
        hr_deploy_calls: list[str] = []
        payroll_deploy_calls: list[str] = []

        def deploy_spy(alias, tree, plan):
            if alias == "hr":
                hr_deploy_calls.append(alias)
            elif alias == "payroll":
                payroll_deploy_calls.append(alias)

        with self.assertRaisesRegex(ReleaseError, f"required migration unavailable: {pronouns_mid}"):
            apply_release(
                hr_archive,
                self.target_contract,
                plan_hr_refused,
                history=history,
                deploy_application=deploy_spy,
            )
        self.assertEqual(hr_deploy_calls, [])
        self.assertEqual(payroll_deploy_calls, [])

        # 4. Schema release applies once; zero apps deployed
        plan_schema = plan_release(schema_archive, history, self.target_contract)
        self.assertEqual(plan_schema.pending, (pronouns_mid,))
        applied_migrations: list[str] = []
        schema_deploy_calls: list[str] = []

        schema_report = apply_release(
            schema_archive,
            self.target_contract,
            plan_schema,
            history=history,
            apply_migrations=lambda pending, p: applied_migrations.extend(item["id"] for item in pending),
            deploy_application=lambda alias, tree, p: schema_deploy_calls.append(alias),
        )
        self.assertEqual(schema_report.status, "applied")
        self.assertEqual(applied_migrations, [pronouns_mid])
        self.assertEqual(schema_deploy_calls, [])

        # Destination history records the applied migration
        history[pronouns_mid] = {"status": "APPLIED", "checksum": pronouns_checksum}

        # 5. Apply HR v2: deploys HR; Payroll deploy spy is NEVER called; Payroll stays v1
        plan_hr_ready = plan_release(hr_archive, history, self.target_contract)
        self.assertEqual(plan_hr_ready.pending, ())
        deployed_trees: dict[str, dict[str, bytes]] = {}

        def deploy_spy_ready(alias, tree, plan):
            if alias == "hr":
                hr_deploy_calls.append(alias)
                deployed_trees[alias] = dict(tree)
            elif alias == "payroll":
                payroll_deploy_calls.append(alias)

        hr_report = apply_release(
            hr_archive,
            self.target_contract,
            plan_hr_ready,
            history=history,
            deploy_application=deploy_spy_ready,
        )
        self.assertEqual(hr_report.status, "applied")
        self.assertEqual(hr_deploy_calls, ["hr"])
        self.assertEqual(payroll_deploy_calls, [])
        self.assertEqual(
            deployed_trees["hr"]["application.apx"],
            b'app "HR" { version: 2, pronouns: true }\n',
        )
        # Payroll generation remains v1 in repository and was not touched
        self.assertEqual(
            (self.repo / "apps" / "payroll" / "application.apx").read_bytes(),
            b'app "Payroll" { version: 1 }\n',
        )

    def test_master_component_dependency_on_sibling_app(self):
        # HR declares a theme subscription to Payroll app (201)
        (self.repo / "apps" / "hr" / "application.apx").write_text(
            'app "HR" {\n'
            '  theme "PayrollTheme" {\n'
            '    subscription { master: @/201/payroll_theme }\n'
            '  }\n'
            '}\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "HR with master subscription"], check=True)

        scratch = Path(self.temp.name) / "scratch"
        scratch.mkdir(exist_ok=True)
        hr_manifest = build_release(self.repo, "HEAD", "2.0.0", scratch / "hr_master", kind="app", alias="hr")
        hr_archive = scratch / "hr_master" / "release.tar"
        hr_tree = {"application.apx": (self.repo / "apps" / "hr" / "application.apx").read_bytes()}

        hr_target = Target(
            project="team", role="test", environment="test", connection="fake",
            instance_id="INSTANCE", db_name="FREEPDB1", service="service",
            session_user="DEMO", current_schema="DEMO", alias="hr", workspace_id=90001,
            app_id=101, parsing_schema="APP", ownership_mode="shared", binding_digest="a" * 64,
        )

        # Case A: Absent master in targets/masters.json refuses HR
        empty_contract = {"version": 1, "masters": []}
        with self.assertRaisesRegex(MasterError, "master component is not contracted: 201/theme/payroll_theme"):
            validate_masters(hr_tree, hr_target, empty_contract)

        # Case B: Contracted in targets/masters.json, but wrong-target / unresolvable on live target refuses HR
        valid_contract = {
            "version": 1,
            "masters": [
                {
                    "app_id": 201,
                    "alias": "payroll",
                    "workspace_id": 90001,
                    "components": [{"type": "theme", "symbol": "payroll_theme"}],
                }
            ],
        }
        with self.assertRaisesRegex(MasterError, "target cannot resolve master component 201/payroll_theme"):
            validate_masters(
                hr_tree,
                hr_target,
                valid_contract,
                component_resolver=lambda ref, entry, target: False,
            )

        # Case C: Present qualified master on target allows HR without deploying Payroll
        report = validate_masters(
            hr_tree,
            hr_target,
            valid_contract,
            component_resolver=lambda ref, entry, target: True,
        )
        self.assertTrue(report.valid)

        # When deploying through apply_release, only HR is deployed; Payroll is never deployed
        init_checksum = bundle_checksum(self.repo / "migrations", self.init_mid)
        history = {self.init_mid: {"status": "APPLIED", "checksum": init_checksum}}
        plan = plan_release(hr_archive, history, self.target_contract)
        deployed: list[str] = []
        apply_release(
            hr_archive,
            self.target_contract,
            plan,
            history=history,
            deploy_application=lambda alias, tree, p: deployed.append(alias),
        )
        self.assertEqual(deployed, ["hr"])
        self.assertNotIn("payroll", deployed)


if __name__ == "__main__":
    unittest.main()
