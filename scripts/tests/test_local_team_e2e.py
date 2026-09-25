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
    Developer,
    FixtureSpec,
    ApexMutation,
    CapturedTree,
    FixtureMutationEvidence,
    ExportConflictEvidence,
    ReviewedExportResolution,
    ConvergenceEvidence,
    CommandResult,
    SqlclGate,
    PreflightEvidence,
    RunManifest,
    create_team_topology,
    capture_live_tree,
    assert_safe_argv,
    cleanup_fixture,
    capture_migration_id,
    complete_authored_migration,
    fixture_builder_save,
    load_export_conflict,
    materialize_export_resolution,
    inspect_fixture,
    parse_saved_connections,
    provision_fixture,
    run_command,
    start_team_command,
    run_team_command,
    save_connection_script,
    verify_convergence,
    write_report,
)
from teamlib.config import load_config, profile_target  # noqa: E402
from teamlib.config import Target  # noqa: E402
from teamlib.control_store import ControlStore  # noqa: E402
from teamlib.page_locks import LockReport, PageLock  # noqa: E402
from teamlib.publish import PublishError, prepare_publish, publish_prepared  # noqa: E402
from teamlib.state import save_capture, save_verified_baseline  # noqa: E402
from teamlib.trees import read_git_tree, tree_digest  # noqa: E402


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


class LocalTeamGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="local-team-gate-")
        self.root = Path(self.temp.name)
        self.real = self.root / "real-sqlcl"
        self.real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(self.real, 0o700)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_gate_signals_import_and_releases_a_waiting_child(self):
        gate = SqlclGate.create(self.root, real_executable=self.real)
        driver = self.root / "import.sql"
        driver.write_text("APEX IMPORT -INPUT /run-owned/app -ID 9099\n", encoding="utf-8")
        process = subprocess.Popen(
            [str(gate.executable), "-S", "@" + str(driver)],
            env={**os.environ, **gate.environment("hold")},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertTrue(gate.wait_for_payload(timeout=3.0))
        self.assertTrue(gate.payload_started.is_file())
        gate.release_payload()
        self.assertEqual(process.wait(timeout=5), 0)
        process.stdout.close()
        process.stderr.close()

    def test_gate_unknown_mode_returns_nonzero_after_payload(self):
        gate = SqlclGate.create(self.root, real_executable=self.real)
        driver = self.root / "import.sql"
        driver.write_text("APEX IMPORT -INPUT /run-owned/app -ID 9099\n", encoding="utf-8")
        result = subprocess.run(
            [str(gate.executable), "@" + str(driver)],
            env={**os.environ, **gate.environment("unknown")},
            capture_output=True,
            check=False,
        )
        self.assertTrue(gate.payload_started.is_file())
        self.assertNotEqual(result.returncode, 0)

    def test_gate_allows_sequential_interlocks_in_one_run(self):
        first = SqlclGate.create(self.root, real_executable=self.real)
        second = SqlclGate.create(self.root, real_executable=self.real)
        self.assertEqual(first.root, second.root)
        self.assertNotEqual(first.executable.parent, second.executable.parent)
        self.assertNotEqual(first.executable, second.executable)

    def test_async_team_command_has_bounded_wait_and_durable_output(self):
        clone = self.root / "dev" / "alice"
        (clone / "scripts").mkdir(parents=True)
        script = clone / "scripts" / "team.py"
        script.write_text("import time; print('started'); time.sleep(0.01)\n", encoding="utf-8")
        developer = Developer("alice", clone, "e2e/alice", "a" * 32, "alice@example.invalid", clone / ".env")
        developer.env_file.write_text("", encoding="utf-8")
        running = start_team_command(developer, "doctor")
        result = running.wait(timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn("started", result.stdout_path.read_text(encoding="utf-8"))


class LocalTeamMigrationAuthoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="local-team-migration-authoring-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_captures_dynamic_id_and_fills_generated_members(self):
        migration_id = capture_migration_id("20260923T120000__alice__shared-note\n")
        self.assertTrue(migration_id.startswith("20260923T"))
        migrations = self.root / "migrations"
        migrations.mkdir()
        (migrations / f"{migration_id}.sql").write_text(
            "-- migration-version: 1\n-- target: tables\n-- destructive: false\n\n",
            encoding="utf-8",
        )
        (migrations / f"{migration_id}.verify.sql").write_text("", encoding="utf-8")
        evidence = complete_authored_migration(
            migrations,
            migration_id,
            forward_sql="CREATE TABLE TEAM_E2E_SHARED_NOTE (NOTE_ID NUMBER PRIMARY KEY);",
            verify_sql="SELECT 'TEAM_ASSERT|team_e2e_shared_note|PASS' FROM DUAL;",
        )
        self.assertEqual(evidence.migration_id, migration_id)
        self.assertIn("-- destructive: false", evidence.sql_path.read_text(encoding="utf-8"))
        self.assertIn("CREATE TABLE TEAM_E2E_SHARED_NOTE", evidence.sql_path.read_text(encoding="utf-8"))
        self.assertIn("TEAM_ASSERT|team_e2e_shared_note|PASS", evidence.verify_path.read_text(encoding="utf-8"))
        self.assertEqual(len(evidence.sql_sha256), 64)

    def test_authoring_helpers_refuse_ambiguous_output_and_destructive_header(self):
        for output in ("", "20260923T120000__alice__shared-note\nextra\n", "not-a-migration\n"):
            with self.subTest(output=repr(output)), self.assertRaises(E2EError):
                capture_migration_id(output)
        migration_id = "20260923T120000__alice__shared-note"
        migrations = self.root / "migrations"
        migrations.mkdir()
        path = migrations / f"{migration_id}.sql"
        path.write_text("-- migration-version: 1\n-- target: tables\n-- destructive: true\n\n", encoding="utf-8")
        (migrations / f"{migration_id}.verify.sql").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(E2EError, "non-destructive migration header"):
            complete_authored_migration(
                migrations,
                migration_id,
                forward_sql="CREATE TABLE X (ID NUMBER);",
                verify_sql="SELECT 1 FROM DUAL;",
            )


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

    def test_topology_creates_three_independent_repositories_and_ignored_env(self):
        topology = create_team_topology(self.manifest, self.env_values, source_repo=self.source)
        self.assertEqual(topology.source_commit, self.commit)
        self.assertFalse((self.run_root / "remote.git").exists())
        self.assertEqual([developer.branch for developer in topology.developers], [
            "e2e/alice", "e2e/bob", "e2e/carol"
        ])
        self.assertEqual(len({developer.checkout_uuid for developer in topology.developers}), 3)
        self.assertEqual(len({developer.git_email for developer in topology.developers}), 3)
        for developer in topology.developers:
            self.assertTrue(developer.clone.is_dir())
            # One repository per developer: no shared remote, no seed ref left
            # behind, and every repository starts at the same template commit.
            self.assertEqual(
                subprocess.check_output(["git", "-C", str(developer.clone), "remote"], text=True).strip(),
                "",
            )
            self.assertEqual(
                subprocess.check_output(["git", "-C", str(developer.clone), "rev-parse", "HEAD"], text=True).strip(),
                self.commit,
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "-C", str(developer.clone), "for-each-ref", "--format=%(refname)"], text=True
                ).split(),
                [f"refs/heads/{developer.branch}"],
            )
            self.assertEqual(
                subprocess.check_output(
                    ["git", "-C", str(developer.clone), "branch", "--show-current"],
                    text=True,
                ).strip(),
                developer.branch,
            )
            self.assertEqual(stat.S_IMODE(developer.env_file.stat().st_mode), 0o600)
            env_text = developer.env_file.read_text(encoding="utf-8")
            self.assertIn("APEX_APPS=team-e2e:9099:DEMO", env_text)
            self.assertIn("TABLES_SCHEMA=DEMO", env_text)
            self.assertIn("CODE_SCHEMA=DEMO", env_text)
            self.assertIn("METADATA_SCHEMA=TEAM_E2E_META", env_text)
            self.assertIn("APP_OWNERSHIP_MODE=shared", env_text)
            config = load_config(developer.env_file, require_verify=True)
            self.assertEqual(config.apps, {"team-e2e": 9099, "payroll": 9100})
            self.assertEqual(config.app_parsing_schemas, {"team-e2e": "DEMO", "payroll": "DEMO"})
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

    def test_convergence_goes_through_the_database_not_a_shared_remote(self):
        topology = create_team_topology(self.manifest, self.env_values, source_repo=self.source)
        live_digest = tree_digest(read_git_tree(self.source, self.commit, "team-e2e"))
        # Bob's own repository has history the others never see.
        bob = topology.developers[1]
        (bob.clone / "NOTES.md").write_text("bob only\n", encoding="utf-8")
        for args in (["add", "NOTES.md"], ["commit", "--quiet", "-m", "Bob's private note"]):
            subprocess.run(["git", "-C", str(bob.clone), *args], check=True, capture_output=True)
        evidence = verify_convergence(
            topology,
            {
                "skip_team_commands": True,
                "application_tree_digest": live_digest,
                "migration_frontier_digest": "c" * 64,
                "migration_history_digest": "d" * 64,
                "mutex_states": {"app": {"is_uncertain": False}},
            },
        )
        self.assertEqual(evidence.errors, ())
        self.assertEqual(len(set(evidence.clone_heads.values())), 2)
        self.assertEqual(set(evidence.clone_tree_digests.values()), {live_digest})
        verbs = {command["argv"][3] for command in evidence.command_evidence if command["argv"][:1] == ["git"]}
        self.assertNotIn("fetch", verbs)
        self.assertNotIn("merge", verbs)
        self.assertNotIn("pull", verbs)

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
        self.seed_digest = "seed-digest"

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
        return tree, self.seed_digest

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

    def test_cleanup_refuses_if_seed_changed_before_owned_deletes(self):
        adapter = FakeFixtureAdapter()
        evidence = inspect_fixture("docker-demo", "docker-sys", FixtureSpec(), adapter=adapter, run_root=self.root)
        provision_fixture(evidence, self.manifest, adapter=adapter, password="generated-only-in-memory")
        adapter.seed_digest = "different-seed"
        with self.assertRaisesRegex(E2EError, "seed application changed"):
            cleanup_fixture(self.manifest, evidence, adapter=adapter)
        self.assertIn(9099, adapter.apps)
        self.assertIn("TEAM_E2E_META", adapter.schemas)




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


class LocalTeamConvergenceAndReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="local-team-report-")
        self.root = Path(self.temp.name) / "run"
        self.manifest = RunManifest.create(
            self.root,
            FixtureSpec(),
            "a" * 40,
            {
                "DB_NAME": "FREEPDB1",
                "SERVICE": "freep1",
                "INSTANCE_ID": "FREEPDB1@docker",
                "SESSION_USER": "DEMO",
                "CURRENT_SCHEMA": "DEMO",
            },
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _phases():
        return {
            "preflight": {"status": "PASS", "evidence": {"identity": "verified"}},
            "provision": {
                "status": "PASS",
                "evidence": {
                    "application_id": 9099,
                    "application_alias": "TEAM-E2E-9099",
                    "application_tree_digest": "d" * 64,
                    "metadata_schema": "TEAM_E2E_META",
                },
            },
            "topology": {
                "status": "PASS",
                "evidence": {
                    "developers": [
                        {"name": name, "checkout_uuid": letter * 32, "head": "b" * 40, "tree_digest": "c" * 64}
                        for name, letter in (("alice", "a"), ("bob", "b"), ("carol", "c"))
                    ],
                },
            },
            "scenario": {
                "status": "PASS",
                "evidence": {
                    "migration_frontier_digest": "e" * 64,
                    "migration_history_digest": "f" * 64,
                    "mutex_states": {"application": {"is_uncertain": False}, "migration": {"is_uncertain": False}},
                },
            },
            "convergence": {
                "status": "PASS",
                "evidence": {
                    "clone_heads": {name: "b" * 40 for name in ("alice", "bob", "carol")},
                    "clone_tree_digests": {name: "d" * 64 for name in ("alice", "bob", "carol")},
                    "live_tree_digest": "d" * 64,
                    "runtime_url": "http://127.0.0.1:8181/ords/r/team-e2e/9099",
                    "runtime_status": "UNKNOWN",
                    "coverage_limits": ["ORDS runtime was not available in this synthetic evidence"],
                },
            },
            "cleanup": {"status": "PASS", "evidence": {"removed": ["application:9099"]}},
        }

    def test_convergence_evidence_has_closed_status_and_report_shape(self):
        evidence = ConvergenceEvidence(
            status="UNKNOWN",
            source_commit="a" * 40,
            clone_heads={"alice": "b" * 40, "bob": "b" * 40, "carol": "b" * 40},
            clone_tree_digests={"alice": "d" * 64, "bob": "d" * 64, "carol": "d" * 64},
            live_tree_digest="d" * 64,
            migration_frontier_digest="e" * 64,
            migration_history_digest="f" * 64,
            mutex_states={"application": {"is_uncertain": False}},
            roster=("a" * 32, "b" * 32, "c" * 32),
            runtime_url="http://127.0.0.1:8181/ords/r/team-e2e/9099",
            runtime_status="UNKNOWN",
            coverage_limits=("Builder UI is not observed",),
        )
        self.assertEqual(evidence.status, "UNKNOWN")
        self.assertIn("runtime_status", evidence.to_dict())
        with self.assertRaises(E2EError):
            ConvergenceEvidence(
                status="OK",
                source_commit="a" * 40,
                clone_heads={},
                clone_tree_digests={},
                live_tree_digest=None,
                migration_frontier_digest=None,
                migration_history_digest=None,
                mutex_states={},
                roster=(),
                runtime_url=None,
                runtime_status="UNKNOWN",
            )

    def test_write_report_is_redacted_atomic_and_bound_to_run_root(self):
        report_path = write_report(self.manifest, self._phases(), {"status": "PASS", "removed": ["application:9099"]})
        self.assertEqual(report_path, self.root / "report.json")
        document = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(document["source_commit"], "a" * 40)
        self.assertEqual(document["run_id"], self.manifest.run_id)
        self.assertEqual(document["status"], "PASS")
        self.assertIn("coverage_limits", document)
        encoded = json.dumps(document)
        self.assertNotIn("password", encoded.casefold())
        self.assertNotIn("raw_environment", encoded.casefold())
        self.assertNotIn(str(self.root.parent), encoded)

        phases = self._phases()
        phases["scenario"]["evidence"]["password"] = "must-not-appear"
        with self.assertRaises(E2EError):
            write_report(self.manifest, phases, {"status": "PASS"})
        phases = self._phases()
        phases["scenario"]["evidence"]["outside_path"] = "/etc/passwd"
        with self.assertRaises(E2EError):
            write_report(self.manifest, phases, {"status": "PASS"})

    def test_write_report_normalizes_command_results_and_rejects_raw_environment(self):
        commands = self.root / "commands"
        commands.mkdir(mode=0o700)
        result = CommandResult(
            argv=("git", "status"),
            cwd=self.root,
            returncode=0,
            stdout_path=commands / "stdout",
            stderr_path=commands / "stderr",
            started_at="2026-09-22T12:00:00+00:00",
            finished_at="2026-09-22T12:00:01+00:00",
            stdout_sha256="a" * 64,
            stderr_sha256="b" * 64,
        )
        phases = self._phases()
        phases["convergence"]["commands"] = [result]
        report_path = write_report(self.manifest, phases, {"status": "PASS"})
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["command_evidence"][0]["argv"], ["git", "status"])
        phases = self._phases()
        phases["scenario"]["evidence"]["environment"] = {"HOME": "/tmp"}
        with self.assertRaises(E2EError):
            write_report(self.manifest, phases, {"status": "PASS"})

    def test_runbook_documents_safe_lifecycle_and_runtime_limit(self):
        runbook = Path(__file__).resolve().parents[2] / "docs" / "local-three-developer-e2e.md"
        text = runbook.read_text(encoding="utf-8")
        for marker in ("preflight", "run", "status", "cleanup", "--confirm-run-id", "9099", "TEAM_E2E_META", "retains", "Builder"):
            self.assertIn(marker, text)
        readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")
        self.assertIn("local-three-developer-e2e.md", readme)


class LocalTeamGuardedPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="local-team-publish-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self._git("init", "--initial-branch", "main")
        self._git("config", "user.name", "Publish Test")
        self._git("config", "user.email", "publish@example.invalid")
        (self.source / "README.md").write_text("e2e fixture\n", encoding="utf-8")
        hr_app_dir = self.source / "apps" / "team-e2e"
        hr_app_dir.mkdir(parents=True)
        (hr_app_dir / "application.apx").write_bytes(b"app TEAM-E2E-9099\n")
        (hr_app_dir / ".apex").mkdir(parents=True)
        (hr_app_dir / ".apex" / "apexlang.json").write_bytes(b'{"format":"APEXLANG"}\n')
        (hr_app_dir / "pages").mkdir()
        (hr_app_dir / "pages" / "p00001-home.apx").write_bytes(b"name: Home\n")
        pay_app_dir = self.source / "apps" / "payroll"
        pay_app_dir.mkdir(parents=True)
        (pay_app_dir / "application.apx").write_bytes(b"app PAYROLL-9100\n")
        (pay_app_dir / ".apex").mkdir(parents=True)
        (pay_app_dir / ".apex" / "apexlang.json").write_bytes(b'{"format":"APEXLANG"}\n')
        (pay_app_dir / "pages").mkdir()
        (pay_app_dir / "pages" / "p00001-home.apx").write_bytes(b"name: Payroll Home\n")
        self._git("add", ".")
        self._git("commit", "-m", "initial commit with hr and payroll")
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
        self.topology = create_team_topology(self.manifest, self.env_values, source_repo=self.source)
        self.alice = next(d for d in self.topology.developers if d.name == "alice")
        self.bob = next(d for d in self.topology.developers if d.name == "bob")
        self.carol = next(d for d in self.topology.developers if d.name == "carol")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        target_dir = cwd or self.source
        result = subprocess.run(
            ["git", "-C", str(target_dir), *args],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_two_developer_publish_refusal_reconciliation_and_sibling_app_preservation(self):
        store_dir = self.root / "store"
        store = ControlStore(store_dir)
        config = load_config(self.alice.env_file)
        hr_target = profile_target(config, "APEX", alias="team-e2e")
        payroll_target = profile_target(config, "APEX", alias="payroll")

        store.setup_state([hr_target, payroll_target])
        store.register_app(hr_target, self.alice.checkout_uuid, "host", "alice")
        store.register_app(hr_target, self.bob.checkout_uuid, "host", "bob")
        store.register_app(payroll_target, self.carol.checkout_uuid, "host", "carol")

        hr_tree_c0 = read_git_tree(self.alice.clone, self.commit, alias="team-e2e")
        pay_tree_c0 = read_git_tree(self.alice.clone, self.commit, alias="payroll")
        state_root = self.alice.clone / ".sync-state"
        save_verified_baseline(hr_target, self.commit, hr_tree_c0, root=state_root)
        save_verified_baseline(payroll_target, self.commit, pay_tree_c0, root=state_root)

        carol_initial_gen = store.read_app_sync_state(payroll_target).generation

        # 1. Alice changes HR APEXlang
        (self.alice.clone / "apps" / "team-e2e" / "pages" / "p00001-home.apx").write_bytes(b"name: Home - Alice\n")
        self._git("commit", "-am", "Alice edit HR", cwd=self.alice.clone)
        alice_commit = self._git("rev-parse", "HEAD", cwd=self.alice.clone)

        # 2. Bob exports a saved HR Builder page (which added page 2 and took page lock)
        bob_live_tree = dict(hr_tree_c0)
        bob_live_tree["pages/p00002-bob.apx"] = b"name: Bob Page\n"
        bob_lock = PageLock(2, "Bob Page", "bob", "2026-09-23T20:00:00Z", "Bob working on page 2")
        hr_lock_report = LockReport("team-e2e", 9099, "KNOWN", (bob_lock,), "APEX_APPLICATION_LOCKED_PAGES")

        live_hr_tree = dict(bob_live_tree)
        live_pay_tree = dict(pay_tree_c0)

        def fake_runner(target, operation, driver, work, **kwargs):
            from types import SimpleNamespace
            if operation == "write":
                if target.alias == "team-e2e":
                    live_hr_tree.clear()
                    live_hr_tree.update(read_git_tree(self.alice.clone, reconciled_commit, alias="team-e2e"))
            export = Path(work) / "exported-app"
            tree = live_hr_tree if target.alias == "team-e2e" else live_pay_tree
            for path, data in tree.items():
                dest = export / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            stdout = (
                f"TEAM_RESULT_BEGIN\n"
                f"TEAM_APP_ID_APEX_VERSION|26.1.4\n"
                f"TEAM_APP_ID_WS_SCHEMA|{target.workspace_id}|{target.parsing_schema}\n"
                f"TEAM_APP_ID_APP|{target.workspace_id}|{target.app_id}|{target.parsing_schema}\n"
                f"TEAM_RESULT_END\n"
            )
            return SimpleNamespace(
                identity={"SESSION_USER": "DEMO", "CURRENT_SCHEMA": "DEMO", "DB_NAME": "FREEPDB1", "SERVICE": "freep1", "INSTANCE_ID": "FREEPDB1@docker"},
                completion={"operation": operation}, result_manifest={"status": "success"},
                log_path=Path(work) / "fake.log", generated_driver=Path(driver),
                stdout=stdout, stderr="", argv=(), exit_code=0,
            )

        def fake_validator(tree, *, sqlcl_bin="sql"):
            from teamlib.apex_validate import ValidationReport
            return ValidationReport(True, "SUCCESS", "Validation successful.", ())

        # 3. Carol keeps working in separate selected-out app (payroll)
        (self.carol.clone / "apps" / "payroll" / "pages" / "p00001-home.apx").write_bytes(b"name: Payroll Home - Carol edit\n")

        # 4. HR preparation sees Bob's lock owner
        bob_capture_id = save_capture(
            hr_target,
            hr_tree_c0,
            hr_tree_c0,
            "0" * 40,
            bob_live_tree,
            {"head_commit": "0" * 40},
            root=state_root,
        )
        prep = prepare_publish(
            self.alice.clone,
            (hr_target,),
            alice_commit,
            {"team-e2e": hr_lock_report},
            store,
            replace_from={"team-e2e": bob_capture_id},
            runner=fake_runner,
            validator=fake_validator,
        )
        self.assertEqual(prep.apps["team-e2e"]["lock_report"]["pages"][0]["locked_by"], "bob")

        # 5. Changed HR capture refuses before import
        live_hr_tree["pages/p00003-extra.apx"] = b"unexpected extra edit in builder\n"
        for checkout_uuid, user in (
            (self.alice.checkout_uuid, "alice"),
            (self.bob.checkout_uuid, "bob"),
        ):
            store.record_publish_acknowledgement(
                hr_target, prep.preparation_id, prep.record_digest,
                checkout_uuid, "host", user,
            )
        with self.assertRaises(PublishError) as ctx:
            publish_prepared(
                self.alice.clone,
                prep.preparation_id,
                confirm_pause=True,
                config=config,
                store=store,
                runner=fake_runner,
                lock_reader=lambda target, **kwargs: hr_lock_report,
            )
        self.assertIn("changed", str(ctx.exception).lower())

        # 6. Reconcile Bob's source and record acknowledgement
        live_hr_tree.pop("pages/p00003-extra.apx")
        (self.alice.clone / "apps" / "team-e2e" / "pages" / "p00002-bob.apx").write_bytes(b"name: Bob Page\n")
        self._git("add", ".", cwd=self.alice.clone)
        self._git("commit", "-m", "Reconcile Bob page into HR", cwd=self.alice.clone)
        reconciled_commit = self._git("rev-parse", "HEAD", cwd=self.alice.clone)

        # Fresh preparation at reconciled_commit with updated capture
        reconciled_capture_id = save_capture(
            hr_target,
            hr_tree_c0,
            hr_tree_c0,
            "0" * 40,
            bob_live_tree,
            {"head_commit": "0" * 40},
            root=state_root,
        )
        prep_reconciled = prepare_publish(
            self.alice.clone,
            (hr_target,),
            reconciled_commit,
            {"team-e2e": hr_lock_report},
            store,
            replace_from={"team-e2e": reconciled_capture_id},
            runner=fake_runner,
            validator=fake_validator,
        )
        for checkout_uuid, user in (
            (self.alice.checkout_uuid, "alice"),
            (self.bob.checkout_uuid, "bob"),
        ):
            store.record_publish_acknowledgement(
                hr_target, prep_reconciled.preparation_id, prep_reconciled.record_digest,
                checkout_uuid, "host", user,
            )

        # 7. Publish verifies HR while Carol's app generation is unchanged
        report = publish_prepared(
            self.alice.clone,
            prep_reconciled.preparation_id,
            confirm_pause=True,
            config=config,
            store=store,
            runner=fake_runner,
            lock_reader=lambda target, **kwargs: hr_lock_report,
        )
        self.assertEqual(report.overall_status, "VERIFIED")
        self.assertEqual(report.app_results["team-e2e"].status, "VERIFIED")

        carol_after_gen = store.read_app_sync_state(payroll_target).generation
        self.assertEqual(carol_after_gen, carol_initial_gen)


if __name__ == "__main__":
    unittest.main()
