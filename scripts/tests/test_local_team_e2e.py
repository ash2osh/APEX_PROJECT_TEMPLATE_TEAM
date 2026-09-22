from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
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
    ApexMutation,
    CapturedTree,
    FixtureMutationEvidence,
    ExportConflictEvidence,
    ReviewedExportResolution,
    PreflightEvidence,
    RunManifest,
    create_team_topology,
    capture_live_tree,
    assert_safe_argv,
    cleanup_fixture,
    fixture_builder_save,
    load_export_conflict,
    materialize_export_resolution,
    inspect_fixture,
    parse_saved_connections,
    provision_fixture,
    run_command,
    run_team_command,
    save_connection_script,
)
from teamlib.config import load_config  # noqa: E402
from teamlib.config import Target  # noqa: E402
from teamlib.state import save_capture  # noqa: E402


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


class LocalTeamTopologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="local-team-topology-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self._git("init", "--initial-branch", "main")
        self._git("config", "user.name", "Topology Test")
        self._git("config", "user.email", "topology@example.invalid")
        scripts = self.source / "scripts"
        scripts.mkdir()
        (scripts / "team.py").write_text(
            "import json, os\n"
            "print(json.dumps({k: os.environ.get(k) for k in ('USER', 'TEAM_CHECKOUT_UUID', 'PYTHONDONTWRITEBYTECODE')}))\n",
            encoding="utf-8",
        )
        (self.source / "README.md").write_text("topology fixture\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "seed topology")
        self.commit = subprocess.check_output(
            ["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True
        ).strip()
        self.run_root = self.root / "run"
        self.manifest = RunManifest.create(
            self.run_root,
            FixtureSpec(),
            self.commit,
            {
                "DB_NAME": "FREEPDB1",
                "SERVICE": "freep1",
                "INSTANCE_ID": "FREEPDB1@docker",
                "WORKSPACE_ID": "90000",
            },
        )
        self.env_values = {
            "payload_connection": "docker-demo",
            "metadata_connection": "docker-team-e2e-meta-test",
            "db_name": "FREEPDB1",
            "service": "freep1",
            "instance_id": "FREEPDB1@docker",
            "workspace_id": "90000",
            "metadata_schema": "TEAM_E2E_META",
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.source), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_topology_creates_three_isolated_local_clones_and_ignored_env(self):
        topology = create_team_topology(self.manifest, self.env_values, source_repo=self.source)
        self.assertEqual(topology.source_commit, self.commit)
        self.assertTrue(topology.remote.is_dir())
        self.assertEqual([developer.branch for developer in topology.developers], [
            "e2e/alice", "e2e/bob", "e2e/carol"
        ])
        self.assertEqual(len({developer.checkout_uuid for developer in topology.developers}), 3)
        self.assertEqual(len({developer.git_email for developer in topology.developers}), 3)
        for developer in topology.developers:
            self.assertTrue(developer.clone.is_dir())
            self.assertEqual(
                subprocess.check_output(
                    ["git", "-C", str(developer.clone), "remote", "get-url", "origin"],
                    text=True,
                ).strip(),
                str(topology.remote),
            )
            self.assertNotIn("://", str(topology.remote))
            self.assertEqual(
                subprocess.check_output(
                    ["git", "-C", str(developer.clone), "branch", "--show-current"],
                    text=True,
                ).strip(),
                developer.branch,
            )
            self.assertEqual(stat.S_IMODE(developer.env_file.stat().st_mode), 0o600)
            env_text = developer.env_file.read_text(encoding="utf-8")
            self.assertIn("APEX_APPS=team-e2e:9099", env_text)
            self.assertIn("TABLES_SCHEMA=DEMO", env_text)
            self.assertIn("CODE_SCHEMA=DEMO", env_text)
            self.assertIn("APEX_PARSING_SCHEMA=DEMO", env_text)
            self.assertIn("METADATA_SCHEMA=TEAM_E2E_META", env_text)
            self.assertIn("APP_OWNERSHIP_MODE=shared", env_text)
            config = load_config(developer.env_file, require_verify=True)
            self.assertEqual(config.apps, {"team-e2e": 9099})
            self.assertEqual(config.metadata_schema, "TEAM_E2E_META")
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(developer.clone), "check-ignore", ".env.local-team-e2e"],
                    text=True,
                    capture_output=True,
                    check=False,
                ).returncode,
                0,
            )
        self.assertEqual(self._git("remote"), "")

    def test_run_team_command_scopes_checkout_environment_to_child(self):
        topology = create_team_topology(self.manifest, self.env_values, source_repo=self.source)
        developer = topology.developers[0]
        previous = os.environ.get("TEAM_CHECKOUT_UUID")
        os.environ.pop("TEAM_CHECKOUT_UUID", None)
        try:
            result = run_team_command(developer, "doctor")
        finally:
            if previous is not None:
                os.environ["TEAM_CHECKOUT_UUID"] = previous
        observed = json.loads(result.stdout_path.read_text(encoding="utf-8"))
        self.assertEqual(observed["USER"], "alice")
        self.assertEqual(observed["TEAM_CHECKOUT_UUID"], developer.checkout_uuid)
        self.assertEqual(observed["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertNotIn("TEAM_CHECKOUT_UUID", os.environ)


class FakeBuilderAdapter:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.events: list[object] = []
        self.workspace_id = 90000
        self.live_tree: dict[str, bytes] = {
            "application.apx": b"app TEAM-E2E-9099\n",
            "shared-components/messages.apx": b"text: Simple App\nother: keep\n",
            "pages/p00001-home.apx": b"name: Simple App\nother: keep\n",
        }

    def capture_fixture_tree(self, developer, spec, destination: Path):
        self.events.append(("capture", developer.name))
        destination.mkdir(parents=True, exist_ok=True)
        for relative, data in self.live_tree.items():
            path = destination / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        from teamlib.trees import tree_digest

        return destination, dict(self.live_tree), tree_digest(self.live_tree)

    def import_fixture_tree(self, developer, spec, source: Path, workspace_id: int, parsing_schema: str):
        self.events.append(("import", spec.fixture_app_id, workspace_id, parsing_schema))
        from teamlib.trees import read_export_tree

        self.live_tree = read_export_tree(source)


class LocalTeamBuilderMutationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="local-team-builder-")
        self.root = Path(self.temp.name)
        self.developer = type("DeveloperStub", (), {"name": "alice", "clone": self.root, "run_root": self.root})()
        self.adapter = FakeBuilderAdapter(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def mutation(self, path="shared-components/messages.apx", old="text: Simple App", replacement="text: Simple App - Alice"):
        return ApexMutation(path, old, replacement, "alice", "test builder save")

    def test_fixture_builder_save_starts_with_fresh_capture_and_binds_import(self):
        evidence = fixture_builder_save(
            self.developer,
            self.mutation(),
            adapter=self.adapter,
            spec=FixtureSpec(),
            workspace_id=90000,
        )
        self.assertIsInstance(evidence, FixtureMutationEvidence)
        self.assertEqual(evidence.event, "fixture_builder_save")
        self.assertEqual(self.adapter.events, [
            ("capture", "alice"),
            ("import", 9099, 90000, "DEMO"),
            ("capture", "alice"),
        ])
        self.assertEqual(evidence.before_line, "text: Simple App")
        self.assertEqual(evidence.after_line, "text: Simple App - Alice")
        self.assertEqual(evidence.after_tree["shared-components/messages.apx"], b"text: Simple App - Alice\nother: keep\n")
        self.assertEqual(evidence.before_tree["pages/p00001-home.apx"], self.adapter.live_tree["pages/p00001-home.apx"])

    def test_mutation_refuses_zero_multiple_binary_stale_and_outside_paths(self):
        with self.assertRaisesRegex(E2EError, "application relative path"):
            ApexMutation("../outside.apx", "old", "new", "alice", "unsafe")
        cases = [
            (self.mutation(old="not present"), "exactly one"),
            (self.mutation(path="shared-components/messages.apx", old="other: keep", replacement="other: changed"), "exactly one"),
            (self.mutation(path="binary.bin"), "UTF-8"),
            (self.mutation(path="missing.apx"), "captured live application"),
        ]
        self.adapter.live_tree["binary.bin"] = b"\xff\x00"
        self.adapter.live_tree["shared-components/messages.apx"] = b"text: Simple App\nother: keep\nother: keep\n"
        for mutation, message in cases:
            with self.subTest(path=mutation.relative_path), self.assertRaisesRegex(E2EError, message):
                fixture_builder_save(self.developer, mutation, adapter=self.adapter, spec=FixtureSpec(), workspace_id=90000)


class LocalTeamConflictEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="local-team-conflict-")
        self.root = Path(self.temp.name)
        self.clone = self.root / "dev" / "carol"
        self.clone.mkdir(parents=True)
        self.state = self.clone / ".sync-state"
        self.developer = type("DeveloperStub", (), {"name": "carol", "clone": self.clone, "run_root": self.root})()
        self.target = Target(
            project="local-team-e2e", role="developer", environment="development",
            connection="docker-demo", instance_id="FREE@docker", db_name="FREEPDB1", service="freep1",
            session_user="DEMO", current_schema="DEMO", alias="team-e2e",
            workspace_id=90000, app_id=9099, parsing_schema="DEMO",
            ownership_mode="shared", binding_digest="a" * 64,
        )
        self.base = {
            "application.apx": b"app TEAM-E2E-9099\n",
            ".apex/apexlang.json": b"{}\n",
            "pages/home.apx": b"name: Simple App\nkeep: base\n",
            "shared-components/messages.apx": b"text: Simple App\n",
        }
        self.head = dict(self.base)
        self.head["pages/home.apx"] = b"name: Simple App - Carol Source\nkeep: base\n"
        self.mine = dict(self.base)
        self.mine["pages/home.apx"] = b"name: Simple App - Shared Builder\nkeep: base\n"
        self.head_commit = "b" * 40
        self.recovery_id = save_capture(
            self.target,
            self.base,
            self.base,
            self.head,
            self.mine,
            {
                "kind": "export",
                "tree_digest": __import__("teamlib.trees", fromlist=["tree_digest"]).tree_digest(self.mine),
                "conflicts": ["pages/home.apx"],
                "head_commit": self.head_commit,
            },
            root=self.state,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_loads_closed_four_way_evidence_and_materializes_reviewed_tree(self):
        evidence = load_export_conflict(self.developer, self.target, self.recovery_id, state_root=self.state)
        self.assertIsInstance(evidence, ExportConflictEvidence)
        self.assertEqual(evidence.application_id, 9099)
        self.assertEqual(evidence.conflicts, ("pages/home.apx",))
        self.assertEqual(evidence.head_tree["pages/home.apx"], self.head["pages/home.apx"])
        result = materialize_export_resolution(
            evidence,
            {"pages/home.apx": self.mine["pages/home.apx"]},
            self.root / "reviewed" / "carol",
        )
        self.assertIsInstance(result, ReviewedExportResolution)
        self.assertEqual(result.tree["pages/home.apx"], self.mine["pages/home.apx"])
        self.assertEqual(result.tree["shared-components/messages.apx"], self.base["shared-components/messages.apx"])
        self.assertTrue((result.root / "application.apx").is_file())
        self.assertTrue((result.root / ".apex" / "apexlang.json").is_file())

    def test_review_requires_exact_conflict_choices_and_safe_output(self):
        evidence = load_export_conflict(self.developer, self.target, self.recovery_id, state_root=self.state)
        for choices in ({}, {"pages/home.apx": b"x", "extra.apx": b"x"}, {"pages/home.apx": "text"}):
            with self.subTest(choices=choices), self.assertRaisesRegex(E2EError, "every conflicted path exactly once|path-safe bytes"):
                materialize_export_resolution(evidence, choices, self.root / "reviewed" / secrets.token_hex(4))
        with self.assertRaisesRegex(E2EError, "inside the developer run root"):
            materialize_export_resolution(
                evidence,
                {"pages/home.apx": self.mine["pages/home.apx"]},
                self.root.parent / "outside",
            )


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
