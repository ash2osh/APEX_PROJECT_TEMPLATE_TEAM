import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)


ROOT = Path(__file__).resolve().parents[1]


class TeamCliTests(unittest.TestCase):
    def run_bash_env_loader(self, environment: Path) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            scripts = Path(temporary) / "scripts"
            scripts.mkdir()
            loader = scripts / "load_env.sh"
            shutil.copy2(ROOT / "scripts" / "load_env.sh", loader)
            return subprocess.run(
                [
                    "bash",
                    "-c",
                    'set -e; source "$1" "$2"; printf "%s|%s|%s|%s\\n" "$CODE_SCHEMA" "$CODE_EXPECTED_USER" "$CODE_PREFIXES" "$APEX_PARSING_SCHEMA"',
                    "bash",
                    str(loader),
                    str(environment),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_team_help_lists_primary_commands(self) -> None:
        result = subprocess.run(
            ["bash", str(ROOT / "scripts" / "team.sh"), "--help"],
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
            "check-conflicts",
            "migrate",
            "compare-schema",
            "backup-db",
            "deploy",
            "upgrade-template",
        ):
            self.assertIn(command, result.stdout)
        self.assertIn("--env dev|staging|prod", result.stdout)
        self.assertIn("--pattern", result.stdout)

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
                    ["bash", str(ROOT / "scripts" / "team.sh"), "migrate", *arguments],
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
        pwsh = shutil.which("pwsh")
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
                ["bash", str(ROOT / "scripts" / "team.sh"), "check-conflicts", f"migrations/{folder.name}", "--local", "--repo-root", str(root)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Local selected-batch analysis only", result.stdout)

    def test_publish_command_routes_non_dev_targets_to_deploy(self) -> None:
        result = subprocess.run(
            ["bash", str(ROOT / "scripts" / "team.sh"), "publish", "100", "--env", "prod"],
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
                    "bash", "-c",
                    'set -e; source "$1" "$2"; printf "%s|%s|%s\\n" "$STAGING_SQLCL_CONNECTION" "$STAGING_EXPECTED_USER" "$STAGING_SCHEMA"',
                    "bash", str(ROOT / "scripts" / "load_env.sh"), str(environment),
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
                ["bash", "-c", 'source "$1" "$2"', "bash", str(ROOT / "scripts" / "load_env.sh"), str(environment)],
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
                    "bash", "-c",
                    'export STAGING_SCHEMA=INHERITED PROD_SCHEMA=INHERITED; source "$1" "$2"; printf "%s|%s\\n" "${STAGING_SCHEMA-}" "${PROD_SCHEMA-}"',
                    "bash", str(ROOT / "scripts" / "load_env.sh"), str(environment),
                ],
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "|\n")

    def test_powershell_loader_accepts_and_clears_target_schemas(self) -> None:
        pwsh = shutil.which("pwsh")
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
        pwsh = shutil.which("pwsh")
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
                self.assertIn(expected, powershell_result.stderr)

    def test_bash_loader_matches_ascii_only_whatever_the_users_locale(self) -> None:
        # In en_US.UTF-8, Bash's [A-Z] and [A-Za-z] match accented letters, so
        # the loader accepted values that the PowerShell loader and the Python
        # resolver reject (an accented DEVELOPER_NAME then failed at publish).
        listed = subprocess.run(["locale", "-a"], text=True, capture_output=True, check=False).stdout.lower()
        if "en_us.utf8" not in listed and "en_us.utf-8" not in listed:
            self.skipTest("the en_US.UTF-8 locale is not installed")
        pwsh = shutil.which("pwsh")
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
                    ["bash", "-c", 'set -e; source "$1" "$2"; echo accepted', "bash", str(scripts / "load_env.sh"), str(environment)],
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
        pwsh = shutil.which("pwsh")
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
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        environment = {**os.environ, "PROJECT_ENV_FILE": "/no/such/env"}
        for arguments in (["--schema=DEMO", "doctor"], ["--schema", "-1", "doctor"], ["-x", "doctor"]):
            with self.subTest(arguments=arguments):
                bash_result = subprocess.run(["bash", str(ROOT / "scripts" / "team.sh"), *arguments], env=environment,
                                             text=True, capture_output=True, check=False, cwd=ROOT)
                powershell_result = subprocess.run([pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"), *arguments],
                                                   env=environment, text=True, capture_output=True, check=False, cwd=ROOT)
                self.assertNotEqual(bash_result.returncode, 0)
                self.assertIn("unknown command", bash_result.stderr)
                self.assertNotEqual(powershell_result.returncode, 0, powershell_result.stdout)
                self.assertIn("unknown command", re.sub(r"\x1b\[[0-9;]*m", "", powershell_result.stderr))

    def test_both_wrappers_still_print_usage_for_no_command_and_for_help(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        for arguments in ([], ["--help"], ["-h"]):
            with self.subTest(arguments=arguments):
                for command in (["bash", str(ROOT / "scripts" / "team.sh")], [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1")]):
                    result = subprocess.run([*command, *arguments], text=True, capture_output=True, check=False, cwd=ROOT)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("Usage: scripts/team.", result.stdout)

    def test_argument_errors_read_and_exit_the_same_in_both_wrappers(self) -> None:
        # An uncaught throw makes PowerShell print an exception block with the
        # script path and line and exit 1; Bash prints one `team error:` line and
        # exits 2, which scripts that call the wrappers can rely on.
        pwsh = shutil.which("pwsh")
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
                bash_result = subprocess.run(["bash", str(ROOT / "scripts" / "team.sh"), *arguments], env=environment,
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
        pwsh = shutil.which("pwsh")
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
                for name, command in (("bash", ["bash", str(ROOT / "scripts" / "team.sh"), "doctor"]),
                                      ("powershell", [pwsh, "-NoProfile", "-File", str(ROOT / "scripts" / "team.ps1"), "doctor"])):
                    result = subprocess.run(command, env=environment, text=True, capture_output=True, check=False, cwd=ROOT)
                    plain = re.sub(r"\x1b\[[0-9;]*m", "", result.stderr)
                    self.assertEqual(result.returncode, expected_status, f"{name}: {plain}")
                    self.assertIn(expected_text, " ".join(plain.split()), name)
                    results[name] = plain
                self.assertNotIn("Exception:", results["powershell"])
                self.assertNotIn("Line |", results["powershell"])

    def test_powershell_exits_130_when_interrupted_while_a_helper_runs(self) -> None:
        # Ctrl-C stops PowerShell's own pipeline as well as the helper, so the
        # line that passes the helper's status on never ran and pwsh exited 0,
        # which a caller reads as success. Bash exits 130.
        pwsh = shutil.which("pwsh")
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

    def test_powershell_passes_on_the_status_a_helper_chose_when_it_was_interrupted(self) -> None:
        # migrate reports "interrupted and may be partially applied" with status 2,
        # not 130; the wrapper must not flatten that into a plain interrupt.
        pwsh = shutil.which("pwsh")
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
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        bash_usage = subprocess.run(["bash", str(ROOT / "scripts" / "team.sh"), "--help"],
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
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = root / "record.txt"
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

    def test_powershell_names_the_fix_when_no_usable_bash_is_found(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        environment = os.environ.copy()
        environment.pop("TEAM_BASH", None)
        environment["PATH"] = str(Path(pwsh).parent)  # pwsh itself, but no bash
        if shutil.which("bash", path=environment["PATH"]):
            self.skipTest("bash lives next to pwsh on this machine")
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
            for name in ("team.sh", "load_env.sh", "check_db_target.sh", "doctor.sql", "sqlcl_safe.sh"):
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
                "printf '%s\\n' \"$@\" > \"$FAKE_SQL_LOG\"\n"
                "if [[ -f login.sql ]]; then touch \"$FAKE_LOGIN_MARKER\"; exit 0; fi\n"
                "if [[ -n \"${SQLPATH:-}\" && -f \"$SQLPATH/login.sql\" ]]; then touch \"$FAKE_LOGIN_MARKER\"; exit 0; fi\n"
                "printf 'APEX_DOCTOR_VERIFIED:DEMO\\n'\n",
                encoding="utf-8",
            )
            fake_sql.chmod(0o755)
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
                ["bash", str(scripts / "team.sh"), "doctor"],
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
            self.assertIn(f"@{scripts / 'doctor.sql'}", args)
            doctor_sql = (scripts / "doctor.sql").read_text(encoding="utf-8")
            self.assertIn("WHENEVER SQLERROR EXIT FAILURE ROLLBACK", doctor_sql)
            self.assertIn("@@verify_db_access.sql", doctor_sql)

    def test_interrupting_doctor_removes_its_working_directory(self) -> None:
        # Doctor makes scratch/sqlcl-doctor.* for SQLcl; Ctrl-C used to end the
        # script before the line that removes it.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            for name in ("team.sh", "load_env.sh", "check_db_target.sh", "doctor.sql", "sqlcl_safe.sh"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)
            (scripts / "verify_db_access.sql").write_text("PROMPT identity checked\n", encoding="utf-8")
            (root / ".env").write_text((ROOT / ".env.example").read_text(encoding="utf-8"))
            fake_bin = root / "bin"
            fake_bin.mkdir()
            started = root / "sql-started"
            fake_sql = fake_bin / "sql"
            fake_sql.write_text(f"#!/bin/sh\n: > '{started}'\nsleep 30\n", encoding="utf-8")
            fake_sql.chmod(0o755)
            environment = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", "PROJECT_ENV_FILE": str(root / ".env")}
            process = subprocess.Popen(
                ["bash", str(scripts / "team.sh"), "doctor"], cwd=root, env=environment,
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

    def test_interrupting_a_live_preflight_reports_that_nothing_changed_and_exits_130(self) -> None:
        # check-conflicts --env only reads, so Ctrl-C while SQLcl runs ends it with
        # one line, status 130, no traceback and no scratch directory.
        pwsh = shutil.which("pwsh")
        wrappers = [["bash", "team.sh"]] + ([[pwsh, "-NoProfile", "-File", "team.ps1"]] if pwsh else [])
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

    def make_deploy_checkout(self, root: Path, configure_profile: bool = True) -> tuple[Path, Path]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in (
            "deploy.sh",
            "publish_app.sh",
            "publish_app.sql",
            "sqlcl_safe.sh",
            "load_env.sh",
            "export_apps.sql",
            "verify_db_access.sql",
            "normalize_apx.sh",
            "record_export_state.py",
            "verify_publish_state.py",
            "stamp_publish_version.py",
            "validate_app_source.py",
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
            "mode=other; export_schema=DEMO\n"
            "for arg in \"$@\"; do case \"$arg\" in *@*publish_app.sql) mode=import ;; *@*export_apps.sql) mode=export ;; esac; done\n"
            "if [[ $mode == export ]]; then found_script=0; for arg in \"$@\"; do if [[ $found_script == 1 ]]; then export_schema=$arg; break; fi; case \"$arg\" in *@*export_apps.sql) found_script=1 ;; esac; done; fi\n"
            "if [[ $mode == import ]]; then printf '%s\\n' \"$@\" > \"$FAKE_SQL_LOG\"; printf '%s\\n' 'Import successful.' 'APEX_IMPORT_VERIFIED:100'; fi\n"
            "if [[ $mode == export ]]; then exported=\"$PWD/apps/$export_schema/100\"; mkdir -p \"$exported/.apex\"; cp \"$FAKE_SOURCE_DIR/application.apx\" \"$exported/application.apx\"; cp \"$FAKE_SOURCE_DIR/.apex/apexlang.json\" \"$exported/.apex/apexlang.json\"; printf '%s\\n' '2026-09-26T09:30:00|2026-09-26T09:30:02|Release 1.0' > \"$PWD/.apex-export-before.txt\"; cp \"$PWD/.apex-export-before.txt\" \"$PWD/.apex-export-after.txt\"; fi\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        return scripts / "deploy.sh", sql_log

    def run_deploy(self, script: Path, sql_log: Path, *args: str, input_text: str = ""):
        environment = os.environ.copy()
        environment["PATH"] = f"{script.parents[1] / 'bin'}{os.pathsep}{environment['PATH']}"
        environment["FAKE_SQL_LOG"] = str(sql_log)
        environment["FAKE_SOURCE_DIR"] = (script.parents[1] / "apps/DEMO/100").as_posix()
        environment["PROJECT_ENV_FILE"] = str(script.parents[1] / ".env")
        return subprocess.run(
            ["bash", str(script), "100", "--env", "prod", *args],
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
            self.assertEqual(
                shlex.split(sql_lines[0]),
                [
                    "sql",
                    "-S",
                    "-noupdates",
                    "-name",
                    "prod-db",
                    f"@{script.parents[1] / 'scripts' / 'publish_app.sql'}",
                    "PROD_APP",
                    "production",
                    "PROD_DEPLOYER",
                    str(script.parents[1] / "apps/DEMO/100"),
                    str(script.parents[1] / "apps/DEMO/100/deployments/prod.json"),
                    "100",
                    "-",
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
                    f"@{script.parents[1] / 'scripts' / 'export_apps.sql'}",
                    "PROD_APP",
                    "100",
                    "production",
                    "PROD_DEPLOYER",
                ],
            )
            self.assertIn("verify_publish_state.py", result.stdout)
            self.assertIn("APEX_PUBLISH_SOURCE_VERIFIED:100", result.stdout)
            self.assertFalse(sql_log.exists(), "manual mode must not invoke SQLcl")

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
            self.assertIn(str(script.parents[1] / "apps/DEMO/100/deployments/prod.json"), args)

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
                ["bash", str(project / "scripts" / "team.sh"), "upgrade-template", "--source", str(template)],
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
                ["bash", str(project / "scripts" / "team.sh"), "upgrade-template", "--source", str(template)],
                cwd=project,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(".env needs attention after the upgrade", result.stderr)

    def test_powershell_upgrade_template_command_runs_the_engine(self) -> None:
        pwsh = shutil.which("pwsh")
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
