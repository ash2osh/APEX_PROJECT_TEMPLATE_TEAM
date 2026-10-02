import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)


ROOT = Path(__file__).resolve().parents[1]
TOO_LONG = "1234567890123456789"  # 19 digits
ABSURD = "9" * 300
LONGEST = "123456789012345678"  # 18 digits
MESSAGE = "positive numeric application id of at most 18 digits"


class AppIdLimitTests(unittest.TestCase):
    """An absurdly long application ID must be refused where it is read.

    It used to reach the scratch path as a directory name, so the command died
    with the operating system's "File name too long" instead of saying what was
    wrong with the ID.
    """

    def setUp(self) -> None:
        self.pwsh = shutil.which("pwsh")
        self.example_environment = {**os.environ, "PROJECT_ENV_FILE": str(ROOT / ".env.example")}
        self.missing_environment = {**os.environ, "PROJECT_ENV_FILE": "/no/such/env"}

    def run_command(self, command: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(command, cwd=ROOT, env=environment, text=True, capture_output=True, check=False)
        # PowerShell colours an uncaught error and wraps it behind a "|" gutter.
        plain = " ".join(re.sub(r"\x1b\[[0-9;]*m|\|", " ", result.stderr).split())
        return subprocess.CompletedProcess(result.args, result.returncode, result.stdout, plain)

    def powershell(self, *arguments: str) -> list[str]:
        if self.pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        return [self.pwsh, "-NoProfile", "-File", *arguments]

    def test_every_entry_point_refuses_an_id_of_more_than_18_digits(self) -> None:
        scripts = ROOT / "scripts"
        for label, build, environment in (
            ("team.sh export", lambda value: ["bash", str(scripts / "team.sh"), "export", value], self.missing_environment),
            ("team.sh publish", lambda value: ["bash", str(scripts / "team.sh"), "publish", value], self.missing_environment),
            ("team.sh deploy", lambda value: ["bash", str(scripts / "team.sh"), "deploy", value, "--env", "staging"], self.missing_environment),
            ("publish_app.sh", lambda value: ["bash", str(scripts / "publish_app.sh"), value], self.missing_environment),
            ("deploy.sh", lambda value: ["bash", str(scripts / "deploy.sh"), value, "--env", "staging"], self.missing_environment),
            ("export_apps.sh", lambda value: ["bash", str(scripts / "export_apps.sh"), value], self.example_environment),
            ("team.ps1 export", lambda value: self.powershell(str(scripts / "team.ps1"), "export", value), self.missing_environment),
            ("team.ps1 publish", lambda value: self.powershell(str(scripts / "team.ps1"), "publish", value), self.missing_environment),
            ("publish_app.ps1", lambda value: self.powershell(str(scripts / "publish_app.ps1"), value), self.missing_environment),
            ("export_apps.ps1", lambda value: self.powershell(str(scripts / "export_apps.ps1"), "-AppId", value), self.example_environment),
        ):
            for value in (TOO_LONG, ABSURD):
                with self.subTest(label, digits=len(value)):
                    result = self.run_command(build(value), environment)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(MESSAGE, result.stderr)
                    self.assertNotIn("File name too long", result.stderr)

    def test_an_id_of_18_digits_passes_the_id_check(self) -> None:
        scripts = ROOT / "scripts"
        for label, command in (
            ("team.sh export", ["bash", str(scripts / "team.sh"), "export", LONGEST]),
            ("team.sh publish", ["bash", str(scripts / "team.sh"), "publish", LONGEST]),
            ("team.sh deploy", ["bash", str(scripts / "team.sh"), "deploy", LONGEST, "--env", "staging"]),
            ("publish_app.sh", ["bash", str(scripts / "publish_app.sh"), LONGEST]),
            ("deploy.sh", ["bash", str(scripts / "deploy.sh"), LONGEST, "--env", "staging"]),
        ):
            with self.subTest(label):
                result = self.run_command(command, self.missing_environment)
                # It stops later, at the missing configuration file.
                self.assertNotIn("positive numeric application id", result.stderr)
                self.assertIn("configuration file not found", result.stderr)

        if self.pwsh is not None:
            for label, command in (
                ("team.ps1 export", [self.pwsh, "-NoProfile", "-File", str(scripts / "team.ps1"), "export", LONGEST]),
                ("team.ps1 publish", [self.pwsh, "-NoProfile", "-File", str(scripts / "team.ps1"), "publish", LONGEST]),
                ("publish_app.ps1", [self.pwsh, "-NoProfile", "-File", str(scripts / "publish_app.ps1"), LONGEST]),
            ):
                with self.subTest(label):
                    result = self.run_command(command, self.missing_environment)
                    self.assertNotIn("positive numeric application id", result.stderr)
                    self.assertIn("configuration file not found", result.stderr)

    def test_both_env_loaders_limit_each_apex_app_id_to_18_digits(self) -> None:
        template = (ROOT / ".env.example").read_text(encoding="utf-8")
        for value, accepted in ((f"{LONGEST},200", True), (f"100,{TOO_LONG}", False), (ABSURD, False)):
            with self.subTest(value=value[:40]), tempfile.TemporaryDirectory() as temporary:
                environment = Path(temporary) / ".env"
                environment.write_text(template.replace("APEX_APP_ID=100,200", f"APEX_APP_ID={value}"), encoding="utf-8")
                bash_result = subprocess.run(
                    ["bash", "-c", 'source "$1" "$2"', "bash", str(ROOT / "scripts" / "load_env.sh"), str(environment)],
                    text=True, capture_output=True, check=False,
                )
                results = [("bash", bash_result)]
                if self.pwsh is not None:
                    results.append(("powershell", subprocess.run(
                        [self.pwsh, "-NoProfile", "-Command", f". '{ROOT / 'scripts' / 'load_env.ps1'}' -EnvFile '{environment}'"],
                        text=True, capture_output=True, check=False,
                    )))
                for name, result in results:
                    plain = " ".join(re.sub(r"\x1b\[[0-9;]*m|\|", " ", result.stderr).split())
                    if accepted:
                        self.assertEqual(result.returncode, 0, f"{name}: {plain}")
                    else:
                        self.assertNotEqual(result.returncode, 0, f"{name} accepted {value[:40]}")
                        self.assertIn("at most 18 digits", plain, name)


if __name__ == "__main__":
    unittest.main()
