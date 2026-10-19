"""CLI integration tests for optional baseline bundle generation."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4
from unittest.mock import patch

from scripts import baseline, directory_install, migration_runtime
from scripts.migration_attempts import write_attempt_event
from scripts.migration_manifest import MigrationManifestError, load_migration
from scripts.baseline import BaselineError
import fake_sqlcl
from fake_sqlcl import BASH


REPO_ROOT = Path(__file__).resolve().parents[1]


class BaselineBundleCliTests(unittest.TestCase):
    def wrapper_fixture(self, temporary: str, *, version: str = "26.2.0.0"):
        root = (Path(temporary) / "baseline project [bundle]").resolve()
        root.mkdir()
        shutil.copytree(REPO_ROOT / "scripts", root / "scripts")
        (root / "migrations").mkdir()
        (root / "baseline.json").write_text(json.dumps({
            "schemaVersion": 1,
            "schemas": ["APP_DEV"],
            "prefixes": ["APP_"],
            "excludedObjects": [],
            "referenceData": {"tables": []},
            "sequenceMappings": [],
            "grants": {"skipGrantees": [], "includeGrantees": [], "keepGrantOptions": True},
        }) + "\n", encoding="utf-8", newline="\n")
        env_file = root / ".env"
        env_file.write_text("\n".join((
            "PROJECT_NAME=baseline-fixture",
            "DEVELOPER_NAME=ALICE",
            "DB_ENVIRONMENT=development",
            "APEX_APP_ID=100",
            "TABLES_SCHEMA=APP_DEV",
            "TABLES_PREFIXES=*",
            "TABLES_SQLCL_CONNECTION=DEV",
            "TABLES_EXPECTED_USER=APP_DEV",
            "CODE_SCHEMA=APP_DEV",
            "CODE_PREFIXES=*",
            "CODE_SQLCL_CONNECTION=DEV",
            "CODE_EXPECTED_USER=APP_DEV",
            "APEX_PARSING_SCHEMA=APP_DEV",
            "APEX_SQLCL_CONNECTION=DEV",
            "APEX_EXPECTED_USER=APP_DEV",
            "STAGING_SCHEMA=APP_STAGE",
            "STAGING_SQLCL_CONNECTION=STAGE",
            "STAGING_EXPECTED_USER=APP_STAGE",
            "",
        )), encoding="utf-8", newline="\n")
        fake_bin = Path(temporary) / "fake sqlcl [bundle]"
        calls = Path(temporary) / "sqlcl calls.log"
        fake_sqlcl.install(fake_bin, """printf '%s\\n' \"$*\" >> \"$FAKE_SQLCL_CALLS\"
