"""Offline dependency and applied-history lifecycle planning."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from .migration_bundle import BundleError, Migration, dependency_order


_STATUSES = {"", "APPLIED", "REVERTED", "RUNNING", "UNKNOWN", "FAILED"}
_UNRESOLVED = {"RUNNING", "UNKNOWN", "FAILED"}


@dataclass(frozen=True)
class Plan:
    pending: tuple[str, ...]
    foreign_applied: tuple[str, ...]
    errors: tuple[str, ...]
    foreign_reverted: tuple[str, ...] = ()


@dataclass(frozen=True)
class _DependencyNode:
    id: str
    stamp: str
    dependencies: tuple[tuple[str, str], ...]


def _history_value(history: Mapping[str, Any], migration_id: str) -> Mapping[str, Any] | None:
    value = history.get(migration_id)
    return value if isinstance(value, Mapping) else None


def _status(entry: Mapping[str, Any] | None) -> str:
    if entry is None:
        return ""
    value = entry.get("status", "")
    return value if isinstance(value, str) else str(value)


def _sequence(entry: Mapping[str, Any], migration_id: str) -> int:
    value = entry.get("sequence", entry.get("applied_sequence"))
    if type(value) is not int or value < 1:
        raise BundleError(f"history sequence is malformed for {migration_id}")
    return value


def _filtered_bundles(bundles: Mapping[str, Migration]) -> dict[str, _DependencyNode]:
    """Remove external dependency edges so local ordering can be computed."""
    filtered: dict[str, _DependencyNode] = {}
    for migration_id, migration in bundles.items():
        dependencies = tuple(edge for edge in migration.dependencies if edge[0] in bundles)
        filtered[migration_id] = _DependencyNode(migration.id, migration.stamp, dependencies)
    return filtered


def _local_order(bundles: Mapping[str, Migration], errors: list[str]) -> tuple[str, ...]:
    try:
        return dependency_order(_filtered_bundles(bundles))
    except BundleError as exc:
        errors.append(str(exc))
        return ()


def plan_migrations(
    bundles: Mapping[str, Migration],
    history: Mapping[str, Any],
    mode: str,
) -> Plan:
    if mode not in {"shared", "strict"}:
        return Plan((), (), (f"unsupported migration plan mode: {mode}",), ())
    errors: list[str] = []
    foreign_applied: list[str] = []
    foreign_reverted: list[str] = []
    for migration_id, entry in history.items():
        if not isinstance(entry, Mapping):
            errors.append(f"malformed history entry: {migration_id}")
            continue
        status = _status(entry)
        if status not in _STATUSES:
            errors.append(f"unknown migration history status for {migration_id}: {status}")
            continue
        if migration_id not in bundles:
            if status == "APPLIED":
                if mode == "shared":
                    foreign_applied.append(migration_id)
                else:
                    errors.append(f"strict history contains foreign migration: {migration_id}")
            elif status == "REVERTED":
                if mode == "shared":
                    foreign_reverted.append(migration_id)
                else:
                    errors.append(f"strict history contains foreign reverted migration: {migration_id}")
            elif status in _UNRESOLVED:
                errors.append(f"unresolved history attempt: {migration_id}")
            continue
        migration = bundles[migration_id]
        if status in {"APPLIED", "REVERTED"} and entry.get("checksum") != migration.checksum:
            errors.append(f"checksum conflict for {status.lower()} migration: {migration_id}")
        if status in _UNRESOLVED:
            errors.append(f"unresolved history attempt: {migration_id}")

    order = _local_order(bundles, errors)
    positions = {migration_id: index for index, migration_id in enumerate(order)}
    for migration in bundles.values():
        current = _history_value(history, migration.id)
        current_status = _status(current)
        for dependency_id, declared_checksum in migration.dependencies:
            local = bundles.get(dependency_id)
            dependency_entry = _history_value(history, dependency_id)
            dependency_status = _status(dependency_entry)
            if local is not None:
                if local.checksum != declared_checksum:
                    errors.append(f"dependency checksum conflict: {migration.id} -> {dependency_id}")
                    continue
                if dependency_status == "APPLIED":
                    continue
                if dependency_status == "REVERTED":
                    errors.append(f"dependency is REVERTED: {migration.id} -> {dependency_id}")
                    continue
                if dependency_status in _UNRESOLVED:
                    continue
                if current_status == "" and positions.get(dependency_id, -1) < positions.get(migration.id, 10**9):
                    continue
                errors.append(f"dependency is not currently APPLIED: {migration.id} -> {dependency_id}")
                continue
            if dependency_status == "APPLIED" and dependency_entry is not None and dependency_entry.get("checksum") == declared_checksum:
                if mode == "shared":
                    continue
            if dependency_status == "REVERTED":
                errors.append(f"dependency is REVERTED: {migration.id} -> {dependency_id}")
            else:
                errors.append(f"dependency is not available in selected history: {migration.id} -> {dependency_id}")

    pending = tuple(
        migration_id for migration_id in order
        if _status(_history_value(history, migration_id)) == ""
    )
    return Plan(
        pending,
        tuple(sorted(set(foreign_applied))),
        tuple(dict.fromkeys(errors)),
        tuple(sorted(set(foreign_reverted))),
    )


def plan_undo(
    bundles: Mapping[str, Migration],
    history: Mapping[str, Any],
    migration_id: str,
) -> str:
    migration = bundles.get(migration_id)
    if migration is None:
        raise BundleError(f"migration is not present locally: {migration_id}")
    entry = _history_value(history, migration_id)
    if _status(entry) != "APPLIED":
        raise BundleError(f"migration is not currently APPLIED: {migration_id}")
    if entry is None or entry.get("checksum") != migration.checksum:
        raise BundleError(f"checksum conflict for applied migration: {migration_id}")
    if not migration.reversible:
        raise BundleError(f"migration is not reversible: {migration_id}")
    applied: list[tuple[int, str]] = []
    for candidate_id, candidate_entry in history.items():
        if not isinstance(candidate_entry, Mapping) or _status(candidate_entry) != "APPLIED":
            continue
        sequence = _sequence(candidate_entry, candidate_id)
        candidate = bundles.get(candidate_id)
        if candidate is not None and candidate_entry.get("checksum") != candidate.checksum:
            raise BundleError(f"checksum conflict for applied migration: {candidate_id}")
        applied.append((sequence, candidate_id))
    if not applied:
        raise BundleError("no currently APPLIED migration is available to undo")
    top = max(applied)[1]
    if top != migration_id:
        raise BundleError(f"only the latest APPLIED migration can be undone; current top is {top}")
    return migration_id


def plan_redo(
    bundles: Mapping[str, Migration],
    history: Mapping[str, Any],
    migration_id: str,
) -> str:
    migration = bundles.get(migration_id)
    if migration is None:
        raise BundleError(f"migration is not present locally: {migration_id}")
    entry = _history_value(history, migration_id)
    if _status(entry) != "REVERTED":
        raise BundleError(f"migration is not currently REVERTED: {migration_id}")
    if entry is None or entry.get("checksum") != migration.checksum:
        raise BundleError(f"checksum conflict for reverted migration: {migration_id}")
    if not migration.reversible:
        raise BundleError(f"migration is not reversible: {migration_id}")
    for dependency_id, declared_checksum in migration.dependencies:
        dependency_entry = _history_value(history, dependency_id)
        if _status(dependency_entry) != "APPLIED" or dependency_entry is None or dependency_entry.get("checksum") != declared_checksum:
            raise BundleError(f"redo dependency is not currently APPLIED: {migration_id} -> {dependency_id}")
        dependency = bundles.get(dependency_id)
        if dependency is not None and dependency.checksum != declared_checksum:
            raise BundleError(f"dependency checksum conflict: {migration_id} -> {dependency_id}")
    return migration_id


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
        raw = json.loads(Path(args.history).read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"invalid history: {exc}") from exc
    history = raw.get("history", raw) if isinstance(raw, dict) else {}
    plan = plan_migrations(load_bundles(args.source), history, args.mode)
    print(json.dumps({"pending": plan.pending, "foreign_applied": plan.foreign_applied, "foreign_reverted": plan.foreign_reverted, "errors": plan.errors}, sort_keys=True))
    return 0 if not plan.errors else 3
