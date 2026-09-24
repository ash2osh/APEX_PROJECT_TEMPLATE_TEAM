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
import re
import tempfile
from typing import Any, Literal
from collections.abc import Callable, Mapping, Sequence

from .apex import (
    ApexError,
    ImportUnknown,
    _git_head,
    _receipt_allows_import,
    capture_app,
    import_app,
)
from .apex_validate import ValidationReport, validate_apexlang_tree
from .config import Target, profile_target
from .control_store import ControlStore, PublishPreparationApp, SqlControlStore
from .page_locks import LockReport, PageLock, format_lock_report, read_page_locks
from .sqlcl import result_is_unknown, run_sqlcl
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


@dataclass(frozen=True)
class AppPublishResult:
    alias: str
    status: Literal["VERIFIED", "UNCHANGED", "UNKNOWN", "FAILED"]
    verified: bool
    operation_id: str | None = None
    recovery_path: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PublishReport:
    preparation_id: str
    source_commit: str
    overall_status: Literal["VERIFIED", "PARTIAL", "FAILED", "UNKNOWN"]
    all_clear_allowed: bool
    app_results: Mapping[str, AppPublishResult]
    journal_path: Path


def _authorize_observed_app(
    target: Target,
    observed_tree: Mapping[str, bytes],
    selected_tree: Mapping[str, bytes],
    source_commit: str,
    state_root: Path,
    replace_id: str | None,
) -> str | None:
    """Apply the same baseline, receipt and replacement conditions as import_app."""
    try:
        baseline = load_baseline(target, root=state_root)
    except StateError:
        baseline = None

    if baseline is None and replace_id is None:
        raise PublishError(
            f"Application '{target.alias}' has no verified baseline; capture existing app and use --replace-from"
        )

    replacement = None
    if replace_id is not None:
        try:
            replacement = load_capture(target, replace_id, root=state_root)
        except StateError as exc:
            raise PublishError(f"--replace-from recovery for '{target.alias}' is unreadable: {exc}") from exc
        if replacement.mine != observed_tree:
            raise PublishError(f"--replace-from capture for '{target.alias}' does not match observed Builder state")

    if not (
        (baseline is not None and observed_tree == baseline.tree)
        or _receipt_allows_import(target, observed_tree, selected_tree, source_commit, state_root)
        or replacement is not None
    ):
        raise PublishError(
            f"Application '{target.alias}' differs from verified baseline; export-app and reconcile before publishing"
        )
    return baseline.tree_digest if baseline is not None else None


