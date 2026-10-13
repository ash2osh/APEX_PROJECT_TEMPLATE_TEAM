#!/usr/bin/env python3
"""Record the database app revision observed around a Builder export."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping

if __package__:
    from .source_provenance import make_source_provenance
else:
    from source_provenance import make_source_provenance


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


def marker_payload(
    app_id: int,
    state: AppState,
    source_dir: Path | None = None,
    page_locks: list[dict] | None = None,
    source_provenance: Mapping[str, object] | None = None,
) -> dict[str, object]:
    payload = {
        "applicationId": app_id,
        "applicationPresent": state.present,
        "builderLastUpdatedOn": state.last_updated_on,
        "version": state.version,
    }
    if source_dir is not None:
        if __package__:
            from .source_evidence import content_evidence
        else:
            from source_evidence import content_evidence
        payload.update(content_evidence(source_dir))
        if source_provenance is not None:
            payload["sourceProvenance"] = dict(source_provenance)
        if page_locks is not None:
            payload['pageLocks'] = page_locks
    return payload


def read_apex_release(path: Path) -> tuple[str, str]:
    """Read one guarded SQLcl release/time observation from a private spool file."""
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"could not read guarded APEX release observation {path}") from error
    line = content.strip()
    fields = line.split("|")
    if (
        len(fields) != 2
        or re.fullmatch(r"[0-9]{1,2}(?:\.[0-9]{1,3}){1,4}", fields[0], re.ASCII) is None
        or not fields[1]
    ):
        raise ValueError("guarded APEX release observation has an invalid format")
    try:
        captured = datetime.fromisoformat(fields[1].replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("guarded APEX release observation has an invalid timestamp") from error
    if captured.tzinfo is None or captured.utcoffset() != timezone.utc.utcoffset(captured):
        raise ValueError("guarded APEX release observation timestamp is not UTC")
    return fields[0], fields[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app_id", type=int)
    parser.add_argument("before_file", type=Path)
    parser.add_argument("after_file", type=Path)
    parser.add_argument("marker_file", type=Path)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument('--page-locks-before', type=Path)
    parser.add_argument('--page-locks-after', type=Path)
    parser.add_argument('--apex-release-before', type=Path)
    parser.add_argument('--apex-release-after', type=Path)
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

    try:
        page_locks = None
        if args.page_locks_before is not None or args.page_locks_after is not None:
            if args.page_locks_before is None or args.page_locks_after is None:
                raise ValueError('both page lock observations are required')
            if __package__:
                from .source_evidence import read_page_lock_evidence
            else:
                from source_evidence import read_page_lock_evidence
            page_locks = read_page_lock_evidence(args.page_locks_before, args.page_locks_after, args.app_id)
        provenance = None
        if args.apex_release_before is not None or args.apex_release_after is not None:
            if args.apex_release_before is None or args.apex_release_after is None or args.source_dir is None:
                raise ValueError("both guarded APEX release observations and --source-dir are required")
            release_before, time_before = read_apex_release(args.apex_release_before)
            release_after, time_after = read_apex_release(args.apex_release_after)
            if release_before != release_after:
                raise ValueError("APEX release changed during export; rerun after confirming the target release")
            observed_before = datetime.fromisoformat(time_before.replace("Z", "+00:00"))
            observed_after = datetime.fromisoformat(time_after.replace("Z", "+00:00"))
            if observed_before > observed_after:
                raise ValueError("database UTC time moved backwards during the guarded APEX export")
            provenance = make_source_provenance(args.source_dir, release_after, time_after)
        marker = marker_payload(args.app_id, before, args.source_dir, page_locks, provenance)
    except (OSError, ValueError) as exc:
        print(f"export error: could not verify canonical source evidence: {exc}", file=sys.stderr)
        return 1
    # newline="\n": the default would write CRLF on Windows.
    args.marker_file.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
