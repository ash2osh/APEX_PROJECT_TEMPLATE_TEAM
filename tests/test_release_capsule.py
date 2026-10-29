from __future__ import annotations

import json
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
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

    def test_capsule_engine_digest_tracks_runtime_helpers_and_trusted_sql(self) -> None:
        for relative in (
            "scripts/migrate.py",
            "scripts/migration_attempts.py",
            "scripts/db_targets.py",
            "scripts/sqlcl_transport.py",
            "scripts/migrate.sql",
            "scripts/verify_migration_access.sql",
            "scripts/compare_env_catalog.sql",
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


if __name__ == "__main__":
    unittest.main()
