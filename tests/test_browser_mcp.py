"""Checkout-local browser MCP selection and safety regressions."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import _test_subprocess_safety  # noqa: F401
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.sqlcl_session import bash_command


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "browser_mcp.py"
EXPECTED = {
    "chrome": {
        "command": "npx",
        "args": ["-y", "chrome-devtools-mcp@1.9.0", "--autoConnect"],
    },
    "firefox": {
        "command": "npx",
        "args": ["-y", "@mozilla/firefox-devtools-mcp@0.10.6", "--connect-existing"],
    },
}


class InteractiveInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class BrowserMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def make_repo(self, name: str = "checkout") -> Path:
        repo = self.base / name
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q", "-b", "main"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Browser MCP tests"], check=True)
        (repo / ".gitignore").write_text(
            "/.mcp.json\n/.browser-mcp-selection.json\n/.codex/config.toml\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "fixture"], check=True)
        return repo

    def run_setup(self, repo: Path, choice: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--repo-root", str(repo), "setup", "--choice", choice],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )

    def config_files(self, repo: Path) -> dict[str, bytes | None]:
        paths = (".mcp.json", ".codex/config.toml", ".browser-mcp-selection.json")
        return {
            relative: (repo / relative).read_bytes() if (repo / relative).is_file() else None
            for relative in paths
        }

    def test_setup_choice_writes_selected_servers(self) -> None:
        repo = self.make_repo()
        selected = {
            "chrome": {"chrome-devtools"},
            "firefox": {"firefox-devtools"},
            "both": {"chrome-devtools", "firefox-devtools"},
            "none": set(),
        }
        for choice, server_names in selected.items():
            with self.subTest(choice=choice):
                result = self.run_setup(repo, choice)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                config_path = repo / ".mcp.json"
                config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
                expected = {
                    server_name: EXPECTED[browser]
                    for browser, server_name in (
                        ("chrome", "chrome-devtools"),
                        ("firefox", "firefox-devtools"),
                    )
                    if server_name in server_names
                }
                self.assertEqual(config.get("mcpServers", {}), expected)

                toml_path = repo / ".codex/config.toml"
                toml = toml_path.read_text(encoding="utf-8") if toml_path.exists() else ""
                tables = set(re.findall(r"^\[mcp_servers\.([A-Za-z0-9_-]+)\]$", toml, re.MULTILINE))
                self.assertEqual(tables, server_names)
                for browser, server_name in (
                    ("chrome", "chrome-devtools"),
                    ("firefox", "firefox-devtools"),
                ):
                    if server_name in server_names:
                        self.assertIn(f"args = {json.dumps(EXPECTED[browser]['args'])}", toml)

                state = json.loads((repo / ".browser-mcp-selection.json").read_text(encoding="utf-8"))
                self.assertEqual(state["selection"], choice)
                self.assertEqual(set(state["servers"]), server_names)
                self.assertIn("No browser or MCP process was started.", result.stdout)
                for relative in (".mcp.json", ".codex/config.toml", ".browser-mcp-selection.json"):
                    ignored = subprocess.run(
                        ["git", "-C", str(repo), "check-ignore", "--quiet", "--", relative],
                        check=False,
                    )
                    self.assertEqual(ignored.returncode, 0, f"{relative} is not ignored")

    def test_none_preserves_unrelated_entries(self) -> None:
        repo = self.make_repo()
        unrelated_json = {"command": "python", "args": ["fixture-mcp"]}
        (repo / ".mcp.json").write_text(
            json.dumps(
                {
                    "vendorSetting": "keep",
                    "mcpServers": {
                        "local-helper": unrelated_json,
                        "chrome-devtools": EXPECTED["chrome"],
                        "firefox-devtools": EXPECTED["firefox"],
                        "chrome-devtools-mcp": {
                            "command": "npx",
                            "args": ["chrome-devtools-mcp@1.9.0", "--autoConnect"],
                        },
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        (repo / ".codex").mkdir()
        (repo / ".codex/config.toml").write_text(
            'model = "keep-me"\n\n[mcp_servers.local-helper]\ncommand = "python"\nargs = ["fixture-mcp"]\n\n'
            "# BEGIN team-template browser MCP settings\n"
            '[mcp_servers.chrome-devtools]\ncommand = "npx"\n\n'
            '[mcp_servers.firefox-devtools]\ncommand = "npx"\n'
            "# END team-template browser MCP settings\n\n[other]\nvalue = true\n",
            encoding="utf-8",
        )

        result = self.run_setup(repo, "none")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = json.loads((repo / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(config["vendorSetting"], "keep")
        self.assertEqual(config["mcpServers"], {"local-helper": unrelated_json})
        toml = (repo / ".codex/config.toml").read_text(encoding="utf-8")
        self.assertIn('model = "keep-me"', toml)
        self.assertIn("[mcp_servers.local-helper]", toml)
        self.assertIn("[other]", toml)
        self.assertNotIn("chrome-devtools", toml)
        self.assertNotIn("firefox-devtools", toml)
        self.assertNotIn("team-template browser MCP settings", toml)

    def test_unignored_config_refuses_before_writing_any_target(self) -> None:
        repo = self.make_repo("not-ignored")
        (repo / ".gitignore").write_text("/.mcp.json\n/.codex/config.toml\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "ignore only client files"], check=True)
        before = self.config_files(repo)

        result = self.run_setup(repo, "firefox")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("must be ignored by Git", result.stderr)
        self.assertEqual(self.config_files(repo), before)

    def test_setup_refuses_a_symlinked_config_parent(self) -> None:
        repo = self.make_repo("symlink-parent")
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "config.toml").write_text('model = "keep"\n', encoding="utf-8")
        (repo / ".codex").symlink_to(outside, target_is_directory=True)
        before = (outside / "config.toml").read_bytes()

        result = self.run_setup(repo, "chrome")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("symlink", result.stderr.lower())
        self.assertEqual((outside / "config.toml").read_bytes(), before)
        self.assertFalse((repo / ".mcp.json").exists())
        self.assertFalse((repo / ".browser-mcp-selection.json").exists())

    def test_conflicting_or_tracked_config_refuses_without_writes(self) -> None:
        conflict_repo = self.make_repo("conflict")
        (conflict_repo / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"chrome-devtools": {"command": "custom"}}}),
            encoding="utf-8",
        )
        (conflict_repo / ".codex").mkdir()
        (conflict_repo / ".codex/config.toml").write_text('model = "preserve"\n', encoding="utf-8")
        before = self.config_files(conflict_repo)
        result = self.run_setup(conflict_repo, "firefox")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("different configuration", result.stderr)
        self.assertEqual(self.config_files(conflict_repo), before)

        tracked_repo = self.make_repo("tracked")
        (tracked_repo / ".mcp.json").write_text(
            json.dumps({"mcpServers": {"manual": {"command": "preserve"}}}), encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(tracked_repo), "add", "-f", ".mcp.json"], check=True)
        subprocess.run(["git", "-C", str(tracked_repo), "commit", "-q", "-m", "tracked config"], check=True)
        before = self.config_files(tracked_repo)
        result = self.run_setup(tracked_repo, "both")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("tracked by Git", result.stderr)
        self.assertEqual(self.config_files(tracked_repo), before)

    def test_prompt_maps_all_choices(self) -> None:
        spec = importlib.util.spec_from_file_location("browser_mcp_prompt_test", SCRIPT)
        self.assertIsNotNone(spec, "browser_mcp.py must exist")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with patch.object(module.sys, "stdin", InteractiveInput("")), patch(
            "builtins.input", side_effect=("1", "2", "3", "4")
        ):
            self.assertEqual(
                [module.prompt_choice() for _ in range(4)],
                ["chrome", "firefox", "both", "none"],
            )

    def test_team_shell_mcp_setup_and_status_dispatch(self) -> None:
        repo = self.make_repo()
        scripts = repo / "scripts"
        scripts.mkdir()
        shutil.copy2(ROOT / "scripts/team.sh", scripts / "team.sh")
        if SCRIPT.is_file():
            shutil.copy2(SCRIPT, scripts / "browser_mcp.py")
        bash = bash_command()

        setup = subprocess.run(
            [bash, str(scripts / "team.sh"), "mcp", "setup", "--choice", "firefox"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(setup.returncode, 0, setup.stdout + setup.stderr)
        status = subprocess.run(
            [bash, str(scripts / "team.sh"), "mcp", "status"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertIn("Browser MCP selection: firefox", status.stdout)

    def test_powershell_wrapper_dispatches_mcp_when_available(self) -> None:
        powershell = _test_subprocess_safety.working_powershell()
        if not powershell:
            self.skipTest("PowerShell is not installed")

        repo = self.make_repo("powershell")
        scripts = repo / "scripts"
        scripts.mkdir()
        for name in ("team.ps1", "resolve_python.ps1", "browser_mcp.py"):
            source = ROOT / "scripts" / name
            if name == "browser_mcp.py":
                source = SCRIPT
            shutil.copy2(source, scripts / name)

        result = subprocess.run(
            [powershell, "-NoProfile", "-File", str(scripts / "team.ps1"), "mcp", "setup", "--choice", "firefox"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads((repo / ".browser-mcp-selection.json").read_text())['selection'], "firefox")


if __name__ == "__main__":
    unittest.main()
