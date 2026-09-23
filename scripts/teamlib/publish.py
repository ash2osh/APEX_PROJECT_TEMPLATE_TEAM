"""Guarded multi-app publish preparation and execution.

Provides durable, non-secret preparation records and preflight checks before
performing any whole-application import into shared development environments.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from collections.abc import Callable, Mapping

from .apex import (
    ApexError,
    _git_head,
    _receipt_allows_import,
    capture_app,
)
from .apex_validate import ValidationReport, validate_apexlang_tree
from .config import Target
from .control_store import ControlStore, SqlControlStore
from .page_locks import LockReport, PageLock, format_lock_report
from .sqlcl import run_sqlcl
from .state import StateError, _target_descriptor, load_baseline, load_capture
from .trees import TreeError, assert_source_clean, read_git_tree, tree_digest


class PublishError(RuntimeError):
    """Raised when publish preparation or execution refuses."""


@dataclass(frozen=True)
class Preparation:
    preparation_id: str
    version: int
    aliases: tuple[str, ...]
    source_commit: str
    prepared_at_utc: str
    record_digest: str
    apps: Mapping[str, Any]
    path: Path


def prepare_publish(
    repo: Path | str,
    targets: tuple[Target, ...],
    source_commit: str,
    lock_reports: Mapping[str, LockReport],
    store: ControlStore | SqlControlStore,
    *,
    runner: Callable[..., Any] = run_sqlcl,
    validator: Callable[..., ValidationReport] = validate_apexlang_tree,
    replace_from: Mapping[str, str] | None = None,
    prepared_at_utc: str | None = None,
    preparation_id: str | None = None,
) -> Preparation:
    """Prepare an exact, app-scoped publish record with durable evidence."""
    if not targets:
        raise PublishError("At least one target is required to prepare publish")

    aliases = [t.alias for t in targets if t.alias]
    if len(aliases) != len(targets) or len(set(aliases)) != len(aliases):
        raise PublishError("Selected targets must have unique, non-duplicate, non-empty aliases")

    sorted_aliases = tuple(sorted(aliases))
    repo_path = Path(repo)
    if not repo_path.is_dir() or repo_path.is_symlink():
        raise PublishError(f"Repository is not a valid directory: {repo_path}")

    # Check that all targets are development targets
    for t in targets:
        if t.role != "developer" or t.environment != "development":
            raise PublishError(
                f"Target '{t.alias}' has role '{t.role}' and environment '{t.environment}'; "
                "publish is restricted to shared development targets"
            )

    # Check git source cleanliness for each selected app
    for t in targets:
        alias = t.alias or ""
        try:
            assert_source_clean(repo_path, alias)
        except (ApexError, TreeError) as exc:
            raise PublishError(f"Source for '{alias}' is not clean: {exc}") from exc

    # Resolve git commit
    try:
        resolved_commit = _git_head(repo_path, source_commit)
    except ApexError as exc:
        raise PublishError(f"Could not resolve source commit ref '{source_commit}': {exc}") from exc

    # Check lock reports
    for t in targets:
        alias = t.alias or ""
        report = lock_reports.get(alias)
        if report is None:
            raise PublishError(f"Missing lock report for application '{alias}'")
        if report.status != "KNOWN":
            raise PublishError(
                f"Lock report for application '{alias}' status is {report.status}; "
                "publish preparation refused"
            )

    state_root = repo_path / ".sync-state"
    replace_map = dict(replace_from or {})
    apps_data: dict[str, Any] = {}

    for t in sorted(targets, key=lambda x: x.alias or ""):
        alias = t.alias or ""
        report = lock_reports[alias]

        # 1. Read Git APEXlang tree
        try:
            selected_tree = read_git_tree(repo_path, resolved_commit, alias)
        except TreeError as exc:
            raise PublishError(f"Cannot read Git tree for '{alias}': {exc}") from exc

        # 2. Validate APEXlang offline
        val_report = validator(selected_tree)
        if not val_report.success:
            err_msg = "; ".join(val_report.errors) if val_report.errors else "validation failed"
            raise PublishError(f"APEXlang validation failed for '{alias}': {err_msg}")

        # 3. Capture Builder app read-only
        try:
            observed = capture_app(
                t,
                repo=repo_path,
                root=state_root,
                persist=False,
                control_store=store,
                runner=runner,
            )
        except ApexError as exc:
            raise PublishError(f"Cannot capture application '{alias}': {exc}") from exc

        obs_digest = tree_digest(observed.tree)

        # 4. Check generation and sync state
        try:
            sync_state = store.read_app_sync_state(t.physical_key)
            generation = sync_state.generation
        except Exception as exc:
            raise PublishError(f"Cannot read sync state for '{alias}': {exc}") from exc

        # 5. Check baseline / receipt / replace-from
        try:
            baseline = load_baseline(t, root=state_root)
        except StateError:
            baseline = None

        allowed = False
        baseline_digest = baseline.tree_digest if baseline is not None else None
        if baseline is not None and observed.tree == baseline.tree:
            allowed = True
        elif _receipt_allows_import(t, observed.tree, selected_tree, resolved_commit, state_root):
            allowed = True
        elif alias in replace_map:
            rec_id = replace_map[alias]
            try:
                replacement = load_capture(t, rec_id, root=state_root)
            except StateError as exc:
                raise PublishError(f"--replace-from recovery for '{alias}' is unreadable: {exc}") from exc
            if replacement.mine != observed.tree:
                raise PublishError(f"--replace-from capture for '{alias}' does not match observed Builder state")
            allowed = True

        if not allowed:
            if baseline is None:
                raise PublishError(
                    f"Application '{alias}' has no verified baseline; capture existing app and use --replace-from"
                )
            raise PublishError(
                f"Application '{alias}' differs from verified baseline; export-app and reconcile before publishing"
            )

        # 6. Registered roster
        try:
            roster = tuple(entry.checkout_uuid for entry in store.list_registry(t))
        except Exception as exc:
            raise PublishError(f"Cannot read registered checkouts for '{alias}': {exc}") from exc

        apps_data[alias] = {
            "target": _target_descriptor(t),
            "state_key": t.state_key,
            "tree_digest": obs_digest,
            "generation": generation,
            "baseline_digest": baseline_digest,
            "roster": list(roster),
            "lock_report": {
                "alias": report.alias,
                "app_id": report.app_id,
                "status": report.status,
                "source": report.source,
                "pages": [
                    {
                        "page_id": p.page_id,
                        "page_name": p.page_name,
                        "locked_by": p.locked_by,
                        "locked_on": p.locked_on,
                        "comment": p.comment,
                    }
                    for p in report.pages
                ],
            },
            "validation": {
                "success": val_report.success,
                "status": val_report.status,
                "log": val_report.log,
                "errors": list(val_report.errors),
            },
            "replace_from": replace_map.get(alias),
        }

    # Store durable preparation record under .sync-state/publish/<prep_id>/prepare.json
    import uuid
    prep_id = preparation_id or uuid.uuid4().hex
    prep_dir = state_root / "publish" / prep_id
    if prep_dir.exists() or prep_dir.is_symlink():
        raise PublishError(f"Publish preparation directory already exists: {prep_dir}")

    record_time = prepared_at_utc or datetime.now(timezone.utc).isoformat(timespec="seconds")
    record_payload = {
        "version": 1,
        "type": "publish-preparation",
        "preparation_id": prep_id,
        "prepared_at_utc": record_time,
        "source_commit": resolved_commit,
        "aliases": list(sorted_aliases),
        "apps": apps_data,
    }

    # Derive canonical SHA-256 record digest before inserting record_digest key
    canonical_bytes = json.dumps(record_payload, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    record_digest = hashlib.sha256(canonical_bytes).hexdigest()
    record_payload["record_digest"] = record_digest

    prep_dir.mkdir(parents=True, exist_ok=False)
    target_file = prep_dir / "prepare.json"
    fd, temp_file_path = tempfile.mkstemp(prefix=".prepare-", dir=str(prep_dir))
    final_bytes = json.dumps(record_payload, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(final_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_file_path, target_file)
    except OSError as exc:
        try:
            Path(temp_file_path).unlink(missing_ok=True)
        except OSError:
            pass
        raise PublishError(f"Could not write preparation record: {exc}") from exc

    return Preparation(
        preparation_id=prep_id,
        version=1,
        aliases=sorted_aliases,
        source_commit=resolved_commit,
        prepared_at_utc=record_time,
        record_digest=record_digest,
        apps=apps_data,
        path=target_file,
    )


def load_preparation(repo: Path | str, preparation_id: str) -> Preparation:
    """Load and verify a stored publish preparation record."""
    repo_path = Path(repo)
    prep_file = repo_path / ".sync-state" / "publish" / preparation_id / "prepare.json"
    if prep_file.is_symlink() or not prep_file.is_file():
        raise PublishError(f"Publish preparation record not found: {prep_file}")

    try:
        data = json.loads(prep_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PublishError(f"Cannot read preparation record '{prep_file}': {exc}") from exc

    if not isinstance(data, dict):
        raise PublishError(f"Preparation record is malformed: {prep_file}")
    if data.get("version") != 1 or data.get("type") != "publish-preparation":
        raise PublishError(f"Preparation record format or version mismatch: {prep_file}")

    stored_digest = data.get("record_digest")
    if not isinstance(stored_digest, str) or not stored_digest:
        raise PublishError("Preparation record is missing record_digest")

    # Recompute record digest without record_digest key
    payload_to_verify = {k: v for k, v in data.items() if k != "record_digest"}
    expected_bytes = json.dumps(payload_to_verify, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    computed_digest = hashlib.sha256(expected_bytes).hexdigest()
    if computed_digest != stored_digest:
        raise PublishError(f"Preparation record digest mismatch; record has been tampered with: {prep_file}")

    aliases = tuple(data.get("aliases", []))
    apps = data.get("apps", {})
    return Preparation(
        preparation_id=data.get("preparation_id", preparation_id),
        version=data.get("version", 1),
        aliases=aliases,
        source_commit=data.get("source_commit", ""),
        prepared_at_utc=data.get("prepared_at_utc", ""),
        record_digest=stored_digest,
        apps=apps,
        path=prep_file,
    )


def format_publish_notice(
    prep: Preparation,
    *,
    operator: str = "the import operator",
    duration: str = "unknown",
) -> str:
    """Format an app-scoped pause notice and lock owner report."""
    lines = [
        f"=== PUBLISH PAUSE NOTICE: {', '.join(prep.aliases).upper()} ===",
        f"Operator: {operator}",
        f"Selected commit: {prep.source_commit}",
        f"Preparation ID: {prep.preparation_id} (digest: {prep.record_digest[:16]}...)",
        f"Prepared at: {prep.prepared_at_utc}",
        f"Expected duration: {duration}",
        "",
        "Everyone must stop editing in App Builder for the selected applications now.",
        "Do not save changes until an all-clear is posted for these applications.",
        "",
    ]

    for alias in prep.aliases:
        app_data = prep.apps.get(alias, {})
        target = app_data.get("target", {})
        lines.append(f"--- Application: {alias} (App {target.get('app_id')}, Workspace {target.get('workspace_id')}) ---")
        roster = app_data.get("roster", [])
        if roster:
            lines.append("Registered checkouts: " + ", ".join(roster))
        else:
            lines.append("Registered checkouts: none")

        lock_data = app_data.get("lock_report", {})
        pages = tuple(
            PageLock(
                page_id=p["page_id"],
                page_name=p.get("page_name"),
                locked_by=p["locked_by"],
                locked_on=p.get("locked_on"),
                comment=p.get("comment"),
            )
            for p in lock_data.get("pages", [])
        )
        report = LockReport(
            alias=alias,
            app_id=target.get("app_id", 0),
            status=lock_data.get("status", "KNOWN"),
            pages=pages,
            source=lock_data.get("source", "UNKNOWN"),
        )
        lines.append(format_lock_report(report).strip())
        lines.append("")

    return "\n".join(lines).strip() + "\n"
