#!/usr/bin/env python3
"""Report the files the 26.2 and 26.1 templates share but that differ.

This repository (APEX 26.2) and APEX_PROJECT_TEMPLATE_26_1 are separate repositories with
many files in common. A fix made in one is easy to miss in the other. This compares the
committed files both repositories track and checks every one that differs against
docs/superpowers/template-parity.json, where each difference is marked `intended` (the
release lines differ on purpose), `feature` (a feature only one line has; porting it is a
separate decision) or `missed` (a fix still to port), with a reason and the Git blob ids
it was reviewed at.

Statuses: `unreviewed` (differs, no entry), `changed` (an entry exists but either file
changed since its review), `missed`, `feature`, `intended`, `stale` (an entry for files that are now
identical or no longer shared). Exit 1 when anything is unreviewed or changed.

Usage:
  python3 tools/template_parity.py ../APEX_PROJECT_TEMPLATE_26_1
  python3 tools/template_parity.py ../APEX_PROJECT_TEMPLATE_26_1 --mark PATH --status intended --reason TEXT
Read-only except --mark, which writes only the parity list. Both checkouts are read at
their committed HEAD, never their working trees.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PARITY_LIST = ROOT / "docs" / "superpowers" / "template-parity.json"
STATUSES = ("intended", "feature", "missed")


def tracked_blobs(repo: Path) -> dict[str, str]:
    """Path -> blob id of every file committed at HEAD."""
    output = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "HEAD"], capture_output=True, check=True
    ).stdout.decode("utf-8")
    blobs = {}
    for record in filter(None, output.split("\0")):
        meta, path = record.split("\t", 1)
        _mode, kind, blob = meta.split()
        if kind == "blob":
            blobs[path] = blob
    return blobs


def load_list(path: Path) -> dict:
    if not path.is_file():
        return {"schemaVersion": 1, "files": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schemaVersion") != 1 or not isinstance(data.get("files"), dict):
        raise SystemExit(f"{path}: expected schemaVersion 1 with a files object")
    return data


def classify(this: dict[str, str], other: dict[str, str], entries: dict) -> dict[str, list[tuple[str, str]]]:
    """Status -> sorted (path, reason) pairs."""
    report: dict[str, list[tuple[str, str]]] = {name: [] for name in ("unreviewed", "changed", "missed", "feature", "intended", "stale")}
    differing = {path for path in this.keys() & other.keys() if this[path] != other[path]}
    for path in sorted(differing):
        entry = entries.get(path)
        if entry is None:
            report["unreviewed"].append((path, ""))
        elif entry.get("blobs") != [this[path], other[path]]:
            report["changed"].append((path, entry.get("reason", "")))
        else:
            report[entry["status"]].append((path, entry.get("reason", "")))
    for path in sorted(set(entries) - differing):
        report["stale"].append((path, entries[path].get("reason", "")))
    return report


def mark(path: str, status: str, reason: str, this: dict[str, str], other: dict[str, str], list_path: Path) -> int:
    if path not in this or path not in other or this[path] == other[path]:
        print(f"{path}: not a shared file that differs", file=sys.stderr)
        return 2
    data = load_list(list_path)
    data["files"][path] = {"status": status, "reason": reason, "blobs": [this[path], other[path]]}
    data["files"] = dict(sorted(data["files"].items()))
    list_path.parent.mkdir(parents=True, exist_ok=True)
    list_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"marked {path}: {status}")
    return 0


def main(argv: Sequence[str] | None = None, *, root: Path = ROOT, list_path: Path = PARITY_LIST) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("other", type=Path, help="checkout of the other template repository")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--mark", metavar="PATH", help="record a review of one differing file")
    parser.add_argument("--status", choices=STATUSES)
    parser.add_argument("--reason", default="")
    args = parser.parse_args(argv)
    this, other = tracked_blobs(root), tracked_blobs(args.other)
    if args.mark:
        if not args.status or not args.reason.strip():
            parser.error("--mark needs --status and a --reason")
        return mark(args.mark, args.status, args.reason.strip(), this, other, list_path)
    report = classify(this, other, load_list(list_path)["files"])
    if args.format == "json":
        print(json.dumps({name: [{"path": p, "reason": r} for p, r in rows] for name, rows in report.items()}, indent=2))
    else:
        shared = len(this.keys() & other.keys())
        print(f"shared files: {shared}; differing: {sum(len(rows) for name, rows in report.items() if name != 'stale')}")
        for name, rows in report.items():
            print(f"\n{name}: {len(rows)}")
            for path, reason in rows:
                print(f"  {path}" + (f"  -- {reason}" if reason else ""))
    return 1 if report["unreviewed"] or report["changed"] else 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
