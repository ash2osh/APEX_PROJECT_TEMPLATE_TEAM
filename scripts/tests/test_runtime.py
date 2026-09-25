from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import tempfile
import unittest
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

from teamlib.config import Config, Profile
from teamlib import runtime
from teamlib.runtime import preflight_online


def config_for(*, role: str = "integration", environment: str = "staging", instance_id: str = "INSTANCE") -> Config:
    profiles = {
        name: Profile(
            name,
            f"{name.lower()}-connection",
            "APP",
            "APP",
            "FREEPDB1",
            "service",
            instance_id,
        )
        for name in ("TABLES", "CODE", "APEX", "METADATA", "VERIFY")
    }
    return Config(
        values={}, profiles=profiles, apps={"employee": 101},
        app_parsing_schemas={"employee": "APP"}, project="team",
        role=role, environment=environment, tables_schema="APP", code_schema="APP_CODE",
        metadata_schema="APP_META", workspace_id=90001,
        ownership_mode="shared",
    )


class RuntimePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-runtime-")
        self.root = Path(self.temp.name)
        self.previous_sqlcl_executable = os.environ.get("TEAM_SQLCL_EXECUTABLE")
        self.sqlcl_executable = self.root / "sql"
        self.sqlcl_executable.touch()
        self.sqlcl_executable.chmod(0o755)
        os.environ["TEAM_SQLCL_EXECUTABLE"] = str(self.sqlcl_executable)
        self.flow_runner = self.root / "flow-runner"
        self.flow_runner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.flow_runner.chmod(0o755)
        (self.root / "scripts" / "sql").mkdir(parents=True)
        (self.root / "scripts" / "sql" / "identity.sql").write_text("SELECT 1 FROM dual;\n", encoding="utf-8")
        (self.root / "scripts" / "sql" / "runtime_versions.sql").write_text("SELECT 1 FROM dual;\n", encoding="utf-8")
        (self.root / "ci").mkdir()
        (self.root / "ci" / "runner-contract.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "toolchain": {
                        "python": "3.10+", "sqlcl": "26.2.1+", "jdk": "17+",
                        "apex": "26.1+", "database": "23ai+",
                        "cryptography": "Ed25519-qualified",
                    },
                    "profiles": ["TABLES", "CODE", "APEX", "METADATA", "VERIFY"],
                    "production": {"credentials": False, "writes": False},
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        if self.previous_sqlcl_executable is None:
            os.environ.pop("TEAM_SQLCL_EXECUTABLE", None)
        else:
            os.environ["TEAM_SQLCL_EXECUTABLE"] = self.previous_sqlcl_executable
        self.temp.cleanup()

    def fake_which(self, name: str) -> str | None:
        return "/usr/bin/" + name

    def fake_versions(self, args, **kwargs):
        executable = Path(args[0]).name
        return SimpleNamespace(
            returncode=0,
            stdout="SQLcl 26.2.1+\n" if executable == "sql" else "openjdk 17.0.1\n",
            stderr="",
        )

    def fake_sqlcl(self, target, operation, driver, work, **kwargs):
        return SimpleNamespace(
            stdout="TEAM_RUNTIME|database|23.4\nTEAM_RUNTIME|apex|26.1\n",
            identity={
                "SESSION_USER": target.session_user,
                "CURRENT_SCHEMA": target.current_schema,
                "DB_NAME": target.db_name,
                "SERVICE": target.service,
                "INSTANCE_ID": target.instance_id,
            },
        )

    def test_missing_sqlcl_and_flow_adapter_refuse_before_profile_probe(self):
        calls = []
        with self.assertRaisesRegex(RuntimeError, "SQLcl executable"):
            preflight_online(
                config_for(), self.root, str(self.root / "missing-flow"),
                runner=lambda *args, **kwargs: calls.append(args),
                sqlcl_resolver=lambda: "",
                which=lambda name: None,
            )
        self.assertEqual(calls, [])

    def test_every_profile_identity_and_observed_version_is_bound(self):
        report = preflight_online(
            config_for(), self.root, str(self.flow_runner),
            runner=self.fake_sqlcl,
            which=self.fake_which,
            command_runner=self.fake_versions,
        )
        self.assertEqual(
            set(report.profiles),
            {"TABLES", "CODE", "APEX:employee", "METADATA", "VERIFY"},
        )
        self.assertEqual(report.versions["database"], "23.4")
        self.assertRegex(report.toolchain_digest, r"^[0-9a-f]{64}$")

    def test_old_observed_toolchain_is_refused(self):
        def old_versions(args, **kwargs):
            executable = Path(args[0]).name
            return SimpleNamespace(
                returncode=0,
                stdout="SQLcl 25.1\n" if executable == "sql" else "openjdk 11.0.1\n",
                stderr="",
            )

        def old_sqlcl(target, *args, **kwargs):
            return SimpleNamespace(
                stdout="TEAM_RUNTIME|database|22.1\nTEAM_RUNTIME|apex|25.1\n",
                identity={
                    "SESSION_USER": target.session_user,
                    "CURRENT_SCHEMA": target.current_schema,
                    "DB_NAME": target.db_name,
                    "SERVICE": target.service,
                    "INSTANCE_ID": target.instance_id,
                },
            )

        with self.assertRaisesRegex(RuntimeError, "required"):
            preflight_online(
                config_for(), self.root, str(self.flow_runner), runner=old_sqlcl,
                which=self.fake_which, command_runner=old_versions,
            )

    def test_non_executable_flow_adapter_is_refused(self):
        flow = self.root / "not-executable"
        flow.write_text("flow", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "executable regular file"):
            preflight_online(
                config_for(), self.root, str(flow), runner=self.fake_sqlcl,
                which=self.fake_which, command_runner=self.fake_versions,
            )

    def test_production_config_is_refused_before_executable_probe(self):
        with self.assertRaisesRegex(RuntimeError, "production"):
            preflight_online(
                config_for(role="production", environment="production"),
                self.root, str(self.flow_runner), which=lambda name: (_ for _ in ()).throw(AssertionError(name)),
            )

    def test_mismatched_profile_identity_is_refused(self):
        config = config_for()
        profiles = dict(config.profiles)
        profiles["CODE"] = Profile(
            "CODE", "code-connection", "APP", "APP", "FREEPDB1", "service", "OTHER"
        )
        config = Config(
            values=config.values, profiles=profiles, apps=config.apps,
            app_parsing_schemas=config.app_parsing_schemas, project=config.project,
            role=config.role, environment=config.environment, tables_schema=config.tables_schema,
            code_schema=config.code_schema,
            metadata_schema=config.metadata_schema, workspace_id=config.workspace_id,
            ownership_mode=config.ownership_mode,
        )
        with self.assertRaisesRegex(RuntimeError, "profile identity"):
            preflight_online(
                config, self.root, str(self.flow_runner), runner=self.fake_sqlcl,
                which=self.fake_which, command_runner=self.fake_versions,
            )

    def test_runtime_version_markers_must_be_exact(self):
        def malformed_sqlcl(target, *args, **kwargs):
            return SimpleNamespace(
                stdout="TEAM_RUNTIME|database|23.4\nTEAM_RUNTIME|database|23.5\n",
                identity={
                    "SESSION_USER": target.session_user,
                    "CURRENT_SCHEMA": target.current_schema,
                    "DB_NAME": target.db_name,
                    "SERVICE": target.service,
                    "INSTANCE_ID": target.instance_id,
                },
            )

        with self.assertRaisesRegex(RuntimeError, "runtime"):
            preflight_online(
                config_for(), self.root, str(self.flow_runner), runner=malformed_sqlcl,
                which=self.fake_which, command_runner=self.fake_versions,
            )

    def test_release_sqlcl_build_is_pinned_to_the_exact_production_build(self):
        check = getattr(runtime, "require_release_sqlcl_build", None)
        self.assertTrue(callable(check), "release toolchain needs an exact SQLcl build guard")
        if not callable(check):
            return

        expected = "26.2.2.233.1901"

        def version_for(build):
            return lambda _args, **_kwargs: SimpleNamespace(
                returncode=0,
                stdout=f"SQLcl: Release 26.2.2.0 Production Build: {build}\n",
                stderr="",
            )

        self.assertEqual(
            check(
                executable_resolver=lambda: str(self.sqlcl_executable),
                command_runner=version_for(expected),
            ),
            expected,
        )
        with self.assertRaisesRegex(RuntimeError, "require SQLcl build"):
            check(
                executable_resolver=lambda: str(self.sqlcl_executable),
                command_runner=version_for("26.2.2.233.1900"),
            )

    def test_release_sqlcl_pin_checks_the_executable_selected_by_sqlcl_runner(self):
        check = runtime.require_release_sqlcl_build
        selected = self.root / "selected-sql"
        ignored = self.root / "ignored-sql"
        selected.touch()
        ignored.touch()
        calls = []

        def version_runner(args, **_kwargs):
            calls.append(args[0])
            return SimpleNamespace(
                returncode=0,
                stdout="SQLcl: Release 26.2.2.0 Production Build: 26.2.2.233.1901\n",
                stderr="",
            )

        with patch.dict(os.environ, {
            "TEAM_SQLCL_EXECUTABLE": str(selected),
            "SQLCL_BIN": str(ignored),
        }):
            check(command_runner=version_runner)

        self.assertEqual(calls, [str(selected)])


if __name__ == "__main__":
    unittest.main()
