#!/usr/bin/env python3
"""Run the quick CI checks locally before a push: syntax, Ruff, shell syntax and the tests you changed.

By default it checks only the files you changed (against the upstream branch or origin/main, plus uncommitted and
untracked files), so existing problems elsewhere in a project do not block you. `--all` checks every file under
scripts/, tools/ and tests/, as CI does; use it before opening a pull request for a broad change.

CI runs the full suite on Linux, Windows and macOS, which takes 10 to 30 minutes. Most failed runs were lint errors or a
changed test that fails on Linux, both caught here in seconds. This does not run the whole suite or any Windows check.
Read-only except `--install-hook`.

  python3 scripts/pre_push_check.py                 check the files you changed
  python3 scripts/pre_push_check.py --all           check everything CI lints
  python3 scripts/pre_push_check.py --install-hook  also run the check on every `git push` (never replaces a hook)
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODE_DIRS = ("scripts", "tools", "tests")
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


def changed_test_files(paths: Sequence[str]) -> list[str]:
    """Test modules among the changed paths: tests/test_*.py, sorted, each once."""
    return sorted({p for p in paths if p.startswith("tests/") and "/" not in p[len("tests/"):] and Path(p).name.startswith("test_") and p.endswith(".py")})


def git_lines(*args: str) -> list[str]:
    result = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    return result.stdout.splitlines() if result.returncode == 0 else []


def changed_paths() -> list[str]:
    """Files changed against the upstream branch (or origin/main), plus uncommitted and untracked ones."""
    base = next((ref for ref in ("@{upstream}", "origin/main") if git_lines("rev-parse", "--verify", "-q", ref)), None)
    names = set(git_lines("diff", "--name-only", f"{base}...HEAD") if base else [])
    names.update(git_lines("diff", "--name-only", "HEAD"))
    names.update(git_lines("ls-files", "--others", "--exclude-standard"))
    return sorted(names)


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
    parser.add_argument("--all", action="store_true", help="check every file under scripts/, tools/ and tests/ (as CI does), not only your changes")
    parser.add_argument("--install-hook", action="store_true", help="install a pre-push hook that runs this script")
    args = parser.parse_args(argv)
    if args.install_hook:
        return install_hook()
    paths = changed_paths()
    if args.all:
        python_targets = existing(CODE_DIRS)
        shell_targets = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "scripts").glob("*.sh"))
        print("mode: all files")
    else:
        python_targets = changed_python_files(paths)
        shell_targets = changed_shell_scripts(paths)
        print(f"mode: changed files ({len(python_targets)} Python, {len(shell_targets)} shell); --all checks everything")
    results = []
    if python_targets:
        results.append(step("Python syntax", [sys.executable, "-m", "compileall", "-q", *python_targets]))
        ruff = shutil.which("ruff")
        if ruff:
            results.append(step("Ruff", [ruff, "check", *python_targets]))
        else:
            print("== Ruff\n   MISSING: install it with: python3 -m pip install 'ruff>=0.5'", file=sys.stderr)
            results.append(False)
    bash = shutil.which("bash")
    if bash:
        for name in shell_targets:
            results.append(step(f"Shell syntax {name}", [bash, "-n", name]))
    for test in changed_test_files(paths):
        results.append(step(f"Changed test {test}", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", Path(test).name, "-v"]))
    if not results:
        print("nothing to check: no changed Python, shell or test files")
    print("all checks passed; CI also runs the full suite on Linux, Windows and macOS" if all(results) else "CHECKS FAILED: fix them before pushing", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
