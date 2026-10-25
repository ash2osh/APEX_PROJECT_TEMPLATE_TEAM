#!/usr/bin/env python3
"""Configure per-checkout Chrome and Firefox DevTools MCP servers."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

CHOICES = ("chrome", "firefox", "both", "none")
BEGIN_MARKER = "# BEGIN team-template browser MCP settings"
END_MARKER = "# END team-template browser MCP settings"
STATE_FILE = ".browser-mcp-selection.json"

BROWSERS: dict[str, dict[str, Any]] = {
    "chrome": {
        "server_name": "chrome-devtools",
        "json": {
            "command": "npx",
            "args": ["-y", "chrome-devtools-mcp@1.9.0", "--autoConnect"],
        },
        "legacy_json": {
            "chrome-devtools-mcp": {
                "command": "npx",
                "args": ["chrome-devtools-mcp@1.9.0", "--autoConnect"],
            }
        },
        "codex_args": ["-y", "chrome-devtools-mcp@1.9.0", "--autoConnect"],
    },
    "firefox": {
        "server_name": "firefox-devtools",
        "json": {
            "command": "npx",
            "args": ["-y", "@mozilla/firefox-devtools-mcp@0.10.6", "--connect-existing"],
        },
        "legacy_json": {},
        "codex_args": ["-y", "@mozilla/firefox-devtools-mcp@0.10.6", "--connect-existing"],
    },
}

MANAGED_JSON: dict[str, list[dict[str, Any]]] = {}
for browser in BROWSERS.values():
    name = browser["server_name"]
    MANAGED_JSON.setdefault(name, []).append(browser["json"])
    for legacy_name, config in browser["legacy_json"].items():
        MANAGED_JSON.setdefault(legacy_name, []).append(config)

MANAGED_TOML_NAMES = {browser["server_name"] for browser in BROWSERS.values()}
MANAGED_TOML_NAMES.add("chrome-devtools-mcp")
TOML_TABLE_RE = re.compile(
    r'^\s*\[\s*mcp_servers\.(?:"([^"]+)"|([A-Za-z0-9_-]+))\s*\]\s*(?:#.*)?$'
)


class SetupError(Exception):
    """A configuration cannot be safely written."""


def selected_browsers(choice: str) -> set[str]:
    if choice == "both":
        return set(BROWSERS)
    if choice == "none":
        return set()
    return {choice}


def prompt_choice() -> str:
    if not sys.stdin.isatty():
        raise SetupError("choose --choice chrome|firefox|both|none when stdin is not interactive")
    labels = ("Chrome", "Firefox", "Both", "None")
    print("Choose browser MCPs for this checkout (default: none):")
    for number, label in enumerate(labels, start=1):
        print(f"  {number}. {label}")
    while True:
        answer = input("Choice [1-4, default 4]: ").strip().lower()
        if not answer:
            return "none"
        mapping = {"1": "chrome", "2": "firefox", "3": "both", "4": "none"}
        choice = mapping.get(answer, answer)
        if choice in CHOICES:
            return choice
        print("Enter 1, 2, 3, 4, chrome, firefox, both, or none.")


def read_json_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"mcpServers": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SetupError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SetupError(f"{path} must contain a JSON object")
    servers = value.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise SetupError(f"{path} mcpServers must be a JSON object")
    for name in MANAGED_JSON:
        existing = servers.get(name)
        if existing is not None and existing not in MANAGED_JSON[name]:
            raise SetupError(
                f"{path} already defines {name} with a different configuration; "
                "move that entry before running mcp setup"
            )
    return value


def render_json_config(path: Path, choice: str) -> bytes | None:
    value = read_json_config(path)
    servers = value.get("mcpServers", {})
    for name in MANAGED_JSON:
        servers.pop(name, None)
    selected = selected_browsers(choice)
    if selected and "mcpServers" not in value:
        value["mcpServers"] = servers
    if selected:
        servers = value["mcpServers"]
    for key, browser in BROWSERS.items():
        if key not in selected:
            continue
        servers[browser["server_name"]] = browser["json"]
    if not servers:
        value.pop("mcpServers", None)
    if not value:
        return None
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def toml_managed_table(line: str) -> str | None:
    match = TOML_TABLE_RE.match(line)
    if not match:
        return None
    return match.group(1) or match.group(2)


def render_codex_config(path: Path, choice: str) -> bytes | None:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = current.splitlines(keepends=True)
    begin_indexes = [i for i, line in enumerate(lines) if line.strip() == BEGIN_MARKER]
    end_indexes = [i for i, line in enumerate(lines) if line.strip() == END_MARKER]

    if begin_indexes or end_indexes:
        if len(begin_indexes) != 1 or len(end_indexes) != 1 or begin_indexes[0] >= end_indexes[0]:
            raise SetupError(f"{path} has incomplete or repeated browser MCP markers")
        before = lines[: begin_indexes[0]]
        after = lines[end_indexes[0] + 1 :]
    else:
        before = lines
        after = []
        for line in lines:
            name = toml_managed_table(line)
            if name in MANAGED_TOML_NAMES:
                raise SetupError(
                    f"{path} already defines {name} outside the managed block; "
                    "move that table before running mcp setup"
                )

    selected = selected_browsers(choice)
    for line in before + after:
        name = toml_managed_table(line)
        if name in MANAGED_TOML_NAMES:
            raise SetupError(
                f"{path} also defines {name} outside the managed block; remove the duplicate table"
            )

    block: list[str] = []
    if selected:
        block = [BEGIN_MARKER + "\n"]
        for key, browser in BROWSERS.items():
            if key not in selected:
                continue
            block.extend(
                [
                    f"[mcp_servers.{browser['server_name']}]\n",
                    'command = "npx"\n',
                    f"args = {json.dumps(browser['codex_args'])}\n",
                    "enabled = true\n",
                    "startup_timeout_sec = 60\n",
                    "\n",
                ]
            )
        block.append(END_MARKER + "\n")

    prefix = "".join(before)
    suffix = "".join(after)
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if suffix and not suffix.startswith("\n"):
        suffix = "\n" + suffix
    rendered = prefix + "".join(block) + suffix
    if not rendered:
        return None
    return rendered.encode("utf-8")


def git_result(repo_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise SetupError(f"cannot run Git for {repo_root}: {exc}") from exc


def validate_repo_root(repo_root: Path) -> None:
    result = git_result(repo_root, "rev-parse", "--show-toplevel")
    if result.returncode:
        raise SetupError(f"{repo_root} is not a Git checkout")
    top_level = Path(result.stdout.strip()).resolve()
    if os.path.normcase(str(top_level)) != os.path.normcase(str(repo_root.resolve())):
        raise SetupError(f"{repo_root} is not the root of its Git checkout ({top_level})")


def validate_target_path(repo_root: Path, relative_path: str) -> Path:
    target = repo_root / relative_path
    cursor = repo_root
    for part in Path(relative_path).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise SetupError(f"refusing symlink in browser MCP config path: {relative_path}")
    try:
        target.resolve(strict=False).relative_to(repo_root.resolve())
    except ValueError as exc:
        raise SetupError(f"browser MCP config path escapes the checkout: {relative_path}") from exc
    if target.exists() and not target.is_file():
        raise SetupError(f"browser MCP config target is not a file: {relative_path}")
    return target


def tracked_by_git(repo_root: Path, relative_path: str) -> bool:
    result = git_result(repo_root, "ls-files", "--error-unmatch", "--", relative_path)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise SetupError(f"Git could not check whether {relative_path} is tracked: {result.stderr.strip()}")


def ignored_by_git(repo_root: Path, relative_path: str) -> bool:
    result = git_result(repo_root, "check-ignore", "--quiet", "--", relative_path)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise SetupError(f"Git could not check whether {relative_path} is ignored: {result.stderr.strip()}")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def setup(repo_root: Path, choice: str | None) -> int:
    choice = choice or prompt_choice()
    if choice not in CHOICES:
        raise SetupError(f"invalid choice {choice!r}; use chrome, firefox, both, or none")

    validate_repo_root(repo_root)
    targets = [".mcp.json", ".codex/config.toml", STATE_FILE]
    for relative_path in targets:
        validate_target_path(repo_root, relative_path)
        if tracked_by_git(repo_root, relative_path):
            raise SetupError(
                f"{relative_path} is tracked by Git; move it out of version control "
                "before saving a per-developer browser MCP choice"
            )
        if not ignored_by_git(repo_root, relative_path):
            raise SetupError(
                f"{relative_path} must be ignored by Git before saving a per-developer browser MCP choice"
            )

    mcp_path = repo_root / ".mcp.json"
    codex_path = repo_root / ".codex" / "config.toml"
    state_path = repo_root / STATE_FILE

    # Validate and render every target before changing any file.
    mcp_data = render_json_config(mcp_path, choice)
    codex_data = render_codex_config(codex_path, choice)
    selected = selected_browsers(choice)
    state_data = (
        json.dumps(
            {
                "schemaVersion": 1,
                "selection": choice,
                "servers": {
                    BROWSERS[key]["server_name"]: BROWSERS[key]["json"]
                    for key in BROWSERS
                    if key in selected
                },
            },
            indent=2,
        )
        + "\n"
    ).encode("utf-8")

    if mcp_data is None:
        mcp_path.unlink(missing_ok=True)
    else:
        atomic_write(mcp_path, mcp_data)
    if codex_data is None:
        codex_path.unlink(missing_ok=True)
        try:
            codex_path.parent.rmdir()
        except OSError:
            pass
    else:
        atomic_write(codex_path, codex_data)
    atomic_write(state_path, state_data)
    print(f"Browser MCP selection saved: {choice}")
    print(f"Updated {mcp_path}")
    print(f"Updated {codex_path}")
    print("Reload the coding client; Codex loads project MCP settings only for trusted checkouts.")
    print("No browser or MCP process was started.")
    return 0


def status(repo_root: Path) -> int:
    path = repo_root / STATE_FILE
    if not path.exists():
        print("Browser MCP is not configured for this checkout. Run: scripts/team.sh mcp setup")
        return 0
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        choice = value.get("selection")
        if value.get("schemaVersion") != 1 or choice not in CHOICES:
            raise ValueError("unsupported state file")
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, ValueError) as exc:
        raise SetupError(f"cannot read {path}: {exc}") from exc
    print(f"Browser MCP selection: {choice}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parent.parent)
    commands = parser.add_subparsers(dest="command", required=True)
    setup_parser = commands.add_parser("setup", help="choose browser MCP servers for this checkout")
    setup_parser.add_argument("--choice", choices=CHOICES, help="skip the interactive prompt")
    commands.add_parser("status", help="show the saved browser MCP selection")
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()

    try:
        if args.command == "setup":
            return setup(repo_root, args.choice)
        return status(repo_root)
    except SetupError as exc:
        print(f"mcp error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
