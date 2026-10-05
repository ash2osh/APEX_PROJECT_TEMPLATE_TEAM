#!/usr/bin/env python3
"""Fail when eligible APEXlang cannot parse or is absent from a built graph.

Uses the active Graphify detector so .graphifyignore and Git exclusions match
the CLI. Scanning writes only to a temporary cache; repository files and the
installed package are not changed. No semantic backend or database is used.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# The documented command must not leave local import caches behind.
sys.dont_write_bytecode = True

from graphify_apexlang_extractor import parse_apexlang
from setup_graphify_apx import graphify_console_interpreter


ROOT = Path(__file__).resolve().parents[1]
SCAN = """
import json, sys, tempfile
from contextlib import redirect_stdout
from pathlib import Path
from graphify.detect import detect
from graphify.watch import _read_build_excludes, _read_build_gitignore
root = Path(sys.argv[1])
build = root / "graphify-out"
with tempfile.TemporaryDirectory(prefix="graphify-coverage.") as cache:
    with redirect_stdout(sys.stderr):
        result = detect(root, cache_root=Path(cache),
                        extra_excludes=_read_build_excludes(build) or None,
                        gitignore=_read_build_gitignore(build))
    print(json.dumps({"files": result["files"]["code"],
                      "walk_errors": result["walk_errors"]}))
"""


def scan_files(root: Path) -> list[Path]:
    """Return eligible .apx paths; unavailable/incomplete scans are failures."""
    interpreter = graphify_console_interpreter()
    if not interpreter:
        raise RuntimeError("Graphify Python launcher is unavailable")
    if not root.is_dir():
        raise ValueError(f"repository root is not a directory: {root}")
    result = subprocess.run(
        [interpreter, "-B", "-c", SCAN, str(root)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    if result.returncode:
        raise RuntimeError(f"Graphify corpus scan failed: {result.stderr.strip()}")
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list) or not isinstance(payload.get("walk_errors"), list):
        raise ValueError("invalid Graphify corpus scan result")
    if payload["walk_errors"]:
        raise RuntimeError(f"incomplete Graphify corpus scan: {payload['walk_errors']}")
    if not all(isinstance(path, str) for path in payload["files"]):
        raise ValueError("invalid Graphify corpus paths")
    return sorted({Path(path) for path in payload["files"] if Path(path).suffix.lower() == ".apx"})


def check_files(root: Path, files: list[Path], graph: Path | None = None) -> list[str]:
    """Check syntax and optional file coverage, not runtime or content freshness."""
    errors = []
    selected = set()
    for path in files:
        try:
            relative = path.resolve().relative_to(root).as_posix()
        except ValueError:
            errors.append(f"source outside repository: {path}")
            continue
        selected.add(relative)
        try:
            parse_apexlang(path.read_text(encoding="utf-8"), path)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"{relative}: {exc}")
    if graph is not None:
        payload = json.loads(graph.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
            raise ValueError("graph has no valid nodes list")
        represented = set()
        for node in payload["nodes"]:
            if not isinstance(node, dict) or not isinstance(node.get("source_file", ""), str):
                raise ValueError("graph has invalid node source metadata")
            source = node.get("source_file", "")
            if source:
                source_path = Path(source)
                if source_path.is_absolute():
                    try:
                        source_path = source_path.relative_to(root)
                    except ValueError:
                        continue
                represented.add(source_path.as_posix())
        errors.extend(f"{path}: missing from graph" for path in sorted(selected - represented))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--graph", type=Path, help="also require every eligible .apx source in this graph")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        files = scan_files(root)
        errors = check_files(root, files, args.graph)
    except (OSError, UnicodeError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"FAIL: {exc}")
        return 1
    for error in errors:
        print(f"FAIL: {error}")
    if errors:
        print(f"FAIL: {len(files)} APEXlang files checked; {len(errors)} errors")
        return 1
    suffix = " and graph file coverage" if args.graph is not None else ""
    print(f"OK: {len(files)} APEXlang files passed parsing{suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
