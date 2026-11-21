#!/usr/bin/env python3
"""Run the quick CI checks locally before a push: syntax, Ruff, shell syntax, ShellCheck, PowerShell parsing and the tests you changed.

By default it checks only the files you changed (against the upstream branch or origin/main, plus uncommitted and
untracked files; with neither, every tracked file), so existing problems elsewhere in a project do not block you. `--all`
compiles and lints every file under scripts/, tools/ and tests/, as CI does; use it before opening a pull request for a broad change.

ShellCheck (error severity) runs when `shellcheck` is on PATH. The PowerShell parse runs with `pwsh`, or on Windows with
Windows PowerShell when `pwsh` is missing. The shell syntax check uses Git Bash on Windows, never the WSL launcher in
System32. A missing tool prints a one-line note and does not fail the check.

It also always runs the contract tests (template manifest, documentation contract, portability guards) when they exist, and in a
template checkout it refuses untracked files under scripts/, tests/, tools/ and docs/, because CI sees only tracked files.

CI runs the full suite on Linux, Windows and macOS, which takes 10 to 30 minutes. Most failed runs were lint errors or a
changed test that fails on Linux, both caught here in seconds. This does not run the whole suite or any Windows check.
Read-only except `--install-hook`.

  python3 scripts/pre_push_check.py                 check the files you changed
  python3 scripts/pre_push_check.py --all           check everything CI lints
  python3 scripts/pre_push_check.py --install-hook  also run the check on every `git push` (never replaces a hook)
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_DIRS = ("scripts", "tools", "tests")
# The Windows WSL launcher lives in System32 (or a similar system folder); see find_bash.
_WSL_LAUNCHER_RE = re.compile(r"(?i)[\\/](System32|SysWOW64|Sysnative|WindowsApps)[\\/]")
# A checkout from before the script existed skips the check instead of refusing every push.
HOOK = (
    "#!/bin/sh\n"
    "if [ ! -f scripts/pre_push_check.py ]; then\n"
    "  echo 'pre-push: scripts/pre_push_check.py is not in this checkout; check skipped' >&2\n"
    "  exit 0\n"
    "fi\n"
    "exec python3 scripts/pre_push_check.py\n"
)


def existing(names: Sequence[str], root: Path = ROOT) -> list[str]:
    return [name for name in names if (root / name).is_dir()]


def changed_python_files(paths: Sequence[str], root: Path = ROOT) -> list[str]:
    """Changed `.py` files under scripts/, tools/ and tests/ that still exist, sorted, each once."""
    return sorted({p for p in paths if p.endswith(".py") and p.split("/", 1)[0] in CODE_DIRS and (root / p).is_file()})


def changed_shell_scripts(paths: Sequence[str], root: Path = ROOT) -> list[str]:
    """Changed `scripts/*.sh` files that still exist, sorted, each once."""
    return sorted({p for p in paths if p.startswith("scripts/") and "/" not in p[len("scripts/"):] and p.endswith(".sh") and (root / p).is_file()})


def changed_powershell_scripts(paths: Sequence[str], root: Path = ROOT) -> list[str]:
    """Changed `scripts/*.ps1` files that still exist, sorted, each once."""
    return sorted({p for p in paths if p.startswith("scripts/") and "/" not in p[len("scripts/"):] and p.endswith(".ps1") and (root / p).is_file()})


def powershell_parse_command(pwsh: str, files: Sequence[str]) -> list[str]:
    """A `pwsh` command that parses each file without running it, and exits 1 when any file has a parse error."""
    quoted = ", ".join("'" + name.replace("'", "''") + "'" for name in files)
    script = (
        f"$bad = 0; foreach ($file in @({quoted})) {{ "
        "$tokens = $null; $errors = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile($file, [ref]$tokens, [ref]$errors); "
        "foreach ($problem in $errors) { Write-Host ($file + ':' + $problem.Extent.StartLineNumber + ': ' + $problem.Message); $bad++ } "
        "}; if ($bad -gt 0) { exit 1 }"
    )
    return [pwsh, "-NoProfile", "-Command", script]


def find_bash(which: Callable[[str], str | None] | None = None, environ: Mapping[str, str] | None = None,
              is_windows: bool | None = None) -> str | None:
    """Return the Bash for the shell syntax check, or None when there is none.

    Off Windows this is the Bash on PATH. On Windows, CreateProcess searches System32 before PATH, so a bare "bash"
    can start the WSL launcher, which fails when WSL has no distribution. Use TEAM_BASH when it names a file, else the
    first bash.exe on PATH that is not the launcher, else the Git for Windows copy beside git.exe. Never returns the
    launcher or a bare "bash".
    """
    if which is None:
        which = shutil.which
    if environ is None:
        environ = os.environ
    if is_windows is None:
        is_windows = os.name == "nt"
    if not is_windows:
        return which("bash")
    override = environ.get("TEAM_BASH")
    if override and os.path.isfile(override):
        return override
    for entry in environ.get("PATH", "").split(os.pathsep):
        directory = entry.strip('"')
        if not directory:
            continue
        candidate = os.path.join(directory, "bash.exe")
        if os.path.isfile(candidate) and _WSL_LAUNCHER_RE.search(candidate) is None:
            return candidate
    git = which("git")
    if git:
        base = os.path.dirname(git)
        for relative in (("..", "bin"), ("..", "..", "bin"), ("..", "usr", "bin"), ("..", "..", "usr", "bin")):
            candidate = os.path.normpath(os.path.join(base, *relative, "bash.exe"))
            if os.path.isfile(candidate):
                return candidate
    return None


def find_powershell(which: Callable[[str], str | None] | None = None, is_windows: bool | None = None) -> tuple[str, str] | None:
    """Return (path, step label) for the PowerShell that parses the scripts, or None when there is none.

    pwsh is preferred. On Windows, Windows PowerShell (5.1) is the fallback, since it is the team's main engine.
    """
    if which is None:
        which = shutil.which
    if is_windows is None:
        is_windows = os.name == "nt"
    pwsh = which("pwsh")
    if pwsh:
        return pwsh, "PowerShell parse"
    windows_powershell = which("powershell") if is_windows else None
    if windows_powershell:
        return windows_powershell, "PowerShell parse (Windows PowerShell)"
    return None


# Contract modules run on every check, changed or not. A downstream project removes them, so a missing one is skipped.
CONTRACT_TESTS = (
    "tests/test_template_manifest.py",
    "tests/test_documentation_contract.py",
    "tests/test_portability_guards.py",
)
# Only the template's own tracked source is checked for untracked files; CI and the manifest tests see only tracked files.
UNTRACKED_CHECKED_DIRS = ("scripts", "tests", "tools", "docs")


def changed_test_files(paths: Sequence[str], root: Path = ROOT) -> list[str]:
    """Test modules among the changed paths that still exist: tests/test_*.py, sorted, each once."""
    return sorted({
        p for p in paths
        if p.startswith("tests/") and "/" not in p[len("tests/"):] and Path(p).name.startswith("test_") and p.endswith(".py")
        and (root / p).is_file()
    })


def test_modules_to_run(paths: Sequence[str], root: Path = ROOT) -> list[str]:
    """Changed test modules plus the contract modules that exist, sorted, each once."""
    contract = [name for name in CONTRACT_TESTS if (root / name).is_file()]
    return sorted({*changed_test_files(paths, root), *contract})


def is_template_checkout(root: Path = ROOT) -> bool:
    """A template carries .core-stamp.json. A downstream project has .template-lock.json, even if it inherited the stamp."""
    return (root / ".core-stamp.json").is_file() and not (root / ".template-lock.json").exists()


def git_lines(*args: str) -> list[str]:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    return result.stdout.splitlines() if result.returncode == 0 else []


def comparison_base() -> str | None:
    """The upstream branch, else origin/main, that the changes are compared with; None when neither exists."""
    return next((ref for ref in ("@{upstream}", "origin/main") if git_lines("rev-parse", "--verify", "-q", ref)), None)


def changed_paths() -> list[str]:
    """Files changed against the upstream branch (or origin/main), plus uncommitted and untracked ones.

    With no comparison base (for example the first push of a new branch), every tracked file is returned, so a
    first push cannot pass having checked nothing.
    """
    base = comparison_base()
    if base:
        names = set(git_lines("diff", "--name-only", f"{base}...HEAD"))
    else:
        names = set(git_lines("ls-files"))
    names.update(git_lines("diff", "--name-only", "HEAD"))
    names.update(git_lines("ls-files", "--others", "--exclude-standard"))
    return sorted(names)


def untracked_step(files: Sequence[str]) -> bool:
    """Print each untracked file and report the step; any untracked file fails it."""
    print("== Untracked files", flush=True)
    for name in files:
        print(f"untracked file {name}: git add it or ignore it; CI and the manifest/ownership tests see only tracked files", flush=True)
    print(f"   {'ok' if not files else 'FAILED'}", flush=True)
    return not files


def step(label: str, command: Sequence[str]) -> bool:
    start = time.monotonic()
    print(f"== {label}", flush=True)
    code = subprocess.run(command, cwd=ROOT).returncode
    print(f"   {'ok' if code == 0 else 'FAILED'} ({time.monotonic() - start:.1f}s)", flush=True)
    return code == 0


def install_hook() -> int:
    paths = git_lines("rev-parse", "--git-path", "hooks/pre-push")
    if not paths:
        print("not inside a Git repository", file=sys.stderr)
        return 2
    hook = (ROOT / paths[0]).resolve()
    if hook.exists():
        print(f"{hook} already exists; not replaced. Add this line to it: python3 scripts/pre_push_check.py")
        return 0
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(HOOK, encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    print(f"installed {hook}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--all", action="store_true", help="compile and lint every file under scripts/, tools/ and tests/ (as CI does), not only your changes; tests still run only for the changed test modules, the full suite runs in CI")
    parser.add_argument("--install-hook", action="store_true", help="install a pre-push hook that runs this script")
    args = parser.parse_args(argv)
    if args.install_hook:
        return install_hook()
    # Hooks export repository-local Git variables. Fixture tests use other
    # repositories; inheriting these redirects their Git writes into this one.
    for name in git_lines("rev-parse", "--local-env-vars"):
        os.environ.pop(name, None)
    if not args.all and comparison_base() is None:
        print("note: no upstream branch or origin/main to compare with; checking every tracked file", flush=True)
    paths = changed_paths()
    if args.all:
        python_targets = existing(CODE_DIRS, ROOT)
        shell_targets = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "scripts").glob("*.sh"))
        powershell_targets = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "scripts").glob("*.ps1"))
        print("mode: all files")
    else:
        python_targets = changed_python_files(paths, ROOT)
        shell_targets = changed_shell_scripts(paths, ROOT)
        powershell_targets = changed_powershell_scripts(paths, ROOT)
        print(f"mode: changed files ({len(python_targets)} Python, {len(shell_targets)} shell, "
              f"{len(powershell_targets)} PowerShell); --all checks everything")
    results: list[tuple[str, bool]] = []
    if python_targets:
        results.append(("Python syntax", step("Python syntax", [sys.executable, "-m", "compileall", "-q", *python_targets])))
        ruff = shutil.which("ruff")
        if ruff:
            results.append(("Ruff", step("Ruff", [ruff, "check", *python_targets])))
        else:
            print("== Ruff\n   MISSING: install it with: python3 -m pip install 'ruff>=0.5'", file=sys.stderr)
            results.append(("Ruff", False))
    bash = find_bash()
    if bash:
        for name in shell_targets:
            label = f"Shell syntax {name}"
            results.append((label, step(label, [bash, "-n", name])))
    elif shell_targets:
        print("note: Git Bash (bash) is not on PATH, so the shell syntax check was not run (CI runs it on Linux and Windows)", flush=True)
    if shell_targets:
        shellcheck = shutil.which("shellcheck")
        if shellcheck:
            label = "ShellCheck (error severity)"
            results.append((label, step(label, [shellcheck, "--severity=error", *shell_targets])))
        else:
            print("note: shellcheck is not on PATH, so ShellCheck was not run (CI runs it on Linux)", flush=True)
    if powershell_targets:
        found = find_powershell()
        if found:
            powershell, label = found
            results.append((label, step(label, powershell_parse_command(powershell, powershell_targets))))
        elif os.name == "nt":
            print("note: neither pwsh nor Windows PowerShell is on PATH, so the PowerShell parse was not run (CI runs it on Linux and Windows)", flush=True)
        else:
            print("note: pwsh is not on PATH, so the PowerShell parse was not run (CI runs it on Linux and Windows)", flush=True)
    if is_template_checkout(ROOT):
        untracked = git_lines("ls-files", "--others", "--exclude-standard", "--", *UNTRACKED_CHECKED_DIRS)
        results.append(("Untracked files", untracked_step(untracked)))
    changed_tests = set(changed_test_files(paths, ROOT))
    for test in test_modules_to_run(paths, ROOT):
        label = f"Changed test {test}" if test in changed_tests else f"Contract test {test}"
        results.append((label, step(label, [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", Path(test).name, "-v"])))
    # A template carries the shared core files with a stamp. Editing one of them in the template fails here:
    # fix it in the core repository and sync it. A downstream project has no stamp, so nothing runs. A downstream
    # project can still inherit a stale .core-stamp.json; its .template-lock.json marks it as downstream, so skip then.
    checker = ROOT / "scripts" / "sync_core.py"
    if is_template_checkout(ROOT) and checker.is_file():
        label = "Shared core files (edit them in APEX_TEMPLATE_CORE, then sync)"
        results.append((label, step(label, [sys.executable, str(checker), "check", str(ROOT)])))
    if not results:
        print("nothing to check: no changed Python, shell, PowerShell or test files")
    failed = [label for label, ok in results if not ok]
    if failed:
        print("some checks failed; fix them before pushing", flush=True)
        print("CHECKS FAILED: " + "; ".join(failed), flush=True)
        return 1
    print("all checks passed; CI also runs the full suite on Linux, Windows and macOS", flush=True)
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
