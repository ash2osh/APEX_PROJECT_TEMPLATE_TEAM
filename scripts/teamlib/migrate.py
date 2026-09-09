"""Migration application state machine built on migration_store."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import uuid
from typing import Any
from collections.abc import Callable, Mapping

from .config import Target
from .migration_bundle import Migration, load_bundles
from .migration_plan import Plan, plan_migrations
from .migration_store import MigrationSetupRequired
from .sqlcl import SqlclError


class MigrationRunError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunReport:
    run_token: str
    applied: tuple[str, ...]
    foreign_applied: tuple[str, ...]
    blocked_attempt: str | None
    verified_inventory_digest: str | None


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


def _result_is_unknown(exc: BaseException) -> bool:
    """Classify only lost acknowledgement/timeout failures as UNKNOWN.

    A normal Oracle/SQLcl error is a known failed payload and is recorded as
    FAILED.  A timeout or explicit unknown-state boundary cannot establish
    whether the database committed, so it must retain the mutex as UNKNOWN.
    """
    if not isinstance(exc, SqlclError):
        return False
    text = str(exc).casefold()
    return any(marker in text for marker in ("timed out", "state is unknown", "acknowledg", "lost result"))


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


def apply_plan(
    source: str | Path,
    profiles: Mapping[str, Any],
    bootstrap: bool = False,
    confirmation: Mapping[str, Any] | bool | None = None,
    expected_plan: Plan | Mapping[str, Any] | None = None,
) -> RunReport:
    target = _target(profiles)
    dry_run = bool(profiles.get("dry_run", False))
    if target.environment == "production" and not dry_run:
        raise MigrationRunError("production migration writes are refused")
    bundles = load_bundles(source)
    store = profiles.get("store")
    required_store_methods = (
        "bootstrap", "acquire", "release", "read_history", "record_attempt_start",
        "record_attempt_state", "record_applied",
    )
    if not all(callable(getattr(store, name, None)) for name in required_store_methods):
        raise MigrationRunError("migration profiles require a compatible migration metadata store")
    if dry_run:
        try:
            history = store.read_history(target)
        except MigrationSetupRequired:
            history = profiles.get("history", {})
        plan = plan_migrations(bundles, history, _mode(target, profiles))
        if plan.errors:
            raise MigrationRunError("migration plan is blocked: " + "; ".join(plan.errors))
        return RunReport("dry-run", (), plan.foreign_applied, None, None)
    do_bootstrap = bootstrap or bool(profiles.get("bootstrap", False))
    if do_bootstrap:
        store.bootstrap(target, schema_set_digest=str(profiles.get("schema_set_digest", "")))
    run_token = uuid.uuid4().hex
    worker = str(profiles.get("worker_identity", "migration-worker"))
    host = str(profiles.get("host", "local"))
    store.acquire(target, run_token, worker, host)
    try:
        history = store.read_history(target)
        validate_chain = getattr(store, "validate_observation_chain", None)
        if callable(validate_chain):
            validate_chain(target)
        plan = plan_migrations(bundles, history, _mode(target, profiles))
        if plan.errors:
            raise MigrationRunError("migration plan is blocked: " + "; ".join(plan.errors))
        if expected_plan is not None:
            expected_pending = tuple(expected_plan.pending) if isinstance(expected_plan, Plan) else tuple(expected_plan.get("pending", ()))
            if expected_pending != plan.pending:
                raise MigrationRunError("recomputed migration plan differs from expected release plan")
        destructive = [migration_id for migration_id in plan.pending if bundles[migration_id].destructive]
        if destructive and confirmation is not True and not (isinstance(confirmation, Mapping) and confirmation.get("confirmed") is True):
            raise MigrationRunError("destructive migrations require an explicit confirmation record")
        execute: Callable[[Migration], Any] | None = profiles.get("execute")
        verify: Callable[[Migration], Any] | None = profiles.get("verify")
        applied: list[str] = []
        accepted_frontier = str(profiles.get("verified_inventory_digest")) if profiles.get("verified_inventory_digest") else None
        for migration_id in plan.pending:
            migration = bundles[migration_id]
            attempt_id = uuid.uuid4().hex
            before_observation = _observation(profiles.get("observe"), migration, "before")
            before_digest = before_observation.digest if before_observation else str(profiles.get("before_digest", ""))
            if profiles.get("require_observation") and not before_digest:
                raise MigrationRunError(f"migration requires a verified before inventory: {migration_id}")
            if accepted_frontier and before_digest and before_digest != accepted_frontier:
                raise MigrationRunError(f"live inventory changed before migration {migration_id}; drift gate must be rerun")
            record_inventory = getattr(store, "record_inventory", None)
            if before_observation and before_observation.manifest is not None and callable(record_inventory):
                record_inventory(target, before_observation.manifest, run_token=run_token)
            ensure_observation = getattr(store, "ensure_observation", None)
            validate_frontier = getattr(store, "validate_frontier", None)
            if before_digest and callable(ensure_observation):
                ensure_observation(target, before_digest, run_token=run_token)
            if before_digest and callable(validate_frontier):
                validate_frontier(target, before_digest)
            store.record_attempt_start(target, attempt_id, migration_id, migration.checksum, run_token)
            try:
                if execute is not None:
                    execute(migration)
                if verify is not None and verify(migration) is not True:
                    raise MigrationRunError(f"verification failed for {migration_id}")
                after_observation = _observation(profiles.get("observe"), migration, "after")
                after_digest = after_observation.digest if after_observation else str(profiles.get("after_digest", ""))
                evidence_digest = str(profiles.get("evidence_digest", ""))
                if profiles.get("require_observation") and not after_digest:
                    raise MigrationRunError(f"migration requires a verified after inventory: {migration_id}")
                if not evidence_digest and before_digest and after_digest:
                    evidence_digest = hashlib.sha256(f"{before_digest}:{after_digest}".encode("ascii")).hexdigest()
                if after_observation and after_observation.manifest is not None and callable(record_inventory):
                    record_inventory(target, after_observation.manifest, run_token=run_token)
            except MigrationRunError:
                store.record_attempt_state(target, attempt_id, run_token, "FAILED")
                raise
            except Exception as exc:
                state = "UNKNOWN" if _result_is_unknown(exc) else "FAILED"
                store.record_attempt_state(target, attempt_id, run_token, state, hashlib.sha256(str(exc).encode()).hexdigest())
                if state == "UNKNOWN":
                    raise MigrationRunError(f"migration result is unknown: {migration_id}: {exc}") from exc
                raise MigrationRunError(f"migration payload failed: {migration_id}: {exc}") from exc
            store.record_applied(
                target,
                migration_id,
                migration.checksum,
                migration.target,
                migration.dependencies,
                str(profiles.get("source_commit", "unknown")),
                str(profiles.get("applied_by", worker)),
                {
                    "before": before_digest,
                    "after": after_digest,
                    "evidence": evidence_digest,
                },
                run_token=run_token,
                attempt_id=attempt_id,
            )
            applied.append(migration_id)
            if after_digest:
                accepted_frontier = after_digest
        store.release(target, run_token)
        return RunReport(run_token, tuple(applied), plan.foreign_applied, None, accepted_frontier)
    except Exception:
        # A known failure intentionally leaves the mutex held with its FAILED
        # attempt. The recovery command, not an exception handler, clears it.
        raise
