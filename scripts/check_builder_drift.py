#!/usr/bin/env python3
"""Refuse an APEXlang import when Builder has newer uncaptured edits."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


TIMESTAMP_PATTERN = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
EXPORT_KEYS = {
    "exportedat",
    "exporttimestamp",
    "lastexportat",
    "lastexporttimestamp",
    "lastexportedat",
    "lastupdatedon",
}


def parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        cleaned = value.strip().strip("\"'")
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(cleaned)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def _timestamp_from_json(value: Any) -> datetime | None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key in EXPORT_KEYS:
                timestamp = parse_timestamp(item)
                if timestamp is not None:
                    return timestamp
        for item in value.values():
            timestamp = _timestamp_from_json(item)
            if timestamp is not None:
                return timestamp
    elif isinstance(value, list):
        for item in value:
            timestamp = _timestamp_from_json(item)
            if timestamp is not None:
                return timestamp
    return None


def get_local_export_timestamp(app_dir: Path, app_id: int) -> datetime | None:
    """Read a team export marker, then compatible timestamp fields in APEXlang."""
    marker_path = app_dir / "apex-team-export.json"
    if marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            marker_id = marker.get("applicationId")
            if isinstance(marker_id, int) and not isinstance(marker_id, bool) and marker_id != app_id:
                return None
            timestamp = parse_timestamp(marker.get("exportedAt"))
            if timestamp is not None:
                return timestamp
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

    metadata_path = app_dir / ".apex" / "apexlang.json"
    if metadata_path.is_file():
        try:
            timestamp = _timestamp_from_json(
                json.loads(metadata_path.read_text(encoding="utf-8"))
            )
            if timestamp is not None:
                return timestamp
        except (OSError, json.JSONDecodeError):
            pass

    application_path = app_dir / "application.apx"
    if application_path.is_file():
        try:
            source = application_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None
        for match in re.finditer(
            r"(?im)\b(exportedAt|exportTimestamp|lastExportAt|lastExportTimestamp|"
            r"lastExportedAt|lastUpdatedOn)\s*:\s*['\"]?([^\s,}\]]+)",
            source,
        ):
            timestamp = parse_timestamp(match.group(2))
            if timestamp is not None:
                return timestamp
    return None


def _query_live_timestamp(
    app_id: int, connection: str, expected_user: str | None, app_dir: Path
) -> tuple[datetime | None, bool, str | None]:
    sql_script = Path(__file__).with_name("check_builder_drift.sql")
    if not sql_script.is_file():
        return None, False, f"SQLcl query script is missing: {sql_script}"
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
            return None, False, f"SQLcl query failed: {exc}"

    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or re.search(r"\b(?:ORA|SP2|SQL)\s*-\d+", output, re.I):
        detail = output.strip() or f"SQLcl exited with status {result.returncode}"
        return None, False, detail
    if re.search(r"\bNOT_FOUND\b", output, re.I):
        return None, True, None
    timestamps = TIMESTAMP_PATTERN.findall(output)
    if not timestamps:
        return None, False, "SQLcl returned no application timestamp"
    live_timestamp = parse_timestamp(timestamps[-1])
    if live_timestamp is None:
        return None, False, "SQLcl returned an invalid application timestamp"
    return live_timestamp, False, None


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

    exported_at = get_local_export_timestamp(app_dir, args.app_id)
    export_command = f"scripts/team.sh export {args.app_id}"
    if exported_at is None:
        print(
            f"[DRIFT UNKNOWN] Local export timestamp is unavailable for APEX App {args.app_id}.\n"
            f"Run {export_command} before publishing.",
            file=sys.stderr,
        )
        return 1

    live_at, not_found, error = _query_live_timestamp(
        args.app_id, args.connection, args.expected_user, app_dir
    )
    if error:
        print(
            f"[DRIFT UNKNOWN] Could not read live APEX App {args.app_id}: {error}",
            file=sys.stderr,
        )
        return 1
    if not_found:
        print(f"[DRIFT OK] APEX App {args.app_id} is not installed in this target yet.")
        return 0

    assert live_at is not None
    if live_at > exported_at:
        formatted = live_at.isoformat(timespec="seconds")
        print(
            f"[DRIFT DETECTED] Live APEX App {args.app_id} was modified in Builder on {formatted}."
        )
        print(
            f"To prevent accidental overwrites, run: {export_command} to review and merge changes."
        )
        return 1

    print("[DRIFT OK] No uncaptured Builder edits detected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
