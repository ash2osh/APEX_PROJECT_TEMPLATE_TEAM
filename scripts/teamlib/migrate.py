"""Migration lifecycle state machine for forward, undo, and redo operations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import inspect
from pathlib import Path
import uuid
from typing import Any
from collections.abc import Mapping

from .config import Target
from .destructive_confirmation import (
    ConfirmationError,
    ConfirmationRequirement,
    confirmation_template,
    require_confirmations,
)
from .migration_bundle import BundleError, Migration, load_bundles
from .migration_plan import Plan, plan_migrations, plan_redo, plan_undo
from .migration_store import MigrationSetupRequired
from .sqlcl import result_is_unknown


class MigrationRunError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunReport:
    run_token: str
    action: str
    selected: tuple[str, ...]
    applied: tuple[str, ...]
    reverted: tuple[str, ...]
    foreign_applied: tuple[str, ...]
    foreign_reverted: tuple[str, ...]
    blocked_attempt: str | None
    verified_inventory_digest: str | None
    confirmation_template: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class InventoryObservation:
    """A live observation with both its identity and complete manifest."""

    digest: str
    manifest: Mapping[str, Any] | None = None


def _target(profiles: Mapping[str, Any]) -> Target:
    target = profiles.get("target")
    if not isinstance(target, Target):
        raise MigrationRunError("migration profiles require a verified METADATA Target")
    return target


def _mode(target: Target, profiles: Mapping[str, Any]) -> str:
    value = profiles.get("mode")
    if value in {"shared", "strict"}:
        return value
    return "shared" if target.role in {"developer", "integration"} else "strict"


def _observation(callback: Any, migration: Migration, phase: str) -> InventoryObservation | None:
    if not callable(callback):
        return None
    value = callback(migration, phase)
    digest = getattr(value, "digest", None)
    if digest is None and isinstance(value, Mapping):
        digest = value.get("inventory_digest")
    if digest is None:
        digest = value
    if not isinstance(digest, str) or not digest:
        raise MigrationRunError(f"{phase} inventory observation did not return a digest")
    manifest: Mapping[str, Any] | None = None
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        candidate = as_dict()
        if isinstance(candidate, Mapping):
            manifest = dict(candidate)
    elif isinstance(value, Mapping) and isinstance(value.get("objects"), Mapping):
        manifest = dict(value)
    if manifest is not None and manifest.get("inventory_digest") != digest:
        raise MigrationRunError(f"{phase} inventory manifest digest does not match its observation")
    return InventoryObservation(digest, manifest)


def _call_callback(callback: Any, arguments: tuple[Any, ...], legacy_arguments: tuple[Any, ...] | None = None) -> Any:
    """Call the v2 callback interface with a narrow one-argument compatibility shim."""
    if not callable(callback):
        return None
    call_arguments = arguments
    if legacy_arguments is not None:
        try:
            signature = inspect.signature(callback)
            positional = [
                parameter for parameter in signature.parameters.values()
                if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
            ]
            accepts_varargs = any(parameter.kind is parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
            if not accepts_varargs and len(positional) < len(arguments):
                call_arguments = legacy_arguments
        except (TypeError, ValueError):
            pass
    return callback(*call_arguments)


def _payload_targets(profiles: Mapping[str, Any]) -> dict[str, Target]:
    value = profiles.get("payload_targets")
    if not isinstance(value, Mapping) or set(value) != {"tables", "code"}:
        raise MigrationRunError("migration profiles require exactly tables and code payload targets")
    targets: dict[str, Target] = {}
    for role in ("tables", "code"):
        target = value.get(role)
        if not isinstance(target, Target):
            raise MigrationRunError(f"migration payload target is not a verified Target: {role}")
        if target.environment == "production":
            raise MigrationRunError(f"migration payload target is production: {role}")
        if not target.state_key:
            raise MigrationRunError(f"migration payload target identity is incomplete: {role}")
        targets[role] = target
    return targets


def _store_methods(store: Any) -> None:
    required = (
        "bootstrap", "acquire", "release", "read_history", "record_attempt_start",
        "record_attempt_state", "record_event",
    )
    if not all(callable(getattr(store, name, None)) for name in required):
        raise MigrationRunError("migration profiles require a compatible v2 migration metadata store")


def _history_for_dry_run(store: Any, target: Target, profiles: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        history = store.read_history(target)
    except MigrationSetupRequired:
        history = profiles.get("history", {})
    if not isinstance(history, Mapping):
        raise MigrationRunError("migration history must be a mapping")
    return history


def _base_plan(bundles: Mapping[str, Migration], history: Mapping[str, Any], target: Target, profiles: Mapping[str, Any]) -> Plan:
    plan = plan_migrations(bundles, history, _mode(target, profiles))
    if plan.errors:
        raise MigrationRunError("migration plan is blocked: " + "; ".join(plan.errors))
    return plan


def _selected_plan(
    action: str,
    selected_ids: tuple[str, ...],
    bundles: Mapping[str, Migration],
    history: Mapping[str, Any],
    target: Target,
    profiles: Mapping[str, Any],
) -> Plan:
    if action == "migrate":
        plan = _base_plan(bundles, history, target, profiles)
    else:
        # The forward planner intentionally reports dependents of reverted
        # migrations as blocked.  That diagnostic must not prevent restoring
        # the dependency itself; the direction-specific planner below checks
        # only the selected lifecycle transition.  Unresolved attempts remain
        # a global fail-closed gate for every action.
        plan = plan_migrations(bundles, history, _mode(target, profiles))
        unresolved = [error for error in plan.errors if error.startswith("unresolved history attempt")]
        if unresolved:
            raise MigrationRunError("migration plan is blocked: " + "; ".join(unresolved))
    if action == "migrate":
        if selected_ids and selected_ids != plan.pending:
            raise MigrationRunError("recomputed migration plan differs from requested forward selection")
        return plan
    if len(selected_ids) != 1:
        raise MigrationRunError(f"{action} requires exactly one migration ID")
    try:
        if action == "undo":
            plan_undo(bundles, history, selected_ids[0])
        else:
            plan_redo(bundles, history, selected_ids[0])
    except BundleError as exc:
        raise MigrationRunError(str(exc)) from exc
    return plan


def _requirements(
    action: str,
    selected_ids: tuple[str, ...],
    bundles: Mapping[str, Migration],
    payload_targets: Mapping[str, Target],
) -> tuple[tuple[ConfirmationRequirement, ...], Mapping[str, bool]]:
    requirements: list[ConfirmationRequirement] = []
    destructive_by_id: dict[str, bool] = {}
    for migration_id in selected_ids:
        migration = bundles[migration_id]
        destructive = migration.down_destructive if action == "undo" else migration.destructive
        destructive_by_id[migration_id] = destructive
        if destructive:
            requirements.append(
                ConfirmationRequirement(
                    migration_id=migration.id,
                    action=action,
                    bundle_checksum=migration.checksum,
                    payload_target_state_key=payload_targets[migration.target].state_key,
                )
            )
    return tuple(requirements), destructive_by_id


def _report(
    run_token: str,
    action: str,
    selected: tuple[str, ...],
    plan: Plan,
    applied: list[str],
    reverted: list[str],
    verified_inventory_digest: str | None,
    confirmation: Mapping[str, Any] | None,
) -> RunReport:
    return RunReport(
        run_token=run_token,
        action=action,
        selected=selected,
        applied=tuple(applied),
        reverted=tuple(reverted),
        foreign_applied=plan.foreign_applied,
        foreign_reverted=plan.foreign_reverted,
        blocked_attempt=None,
        verified_inventory_digest=verified_inventory_digest,
        confirmation_template=confirmation,
    )


def _apply_operation(
    action: str,
    selected_ids: tuple[str, ...],
    bundles: Mapping[str, Migration],
    profiles: Mapping[str, Any],
    confirmation: Mapping[str, Any] | None,
) -> RunReport:
    if action not in {"migrate", "undo", "redo"}:
        raise MigrationRunError(f"unsupported migration action: {action}")
    target = _target(profiles)
    if target.environment == "production":
        raise MigrationRunError("production migration writes are refused")
    payload_targets = _payload_targets(profiles)
    store = profiles.get("store")
    _store_methods(store)
    dry_run = bool(profiles.get("dry_run", False))
    history = _history_for_dry_run(store, target, profiles) if dry_run else None
    if dry_run:
        plan = _selected_plan(action, selected_ids, bundles, history or {}, target, profiles)
        if action == "migrate":
            selected_ids = plan.pending
        requirements, _ = _requirements(action, selected_ids, bundles, payload_targets)
        template = confirmation_template(requirements) if requirements else None
        return _report("dry-run", action, selected_ids, plan, [], [], None, template)

    if action != "migrate" or bool(profiles.get("bootstrap", False)):
        store.bootstrap(target, schema_set_digest=str(profiles.get("schema_set_digest", "")))
    run_token = uuid.uuid4().hex
    worker = str(profiles.get("worker_identity", "migration-worker"))
    host = str(profiles.get("host", "local"))
    acquired = False
    current_attempt_started = False
    applied: list[str] = []
    reverted: list[str] = []
    accepted_frontier = str(profiles.get("verified_inventory_digest")) if profiles.get("verified_inventory_digest") else None
    try:
        store.acquire(target, run_token, worker, host)
        acquired = True
        history = store.read_history(target)
        validate_chain = getattr(store, "validate_observation_chain", None)
        if callable(validate_chain):
            validate_chain(target)
        plan = _selected_plan(action, selected_ids, bundles, history, target, profiles)
        if action == "migrate":
            selected_ids = plan.pending
        if action == "migrate":
            expected_plan = profiles.get("expected_plan")
            if expected_plan is not None:
                expected_pending = tuple(expected_plan.pending) if isinstance(expected_plan, Plan) else tuple(expected_plan.get("pending", ()))
                if expected_pending != plan.pending:
                    raise MigrationRunError("recomputed migration plan differs from expected release plan")
        requirements, destructive_by_id = _requirements(action, selected_ids, bundles, payload_targets)
        if confirmation is not None and not isinstance(confirmation, Mapping):
            raise MigrationRunError("destructive confirmation must be a version-one document")
        try:
            confirmation_digest = require_confirmations(requirements, confirmation)
        except ConfirmationError as exc:
            raise MigrationRunError(str(exc)) from exc
        execute = profiles.get("execute")
        verify = profiles.get("verify")
        observe = profiles.get("observe")
        record_inventory = getattr(store, "record_inventory", None)
        ensure_observation = getattr(store, "ensure_observation", None)
        validate_frontier = getattr(store, "validate_frontier", None)
        for migration_id in selected_ids:
            migration = bundles.get(migration_id)
            if migration is None:
                raise MigrationRunError(f"migration is not present locally: {migration_id}")
            if action == "undo":
                sql_path = migration.down_sql_path
                verify_path = migration.down_verify_path
                operation = "down"
            else:
                sql_path = migration.sql_path
                verify_path = migration.verify_path
                operation = "up"
            if sql_path is None or verify_path is None:
                raise MigrationRunError(f"migration has no complete {operation} payload: {migration_id}")
            attempt_id = uuid.uuid4().hex
            current_attempt_started = False
            before_observation = _observation(observe, migration, "before")
            before_digest = before_observation.digest if before_observation else str(profiles.get("before_digest", ""))
            if profiles.get("require_observation") and not before_digest:
                raise MigrationRunError(f"migration requires a verified before inventory: {migration_id}")
            if accepted_frontier and before_digest and before_digest != accepted_frontier:
                raise MigrationRunError(f"live inventory changed before migration {migration_id}; drift gate must be rerun")
            if before_observation and before_observation.manifest is not None and callable(record_inventory):
                record_inventory(target, before_observation.manifest, run_token=run_token)
            if before_digest and callable(ensure_observation):
                ensure_observation(target, before_digest, run_token=run_token)
            if before_digest and callable(validate_frontier):
                validate_frontier(target, before_digest)
            destructive_digest = confirmation_digest if destructive_by_id[migration_id] else ""
            store.record_attempt_start(
                target, attempt_id, migration_id, migration.checksum, run_token,
                action=action, confirmation_digest=destructive_digest,
            )
            current_attempt_started = True
            phase = "execute"
            try:
                _call_callback(execute, (migration, action, sql_path), (migration,))
                phase = "verify"
                verification = _call_callback(verify, (migration, action, verify_path), (migration,))
                if callable(verify) and verification is not True:
                    raise MigrationRunError(f"verification failed for {migration_id}")
                phase = "observe-after"
                after_observation = _observation(observe, migration, "after")
                after_digest = after_observation.digest if after_observation else str(profiles.get("after_digest", ""))
                evidence_digest = str(profiles.get("evidence_digest", ""))
                if profiles.get("require_observation") and not after_digest:
                    raise MigrationRunError(f"migration requires a verified after inventory: {migration_id}")
                if not evidence_digest and before_digest and after_digest:
                    evidence_digest = hashlib.sha256(f"{before_digest}:{after_digest}".encode("ascii")).hexdigest()
                phase = "record-inventory"
                if after_observation and after_observation.manifest is not None and callable(record_inventory):
                    record_inventory(target, after_observation.manifest, run_token=run_token)
                phase = "record-event"
                store.record_event(
                    target, migration_id, migration.checksum, migration.target, migration.dependencies,
                    str(profiles.get("source_commit", "unknown")), str(profiles.get("applied_by", worker)),
                    {"before": before_digest, "after": after_digest, "evidence": evidence_digest},
                    operation=operation, run_token=run_token, attempt_id=attempt_id,
                )
            except Exception as exc:
                unknown = result_is_unknown(exc)
                state = "UNKNOWN" if unknown else "FAILED"
                if not (unknown and phase == "record-event"):
                    try:
                        store.record_attempt_state(
                            target,
                            attempt_id,
                            run_token,
                            state,
                            hashlib.sha256(str(exc).encode()).hexdigest(),
                        )
                    except Exception as state_exc:
                        if result_is_unknown(state_exc):
                            raise MigrationRunError(
                                f"migration attempt-state result is unknown: {migration_id}"
                            ) from state_exc
                        raise
                if unknown:
                    raise MigrationRunError(
                        f"migration result is unknown during {phase}: {migration_id}: {exc}"
                    ) from exc
                if isinstance(exc, MigrationRunError):
                    raise
                raise MigrationRunError(
                    f"migration payload failed during {phase}: {migration_id}: {exc}"
                ) from exc
            if operation == "up":
                applied.append(migration_id)
            else:
                reverted.append(migration_id)
            if after_digest:
                accepted_frontier = after_digest
        try:
            store.release(target, run_token)
        except Exception as exc:
            if result_is_unknown(exc):
                raise MigrationRunError(
                    f"migration mutex release is unknown for run {run_token}; "
                    "inspect live metadata before recovery"
                ) from exc
            raise MigrationRunError(
                f"migration mutex release failed for run {run_token}: {exc}"
            ) from exc
        return _report(run_token, action, selected_ids, plan, applied, reverted, accepted_frontier, None)
    except Exception:
        # A failure before the current attempt starts is safe to release. Once
        # a payload attempt exists, FAILED/UNKNOWN evidence deliberately keeps
        # the mutex for the recovery command.
        if acquired and not current_attempt_started:
            try:
                store.release(target, run_token)
            except Exception:
                pass
        raise


def apply_plan(
    source: str | Path,
    profiles: Mapping[str, Any],
    bootstrap: bool | None = None,
    confirmation: Mapping[str, Any] | None = None,
    expected_plan: Plan | Mapping[str, Any] | None = None,
) -> RunReport:
    options = dict(profiles)
    if bootstrap is not None:
        options["bootstrap"] = bootstrap
    if expected_plan is not None:
        options["expected_plan"] = expected_plan
    bundles = load_bundles(source)
    return _apply_operation("migrate", (), bundles, options, confirmation)


def _apply_selected(action: str, source: str | Path, migration_id: str, profiles: Mapping[str, Any], confirmation: Mapping[str, Any] | None) -> RunReport:
    bundles = load_bundles(source)
    if migration_id not in bundles:
        raise MigrationRunError(f"migration is not present locally: {migration_id}")
    return _apply_operation(action, (migration_id,), bundles, dict(profiles), confirmation)


def apply_undo(source: str | Path, migration_id: str, profiles: Mapping[str, Any], *, confirmation: Mapping[str, Any] | None = None) -> RunReport:
    return _apply_selected("undo", source, migration_id, profiles, confirmation)


def apply_redo(source: str | Path, migration_id: str, profiles: Mapping[str, Any], *, confirmation: Mapping[str, Any] | None = None) -> RunReport:
    return _apply_selected("redo", source, migration_id, profiles, confirmation)
