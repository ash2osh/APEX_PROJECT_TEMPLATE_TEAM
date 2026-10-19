#!/usr/bin/env python3
"""Write the readable diff of a migration folder to ai_generate/<date>/<folder>.diff.

A package migration holds the whole package source, so Git shows thousands of added lines
for a few changed ones. This compares every SQL step of a migration folder with the
mirrored source it replaces (the files `scripts/team.sh backup-db` writes) and writes one
unified diff. Read-only: no database connection, no change to the migration folder.

A step is matched to its mirror file by the object named in its first lines
(CREATE OR REPLACE ... PACKAGE [BODY] "SCHEMA"."NAME"), which gives
database/<SCHEMA>/packages/<NAME>_SPEC.sql or <NAME>_BODY.sql (`$` in a name is `-S-`, as
backup-db writes it). A step without a mirror counterpart is written in full as new
content. The default base is the mirror in the working tree, so run this before backup-db
refreshes the mirror; `--base <git-ref>` reads the mirror from that commit instead.
"""
from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER = re.compile(
    r'CREATE\s+OR\s+REPLACE\s+(?:NON)?EDITIONABLE\s+PACKAGE\s+(BODY\s+)?"?([\w$#]+)"?\."?([\w$#]+)"?'
    r'|CREATE\s+OR\s+REPLACE\s+PACKAGE\s+(BODY\s+)?"?([\w$#]+)"?\."?([\w$#]+)"?',
    re.IGNORECASE,
)


def mirror_path(sql_text: str) -> Path | None:
    """The mirror file a package step replaces, or None when the step names no schema-qualified package."""
    for line in sql_text.splitlines()[:8]:
        match = HEADER.search(line)
        if match:
            body, schema, name = (match.group(1), match.group(2), match.group(3)) if match.group(2) else (
                match.group(4), match.group(5), match.group(6))
            kind = "BODY" if body else "SPEC"
            return Path("database") / schema.upper() / "packages" / f"{name.upper().replace('$', '-S-')}_{kind}.sql"
    return None


def read_base(root: Path, rel: Path, ref: str | None) -> str | None:
    if ref:
        result = subprocess.run(
            ["git", "show", f"{ref}:{rel.as_posix()}"], cwd=root, capture_output=True, text=True, encoding="utf-8"
        )
        return result.stdout if result.returncode == 0 else None
    path = root / rel
    return path.read_text(encoding="utf-8") if path.is_file() else None


def build_diff(root: Path, folder: Path, ref: str | None) -> tuple[list[str], list[str]]:
    summary: list[str] = []
    parts: list[str] = []
    steps = sorted(folder.glob("[0-9][0-9][0-9]-*.sql"))
    if not steps:
        raise SystemExit(f"no NNN-*.sql step found in {folder}")
    for step in steps:
        new = step.read_text(encoding="utf-8")
        rel = mirror_path(new)
        old = read_base(root, rel, ref) if rel else None
        label = rel.as_posix() if rel is not None and old is not None else None
        diff = list(difflib.unified_diff(
            (old or "").splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"a/{label}" if label else "/dev/null", tofile=f"b/{folder.name}/{step.name}", n=3))
        added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))
        origin = f"vs {label}" if label else "no mirror counterpart, shown in full"
        summary.append(f"# {step.name}: +{added} -{removed} ({origin})")
        parts.append("".join(diff) if diff else f"# {step.name}: no difference\n")
    return summary, parts


def main(argv: Sequence[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("migration", help="migration folder, e.g. migrations/<SCHEMA>/2026-01-31_add-index-r001")
    parser.add_argument("--date", default=date.today().isoformat(), help="dated folder under ai_generate (default: today)")
    parser.add_argument("--base", default=None, help="git ref to read the mirror from (default: the working tree)")
    args = parser.parse_args(argv)
    folder = (root / args.migration).resolve()
    if not folder.is_dir():
        print(f"not a migration folder: {args.migration}", file=sys.stderr)
        return 2
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        print("--date must be YYYY-MM-DD", file=sys.stderr)
        return 2
    summary, parts = build_diff(root, folder, args.base)
    out_dir = root / "ai_generate" / args.date
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{folder.name}.diff"
    out.write_text("\n".join(summary) + "\n\n" + "\n".join(parts), encoding="utf-8", newline="\n")
    print(f"wrote {out.relative_to(root).as_posix()}")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
