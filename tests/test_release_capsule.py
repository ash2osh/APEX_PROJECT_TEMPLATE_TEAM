from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


CHECKS = {
    "schemaVersion": 1,
    "preconditions": [],
    "postconditions": [{"id": "ready", "sql": "SELECT 1 FROM dual", "expected": 1}],
}


class ReleaseCapsuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        actual_root = Path(__file__).resolve().parents[1]
        shutil.copytree(actual_root / "scripts", self.root / "scripts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(actual_root / "template-manifest.json", self.root / "template-manifest.json")
        try:
            from scripts import feature_manifest, release_capsule, rollout
        except ImportError as error:
            self.fail(f"release capsule support is not implemented: {error}")
        self.feature_manifest = feature_manifest
        self.release_capsule = release_capsule
        self.rollout = rollout
        self.manifest_path = self.root / "releases" / "current.json"
        self.create_feature()
        self.init_git()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_feature(self, *, source: str = "CREATE TABLE APP_CODE.DISPATCH (ID NUMBER);\n", second: bool = False) -> None:
        feature_dir = self.root / "features" / "dispatch" / "APP_CODE"
        feature_dir.mkdir(parents=True, exist_ok=True)
        (feature_dir / "ddl.sql").write_text(source, encoding="utf-8", newline="\n")
        (feature_dir / "checks.json").write_text(json.dumps(CHECKS) + "\n", encoding="utf-8")
        inputs = ["features/dispatch/APP_CODE/ddl.sql"]
        steps = [{"type": "feature-migrate", "schema": "APP_CODE", "inputs": inputs}]
        if second:
            (feature_dir / "second.sql").write_text("CREATE TABLE APP_CODE.SECONDARY (ID NUMBER);\n", encoding="utf-8")
            steps[0]["inputs"].append("features/dispatch/APP_CODE/second.sql")
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps({"schemaVersion": 2, "steps": steps}, indent=2) + "\n", encoding="utf-8")

    def init_git(self) -> None:
        for args in (
            ["git", "init", "-q"],
            ["git", "config", "user.name", "Capsule Tests"],
            ["git", "config", "user.email", "capsules@example.invalid"],
        ):
            subprocess.run(args, cwd=self.root, check=True, capture_output=True)
        self.commit()

    def commit(self, message: str = "fixture") -> str:
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", message], cwd=self.root, check=True, capture_output=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, check=True, capture_output=True, text=True).stdout.strip()

    def checkpoints(self):
        manifest = self.rollout.load_manifest(self.manifest_path, self.root)
        return tuple(
            self.feature_manifest.load_feature_checkpoint(self.root, step)
            for step in manifest.steps if step["type"] == "feature-migrate"
        )

    def build(self, shared_inputs: dict[str, bytes] | None = None):
        return self.release_capsule.build_capsule(
            self.root, self.manifest_path, self.checkpoints(), shared_inputs or {},
        )

    def set_stored_source_release(self, *, duplicate: bool) -> list[dict]:
        first_dir = self.root / "features" / "dispatch" / "APP_CODE" / "packages"
        first_dir.mkdir(parents=True, exist_ok=True)
        first = first_dir / "shared.pks.sql"
        first.write_text(
            "CREATE OR REPLACE PACKAGE APP_CODE.SHARED_PKG IS PROCEDURE RUN; END SHARED_PKG;\n/\n",
            encoding="utf-8",
        )
        second_dir = self.root / "features" / "other" / "APP_CODE" / "packages"
        second_dir.mkdir(parents=True)
        second = second_dir / "shared.pks.sql"
        second_source = first.read_text(encoding="utf-8") if duplicate else (
            "CREATE OR REPLACE PACKAGE APP_CODE.OTHER_PKG IS PROCEDURE RUN; END OTHER_PKG;\n/\n"
        )
        second.write_text(second_source, encoding="utf-8")
        (self.root / "features" / "other" / "APP_CODE" / "checks.json").write_text(
            json.dumps(CHECKS) + "\n", encoding="utf-8",
        )
        steps = [
            {
                "type": "feature-migrate", "schema": "APP_CODE",
                "inputs": ["features/dispatch/APP_CODE/packages/shared.pks.sql"],
            },
            {
                "type": "feature-migrate", "schema": "APP_CODE",
                "inputs": ["features/other/APP_CODE/packages/shared.pks.sql"],
            },
        ]
        self.manifest_path.write_text(
            json.dumps({"schemaVersion": 2, "steps": steps}, indent=2) + "\n",
            encoding="utf-8",
        )
        self.commit("select stored source checkpoints")
        return steps

    def test_identical_committed_inputs_build_and_load_one_exact_payload(self) -> None:
        first = self.build()
        second = self.build()
        loaded = self.release_capsule.load_capsule(self.root, first.root)

        self.assertEqual(first.payload_digest, second.payload_digest)
        self.assertEqual(first.root, second.root)
        self.assertEqual(first.source_commit, subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root, check=True, capture_output=True, text=True,
        ).stdout.strip())
        self.assertEqual(loaded.payload_digest, first.payload_digest)
        self.assertEqual(loaded.engine_digest, first.engine_digest)
        self.assertEqual(loaded.checkpoints, first.checkpoints)
        self.assertTrue((first.root / "capsule.json").is_file())

    def test_dedicated_view_release_does_not_replay_earlier_column_or_grant(self) -> None:
        schema_path = self.root / "features/dispatch/APP_CODE"
        earlier = schema_path / "ddl.sql"
        earlier.write_text(
            "ALTER TABLE APP_CODE.DISPATCH ADD LABEL VARCHAR2(30);\n"
            "GRANT SELECT ON APP_CODE.DISPATCH TO REPORTING;\n"
            "CREATE OR REPLACE VIEW APP_CODE.DISPATCH_V AS SELECT ID FROM APP_CODE.DISPATCH;\n",
            encoding="utf-8",
        )
        self.commit("earlier whole feature with DDL, grant, and inline view")
        legacy = self.build()
        self.assertEqual(legacy.checkpoints[0].inputs[0].source, earlier.read_bytes())

        # A reviewed split leaves one owner while later manifests select only the changed view.
        earlier.write_text(
            "ALTER TABLE APP_CODE.DISPATCH ADD LABEL VARCHAR2(30);\n"
            "GRANT SELECT ON APP_CODE.DISPATCH TO REPORTING;\n",
            encoding="utf-8",
        )
        view = schema_path / "views/dispatch_v.sql"
        view.parent.mkdir()
        view.write_text(
            "CREATE OR REPLACE VIEW APP_CODE.DISPATCH_V AS SELECT ID, LABEL FROM APP_CODE.DISPATCH;\n",
            encoding="utf-8",
        )
        relative = view.relative_to(self.root).as_posix()
        self.manifest_path.write_text(json.dumps({
            "schemaVersion": 2, "steps": [{"type": "feature-migrate", "schema": "APP_CODE", "inputs": [relative]}],
        }) + "\n", encoding="utf-8")
        self.commit("select only the dedicated view")
        later = self.build()
        selected, = later.checkpoints[0].inputs
        self.assertEqual((selected.path, selected.source), (relative, view.read_bytes()))
        frozen = later.root / "payload" / earlier.relative_to(self.root)
        self.assertFalse(frozen.exists())

    def test_capsule_preserves_object_budget_for_stage_promotion_proof(self) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["databaseObjectBudgetSeconds"] = 90
        self.manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        self.commit("set object budget")

        capsule = self.build()

        self.assertEqual(getattr(capsule, "database_object_budget_seconds", None), 90)

    def test_capsule_construction_rejects_duplicate_stored_units_across_checkpoints(self) -> None:
        steps = self.set_stored_source_release(duplicate=True)
        checkpoints = tuple(self.feature_manifest.load_feature_checkpoint(self.root, step) for step in steps)
        raw = self.manifest_path.read_bytes()
        manifest = self.rollout.RolloutManifest(
            self.manifest_path, self.root, hashlib.sha256(raw).hexdigest(), tuple(steps), 2, 120,
        )
        with patch.object(self.release_capsule.rollout, "load_manifest", return_value=manifest):
            with self.assertRaisesRegex(self.release_capsule.ReleaseCapsuleError, "duplicate"):
                self.release_capsule.build_capsule(self.root, self.manifest_path, checkpoints, {})

    def test_capsule_loading_rejects_stored_unit_metadata_that_disagrees_with_frozen_source(self) -> None:
        self.set_stored_source_release(duplicate=False)
        capsule = self.build()
        document = json.loads((capsule.root / "capsule.json").read_text(encoding="utf-8"))
        first_unit = document["checkpoints"][0]["inputs"][0]["unit"]
        document["checkpoints"][1]["inputs"][0]["unit"] = first_unit
        document["payloadDefinition"]["checkpoints"] = document["checkpoints"]
        second_source = next(
            row for row in document["sourceInputs"]
            if row.get("step") == 2 and row.get("unit") is not None
        )
        second_source["unit"] = first_unit
        document["payloadDefinition"]["sourceInputs"] = document["sourceInputs"]
        digest = self.release_capsule._sha256(
            self.release_capsule._canonical_bytes(document["payloadDefinition"]),
        )
        document["payloadDigest"] = digest
        moved = capsule.root.with_name(f"{digest}-{document['sourceCommit'][:12]}")
        (capsule.root / "capsule.json").write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        capsule.root.rename(moved)

        with self.assertRaisesRegex(
            self.release_capsule.ReleaseCapsuleError,
            "identity.*frozen source|frozen source.*identity",
        ):
            self.release_capsule.load_capsule(self.root, moved)

    def test_capsule_loading_rejects_duplicate_stored_units_in_digest_valid_payload(self) -> None:
        self.set_stored_source_release(duplicate=False)
        capsule = self.build()
        document = json.loads((capsule.root / "capsule.json").read_text(encoding="utf-8"))
        first_record = next(row for row in document["sourceInputs"] if row.get("step") == 1 and row.get("unit"))
        second_record = next(row for row in document["sourceInputs"] if row.get("step") == 2 and row.get("unit"))
        first_source = capsule.root / first_record["sourcePath"]
        second_source = capsule.root / second_record["sourcePath"]
        first_driver = capsule.root / first_record["driverPath"]
        second_driver = capsule.root / second_record["driverPath"]
        source_bytes = first_source.read_bytes()
        driver_bytes = first_driver.read_bytes()
        second_source.write_bytes(source_bytes)
        second_driver.write_bytes(driver_bytes)
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        driver_hash = hashlib.sha256(driver_bytes).hexdigest()

        document["checkpoints"][1]["inputs"][0].update(
            sha256=source_hash, unit=first_record["unit"],
        )
        second_record.update(sourceSha256=source_hash, driverSha256=driver_hash, unit=first_record["unit"])
        mapping = next(
            row for row in document["payloadDefinition"]["sourceToDriver"]
            if row["driverPath"] == second_record["driverPath"]
        )
        mapping.update(sourceSha256=source_hash, driverSha256=driver_hash)
        for row in document["files"]:
            if row["path"] == second_record["sourcePath"]:
                row["sha256"] = source_hash
            elif row["path"] == second_record["driverPath"]:
                row["sha256"] = driver_hash
        document["payloadDefinition"]["files"] = document["files"]
        document["payloadDefinition"]["checkpoints"] = document["checkpoints"]
        document["payloadDefinition"]["sourceInputs"] = document["sourceInputs"]
        digest = self.release_capsule._sha256(
            self.release_capsule._canonical_bytes(document["payloadDefinition"]),
        )
        document["payloadDigest"] = digest
        moved = capsule.root.with_name(f"{digest}-{document['sourceCommit'][:12]}")
        (capsule.root / "capsule.json").write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        capsule.root.rename(moved)

        with self.assertRaisesRegex(self.release_capsule.ReleaseCapsuleError, "duplicate"):
            self.release_capsule.load_capsule(self.root, moved)

    def test_selected_dirty_sources_refuse_but_unrelated_dirty_files_are_allowed(self) -> None:
        (self.root / "docs" / "unrelated.md").parent.mkdir(parents=True)
        (self.root / "docs" / "unrelated.md").write_text("not part of this release\n", encoding="utf-8")
        capsule = self.build()
        self.assertTrue(capsule.payload_digest)

        feature_source = self.root / "features" / "dispatch" / "APP_CODE" / "ddl.sql"
        feature_source.write_text("CREATE TABLE APP_CODE.CHANGED (ID NUMBER);\n", encoding="utf-8")
        with self.assertRaisesRegex(self.release_capsule.ReleaseCapsuleError, "committed"):
            self.build()

    def test_untracked_selected_input_refuses_before_capsule_install(self) -> None:
        second = self.root / "features" / "dispatch" / "APP_CODE" / "untracked.sql"
        second.write_text("CREATE TABLE APP_CODE.UNTRACKED (ID NUMBER);\n", encoding="utf-8")
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["steps"][0]["inputs"].append("features/dispatch/APP_CODE/untracked.sql")
        self.manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(self.release_capsule.ReleaseCapsuleError, "committed Git bytes"):
            self.build()
        capsule_parent = self.root / "scratch" / "release-capsules"
        self.assertFalse(capsule_parent.exists())

    def test_manifest_input_order_changes_capsule_digest_and_driver_mapping_order(self) -> None:
        self.create_feature(second=True)
        self.commit("add second input")
        first = self.build()
        first_data = json.loads((first.root / "capsule.json").read_text(encoding="utf-8"))

        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["steps"][0]["inputs"].reverse()
        self.manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        self.commit("reverse selected input order")
        second = self.build()
        second_data = json.loads((second.root / "capsule.json").read_text(encoding="utf-8"))

        self.assertNotEqual(first.payload_digest, second.payload_digest)
        self.assertEqual(
            [row["sourcePath"] for row in first_data["payloadDefinition"]["sourceToDriver"]],
            ["payload/features/dispatch/APP_CODE/ddl.sql", "payload/features/dispatch/APP_CODE/second.sql"],
        )
        self.assertEqual(
            [row["sourcePath"] for row in second_data["payloadDefinition"]["sourceToDriver"]],
            ["payload/features/dispatch/APP_CODE/second.sql", "payload/features/dispatch/APP_CODE/ddl.sql"],
        )

    def test_changing_a_check_or_feature_source_changes_the_payload_digest(self) -> None:
        original = self.build()
        check_path = self.root / "features" / "dispatch" / "APP_CODE" / "checks.json"
        changed_checks = dict(CHECKS)
        changed_checks["postconditions"] = [{"id": "ready", "sql": "SELECT 0 FROM dual", "expected": 1}]
        check_path.write_text(json.dumps(changed_checks) + "\n", encoding="utf-8")
        self.commit("change check")
        checked = self.build()
        self.assertNotEqual(original.payload_digest, checked.payload_digest)

        source_path = self.root / "features" / "dispatch" / "APP_CODE" / "ddl.sql"
        source_path.write_text("CREATE TABLE APP_CODE.DISPATCH (ID NUMBER, LABEL VARCHAR2(20));\n", encoding="utf-8")
        self.commit("change source")
        changed = self.build()
        self.assertNotEqual(checked.payload_digest, changed.payload_digest)

    def test_shared_app_and_ords_sources_are_frozen_but_descriptors_are_not(self) -> None:
        app_dir = self.root / "apps" / "DEMO" / "100"
        deployments = app_dir / "deployments"
        deployments.mkdir(parents=True)
        (app_dir / "application.apx").write_text("app 100 {\n}\n", encoding="utf-8")
        (app_dir / "status.staging.json").write_text("app-owned file\n", encoding="utf-8")
        (app_dir / "apex-team-export.json").write_text("local baseline\n", encoding="utf-8")
        descriptor = {"workspace": {"name": "TEAM_WS"}, "app": {"id": 100, "databaseSession": {"parsingSchema": "DEMO"}}}
        (deployments / "staging.json").write_text(json.dumps(descriptor) + "\n", encoding="utf-8")
        (deployments / "prod.json").write_text(json.dumps(descriptor) + "\n", encoding="utf-8")
        ords = self.root / "ords" / "import.sql"
        ords.parent.mkdir()
        ords.write_text(
            "-- Generated by SQLcl\n-- Schema: REST_API\nBEGIN\n"
            "  ORDS.ENABLE_SCHEMA(p_schema => 'REST_API');\n  COMMIT;\nEND;\n/\n",
            encoding="utf-8",
        )
        manifest = {"schemaVersion": 2, "steps": [
            {"type": "feature-migrate", "schema": "APP_CODE", "inputs": ["features/dispatch/APP_CODE/ddl.sql"]},
            {"type": "app-deploy", "appId": 100},
            {"type": "ords-import", "file": "ords/import.sql", "schema": "REST_API"},
        ]}
        self.manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        self.commit("add shared inputs")
        paths = ["apps/DEMO/100/application.apx", "apps/DEMO/100/status.staging.json", "ords/import.sql"]
        shared = {path: (self.root / path).read_bytes() for path in paths}
        first = self.build(shared)
        manifest_data = json.loads((first.root / "capsule.json").read_text(encoding="utf-8"))
        frozen_paths = {row["sourcePath"] for row in manifest_data["sourceInputs"]}
        self.assertIn("payload/apps/DEMO/100/application.apx", frozen_paths)
        self.assertIn("payload/apps/DEMO/100/status.staging.json", frozen_paths)
        self.assertIn("payload/ords/import.sql", frozen_paths)
        self.assertNotIn("payload/apps/DEMO/100/apex-team-export.json", frozen_paths)
        self.assertNotIn("payload/apps/DEMO/100/deployments/staging.json", frozen_paths)

        (deployments / "prod.json").write_text(json.dumps({"workspace": {"name": "OTHER"}}) + "\n", encoding="utf-8")
        (app_dir / "application.apx").touch()
        second = self.build(shared)
        self.assertEqual(first.payload_digest, second.payload_digest)

        (app_dir / "application.apx").write_text("app 100 {\n  name = 'Changed';\n}\n", encoding="utf-8")
        self.commit("change selected app source")
        updated_shared = {path: (self.root / path).read_bytes() for path in paths}
        app_changed = self.build(updated_shared)
        self.assertNotEqual(second.payload_digest, app_changed.payload_digest)

        ords.write_text(ords.read_text(encoding="utf-8").replace("BEGIN\n", "-- changed export\nBEGIN\n"), encoding="utf-8")
        self.commit("change selected ORDS source")
        updated_shared = {path: (self.root / path).read_bytes() for path in paths}
        ords_changed = self.build(updated_shared)
        self.assertNotEqual(app_changed.payload_digest, ords_changed.payload_digest)

    def test_stored_package_uses_transport_safe_driver_and_keeps_original_hash(self) -> None:
        self.create_feature(source=(
            "CREATE OR REPLACE PACKAGE APP_CODE.DISPATCH_PKG AS\n"
            "  PROCEDURE RUN;\n"
            "   \n"
            "  -- preserve-trailing-\n"
            "END DISPATCH_PKG;\n/\n"
        ))
        self.commit("add package")
        capsule = self.build()
        data = json.loads((capsule.root / "capsule.json").read_text(encoding="utf-8"))
        mapping = data["sourceInputs"][0]
        driver = capsule.root / mapping["driverPath"]
        self.assertEqual(mapping["delivery"], "clob")
        self.assertNotEqual(mapping["sourceSha256"], mapping["driverSha256"])
        self.assertIn(b"EXECUTE IMMEDIATE l_source", driver.read_bytes())
        self.assertIn(b"   ", (capsule.root / mapping["sourcePath"]).read_bytes())

    def test_capsule_load_rejects_path_traversal_symlinks_and_edited_drivers(self) -> None:
        capsule = self.build()
        data = json.loads((capsule.root / "capsule.json").read_text(encoding="utf-8"))
        driver = capsule.root / data["sourceInputs"][0]["driverPath"]
        driver.write_bytes(driver.read_bytes() + b"-- edited\n")
        with self.assertRaises(self.release_capsule.ReleaseCapsuleError):
            self.release_capsule.load_capsule(self.root, capsule.root)

        fresh_root = Path(self.temporary.name).resolve() / "second-root"
        fresh_root.mkdir()
        shutil.copytree(Path(__file__).resolve().parents[1] / "scripts", fresh_root / "scripts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(Path(__file__).resolve().parents[1] / "template-manifest.json", fresh_root / "template-manifest.json")
        other = ReleaseCapsuleTests()
        other.root = fresh_root
        other.release_capsule = self.release_capsule
        other.feature_manifest = self.feature_manifest
        other.rollout = self.rollout
        other.manifest_path = fresh_root / "releases" / "current.json"
        other.create_feature()
        other.init_git()
        fresh = other.build()
        extra = fresh.root / "extra.txt"
        try:
            extra.symlink_to(fresh.root / "capsule.json")
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlinks are unavailable: {error}")
        with self.assertRaises(self.release_capsule.ReleaseCapsuleError):
            self.release_capsule.load_capsule(fresh_root, fresh.root)

    def test_explicit_feature_checks_survive_manifest_normalization_and_are_frozen(self) -> None:
        checks_dir = self.root / "features" / "dispatch" / "APP_CODE" / "checks"
        checks_dir.mkdir()
        explicit = {
            "schemaVersion": 1,
            "preconditions": [{"id": "before", "sql": "SELECT 1 FROM dual", "expected": 1}],
            "postconditions": [{"id": "after", "sql": "SELECT 1 FROM dual", "expected": 1}],
        }
        check_file = checks_dir / "boundary.json"
        check_file.write_text(json.dumps(explicit) + "\n", encoding="utf-8")
        manifest = {"schemaVersion": 2, "steps": [{
            "type": "feature-migrate", "schema": "APP_CODE",
            "inputs": ["features/dispatch/APP_CODE/ddl.sql"],
            "checks": ["features/dispatch/APP_CODE/checks/boundary.json"],
        }]}
        self.manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        self.commit("select explicit checks")

        loaded_manifest = self.rollout.load_manifest(self.manifest_path, self.root)
        self.assertEqual(loaded_manifest.steps[0]["checks"], manifest["steps"][0]["checks"])
        capsule = self.build()
        self.assertEqual(capsule.checkpoints[0].preconditions[0].id, "dispatch-boundary-before")
        data = json.loads((capsule.root / "capsule.json").read_text(encoding="utf-8"))
        frozen_files = {row["path"] for row in data["files"]}
        self.assertIn("payload/features/dispatch/APP_CODE/checks/boundary.json", frozen_files)
        self.assertNotIn("payload/features/dispatch/APP_CODE/checks.json", frozen_files)

    def test_capsule_payload_digest_binds_checkpoint_semantics(self) -> None:
        capsule = self.build()
        path = capsule.root / "capsule.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["checkpoints"][0]["postconditions"][0]["expected"] = 0
        data["payloadDefinition"]["checkpoints"] = data["checkpoints"]
        path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(self.release_capsule.ReleaseCapsuleError, "payload digest"):
            self.release_capsule.load_capsule(self.root, capsule.root)

    def test_capsule_loader_rejects_traversal_missing_and_extra_artifacts(self) -> None:
        capsule = self.build()
        path = capsule.root / "capsule.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["files"][0]["path"] = "payload/../outside.sql"
        data["payloadDefinition"]["files"] = data["files"]
        data["payloadDigest"] = self.release_capsule._sha256(
            self.release_capsule._canonical_bytes(data["payloadDefinition"])
        )
        path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(self.release_capsule.ReleaseCapsuleError, "safe relative path"):
            self.release_capsule.load_capsule(self.root, capsule.root)

        # Rebuild into a fresh temporary repository for independent file-set checks.
        fresh_root = Path(self.temporary.name).resolve() / "file-set-root"
        fresh_root.mkdir()
        shutil.copytree(Path(__file__).resolve().parents[1] / "scripts", fresh_root / "scripts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(Path(__file__).resolve().parents[1] / "template-manifest.json", fresh_root / "template-manifest.json")
        (fresh_root / "features").mkdir()
        other = ReleaseCapsuleTests()
        other.root = fresh_root
        other.release_capsule = self.release_capsule
        other.feature_manifest = self.feature_manifest
        other.rollout = self.rollout
        other.manifest_path = fresh_root / "releases" / "current.json"
        other.create_feature()
        other.init_git()
        fresh = other.build()
        driver = next((fresh.root / "drivers").glob("*.sql"))
        driver.unlink()
        with self.assertRaisesRegex(self.release_capsule.ReleaseCapsuleError, "incomplete or has extra"):
            self.release_capsule.load_capsule(fresh_root, fresh.root)

        # Restore by making another independent capsule, then add an unlisted file.
        extra_root = Path(self.temporary.name).resolve() / "extra-root"
        extra_root.mkdir()
        shutil.copytree(Path(__file__).resolve().parents[1] / "scripts", extra_root / "scripts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(Path(__file__).resolve().parents[1] / "template-manifest.json", extra_root / "template-manifest.json")
        other.root = extra_root
        other.manifest_path = extra_root / "releases" / "current.json"
        other.create_feature()
        other.init_git()
        extra_capsule = other.build()
        (extra_capsule.root / "extra.sql").write_text("-- extra\n", encoding="utf-8")
        with self.assertRaisesRegex(self.release_capsule.ReleaseCapsuleError, "incomplete or has extra"):
            self.release_capsule.load_capsule(extra_root, extra_capsule.root)

    def test_capsule_generation_refuses_occupied_or_partial_destination_without_replacing_it(self) -> None:
        capsule = self.build()
        prior = {path.relative_to(capsule.root): path.read_bytes() for path in capsule.root.rglob("*") if path.is_file()}
        (capsule.root / "capsule.json").write_bytes(b"occupied\n")
        occupied_before = {path.relative_to(capsule.root): path.read_bytes() for path in capsule.root.rglob("*") if path.is_file()}
        with self.assertRaises(self.release_capsule.ReleaseCapsuleError):
            self.build()
        occupied_after = {path.relative_to(capsule.root): path.read_bytes() for path in capsule.root.rglob("*") if path.is_file()}
        self.assertEqual(occupied_before, occupied_after)
        self.assertIn(Path("capsule.json"), prior)

        partial_root = Path(self.temporary.name).resolve() / "partial-root"
        partial_root.mkdir()
        shutil.copytree(Path(__file__).resolve().parents[1] / "scripts", partial_root / "scripts", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copy2(Path(__file__).resolve().parents[1] / "template-manifest.json", partial_root / "template-manifest.json")
        other = ReleaseCapsuleTests()
        other.root = partial_root
        other.release_capsule = self.release_capsule
        other.feature_manifest = self.feature_manifest
        other.rollout = self.rollout
        other.manifest_path = partial_root / "releases" / "current.json"
        other.create_feature()
        other.init_git()
        original_writer = self.release_capsule._write_stage_file
        calls = 0

        def fail_during_stage(stage: Path, relative: str, source: bytes) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise self.release_capsule.ReleaseCapsuleError("injected partial stage failure")
            original_writer(stage, relative, source)

        with patch.object(self.release_capsule, "_write_stage_file", side_effect=fail_during_stage):
            with self.assertRaisesRegex(self.release_capsule.ReleaseCapsuleError, "partial stage failure"):
                other.build()
        parent = partial_root / "scratch" / "release-capsules"
        self.assertEqual(list(parent.iterdir()), [])

    def test_capsule_engine_digest_tracks_ordered_engine_files(self) -> None:
        first = self.release_capsule.capsule_engine_digest(self.root)
        engine_file = self.root / "scripts" / "feature_manifest.py"
        engine_file.write_bytes(engine_file.read_bytes() + b"\n# engine change\n")
        second = self.release_capsule.capsule_engine_digest(self.root)

        self.assertNotEqual(first, second)

    def test_migration_resolution_is_a_capsule_engine_file_listed_beside_attempts(self) -> None:
        helpers = self.release_capsule.ENGINE_HELPER_FILES

        self.assertIn("scripts/migration_resolution.py", helpers)
        self.assertEqual(helpers.index("scripts/migration_resolution.py"), helpers.index("scripts/migration_attempts.py") + 1)

    def test_capsule_engine_digest_tracks_runtime_helpers_and_trusted_sql(self) -> None:
        for relative in (
            "scripts/migrate.py",
            "scripts/migration_attempts.py",
            "scripts/migration_resolution.py",
            "scripts/rollout_capsule.py",
            "scripts/release_cleanup.py",
            "scripts/release_recovery.py",
            "scripts/baseline_data.py",
            "scripts/baseline_ords_filter.py",
            "scripts/baseline_build.py",
            "scripts/baseline_sql.py",
            "scripts/baseline_reference_data.py",
            "scripts/baseline_bundles.py",
            "scripts/db_targets.py",
            "scripts/sqlcl_transport.py",
            "scripts/migrate.sql",
            "scripts/verify_migration_access.sql",
            "scripts/compare_env_catalog.sql",
            "scripts/feature_claims.py",
            "scripts/feature_ddl_forms.py",
            "scripts/feature_preflight.py",
            "scripts/schema_catalog.sql",
            "scripts/deploy.sh",
            "scripts/publish_app.sh",
            "scripts/application_lifecycle.sql",
            "scripts/publish_app.sql",
        ):
            with self.subTest(relative=relative):
                first = self.release_capsule.capsule_engine_digest(self.root)
                path = self.root / relative
                path.write_bytes(path.read_bytes() + b"\n-- engine dependency change\n")
                second = self.release_capsule.capsule_engine_digest(self.root)
                self.assertNotEqual(first, second, f"engine digest ignored {relative}")

    def test_capsule_engine_digest_refuses_a_missing_required_runtime_helper(self) -> None:
        (self.root / "scripts" / "migrate.sql").unlink()

        with self.assertRaisesRegex(self.release_capsule.ReleaseCapsuleError, "engine file is missing"):
            self.release_capsule.capsule_engine_digest(self.root)

    def test_migration_only_release_freezes_and_loads_without_a_feature_checkpoint(self) -> None:
        folder = "migrations/APP_CODE/2026-01-01_legacy-r001"
        migration = self.root / folder
        migration.mkdir(parents=True)
        (migration / "001-create-legacy.sql").write_text("CREATE TABLE APP_CODE.LEGACY (ID NUMBER);\n", encoding="utf-8", newline="\n")
        (migration / "checks.json").write_text(json.dumps(CHECKS) + "\n", encoding="utf-8")
        self.manifest_path.write_text(
            json.dumps({"schemaVersion": 2, "steps": [{"type": "migrate", "folders": [folder]}]}, indent=2) + "\n",
            encoding="utf-8",
        )
        self.commit("migration-only release")

        capsule = self.release_capsule.build_capsule(self.root, self.manifest_path, (), {})

        self.assertEqual(capsule.checkpoints, ())
        self.assertEqual(self.release_capsule.load_capsule(self.root, capsule.root).checkpoints, ())
        document = json.loads((capsule.root / "capsule.json").read_text(encoding="utf-8"))
        self.assertEqual(document["checkpoints"], [])
        self.assertEqual([step["type"] for step in document["payloadDefinition"]["steps"]], ["migrate"])

    def test_final_source_and_ordered_backfills_freeze_without_replaying_the_archive(self) -> None:
        def source(version: int) -> str:
            return (
                "CREATE OR REPLACE PACKAGE BODY APP_CODE.DISPATCH AS\n"
                f"  FUNCTION VERSION_NUMBER RETURN NUMBER IS BEGIN RETURN {version}; END;\n"
                "END DISPATCH;\n/\n"
            )

        backfill = "UPDATE APP_CODE.JOBS SET VERSION_USED = APP_CODE.DISPATCH.VERSION_NUMBER();\n"
        assert_version = (
            "DECLARE\n  l_valid NUMBER;\nBEGIN\n"
            "  SELECT COUNT(*) INTO l_valid FROM USER_OBJECTS\n"
            "  WHERE OBJECT_NAME = 'DISPATCH' AND OBJECT_TYPE = 'PACKAGE BODY' AND STATUS = 'VALID';\n"
            "  IF APP_CODE.DISPATCH.VERSION_NUMBER() <> 2 THEN\n"
            "    RAISE_APPLICATION_ERROR(-20001, 'Expected version 2');\n"
            "  END IF;\n"
            "  IF l_valid <> 1 THEN RAISE_APPLICATION_ERROR(-20002, 'Intermediate body is invalid'); END IF;\n"
            "END;\n/\n"
        )
        body_path = "features/dispatch/APP_CODE/packages/dispatch.pkb.sql"
        body = self.root / body_path
        body.parent.mkdir()
        body.write_text(source(3), encoding="utf-8")
        archive = self.root / "migrations-archive/APP_CODE/2026-01-01_dispatch-r001"
        archive.mkdir(parents=True)
        historical = {
            archive / "001-package-body.sql": source(1),
            archive / "status.dev.json": '{"status":"applied"}\n',
        }
        for path, content in historical.items():
            path.write_text(content, encoding="utf-8")
        archive_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in historical}

        feature = {
            "type": "feature-migrate", "schema": "APP_CODE", "inputs": [body_path],
            "checks": ["features/dispatch/APP_CODE/checks.json"],
        }
        ordinary = "migrations/APP_CODE/2026-01-02_backfill-r001"
        transition = "migrations/APP_CODE/2026-01-02_transition-r001"
        jobs_exists = {"id": "jobs-exists", "sql": "SELECT COUNT(*) FROM APP_CODE.JOBS", "expected": 1}
        backfill_complete = {"id": "backfill-complete", "sql": "SELECT COUNT(*) FROM APP_CODE.JOBS WHERE VERSION_USED = 3", "expected": 1}
        for folder, sources, steps, checkpoint_count in (
            (ordinary, {"001-backfill.sql": backfill}, [feature, {"type": "migrate", "folders": [ordinary]}], 1),
            (transition, {
                "001-intermediate.sql": source(2), "002-assert-version.sql": assert_version,
                "003-backfill.sql": backfill.replace("VERSION_NUMBER()", "VERSION_NUMBER() + 1"), "004-final.sql": source(3),
            }, [{"type": "migrate", "folders": [transition]}], 0),
        ):
            with self.subTest(folder=folder):
                migration = self.root / folder
                migration.mkdir(parents=True)
                for filename, sql in sources.items():
                    (migration / filename).write_text(sql, encoding="utf-8")
                migration_checks = {
                    "schemaVersion": 1,
                    "preconditions": [jobs_exists],
                    "postconditions": [backfill_complete],
                }
                (migration / "checks.json").write_text(json.dumps(migration_checks) + "\n", encoding="utf-8")
                self.manifest_path.write_text(json.dumps({"schemaVersion": 2, "steps": steps}) + "\n", encoding="utf-8")
                self.commit(folder)

                capsule = self.build()
                loaded = self.release_capsule.load_capsule(self.root, capsule.root)
                document = json.loads((loaded.root / "capsule.json").read_bytes())

                self.assertEqual(document["payloadDefinition"]["steps"], steps)
                self.assertEqual(len(loaded.checkpoints), checkpoint_count)
                frozen_sql = {
                    row["sourcePath"]: (loaded.root / row["sourcePath"]).read_text(encoding="utf-8")
                    for row in document["sourceInputs"] if row["sourcePath"].endswith(".sql")
                }
                expected = {f"payload/{folder}/{filename}": sql for filename, sql in sources.items()}
                if checkpoint_count:
                    expected[f"payload/{body_path}"] = source(3)
                self.assertEqual(frozen_sql, expected)
                self.assertEqual(
                    {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in historical},
                    archive_hashes,
                )
                expected_trace = ["body:3", "backfill:3"] if checkpoint_count else ["body:2", "assert:2", "backfill:3", "body:3"]
                self._apply_consolidation_with_fake_sqlcl(source(0), expected_trace, capsule=loaded)

        legacy = []
        for version in (1, 2, 3):
            folder = f"migrations/APP_CODE/2026-02-0{version}_legacy-history-r00{version}"
            directory = self.root / folder
            directory.mkdir()
            (directory / "001-body.sql").write_text(source(version), encoding="utf-8")
            if version == 3:
                (directory / "002-backfill.sql").write_text(backfill, encoding="utf-8")
            (directory / "checks.json").write_text(json.dumps({
                "schemaVersion": 1, "preconditions": [jobs_exists],
                "postconditions": [backfill_complete if version == 3 else CHECKS["postconditions"][0]],
            }), encoding="utf-8")
            legacy.append(folder)
        self._apply_consolidation_with_fake_sqlcl(source(0), ["body:1", "body:2", "body:3", "backfill:3"], legacy=legacy)

    def _apply_consolidation_with_fake_sqlcl(self, initial_body: str, expected_trace: list[str], *, capsule=None, legacy=()) -> None:
        from dataclasses import replace

        from scripts import feature_release, migrate, migration_checks
        from scripts.db_targets import Target
        from test_feature_runtime import SETTINGS
        from test_migrate_cli import FakeDatabase

        target = Target("staging", "stage-consolidation", "APP_CODE", "APP_CODE", "staging")
        database = FakeDatabase(target)
        database.objects[("JOBS", "TABLE")] = {"columns": {"VERSION_USED"}}
        database.objects[("DISPATCH", "PACKAGE")] = {}
        database.objects[("DISPATCH", "PACKAGE BODY")] = {}
        identity = database.identity()
        state = {
            "schema": "APP_CODE", "identity": identity,
            "sourceUnits": {"PACKAGE BODY:DISPATCH": {
                "lines": feature_release._stored_source(replace(
                    self.feature_manifest.FeatureInput("body.sql", b"", "", ("APP_CODE", "PACKAGE BODY", "DISPATCH"), "stored-source"),
                    source=initial_body.encode("utf-8"),
                ))[0]["lines"], "settings": SETTINGS, "valid": True,
            }},
            "structural": {section: [] for section in feature_release.STRUCTURAL_SECTIONS},
            "coverage": {"ownerComplete": False, "selectedScopeComplete": True},
        }
        trace = []
        version = 0
        data = None

        def fake_sqlcl(_target, driver, run_dir, **_kwargs):
            nonlocal version, data
            source = driver.read_text(encoding="utf-8")
            payloads = re.findall(r"(?m)^PROMPT MIGRATION_PAYLOAD_STARTED:(\S+)\n@@(\S+)$", source)
            self.assertTrue(payloads)
            for _, relative in payloads:
                raw = (run_dir / relative).read_bytes()
                sql = raw.decode("utf-8")
                if "CREATE OR REPLACE PACKAGE BODY" in sql:
                    version = int(re.search(r"\bRETURN (\d+);", sql).group(1))
                    item = self.feature_manifest.FeatureInput("body.sql", raw, "", ("APP_CODE", "PACKAGE BODY", "DISPATCH"), "stored-source")
                    state["sourceUnits"]["PACKAGE BODY:DISPATCH"]["lines"] = feature_release._stored_source(item)[0]["lines"]
                    trace.append(f"body:{version}")
                elif "RAISE_APPLICATION_ERROR" in sql:
                    self.assertIn("USER_OBJECTS", sql)
                    self.assertEqual(version, 2)
                    self.assertTrue(state["sourceUnits"]["PACKAGE BODY:DISPATCH"]["valid"])
                    trace.append(f"assert:{version}")
                elif sql.startswith("UPDATE APP_CODE.JOBS"):
                    data = version + int("VERSION_NUMBER() + 1" in sql)
                    trace.append(f"backfill:{data}")
                else:
                    self.fail(f"fixture SQLcl received an unexpected payload {relative}")
            output = "\n".join([
                "MIGRATION_IDENTITY_BEGIN", json.dumps(identity), "MIGRATION_IDENTITY_END", "MIGRATION_IDENTITY_VERIFIED",
                *(f"MIGRATION_PAYLOAD_STARTED:{name}" for name, _ in payloads),
                "MIGRATION_APPLY_COMPLETED", *(["FEATURE_CHECKPOINT_APPLY_COMPLETED"] if "FEATURE_CHECKPOINT_APPLY_COMPLETED" in source else []), "",
            ])
            (run_dir / "sqlcl-output.log").write_text(output, encoding="utf-8")
            return SimpleNamespace(returncode=0, output=output, run_dir=run_dir)

        def checks(target, selected, directory, *, phase="preconditions", **_kwargs):
            if any(check.id == "backfill-complete" for check in selected):
                self.assertEqual((version, data), (3, 3))
            report = database.run_checks(target, selected, directory, phase=phase)
            return replace(report, coverage={**report.coverage, "sessions": 1, "sessionIdentities": [identity]})

        apply_batch = migrate.apply_batch

        def run_batch(*args, **kwargs):
            return apply_batch(
                *args, **kwargs, capture_inventory_fn=database.capture_inventory,
                capture_snapshot_fn=database.capture_snapshot, run_checks_fn=checks,
            )

        env = {
            "STAGING_MIGRATION_SCHEMA": target.schema, "STAGING_MIGRATION_EXPECTED_USER": target.expected_user,
            "STAGING_MIGRATION_SQLCL_CONNECTION": target.connection,
        }
        output = StringIO()
        with patch.dict(os.environ, env), \
             patch.object(feature_release, "capture_selected_state", return_value=state), \
             patch.object(self.rollout, "_observe_target_identity", return_value=identity), \
             patch.object(migration_checks, "_live_report", return_value=migration_checks.PreflightReport(0, (), (), {})), \
             patch.object(migration_checks, "run_checks", side_effect=checks), \
             patch.object(feature_release.sqlcl_session, "run_sqlcl", side_effect=fake_sqlcl), \
             patch.object(migrate, "run_sqlcl", side_effect=fake_sqlcl), \
             patch.object(migrate, "apply_batch", side_effect=run_batch), \
             redirect_stdout(output), redirect_stderr(output):
            if capsule is None:
                result = migrate.main([*legacy, "--env", "staging", "--yes"], repo_root=self.root, environ=env)
                self.assertEqual(result, 0, output.getvalue())
            else:
                report_path = self.root / "scratch" / f"consolidation-{capsule.payload_digest}.json"
                options = {"repo_root": self.root, "capsule_path": capsule.root, "confirm": lambda _prompt: True, "report_path": report_path}
                self.assertEqual(self.rollout.run_rollout(self.manifest_path, "staging", prepare_only=True, **options), 0, output.getvalue())
                status = self.rollout.run_rollout(self.manifest_path, "staging", **options)
                report = json.loads(report_path.read_bytes())
                diagnostics = [
                    (self.root / path).read_text(encoding="utf-8")
                    for row in report["steps"] for path in row.get("evidencePaths", [])
                    if path.endswith("step-output.log")
                ]
                self.assertEqual(status, 0, report.get("error", "") + "\n".join(diagnostics) if status else "")
        self.assertEqual(trace, expected_trace)
        self.assertEqual((version, data), (3, 3))

    def test_final_body_guard_refuses_after_unmodeled_migration_replacement(self) -> None:
        from scripts import feature_release, migration_checks
        from test_feature_runtime import IDENTITY, SETTINGS

        def body(version: int) -> str:
            return (
                "PACKAGE BODY DISPATCH AS\n"
                f"  FUNCTION VERSION_NUMBER RETURN NUMBER IS BEGIN RETURN {version}; END;\n"
                "END DISPATCH;\n"
            )

        feature = self.root / "features/dispatch/APP_CODE/packages/dispatch.pkb.sql"
        feature.parent.mkdir()
        feature.write_text(f"CREATE OR REPLACE {body(3)}/\n", encoding="utf-8")
        folder = "migrations/APP_CODE/2026-01-02_intermediate-r001"
        migration = self.root / folder
        migration.mkdir(parents=True)
        (migration / "001-package-body.sql").write_text(
            f"BEGIN EXECUTE IMMEDIATE q'[CREATE OR REPLACE {body(2)}]'; END;\n/\n",
            encoding="utf-8",
        )
        (migration / "checks.json").write_text(json.dumps(CHECKS) + "\n", encoding="utf-8")
        steps = [
            {"type": "migrate", "folders": [folder]},
            {"type": "feature-migrate", "schema": "APP_CODE", "inputs": [feature.relative_to(self.root).as_posix()]},
        ]
        self.manifest_path.write_text(json.dumps({"schemaVersion": 2, "steps": steps}) + "\n", encoding="utf-8")
        self.commit("rollout with unmodeled preceding source replacement")
        capsule = self.build()
        catalog = {
            "schema": "APP_CODE",
            "identity": dict(IDENTITY),
            "sourceUnits": {
                "PACKAGE BODY:DISPATCH": {"lines": body(1).splitlines(keepends=True), "settings": SETTINGS, "valid": True},
            },
            "structural": {section: [] for section in feature_release.STRUCTURAL_SECTIONS},
            "coverage": {"ownerComplete": False, "selectedScopeComplete": True},
        }
        env = {
            "STAGING_MIGRATION_SCHEMA": "APP_CODE", "STAGING_MIGRATION_EXPECTED_USER": "APP_CODE",
            "STAGING_MIGRATION_SQLCL_CONNECTION": "staging-conn",
        }

        def simulate_verified_migration(step, **_kwargs):
            self.assertEqual(step["type"], "migrate")
            catalog["sourceUnits"]["PACKAGE BODY:DISPATCH"]["lines"] = body(2).splitlines(keepends=True)
            return self.rollout.StepResult(0)

        output = StringIO()
        report_path = self.root / "scratch/test-rollout.json"
        with patch.dict(os.environ, env), \
             patch.object(feature_release, "capture_selected_state", return_value=catalog), \
             patch.object(self.rollout, "_observe_target_identity", return_value=IDENTITY), \
             patch.object(migration_checks, "_live_report", return_value=migration_checks.PreflightReport(0, (), (), {})), \
             patch.object(self.rollout, "execute_step", side_effect=simulate_verified_migration) as migrate, \
             patch.object(feature_release.sqlcl_session, "run_sqlcl") as apply, \
             redirect_stdout(output), redirect_stderr(output):
            run = {
                "repo_root": self.root, "capsule_path": capsule.root,
                "report_path": report_path, "confirm": lambda _prompt: True,
            }
            prepared = self.rollout.run_rollout(self.manifest_path, "staging", prepare_only=True, **run)
            self.assertEqual(prepared, 0, output.getvalue())
            result = self.rollout.run_rollout(self.manifest_path, "staging", **run)

        self.assertEqual(result, 2, output.getvalue())
        report = json.loads(report_path.read_bytes())
        self.assertRegex(report["error"], r"(?i)(source|before-state).*(changed|drift)|changed.*source")
        self.assertEqual([step["status"] for step in report["steps"]], ["succeeded", "failed"])
        receipt = json.loads((self.root / report["steps"][0]["receiptPath"]).read_bytes())
        self.assertEqual((receipt["status"], receipt["stepType"]), ("succeeded", "migrate"))
        migrate.assert_called_once()
        apply.assert_not_called()

    def test_release_with_neither_a_checkpoint_nor_a_migration_is_refused_at_freeze(self) -> None:
        self.manifest_path.write_text(
            json.dumps({"schemaVersion": 2, "steps": [{"type": "pause", "message": "review"}]}, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(self.release_capsule.ReleaseCapsuleError, "feature checkpoint or migrate step"):
            self.release_capsule.build_capsule(self.root, self.manifest_path, (), {})


class EngineDigestCoverageTests(unittest.TestCase):
    """B5: every file a capsule rollout imports, sources or includes is bound by the engine digest."""

    ENTRY_POINTS = (
        "scripts/rollout.py", "scripts/rollout_capsule.py", "scripts/feature_release.py",
        "scripts/migrate.py", "scripts/release_state.py",
    )
    # Reached by the walk but never executed during a capsule rollout (developer or DEV-only paths).
    NOT_RUNTIME: frozenset[str] = frozenset({
        # feature_cli.py (an engine file) imports these command modules for freeze, doctor, convert-plan,
        # release-plan and DEV apply; a capsule prepare or rollout never runs them.
        "scripts/feature_convert.py", "scripts/feature_doctor.py", "scripts/release_plan.py",
        "scripts/migration_plan.py", "scripts/feature_dev.py", "scripts/feature_dev_views.py",
    })
    # Only references that are used as paths count; a file name inside a message (for example
    # "run scripts/team.sh reconcile-migration") does not make that file part of the rollout.
    _PY_PATH = re.compile(r"""(?:with_name\(\s*|/\s*|["']scripts/)["']?([A-Za-z0-9_]+\.(?:sql|sh|py))["']""")
    _SQL_INCLUDE = re.compile(r"""(?m)^\s*@@?"?(?:[^"\s]*/)?([A-Za-z0-9_]+\.sql)""")
    _SH_PATH = re.compile(r"""\$\{?[A-Za-z_][A-Za-z0-9_]*\}?/([A-Za-z0-9_]+\.(?:sql|sh|py))""")

    def setUp(self) -> None:
        import sys

        self.root = Path(__file__).resolve().parents[1]
        if str(self.root) not in sys.path:
            sys.path.insert(0, str(self.root))
        from scripts import release_capsule

        self.engine = set(release_capsule._engine_file_list(self.root))

    def _python_imports(self, relative: str) -> set[str]:
        import ast

        tree = ast.parse((self.root / relative).read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level == 1 and not module:
                    names.update(alias.name for alias in node.names)
                elif node.level == 1:
                    names.add(module.split(".")[0])
                elif module.startswith("scripts."):
                    names.add(module.split(".")[1])
                elif module == "scripts":
                    names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                names.update(alias.name.split(".")[1] for alias in node.names if alias.name.startswith("scripts."))
        return {f"scripts/{name}.py" for name in names if (self.root / "scripts" / f"{name}.py").is_file()}

    def _referenced_files(self, relative: str) -> set[str]:
        text = (self.root / relative).read_text(encoding="utf-8", errors="replace")
        pattern = {".py": self._PY_PATH, ".sql": self._SQL_INCLUDE, ".sh": self._SH_PATH}[Path(relative).suffix]
        found: set[str] = set()
        for name in pattern.findall(text):
            candidate = f"scripts/{name}"
            if candidate != relative and (self.root / candidate).is_file() and not name.startswith("test_"):
                found.add(candidate)
        return found

    def test_engine_files_cover_runtime_imports_and_sql_includes(self) -> None:
        reached: set[str] = set()
        pending = [path for path in self.ENTRY_POINTS if (self.root / path).is_file()]
        while pending:
            current = pending.pop()
            if current in reached:
                continue
            reached.add(current)
            following = self._referenced_files(current)
            if current.endswith(".py"):
                following |= self._python_imports(current)
            pending.extend(sorted(following - reached))
        missing = sorted(reached - self.engine - self.NOT_RUNTIME - {"scripts/__init__.py"})
        self.assertEqual([], missing, "runtime files reached from the rollout entry points are not in the engine digest")


if __name__ == "__main__":
    unittest.main()