if [ \"${1:-}\" = \"-V\" ]; then
  printf 'SQLcl: Release %s Production\\n' \"$FAKE_SQLCL_VERSION\"
  exit 0
fi
printf 'unexpected fake SQLcl command\\n' >&2
exit 17
""")
        environment = fake_sqlcl.environment(
            fake_bin,
            base=os.environ,
            PROJECT_ENV_FILE=str(env_file),
            PYTHONPATH=str(root),
            FAKE_SQLCL_CALLS=str(calls),
            FAKE_SQLCL_VERSION=version,
            TEAM_BASH=shutil.which(BASH) or str(BASH),
        )
        return root, environment, calls

    def new_fixture(self):
        from tests.test_baseline import BaselineBuildIntegrationTests

        fixture = BaselineBuildIntegrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def run_build(self, fixture, arguments: list[str], *, capture_hook=None, snapshot_hook=None):
        def capture(target, environment, _sections, _run_dir, **_kwargs):
            catalog = fixture.source if environment == "dev" else fixture.target
            if capture_hook is not None:
                catalog = capture_hook(target, environment, catalog)
            return json.loads(json.dumps(catalog))

        def snapshot(target, _keys, _run_dir):
            selected = snapshot_hook(target) if snapshot_hook is not None else fixture.snapshot
            return None, selected

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(baseline, "ROOT", fixture.root),
            patch.object(baseline, "capture_environment_catalog", side_effect=capture),
            patch.object(baseline, "capture_inventory_snapshot_with_retries", side_effect=snapshot),
            patch.object(migration_runtime, "require_migration_sqlcl_version"),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = baseline.main(arguments, environ=fixture.values)
        return result, stdout.getvalue(), stderr.getvalue()

    def migration_folders(self, fixture, schema: str = "APP_STAGE") -> list[Path]:
        parent = fixture.root / "migrations" / schema
        if not parent.exists():
            return []
        return sorted(path for path in parent.iterdir() if path.is_dir())

    def folder_bytes(self, folder: Path) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in sorted(folder.iterdir()) if path.is_file()}

    def example_payload(self, sql: str = "CREATE TABLE T (ID NUMBER);\n", readme: str = "# Example") -> dict[str, bytes]:
        return baseline._migration_bytes(
            {"001-code-example.sql": sql},
            {"preconditions": [], "postconditions": [{"id": "ready", "sql": "SELECT 1 FROM DUAL", "expected": 1}]},
            readme,
        )

    def add_attempt_chain_and_receipt(self, fixture, folder: Path) -> dict[str, bytes]:
        migration = load_migration(fixture.root, folder.relative_to(fixture.root).as_posix())
        sql_files = [item for item in migration.files]
        recorded = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        pending_id = str(uuid4())
        attempt_id = str(uuid4())
        common = {
            "schemaVersion": 1,
            "attemptId": attempt_id,
            "runId": "migration-attempt-baseline-test",
            "recordedAt": recorded,
            "migrationPath": folder.relative_to(fixture.root).as_posix(),
            "environment": "staging",
            "target": {
                "environment": "staging",
                "connection": "STAGE",
                "expected_user": "APP_STAGE",
                "current_schema": "APP_STAGE",
            },
            "identity": None,
            "files": [
                {"name": item.name, "sequence": item.sequence, "sha256": item.sha256}
                for item in sql_files
            ],
            "checksSha256": migration.checks_sha256,
            "payloadDigest": migration.payload_digest,
            "writeAttempted": True,
            "evidence": [],
        }
        pending = {
            **common,
            "eventId": pending_id,
            "predecessorEventId": None,
            "state": "pending",
        }
        write_attempt_event(folder, pending)
        verified = {
            **common,
            "eventId": str(uuid4()),
            "predecessorEventId": pending_id,
            "state": "verified",
        }
        write_attempt_event(folder, verified)
        (folder / "status.staging.json").write_text(
            '{"schemaVersion":1,"state":"verified","environment":"staging"}\n',
            encoding="utf-8",
        )
        return self.folder_bytes(folder)

    def prepare_reference_data(self, fixture, *, core_equal: bool = False):
        config = {
            "schemaVersion": 1,
            "schemas": ["APP_DEV"],
            "prefixes": ["APP_", "PKG_", "V_"],
            "excludedObjects": [],
            "referenceData": {"tables": [
                {"name": "APP_CANCEL_REASON", "excludeColumns": ["API_TOKEN"], "keyColumns": ["REASON_CODE"], "labelColumns": ["REASON_LABEL"], "identity": {"column": "REASON_ID", "generationType": "ALWAYS"}, "rowLimit": 100},
                {"name": "APP_CANCEL_POLICY", "excludeColumns": ["API_TOKEN", "REFRESH_TOKEN"], "keyColumns": ["POLICY_CODE"], "labelColumns": ["POLICY_NAME"], "identity": {"column": "POLICY_ID", "generationType": "BY DEFAULT"}, "rowLimit": 100},
                {"name": "APP_ALWAYS_LOOKUP", "excludeColumns": [], "keyColumns": ["LOOKUP_CODE"], "labelColumns": ["LOOKUP_LABEL"], "identity": {"column": "LOOKUP_ID", "generationType": "ALWAYS"}, "rowLimit": 100},
            ]},
            "sequenceMappings": [],
            "grants": {"skipGrantees": [], "includeGrantees": [], "keepGrantOptions": True},
        }
        (fixture.root / "baseline.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
        source_data = json.loads((fixture.fixtures / "reference_data_source.json").read_text(encoding="utf-8"))
        target_data = json.loads((fixture.fixtures / "reference_data_target.json").read_text(encoding="utf-8"))
        constraints = json.loads((fixture.fixtures / "reference_data_constraints.json").read_text(encoding="utf-8"))
        source_catalog = copy.deepcopy(fixture.source)
        target_catalog = copy.deepcopy(fixture.target)
        if core_equal:
            target_catalog["sections"] = copy.deepcopy(source_catalog["sections"])
            for grant in target_catalog["sections"].get("object-grants", []):
                grant["owner"] = "APP_STAGE"
            for view in target_catalog["sections"].get("baseline-views", []):
                view["ddl"] = view["ddl"].replace('"APP_DEV".', '"APP_STAGE".')
        for catalog in (source_catalog, target_catalog):
            catalog["sections"]["tables"].extend({"name": name} for name in target_data)
            catalog["sections"]["constraints"].extend(copy.deepcopy(constraints))
        target_catalog["sections"]["baseline-data"] = list(target_data.values())
        target_catalog["coverage"]["sections"]["baseline-data"] = {"complete": True, "pages": [3]}
        data_root = fixture.root / "scratch" / "baseline" / "dev" / "data" / "APP_DEV"
        data_root.mkdir(parents=True)
        for name, table in source_data.items():
            (data_root / f"{name}.json").write_text(json.dumps(table) + "\n", encoding="utf-8")
        manifest_tables = []
        for name, table in source_data.items():
            path = data_root / f"{name}.json"
            contents = path.read_bytes()
            manifest_tables.append({"name": name, "file": path.name, "sha256": hashlib.sha256(contents).hexdigest(), "rowCount": table["rowCount"]})
        (data_root / "manifest.json").write_text(json.dumps({
            "schemaVersion": baseline.DATA_EXPORT_MANIFEST_VERSION,
            "environment": "dev", "schema": "APP_DEV", "sourceIdentity": fixture.source["identity"],
            "tableCount": len(manifest_tables), "rowCount": sum(item["rowCount"] for item in manifest_tables),
            "tables": manifest_tables, "complete": True,
        }) + "\n", encoding="utf-8")

        def capture_hook(_target, environment, _catalog):
            return source_catalog if environment == "dev" else target_catalog

        return capture_hook

    def test_bundle_reduces_three_folders_to_one(self) -> None:
        granular = self.new_fixture()
        code, output, error = self.run_build(granular, ["build", "--to", "staging"])
        self.assertEqual(code, 0, output + error)
        granular_folders = self.migration_folders(granular)
        self.assertEqual(len(granular_folders), 3, output)
        expected_sql: list[bytes] = []
        expected_checks = {"preconditions": [], "postconditions": []}
        for kind in ("structure", "grants", "code"):
            folder = next(path for path in granular_folders if f"_baseline-{kind}-r" in path.name)
            expected_sql.extend(path.read_bytes() for path in sorted(folder.glob("*.sql")))
            checks = json.loads((folder / "checks.json").read_text(encoding="utf-8"))
            for phase in expected_checks:
                expected_checks[phase].extend(
                    {**check, "id": f"{kind}-{check['id']}"} for check in checks[phase]
                )

        bundled = self.new_fixture()
        code, output, error = self.run_build(
            bundled, ["build", "--to", "staging", "--bundle", "customer-status"]
        )
        self.assertEqual(code, 0, output + error)
        folders = self.migration_folders(bundled)
        self.assertEqual(len(folders), 1, output)
        folder = folders[0]
        self.assertIn("_baseline-bundle-customer-status-r001", folder.name)
        migration = load_migration(bundled.root, folder.relative_to(bundled.root).as_posix())
        self.assertTrue(migration.files)
        self.assertEqual([path.read_bytes() for path in sorted(folder.glob("*.sql"))], expected_sql)
        self.assertEqual(json.loads((folder / "checks.json").read_text(encoding="utf-8")), {
            "schemaVersion": 1,
            **expected_checks,
        })
        filenames = [path.name for path in sorted(folder.glob("*.sql"))]
        self.assertTrue(all(name.startswith(f"{index:03d}-") for index, name in enumerate(filenames, 1)))
        readme = (folder / "README.md").read_text(encoding="utf-8")
        self.assertIn("001-structure-", readme)
        self.assertIn("002-grants-", readme)
        self.assertIn("003-code-", readme)

    def test_bundle_with_data_keeps_two_folders_and_data_only_skips_empty_bundle(self) -> None:
        with_core_changes = self.new_fixture()
        capture_hook = self.prepare_reference_data(with_core_changes)
        code, output, error = self.run_build(
            with_core_changes,
            ["build", "--to", "staging", "--data", "--bundle", "customer-status"],
            capture_hook=capture_hook,
        )
        self.assertEqual(code, 0, output + error)
        folders = self.migration_folders(with_core_changes)
        self.assertEqual(len(folders), 2, output)
        bundle_folder = next(path for path in folders if "_baseline-bundle-" in path.name)
        data_folder = next(path for path in folders if "_baseline-data-r" in path.name)
        for folder in folders:
            self.assertTrue(load_migration(with_core_changes.root, folder.relative_to(with_core_changes.root).as_posix()).files)
        bundle_readme = (bundle_folder / "README.md").read_text(encoding="utf-8")
        data_readme = (data_folder / "README.md").read_text(encoding="utf-8")
        self.assertIn("baseline-data` folder is installed separately after this DDL bundle", bundle_readme)
        self.assertIn("--rehearse", data_readme)
        self.assertIn('"REASON_ID"', "\n".join(path.read_text(encoding="utf-8") for path in data_folder.glob("*.sql")))

        data_only = self.new_fixture()
        capture_hook = self.prepare_reference_data(data_only, core_equal=True)
        code, output, error = self.run_build(
            data_only,
            ["build", "--to", "staging", "--data", "--bundle", "customer-status"],
            capture_hook=capture_hook,
        )
        self.assertEqual(code, 0, output + error)
        folders = self.migration_folders(data_only)
        self.assertEqual(len(folders), 1, output)
        self.assertIn("_baseline-data-r001", folders[0].name)
        self.assertTrue(load_migration(data_only.root, folders[0].relative_to(data_only.root).as_posix()).files)

    def test_post_install_loader_failure_reports_every_installed_folder(self) -> None:
        fixture = self.new_fixture()
        capture_hook = self.prepare_reference_data(fixture)
        unrelated_stage = fixture.root / "scratch" / "baseline-stage-other-operation"
        unrelated_stage.mkdir()
        (unrelated_stage / "sentinel.txt").write_text("keep", encoding="utf-8")

        post_install_bytes: dict[str, dict[str, bytes]] = {}
        post_install_calls = 0

        def validate_then_fail_on_data(root: Path, relative: str):
            nonlocal post_install_calls
            migration = load_migration(root, relative)
            folder = Path(root) / relative
            post_install_bytes[relative] = self.folder_bytes(folder)
            post_install_calls += 1
            if relative.split("/")[-1].split("_", 1)[1].startswith("baseline-data-"):
                raise MigrationManifestError("controlled post-install loader failure")
            return migration

        with patch.object(baseline, "load_migration", side_effect=validate_then_fail_on_data):
            code, output, error = self.run_build(
                fixture,
                ["build", "--to", "staging", "--data"],
                capture_hook=capture_hook,
            )

        self.assertEqual(code, 2, output + error)
        self.assertEqual(post_install_calls, 4)
        folders = self.migration_folders(fixture)
        self.assertEqual(len(folders), 4, output + error)
        self.assertEqual(set(post_install_bytes), {
            folder.relative_to(fixture.root).as_posix() for folder in folders
        })
        for folder in folders:
            relative = folder.relative_to(fixture.root).as_posix()
            self.assertEqual(self.folder_bytes(folder), post_install_bytes[relative])
            self.assertTrue(load_migration(fixture.root, relative).files)
            self.assertIn(relative, error)
        self.assertIn("post-install validation failed", error)
        self.assertIn("controlled post-install loader failure", error)
        self.assertNotIn("atomic", error.lower())
        self.assertEqual(
            sorted(path.name for path in (fixture.root / "scratch").glob("baseline-stage-*")),
            [unrelated_stage.name],
        )
        self.assertEqual((unrelated_stage / "sentinel.txt").read_text(encoding="utf-8"), "keep")

    def test_bundle_multi_schema_never_crosses_owner(self) -> None:
        from scripts.schema_catalog import ObjectDefinition, ObjectKey, SchemaSnapshot

        fixture = self.new_fixture()
        config = json.loads((fixture.root / "baseline.json").read_text(encoding="utf-8"))
        config["schemas"] = ["APP_DEV", "BILL_DEV"]
        (fixture.root / "baseline.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
        fixture.values.update({
            "CODE_SCHEMA": "APP_DEV,BILL_DEV",
            "CODE_EXPECTED_USER": "APP_DEV,BILL_DEV",
            "CODE_SQLCL_CONNECTION": "DEV,DEV2",
            "STAGING_SCHEMA": "APP_DEV,BILL_DEV",
            "STAGING_EXPECTED_USER": "APP_DEV,BILL_DEV",
            "STAGING_SQLCL_CONNECTION": "STAGE,STAGE2",
        })

        def capture_hook(target, _environment, catalog):
            result = copy.deepcopy(catalog)
            result["identity"]["current_schema"] = target.schema
            result["identity"]["session_user"] = target.expected_user
            for grant in result["sections"].get("object-grants", []):
                grant["owner"] = target.schema
            return result

        def snapshot_hook(target):
            definitions = {}
            for key, definition in fixture.snapshot.objects.items():
                mapped_key = ObjectKey(target.schema, key.name, key.object_type, key.subobject_name)
                dependents = tuple(
                    ObjectKey(target.schema, item.name, item.object_type, item.subobject_name)
                    for item in definition.dependents
                )
                definitions[mapped_key] = ObjectDefinition(
                    mapped_key,
                    definition.attributes,
                    definition.raw_ddl.replace("APP_DEV", target.schema),
                    dependents,
                    definition.valid,
                )
            return SchemaSnapshot(
                {**fixture.snapshot.identity, "current_schema": target.schema, "session_user": target.expected_user},
                fixture.snapshot.inventory,
                definitions,
                fixture.snapshot.coverage,
                fixture.snapshot.started_at,
                fixture.snapshot.completed_at,
            )

        code, output, error = self.run_build(
            fixture,
            ["build", "--to", "staging", "--bundle", "customer-status"],
            capture_hook=capture_hook,
            snapshot_hook=snapshot_hook,
        )
        self.assertEqual(code, 0, output + error)
        for schema in ("APP_DEV", "BILL_DEV"):
            folders = self.migration_folders(fixture, schema)
            self.assertEqual(len(folders), 1, output)
            folder = folders[0]
            self.assertIn("_baseline-bundle-customer-status-r001", folder.name)
            self.assertTrue(load_migration(fixture.root, folder.relative_to(fixture.root).as_posix()).files)
            readme = (folder / "README.md").read_text(encoding="utf-8")
            self.assertIn(f"Target: staging / {schema}", readme)
            checks = json.loads((folder / "checks.json").read_text(encoding="utf-8"))
            self.assertTrue(any(schema in check["sql"] for check in checks["postconditions"]))

    def test_bundle_repeat_revision_and_attempt_evidence_are_immutable(self) -> None:
        fixture = self.new_fixture()
        arguments = ["build", "--to", "staging", "--bundle", "customer-status"]

        code, output, error = self.run_build(fixture, arguments)
        self.assertEqual(code, 0, output + error)
        first = self.migration_folders(fixture)[0]
        first_bytes = self.folder_bytes(first)

        code, output, error = self.run_build(fixture, arguments)
        self.assertEqual(code, 0, output + error)
        self.assertIn("Unchanged migration", output)
        self.assertEqual(self.migration_folders(fixture), [first])
        self.assertEqual(self.folder_bytes(first), first_bytes)

        fixture.source["sections"]["baseline-source"][2]["text"] = "  x := 2-\n"
        code, output, error = self.run_build(fixture, arguments)
        self.assertEqual(code, 0, output + error)
        folders = self.migration_folders(fixture)
        self.assertEqual(len(folders), 2, output)
        second = folders[-1]
        self.assertIn("-r002", second.name)
        second_evidence_bytes = self.add_attempt_chain_and_receipt(fixture, second)

        code, output, error = self.run_build(fixture, arguments)
        self.assertEqual(code, 0, output + error)
        self.assertIn("Skipped receipted or attempted migration", output)
        self.assertEqual(self.migration_folders(fixture), folders)
        self.assertEqual(self.folder_bytes(second), second_evidence_bytes)

        fixture.source["sections"]["baseline-source"][2]["text"] = "  x := 3-\n"
        code, output, error = self.run_build(fixture, arguments)
        self.assertEqual(code, 0, output + error)
        folders = self.migration_folders(fixture)
        self.assertEqual(len(folders), 3, output)
        self.assertIn("-r003", folders[-1].name)
        self.assertEqual(self.folder_bytes(second), second_evidence_bytes)
        self.assertEqual(load_migration(fixture.root, folders[-1].relative_to(fixture.root).as_posix()).revision, 3)

    def test_bundle_refuses_unknown_evidence_gaps_duplicates_and_flat_family(self) -> None:
        fixture = self.new_fixture()
        arguments = ["build", "--to", "staging", "--bundle", "customer-status"]
        code, output, error = self.run_build(fixture, arguments)
        self.assertEqual(code, 0, output + error)
        first = self.migration_folders(fixture)[0]
        (first / "status.staging.json").write_text("{bad evidence\n", encoding="utf-8")
        frozen_bytes = self.folder_bytes(first)
        fixture.source["sections"]["baseline-source"][2]["text"] = "  x := 2-\n"
        code, output, error = self.run_build(fixture, arguments)
        self.assertEqual(code, 2, output + error)
        self.assertIn("cannot determine whether existing migration is immutable", error)
        self.assertEqual(self.folder_bytes(first), frozen_bytes)
        self.assertEqual(len(self.migration_folders(fixture)), 1)

        family = "baseline-bundle-customer-status"
        schema_parent = fixture.root / "migrations" / "BILL_DEV"
        schema_parent.mkdir()
        (schema_parent / f"2026-10-12_{family}-r001").mkdir()
        (schema_parent / f"2026-10-13_{family}-r003").mkdir()
        with self.assertRaisesRegex(BaselineError, "history .* incomplete"):
            baseline._build_migration_folder(
                fixture.root,
                "BILL_DEV",
                family,
                self.example_payload(),
            )

        duplicate_fixture = self.new_fixture()
        duplicate_parent = duplicate_fixture.root / "migrations" / "APP_STAGE"
        duplicate_parent.mkdir()
        (duplicate_parent / f"2026-10-12_{family}-r001").mkdir()
        (duplicate_parent / f"2026-10-13_{family}-r001").mkdir()
        with self.assertRaisesRegex(BaselineError, "duplicate revision r001"):
            baseline._build_migration_folder(
                duplicate_fixture.root,
                "APP_STAGE",
                family,
                self.example_payload(),
            )

        flat_fixture = self.new_fixture()
        flat_folder = flat_fixture.root / "migrations" / f"2026-10-13_{family}-r001"
        flat_folder.mkdir()
        for filename, content in self.example_payload().items():
            (flat_folder / filename).write_bytes(content)
        with self.assertRaisesRegex(MigrationManifestError, "flat migrations/ layout"):
            baseline._build_migration_folder(
                flat_fixture.root,
                "APP_STAGE",
                family,
                self.example_payload(),
            )
        self.assertFalse((flat_fixture.root / "migrations" / "APP_STAGE" / f"2026-10-13_{family}-r001").exists())
        self.assertEqual(list((flat_fixture.root / "scratch").iterdir()), [])

    def test_generation_failures_clean_only_the_owned_stage_and_preserve_old_evidence(self) -> None:
        fixture = self.new_fixture()
        family = "baseline-bundle-customer-status"
        payload = self.example_payload()
        first = baseline._build_migration_folder(fixture.root, "APP_STAGE", family, payload)
        assert first is not None
        evidence_bytes = self.add_attempt_chain_and_receipt(fixture, first)
        second_payload = self.example_payload("CREATE TABLE T (ID NUMBER, NAME VARCHAR2(20));\n", "# Changed example")
        validate = baseline.validate_staged_migration
        captured_stages: list[Path] = []

        def validate_and_capture(root, stage, relative):
            captured_stages.append(stage)
            self.assertEqual(stage.parent, fixture.root / "scratch")
            self.assertFalse(stage.is_relative_to(fixture.root / "migrations"))
            self.assertEqual(stage.stat().st_dev, (fixture.root / "migrations" / "APP_STAGE").stat().st_dev)
            if os.name != "nt":
                self.assertEqual(stage.stat().st_mode & 0o777, 0o700)
                self.assertEqual((stage / "001-code-example.sql").stat().st_mode & 0o777, 0o600)
            return validate(root, stage, relative)

        with patch.object(baseline, "validate_staged_migration", side_effect=MigrationManifestError("validation sentinel")):
            with self.assertRaisesRegex(MigrationManifestError, "validation sentinel"):
                baseline._build_migration_folder(fixture.root, "APP_STAGE", family, second_payload)
        self.assertEqual(list((fixture.root / "scratch").iterdir()), [])

        with patch.object(baseline, "_write_migration_stage", side_effect=OSError("stage write sentinel")):
            with self.assertRaisesRegex(OSError, "stage write sentinel"):
                baseline._build_migration_folder(fixture.root, "APP_STAGE", family, second_payload)
        self.assertEqual(list((fixture.root / "scratch").iterdir()), [])

        with (
            patch.object(baseline, "validate_staged_migration", side_effect=validate_and_capture),
            patch.object(directory_install, "install_directory_exclusive", side_effect=directory_install.DirectoryInstallError("install sentinel")) as install,
        ):
            with self.assertRaisesRegex(directory_install.DirectoryInstallError, "install sentinel"):
                baseline._build_migration_folder(fixture.root, "APP_STAGE", family, second_payload)
        install.assert_called_once()
        self.assertEqual(len(captured_stages), 1)
        self.assertFalse(captured_stages[0].exists())
        self.assertEqual(list((fixture.root / "scratch").iterdir()), [])
        self.assertEqual(self.folder_bytes(first), evidence_bytes)
        self.assertFalse((first.parent / f"{first.name.rsplit('-r', 1)[0]}-r002").exists())

    def test_windows_private_stage_acl_removes_inheritance_and_grants_only_the_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "stage"
            stage.mkdir()
            sid = "S-1-5-21-1-2-3-1001"
            result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with (
                patch.object(baseline, "_windows_current_user_sid", return_value=sid),
                patch.object(baseline, "_windows_system_tool", return_value=Path("icacls.exe")),
                patch.object(baseline.subprocess, "run", return_value=result) as run,
            ):
                baseline._secure_windows_private_stage(stage)

            self.assertEqual(
                run.call_args.args[0],
                [
                    "icacls.exe",
                    str(stage),
                    "/inheritancelevel:r",
                    "/grant:r",
                    f"*{sid}:(OI)(CI)F",
                ],
            )
            self.assertEqual(
                run.call_args.kwargs,
                {"capture_output": True, "text": True, "errors": "replace", "check": False},
            )

    def test_windows_private_stage_acl_failure_refuses_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary) / "stage"
            stage.mkdir()
            result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="denied")
            with (
                patch.object(baseline, "_windows_current_user_sid", return_value="S-1-5-21-1-2-3-1001"),
                patch.object(baseline, "_windows_system_tool", return_value=Path("icacls.exe")),
                patch.object(baseline.subprocess, "run", return_value=result),
            ):
                with self.assertRaisesRegex(BaselineError, "could not secure private baseline staging directory"):
                    baseline._secure_windows_private_stage(stage)

    def test_bundle_refuses_revision_exhaustion_before_staging(self) -> None:
        fixture = self.new_fixture()
        family = "baseline-bundle-customer-status"
        parent = fixture.root / "migrations" / "APP_STAGE"
        parent.mkdir()
        for revision in range(1, 1000):
            (parent / f"2026-10-13_{family}-r{revision:03d}").mkdir()

        with (
            patch.object(baseline, "inspect_migration_lock", return_value=type("Lock", (), {"status": "unlocked", "states": ()})()),
            patch.object(baseline, "load_migration"),
        ):
            with self.assertRaisesRegex(BaselineError, "revisions are exhausted"):
                baseline._build_migration_folder(
                    fixture.root,
                    "APP_STAGE",
                    family,
                    self.example_payload(),
                )
        self.assertFalse((fixture.root / "scratch").exists())

    def test_bash_and_powershell_route_bundle_arguments_through_fake_sqlcl(self) -> None:
        pwsh = shutil.which("pwsh")
        entries = [("team.sh", [BASH])]
        if pwsh is not None:
            entries.append(("team.ps1", [pwsh, "-NoProfile", "-File"]))

        with tempfile.TemporaryDirectory() as temporary:
            root, environment, calls = self.wrapper_fixture(temporary)
            usage_entries = []
            for entry, launcher in entries:
                usage = subprocess.run(
                    [*launcher, str(root / "scripts" / entry), "--help"],
                    cwd=temporary,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(usage.returncode, 0, usage.stdout + usage.stderr)
                usage_entries.append(usage.stdout.replace("team.sh", "team.ps1"))
                self.assertIn("--bundle <name>", usage.stdout)
            if len(usage_entries) == 2:
                self.assertEqual(usage_entries[0].splitlines(), usage_entries[1].splitlines())

            for entry, launcher in entries:
                with self.subTest(entry=entry):
                    result = subprocess.run(
                        [*launcher, str(root / "scripts" / entry), "baseline", "build", "--from", "dev", "--to", "staging", "--bundle", "customer-status"],
                        cwd=temporary,
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("SQLcl 26.3.0.0 or newer is required", result.stderr)
                    self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["-V"])
                    calls.write_text("", encoding="utf-8")

    def test_baseline_subcommand_help_is_offline_through_both_wrappers(self) -> None:
        pwsh = shutil.which("pwsh")
        entries = [("team.sh", [BASH])]
        if pwsh is not None:
            entries.append(("team.ps1", [pwsh, "-NoProfile", "-File"]))

        with tempfile.TemporaryDirectory() as temporary:
            root, environment, calls = self.wrapper_fixture(temporary)
            environment["PROJECT_ENV_FILE"] = str(Path(temporary) / "missing.env")
            commands = ("export-source", "export-grants", "export-data", "build", "filter-ords")
            for entry, launcher in entries:
                for command in commands:
                    with self.subTest(entry=entry, command=command):
                        result = subprocess.run(
                            [*launcher, str(root / "scripts" / entry), "baseline", command, "--help"],
                            cwd=temporary,
                            env=environment,
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertIn(f"usage: scripts/team.sh baseline {command}", result.stdout)
            self.assertFalse(calls.exists())

    def test_invalid_bundle_name_is_rejected_before_sqlcl_or_capture(self) -> None:
        fixture = self.new_fixture()
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(baseline, "ROOT", fixture.root),
            patch.object(baseline, "capture_environment_catalog") as capture,
            patch.object(migration_runtime, "require_migration_sqlcl_version") as version_check,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            result = baseline.main(
                ["build", "--to", "staging", "--bundle", "Customer-status"],
                environ=fixture.values,
            )

        self.assertEqual(result, 2, stdout.getvalue() + stderr.getvalue())
        self.assertIn("bundle name", stderr.getvalue())
        version_check.assert_not_called()
        capture.assert_not_called()

    def test_live_baseline_operations_check_the_sqlcl_floor_before_capture(self) -> None:
        fixture = self.new_fixture()
        for arguments in (
            ["export-source", "--from", "dev"],
            ["export-grants", "--from", "dev"],
            ["export-data", "--from", "dev"],
            ["build", "--to", "staging"],
        ):
            with self.subTest(command=arguments[0]):
                with (
                    patch.object(baseline, "ROOT", fixture.root),
                    patch.object(baseline, "capture_environment_catalog") as capture,
                    patch.object(
                        migration_runtime,
                        "require_migration_sqlcl_version",
                        side_effect=migration_runtime.MigrationRuntimeError("floor sentinel"),
                    ) as version_check,
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    result = baseline.main(arguments, environ=fixture.values)
                self.assertEqual(result, 2)
                version_check.assert_called_once_with()
                capture.assert_not_called()

    def test_offline_baseline_help_and_filter_skip_the_sqlcl_floor(self) -> None:
        fixture = self.new_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "filtered.sql"
            with patch.object(migration_runtime, "require_migration_sqlcl_version") as version_check:
                help_result = baseline.main(["build", "--help"])
                filter_result = baseline.main([
                    "filter-ords", "--exclude-module", "legacy.api",
                    "--input", str(fixture.fixtures / "ords_modules.sql"),
                    "--output", str(output),
                ])

        self.assertEqual(help_result, 0)
        self.assertEqual(filter_result, 0)
        version_check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