def _require_roster_unchanged(
    store: ControlStore | SqlControlStore, target: Target, expected: Sequence[str]
) -> None:
    try:
        current = {entry.checkout_uuid for entry in store.list_registry(target)}
    except Exception as exc:
        raise PublishError(f"Cannot refresh checkout roster for '{target.alias}': {exc}") from exc
    if current != set(expected):
        raise PublishError(f"Application '{target.alias}' checkout roster changed after preparation; prepare again")


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
        baseline_digest = _authorize_observed_app(
            t, observed.tree, selected_tree, resolved_commit, state_root, replace_map.get(alias)
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

    try:
        store.register_publish_preparation(
            prep_id,
            record_digest,
            [
                (target, target.alias or "", tuple(apps_data[target.alias or ""]["roster"]))
                for target in sorted(targets, key=lambda value: value.alias or "")
            ],
        )
    except Exception as exc:
        raise PublishError(f"Could not register shared publish preparation: {exc}") from exc

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
            lines.append(f"Registered checkouts: {len(roster)}")
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


def acknowledge_publish(
    repo: Path | str,
    preparation_id: str,
    checkout_uuid: str,
    host: str,
    acknowledged_by_user: str,
    *,
    config: Any,
    store: ControlStore | SqlControlStore,
) -> tuple[str, ...]:
    """Record this checkout's acknowledgement for selected apps where it is registered."""
    if getattr(config, "role", None) != "developer" or getattr(config, "environment", None) != "development":
        raise PublishError("publish acknowledgement is restricted to shared development targets")
    if not all((checkout_uuid, host, acknowledged_by_user)):
        raise PublishError("TEAM_CHECKOUT_UUID, host and user are required to acknowledge publish")

    del repo  # A teammate checkout only needs the shared preparation record.
    try:
        shared_preparation = store.list_publish_preparation(preparation_id)
    except Exception as exc:
        raise PublishError(f"Cannot read shared publish preparation: {exc}") from exc
    _validate_shared_preparation(shared_preparation, preparation_id)
    targets: list[tuple[str, Target, str]] = []

    # Validate all target bindings and checkout identities before writing any
    # row, so a bad multi-app invocation cannot acknowledge only a prefix.
    for app in shared_preparation:
        alias = app.alias
        if alias not in config.apps:
            raise PublishError(f"Application '{alias}' is not configured in environment")
        target = profile_target(config, "APEX", alias=alias)
        if target.physical_key != app.target_key:
            raise PublishError(f"Target binding for '{alias}' has changed since preparation")
        try:
            roster = store.list_registry(target)
        except Exception as exc:
            raise PublishError(f"Cannot read checkout roster for '{alias}': {exc}") from exc
        current_roster = {entry.checkout_uuid for entry in roster}
        prepared_roster = set(app.checkout_roster)
        if current_roster != prepared_roster:
            raise PublishError(f"Application '{alias}' checkout roster changed after preparation; prepare again")
        current_checkout = next(
            (entry for entry in roster if entry.checkout_uuid == checkout_uuid), None
        )
        if current_checkout is not None:
            if current_checkout.host != host or current_checkout.registered_by_user != acknowledged_by_user:
                raise PublishError(f"Registered host and user for checkout '{checkout_uuid}' do not match this invocation")
            targets.append((alias, target, app.preparation_digest))

    if not targets:
        raise PublishError("This checkout is not registered for any application in the preparation")

    acknowledged: list[str] = []
    for alias, target, preparation_digest in targets:
        try:
            store.record_publish_acknowledgement(
                target,
                preparation_id,
                preparation_digest,
                checkout_uuid,
                host,
                acknowledged_by_user,
            )
        except Exception as exc:
            raise PublishError(f"Cannot record publish acknowledgement for '{alias}': {exc}") from exc
        acknowledged.append(alias)
    return tuple(acknowledged)


def _validate_shared_preparation(
    apps: Sequence[PublishPreparationApp], preparation_id: str
) -> tuple[str, tuple[PublishPreparationApp, ...]]:
    if not apps:
        raise PublishError(f"Shared preparation '{preparation_id}' is unknown or stale; prepare again")
    if any(app.preparation_id != preparation_id for app in apps):
        raise PublishError("Shared preparation metadata is malformed")
    digests = {app.preparation_digest for app in apps}
    aliases = [app.alias for app in apps]
    if len(digests) != 1 or len(aliases) != len(set(aliases)) or not all(aliases):
        raise PublishError("Shared preparation metadata is inconsistent")
    digest = next(iter(digests))
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PublishError("Shared preparation digest is malformed")
    return digest, tuple(sorted(apps, key=lambda app: app.alias))


def _verify_shared_preparation_matches_local(
    prep: Preparation,
    config: Any,
    store: ControlStore | SqlControlStore,
) -> Mapping[str, PublishPreparationApp]:
    try:
        shared_rows = store.list_publish_preparation(prep.preparation_id)
    except Exception as exc:
        raise PublishError(f"Cannot read shared publish preparation: {exc}") from exc
    shared_digest, shared_apps = _validate_shared_preparation(shared_rows, prep.preparation_id)
    if shared_digest != prep.record_digest:
        raise PublishError("Shared preparation digest does not match the local preparation record")
    if tuple(app.alias for app in shared_apps) != tuple(sorted(prep.aliases)):
        raise PublishError("Shared preparation aliases do not match the local preparation record")
    by_alias = {app.alias: app for app in shared_apps}
    for alias in prep.aliases:
        if alias not in config.apps:
            raise PublishError(f"Application '{alias}' is not configured in environment")
        target = profile_target(config, "APEX", alias=alias)
        app_data = prep.apps.get(alias, {})
        shared = by_alias[alias]
        if target.physical_key != shared.target_key:
            raise PublishError(f"Target binding for '{alias}' has changed since preparation")
        if tuple(sorted(app_data.get("roster", []))) != shared.checkout_roster:
            raise PublishError(f"Shared preparation checkout roster does not match local preparation for '{alias}'")
        if target.state_key != app_data.get("state_key") or target.binding_digest != app_data.get("target", {}).get("binding_digest"):
            raise PublishError(f"Target binding for '{alias}' has changed since preparation")
    return by_alias


def publish_prepared(
    repo: Path | str,
    preparation_id: str,
    confirm_pause: bool,
    *,
    config: Any,
    store: ControlStore | SqlControlStore,
    runner: Callable[..., Any] = run_sqlcl,
    lock_reader: Callable[..., LockReport] = read_page_locks,
) -> PublishReport:
    """Execute guarded publish for previously prepared and acknowledged applications."""
    if not confirm_pause:
        raise PublishError("Publish cancelled; explicit pause confirmation was not received (--confirm-pause)")
    if getattr(config, "role", None) != "developer" or getattr(config, "environment", None) != "development":
        raise PublishError("publish is restricted to shared development targets")

    repo_path = Path(repo)
    prep = load_preparation(repo_path, preparation_id)
    shared_preparation = _verify_shared_preparation_matches_local(prep, config, store)

    # 1. Check the acknowledgements written by each registered checkout.
    for alias in prep.aliases:
        app_data = prep.apps.get(alias, {})
        required_roster = set(app_data.get("roster", []))
        if alias not in config.apps:
            raise PublishError(f"Application '{alias}' is not configured in environment")
        target = profile_target(config, "APEX", alias=alias)
        stored_target = app_data.get("target", {})
        if target.state_key != app_data.get("state_key") or target.binding_digest != stored_target.get("binding_digest"):
            raise PublishError(f"Target binding for '{alias}' has changed since preparation")
        if target.physical_key != shared_preparation[alias].target_key:
            raise PublishError(f"Target binding for '{alias}' has changed since preparation")
        try:
            stored_acknowledgements = store.list_publish_acknowledgements(
                target, prep.preparation_id, prep.record_digest
            )
        except Exception as exc:
            raise PublishError(f"Cannot read checkout acknowledgements for '{alias}': {exc}") from exc
        try:
            current_registry = {
                row.checkout_uuid: row for row in store.list_registry(target)
            }
        except Exception as exc:
            raise PublishError(f"Cannot read checkout roster for '{alias}': {exc}") from exc
        provided = {
            row.checkout_uuid
            for row in stored_acknowledgements
            if row.preparation_digest == prep.record_digest
            and row.checkout_uuid in current_registry
            and row.host == current_registry[row.checkout_uuid].host
            and row.acknowledged_by_user == current_registry[row.checkout_uuid].registered_by_user
        }
        missing = required_roster - provided
        if missing:
            raise PublishError(
                f"Missing required checkout acknowledgements from the control store for '{alias}': {', '.join(sorted(missing))}"
            )

    # 3. All-app preflight: check target binding, source cleanliness, recapture and locks for ALL selected apps before any write
    preflight_targets: list[Target] = []
    state_root = repo_path / ".sync-state"

    for alias in prep.aliases:
        if alias not in config.apps:
            raise PublishError(f"Application '{alias}' is not configured in environment")
        target = profile_target(config, "APEX", alias=alias)
        stored_target = prep.apps[alias]["target"]
        if target.state_key != prep.apps[alias]["state_key"] or target.binding_digest != stored_target.get("binding_digest"):
            raise PublishError(f"Target binding for '{alias}' has changed since preparation")

        try:
            assert_source_clean(repo_path, alias)
        except (ApexError, TreeError) as exc:
            raise PublishError(f"Source for '{alias}' is not clean: {exc}") from exc

        # Recapture Builder app
        try:
            recapture = capture_app(
                target,
                repo=repo_path,
                root=state_root,
                persist=False,
                control_store=store,
                runner=runner,
            )
        except ApexError as exc:
            raise PublishError(f"Preflight capture failed for '{alias}': {exc}") from exc

        if recapture.after_sync.generation != prep.apps[alias]["generation"]:
            raise PublishError(f"Application '{alias}' generation changed after preparation; publish refused before write")
        if tree_digest(recapture.tree) != prep.apps[alias]["tree_digest"]:
            raise PublishError(f"Application '{alias}' changed after preparation; publish refused before write")

        # Page-lock inspection is an external read. Check authorization and
        # roster after it so a change during the report cannot pass preflight.
        refreshed_locks = lock_reader(target, runner=runner)
        if refreshed_locks.status != "KNOWN":
            raise PublishError(f"Refreshed page locks for '{alias}' is UNKNOWN; publish refused before write")

        try:
            selected_tree = read_git_tree(repo_path, prep.source_commit, alias)
        except TreeError as exc:
            raise PublishError(f"Selected source for '{alias}' is unavailable: {exc}") from exc
        baseline_digest = _authorize_observed_app(
            target, recapture.tree, selected_tree, prep.source_commit,
            state_root, prep.apps[alias].get("replace_from"),
        )
        if baseline_digest != prep.apps[alias]["baseline_digest"]:
            raise PublishError(f"Application '{alias}' baseline changed after preparation; publish refused before write")

        _require_roster_unchanged(store, target, prep.apps[alias]["roster"])

        preflight_targets.append(target)

    # 4. Execute sequential imports
    results: dict[str, AppPublishResult] = {}
    failed = False
    failed_alias: str | None = None
    failed_error: str | None = None
    first_failure_status: Literal["UNKNOWN", "FAILED"] = "FAILED"

    for target in preflight_targets:
        alias = target.alias or ""
        if failed:
            results[alias] = AppPublishResult(
                alias=alias,
                status="UNCHANGED",
                verified=False,
            )
            continue

        replace_id = prep.apps[alias].get("replace_from")
        try:
            # A previous app import takes time; never import this app if a
            # checkout registered after the all-app preflight.
            _require_roster_unchanged(store, target, prep.apps[alias]["roster"])
            baseline = import_app(
                target,
                prep.source_commit,
                replace_from=replace_id,
                repo=repo_path,
                root=state_root,
                control_store=store,
                runner=runner,
                expected_roster=frozenset(prep.apps[alias]["roster"]),
            )
            op_id = baseline.operation_id
            if not op_id:
                raise PublishError(f"Verified import for '{alias}' did not return its recovery operation")
            rec_path = str(state_root / "recovery" / op_id)
            results[alias] = AppPublishResult(
                alias=alias,
                status="VERIFIED",
                verified=True,
                operation_id=op_id,
                recovery_path=rec_path,
            )
        except Exception as exc:
            failed = True
            failed_alias = alias
            failed_error = str(exc)
            is_unknown = isinstance(exc, ImportUnknown) or result_is_unknown(exc)
            app_status: Literal["UNKNOWN", "FAILED"] = "UNKNOWN" if is_unknown else "FAILED"
            first_failure_status = app_status
            results[alias] = AppPublishResult(
                alias=alias,
                status=app_status,
                verified=False,
                operation_id=exc.operation_id if isinstance(exc, ImportUnknown) else None,
                recovery_path=str(exc.recovery_path) if isinstance(exc, ImportUnknown) else None,
                error=failed_error,
            )

    # 5. Determine overall status and write durable result journal
    all_verified = all(r.status == "VERIFIED" for r in results.values())
    if all_verified:
        overall_status = "VERIFIED"
        all_clear_allowed = True
    elif first_failure_status == "UNKNOWN":
        overall_status = "UNKNOWN"
        all_clear_allowed = False
    elif any(r.status == "VERIFIED" for r in results.values()):
        overall_status = "PARTIAL"
        all_clear_allowed = False
    else:
        overall_status = "FAILED"
        all_clear_allowed = False

    prep_dir = state_root / "publish" / prep.preparation_id
    prep_dir.mkdir(parents=True, exist_ok=True)
    journal_path = prep_dir / "result.json"
    journal_payload = {
        "version": 1,
        "type": "publish-result",
        "preparation_id": prep.preparation_id,
        "published_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_commit": prep.source_commit,
        "overall_status": overall_status,
        "all_clear_allowed": all_clear_allowed,
        "apps": {
            alias: {
                "status": r.status,
                "verified": r.verified,
                "operation_id": r.operation_id,
                "recovery_path": r.recovery_path,
                "error": r.error,
            }
            for alias, r in results.items()
        },
    }
    # Atomic write to result.json
    fd, temp_file_path = tempfile.mkstemp(prefix=".result-", dir=str(prep_dir))
    final_bytes = json.dumps(journal_payload, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(final_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_file_path, journal_path)
    except OSError as exc:
        try:
            Path(temp_file_path).unlink(missing_ok=True)
        except OSError:
            pass
        raise PublishError(f"Could not write publish journal: {exc}") from exc

    if failed:
        raise PublishError(f"Publish failed during import of '{failed_alias}': {failed_error}")

    return PublishReport(
        preparation_id=prep.preparation_id,
        source_commit=prep.source_commit,
        overall_status=overall_status,
        all_clear_allowed=all_clear_allowed,
        app_results=results,
        journal_path=journal_path,
    )
