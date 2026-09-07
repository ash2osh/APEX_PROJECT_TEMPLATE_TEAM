"""CLI adapter for the read-only conflict assistant."""

from __future__ import annotations

import argparse

from .conflict_assistant import explain_conflict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="explain-conflict")
    parser.add_argument("recovery_id")
    parser.add_argument("--root", default=".sync-state")
    args = parser.parse_args(list(argv or []))
    print(explain_conflict(args.recovery_id, root=args.root).text)
    return 0
