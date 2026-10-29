from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import feature_cli


class FeatureCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_init_is_offline_creates_valid_scaffold_and_does_not_replace_existing_files(self) -> None:
        with patch.object(feature_cli.feature_sources, "capture_package") as capture:
            status = feature_cli.main(
                ["init", "staff-scheduling", "--schema", "APP_CODE"],
                repo_root=self.root,
                environ={},
            )

        self.assertEqual(status, 0)
        capture.assert_not_called()
        readme = self.root / "features/staff-scheduling/README.md"
        checks_path = self.root / "features/staff-scheduling/APP_CODE/checks.json"
        self.assertTrue(readme.is_file())
        checks = json.loads(checks_path.read_text(encoding="utf-8"))
        self.assertEqual(checks["schemaVersion"], 1)
        self.assertEqual(checks["preconditions"], [])
        self.assertEqual(checks["postconditions"][0]["id"], "replace-me")
        self.assertFalse((self.root / ".env").exists())
        readme.write_text("developer content\n", encoding="utf-8")
        self.assertEqual(
            feature_cli.main(["init", "staff-scheduling", "--schema", "APP_CODE"], repo_root=self.root, environ={}),
            2,
        )
        self.assertEqual(readme.read_text(encoding="utf-8"), "developer content\n")

    def test_init_second_schema_preserves_shared_readme_and_first_checks(self) -> None:
        self.assertEqual(feature_cli._init_feature(self.root, "staff-scheduling", "APP_CODE")[0].name, "README.md")
        feature_dir = self.root / "features/staff-scheduling"
        readme = feature_dir / "README.md"
        first_checks = feature_dir / "APP_CODE/checks.json"
        first_checks_bytes = first_checks.read_bytes()
        readme.write_bytes(b"team-owned shared guidance\n")

        created = feature_cli._init_feature(self.root, "staff-scheduling", "APP_DATA")

        second_checks = feature_dir / "APP_DATA/checks.json"
        self.assertEqual(created, (second_checks,))
        self.assertEqual(readme.read_bytes(), b"team-owned shared guidance\n")
        self.assertEqual(first_checks.read_bytes(), first_checks_bytes)
        self.assertTrue(second_checks.is_file())

    def test_init_refuses_nonfile_or_symlink_shared_readme(self) -> None:
        feature_dir = self.root / "features/staff-scheduling"
        feature_dir.mkdir(parents=True)
        readme = feature_dir / "README.md"
        readme.mkdir()
        with self.assertRaises((feature_cli.FeatureCliError, feature_cli.feature_sources.FeatureSourcesError)):
            feature_cli._init_feature(self.root, "staff-scheduling", "APP_CODE")
        self.assertFalse((feature_dir / "APP_CODE/checks.json").exists())

        readme.rmdir()
        outside = self.root / "outside.md"
        outside.write_bytes(b"outside content\n")
        try:
            readme.symlink_to(outside)
        except (NotImplementedError, OSError):
            self.skipTest("symlinks are unavailable on this platform")
        with self.assertRaises((feature_cli.FeatureCliError, feature_cli.feature_sources.FeatureSourcesError)):
            feature_cli._init_feature(self.root, "staff-scheduling", "APP_CODE")
        self.assertEqual(outside.read_bytes(), b"outside content\n")
        self.assertFalse((feature_dir / "APP_CODE/checks.json").exists())

    def test_init_rolls_back_only_files_created_before_second_file_failure(self) -> None:
        feature_dir = self.root / "features/staff-scheduling"
        feature_dir.mkdir(parents=True)
        unrelated = feature_dir / "team-notes.txt"
        unrelated.write_bytes(b"keep me\n")
        readme = feature_dir / "README.md"
        checks = feature_dir / "APP_CODE/checks.json"
        original_open = Path.open

        def fail_second_create(path: Path, mode: str = "r", *args, **kwargs):
            if path == checks and mode == "xb":
                raise OSError("injected second-file failure")
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", new=fail_second_create):
            with self.assertRaisesRegex(feature_cli.FeatureCliError, "created files were removed"):
                feature_cli._init_feature(self.root, "staff-scheduling", "APP_CODE")

        self.assertFalse(readme.exists())
        self.assertFalse(checks.exists())
        self.assertEqual(unrelated.read_bytes(), b"keep me\n")

    def test_capture_is_dev_only_and_dispatches_with_the_supplied_environment(self) -> None:
        with patch.object(feature_cli.feature_sources, "capture_package") as capture:
            invalid = feature_cli.main(
                ["capture", "staff-scheduling", "--package", "DISPATCH", "--schema", "APP_CODE", "--env", "prod"],
                repo_root=self.root,
                environ={},
            )
            self.assertEqual(invalid, 2)
            capture.assert_not_called()

            duplicate = feature_cli.main(
                ["capture", "staff-scheduling", "--package", "DISPATCH", "--schema", "APP_CODE", "--env", "dev", "--env", "dev"],
                repo_root=self.root,
                environ={},
            )
            self.assertEqual(duplicate, 2)
            capture.assert_not_called()

            source_path = self.root / "features/staff-scheduling/APP_CODE/packages/dispatch.pks.sql"
            capture.return_value = (source_path,)
            status = feature_cli.main(
                ["capture", "staff-scheduling", "--package", "DISPATCH", "--schema", "APP_CODE", "--env", "dev", "--refresh"],
                repo_root=self.root,
                environ={"DEV_SCHEMA": "APP_CODE"},
            )

        self.assertEqual(status, 0)
        self.assertEqual(capture.call_args.args[:4], (self.root, "staff-scheduling", "APP_CODE", "DISPATCH"))
        self.assertEqual(capture.call_args.kwargs["environ"], {"DEV_SCHEMA": "APP_CODE"})
        self.assertTrue(capture.call_args.kwargs["refresh"])
        self.assertFalse((self.root / ".env").exists())

    def test_diff_and_cleanup_are_offline_and_cleanup_is_dry_run_without_yes(self) -> None:
        diff_paths = (self.root / "ai_generate/2026-10-27/feature-diffs/package.diff",)
        with patch.object(feature_cli.feature_sources, "diff_captured_package", return_value=diff_paths) as diff, \
             patch.object(feature_cli.release_state, "cleanup_capsule", return_value={"state": "dry-run", "eligible": True}) as cleanup:
            diff_status = feature_cli.main(
                ["diff", "staff-scheduling", "--schema", "APP_CODE", "--day", "2026-10-27"],
                repo_root=self.root,
                environ={},
            )
            clean_status = feature_cli.main(
                ["clean-capsule", "a" * 64], repo_root=self.root, environ={},
            )

        self.assertEqual(diff_status, 0)
        self.assertEqual(clean_status, 0)
        self.assertEqual(diff.call_args.kwargs, {"day": "2026-10-27"})
        cleanup.assert_called_once_with(self.root, "a" * 64, apply=False)
        self.assertFalse((self.root / ".env").exists())

    def test_freeze_loads_version_two_inputs_and_builds_offline_capsule(self) -> None:
        manifest_path = self.root / "releases/current.json"
        checkpoint = object()
        capsule = SimpleNamespace(
            payload_digest="a" * 64,
            engine_digest="b" * 64,
            source_commit="c" * 40,
            root=self.root / "scratch/release-capsules/example",
        )
        manifest = SimpleNamespace(
            path=manifest_path,
            steps=(
                {"type": "feature-migrate", "schema": "APP_CODE", "inputs": ["features/x/APP_CODE/checks.json"]},
                {"type": "pause", "message": "review"},
            ),
        )
        with patch.object(feature_cli.rollout, "load_manifest", return_value=manifest) as load_manifest, \
             patch.object(feature_cli, "load_feature_checkpoint", return_value=checkpoint) as load_checkpoint, \
             patch.object(feature_cli.release_capsule, "build_capsule", return_value=capsule) as build_capsule:
            status = feature_cli.main(["freeze", str(manifest_path)], repo_root=self.root, environ={})

        self.assertEqual(status, 0)
        load_manifest.assert_called_once_with(manifest_path, self.root, version=2)
        load_checkpoint.assert_called_once_with(self.root, manifest.steps[0])
        build_capsule.assert_called_once_with(self.root, manifest_path, (checkpoint,), {})
        self.assertFalse((self.root / ".env").exists())

    def test_cleanup_requires_explicit_yes_to_apply_and_reports_blocked_gate(self) -> None:
        with patch.object(feature_cli.release_state, "cleanup_capsule", return_value={"state": "complete"}) as cleanup:
            status = feature_cli.main(["clean-capsule", "b" * 64, "--yes"], repo_root=self.root, environ={})
        self.assertEqual(status, 0)
        cleanup.assert_called_once_with(self.root, "b" * 64, apply=True)

        with patch.object(
            feature_cli.release_state,
            "cleanup_capsule",
            side_effect=feature_cli.release_state.ReleaseStateError("capsule cleanup refused: unresolved attempt"),
        ):
            status = feature_cli.main(["clean-capsule", "c" * 64, "--yes"], repo_root=self.root, environ={})
        self.assertEqual(status, 1)


if __name__ == "__main__":
    unittest.main()
