#!/usr/bin/env python3
"""Print one read-only update notice for the lock's recorded branch or tag."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.upgrade_template import UpgradeError, read_lock


SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z", re.ASCII)


def update_notice(root: Path) -> str | None:
    """Missing, pinned, ambiguous and unavailable observations stay silent."""
    try:
        lock = read_lock(root)
        recorded, ref, source = (lock.get(k) for k in ("commit", "templateRef", "upstream"))
        if not all(isinstance(v, str) and v for v in (recorded, ref, source)):
            return None
        if not SHA.fullmatch(recorded) or SHA.fullmatch(ref):
            return None
        if ref.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in ref):
            return None
        if any(ord(c) < 32 or ord(c) == 127 for c in source):
            return None
        candidate = Path(source).expanduser()
        if candidate.is_dir():
            source = str(candidate.resolve())
        else:
            url = urlsplit(source)
            if (url.scheme != "https" or not url.hostname or url.username is not None
                    or url.password is not None or url.query or url.fragment or "\\" in source):
                return None
        environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never", GIT_OPTIONAL_LOCKS="0")
        valid = subprocess.run(
            ["git", "check-ref-format", "--branch", ref], cwd=root, env=environment,
            capture_output=True, text=True, check=False, timeout=10,
        )
        if valid.returncode != 0:
            return None
        if ref.startswith(("refs/heads/", "refs/tags/")):
            names = [ref]
        elif ref.startswith("refs/"):
            return None
        else:
            names = ["refs/heads/" + ref, "refs/tags/" + ref]
        patterns = names + [name + "^{}" for name in names if name.startswith("refs/tags/")]
        result = subprocess.run(
            ["git", "ls-remote", "--exit-code", "--", source, *patterns],
            cwd=root, env=environment, capture_output=True, text=True, check=False, timeout=10,
        )
        if result.returncode != 0:
            return None
        found = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) != 2 or not SHA.fullmatch(fields[0]) or fields[1] not in patterns:
                return None
            commit, name = fields
            if name in found and found[name] != commit:
                return None
            found[name] = commit
        matches = [name for name in names if name in found]
        if len(matches) != 1:
            return None
        name = matches[0]
        observed = found.get(name + "^{}", found[name])
        if observed.casefold() == recorded.casefold():
            return None
        return (f"Template update available: {recorded[:7]} -> {observed[:7]}; "
                "preview with scripts/team.sh upgrade-template --dry-run (needs a clean working tree)")
    except (OSError, ValueError, UpgradeError, subprocess.SubprocessError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    arguments = parser.parse_args(argv)
    notice = update_notice(arguments.repo_root.resolve())
    if notice:
        print(notice)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
