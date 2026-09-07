"""Migration application state machine built on migration_store."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import uuid
from typing import Any, Callable, Mapping

from .config import Target
from .migration_bundle import Migration, load_bundles
from .migration_plan import Plan, plan_migrations
from .migration_store import MigrationMutexHeld, MigrationSetupRequired, MigrationStore, MigrationStoreError


class MigrationRunError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunReport:
    run_token: str
    applied: tuple[str, ...]
    foreign_applied: tuple[str, ...]
    blocked_attempt: str | None
    verified_inventory_digest: str | None


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
    if not isinstance(store, MigrationStore):
        raise MigrationRunError("migration profiles require MigrationStore")
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
        for migration_id in plan.pending:
            migration = bundles[migration_id]
            attempt_id = uuid.uuid4().hex
            store.record_attempt_start(target, attempt_id, migration_id, migration.checksum, run_token)
            try:
                if execute is not None:
                    execute(migration)
                if verify is not None and verify(migration) is not True:
                    raise MigrationRunError(f"verification failed for {migration_id}")
            except MigrationRunError:
                store.record_attempt_state(target, attempt_id, run_token, "FAILED")
                raise
            except Exception as exc:
                store.record_attempt_state(target, attempt_id, run_token, "FAILED", hashlib.sha256(str(exc).encode()).hexdigest())
                raise MigrationRunError(f"migration payload failed: {migration_id}: {exc}") from exc
            store.record_applied(target, migration_id, migration.checksum, migration.target, migration.dependencies, str(profiles.get("source_commit", "unknown")), str(profiles.get("applied_by", worker)), {"before": profiles.get("before_digest", ""), "after": profiles.get("after_digest", "")}, run_token=run_token, attempt_id=attempt_id)
            applied.append(migration_id)
        inventory_digest = str(profiles.get("verified_inventory_digest")) if profiles.get("verified_inventory_digest") else None
        store.release(target, run_token)
        return RunReport(run_token, tuple(applied), plan.foreign_applied, None, inventory_digest)
    except Exception:
        # A known failure intentionally leaves the mutex held with its FAILED
        # attempt. The recovery command, not an exception handler, clears it.
        raise

