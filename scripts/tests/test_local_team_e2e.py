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
    PreflightEvidence,
    RunManifest,
    assert_safe_argv,
    cleanup_fixture,
    inspect_fixture,
    parse_saved_connections,
    provision_fixture,
    run_command,
    save_connection_script,
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

    def test_saved_connection_parser_handles_sqlcl_tree_and_ansi_output(self):
        output = """.
├── 41
│   ├── \x1b[0;33m41-careers\x1b[0m
├── \x1b[0;33mdocker-demo\x1b[0m
└── \x1b[0;33mdocker-sys\x1b[0m
"""
        self.assertEqual(
            parse_saved_connections(output),
            {"41", "41-careers", "docker-demo", "docker-sys"},
        )

    def test_saved_connection_script_places_credentials_in_connection_spec(self):
        script = save_connection_script(
            "docker-team-e2e-meta-test",
            "TEAM_E2E_META",
            "E2ETest_Abc123_X",
        )
        self.assertIn(
            "CONNECT -SAVE docker-team-e2e-meta-test -SAVEPWD TEAM_E2E_META/E2ETest_Abc123_X@//127.0.0.1:1521/FREEPDB1",
            script,
        )
        self.assertNotIn("\nE2ETest_Abc123_X\n", script)


class FakeFixtureAdapter:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.environment = "development"
        self.payload_identity = {
            "DB_NAME": "FREEPDB1",
            "SERVICE": "freep1",
            "INSTANCE_ID": "FREEPDB1@docker",
            "SESSION_USER": "DEMO",
            "CURRENT_SCHEMA": "DEMO",
        }
        self.admin_identity = {
            "DB_NAME": "FREEPDB1",
            "SERVICE": "freep1",
            "INSTANCE_ID": "FREEPDB1@docker",
            "SESSION_USER": "SYS",
            "CURRENT_SCHEMA": "SYS",
        }
        self.workspace = {"id": 90000, "name": "DEMO"}
        self.apps = {
            103: {"id": 103, "alias": "SIMPLE-APP", "workspace_id": 90000, "parsing_schema": "DEMO"}
        }
        self.schemas = {"DEMO"}
        self.saved_connections = {"docker-demo", "docker-sys"}

    def read_identity(self, connection: str) -> dict[str, str]:
        self.events.append(f"read:{connection}-identity")
        return dict(self.admin_identity if connection == "docker-sys" else self.payload_identity)

    def read_workspace_and_apps(self, connection: str) -> tuple[dict[str, object], dict[int, dict[str, object]]]:
        self.events.append("read:workspace-and-apps")
        return dict(self.workspace), {key: dict(value) for key, value in self.apps.items()}

    def read_schemas_and_users(self, connection: str) -> set[str]:
        self.events.append("read:schemas-and-users")
        return set(self.schemas)

    def read_saved_connections(self) -> set[str]:
        self.events.append("read:saved-connections")
        return set(self.saved_connections)

    def capture_seed(self, connection: str, app_id: int, destination: Path) -> tuple[Path, str]:
        self.events.append("read:capture-seed")
        destination.mkdir(parents=True, exist_ok=True)
        tree = destination / "seed-tree"
        tree.mkdir()
        (tree / "application.apx").write_bytes(b"seed")
        return tree, "seed-digest"

    def create_metadata_user(self, admin_connection: str, schema: str, password: str) -> None:
        self.events.append("write:create-metadata-user")
        self.schemas.add(schema)

    def save_metadata_connection(self, admin_connection: str, name: str, schema: str, password: str) -> None:
        self.events.append("write:save-metadata-connection")
        self.saved_connections.add(name)

    def clone_seed_to_fixture(self, payload_connection: str, seed_path: Path, spec: FixtureSpec, workspace_id: int) -> None:
        self.events.append("write:clone-seed-to-fixture")
        self.apps[spec.fixture_app_id] = {
            "id": spec.fixture_app_id,
            "alias": spec.apex_alias,
            "workspace_id": workspace_id,
            "parsing_schema": "DEMO",
        }

    def verify_fixture_export(self, payload_connection: str, spec: FixtureSpec) -> dict[str, object]:
        self.events.append("read:verify-fixture-export")
        return dict(self.apps[spec.fixture_app_id])

    def record_marker(self, metadata_connection: str, schema: str, run_id: str, app_digest: str) -> None:
        self.events.append("write:record-marker")

    def verify_marker(self, metadata_connection: str, schema: str, run_id: str, app_id: int) -> bool:
        self.events.append("read:verify-marker")
        return True

    def remove_fixture_app(self, admin_connection: str, spec: FixtureSpec, workspace_name: str) -> None:
        self.events.append("write:remove-fixture-app")
        self.apps.pop(spec.fixture_app_id, None)

    def drop_metadata_user(self, admin_connection: str, schema: str, run_id: str) -> None:
        self.events.append("write:drop-metadata-user")
        self.schemas.discard(schema)

    def delete_saved_connection(self, name: str) -> None:
        self.events.append("write:delete-saved-connection")
        self.saved_connections.discard(name)


class LocalTeamFixtureLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="local-team-fixture-")
        self.root = Path(self.temp.name) / "run"
        self.manifest = RunManifest.create(
            self.root,
            FixtureSpec(),
            "a" * 40,
            {
                "DB_NAME": "FREEPDB1",
                "SERVICE": "freep1",
                "INSTANCE_ID": "FREEPDB1@docker",
                "WORKSPACE_ID": "90000",
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_preflight_accepts_absent_fixture_and_records_seed(self):
        adapter = FakeFixtureAdapter()
        evidence = inspect_fixture("docker-demo", "docker-sys", FixtureSpec(), adapter=adapter, run_root=self.root)
        self.assertIsInstance(evidence, PreflightEvidence)
        self.assertEqual(evidence.workspace_id, 90000)
        self.assertEqual(evidence.seed_app_id, 103)
        self.assertEqual(evidence.seed_tree_digest, "seed-digest")
        self.assertTrue(evidence.seed_tree_path.is_dir())
        self.assertTrue((evidence.seed_tree_path / "application.apx").is_file())
        self.assertNotIn(9099, evidence.apps)

    def test_preflight_refuses_before_writes_for_identity_or_occupied_resources(self):
        cases: list[tuple[str, str]] = []
        adapter = FakeFixtureAdapter()
        adapter.admin_identity["INSTANCE_ID"] = "OTHER@docker"
        cases.append(("physical database identity", "docker-sys-identity"))
        for label, mutate in (
            ("production", lambda a: setattr(a, "environment", "production")),
            ("seed missing", lambda a: a.apps.pop(103)),
            ("fixture id occupied", lambda a: a.apps.__setitem__(9099, {"id": 9099, "alias": "other", "workspace_id": 90000, "parsing_schema": "DEMO"})),
            ("fixture alias occupied", lambda a: a.apps.__setitem__(104, {"id": 104, "alias": "TEAM-E2E-9099", "workspace_id": 90000, "parsing_schema": "DEMO"})),
            ("metadata schema exists", lambda a: a.schemas.add("TEAM_E2E_META")),
            ("workspace missing", lambda a: a.workspace.update({"id": None, "name": None})),
        ):
            case = FakeFixtureAdapter()
            mutate(case)
            with self.subTest(label=label), self.assertRaises(E2EError):
                inspect_fixture("docker-demo", "docker-sys", FixtureSpec(), adapter=case, run_root=self.root / label.replace(" ", "-"))
            self.assertFalse(any(item.startswith("write:") for item in case.events), label)
        mismatch = FakeFixtureAdapter()
        mismatch.admin_identity["INSTANCE_ID"] = "OTHER@docker"
        (self.root / "identity").mkdir()
        with self.assertRaisesRegex(E2EError, "physical database identity"):
            inspect_fixture("docker-demo", "docker-sys", FixtureSpec(), adapter=mismatch, run_root=self.root / "identity")
        self.assertFalse(any(item.startswith("write:") for item in mismatch.events))

    def test_provision_uses_bounded_write_order_and_records_marker(self):
        adapter = FakeFixtureAdapter()
        evidence = inspect_fixture("docker-demo", "docker-sys", FixtureSpec(), adapter=adapter, run_root=self.root)
        adapter.events.clear()
        provisioned = provision_fixture(evidence, self.manifest, adapter=adapter, password="generated-only-in-memory")
        self.assertEqual(provisioned.metadata_connection, self.manifest.cleanup_targets().saved_connection)
        self.assertEqual(adapter.events, [
            "write:create-metadata-user",
            "write:save-metadata-connection",
            "write:clone-seed-to-fixture",
            "read:verify-fixture-export",
            "write:record-marker",
        ])

    def test_cleanup_refuses_if_live_identity_or_marker_does_not_match(self):
        adapter = FakeFixtureAdapter()
        evidence = inspect_fixture("docker-demo", "docker-sys", FixtureSpec(), adapter=adapter, run_root=self.root)
        provision_fixture(evidence, self.manifest, adapter=adapter, password="generated-only-in-memory")
        adapter.admin_identity["INSTANCE_ID"] = "OTHER@docker"
        with self.assertRaisesRegex(E2EError, "identity"):
            cleanup_fixture(self.manifest, evidence, adapter=adapter)
        self.assertFalse(any(item.startswith("write:remove") for item in adapter.events))

    def test_cleanup_removes_only_manifest_owned_resources(self):
        adapter = FakeFixtureAdapter()
        evidence = inspect_fixture("docker-demo", "docker-sys", FixtureSpec(), adapter=adapter, run_root=self.root)
        provision_fixture(evidence, self.manifest, adapter=adapter, password="generated-only-in-memory")
        report = cleanup_fixture(self.manifest, evidence, adapter=adapter)
        self.assertEqual(report.status, "PASS")
        self.assertNotIn(9099, adapter.apps)
        self.assertNotIn("TEAM_E2E_META", adapter.schemas)
        self.assertNotIn(self.manifest.cleanup_targets().saved_connection, adapter.saved_connections)
        self.assertIn(103, adapter.apps)
        self.assertIn("DEMO", adapter.schemas)




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
