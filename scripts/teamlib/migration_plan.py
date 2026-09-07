"""Offline dependency and applied-history planning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .migration_bundle import BundleError, Migration, dependency_order


@dataclass(frozen=True)
class Plan:
    pending: tuple[str, ...]
    foreign_applied: tuple[str, ...]
    errors: tuple[str, ...]


def _history_value(history: Mapping[str, Any], migration_id: str) -> Mapping[str, Any] | None:
    value = history.get(migration_id)
    return value if isinstance(value, Mapping) else None


def plan_migrations(
    bundles: Mapping[str, Migration],
    history: Mapping[str, Any],
    mode: str,
) -> Plan:
    if mode not in {"shared", "strict"}:
        return Plan((), (), (f"unsupported migration plan mode: {mode}",))
    errors: list[str] = []
    foreign: list[str] = []
    for migration_id, entry in history.items():
        if not isinstance(entry, Mapping):
            errors.append(f"malformed history entry: {migration_id}")
            continue
        status = str(entry.get("status", ""))
        if migration_id not in bundles:
            if status == "APPLIED" and mode == "shared":
                foreign.append(migration_id)
            elif status == "APPLIED":
                errors.append(f"strict history contains foreign migration: {migration_id}")
            elif status in {"RUNNING", "UNKNOWN", "FAILED"}:
                errors.append(f"unresolved history attempt: {migration_id}")
            continue
        migration = bundles[migration_id]
        if status == "APPLIED" and entry.get("checksum") != migration.checksum:
            errors.append(f"checksum conflict for applied migration: {migration_id}")
        if status in {"RUNNING", "UNKNOWN", "FAILED"}:
            errors.append(f"unresolved history attempt: {migration_id}")
        if status not in {"", "APPLIED", "RUNNING", "UNKNOWN", "FAILED"}:
            errors.append(f"unknown migration history status for {migration_id}: {status}")
    for migration in bundles.values():
        for dependency_id, declared_checksum in migration.dependencies:
            local = bundles.get(dependency_id)
            history_entry = _history_value(history, dependency_id)
            if local is not None and local.checksum != declared_checksum:
                errors.append(f"dependency checksum conflict: {migration.id} -> {dependency_id}")
            elif local is None and not (
                mode == "shared"
                and history_entry is not None
                and history_entry.get("status") == "APPLIED"
                and history_entry.get("checksum") == declared_checksum
            ):
                errors.append(f"dependency is not available in selected history: {migration.id} -> {dependency_id}")
    try:
        order = dependency_order(bundles)
    except BundleError as exc:
        # A dependency satisfied by shared central history is not part of the
        # local graph, so retry ordering with those edges removed.
        filtered: dict[str, Migration] = {}
        for migration_id, migration in bundles.items():
            dependencies = tuple(
                edge for edge in migration.dependencies
                if edge[0] in bundles
            )
            filtered[migration_id] = Migration(
                id=migration.id, stamp=migration.stamp, target=migration.target,
                checksum=migration.checksum, dependencies=dependencies,
                destructive=migration.destructive, sql_path=migration.sql_path,
                verify_path=migration.verify_path, sql_bytes=migration.sql_bytes,
                verify_bytes=migration.verify_bytes, version=migration.version,
            )
        try:
            order = dependency_order(filtered)
        except BundleError:
            errors.append(str(exc))
            order = ()
    pending = tuple(
        migration_id for migration_id in order
        if not (_history_value(history, migration_id) or {}).get("status") == "APPLIED"
    )
    return Plan(pending, tuple(sorted(foreign)), tuple(dict.fromkeys(errors)))


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="migration-plan")
    parser.add_argument("--source", required=True)
    parser.add_argument("--history", required=True)
    parser.add_argument("--mode", choices=("shared", "strict"), required=True)
    args = parser.parse_args(list(argv or []))
    from .migration_bundle import load_bundles
    try:
        raw = json.loads(__import__("pathlib").Path(args.history).read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"invalid history: {exc}")
    history = raw.get("history", raw) if isinstance(raw, dict) else {}
    plan = plan_migrations(load_bundles(args.source), history, args.mode)
    print(json.dumps({"pending": plan.pending, "foreign_applied": plan.foreign_applied, "errors": plan.errors}, sort_keys=True))
    return 0 if not plan.errors else 3
