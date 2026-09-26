#!/usr/bin/env python3
"""Refuse an APEXlang import when the live app changed after the last export."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from record_export_state import NO_TIMESTAMP, NOT_FOUND, AppState, parse_state


DATABASE_STATE_LINE = re.compile(
    rf"^[ \t]*(?:{NOT_FOUND}|{NO_TIMESTAMP}|\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}:\d{{2}}:\d{{2}})"
    r"\|\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\|.*$",
    re.MULTILINE,
)


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    # Oracle DATE and the APEX application update field are in database-local
    # time. The exporter stores that exact value; workstation zone conversion
    # would make the comparison unsafe.
    if parsed.tzinfo is not None:
        return None
    return parsed.replace(microsecond=0)


def get_export_baseline(app_dir: Path, app_id: int) -> AppState | None:
    """Read the database revision captured before the APEX source export."""
    marker_path = app_dir / "apex-team-export.json"
    if not marker_path.is_file():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(marker, dict):
        return None
    marker_id = marker.get("applicationId")
    if isinstance(marker_id, bool) or not isinstance(marker_id, int) or marker_id != app_id:
        return None
    if "builderLastUpdatedOn" not in marker:
        return None
    baseline = marker["builderLastUpdatedOn"]
    if baseline is not None and parse_timestamp(baseline) is None:
        return None
    # A marker without the version cannot show a teammate's import, so an
    # older marker is unavailable until the next export records one.
    present = marker.get("applicationPresent")
    version = marker.get("version", False)
    if not isinstance(present, bool):
        return None
    if present and not isinstance(version, str):
        return None
    if not present and (baseline is not None or version is not None):
        return None
    return AppState(present, baseline, version)


def _query_live_timestamp(
    app_id: int, connection: str, expected_user: str | None, app_dir: Path
) -> tuple[AppState | None, datetime | None, str | None]:
    sql_script = Path(__file__).with_name("check_builder_drift.sql")
    if not sql_script.is_file():
        return None, None, f"SQLcl query script is missing: {sql_script}"
    expected_arg = expected_user or "-"
    with tempfile.TemporaryDirectory(prefix="apex-builder-drift-") as temp_dir:
        stdin_path = Path(temp_dir) / "sqlcl-stdin"
        stdin_path.write_text("", encoding="utf-8")
        try:
            with stdin_path.open("r", encoding="utf-8") as stdin:
                result = subprocess.run(
                    [
                        "sql",
                        "-S",
                        "-noupdates",
                        "-name",
                        connection,
                        f"@{sql_script}",
                        str(app_id),
                        expected_arg,
                    ],
                    cwd=app_dir,
                    stdin=stdin,
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, None, f"SQLcl query failed: {exc}"

    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or re.search(r"\b(?:ORA|SP2|SQL)\s*-\d+", output, re.I):
        detail = output.strip() or f"SQLcl exited with status {result.returncode}"
        return None, None, detail
    lines = DATABASE_STATE_LINE.findall(output)
    parsed = parse_state(lines[-1]) if lines else None
    if parsed is None:
        return None, None, "SQLcl returned no valid application state and database time"
    live, database_time = parsed
    return live, database_time, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app_id", type=int, help="numeric APEX application ID")
    parser.add_argument("connection", help="SQLcl saved connection alias")
    parser.add_argument("app_dir", type=Path, help="local APEXlang application directory")
    parser.add_argument("--expected-user", help="expected Oracle session user")
    args = parser.parse_args(argv)

    if args.app_id < 1:
        parser.error("app_id must be a positive integer")
    if not args.connection.strip():
        parser.error("connection alias must not be empty")
    app_dir = args.app_dir.resolve()
    if not app_dir.is_dir():
        print(f"[DRIFT UNKNOWN] Application source directory does not exist: {app_dir}", file=sys.stderr)
        return 1

    baseline = get_export_baseline(app_dir, args.app_id)
    export_command = f"scripts/team.sh export {args.app_id}"
    if baseline is None:
        print(
            f"[DRIFT UNKNOWN] Database export baseline is unavailable for APEX App {args.app_id}.\n"
            f"Run {export_command} before publishing.",
            file=sys.stderr,
        )
        return 1

    live, database_time, error = _query_live_timestamp(
        args.app_id, args.connection, args.expected_user, app_dir
    )
    if error or live is None:
        print(
            f"[DRIFT UNKNOWN] Could not read live APEX App {args.app_id}: {error}",
            file=sys.stderr,
        )
        return 1

    baseline_at = parse_timestamp(baseline.last_updated_on)
    live_at = parse_timestamp(live.last_updated_on)
    if not baseline.present and not live.present:
        print(f"[DRIFT OK] APEX App {args.app_id} remains absent since the local export.")
        return 0
    if not baseline.present:
        reason = "was created after the local export."
    elif not live.present:
        reason = "no longer exists in the target after the local export."
    elif baseline_at is None and live_at is None:
        # APEX leaves last_updated_on NULL on import and sets it on any Builder
        # save. The publish tag in the version identifies which import is live.
        if live.version != baseline.version:
            reason = (
                f"was re-imported since the local export (live version: {live.version!r}, "
                f"local baseline: {baseline.version!r})."
            )
        else:
            print(
                f"[DRIFT OK] APEX App {args.app_id} has no Builder edits since its last import "
                f"(version {live.version!r})."
            )
            return 0
    elif live_at is None:
        reason = "was re-imported after the local export (APEX clears last_updated_on on import)."
    elif baseline_at is None:
        reason = f"was modified in Builder on {live_at.isoformat(timespec='seconds')}."
    elif live_at == baseline_at == database_time:
        print(
            f"[DRIFT UNKNOWN] APEX App {args.app_id} last_updated_on matches the current database second. "
            "Oracle DATE has one-second precision, so the revision is ambiguous; wait one second and retry.",
            file=sys.stderr,
        )
        return 1
    elif live_at > baseline_at:
        reason = f"was modified in Builder on {live_at.isoformat(timespec='seconds')}."
    elif live.version != baseline.version:
        reason = (
            f"changed version since the local export (live: {live.version!r}, "
            f"local baseline: {baseline.version!r})."
        )
    else:
        print("[DRIFT OK] No uncaptured Builder edits detected.")
        return 0

    print(f"[DRIFT DETECTED] Live APEX App {args.app_id} {reason}")
    print(f"To prevent accidental overwrites, run: {export_command} to review and merge changes.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
