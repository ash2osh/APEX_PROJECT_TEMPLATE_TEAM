import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import _test_subprocess_safety  # noqa: F401
import tempfile
import time
import unittest
from pathlib import Path
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
import _console_ctrl_c
import fake_sqlcl
from fake_sqlcl import BASH


ROOT = Path(__file__).resolve().parents[1]


def set_environment_value(environment: dict[str, str], name: str, value: str | None) -> None:
    for existing_name in list(environment):
        if existing_name.casefold() == name.casefold():
            del environment[existing_name]
    if value is not None:
        environment[name] = value


class TeamCliTests(unittest.TestCase):
    def migration_plan_checkout(self, parent: Path, *, env_contents: str | None = None, sqlcl_version: str = "26.2.0.0"):
        """Copy only the real wrappers into a checkout path that stresses spaces and glob characters."""
        root = parent / "migration project [report path]"
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        for name in ("team.sh", "team.ps1", "resolve_python.ps1", "migration_plan_args.ps1"):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        (root / "migrations").mkdir()
        if env_contents is not None:
            (root / ".env").write_text(env_contents, encoding="utf-8", newline="\n")
        fake_bin = parent / "fake sqlcl [report]"
        calls = parent / "sqlcl calls.log"
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
            PROJECT_ENV_FILE=str(root / ".env"),
            PYTHONPATH=str(ROOT),
            FAKE_SQLCL_CALLS=str(calls),
            FAKE_SQLCL_VERSION=sqlcl_version,
        )
        return root, environment, calls

    def migration_archive_checkout(self, parent: Path):
        """Copy only the offline archive command and its shared validators."""
        root = parent / "archive project [folder path]"
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        names = (
            "team.sh", "team.ps1", "resolve_python.ps1", "__init__.py", "migration_archive.py",
            "migration_manifest.py", "migration_revision.py", "migration_attempts.py", "path_utils.py",
            "validate_migration.py",
        )
        for name in names:
            source = ROOT / "scripts" / name
            if source.is_file():
                shutil.copy2(source, scripts / name)
        (root / "migrations" / "APP_DEV").mkdir(parents=True)
        (root / "migrations" / "APP_STAGE").mkdir(parents=True)
        for schema, family in (("APP_DEV", "first-family"), ("APP_STAGE", "second-family")):
            folder = root / "migrations" / schema / f"2026-10-01_{family}-r001"
            folder.mkdir()
            (folder / "001-create-table.sql").write_text("CREATE TABLE T (ID NUMBER);\n", encoding="utf-8")
            (folder / "checks.json").write_text(
                '{"schemaVersion":1,"preconditions":[],"postconditions":[{"id":"ready","sql":"SELECT 1 FROM dual","expected":1}]}\n',
                encoding="utf-8",
            )
        return root

    def test_powershell_archive_route_keeps_schema_option_and_matches_bash_dispatch(self) -> None:
        wrapper = (ROOT / "scripts" / "team.ps1").read_text(encoding="utf-8")

        self.assertIn("archive-migrations <folder> [...] [--schema SCHEMA]", wrapper)
        self.assertIn('"archive-migrations" {', wrapper)
        self.assertIn('"archive-migrations") + $archiveArguments)', wrapper)
        self.assertIn('"migration-plan", "archive-migrations", "dev-test"', wrapper)

    def test_archive_migrations_help_and_schema_scope_are_offline_in_both_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self.migration_archive_checkout(Path(temporary))
            environment = dict(os.environ, PROJECT_ENV_FILE="/no/such/team-env")
            entries = [("team.sh", [BASH])]
            pwsh = _test_subprocess_safety.working_pwsh()
            if pwsh:
                entries.append(("team.ps1", [pwsh, "-NoProfile", "-File"]))

            for name, launcher in entries:
                script = root / "scripts" / name
                with self.subTest(entry=name, command="help"):
                    help_result = subprocess.run(
                        [*launcher, str(script), "--help"], cwd=root, env=environment,
                        capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL, timeout=30,
                    )
                    self.assertEqual(0, help_result.returncode, help_result.stdout + help_result.stderr)
                    self.assertIn("archive-migrations", help_result.stdout)

                with self.subTest(entry=name, command="archive-migrations"):
                    result = subprocess.run(
                        [*launcher, str(script), "archive-migrations", "--all", "--schema", "APP_DEV"],
                        cwd=root, env=environment, capture_output=True, text=True, check=False,
                        stdin=subprocess.DEVNULL, timeout=30,
                    )
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertIn("dry run", result.stdout)
                    self.assertIn("migrations/APP_DEV/2026-10-01_first-family-r001", result.stdout)
                    self.assertNotIn("APP_STAGE", result.stdout)
                    self.assertFalse((root / "migrations-archive").exists())

    def run_bash_env_loader(self, environment: Path) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            scripts = Path(temporary) / "scripts"
            scripts.mkdir()
            loader = scripts / "load_env.sh"
            shutil.copy2(ROOT / "scripts" / "load_env.sh", loader)
            return subprocess.run(
                [
                    BASH,
                    "-c",
                    'set -e; source "$1" "$2"; printf "%s|%s|%s|%s\\n" "$CODE_SCHEMA" "$CODE_EXPECTED_USER" "$CODE_PREFIXES" "$APEX_PARSING_SCHEMA"',
                    BASH,
                    str(loader),
                    str(environment),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_team_help_lists_primary_commands(self) -> None:
        result = subprocess.run(
            [BASH, str(ROOT / "scripts" / "team.sh"), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for command in (
            "doctor",
            "export",
            "publish",
            "app-lock",
            "app-unlock",
            "check-conflicts",
            "migration-plan",
            "dev-test",
            "migrate",
            "query",
            "compare-schema",
            "compare-env",
            "--emit-dba-script <file>",
            "backup-db",
            "deploy",
            "upgrade-template",
            "verify-local",
        ):
            self.assertIn(command, result.stdout)
        self.assertIn("--env dev|staging|prod", result.stdout)
        self.assertIn("--pattern", result.stdout)

    def test_dev_test_help_is_offline_in_both_wrappers(self) -> None:
        for entry in ("team.sh", "team.ps1"):
            if entry.endswith("ps1") and not _test_subprocess_safety.working_pwsh():
                continue
            launcher = [BASH] if entry.endswith("sh") else [_test_subprocess_safety.working_pwsh(), "-NoProfile", "-File"]
            result = subprocess.run(
                [*launcher, str(ROOT / "scripts" / entry), "dev-test", "--help"],
                cwd=ROOT,
                env=dict(os.environ, PROJECT_ENV_FILE="/no/such/dev-test.env"),
                capture_output=True,
                text=True,
                check=False,
            )
            with self.subTest(entry=entry):
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("--plan", result.stdout)
                self.assertIn("--cleanup", result.stdout)
                self.assertNotIn("configuration file not found", result.stdout + result.stderr)

    def test_top_level_help_lists_project_dev_test_options_in_both_wrappers(self) -> None:
        for entry in ("team.sh", "team.ps1"):
            if entry.endswith("ps1") and not _test_subprocess_safety.working_pwsh():
                continue
            launcher = [BASH] if entry.endswith("sh") else [_test_subprocess_safety.working_pwsh(), "-NoProfile", "-File"]
            result = subprocess.run(
                [*launcher, str(ROOT / "scripts" / entry), "--help"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            with self.subTest(entry=entry):
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("--suite FILE", result.stdout)
                self.assertIn("--scenario ID ...", result.stdout)
                self.assertIn("--format text|json|markdown", result.stdout)

    def test_dev_test_forwards_schema_arguments_unchanged_in_both_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "dev test project [unicode]"
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            for name in ("team.sh", "team.ps1", "dev_test.sh", "resolve_python.ps1"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            fake_bin = parent / "fake python [dev-test]"
            fake_bin.mkdir()
            captured = parent / "python arguments.txt"
            python = fake_bin / "python3"
            python.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$TEAM_CAPTURE_ARGS\"\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            environment = dict(os.environ)
            environment.update({
                "PATH": str(fake_bin) + os.pathsep + environment.get("PATH", ""),
                "TEAM_CAPTURE_ARGS": str(captured),
            })
            arguments = [
                "dev-test", "--schema=APP", "--env", "dev", "--suite",
                "dev-tests/orders été/suite.json", "--scenario", "first", "--plan",
            ]
            for entry in ("team.sh", "team.ps1"):
                if entry.endswith("ps1") and not _test_subprocess_safety.working_pwsh():
                    continue
                launcher = [BASH] if entry.endswith("sh") else [_test_subprocess_safety.working_pwsh(), "-NoProfile", "-File"]
                result = subprocess.run(
                    [*launcher, str(scripts / entry), *arguments],
                    cwd=parent,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                with self.subTest(entry=entry):
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertEqual(
                        ["-m", "scripts.dev_test", *arguments[1:]],
                        captured.read_text(encoding="utf-8").splitlines(),
                    )

    def test_migration_plan_help_and_offline_need_no_env(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, environment, calls = self.migration_plan_checkout(Path(temporary))
            for entry in ("team.sh", "team.ps1"):
                if entry.endswith("ps1") and not _test_subprocess_safety.working_pwsh():
                    continue
                launcher = [BASH] if entry.endswith("sh") else [_test_subprocess_safety.working_pwsh(), "-NoProfile", "-File"]
                script = root / "scripts" / entry
                with self.subTest(entry=entry, mode="help"):
                    help_result = subprocess.run(
                        [*launcher, str(script), "migration-plan", "--help"],
                        cwd=temporary, env=environment, capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(0, help_result.returncode, help_result.stderr)
                    self.assertIn("migration-plan", help_result.stdout)
                    self.assertIn("--format", help_result.stdout)
                with self.subTest(entry=entry, mode="offline"):
                    result = subprocess.run(
                        [*launcher, str(script), "migration-plan", "--format", "json"],
                        cwd=temporary, env=environment, capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    report = json.loads(result.stdout)
                    self.assertEqual("offline", report["mode"])
                    self.assertEqual(0, report["summary"]["folderCount"])
                    self.assertEqual("", result.stderr)
            self.assertFalse(calls.exists(), "help and offline inventory must not start SQLcl or load .env")

    def test_git_bash_migration_plan_converts_only_repo_root_for_native_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "project with spaces [native path]"
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(ROOT / "scripts" / "team.sh", scripts / "team.sh")
            fake_bin = parent / "fake-bin"
            fake_bin.mkdir()
            if os.name != "nt":
                (fake_bin / "uname").write_text("#!/bin/sh\nprintf 'MINGW64_NT-10.0\\n'\n", encoding="utf-8")
                (fake_bin / "cygpath").write_text(
                    "#!/bin/sh\n[ \"$1\" = -m ] || exit 3\nprintf 'C:/native/project [repo]\\n'\n",
                    encoding="utf-8",
                )
            captured = parent / "python-args.txt"
            (fake_bin / "python3").write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$TEAM_CAPTURE_ARGS\"\n",
                encoding="utf-8",
            )
            for tool in fake_bin.iterdir():
                tool.chmod(0o755)
            environment = dict(os.environ)
            environment.update({
                "PATH": str(fake_bin) + os.pathsep + environment.get("PATH", ""),
                "TEAM_CAPTURE_ARGS": str(captured),
            })

            result = subprocess.run(
                [BASH, str(scripts / "team.sh"), "migration-plan", "--schema", "APP", "--format", "json"],
                cwd=parent,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            arguments = captured.read_text(encoding="utf-8").splitlines()
            repo_index = arguments.index("--repo-root")
            expected_root = root.resolve().as_posix() if os.name == "nt" else "C:/native/project [repo]"
            self.assertEqual(expected_root, arguments[repo_index + 1])
            self.assertEqual(["--schema", "APP", "--format", "json"], arguments[repo_index + 2:])

    def test_both_wrappers_forward_schema_and_folder_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, environment, calls = self.migration_plan_checkout(Path(temporary))
            selected = (
                "migrations/2026-10-13_zulu-first-r001",
                "migrations/2026-10-13_alpha-second-r001",
            )
            for relative in selected:
                folder = root / relative
                folder.mkdir(parents=True)
                (folder / "001-step.sql").write_text("SELECT 1 FROM dual;\n", encoding="utf-8", newline="\n")
                (folder / "checks.json").write_text(
                    '{"schemaVersion":1,"preconditions":[],"postconditions":[{"id":"ready","sql":"SELECT 1 FROM dual","expected":1}]}\n',
                    encoding="utf-8", newline="\n",
                )
            reports = []
            statuses = []
            for entry in ("team.sh", "team.ps1"):
                if entry.endswith("ps1") and not _test_subprocess_safety.working_pwsh():
                    continue
                launcher = [BASH] if entry.endswith("sh") else [_test_subprocess_safety.working_pwsh(), "-NoProfile", "-File"]
                result = subprocess.run(
                    [*launcher, str(root / "scripts" / entry), "migration-plan", *selected, "--schema", "ALPHA", "--format", "json"],
                    cwd=temporary, env=environment, capture_output=True, text=True, check=False,
                )
                with self.subTest(entry=entry):
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertEqual("", result.stderr)
                    report = json.loads(result.stdout)
                    self.assertEqual("explicit", report["selection"])
                    self.assertEqual("caller-asserted", report["migrations"][0]["schemaSource"])
                    self.assertEqual(list(selected), [item["path"] for item in report["migrations"]])
                    self.assertTrue(all(item["schema"] == "ALPHA" for item in report["migrations"]))
                    reports.append(report)
                    statuses.append(result.returncode)
            self.assertGreaterEqual(len(reports), 1)
            self.assertTrue(all(report == reports[0] for report in reports))
            self.assertEqual([0] * len(statuses), statuses)
            self.assertFalse(calls.exists(), "offline reporting must not start fake SQLcl")

    def test_malformed_migration_plan_options_remain_invalid_before_sqlcl(self) -> None:
        if not _test_subprocess_safety.working_pwsh():
            self.skipTest("PowerShell 7 is required for cross-wrapper migration-plan verification")
        env_contents = "\n".join((
            "PROJECT_NAME=fixture",
            "DEVELOPER_NAME=ALICE",
            "DB_ENVIRONMENT=development",
            "APEX_APP_ID=100",
            "TABLES_SCHEMA=APP",
            "TABLES_PREFIXES=*",
            "TABLES_SQLCL_CONNECTION=dev-app",
            "TABLES_EXPECTED_USER=APP",
            "CODE_SCHEMA=APP",
            "CODE_PREFIXES=*",
            "CODE_SQLCL_CONNECTION=dev-app",
            "CODE_EXPECTED_USER=APP",
            "APEX_PARSING_SCHEMA=APP",
            "APEX_SQLCL_CONNECTION=dev-app",
            "APEX_EXPECTED_USER=APP",
            "",
        ))
        marker = "SECRET_ENV_MARKER_2R"
        with tempfile.TemporaryDirectory() as temporary:
            root, environment, calls = self.migration_plan_checkout(Path(temporary), env_contents=env_contents)
            folder = root / "migrations" / "2026-10-13_option-values-r001"
            folder.mkdir()
            (folder / "001-step.sql").write_text("SELECT 1 FROM dual;\n", encoding="utf-8", newline="\n")
            (folder / "checks.json").write_text(
                '{"schemaVersion":1,"preconditions":[],"postconditions":[{"id":"ready","sql":"SELECT 1 FROM dual","expected":1}]}\n',
                encoding="utf-8", newline="\n",
            )
            cases = (
                ("invalid environment slash", ["migrations/2026-10-13_option-values-r001", "--live", "--env", "staging/", "--format=json"], True),
                ("invalid abbreviated environment option", ["migrations/2026-10-13_option-values-r001", "--live", "--en", "dev/", "--format=json"], True),
                ("invalid environment marker", ["migrations/2026-10-13_option-values-r001", "--live", "--env", marker + "/", "--format=json"], True),
                ("invalid schema slash", ["migrations/2026-10-13_option-values-r001", "--schema", "ALPHA/", "--format=json"], True),
                ("invalid format slash", ["migrations/2026-10-13_option-values-r001", "--format", "json/"], False),
            )
            wrapper_results = {"team.sh": [], "team.ps1": []}
            for entry in ("team.sh", "team.ps1"):
                launcher = [BASH] if entry.endswith("sh") else [_test_subprocess_safety.working_pwsh(), "-NoProfile", "-File"]
                for label, arguments, json_output in cases:
                    result = subprocess.run(
                        [*launcher, str(root / "scripts" / entry), "migration-plan", *arguments],
                        cwd=temporary,
                        env=environment,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    with self.subTest(entry=entry, case=label):
                        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                        self.assertEqual("", result.stderr)
                        self.assertNotIn(marker, result.stdout + result.stderr)
                        self.assertNotIn("SELECT", result.stdout + result.stderr)
                        if json_output:
                            report = json.loads(result.stdout)
                            self.assertEqual(1, report["schemaVersion"])
                            if label.startswith(("invalid environment", "invalid abbreviation")):
                                self.assertIsNone(report["environment"])
                        else:
                            self.assertIn("Invalid migration-plan arguments", result.stdout)
                        wrapper_results[entry].append((result.returncode, result.stdout))
            self.assertEqual(wrapper_results["team.sh"], wrapper_results["team.ps1"])
            self.assertFalse(calls.exists(), "malformed option values must not start fake SQLcl")

    def test_migration_plan_powershell_classifies_only_folder_positionals(self) -> None:
        powershell = _test_subprocess_safety.working_pwsh()
        if not powershell:
            self.skipTest("PowerShell 7 is required for the portable argument classifier test")
        helper = ROOT / "scripts" / "migration_plan_args.ps1"
        arguments = [
            "migrations\\first\\",
            "--live",
            "--env",
            "staging/",
            "--schema=ALPHA/",
            "--format",
            "json/",
            "--repo-root",
            "C:\\repo with space [x]\\",
            "--format=json",
            "migrations\\second\\",
            "--env",
            "--format=json",
            "migrations\\third\\",
        ]
        encoded_arguments = json.dumps(arguments)
        command = (
            "$ErrorActionPreference = 'Stop'; "
            f". '{helper.as_posix()}'; "
            f"$values = ConvertFrom-Json -AsHashtable @'\n{encoded_arguments}\n'@; "
            "$items = @(Get-MigrationPlanArgumentKinds -Values $values); "
            "$items | ConvertTo-Json -Compress -Depth 3"
        )
        result = subprocess.run(
            [powershell, "-NoProfile", "-Command", command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        classified = json.loads(result.stdout)
        self.assertEqual(
            ["folder", "option", "option", "option-value", "option", "option", "option-value", "option", "option-value", "option", "folder", "option", "option", "folder"],
            [item["kind"] for item in classified],
        )
        self.assertEqual(arguments, [item["value"] for item in classified])

    def test_live_refuses_ambiguous_flags_before_connection(self) -> None:
        secret = "SECRET_MARKER_FROM_ENV_6P"
        with tempfile.TemporaryDirectory() as temporary:
            root, environment, calls = self.migration_plan_checkout(
                Path(temporary), env_contents=f"INVALID LINE {secret}\n"
            )
            cases = (
                ("--env", "dev", "--format", "json"),
                ("--live", "--format", "json"),
                ("--live", "--env", "dev", "--format", "json"),
            )
            wrapper_results = {"team.sh": [], "team.ps1": []}
            for entry in ("team.sh", "team.ps1"):
                if entry.endswith("ps1") and not _test_subprocess_safety.working_pwsh():
                    continue
                launcher = [BASH] if entry.endswith("sh") else [_test_subprocess_safety.working_pwsh(), "-NoProfile", "-File"]
                for arguments in cases:
                    result = subprocess.run(
                        [*launcher, str(root / "scripts" / entry), "migration-plan", *arguments],
                        cwd=temporary, env=environment, capture_output=True, text=True, check=False,
                    )
                    with self.subTest(entry=entry, arguments=arguments):
                        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                        report = json.loads(result.stdout)
                        self.assertEqual(1, report["schemaVersion"])
                        self.assertIsNone(report["target"])
                        self.assertTrue(report["diagnostics"])
                        self.assertNotIn(secret, result.stdout + result.stderr)
                        self.assertNotIn("invalid line", result.stderr)
                        wrapper_results[entry].append((result.returncode, report))
            if wrapper_results["team.ps1"]:
                self.assertEqual(wrapper_results["team.sh"], wrapper_results["team.ps1"])
            self.assertFalse(calls.exists(), "invalid live options must refuse before SQLcl is launched")

    def test_live_report_uses_fake_sqlcl_only_for_version_gate(self) -> None:
        env_contents = "\n".join((
            "PROJECT_NAME=fixture",
            "DEVELOPER_NAME=ALICE",
            "DB_ENVIRONMENT=development",
            "APEX_APP_ID=100",
            "TABLES_SCHEMA=APP",
            "TABLES_PREFIXES=*",
            "TABLES_SQLCL_CONNECTION=dev-app",
            "TABLES_EXPECTED_USER=APP",
            "CODE_SCHEMA=APP",
            "CODE_PREFIXES=*",
            "CODE_SQLCL_CONNECTION=dev-app",
            "CODE_EXPECTED_USER=APP",
            "APEX_PARSING_SCHEMA=APP",
            "APEX_SQLCL_CONNECTION=dev-app",
            "APEX_EXPECTED_USER=APP",
            "",
        ))
        with tempfile.TemporaryDirectory() as temporary:
            root, environment, calls = self.migration_plan_checkout(Path(temporary), env_contents=env_contents)
            folder = root / "migrations" / "2026-10-13_live-version-floor-r001"
            folder.mkdir()
            (folder / "001-step.sql").write_text("SELECT 1 FROM dual;\n", encoding="utf-8", newline="\n")
            (folder / "checks.json").write_text(
                '{"schemaVersion":1,"preconditions":[],"postconditions":[{"id":"ready","sql":"SELECT 1 FROM dual","expected":1}]}\n',
                encoding="utf-8", newline="\n",
            )
            reports = []
            for entry in ("team.sh", "team.ps1"):
                if entry.endswith("ps1") and not _test_subprocess_safety.working_pwsh():
                    continue
                launcher = [BASH] if entry.endswith("sh") else [_test_subprocess_safety.working_pwsh(), "-NoProfile", "-File"]
                result = subprocess.run(
                    [*launcher, str(root / "scripts" / entry), "migration-plan", "migrations/2026-10-13_live-version-floor-r001", "--live", "--env", "dev", "--format", "json"],
                    cwd=temporary, env=environment, capture_output=True, text=True, check=False,
                )
                with self.subTest(entry=entry):
                    self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                    self.assertEqual("", result.stderr)
                    report = json.loads(result.stdout)
                    self.assertEqual("live", report["mode"])
                    self.assertIsNone(report["target"])
                    self.assertIsNone(report["preflight"])
                    self.assertIn("sqlcl-version-unavailable", [item["code"] for item in report["diagnostics"]])
                    reports.append(report)
            self.assertGreaterEqual(len(reports), 1)
            self.assertTrue(all(report == reports[0] for report in reports))
            self.assertTrue(calls.exists(), "valid live input must reach the fake SQLcl runtime gate")
            self.assertEqual(["-V"] * len(reports), calls.read_text(encoding="utf-8").splitlines())

    def test_query_command_is_exposed_and_rejects_non_select_before_loading_configuration(self) -> None:
        for entry in ("team.sh", "team.ps1"):
            if entry.endswith("ps1") and not _test_subprocess_safety.working_pwsh():
                continue
            launcher = [BASH] if entry.endswith("sh") else ["pwsh", "-NoProfile", "-File"]
            result = subprocess.run(
                [*launcher, str(ROOT / "scripts" / entry), "query", "--env", "dev", "--connection", "code", "UPDATE t SET x=1"],
                env=dict(os.environ, PROJECT_ENV_FILE="/no/such/project.env"),
                capture_output=True,
                text=True,
                check=False,
            )
            with self.subTest(entry=entry):
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("query error:", result.stderr)
                self.assertIn("SELECT", result.stderr)
                self.assertNotIn("configuration file not found", result.stderr)

    def test_application_lock_rejects_non_dev_before_loading_environment(self):
        for command in ('app-lock', 'app-unlock'):
            for entry in ('team.sh', 'team.ps1'):
                if entry.endswith('ps1') and not shutil.which('pwsh'):
                    continue
                launcher = [BASH] if entry.endswith('sh') else ['pwsh', '-NoProfile', '-File']
                result = subprocess.run([*launcher, str(ROOT/'scripts'/entry), command, '100', '--env', 'prod'],
                    env=dict(os.environ, PROJECT_ENV_FILE='/no/such/env'), capture_output=True, text=True)
                with self.subTest(command=command, entry=entry):
                    self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
                    self.assertIn('locks target DEV only', result.stderr)
                    self.assertNotIn('configuration file', result.stderr)

    def test_verify_local_dispatches_directly_without_loading_environment_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake_repo = Path(temporary) / "repo"
            fake_repo.mkdir()
            (fake_repo / "template-manifest.json").write_text(json.dumps({
                "schemaVersion": 2, "apexRelease": "26.2", "minimumSqlclVersion": "26.3.0.0",
                "upstream": "https://example.com/template.git", "templateOwned": [], "projectOwned": [], "templateOnly": []
            }), encoding="utf-8")
            (fake_repo / "apps").mkdir()
            secret = "SUPER_SECRET_TOKEN_9999"
            (fake_repo / ".env").write_text(f"INVALID_LINE {secret}\n", encoding="utf-8")
            for entry in ("team.sh", "team.ps1"):
                if entry.endswith("ps1") and not _test_subprocess_safety.working_pwsh():
                    continue
                launcher = [BASH] if entry.endswith("sh") else ["pwsh", "-NoProfile", "-File"]
                result = subprocess.run(
                    [*launcher, str(ROOT / "scripts" / entry), "verify-local", "--repo-root", str(fake_repo), "--format", "json"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                with self.subTest(entry=entry):
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertNotIn(secret, result.stderr)
                    self.assertNotIn(secret, result.stdout)
                    data = json.loads(result.stdout)
                    self.assertEqual(data["schemaVersion"], 1)
                    self.assertEqual(data["exitCode"], 2)

    def test_verify_local_clean_pythonpath_and_nonroot_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            # Run from nonrootcwd with clean PYTHONPATH
            clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
            for entry in ("team.sh", "team.ps1"):
                if entry.endswith("ps1") and not _test_subprocess_safety.working_pwsh():
                    continue
                launcher = [BASH] if entry.endswith("sh") else ["pwsh", "-NoProfile", "-File"]
                result = subprocess.run(
                    [*launcher, str(ROOT / "scripts" / entry), "verify-local", "--format", "json"],
                    cwd=temporary,
                    env=clean_env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                with self.subTest(entry=entry):
                    self.assertNotIn("ModuleNotFoundError", result.stderr)
                    self.assertIn('"schemaVersion": 1', result.stdout)
                    data = json.loads(result.stdout)
                    self.assertEqual(data["schemaVersion"], 1)

    def test_team_migrate_rejects_missing_duplicate_and_invalid_environment_before_loading_env(self) -> None:

        for arguments, expected in (
            (["migrations/2026-09-27_create-sample-r001"], "exactly one --env"),
            (["migrations/2026-09-27_create-sample-r001", "--env", "dev", "--env", "prod"], "exactly one --env"),
            (["migrations/2026-09-27_create-sample-r001", "--env", "production"], "--env must be dev, staging, or prod"),
        ):
            with self.subTest(arguments=arguments):
                environment = os.environ.copy()
                environment["PROJECT_ENV_FILE"] = "/no/such/migration-env-file"
                result = subprocess.run(
                    [BASH, str(ROOT / "scripts" / "team.sh"), "migrate", *arguments],
                    cwd=ROOT,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(expected, result.stderr)
                self.assertNotIn("configuration file not found", result.stderr)

    def test_powershell_migrate_uses_the_same_early_environment_validation(self) -> None:
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        environment = os.environ.copy()
        environment["PROJECT_ENV_FILE"] = "/no/such/migration-env-file"
        result = subprocess.run(
            [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"), "migrate", "migrations/2026-09-27_create-sample-r001"],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("specify exactly one --env", result.stderr)
        self.assertNotIn("configuration file not found", result.stderr)

    def test_local_check_conflicts_command_does_not_require_database_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = root / "migrations" / "2026-09-27_create-orders-r001"
            folder.mkdir(parents=True)
            (folder / "001-create-table.sql").write_text("CREATE TABLE ORDERS (ID NUMBER);\n", encoding="utf-8")
            (folder / "checks.json").write_text(
                '{"schemaVersion":1,"preconditions":[],"postconditions":[{"id":"ok","sql":"SELECT 1 FROM dual","expected":1}]}\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                [BASH, str(ROOT / "scripts" / "team.sh"), "check-conflicts", f"migrations/{folder.name}", "--local", "--repo-root", str(root)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Local selected-batch analysis only", result.stdout)

    def test_publish_command_routes_non_dev_targets_to_deploy(self) -> None:
        result = subprocess.run(
            [BASH, str(ROOT / "scripts" / "team.sh"), "publish", "100", "--env", "prod"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("use deploy for staging or production", result.stderr)

    def test_bash_environment_loader_accepts_dollar_and_hash_oracle_identifiers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / ".env"
            content = (ROOT / ".env.example").read_text(encoding="utf-8")
            content = content.replace("CODE_SCHEMA=DEMO", "CODE_SCHEMA=DEMO$")
            content = content.replace("CODE_EXPECTED_USER=DEMO", "CODE_EXPECTED_USER=DEMO#")
            content = content.replace("CODE_PREFIXES=*", "CODE_PREFIXES=AB$,XY#")
            content = content.replace("APEX_PARSING_SCHEMA=DEMO", "APEX_PARSING_SCHEMA=APP$")
            environment.write_text(content, encoding="utf-8")

            result = self.run_bash_env_loader(environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "DEMO$|DEMO#|AB$,XY#|APP$\n")

    def test_bash_environment_loader_accepts_utf8_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / ".env"
            example = (ROOT / ".env.example").read_bytes()
            environment.write_bytes(b"\xef\xbb\xbf" + example)

            result = self.run_bash_env_loader(environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "DEMO|DEMO|*|DEMO\n")

    def test_bash_environment_loader_keeps_schema_separate_from_target_login(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / ".env"
            content = (ROOT / ".env.example").read_text(encoding="utf-8")
            content += (
                "\nSTAGING_SQLCL_CONNECTION=stage-db\n"
                "STAGING_EXPECTED_USER=STAGE_DEPLOYER\n"
                "STAGING_SCHEMA=APP_STAGE\n"
            )
            environment.write_text(content, encoding="utf-8")
            result = subprocess.run(
                [
                    BASH, "-c",
                    'set -e; source "$1" "$2"; printf "%s|%s|%s\\n" "$STAGING_SQLCL_CONNECTION" "$STAGING_EXPECTED_USER" "$STAGING_SCHEMA"',
                    BASH, str(ROOT / "scripts" / "load_env.sh"), str(environment),
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "stage-db|STAGE_DEPLOYER|APP_STAGE\n")

    def test_bash_environment_loader_requires_a_connection_pair_for_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / ".env"
            content = (ROOT / ".env.example").read_text(encoding="utf-8")
            environment.write_text(content + "\nSTAGING_SCHEMA=APP_STAGE\n", encoding="utf-8")
            result = subprocess.run(
                [BASH, "-c", 'source "$1" "$2"', "bash", str(ROOT / "scripts" / "load_env.sh"), str(environment)],
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("STAGING_SCHEMA requires", result.stderr)

    def test_bash_environment_loader_clears_inherited_optional_schemas(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / ".env"
            environment.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
            result = subprocess.run(
                [
                    BASH, "-c",
                    'export STAGING_SCHEMA=INHERITED PROD_SCHEMA=INHERITED; source "$1" "$2"; printf "%s|%s\\n" "${STAGING_SCHEMA-}" "${PROD_SCHEMA-}"',
                    BASH, str(ROOT / "scripts" / "load_env.sh"), str(environment),
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "|\n")

    def test_powershell_loader_accepts_and_clears_target_schemas(self) -> None:
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / ".env"
            content = (ROOT / ".env.example").read_text(encoding="utf-8")
            content += "\nSTAGING_SQLCL_CONNECTION=stage-db\nSTAGING_EXPECTED_USER=STAGE_DEPLOYER\nSTAGING_SCHEMA=APP_STAGE$\n"
            environment.write_text(content, encoding="utf-8")
            probe = Path(temporary) / "load-env-probe.ps1"
            probe.write_text(
                'param([string]$Loader, [string]$EnvironmentFile)\n'
                '$env:STAGING_SCHEMA = "INHERITED"\n'
                '. $Loader -EnvFile $EnvironmentFile\n'
                'Write-Output "$($env:STAGING_SCHEMA)|$($env:PROD_SCHEMA)"\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                [pwsh, "-NoProfile", "-File", str(probe), str(ROOT / "scripts" / "load_env.ps1"), str(environment)],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "APP_STAGE$|")

    def test_both_loaders_reject_a_db_environment_that_is_not_exactly_lowercase(self) -> None:
        # PowerShell's -in is case-insensitive; the loaders must still agree.
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        expected = "DB_ENVIRONMENT must be development, test, staging, or production"
        for value in ("Development", "PRODUCTION", "Test"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                environment = Path(temporary) / ".env"
                lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
                environment.write_text(
                    "\n".join(f"DB_ENVIRONMENT={value}" if line.startswith("DB_ENVIRONMENT=") else line for line in lines) + "\n",
                    encoding="utf-8",
                )
                bash_result = self.run_bash_env_loader(environment)
                powershell_result = subprocess.run(
                    [pwsh, "-NoProfile", "-Command", f". '{ROOT / 'scripts' / 'load_env.ps1'}' -EnvFile '{environment}'"],
                    text=True, capture_output=True, check=False,
                )
                self.assertNotEqual(bash_result.returncode, 0)
                self.assertIn(expected, bash_result.stderr)
                self.assertNotEqual(powershell_result.returncode, 0, powershell_result.stdout)
                # PowerShell's error view colours the message and wraps it at the console width.
                unwrapped = re.sub(r"\s*\n\s*\|?\s*", " ", re.sub(r"\x1b\[[0-9;]*m", "", powershell_result.stderr))
                self.assertIn(expected, unwrapped)

    def test_bash_loader_matches_ascii_only_whatever_the_users_locale(self) -> None:
        # In en_US.UTF-8, Bash's [A-Z] and [A-Za-z] match accented letters, so
        # the loader accepted values that the PowerShell loader and the Python
        # resolver reject (an accented DEVELOPER_NAME then failed at publish).
        listed = subprocess.run(["locale", "-a"], text=True, capture_output=True, check=False).stdout.lower()
        if "en_us.utf8" not in listed and "en_us.utf-8" not in listed:
            self.skipTest("the en_US.UTF-8 locale is not installed")
        pwsh = _test_subprocess_safety.working_pwsh()
        for key, value in (
            ("DEVELOPER_NAME", "\u00c9RIC"),
            ("CODE_SQLCL_CONNECTION", "pr\u00fcfung"),
            ("CODE_SCHEMA", "D\u00c9MO"),
            ("TABLES_PREFIXES", "\u00dc_"),
        ):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as temporary:
                environment = Path(temporary) / ".env"
                lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
                environment.write_text(
                    "\n".join(f"{key}={value}" if line.startswith(f"{key}=") else line for line in lines) + "\n",
                    encoding="utf-8",
                )
                scripts = Path(temporary) / "scripts"
                scripts.mkdir()
                shutil.copy2(ROOT / "scripts" / "load_env.sh", scripts / "load_env.sh")
                bash_result = subprocess.run(
                    [BASH, "-c", 'set -e; source "$1" "$2"; echo accepted', "bash", str(scripts / "load_env.sh"), str(environment)],
                    text=True, capture_output=True, check=False,
                    env={**os.environ, "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"},
                )
                self.assertNotEqual(bash_result.returncode, 0, f"{key}={value!r} was accepted: {bash_result.stdout}")
                self.assertIn(key, bash_result.stderr)
                if pwsh is not None:
                    powershell_result = subprocess.run(
                        [pwsh, "-NoProfile", "-Command", f". '{ROOT / 'scripts' / 'load_env.ps1'}' -EnvFile '{environment}'"],
                        text=True, capture_output=True, check=False,
                    )
                    self.assertNotEqual(powershell_result.returncode, 0, powershell_result.stdout)

    def test_both_loaders_refuse_a_utf16_env_file_with_a_clear_message(self) -> None:
        # Windows PowerShell 5.1 writes UTF-16 for `>` and Out-File. PowerShell
        # decoded it, Bash read garbage: doctor passed and migrate then failed
        # with an unreadable "invalid line" message.
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        for label, content in (
            ("utf-16-le", b"\xff\xfe" + text.encode("utf-16-le")),
            ("utf-16-be", b"\xfe\xff" + text.encode("utf-16-be")),
        ):
            with self.subTest(encoding=label), tempfile.TemporaryDirectory() as temporary:
                environment = Path(temporary) / ".env"
                environment.write_bytes(content)
                bash_result = self.run_bash_env_loader(environment)
                powershell_result = subprocess.run(
                    [pwsh, "-NoProfile", "-Command", f". '{ROOT / 'scripts' / 'load_env.ps1'}' -EnvFile '{environment}'"],
                    text=True, capture_output=True, check=False,
                )
                for name, result in (("bash", bash_result), ("powershell", powershell_result)):
                    self.assertNotEqual(result.returncode, 0, f"{name} accepted a {label} file: {result.stdout}")
                    # PowerShell wraps its error text across lines and colours it.
                    plain = " ".join(re.sub(r"\x1b\[[0-9;]*m|\|", " ", result.stderr).split())
                    self.assertIn("is UTF-16; save it as UTF-8", plain, f"{name}: {result.stderr}")

    def test_a_leading_option_is_an_unknown_command_in_both_wrappers(self) -> None:
        # PowerShell's parameter binder keeps option-like tokens out of $Command,
        # so `team.ps1 --schema=DEMO doctor` printed usage and exited 0, silently
        # dropping the command. Bash says unknown command; so must PowerShell.
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        environment = {**os.environ, "PROJECT_ENV_FILE": "/no/such/env"}
        for arguments in (["--schema=DEMO", "doctor"], ["--schema", "-1", "doctor"], ["-x", "doctor"]):
            with self.subTest(arguments=arguments):
                bash_result = subprocess.run([BASH, str(ROOT / "scripts" / "team.sh"), *arguments], env=environment,
                                             text=True, capture_output=True, check=False, cwd=ROOT)
                powershell_result = subprocess.run([pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"), *arguments],
                                                   env=environment, text=True, capture_output=True, check=False, cwd=ROOT)
                self.assertNotEqual(bash_result.returncode, 0)
                self.assertIn("unknown command", bash_result.stderr)
                self.assertNotEqual(powershell_result.returncode, 0, powershell_result.stdout)
                self.assertIn("unknown command", re.sub(r"\x1b\[[0-9;]*m", "", powershell_result.stderr))

    def test_both_wrappers_still_print_usage_for_no_command_and_for_help(self) -> None:
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        for arguments in ([], ["--help"], ["-h"]):
            with self.subTest(arguments=arguments):
                for command in ([BASH, str(ROOT / "scripts" / "team.sh")], [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1")]):
                    result = subprocess.run([*command, *arguments], text=True, capture_output=True, check=False, cwd=ROOT)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("Usage: scripts/team.", result.stdout)

    def test_feature_help_is_wired_through_both_wrappers(self) -> None:
        entries = [[BASH, str(ROOT / "scripts" / "team.sh")]]
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh:
            entries.append([pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1")])
        for command in entries:
            with self.subTest(entry=command[-1]):
                result = subprocess.run(
                    [*command, "feature", "--help"],
                    text=True, capture_output=True, check=False, cwd=ROOT,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                for operation in ("init", "capture", "diff", "freeze", "clean-capsule"):
                    self.assertIn(operation, result.stdout)

    def test_new_migration_is_listed_and_needs_arguments_in_both_wrappers(self) -> None:
        entries = [[BASH, str(ROOT / "scripts" / "team.sh")]]
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh:
            entries.append([pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1")])
        for command in entries:
            with self.subTest(entry=command[-1]):
                help_text = subprocess.run([*command, "--help"], text=True, capture_output=True, check=False, cwd=ROOT).stdout
                self.assertIn("new-migration <name> --source TYPE:NAME [...] --env dev", help_text)
                usage = subprocess.run([*command, "new-migration"], text=True, capture_output=True, check=False, cwd=ROOT)
                self.assertNotEqual(usage.returncode, 0)
                self.assertIn("new-migration <name> --source TYPE:NAME", usage.stderr)

    def test_no_command_lists_only_everyday_commands_and_help_lists_all(self) -> None:
        entries = [[BASH, str(ROOT / "scripts" / "team.sh")]]
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh:
            entries.append([pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1")])
        for command in entries:
            with self.subTest(entry=command[-1]):
                short = subprocess.run(command, text=True, capture_output=True, check=False, cwd=ROOT)
                self.assertEqual(short.returncode, 0, short.stderr)
                self.assertIn("Everyday commands:", short.stdout)
                for everyday in ("doctor", "export <app_id>", "migrate <folder>", "revise <folder>", "backup-db"):
                    self.assertIn(everyday, short.stdout)
                self.assertNotIn("compare-env", short.stdout)
                self.assertIn("--help for every command", short.stdout)
                full = subprocess.run([*command, "--help"], text=True, capture_output=True, check=False, cwd=ROOT)
                self.assertEqual(full.returncode, 0, full.stderr)
                self.assertIn("compare-env", full.stdout)
                self.assertNotIn("Everyday commands:", full.stdout)

    def test_argument_errors_read_and_exit_the_same_in_both_wrappers(self) -> None:
        # An uncaught throw makes PowerShell print an exception block with the
        # script path and line and exit 1; Bash prints one `team error:` line and
        # exits 2, which scripts that call the wrappers can rely on.
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        environment = {**os.environ, "PROJECT_ENV_FILE": "/no/such/env"}
        for arguments in (
            ["export"], ["export", "bad"], ["export", "1", "2"],
            ["doctor", "extra"], ["doctor", "--schema"], ["doctor", "--schema", "lowercase"],
            ["publish"], ["publish", "1", "--env"], ["publish", "1", "--env", "prod"],
            ["check-conflicts"], ["migrate"], ["compare-schema"], ["deploy"],
            ["backup-db", "extra"], ["no-such-command"],
        ):
            with self.subTest(arguments=arguments):
                bash_result = subprocess.run([BASH, str(ROOT / "scripts" / "team.sh"), *arguments], env=environment,
                                             text=True, capture_output=True, check=False, cwd=ROOT)
                powershell_result = subprocess.run([pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"), *arguments],
                                                   env=environment, text=True, capture_output=True, check=False, cwd=ROOT)
                self.assertEqual(bash_result.returncode, 2, bash_result.stderr)
                self.assertEqual(powershell_result.returncode, 2, powershell_result.stderr)
                self.assertEqual(
                    re.sub(r"\x1b\[[0-9;]*m", "", powershell_result.stderr).strip(),
                    bash_result.stderr.replace("team.sh", "team.ps1").strip(),
                )

    def test_a_refusal_raised_inside_a_helper_script_is_one_line_with_bash_s_status_in_both_wrappers(self) -> None:
        # check_db_target.ps1 and load_env.ps1 refuse by throwing a string, which
        # pwsh prints as an exception block (script path, line, caret) and turns
        # into status 1; Bash prints the message alone and the guard exits 2.
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        template = (ROOT / ".env.example").read_text(encoding="utf-8")
        for label, replacements, expected_status, expected_text in (
            ("a connection that resembles production", {"APEX_SQLCL_CONNECTION=docker-demo": "APEX_SQLCL_CONNECTION=docker-prod"}, 2,
             "resembles production but DB_ENVIRONMENT=development"),
            ("an invalid DB_ENVIRONMENT", {"DB_ENVIRONMENT=development": "DB_ENVIRONMENT=Development"}, 1,
             "DB_ENVIRONMENT must be development, test, staging, or production"),
        ):
            with self.subTest(label), tempfile.TemporaryDirectory() as temporary:
                environment_file = Path(temporary) / ".env"
                content = template
                for old, new_value in replacements.items():
                    self.assertIn(old, content)
                    content = content.replace(old, new_value)
                environment_file.write_text(content, encoding="utf-8")
                environment = {**os.environ, "PROJECT_ENV_FILE": str(environment_file)}
                results = {}
                for name, command in (("bash", [BASH, str(ROOT / "scripts" / "team.sh"), "doctor"]),
                                      ("powershell", [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"), "doctor"])):
                    result = subprocess.run(command, env=environment, text=True, capture_output=True, check=False, cwd=ROOT)
                    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stderr)
                    self.assertEqual(result.returncode, expected_status, f"{name}: {plain}")
                    self.assertIn(expected_text, " ".join(plain.split()), name)
                    results[name] = plain
                self.assertNotIn("Exception:", results["powershell"])
                self.assertNotIn("Line |", results["powershell"])

    @unittest.skipIf(os.name == "nt", "needs POSIX process groups and signals (preexec_fn, os.killpg); Windows twin: test_ctrl_c_in_a_windows_console_ends_a_live_preflight_and_everything_it_started")
    def test_powershell_exits_130_when_interrupted_while_a_helper_runs(self) -> None:
        # Ctrl-C stops PowerShell's own pipeline as well as the helper, so the
        # line that passes the helper's status on never ran and pwsh exited 0,
        # which a caller reads as success. Bash exits 130.
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = root / "started"
            fake_bash = root / "git-bash"
            fake_bash.write_text(f"#!/bin/sh\n: > '{started}'\nsleep 30\n", encoding="utf-8")
            fake_bash.chmod(0o755)
            process = subprocess.Popen(
                [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"), "compare-schema", "--env", "dev", "--object", "T"],
                cwd=ROOT, env={**os.environ, "TEAM_BASH": str(fake_bash)},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                # A background job in a non-interactive shell inherits SIGINT as ignored.
                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
            )
            try:
                deadline = time.monotonic() + 60
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(started.exists(), "the helper never started")
                os.killpg(process.pid, signal.SIGINT)
                process.communicate(timeout=60)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
            self.assertEqual(process.returncode, 130)

    @unittest.skipIf(os.name == "nt", "pwsh on Linux and macOS ends the tree with Kill(true); Windows twin: SqlclProcessTreeTests in test_windows_support.py")
    def test_powershell_ends_the_whole_sqlcl_tree_when_sqlcl_ignores_ctrl_c(self) -> None:
        # After Ctrl-C, Invoke-Sqlcl gives SQLcl ten seconds and then kills it. Off Windows
        # that killed only the launcher it started, and whatever the launcher had started
        # (the JVM) carried on, orphaned.
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started, child_pid = root / "started", root / "child-pid"
            fake_bin = root / "bin"
            fake_sqlcl.install(
                fake_bin,
                "#!/bin/sh\ntrap '' INT\n"
                f"sh -c 'echo $$ > \"{child_pid}\"; exec sleep 60' &\n"
                f": > '{started}'\nwait\n",
            )
            env_file = root / ".env"
            env_file.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
            process = subprocess.Popen(
                [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"), "doctor"],
                cwd=root, env=fake_sqlcl.environment(fake_bin, PROJECT_ENV_FILE=str(env_file)),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
            )
            child = None
            try:
                deadline = time.monotonic() + 60
                while not (started.exists() and child_pid.exists() and child_pid.read_text().strip()) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(started.exists(), "SQLcl never started")
                child = int(child_pid.read_text())
                # SQLcl can start before Start-Process has handed it to PowerShell; a Ctrl-C
                # inside Start-Process cannot be handled, so let it return first.
                time.sleep(1)
                interrupted_at = time.monotonic()
                os.killpg(process.pid, signal.SIGINT)
                output, errors = process.communicate(timeout=60)
                elapsed = time.monotonic() - interrupted_at
                time.sleep(0.5)
                try:
                    os.kill(child, 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                state = Path(f"/proc/{child}/stat").read_text().split(")")[-1].split()[0] if alive and Path(f"/proc/{child}/stat").exists() else "-"
                self.assertFalse(alive, f"the process SQLcl started outlived the interrupt (state {state}, pwsh exit {process.returncode} after {elapsed:.1f} s: {output}{errors})")
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
                if child is not None:
                    try:
                        os.kill(child, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    @unittest.skipIf(os.name == "nt", "needs POSIX process groups and signals (preexec_fn, os.killpg); Windows twin: test_ctrl_c_in_a_windows_console_waits_for_python_and_passes_its_status_on")
    def test_powershell_passes_on_the_status_a_helper_chose_when_it_was_interrupted(self) -> None:
        # migrate reports "interrupted and may be partially applied" with status 2,
        # not 130; the wrapper must not flatten that into a plain interrupt.
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = root / "started"
            fake_bash = root / "git-bash"
            fake_bash.write_text(
                f"#!/bin/sh\ntrap 'exit 2' INT\n: > '{started}'\nsleep 30\n", encoding="utf-8")
            fake_bash.chmod(0o755)
            process = subprocess.Popen(
                [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"), "migrate", "migrations/x", "--env", "dev"],
                cwd=ROOT, env={**os.environ, "TEAM_BASH": str(fake_bash)},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
            )
            try:
                deadline = time.monotonic() + 60
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(started.exists(), "the helper never started")
                os.killpg(process.pid, signal.SIGINT)
                process.communicate(timeout=60)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
            self.assertEqual(process.returncode, 2)

    def test_the_two_usage_screens_match_apart_from_the_script_name(self) -> None:
        # The PowerShell screen once left out `--help`, an option it accepts.
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        bash_usage = subprocess.run([BASH, str(ROOT / "scripts" / "team.sh"), "--help"],
                                    text=True, capture_output=True, check=False, cwd=ROOT)
        powershell_usage = subprocess.run([pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"), "--help"],
                                          text=True, capture_output=True, check=False, cwd=ROOT)
        self.assertEqual(bash_usage.returncode, 0, bash_usage.stderr)
        self.assertEqual(powershell_usage.returncode, 0, powershell_usage.stderr)
        self.assertEqual(
            [line.rstrip() for line in bash_usage.stdout.replace("team.sh", "team.ps1").splitlines()],
            [line.rstrip() for line in powershell_usage.stdout.splitlines()],
        )

    def test_powershell_runs_bash_helpers_with_the_bash_named_by_team_bash(self) -> None:
        # On Windows the first bash on PATH may be the WSL launcher, or absent
        # when Git for Windows keeps only its cmd directory on PATH.
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = root / "record.txt"
            if os.name == "nt":
                # PowerShell cannot run an extensionless script; a batch file records the same lines.
                fake_bash = root / "git-bash.cmd"
                fake_bash.write_text(
                    "@echo off\r\n"
                    # team.ps1 asks Bash whether python3 works (bash -c "python3 ..."): answer yes, record nothing.
                    'if "%~1"=="-c" exit /b 0\r\n'
                    f'break > "{record}"\r\n'
                    ":loop\r\n"
                    'if "%~1"=="" goto done\r\n'
                    f'>>"{record}" echo %~1\r\n'
                    "shift\r\n"
                    "goto loop\r\n"
                    ":done\r\n",
                    encoding="ascii",
                    newline="",
                )
            else:
                fake_bash = root / "git-bash"
                fake_bash.write_text(
                    "#!/bin/sh\n"
                    f"printf '%s\\n' \"$@\" > '{record}'\n",
                    encoding="utf-8",
                )
                fake_bash.chmod(0o755)
            environment = os.environ.copy()
            environment["TEAM_BASH"] = str(fake_bash)
            environment["PATH"] = str(root / "no-bash-here")
            result = subprocess.run(
                [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"), "compare-schema", "--env", "dev", "--object", "CUSTOMERS"],
                cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            seen = record.read_text(encoding="utf-8").splitlines()
            self.assertEqual(seen[0], str(ROOT / "scripts" / "compare_schema.sh"))
            self.assertEqual(seen[1:], ["--env", "dev", "--object", "CUSTOMERS"])

    @unittest.skipUnless(os.name == "nt", "the signal status is Git Bash's convention on Windows")
    def test_powershell_reports_git_bash_signal_statuses_as_bash_does(self) -> None:
        # A Git Bash process that a signal ended exits with the signal number times 256
        # (512 for Ctrl-C); Bash itself says 128 plus the signal (130). Plain statuses stay.
        engines = [path for path in (_test_subprocess_safety.working_pwsh(), _test_subprocess_safety.working_windows_powershell()) if path]
        for engine in engines:
            for status, expected in ((512, 130), (3840, 143), (2, 2), (1, 1)):
                with self.subTest(engine=Path(engine).name, status=status), tempfile.TemporaryDirectory() as temporary:
                    fake_bash = Path(temporary) / "git-bash.cmd"
                    # team.ps1 asks Bash whether python3 works (bash -c "python3 ..."): answer yes.
                    fake_bash.write_text(
                        f'@echo off\r\nif "%~1"=="-c" exit /b 0\r\nexit /b {status}\r\n', encoding="ascii", newline=""
                    )
                    result = subprocess.run(
                        [engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "team.ps1"),
                         "compare-schema", "--env", "dev", "--object", "T"],
                        cwd=ROOT, env={**os.environ, "TEAM_BASH": str(fake_bash)}, text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)

    def test_powershell_names_the_fix_when_no_usable_bash_is_found(self) -> None:
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        environment = os.environ.copy()
        set_environment_value(environment, "TEAM_BASH", None)
        set_environment_value(environment, "PATH", str(Path(pwsh).parent))  # pwsh itself, but no bash
        if shutil.which("bash", path=environment["PATH"]):
            self.skipTest("bash lives next to pwsh on this machine")
        with tempfile.TemporaryDirectory() as temporary:
            empty = Path(temporary)
            env_file = empty / ".env"
            shutil.copy2(ROOT / ".env.example", env_file)
            environment["PROJECT_ENV_FILE"] = str(env_file)
            if os.name == "nt":
                # team.ps1 also looks for Git for Windows under Program Files and LOCALAPPDATA\Programs.
                for name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
                    set_environment_value(environment, name, str(empty))
            result = subprocess.run(
                [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"), "migrate", "migrations/x", "--env", "dev"],
                cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TEAM_BASH", result.stderr + result.stdout)

    def test_doctor_uses_read_only_identity_sqlcl_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            for name in ("team.sh", "load_env.sh", "check_db_target.sh", "doctor.sql", "sqlcl_safe.sh", "apex_compatibility.py", "sqlcl_version.py"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            (root / "scripts" / "verify_db_access.sql").write_text(
                "PROMPT identity checked\n", encoding="utf-8"
            )
            (root / ".env").write_text((ROOT / ".env.example").read_text(encoding="utf-8"))
            fake_bin = root / "bin"
            fake_bin.mkdir()
            sql_log = root / "sql-args.txt"
            fake_sql = fake_bin / "sql"
            fake_sql.write_text(
                "#!/usr/bin/env bash\n"
                "cat > /dev/null\n"
            "if [[ ${1:-} == -V ]]; then printf 'SQLcl: Release 26.3 Production\\n'; exit 0; fi\n"
                "printf '%s\\n' \"$@\" > \"$FAKE_SQL_LOG\"\n"
                "if [[ -f login.sql ]]; then touch \"$FAKE_LOGIN_MARKER\"; exit 0; fi\n"
                "if [[ -n \"${SQLPATH:-}\" && -f \"$SQLPATH/login.sql\" ]]; then touch \"$FAKE_LOGIN_MARKER\"; exit 0; fi\n"
                "printf 'DATABASE_VERSION_VERIFIED:23.26.0.0.0:23.0.0\\nAPEX_DOCTOR_VERIFIED:DEMO\\nAPEX_RELEASE_VERIFIED:26.2\\nAPEX_WORKSPACE_USERS_VERIFIED\\n'\n",
                encoding="utf-8",
            )
            fake_sql.chmod(0o755)
            fake_sqlcl.add_launcher(fake_bin)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SQL_LOG"] = str(sql_log)
            login_marker = root / "login-marker"
            environment["FAKE_LOGIN_MARKER"] = str(login_marker)
            environment["PROJECT_ENV_FILE"] = str(root / ".env")
            (root / "login.sql").write_text("HOST touch should-not-run\n", encoding="utf-8")
            malicious_sqlpath = root / "malicious-sqlpath"
            malicious_sqlpath.mkdir()
            (malicious_sqlpath / "login.sql").write_text("HOST touch should-not-run\n", encoding="utf-8")
            environment["SQLPATH"] = str(malicious_sqlpath)

            result = subprocess.run(
                [BASH, str(scripts / "team.sh"), "doctor"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(login_marker.exists(), "SQLcl must not start in the caller's directory")
            args = sql_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("docker-demo", args)
            self.assertIn("@" + fake_sqlcl.native((scripts / "doctor.sql").resolve()), args)
            doctor_sql = (scripts / "doctor.sql").read_text(encoding="utf-8")
            self.assertIn("WHENEVER SQLERROR EXIT FAILURE ROLLBACK", doctor_sql)
            self.assertIn("@@verify_db_access.sql", doctor_sql)

    @unittest.skipIf(os.name == "nt", "needs POSIX process groups and signals (preexec_fn, os.killpg); Windows twin: test_ctrl_c_in_a_windows_console_ends_doctor_and_removes_its_working_directory")
    def test_interrupting_doctor_removes_its_working_directory(self) -> None:
        # Doctor makes scratch/sqlcl-doctor.* for SQLcl; Ctrl-C used to end the
        # script before the line that removes it.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            for name in ("team.sh", "load_env.sh", "check_db_target.sh", "doctor.sql", "sqlcl_safe.sh", "apex_compatibility.py", "sqlcl_version.py"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            (scripts / "verify_db_access.sql").write_text("PROMPT identity checked\n", encoding="utf-8")
            (root / ".env").write_text((ROOT / ".env.example").read_text(encoding="utf-8"))
            fake_bin = root / "bin"
            fake_bin.mkdir()
            started = root / "sql-started"
            fake_sql = fake_bin / "sql"
            fake_sql.write_text(f"#!/bin/sh\n: > '{started}'\nsleep 30\n", encoding="utf-8")
            fake_sql.chmod(0o755)
            fake_sqlcl.add_launcher(fake_bin)
            environment = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", "PROJECT_ENV_FILE": str(root / ".env")}
            process = subprocess.Popen(
                [BASH, str(scripts / "team.sh"), "doctor"], cwd=root, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                # A background job in a non-interactive shell inherits SIGINT as ignored.
                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
            )
            try:
                deadline = time.monotonic() + 60
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(started.exists(), "SQLcl never started")
                os.killpg(process.pid, signal.SIGINT)
                process.communicate(timeout=60)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
            self.assertEqual(process.returncode, 130)
            self.assertEqual(sorted(path.name for path in (root / "scratch").glob("sqlcl-doctor.*")), [])

    @unittest.skipIf(os.name == "nt", "needs POSIX process groups and signals (preexec_fn, os.killpg); Windows twin: test_ctrl_c_in_a_windows_console_ends_a_live_preflight_and_everything_it_started")
    def test_interrupting_a_live_preflight_reports_that_nothing_changed_and_exits_130(self) -> None:
        # check-conflicts --env only reads, so Ctrl-C while SQLcl runs ends it with
        # one line, status 130, no traceback and no scratch directory.
        pwsh = _test_subprocess_safety.working_pwsh()
        wrappers = [[BASH, "team.sh"]] + ([[pwsh, "-NoProfile", "-File", "team.ps1"]] if pwsh else [])
        for wrapper in wrappers:
            with self.subTest(wrapper=Path(wrapper[0]).name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                shutil.copytree(ROOT / "scripts", root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
                (root / ".env").write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
                folder = root / "migrations" / "2026-10-02_interrupt-r001"
                folder.mkdir(parents=True)
                (folder / "001-create.sql").write_text("CREATE TABLE ZZ_INTERRUPT (ID NUMBER);\n", encoding="utf-8")
                (folder / "checks.json").write_text(
                    json.dumps({"schemaVersion": 1, "preconditions": [], "postconditions": [{"id": "ok", "sql": "SELECT 1 FROM dual", "expected": 1}]}) + "\n",
                    encoding="utf-8",
                )
                fake_bin = root / "bin"
                fake_bin.mkdir()
                started = root / "sql-started"
                fake_sql = fake_bin / "sql"
                fake_sql.write_text(f"#!/bin/sh\n: > '{started}'\nsleep 30\n", encoding="utf-8")
                fake_sql.chmod(0o755)
                fake_sqlcl.add_launcher(fake_bin)
                environment = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", "PROJECT_ENV_FILE": str(root / ".env")}
                command = [*wrapper[:-1], str(root / "scripts" / wrapper[-1]), "check-conflicts", "migrations/2026-10-02_interrupt-r001", "--env", "dev"]
                process = subprocess.Popen(
                    command, cwd=root, env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                    # A background job in a non-interactive shell inherits SIGINT as ignored.
                    preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
                )
                try:
                    deadline = time.monotonic() + 60
                    while not started.exists() and time.monotonic() < deadline:
                        time.sleep(0.05)
                    self.assertTrue(started.exists(), "SQLcl never started")
                    os.killpg(process.pid, signal.SIGINT)
                    _, stderr = process.communicate(timeout=60)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                self.assertEqual(process.returncode, 130, stderr)
                self.assertEqual(stderr.strip(), "preflight interrupted; it only reads, so nothing was changed")
                self.assertEqual(sorted(path.name for path in (root / "scratch").glob("*")), [])

    @unittest.skipIf(os.name == "nt", "on Windows Python runs as a child on purpose: Git Bash ends itself on Ctrl-C (see check_conflicts.sh)")
    def test_helper_scripts_hand_their_process_to_python_off_windows(self) -> None:
        # With exec Python keeps the script's PID, so a signal sent to the script alone (timeout, kill,
        # a supervisor that signals its child only) reaches it. As a child it would be orphaned and carry
        # on: a migration that was "stopped" would go on applying.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "scripts", root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
            (root / ".env").write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
            record = root / "python-pid"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_python = fake_bin / "python3"
            fake_python.write_text('#!/bin/sh\ncase "$*" in "-m scripts."*) echo $$ > "$PYTHON_PID_FILE" ;; esac\n', encoding="utf-8")
            fake_python.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "PROJECT_ENV_FILE": str(root / ".env"),
                "PYTHON_PID_FILE": str(record),
            }
            folder = "migrations/2026-10-02_exec-r001"
            for script, arguments in (
                ("check_conflicts.sh", [folder, "--local"]),
                ("check_conflicts.sh", [folder, "--env", "dev"]),
                ("migrate.sh", [folder, "--env", "dev"]),
                ("compare_schema.sh", []),
                # team.sh hands its process on too, or a signal sent to team.sh alone
                # would leave the helper (and its migration) running.
                ("team.sh", ["check-conflicts", folder, "--local"]),
                ("team.sh", ["migrate", folder, "--env", "dev"]),
                ("team.sh", ["compare-schema", "--env", "dev", "--object", "X"]),
            ):
                with self.subTest(script=script, arguments=arguments):
                    record.unlink(missing_ok=True)
                    process = subprocess.Popen(
                        [BASH, str(root / "scripts" / script), *arguments], cwd=root, env=environment,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                    _, stderr = process.communicate(timeout=60)
                    self.assertTrue(record.exists(), f"python3 was never started: {stderr}")
                    self.assertEqual(int(record.read_text()), process.pid)

    @staticmethod
    def windows_wrappers() -> list[list[str]]:
        """Every way to start team on Windows: Git Bash, and each PowerShell that is installed."""
        wrappers = [[BASH, "team.sh"]]
        windows_powershell = _test_subprocess_safety.working_windows_powershell()
        if windows_powershell:
            wrappers.append([windows_powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "team.ps1"])
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh:
            wrappers.append([pwsh, "-NoProfile", "-File", "team.ps1"])
        return wrappers

    def make_windows_interrupt_checkout(self, root: Path) -> None:
        shutil.copytree(ROOT / "scripts", root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        (root / ".env").write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")

    @unittest.skipUnless(os.name == "nt", "presses Ctrl-C in a Windows console; the POSIX twin is above")
    def test_ctrl_c_in_a_windows_console_ends_a_live_preflight_and_everything_it_started(self) -> None:
        # The keypress reaches every process on the console, but Python waits for its
        # child where the keypress cannot interrupt it, and the Git Bash processes
        # below it do not take it. The preflight used to run on, with SQLcl under it,
        # until SQLcl ended by itself.
        for wrapper in self.windows_wrappers():
            with self.subTest(wrapper=Path(wrapper[0]).name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.make_windows_interrupt_checkout(root)
                folder = root / "migrations" / "2026-10-02_interrupt-r001"
                folder.mkdir(parents=True)
                (folder / "001-create.sql").write_text("CREATE TABLE ZZ_INTERRUPT (ID NUMBER);\n", encoding="utf-8")
                (folder / "checks.json").write_text(
                    json.dumps({"schemaVersion": 1, "preconditions": [], "postconditions": [{"id": "ok", "sql": "SELECT 1 FROM dual", "expected": 1}]}) + "\n",
                    encoding="utf-8",
                )
                started = root / "sql-started"
                sql_pid = root / "sql-pid"
                # /proc/$$/winpid is the Windows process id of the script that stands for SQLcl.
                fake_bin = fake_sqlcl.install(
                    root / "bin", f"cat /proc/$$/winpid > '{sql_pid.as_posix()}'\n: > '{started.as_posix()}'\nsleep 60\n"
                )
                environment = fake_sqlcl.environment(fake_bin, PROJECT_ENV_FILE=str(root / ".env"))
                command = [*wrapper[:-1], str(root / "scripts" / wrapper[-1]), "check-conflicts", "migrations/2026-10-02_interrupt-r001", "--env", "dev"]
                process, screen, sql_survived = _console_ctrl_c.interrupt(command, root, environment, started, sql_pid)
                self.assertEqual(process.returncode, 130, screen)
                self.assertEqual(screen.strip(), "preflight interrupted; it only reads, so nothing was changed")
                self.assertFalse(sql_survived, "SQLcl was still running after the preflight ended")
                self.assertEqual(sorted(path.name for path in (root / "scratch").glob("*")), [])

    @unittest.skipUnless(os.name == "nt", "presses Ctrl-C in a Windows console")
    def test_ctrl_c_in_a_windows_console_waits_for_python_and_passes_its_status_on(self) -> None:
        # Python needs a moment after Ctrl-C to say what it left and to clean up, and
        # migrate chooses status 2 ("interrupted and may be partially applied") over 130.
        # A Bash that had exec'd Python is a bare stand-in that the keypress ends at once
        # with a status of its own (512): the prompt came back first, Python's last words
        # appeared after it, and the status it chose was lost.
        for wrapper in self.windows_wrappers():
            with self.subTest(wrapper=Path(wrapper[0]).name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.make_windows_interrupt_checkout(root)
                started = root / "python-started"
                # A python3 that waits for Ctrl-C, takes a moment to finish and exits 2, as
                # scripts.migrate does when it is interrupted inside an apply.
                _console_ctrl_c.write_python_command(
                    root / "bin", "python3",
                    "import sys\nimport time\nfrom pathlib import Path\n"
                    'if sys.argv[1:2] == ["-c"]:\n'
                    "    exec(sys.argv[2])  # team.ps1 asks whether this is Python 3.10 or newer\n"
                    "    raise SystemExit(0)\n"
                    f"Path({str(started)!r}).write_text('started')\n"
                    "try:\n    time.sleep(60)\nexcept KeyboardInterrupt:\n"
                    "    print('python is cleaning up', flush=True)\n    time.sleep(1.5)\n"
                    "    print('python is done', flush=True)\n    raise SystemExit(2)\n",
                )
                environment = fake_sqlcl.environment(root / "bin", PROJECT_ENV_FILE=str(root / ".env"))
                command = [*wrapper[:-1], str(root / "scripts" / wrapper[-1]), "migrate", "migrations/2026-10-02_interrupt-r001", "--env", "dev"]
                process, screen, _ = _console_ctrl_c.interrupt(command, root, environment, started)
                self.assertEqual(process.returncode, 2, screen)
                self.assertEqual(screen.strip().splitlines(), ["python is cleaning up", "python is done"])

    @unittest.skipUnless(os.name == "nt", "presses Ctrl-C in a Windows console; off Windows Python takes over the process (exec)")
    def test_ctrl_c_in_a_windows_console_passes_on_the_status_of_a_python_that_exits_at_once(self) -> None:
        # Git Bash reports 130 for a native child that ends within a moment of the Ctrl-C,
        # whatever status it chose: an interrupted apply (scripts.migrate exits 2 at once,
        # "may be partially applied") came out as a clean 130 most of the time. Python
        # also writes its status to MIGRATE_STATUS_FILE; repeat, since it is a race.
        for wrapper in self.windows_wrappers():
            for attempt in range(3):
                with self.subTest(wrapper=Path(wrapper[0]).name, attempt=attempt), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    self.make_windows_interrupt_checkout(root)
                    started = root / "python-started"
                    # A python3 that waits for Ctrl-C and exits 2 at once, recording the status
                    # where scripts.migrate does.
                    _console_ctrl_c.write_python_command(
                        root / "bin", "python3",
                        "import os\nimport sys\nimport time\nfrom pathlib import Path\n"
                        'if sys.argv[1:2] == ["-c"]:\n'
                        "    exec(sys.argv[2])  # team.ps1 asks whether this is Python 3.10 or newer\n"
                        "    raise SystemExit(0)\n"
                        f"Path({str(started)!r}).write_text('started')\n"
                        "try:\n    time.sleep(60)\nexcept KeyboardInterrupt:\n"
                        "    Path(os.environ['MIGRATE_STATUS_FILE']).write_text('2\\n')\n"
                        "    raise SystemExit(2)\n",
                    )
                    environment = fake_sqlcl.environment(root / "bin", PROJECT_ENV_FILE=str(root / ".env"))
                    command = [*wrapper[:-1], str(root / "scripts" / wrapper[-1]), "migrate", "migrations/2026-10-02_interrupt-r001", "--env", "dev"]
                    process, screen, _ = _console_ctrl_c.interrupt(command, root, environment, started)
                    self.assertEqual(process.returncode, 2, screen)
                    self.assertEqual(list((root / "scratch").glob("migrate-status.*")), [], "the status file is removed")

    @unittest.skipUnless(os.name == "nt", "presses Ctrl-C in a Windows console; the POSIX twin is test_interrupting_doctor_removes_its_working_directory")
    def test_ctrl_c_in_a_windows_console_ends_doctor_and_removes_its_working_directory(self) -> None:
        for wrapper in self.windows_wrappers():
            with self.subTest(wrapper=Path(wrapper[0]).name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self.make_windows_interrupt_checkout(root)
                started = root / "sql-started"
                # Real SQLcl is one native program that Ctrl-C ends; so is this stand-in.
                _console_ctrl_c.write_python_command(
                    root / "bin", "sql",
                    f"import time\nfrom pathlib import Path\nPath({str(started)!r}).write_text('started')\ntime.sleep(60)\n",
                )
                environment = fake_sqlcl.environment(root / "bin", PROJECT_ENV_FILE=str(root / ".env"))
                command = [*wrapper[:-1], str(root / "scripts" / wrapper[-1]), "doctor"]
                process, screen, _ = _console_ctrl_c.interrupt(command, root, environment, started)
                self.assertEqual(process.returncode, 130, screen)
                self.assertEqual(sorted(path.name for path in (root / "scratch").glob("sqlcl-doctor*")), [])

    def make_deploy_checkout(self, root: Path, configure_profile: bool = True) -> tuple[Path, Path]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in (
            "deploy.sh",
            "publish_app.sh",
            "publish_app.sql", "deployment_descriptor.py", "apex_compatibility.py", "sqlcl_version.py", "verify_apex_release.sql", "application_lifecycle.py", "application_lifecycle.sql", "db_targets.py", "sqlcl_session.py", "sqlcl_session.sh", "windows_job.py",
            "sqlcl_safe.sh",
            "load_env.sh",
            "export_apps.sql", "verify_deployment_state.sql",
            "verify_db_access.sql",
            "normalize_apx.sh",
            "record_export_state.py", "source_provenance.py", "apexlang_diff.py", "source_evidence.py",
            "verify_publish_state.py",
            "apexlang_normalization.py",
            "stamp_publish_version.py",
            "validate_app_source.py",
            "path_utils.py",
        ):
            source = ROOT / "scripts" / name
            if source.exists():
                shutil.copy2(source, scripts / name)
        env_text = (ROOT / ".env.example").read_text(encoding="utf-8")
        if configure_profile:
            env_text += "\nPROD_SQLCL_CONNECTION=prod-db\nPROD_EXPECTED_USER=PROD_DEPLOYER\n"
        (root / ".env").write_text(env_text, encoding="utf-8")
        app = root / "apps" / "DEMO" / "100"
        deployments = app / "deployments"
        deployments.mkdir(parents=True)
        (app / "application.apx").write_text("app SAMPLE ()\n", encoding="utf-8")
        (app / ".apex").mkdir()
        (app / ".apex" / "apexlang.json").write_text('{"version":1}\n', encoding="utf-8")
        (deployments / "prod.json").write_text(
            '{"workspace":{"name":"PROD_WORKSPACE"},"app":{"id":100,'
            '"databaseSession":{"parsingSchema":"PROD_APP"}}}\n',
            encoding="utf-8",
        )
        fake_bin = root / "bin"
        fake_bin.mkdir()
        sql_log = root / "sql-called"
        fake_sql = fake_bin / "sql"
        fake_sql.write_text(
            "#!/usr/bin/env bash\n"
            "cat > /dev/null\n"
            f'python3 "{(ROOT / "tests" / "fake_application_lock_sql.py").as_posix()}" "$@"; fixture_status=$?; [[ $fixture_status == 3 ]] || exit "$fixture_status"\n'
            "if [[ ${1:-} == -V ]]; then printf 'SQLcl: Release 26.3 Production\\n'; exit 0; fi\n"
            "mode=other; export_schema=DEMO\n"
            "for arg in \"$@\"; do case \"$arg\" in *@*publish_app.sql) mode=import ;; *@*export_apps.sql) mode=export ;; esac; done\n"
            "if [[ $mode == export ]]; then found_script=0; for arg in \"$@\"; do if [[ $found_script == 1 ]]; then export_schema=$arg; break; fi; case \"$arg\" in *@*export_apps.sql) found_script=1 ;; esac; done; fi\n"
            "if [[ $mode == import ]]; then printf '%s\\n' \"$@\" > \"$FAKE_SQL_LOG\"; printf '%s\\n' 'Import successful.' 'APEX_IMPORT_VERIFIED:100'; fi\n"
            "printf '26.2.0|2026-10-12T03:00:00.000Z\\n' > .apex-release-before.txt; printf '26.2.0|2026-10-12T03:00:01.000Z\\n' > .apex-release-after.txt\n"
            "if [[ $mode == export ]]; then exported=\"$PWD/apps/$export_schema/100\"; mkdir -p \"$exported/.apex\"; cp \"$FAKE_SOURCE_DIR/application.apx\" \"$exported/application.apx\"; cp \"$FAKE_SOURCE_DIR/.apex/apexlang.json\" \"$exported/.apex/apexlang.json\"; printf '%s\\n' '2026-09-26T09:30:00|2026-09-26T09:30:02|Release 1.0' > \"$PWD/.apex-export-before.txt\"; cp \"$PWD/.apex-export-before.txt\" \"$PWD/.apex-export-after.txt\"; fi\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        fake_sqlcl.add_launcher(fake_bin)
        return scripts / "deploy.sh", sql_log

    def run_deploy(self, script: Path, sql_log: Path, *args: str, input_text: str = ""):
        environment = os.environ.copy()
        environment["PATH"] = f"{script.parents[1] / 'bin'}{os.pathsep}{environment['PATH']}"
        environment["FAKE_SQL_LOG"] = str(sql_log)
        environment["FAKE_SOURCE_DIR"] = (script.parents[1] / "apps/DEMO/100").as_posix()
        environment["FAKE_AUTOMATION_CALLS"] = str(script.parents[1] / "automation-calls.txt")
        environment["PROJECT_ENV_FILE"] = str(script.parents[1] / ".env")
        return subprocess.run(
            [BASH, str(script), "100", "--env", "prod", *args],
            cwd=script.parents[1],
            env=environment,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_manual_deploy_prints_explicit_descriptor_without_running_sqlcl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))

            result = self.run_deploy(script, sql_log, "--manual")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("DBA runbook", result.stdout)
            self.assertIn("PROD_WORKSPACE", result.stdout)
            self.assertIn("PROD_APP", result.stdout)
            self.assertIn("deployments/prod.json", result.stdout)
            sql_lines = [line.strip() for line in result.stdout.splitlines() if line.strip().startswith("sql ")]
            self.assertEqual(len(sql_lines), 2, result.stdout)
            checkout_root = script.parents[1].resolve()
            self.assertEqual(
                shlex.split(sql_lines[0]),
                [
                    "sql",
                    "-S",
                    "-noupdates",
                    "-name",
                    "prod-db",
                    "@" + fake_sqlcl.native(checkout_root / "scripts" / "publish_app.sql"),
                    "PROD_APP",
                    "production",
                    "PROD_DEPLOYER",
                    fake_sqlcl.native(checkout_root / "apps/DEMO/100"),
                    fake_sqlcl.native(checkout_root / "apps/DEMO/100/deployments/prod.json"),
                    "100",
                    "-",
                    fake_sqlcl.native(checkout_root / "scripts/no_application_lock.sql"),
                    "${publication_identity[@]}",
                ],
            )
            self.assertEqual(
                shlex.split(sql_lines[1]),
                [
                    "sql",
                    "-S",
                    "-noupdates",
                    "-name",
                    "prod-db",
                    "@" + fake_sqlcl.native(checkout_root / "scripts" / "export_apps.sql"),
                    "PROD_APP",
                    "100",
                    "production",
                    "PROD_DEPLOYER",
                ],
            )
            self.assertIn("verify_publish_state.py", result.stdout)
            self.assertIn('application_lifecycle.py capture',result.stdout)
            self.assertIn('application_lifecycle.py restore-automations',result.stdout)
            self.assertIn('application_lifecycle.py verify',result.stdout)
            self.assertIn('verify normalized APEXlang source',result.stdout)
            self.assertIn("APEX_PUBLISH_SOURCE_VERIFIED:100", result.stdout)
            self.assertFalse(sql_log.exists(), "manual mode must not invoke SQLcl")

    def test_manual_deploy_optout_is_connection_free_and_omits_the_restore_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))

            result = self.run_deploy(script, sql_log, "--manual", "--no-restore-automations")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--no-restore-automations", result.stdout)
            self.assertNotIn("restore-automations --connection", result.stdout)
            self.assertIn("the lifecycle check can still report disabled automations", result.stdout)
            self.assertFalse(sql_log.exists())

    def test_manual_deploy_strict_bytes_is_carried_into_the_runbook(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))

            result = self.run_deploy(script, sql_log, "--manual", "--strict-bytes")

            self.assertEqual(result.returncode, 0, result.stderr)
            verifier_lines = [line for line in result.stdout.splitlines() if "verify_publish_state.py" in line]
            self.assertTrue(verifier_lines)
            self.assertIn("--strict-bytes", verifier_lines[0])
            self.assertFalse(sql_log.exists())

    def test_manual_deploy_runbook_removes_both_of_its_temporary_directories(self) -> None:
        # Step 2 sets an EXIT trap for SQLcl's directory; step 4 makes a second one for the
        # re-export, which stayed behind after a successful verification.
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))

            result = self.run_deploy(script, sql_log, "--manual")

            self.assertEqual(result.returncode, 0, result.stderr)
            lines = [line.strip() for line in result.stdout.splitlines()]
            created = next(index for index, line in enumerate(lines) if line.startswith("verify_dir=$(mktemp -d"))
            self.assertIn('if [ "$deployment_verified" = true ]; then rm -rf -- "$sqlcl_dir" "$verify_dir"',lines[created + 1])
            self.assertIn('else echo "Retained recovery:',lines[created + 1])

    def test_manual_deploy_works_without_a_local_production_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary), configure_profile=False)

            result = self.run_deploy(script, sql_log, "--manual")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("YOUR_PROD_SQLCL_CONNECTION", result.stdout)
            self.assertIn("YOUR_PROD_EXPECTED_USER", result.stdout)
            self.assertFalse(sql_log.exists(), "manual mode must not invoke SQLcl")

    def test_unconfirmed_deploy_does_not_invoke_sqlcl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))

            result = self.run_deploy(script, sql_log, input_text="n\n")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Deploying to PROD. Proceed? [y/N]", result.stdout)
            self.assertFalse(sql_log.exists(), "a declined deployment must not invoke SQLcl")

    def test_confirmed_deploy_uses_the_production_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))

            result = self.run_deploy(script, sql_log, input_text="y\n")

            self.assertEqual(result.returncode, 0, result.stderr)
            args = sql_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("-name", args)
            self.assertIn("prod-db", args)
            self.assertIn(fake_sqlcl.native((script.parents[1] / "apps/DEMO/100/deployments/prod.json").resolve()), args)

    def test_confirmed_deploy_restores_automations_and_optout_skips_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))
            result = self.run_deploy(script, sql_log, input_text="y\n")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("AUTO | ACTIVE | ACTIVE", result.stdout)
            self.assertEqual((script.parents[1] / "automation-calls.txt").read_text().splitlines(), ["4155544F"])

        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))
            result = self.run_deploy(script, sql_log, "--no-restore-automations", input_text="y\n")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((script.parents[1] / "automation-calls.txt").exists())

    def test_confirmed_deploy_accepts_strict_byte_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))

            result = self.run_deploy(script, sql_log, "--strict-bytes", input_text="y\n")

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("APEX_PUBLISH_SOURCE_VERIFIED:100", result.stdout)

    def make_upgrade_fixture(self, root: Path) -> tuple[Path, Path]:
        template = root / "template"
        project = root / "project"
        for repo in (template, project):
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
        (template / "template-manifest.json").write_text(
            '{"schemaVersion": 1, "upstream": "x", "templateOwned": ["scripts/**", "template-manifest.json"],'
            ' "projectOwned": [], "templateOnly": []}',
            encoding="utf-8",
        )
        (template / "scripts").mkdir()
        (template / "scripts" / "hello.sh").write_text("echo template\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(template), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(template), "commit", "-q", "-m", "v1"], check=True)
        (project / "scripts").mkdir()
        for name in ("team.sh", "team.ps1", "upgrade_template.py", "load_env.sh", "load_env.ps1", "resolve_python.ps1"):
            shutil.copy2(ROOT / "scripts" / name, project / "scripts" / name)
        # .env is local configuration; ignoring it keeps the tree clean for the engine.
        (project / ".gitignore").write_text(".env\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(project), "commit", "-q", "-m", "project"], check=True)
        return template, project

    def test_upgrade_template_command_runs_the_engine_for_this_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            template, project = self.make_upgrade_fixture(Path(temporary))

            result = subprocess.run(
                [BASH, str(project / "scripts" / "team.sh"), "upgrade-template", "--source", str(template)],
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("CREATE scripts/hello.sh", result.stdout)
            self.assertTrue((project / ".template-lock.json").is_file())

    def test_upgrade_template_warns_when_env_misses_a_new_required_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            template, project = self.make_upgrade_fixture(Path(temporary))
            (project / ".env").write_text("PROJECT_NAME=x\n", encoding="utf-8")

            result = subprocess.run(
                [BASH, str(project / "scripts" / "team.sh"), "upgrade-template", "--source", str(template)],
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(".env needs attention after the upgrade", result.stderr)

    def test_upgrade_template_runs_in_a_checkout_whose_path_has_spaces_and_brackets(self) -> None:
        # Git Bash does not rewrite a /c/... argument that holds glob characters such as
        # [1] for a native program, so the Windows Python was asked for C:\c\...
        with tempfile.TemporaryDirectory(prefix="work ", suffix=" [1]") as temporary:
            template, project = self.make_upgrade_fixture(Path(temporary))

            result = subprocess.run(
                [BASH, str(project / "scripts" / "team.sh"), "upgrade-template", "--source", str(template), "--dry-run"],
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("CREATE scripts/hello.sh", result.stdout)
            self.assertFalse((project / ".template-lock.json").exists())

    def test_powershell_upgrade_template_command_runs_the_engine(self) -> None:
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            template, project = self.make_upgrade_fixture(Path(temporary))

            result = subprocess.run(
                [
                    pwsh,
                    "-NoProfile",
                    "-File",
                    str(project / "scripts" / "team.ps1"),
                    "upgrade-template",
                    "--source",
                    str(template),
                    "--dry-run",
                ],
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("CREATE scripts/hello.sh", result.stdout)
            self.assertFalse((project / ".template-lock.json").exists())


if __name__ == "__main__":
    unittest.main()
