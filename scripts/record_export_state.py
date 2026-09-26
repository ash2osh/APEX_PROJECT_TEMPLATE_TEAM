#!/usr/bin/env python3
"""Record the database app revision observed around a Builder export."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


DB_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
NOT_FOUND = "NOT_FOUND"
# APEX skips its audit columns while importing, so an app installed from
# APEXlang exists with a NULL last_updated_on until someone edits it in Builder.
NO_TIMESTAMP = "NO_TIMESTAMP"


@dataclass(frozen=True)
class AppState:
    """One observation of a live application's revision.

    The Builder timestamp shows Builder saves; the version text carries the
    DEV publish tag, which is the only trace an import leaves.
    """

    present: bool
    last_updated_on: str | None
    version: str | None = None

    def describe(self) -> str:
        if not self.present:
            return NOT_FOUND
        return f"{self.last_updated_on or NO_TIMESTAMP}, version {self.version!r}"


def parse_state(line: str) -> tuple[AppState, datetime] | None:
    """Parse `revision|database time|version`; the version may contain '|'."""
    parts = line.strip().split("|", 2)
    if len(parts) != 3 or not DB_TIMESTAMP.fullmatch(parts[1]):
        return None
    observed_at = datetime.fromisoformat(parts[1])
    if parts[0] == NOT_FOUND:
        return AppState(False, None), observed_at
    if parts[0] == NO_TIMESTAMP:
        return AppState(True, None, parts[2]), observed_at
    if not DB_TIMESTAMP.fullmatch(parts[0]):
        return None
    datetime.fromisoformat(parts[0])
    return AppState(True, parts[0], parts[2]), observed_at


def read_state(path: Path) -> tuple[AppState, datetime]:
    parsed = parse_state(path.read_text(encoding="utf-8"))
    if parsed is None:
        raise ValueError(f"invalid database application state in {path}")
    return parsed


def marker_payload(app_id: int, state: AppState) -> dict[str, object]:
    return {
        "applicationId": app_id,
        "applicationPresent": state.present,
        "builderLastUpdatedOn": state.last_updated_on,
        "version": state.version,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app_id", type=int)
    parser.add_argument("before_file", type=Path)
    parser.add_argument("after_file", type=Path)
    parser.add_argument("marker_file", type=Path)
    args = parser.parse_args(argv)
    if args.app_id < 1:
        parser.error("app_id must be a positive integer")

    try:
        before, before_observed_at = read_state(args.before_file)
        after, after_observed_at = read_state(args.after_file)
    except (OSError, ValueError) as exc:
        print(f"export error: could not verify the Builder state around export: {exc}", file=sys.stderr)
        return 1

    if before != after:
        print(
            f"export error: APEX App {args.app_id} changed (Builder save or import) while export was running "
            f"(before: {before.describe()}, after: {after.describe()}). "
            "Wait for Builder edits to stop, then export again.",
            file=sys.stderr,
        )
        return 1

    if (before.last_updated_on and datetime.fromisoformat(before.last_updated_on) == before_observed_at) or (
        after.last_updated_on and datetime.fromisoformat(after.last_updated_on) == after_observed_at
    ):
        print(
            f"export error: APEX App {args.app_id} has a last_updated_on value in the same database second "
            "as the export observation. Oracle DATE has one-second precision; wait one second and export again.",
            file=sys.stderr,
        )
        return 1

    marker = marker_payload(args.app_id, before)
    args.marker_file.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
