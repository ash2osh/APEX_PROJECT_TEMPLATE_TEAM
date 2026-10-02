#!/usr/bin/env python3
"""Refuse an APEXlang import when the live app changed after the last export."""

from __future__ import annotations

import argparse
import json
import os
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


def live_state_token(state: AppState) -> str:
    """Encode an approved live state for publish_app.sql to re-check before import.

    The version is free text, so it travels as hex of its UTF-8 bytes; the
    token uses only letters, digits, '.', ':' and '-', which no Windows
    command wrapper treats specially. SQLcl
    pads the drift query line, so trailing whitespace is not observable here;
    both sides drop exactly what str.rstrip() drops (publish_app.sql lists the
    same characters).
    """
    if not state.present:
        return "ABSENT"
    version = (state.version or "").rstrip()
    return f"P.{state.last_updated_on or 'NONE'}.{version.encode('utf-8').hex().upper()}"


def _write_state(path: Path | None, state: AppState) -> None:
    if path is not None:
        path.write_text(live_state_token(state) + "\n", encoding="utf-8", newline="\n")


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
        environment = os.environ.copy()
        # SQLcl must not inherit a login.sql from application source or from a
        # user-configured search path. The SQL script is absolute and resolves
        # its own @@ include relative to that script.
        environment["SQLPATH"] = temp_dir
        environment["ORACLE_PATH"] = temp_dir
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
                    cwd=temp_dir,
                    env=environment,
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
    if not re.search(r"(?m)^\s*APEX_DRIFT_QUERY_VERIFIED\s*$", output):
        return None, None, "SQLcl did not verify the drift query; the result is unknown"
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
    parser.add_argument(
        "--state-out",
        type=Path,
        help="write the approved live state here so the import session can re-check it",
    )
    parser.add_argument(
        "--wrapper",
        choices=("team.sh", "team.ps1"),
        default="team.sh",
        help="the wrapper the developer ran, so refusals name its export command",
    )
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
    export_command = f"scripts/{args.wrapper} export {args.app_id}"
    if baseline is None and not (app_dir / "apex-team-export.json").exists():
        # A brand-new app has never been exported, and an export of an app that
        # does not exist fails, so no baseline can ever be recorded. When the
        # target has no such app there is nothing to overwrite: publishing only
        # creates it. Any other answer, including a failed query, keeps the
        # refusal below.
        live, _, error = _query_live_timestamp(args.app_id, args.connection, args.expected_user, app_dir)
        if not error and live is not None and not live.present:
            print(
                f"[DRIFT OK] APEX App {args.app_id} does not exist in the target yet; "
                "publishing creates it, so there is no earlier export to compare."
            )
            _write_state(args.state_out, live)
            return 0
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
        _write_state(args.state_out, live)
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
            _write_state(args.state_out, live)
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
        _write_state(args.state_out, live)
        return 0

    print(f"[DRIFT DETECTED] Live APEX App {args.app_id} {reason}")
    if not live.present:
        # Nothing is live to export. An import would recreate the app, which
        # is the team's call (docs/publish-rules.md), not a merge.
        print("Ask the team before recreating it; there is nothing to export or merge.")
    else:
        print(f"To prevent accidental overwrites, run: {export_command} to review and merge changes.")
    return 1


def _run() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        # Nothing is stamped or imported yet, so there is nothing else to report.
        print("[DRIFT UNKNOWN] interrupted before the live app was checked; nothing was imported", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(_run())
