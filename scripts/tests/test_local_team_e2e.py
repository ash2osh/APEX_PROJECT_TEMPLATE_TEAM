from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from teamlib.local_team_e2e import (  # noqa: E402
    E2EError,
    FixtureSpec,
    RunManifest,
    assert_safe_argv,
    run_command,
)


class LocalTeamManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="local-team-e2e-test-")
        self.root = Path(self.temp.name) / "run"
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def fixture_spec() -> FixtureSpec:
        return FixtureSpec()

    @staticmethod
    def identity() -> dict[str, str]:
        return {
            "DB_NAME": "FREEPDB1",
            "SERVICE": "freep1",
            "INSTANCE_ID": "FREEPDB1@docker",
            "WORKSPACE_ID": "5402650006222933",
        }

    def test_default_fixture_constants_are_reserved_and_distinct(self):
        spec = self.fixture_spec()
        self.assertEqual(spec.seed_app_id, 103)
        self.assertEqual(spec.fixture_app_id, 9099)
        self.assertEqual(spec.tracked_alias, "team-e2e")
        self.assertEqual(spec.apex_alias, "TEAM-E2E-9099")
        self.assertEqual(spec.metadata_schema, "TEAM_E2E_META")
        self.assertNotEqual(spec.seed_app_id, spec.fixture_app_id)
        self.assertNotEqual(spec.metadata_schema, "DEMO")

    def test_manifest_records_source_identity_and_0600_file(self):
        manifest = RunManifest.create(self.root, self.fixture_spec(), "a" * 40, self.identity())
        self.assertRegex(manifest.run_id, r"^\d{8}T\d{6}-[0-9a-f]{8}$")
        self.assertEqual(manifest.source_commit, "a" * 40)
        self.assertEqual(manifest.expected_identity["INSTANCE_ID"], "FREEPDB1@docker")
        path = self.root / "manifest.json"
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        encoded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(encoded["source_commit"], "a" * 40)
        self.assertNotIn("password", json.dumps(encoded).casefold())

    def test_manifest_refuses_rebinding_after_creation(self):
        RunManifest.create(self.root, self.fixture_spec(), "a" * 40, self.identity())
        with self.assertRaisesRegex(E2EError, "already exists"):
            RunManifest.create(self.root, self.fixture_spec(), "b" * 40, self.identity())

    def test_manifest_phase_transitions_are_closed_and_durable(self):
        manifest = RunManifest.create(self.root, self.fixture_spec(), "a" * 40, self.identity())
        manifest.transition("preflight", "PASS", {"identity": "verified"})
        reloaded = RunManifest.load(self.root)
        self.assertEqual(reloaded.phases["preflight"]["status"], "PASS")
        with self.assertRaisesRegex(E2EError, "unsupported phase"):
            manifest.transition("not-a-phase", "PASS", {})
        with self.assertRaisesRegex(E2EError, "unsupported status"):
            manifest.transition("provision", "OK", {})

    def test_cleanup_targets_are_exact_and_never_include_seed(self):
        manifest = RunManifest.create(self.root, self.fixture_spec(), "a" * 40, self.identity())
        targets = manifest.cleanup_targets()
        self.assertEqual(targets.application_id, 9099)
        self.assertEqual(targets.application_alias, "TEAM-E2E-9099")
        self.assertEqual(targets.metadata_schema, "TEAM_E2E_META")
        self.assertNotEqual(targets.application_id, manifest.spec.seed_app_id)
        self.assertNotIn("DEMO", targets.schemas)
        self.assertTrue(targets.saved_connection.startswith("docker-team-e2e-meta-"))

    def test_manifest_rejects_unsafe_run_roots(self):
        for bad in (Path("/"), self.repo, self.root / "..", self.root / "nested" / ".."):
            with self.subTest(path=str(bad)), self.assertRaises(E2EError):
                RunManifest.create(bad, self.fixture_spec(), "a" * 40, self.identity())

    def test_manifest_rejects_invalid_source_commit_and_fixture_rebinding(self):
        for commit in ("", "not-a-sha", "a" * 39, "a" * 41, "a" * 40 + "\n"):
            with self.subTest(commit=repr(commit)), self.assertRaises(E2EError):
                RunManifest.create(self.root / commit.replace("/", "-"), self.fixture_spec(), commit, self.identity())
        with self.assertRaises(E2EError):
            FixtureSpec(seed_app_id=9099)
        with self.assertRaises(E2EError):
            FixtureSpec(metadata_schema="DEMO")
        with self.assertRaises(E2EError):
            FixtureSpec(tracked_alias="../unsafe")


class LocalTeamCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="local-team-command-")
        self.root = Path(self.temp.name) / "run"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_safe_argv_rejects_shell_controls_and_empty_values(self):
        for argv in ([], ["git", "status;touch"], ["git", "$(touch"], ["git", "a\n b"]):
            with self.subTest(argv=argv), self.assertRaises(E2EError):
                assert_safe_argv(argv)

    def test_run_command_uses_argv_writes_redacted_output_and_hashes(self):
        result = run_command(
            [sys.executable, "-c", "print('secret-' + 'value')"],
            cwd=self.root,
            run_root=self.root,
            env={"E2E_SECRET": "secret-value"},
            secrets=("secret-value",),
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.argv[0], sys.executable)
        self.assertTrue(result.stdout_path.is_file())
        self.assertTrue(result.stderr_path.is_file())
        self.assertEqual(len(result.stdout_sha256), 64)
        self.assertNotIn("secret-value", result.stdout_path.read_text(encoding="utf-8"))
        self.assertNotIn("secret-value", json.dumps(result.to_dict()))

    def test_run_command_refuses_cwd_outside_run_root(self):
        with self.assertRaisesRegex(E2EError, "run root"):
            run_command([sys.executable, "-c", "pass"], cwd=Path(self.temp.name), run_root=self.root)


class LocalTeamCliTests(unittest.TestCase):
    def test_launcher_help_is_available_without_database(self):
        from teamlib.local_team_e2e import build_parser

        parser = build_parser()
        args = parser.parse_args(["status", "--run-root", "scratch/local-team-e2e/example"])
        self.assertEqual(args.command, "status")

    def test_cleanup_requires_exact_run_id(self):
        from teamlib.local_team_e2e import build_parser

        parser = build_parser()
        args = parser.parse_args([
            "cleanup",
            "--run-root",
            "scratch/local-team-e2e/example",
            "--confirm-run-id",
            "20260922T120000-12345678",
        ])
        self.assertEqual(args.confirm_run_id, "20260922T120000-12345678")


if __name__ == "__main__":
    unittest.main()
