import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
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


def release_cli_checkout(parent: Path) -> Path:
    """Copy the CLI into a checkout with glob characters and Unicode in its path."""
    root = parent / "release project [unicode] café"
    root.mkdir()
    shutil.copytree(ROOT / "scripts", root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "template-manifest.json", root / "template-manifest.json")
    return root


class TeamCliTests(unittest.TestCase):
    def test_powershell_release_route_keeps_schema_option_and_matches_bash_dispatch(self) -> None:
        wrapper = (ROOT / "scripts" / "team.ps1").read_text(encoding="utf-8")

        self.assertIn("release list [--format text|json]", wrapper)
        self.assertIn('"release" {', wrapper)
        self.assertIn('"release") + $releaseArguments)', wrapper)
        self.assertIn('"targets", "release", "dev-test"', wrapper)

    def test_team_wrappers_expose_release_and_compare_env_with_offline_help(self) -> None:
        environment = dict(os.environ, PROJECT_ENV_FILE="/no/such/team-env")
        entries = [("team.sh", [BASH])]
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh:
            entries.append(("team.ps1", [pwsh, "-NoProfile", "-File"]))

        for name, launcher in entries:
            script = ROOT / "scripts" / name
            with self.subTest(entry=name, command="help"):
                help_result = subprocess.run(
                    [*launcher, str(script), "--help"], cwd=ROOT, env=environment,
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(0, help_result.returncode, help_result.stderr)
                self.assertIn("compare-env", help_result.stdout)
                self.assertIn("release list", help_result.stdout)

            for command in ("release", "compare-env"):
                with self.subTest(entry=name, command=command):
                    result = subprocess.run(
                        [*launcher, str(script), command, "--help"], cwd=ROOT, env=environment,
                        capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    expected = "Lean 6-step release bundle workflow" if command == "release" else "Compare environment catalog"
                    self.assertIn(expected, result.stdout)
                    self.assertNotIn("configuration file", result.stderr)

    def test_release_list_and_new_need_no_env_in_both_wrappers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="release wrapper ") as temporary:
            root = release_cli_checkout(Path(temporary))
            environment = dict(os.environ, PROJECT_ENV_FILE="/no/such/team-env")
            entries = [("team.sh", [BASH])]
            pwsh = _test_subprocess_safety.working_pwsh()
            if pwsh:
                entries.append(("team.ps1", [pwsh, "-NoProfile", "-File"]))
            for name, launcher in entries:
                script = root / "scripts" / name
                slug = f"bundle-{name.split('.', 1)[1]}"
                with self.subTest(entry=name, action="new"):
                    created = subprocess.run(
                        [*launcher, str(script), "release", "new", slug],
                        cwd=root, env=environment, capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(0, created.returncode, created.stdout + created.stderr)
                    self.assertTrue((root / "releases" / slug / "release.json").is_file())
                    self.assertFalse((root / ".env").exists())
                with self.subTest(entry=name, action="list"):
                    listed = subprocess.run(
                        [*launcher, str(script), "release", "list", "--format", "json"],
                        cwd=root, env=environment, capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(0, listed.returncode, listed.stdout + listed.stderr)
                    payload = json.loads(listed.stdout)
                    self.assertIn(slug, [item["slug"] for item in payload["draft"]])

    def test_bash_forwards_converted_repo_root_to_native_python_for_release(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = release_cli_checkout(Path(temporary))
            bash_env = Path(temporary) / "path bridge [fake].bash"
            bash_env.write_text(
                "uname() { printf 'MINGW64_NT-10.0\\n'; }\n"
                "cygpath() {\n"
                "  printf '%s\\n' \"$*\" >> \"$CYGPATH_CAPTURE\"\n"
                "  [ \"$1\" = '-m' ] && [ \"$2\" = '--' ] || return 2\n"
                "  printf '%s\\n' \"$RELEASE_NATIVE_ROOT\"\n"
                "}\n",
                encoding="utf-8",
                newline="\n",
            )
            capture = Path(temporary) / "cygpath args.log"
            environment = dict(
                os.environ,
                PROJECT_ENV_FILE="/no/such/team-env",
                BASH_ENV=str(bash_env),
                CYGPATH_CAPTURE=str(capture),
                RELEASE_NATIVE_ROOT=root.resolve().as_posix(),
            )
            result = subprocess.run(
                [BASH, str(root / "scripts" / "team.sh"), "release", "list", "--format", "json"],
                cwd=temporary,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual({"schemaVersion": 1, "draft": [], "deployed": []}, json.loads(result.stdout))
            self.assertTrue(capture.exists(), result.stdout + result.stderr)
            self.assertEqual("-m --", " ".join(capture.read_text(encoding="utf-8").strip().split(" ")[:2]))
            self.assertTrue(capture.read_text(encoding="utf-8").strip().endswith("/" + root.name))

    def test_release_collect_forwards_schema_flag_without_wrapper_stripping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = release_cli_checkout(Path(temporary))
            (root / ".env").write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
            fake_bin = Path(temporary) / "fake python"
            fake_bin.mkdir()
            captured = Path(temporary) / "python arguments.txt"
            python = fake_bin / "python3"
            python.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$TEAM_CAPTURE_ARGS\"\n", encoding="utf-8")
            python.chmod(0o755)
            environment = dict(
                os.environ,
                PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                PROJECT_ENV_FILE=str(root / ".env"),
                TEAM_CAPTURE_ARGS=str(captured),
            )
            result = subprocess.run(
                [BASH, str(root / "scripts" / "team.sh"), "release", "collect", "my-bundle", "--from", "dev", "--schema", "APP_CODE", "--object", "PACKAGE:PKG", "--mode", "new"],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            forwarded = captured.read_text(encoding="utf-8").splitlines()
            self.assertIn("--schema", forwarded)
            self.assertIn("APP_CODE", forwarded)

    def test_release_run_requires_confirm_target_and_valid_env_in_both_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = release_cli_checkout(Path(temporary))
            (root / ".env").write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
            environment = dict(os.environ, PROJECT_ENV_FILE=str(root / ".env"))
            entries = [("team.sh", [BASH])]
            pwsh = _test_subprocess_safety.working_pwsh()
            if pwsh:
                entries.append(("team.ps1", [pwsh, "-NoProfile", "-File"]))
            for name, launcher in entries:
                with self.subTest(entry=name, case="missing confirm-target"):
                    result = subprocess.run(
                        [*launcher, str(root / "scripts" / name), "release", "run", "my-bundle", "--env", "dev"],
                        cwd=root, env=environment, capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                    self.assertIn("--confirm-target", result.stderr)
                with self.subTest(entry=name, case="invalid env"):
                    result = subprocess.run(
                        [*launcher, str(root / "scripts" / name), "release", "check", "my-bundle", "--env", "production"],
                        cwd=root, env=environment, capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                    self.assertIn("invalid choice: 'production'", result.stderr)

    def test_release_without_subcommand_prints_usage_in_both_wrappers(self) -> None:
        expected = "usage: scripts/team."
        entries = [("team.sh", [BASH, str(ROOT / "scripts" / "team.sh")])]
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh:
            entries.append(("team.ps1", [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1")]))
        for name, command in entries:
            with self.subTest(wrapper=name):
                result = subprocess.run([*command, "release"], text=True, capture_output=True, check=False, cwd=ROOT)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn(expected, result.stdout + result.stderr)
                self.assertIn("release <list|new|collect|check|run>", result.stdout + result.stderr)

    def run_bash_env_loader(self, environment: Path) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            scripts = Path(temporary) / "scripts"
            scripts.mkdir()
            loader = scripts / "load_env.sh"
            shutil.copy2(ROOT / "scripts" / "load_env.sh", loader)
            shutil.copy2(ROOT / "template-manifest.json", Path(temporary) / "template-manifest.json")
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
            "release list",
            "release new",
            "release collect",
            "release check",
            "release run",
            "dev-test",
            "query",
            "compare-schema",
            "compare-env",
            "--emit-dba-script <file>",
            "backup-db",
            "deploy",
            "upgrade-template",
            "verify-local",
            "automations",
        ):
            self.assertIn(command, result.stdout)
        self.assertIn("--env <dev|staging|prod>", result.stdout)
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

    def test_powershell_targets_usage_names_the_ps1_wrapper(self) -> None:
        pwsh = _test_subprocess_safety.working_pwsh()
        if not pwsh:
            self.skipTest("PowerShell 7 is not installed")
        for arguments in (["--help"], ["--format", "xml"]):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"), "targets", *arguments],
                    cwd=ROOT,
                    env=dict(os.environ, PROJECT_ENV_FILE="/no/such/team-targets.env"),
                    capture_output=True,
                    text=True,
                    check=False,
                    stdin=subprocess.DEVNULL,
                    timeout=60,
                )
                output = result.stdout + result.stderr
                self.assertIn("usage: scripts/team.ps1 targets", output)
                self.assertNotIn("scripts/team.sh targets", output)

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
            shutil.copy2(ROOT / "template-manifest.json", root / "template-manifest.json")
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
                shutil.copy2(ROOT / "template-manifest.json", Path(temporary) / "template-manifest.json")
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

    def test_team_sh_refuses_a_bash_older_than_4_4_with_one_line_and_status_2(self) -> None:
        # No Bash 3.2 is installed here, so the guard is tested as a function fed the version parts that BASH_VERSINFO
        # would hold. The real top-level call still runs under the Bash that runs this test.
        source = (ROOT / "scripts" / "team.sh").read_text(encoding="utf-8")
        match = re.search(r"^team_require_bash\(\) \{\n.*?^\}\n", source, re.S | re.M)
        self.assertIsNotNone(match, "team.sh must define team_require_bash")
        guard = match.group(0)
        message = "team error: Bash 4.4 or newer is required (found {found}); on macOS install one with: brew install bash\n"

        def run_guard(major: str, minor: str, version: str) -> subprocess.CompletedProcess[str]:
            script = "set -euo pipefail\n" + guard + 'team_require_bash "$1" "$2" "$3"\n'
            return subprocess.run([BASH, "-c", script, "bash", major, minor, version], text=True, capture_output=True, check=False)

        for major, minor, version, found in (("3", "2", "3.2.57(1)-release", "3.2.57"), ("4", "3", "4.3.48(1)-release", "4.3.48"), ("4", "0", "4.0.44", "4.0.44")):
            with self.subTest(version=version):
                result = run_guard(major, minor, version)
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertEqual(message.format(found=found), result.stderr)
                self.assertEqual("", result.stdout)
        for major, minor, version in (("4", "4", "4.4.23(1)-release"), ("5", "0", "5.0.0"), ("5", "2", "5.2.21(1)-release")):
            with self.subTest(version=version):
                result = run_guard(major, minor, version)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual("", result.stderr)
        # The real guard, against the running shell, must not stop the script.
        running = subprocess.run([BASH, str(ROOT / "scripts" / "team.sh"), "--help"], text=True, capture_output=True, check=False, cwd=ROOT)
        self.assertEqual(0, running.returncode, running.stderr)

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
                for everyday in ("doctor", "export <app_id>", "release list", "release check <bundle> --env dev", "release run <bundle> --env dev", "backup-db"):
                    self.assertIn(everyday, short.stdout)
                self.assertNotIn("compare-env", short.stdout)
                self.assertIn("--help for every command", short.stdout)
                full = subprocess.run([*command, "--help"], text=True, capture_output=True, check=False, cwd=ROOT)
                self.assertEqual(full.returncode, 0, full.stderr)
                self.assertIn("compare-env", full.stdout)
                self.assertNotIn("Everyday commands:", full.stdout)

    def targets_wrappers(self):
        entries = [("team.sh", [BASH])]
        pwsh = _test_subprocess_safety.working_pwsh()
        if pwsh:
            entries.append(("team.ps1", [pwsh, "-NoProfile", "-File"]))
        return entries

    @staticmethod
    def _run_wrapper(launcher, name, environment, *arguments):
        return subprocess.run(
            [*launcher, str(ROOT / "scripts" / name), *arguments], cwd=ROOT, env=environment,
            capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL, timeout=120,
        )

    def test_targets_prints_the_configured_targets_offline_in_both_wrappers(self) -> None:
        # `targets` reads .env and reports schema, connection and login. It must never start SQLcl.
        for name, launcher in self.targets_wrappers():
            with self.subTest(entry=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                env_file = root / ".env"
                env_file.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
                fake_bin = root / "bin"
                started = root / "sqlcl-started"
                fake_sqlcl.install(fake_bin, f": > '{started}'\nexit 97\n")
                result = subprocess.run(
                    [*launcher, str(ROOT / "scripts" / name), "targets"], cwd=ROOT,
                    env=fake_sqlcl.environment(fake_bin, PROJECT_ENV_FILE=str(env_file)),
                    capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL, timeout=120,
                )
                output = result.stdout + result.stderr
                self.assertEqual(0, result.returncode, output)
                self.assertIn("Effective database targets", result.stdout)
                self.assertRegex(result.stdout, r"(?m)^dev\s+read\s+DEMO\s+docker-demo\s+DEMO\s+no\s+development\s+resolved")
                self.assertIn("no connection is made", result.stdout)
                self.assertFalse(started.exists(), f"targets started SQLcl: {output}")

    def test_targets_shows_a_refused_row_for_a_split_owner_in_both_wrappers(self) -> None:
        # CODE_SCHEMA and MIGRATION_SCHEMA name different owners: migrate must refuse the code schema.
        for name, launcher in self.targets_wrappers():
            with self.subTest(entry=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                env_file = root / ".env"
                split = (ROOT / ".env.example").read_text(encoding="utf-8")
                split += "\nMIGRATION_SCHEMA=DATA_OWNER\nMIGRATION_SQLCL_CONNECTION=docker-demo\nMIGRATION_EXPECTED_USER=DATA_OWNER\n"
                env_file.write_text(split, encoding="utf-8")
                fake_bin = root / "bin"
                started = root / "sqlcl-started"
                fake_sqlcl.install(fake_bin, f": > '{started}'\nexit 97\n")
                result = subprocess.run(
                    [*launcher, str(ROOT / "scripts" / name), "targets", "--format", "json"], cwd=ROOT,
                    env=fake_sqlcl.environment(fake_bin, PROJECT_ENV_FILE=str(env_file)),
                    capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL, timeout=120,
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                rows = json.loads(result.stdout)["rows"]
                refused = [row for row in rows if row["purpose"] == "migration" and row["schema"] == "DEMO"]
                self.assertEqual(["REFUSED"], [row["status"] for row in refused])
                self.assertIn("MIGRATION_SCHEMA", refused[0]["message"])
                resolved = [row for row in rows if row["purpose"] == "migration" and row["schema"] == "DATA_OWNER"]
                self.assertEqual(["resolved"], [row["status"] for row in resolved])
                self.assertFalse(started.exists(), "targets started SQLcl")

    def test_targets_refuses_extra_and_schema_arguments_like_other_offline_commands(self) -> None:
        for name, launcher in self.targets_wrappers():
            with self.subTest(entry=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                env_file = root / ".env"
                env_file.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
                fake_bin = root / "bin"
                started = root / "sqlcl-started"
                fake_sqlcl.install(fake_bin, f": > '{started}'\nexit 97\n")
                environment = fake_sqlcl.environment(fake_bin, PROJECT_ENV_FILE=str(env_file))

                reference = self._run_wrapper(launcher, name, environment, "template-status", "extra")
                for arguments in (("targets", "extra"), ("targets", "--schema", "DEMO"), ("targets", "--format", "xml")):
                    with self.subTest(arguments=arguments):
                        result = self._run_wrapper(launcher, name, environment, *arguments)
                        self.assertEqual(reference.returncode, result.returncode, result.stdout + result.stderr)
                        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertFalse(started.exists(), "a refused argument still started SQLcl")

    def test_targets_help_works_without_a_configured_env_in_both_wrappers(self) -> None:
        for name, launcher in self.targets_wrappers():
            with self.subTest(entry=name):
                result = subprocess.run(
                    [*launcher, str(ROOT / "scripts" / name), "targets", "--help"], cwd=ROOT,
                    env=dict(os.environ, PROJECT_ENV_FILE="/no/such/targets.env"),
                    capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL, timeout=120,
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("--format", result.stdout)
                self.assertNotIn("configuration file", result.stdout + result.stderr)

    def test_targets_works_without_an_env_file_in_both_wrappers(self) -> None:
        # Like template-status, a missing .env is not an error: every row reads "not configured", SQLcl never starts,
        # an inherited shell variable is not reported as configuration, and argument errors keep the status 2.
        for name, launcher in self.targets_wrappers():
            with self.subTest(entry=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fake_bin = root / "bin"
                started = root / "sqlcl-started"
                fake_sqlcl.install(fake_bin, f": > '{started}'\nexit 97\n")
                environment = fake_sqlcl.environment(
                    fake_bin, PROJECT_ENV_FILE=str(root / "missing.env"), CODE_SCHEMA="INHERITED_ONLY"
                )

                result = self._run_wrapper(launcher, name, environment, "targets", "--format", "json")
                output = result.stdout + result.stderr
                self.assertEqual(0, result.returncode, output)
                rows = json.loads(result.stdout)["rows"]
                self.assertTrue(rows)
                self.assertEqual({"not configured"}, {row["status"] for row in rows}, output)
                self.assertNotIn("INHERITED_ONLY", output)
                self.assertFalse(started.exists(), f"targets started SQLcl: {output}")
                text = self._run_wrapper(launcher, name, environment, "targets")
                self.assertEqual(0, text.returncode, text.stdout + text.stderr)
                self.assertIn("Effective database targets", text.stdout)
                reference = self._run_wrapper(launcher, name, environment, "template-status", "extra")
                for arguments in (("targets", "extra"), ("targets", "--schema", "DEMO")):
                    with self.subTest(arguments=arguments):
                        refused = self._run_wrapper(launcher, name, environment, *arguments)
                        self.assertEqual(reference.returncode, refused.returncode, refused.stdout + refused.stderr)
                        self.assertEqual(2, refused.returncode, refused.stdout + refused.stderr)
                self.assertFalse(started.exists(), "a refused argument still started SQLcl")

    def test_targets_ignores_an_inherited_project_schema_in_both_wrappers(self) -> None:
        # targets reports every configured schema, so a PROJECT_SCHEMA left in the shell must not narrow it or refuse it.
        for name, launcher in self.targets_wrappers():
            with self.subTest(entry=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                env_file = root / ".env"
                env_file.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
                fake_bin = root / "bin"
                started = root / "sqlcl-started"
                fake_sqlcl.install(fake_bin, f": > '{started}'\nexit 97\n")
                environment = fake_sqlcl.environment(fake_bin, PROJECT_ENV_FILE=str(env_file), PROJECT_SCHEMA="NOT_CONFIGURED")
                result = self._run_wrapper(launcher, name, environment, "targets")
                output = result.stdout + result.stderr
                self.assertEqual(0, result.returncode, output)
                self.assertRegex(result.stdout, r"(?m)^dev\s+read\s+DEMO\s+docker-demo\s+DEMO\s+no\s+development\s+resolved")
                self.assertFalse(started.exists(), f"targets started SQLcl: {output}")

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
            ["release"], ["compare-schema"], ["deploy"],
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
                # The marker is written just before the helper blocks. Let PowerShell's
                # native-process wait settle before broadcasting SIGINT to the whole group;
                # an interrupt during process startup can abort pwsh instead of exercising
                # team.ps1's finally block.
                _test_subprocess_safety.interrupt_process_group(process, settle=0.1)
                stdout, stderr = process.communicate(timeout=60)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
            self.assertEqual(process.returncode, 130, f"stdout: {stdout}\nstderr: {stderr}")

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
                _test_subprocess_safety.interrupt_process_group(process, settle=1.0)
                interrupted_at = time.monotonic()
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
        # release run reports status 2 when interrupted during apply, not 130;
        # the wrapper must not flatten that into a plain interrupt.
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
                [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"), "release", "run", "my-bundle", "--env", "dev", "--confirm-target", "DEVDB"],
                cwd=ROOT, env={**os.environ, "TEAM_BASH": str(fake_bash)},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
                preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
            )
            try:
                deadline = time.monotonic() + 60
                while not started.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(started.exists(), "the helper never started")
                # The marker is written just before the helper blocks. Let PowerShell's
                # native-process wait settle before broadcasting SIGINT to the whole group;
                # an interrupt during process startup can abort pwsh instead of exercising
                # team.ps1's finally block.
                _test_subprocess_safety.interrupt_process_group(process, settle=0.1)
                stdout, stderr = process.communicate(timeout=60)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate()
            self.assertEqual(process.returncode, 2, f"stdout: {stdout}\nstderr: {stderr}")

    def test_yes_and_confirm_target_reach_their_helpers_and_both_usage_screens_list_them(self) -> None:
        # Production refusals come from deploy before any connection; the wrappers must pass the flags through.
        pwsh = _test_subprocess_safety.working_pwsh()
        entries = [("team.sh", [BASH, str(ROOT / "scripts" / "team.sh")])]
        if pwsh:
            entries.append(("team.ps1", [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1")]))
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")
            environment = os.environ.copy()
            environment["PROJECT_ENV_FILE"] = str(env_file)
            for name, command in entries:
                for argv in (
                    ["deploy", "100", "--env", "prod", "-y"],
                ):
                    with self.subTest(wrapper=name, command=argv[0]):
                        result = subprocess.run([*command, *argv], text=True, capture_output=True, check=False, cwd=ROOT,
                                                env=environment, stdin=subprocess.DEVNULL, timeout=120)
                        output = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout + result.stderr)
                        self.assertNotEqual(result.returncode, 0, output)
                        self.assertIn("production also needs --confirm-target <db_unique_name>", re.sub(r"[\s|]+", " ", output), output)
        for name, command in entries:
            with self.subTest(usage=name):
                usage = subprocess.run([*command, "--help"], text=True, capture_output=True, check=False, cwd=ROOT).stdout
                self.assertIn("[--yes|-y]", usage.split("  deploy ", 1)[1].split("\n\n", 1)[0] + usage.split("  deploy ", 1)[1][:400])
                self.assertIn("[--confirm-target <db_unique_name>]", usage)

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

            for command, flag, path in (
                ("run", "--report", r"scratch\release-reports\report.md"),
            ):
                with self.subTest(command=command, flag=flag):
                    result = subprocess.run(
                        [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"),
                         "release", command, "my-bundle", flag, path],
                        cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    seen = record.read_text(encoding="utf-8").splitlines()
                    self.assertEqual(seen[0], str(ROOT / "scripts" / "team.sh"))
                    expected = path.replace("\\", "/") if os.name == "nt" else path
                    self.assertEqual(seen[1:], ["release", command, "my-bundle", flag, expected])

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
                [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"), "release", "check", "releases/x", "--env", "dev"],
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
            shutil.copy2(ROOT / "template-manifest.json", root / "template-manifest.json")
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
            shutil.copy2(ROOT / "template-manifest.json", root / "template-manifest.json")
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

    @unittest.skipIf(os.name == "nt", "on Windows Python runs as a child on purpose: Git Bash ends itself on Ctrl-C")
    def test_helper_scripts_hand_their_process_to_python_off_windows(self) -> None:
        # With exec Python keeps the script's PID, so a signal sent to the script alone (timeout, kill,
        # a supervisor that signals its child only) reaches it instead of orphaning it.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "scripts", root / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy2(ROOT / "template-manifest.json", root / "template-manifest.json")
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
            for script, arguments in (
                ("compare_schema.sh", []),
                ("team.sh", ["release", "list"]),
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
        shutil.copy2(ROOT / "template-manifest.json", root / "template-manifest.json")
        (root / ".env").write_text((ROOT / ".env.example").read_text(encoding="utf-8"), encoding="utf-8")

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

    def make_deploy_checkout(self, root: Path, configure_profile: bool = True, with_lifecycle: bool = False) -> tuple[Path, Path]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in (
            "deploy.sh",
            "publish_app.sh",
            "publish_app.sql", "deployment_descriptor.py", "apex_compatibility.py", "sqlcl_version.py", "verify_apex_release.sql", "application_lifecycle.py", "application_lifecycle.sql", "db_targets.py", "local_config.py", "template_release.py", "sqlcl_session.py", "sqlcl_session.sh", "windows_job.py",
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
        shutil.copy2(ROOT / "template-manifest.json", root / "template-manifest.json")
        if with_lifecycle:
            # The identity reader --confirm-target compares against; a stand-in that reports PRODDB_UNIQUE.
            from test_publish_cli import PublishAppCliTests

            (scripts / "application_lifecycle.py").write_text(PublishAppCliTests.FAKE_LIFECYCLE, encoding="utf-8")
            (scripts / "application_lifecycle.sql").write_text("-- test stand-in\n", encoding="utf-8")
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
            self.assertIn("piped or redirected answers are not accepted", result.stderr)
            self.assertFalse(sql_log.exists(), "a declined deployment must not invoke SQLcl")

    def test_piped_yes_is_refused_for_a_production_deployment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary), with_lifecycle=True)

            result = self.run_deploy(script, sql_log, input_text="y\n")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--yes", result.stderr)
            self.assertFalse(sql_log.exists(), "a piped answer must not confirm a production import")

    def test_yes_alone_is_refused_for_production_before_any_sqlcl_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary), with_lifecycle=True)

            result = self.run_deploy(script, sql_log, "-y")

            self.assertEqual(result.returncode, 2)
            self.assertIn("production also needs --confirm-target <db_unique_name>", result.stderr)
            self.assertFalse(sql_log.exists())

    def test_manual_deploy_takes_neither_yes_nor_confirm_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))

            for flags in (("--manual", "-y"), ("--manual", "--confirm-target", "PRODDB_UNIQUE")):
                with self.subTest(flags=flags):
                    result = self.run_deploy(script, sql_log, *flags)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn("do not apply to --manual", result.stderr)
            self.assertFalse(sql_log.exists())

    def test_confirmed_deploy_uses_the_production_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary), with_lifecycle=True)

            result = self.run_deploy(script, sql_log, "-y", "--confirm-target", "PRODDB_UNIQUE")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Confirmed by --yes and --confirm-target PRODDB_UNIQUE; the interactive prompt was skipped.", result.stdout)
            args = sql_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("-name", args)
            self.assertIn("prod-db", args)
            self.assertIn(fake_sqlcl.native((script.parents[1] / "apps/DEMO/100/deployments/prod.json").resolve()), args)

    def test_confirmed_deploy_restores_automations_and_optout_skips_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))
            result = self.run_deploy(script, sql_log, "-y", "--confirm-target", "TESTDB")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("AUTO | ACTIVE | ACTIVE", result.stdout)
            self.assertEqual((script.parents[1] / "automation-calls.txt").read_text().splitlines(), ["4155544F"])

        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))
            result = self.run_deploy(script, sql_log, "--no-restore-automations", "-y", "--confirm-target", "TESTDB")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse((script.parents[1] / "automation-calls.txt").exists())

    def test_confirmed_deploy_accepts_strict_byte_verification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script, sql_log = self.make_deploy_checkout(Path(temporary))

            result = self.run_deploy(script, sql_log, "--strict-bytes", "-y", "--confirm-target", "TESTDB")

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
        shutil.copy2(template / "template-manifest.json", project / "template-manifest.json")
        if (ROOT / "scripts" / "template_release.ps1").exists():
            shutil.copy2(ROOT / "scripts" / "template_release.ps1", project / "scripts" / "template_release.ps1")
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


class TeamPs1EnvironmentTests(unittest.TestCase):
    """team.ps1 must leave the caller's PROJECT_SCHEMA as it found it, in a persistent session."""

    def run_session(self, setup: str, command: str) -> str:
        pwsh = _test_subprocess_safety.working_pwsh()
        if not pwsh:
            self.skipTest("PowerShell 7 is not installed")
        script = (
            f"{setup}; & ./scripts/team.ps1 {command} | Out-Null; "
            "if ($null -eq $env:PROJECT_SCHEMA) { 'UNSET' } else { $env:PROJECT_SCHEMA }"
        )
        result = subprocess.run(
            [pwsh, "-NoProfile", "-Command", script],
            cwd=ROOT,
            env=dict(os.environ, PROJECT_ENV_FILE="/no/such/team-targets.env"),
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        self.assertNotIn("Traceback", result.stderr)
        return result.stdout.strip().splitlines()[-1]

    def test_targets_keeps_an_inherited_schema_selection(self) -> None:
        self.assertEqual(self.run_session("$env:PROJECT_SCHEMA = 'KEEP'", "targets --help"), "KEEP")

    def test_targets_leaves_an_unset_schema_unset(self) -> None:
        self.assertEqual(
            self.run_session("Remove-Item -LiteralPath Env:PROJECT_SCHEMA -ErrorAction SilentlyContinue", "targets --help"),
            "UNSET",
        )

    def test_refused_schema_argument_does_not_leak_into_the_session(self) -> None:
        # The identifier check refuses before any command dispatches, so nothing connects.
        self.assertEqual(
            self.run_session("$env:PROJECT_SCHEMA = 'KEEP'", "doctor --schema lowercase_name"), "KEEP"
        )
        self.assertEqual(
            self.run_session("Remove-Item -LiteralPath Env:PROJECT_SCHEMA -ErrorAction SilentlyContinue",
                             "doctor --schema lowercase_name"),
            "UNSET",
        )


@unittest.skipUnless(os.name == "nt", "the legacy native argument rules belong to Windows PowerShell; POSIX launchers pass arguments intact")
class NativeArgumentPassingTests(unittest.TestCase):
    """Windows PowerShell 5.1 garbles some native arguments: an empty one, a double quote, or a value with a space
    that ends in a backslash. team.ps1 refuses them before the target starts. A .cmd or .bat target keeps those
    legacy rules even in PowerShell 7.3+'s default mode, so it is refused there too; a normal .exe gets them intact."""

    REFUSAL = "Windows PowerShell 5.1 cannot pass"
    # Values the legacy rules garble: a double quote, an empty value, and a space with a trailing backslash.
    GARBLED = ('A"B', "", "a b\\")
    # Values the legacy rules leave alone: a .cmd target must receive them exactly.
    INTACT = ("a b", "ab\\")
    RECORDER = (
        "import json, sys\n"
        "with open(sys.argv[1], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[2:]) + '\\n')\n"
    )

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.calls = self.root / "bash-calls.jsonl"

    def engines(self) -> list[tuple[str, str]]:
        """The installed engines as (label, executable): Windows PowerShell 5.1, and PowerShell 7.3+ when present."""
        found = []
        windows = _test_subprocess_safety.working_windows_powershell()
        if windows is not None:
            found.append(("Windows PowerShell 5.1", windows))
        pwsh = shutil.which("pwsh")
        if pwsh and self.powershell_is_at_least(pwsh, (7, 3)):
            found.append(("PowerShell 7.3+", pwsh))
        if not found:
            self.skipTest("neither Windows PowerShell 5.1 nor PowerShell 7.3 or newer is installed")
        return found

    def reset_calls(self) -> None:
        if self.calls.exists():
            self.calls.unlink()

    def recorded_calls(self) -> list[list[str]]:
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]

    def windows_powershell(self) -> str:
        executable = _test_subprocess_safety.working_windows_powershell()
        if executable is None:
            self.skipTest("Windows PowerShell 5.1 is not installed")
        return executable

    def require_ascii_cmd_fixture(self, *paths: object) -> None:
        # cmd.exe reads a batch file in the OEM code page. A .cmd fixture whose own path or embedded paths are not
        # ASCII cannot be written and run as intended (UTF-8 would be misread), so the test skips rather than writes it.
        for path in paths:
            if not str(path).isascii():
                self.skipTest(f"cmd.exe reads a .cmd fixture in the OEM code page, so its paths must be ASCII: {ascii(path)}")

    def resolver_choice(self, engine: str, environment: dict[str, str]) -> str | None:
        """The Python that team.ps1's resolver (Resolve-TeamPython in scripts/resolve_python.ps1) picks."""
        result = subprocess.run(
            [engine, "-NoProfile", "-Command",
             ". $env:TEAM_TEST_RESOLVER; $python = Resolve-TeamPython; if ($null -ne $python) { $python.Path }"],
            cwd=ROOT, env={**environment, "TEAM_TEST_RESOLVER": str(ROOT / "scripts" / "resolve_python.ps1")},
            text=True, capture_output=True, check=False,
        )
        return result.stdout.strip() or None

    def interpreter_first_environment(self, engine: str) -> dict[str, str]:
        """An environment in which team.ps1 runs the running interpreter, which must be a normal .exe.

        PATH holds only the interpreter's folder and the Windows system folders. The resolver looks for python3 on the
        whole PATH before python, so a prepended folder alone would lose to a pyenv-win python3.bat on the caller's PATH.
        The choice is read back from the same engine and environment; the test skips when it is not this interpreter.
        """
        interpreter = sys.executable
        if not interpreter.casefold().endswith(".exe"):
            self.skipTest(f"the running interpreter is not an .exe ({ascii(interpreter)}), so a normal .exe target cannot be arranged")
        system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        environment = {**os.environ}
        set_environment_value(environment, "PATH", os.pathsep.join(
            [str(Path(interpreter).parent), str(system / "System32"), str(system)]))
        choice = self.resolver_choice(engine, environment)
        if choice is None or not os.path.samefile(choice, interpreter):
            self.skipTest(f"team.ps1's resolver picks {ascii(choice)}, not the running interpreter, so a normal .exe target cannot be arranged")
        return environment

    def fake_bash(self, *, marker_on_probe: bool) -> Path:
        # A batch file, since PowerShell cannot run an extensionless script on Windows. Each call is recorded: the
        # recorder appends the arguments the batch file received, as a JSON list, to self.calls. Without
        # marker_on_probe the python probe (bash -c "python3 ...") answers yes silently and is not recorded.
        recorder = self.root / "record_call.py"
        recorder.write_text(self.RECORDER, encoding="ascii")
        probe_line = "" if marker_on_probe else 'if "%~1"=="-c" exit /b 0\r\n'
        fake = self.root / "git-bash.cmd"
        # The batch file embeds sys.executable, the recorder and the calls file, so all of them must be ASCII.
        self.require_ascii_cmd_fixture(fake, sys.executable, recorder, self.calls)
        fake.write_text(f'@echo off\r\n{probe_line}"{sys.executable}" "{recorder}" "{self.calls}" %*\r\nexit /b 0\r\n',
                        encoding="ascii", newline="")
        return fake

    def run_team(self, engine: str, arguments: list[str], fake: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "team.ps1"), *arguments],
            cwd=ROOT, env={**os.environ, "TEAM_BASH": str(fake)}, text=True, capture_output=True, check=False,
        )

    def test_refuses_an_argument_for_git_bash_that_the_legacy_rules_would_garble(self) -> None:
        for label, engine in self.engines():
            for value in self.GARBLED:
                with self.subTest(engine=label, value=value):
                    self.reset_calls()
                    result = self.run_team(engine, ["compare-schema", "--env", "dev", "--object", value],
                                           self.fake_bash(marker_on_probe=True))
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(self.REFUSAL, result.stderr)
                    self.assertFalse(self.calls.exists(), "a refused command must not start Git Bash, not even its probe")

    def test_a_cmd_target_gets_each_argument_exactly_or_the_command_is_refused(self) -> None:
        # Under every engine team.ps1 either refuses a value before the .cmd target starts, or the target receives
        # exactly that value. An intact value is never refused, because the legacy rules pass it exactly. A garbled
        # value must be refused, including by PowerShell 7.3+ in its default mode.
        for label, engine in self.engines():
            for value in self.GARBLED + self.INTACT:
                with self.subTest(engine=label, value=value):
                    self.reset_calls()
                    result = self.run_team(engine, ["compare-schema", "--env", "dev", "--object", value],
                                           self.fake_bash(marker_on_probe=False))
                    output = result.stdout + result.stderr
                    refused = result.returncode == 2 and self.REFUSAL in result.stderr
                    if value in self.INTACT:
                        self.assertFalse(refused, f"the legacy rules pass this value intact, so it must not be refused: {output}")
                    if value in self.GARBLED:
                        self.assertTrue(refused, f"the legacy rules garble this value, so it must be refused: {output}")
                    if refused:
                        self.assertFalse(self.calls.exists(), "a refused command must not start the .cmd target")
                        continue
                    self.assertEqual(result.returncode, 0, output)
                    calls = self.recorded_calls()
                    self.assertEqual(len(calls), 1, output)
                    self.assertEqual(calls[0][1:], ["--env", "dev", "--object", value], output)

    def test_refuses_an_argument_for_python_that_the_legacy_rules_would_garble(self) -> None:
        engine = self.windows_powershell()
        result = self.run_team(engine, ["template-status", "--format", 'te"xt'], self.fake_bash(marker_on_probe=True))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(self.REFUSAL, result.stderr)
        self.assertIn("to Python intact", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_an_ordinary_python_argument_is_not_refused(self) -> None:
        engine = self.windows_powershell()
        result = self.run_team(engine, ["template-status", "--format", "text"], self.fake_bash(marker_on_probe=False))
        self.assertNotIn(self.REFUSAL, result.stderr, result.stdout + result.stderr)

    def test_ordinary_arguments_still_reach_git_bash(self) -> None:
        engine = self.windows_powershell()
        result = self.run_team(engine, ["compare-schema", "--env", "dev", "--object", "CUSTOMERS"],
                               self.fake_bash(marker_on_probe=False))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.recorded_calls()
        self.assertEqual(len(calls), 1, "an ordinary command must reach Git Bash")
        self.assertEqual(calls[0][1:], ["--env", "dev", "--object", "CUSTOMERS"])

    @staticmethod
    def powershell_is_at_least(executable: str, minimum: tuple[int, int]) -> bool:
        result = subprocess.run([executable, "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"],
                                text=True, capture_output=True, check=False)
        parts = result.stdout.strip().split(".")
        if result.returncode != 0 or len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
            return False
        return (int(parts[0]), int(parts[1])) >= minimum

    def test_powershell_7_3_refuses_a_double_quote_for_a_cmd_target(self) -> None:
        # Changed: this test used to expect the double quote to pass through to a .cmd fake. A .cmd target keeps the
        # legacy rules in PowerShell 7.3+'s default Windows mode, so the double quote is refused before the batch file runs.
        pwsh = shutil.which("pwsh")
        if not pwsh or not self.powershell_is_at_least(pwsh, (7, 3)):
            self.skipTest("PowerShell 7.3 or newer (pwsh) is not installed")
        self.reset_calls()
        result = self.run_team(pwsh, ["compare-schema", "--env", "dev", "--object", 'A"B'],
                               self.fake_bash(marker_on_probe=False))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(self.REFUSAL, result.stderr)
        self.assertFalse(self.calls.exists(), "a refused command must not start the .cmd target")

    def test_powershell_7_3_hands_a_double_quote_to_a_normal_exe_intact(self) -> None:
        # The running interpreter is a normal .exe, so PowerShell 7.3+ passes its arguments intact and team.ps1 does not
        # refuse them. argparse names the value it received in its invalid-choice error. interpreter_first_environment
        # makes team.ps1's resolver pick this interpreter, or skips; a .bat or .cmd Python is refused, as the next test shows.
        pwsh = shutil.which("pwsh")
        if not pwsh or not self.powershell_is_at_least(pwsh, (7, 3)):
            self.skipTest("PowerShell 7.3 or newer (pwsh) is not installed")
        environment = self.interpreter_first_environment(pwsh)
        result = subprocess.run(
            [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "team.ps1"),
             "template-status", "--format", 'te"xt'],
            cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
        )
        output = result.stdout + result.stderr
        self.assertNotIn(self.REFUSAL, result.stderr, output)
        self.assertIn("invalid choice: 'te\"xt'", result.stderr, output)

    def test_a_cmd_python_shim_is_refused_before_it_starts(self) -> None:
        # A pyenv-win Python is a .bat. The Python call sites pass the resolved path, so the legacy rules apply to it
        # under every engine. PATH holds only the shim's folder and the Windows system folders.
        system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        for label, engine in self.engines():
            with self.subTest(engine=label):
                shim_directory = self.root / ("shim-" + "".join(ch for ch in label if ch.isalnum()))
                shim_directory.mkdir()
                # The shim embeds no path, but cmd.exe still reads it from its own path, so that path must be ASCII.
                self.require_ascii_cmd_fixture(shim_directory / "python.cmd")
                (shim_directory / "python.cmd").write_text("@echo off\r\nexit /b 0\r\n", encoding="ascii", newline="")
                environment = {**os.environ}
                set_environment_value(environment, "PATH", os.pathsep.join(
                    [str(shim_directory), str(system / "System32"), str(system)]))
                result = subprocess.run(
                    [engine, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "team.ps1"),
                     "template-status", "--format", 'te"xt'],
                    cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
                )
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 2, output)
                self.assertIn(self.REFUSAL, result.stderr, output)
                self.assertIn("to Python intact", result.stderr, output)


if __name__ == "__main__":
    unittest.main()
