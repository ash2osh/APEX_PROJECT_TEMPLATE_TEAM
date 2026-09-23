"""Read-only automatic and reviewed-manual APEX page locks inspection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import tempfile
from typing import Any, Literal
from collections.abc import Callable

from .config import Target
from .sqlcl import run_sqlcl


class PageLockError(RuntimeError):
    """Raised when page lock data cannot be loaded or is invalid."""


@dataclass(frozen=True)
class PageLock:
    page_id: int
    page_name: str | None
    locked_by: str
    locked_on: str | None
    comment: str | None


@dataclass(frozen=True)
class LockReport:
    alias: str
    app_id: int
    status: Literal["KNOWN", "UNKNOWN"]
    pages: tuple[PageLock, ...]
    source: str


_UNSAFE_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MANUAL_REPORT_KEYS = {
    "version",
    "target_state_key",
    "alias",
    "app_id",
    "captured_at_utc",
    "reviewed_by",
    "pages",
}
_MANUAL_PAGE_KEYS = {"page_id", "page_name", "locked_by", "locked_on", "comment"}


def read_page_locks(
    target: Target,
    runner: Callable[..., Any] = run_sqlcl,
    *,
    work_dir: str | Path | None = None,
) -> LockReport:
    """Read locked pages from APEX_APPLICATION_LOCKED_PAGES for the target app."""
    alias = target.alias or ""
    app_id = target.app_id or 0
    if not target.app_id:
        return LockReport(alias, app_id, "UNKNOWN", (), "APEX_APPLICATION_LOCKED_PAGES")

    driver_text = (
        "SET DEFINE OFF\n"
        "SET HEADING OFF\n"
        "SET FEEDBACK OFF\n"
        "SET PAGESIZE 0\n"
        "SELECT 'TEAM_PAGE_LOCK|' || page_id || '|' || REPLACE(NVL(page_name, '-'), '|', '/') || '|' ||\n"
        "       REPLACE(locked_by, '|', '/') || '|' ||\n"
        "       REPLACE(NVL(TO_CHAR(locked_on, 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), '-'), '|', '/') || '|' ||\n"
        "       REPLACE(NVL(lock_comment, '-'), '|', '/')\n"
        "  FROM apex_application_locked_pages\n"
        f" WHERE application_id = {int(target.app_id)}\n"
        " ORDER BY page_id;\n"
    )

    with tempfile.TemporaryDirectory(prefix="team-page-locks-") as temp_dir:
        root = Path(work_dir) if work_dir is not None else Path(temp_dir)
        driver_path = root / "page_locks_probe.sql"
        driver_path.write_text(driver_text, encoding="utf-8", newline="\n")

        try:
            result = runner(target, "read", driver_path, root)
        except Exception:
            return LockReport(alias, app_id, "UNKNOWN", (), "APEX_APPLICATION_LOCKED_PAGES")

    stdout = getattr(result, "stdout", "") or ""
    pages: list[PageLock] = []
    seen_page_ids: set[int] = set()

    for raw_line in stdout.splitlines():
        line = raw_line.strip().rstrip("\r")
        if not line.startswith("TEAM_PAGE_LOCK|"):
            continue
        if _UNSAFE_CONTROL_CHARS.search(line):
            return LockReport(alias, app_id, "UNKNOWN", (), "APEX_APPLICATION_LOCKED_PAGES")
        parts = line.split("|")
        if len(parts) != 6:
            return LockReport(alias, app_id, "UNKNOWN", (), "APEX_APPLICATION_LOCKED_PAGES")
        try:
            page_id = int(parts[1].strip())
            if page_id <= 0:
                return LockReport(alias, app_id, "UNKNOWN", (), "APEX_APPLICATION_LOCKED_PAGES")
        except ValueError:
            return LockReport(alias, app_id, "UNKNOWN", (), "APEX_APPLICATION_LOCKED_PAGES")

        if page_id in seen_page_ids:
            return LockReport(alias, app_id, "UNKNOWN", (), "APEX_APPLICATION_LOCKED_PAGES")
        seen_page_ids.add(page_id)

        page_name = parts[2].strip() if parts[2].strip() != "-" else None
        locked_by = parts[3].strip()
        if not locked_by or locked_by == "-":
            return LockReport(alias, app_id, "UNKNOWN", (), "APEX_APPLICATION_LOCKED_PAGES")
        locked_on = parts[4].strip() if parts[4].strip() != "-" else None
        comment = parts[5].strip() if parts[5].strip() != "-" else None

        pages.append(PageLock(page_id, page_name, locked_by, locked_on, comment))

    return LockReport(alias, app_id, "KNOWN", tuple(pages), "APEX_APPLICATION_LOCKED_PAGES")


def load_manual_page_locks(path: str | Path, target: Target) -> LockReport:
    """Load an operator-reviewed JSON report of APEX Builder page locks."""
    p = Path(path)
    if not p.is_file() or p.is_symlink():
        raise PageLockError(f"manual page lock report is not a regular file: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PageLockError(f"invalid JSON in manual page lock report: {exc}") from exc

    if not isinstance(data, dict):
        raise PageLockError("manual page lock report must be a JSON object")

    unknown_keys = sorted(set(data) - _MANUAL_REPORT_KEYS)
    if unknown_keys:
        raise PageLockError(f"unsupported manual lock report fields: {', '.join(unknown_keys)}")

    if data.get("version") != 1:
        raise PageLockError("manual page lock report version must be 1")

    if data.get("target_state_key") != target.state_key:
        raise PageLockError("target_state_key in manual lock report does not match target")

    if data.get("alias") != target.alias:
        raise PageLockError(f"alias in manual lock report ({data.get('alias')}) does not match target {target.alias}")

    if data.get("app_id") != target.app_id:
        raise PageLockError(f"app_id in manual lock report ({data.get('app_id')}) does not match target {target.app_id}")

    reviewed_by = data.get("reviewed_by")
    if not isinstance(reviewed_by, str) or not reviewed_by.strip():
        raise PageLockError("reviewed_by is required in manual lock report")

    captured_at = data.get("captured_at_utc")
    if not isinstance(captured_at, str):
        raise PageLockError("captured_at_utc is required in manual lock report")

    try:
        # Parse ISO-8601 UTC timestamp
        captured_dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        age_seconds = (now_dt - captured_dt).total_seconds()
        if age_seconds < -60 or age_seconds > 300:
            raise PageLockError(
                f"manual lock report is stale ({int(age_seconds)}s old, maximum allowed is 300s / 5 minutes)"
            )
    except (ValueError, TypeError) as exc:
        raise PageLockError(f"invalid captured_at_utc timestamp: {exc}") from exc

    pages_raw = data.get("pages")
    if not isinstance(pages_raw, list):
        raise PageLockError("pages must be a list in manual lock report")

    pages: list[PageLock] = []
    seen_ids: set[int] = set()
    for entry in pages_raw:
        if not isinstance(entry, dict):
            raise PageLockError("each page entry must be an object")
        unknown_entry_keys = sorted(set(entry) - _MANUAL_PAGE_KEYS)
        if unknown_entry_keys:
            raise PageLockError(f"unsupported page entry fields: {', '.join(unknown_entry_keys)}")
        page_id = entry.get("page_id")
        if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id <= 0:
            raise PageLockError(f"invalid page_id: {page_id}")
        if page_id in seen_ids:
            raise PageLockError(f"duplicate page_id in manual report: {page_id}")
        seen_ids.add(page_id)
        locked_by = entry.get("locked_by")
        if not isinstance(locked_by, str) or not locked_by.strip():
            raise PageLockError(f"locked_by is required for page {page_id}")
        page_name = str(entry["page_name"]) if entry.get("page_name") is not None else None
        locked_on = str(entry["locked_on"]) if entry.get("locked_on") is not None else None
        comment = str(entry["comment"]) if entry.get("comment") is not None else None
        pages.append(PageLock(page_id, page_name, locked_by.strip(), locked_on, comment))

    return LockReport(target.alias or "", target.app_id or 0, "KNOWN", tuple(pages), "MANUAL BUILDER REVIEW")


def format_lock_report(report: LockReport) -> str:
    """Render a human-readable display of the lock report."""
    lines = [
        f"=== Page Locks: {report.alias} (App {report.app_id}) ===",
        f"Source: {report.source}",
    ]
    if report.status == "UNKNOWN":
        lines.append("Status: UNKNOWN (unable to verify locked pages; publish refused)")
        return "\n".join(lines) + "\n"

    if not report.pages:
        lines.append("No locked pages reported.")
        return "\n".join(lines) + "\n"

    lines.append(f"{'Page':<6} {'Name':<20} {'Owner':<16} {'Locked On':<22} {'Comment'}")
    lines.append("-" * 75)
    for p in report.pages:
        name = p.page_name or "-"
        locked_on = p.locked_on or "-"
        comment = p.comment or "-"
        lines.append(f"{p.page_id:<6} {name[:19]:<20} {p.locked_by[:15]:<16} {locked_on:<22} {comment}")
    return "\n".join(lines) + "\n"
