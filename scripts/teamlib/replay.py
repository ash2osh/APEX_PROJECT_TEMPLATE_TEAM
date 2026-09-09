"""Deterministic migration replay and reviewed baseline adoption helpers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from .migration_bundle import BundleError, load_bundles
from .migration_plan import plan_migrations


class ReplayError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReplayReport:
    order: tuple[str, ...]
    checksums: dict[str, str]
    source_digest: str
    previous: bool = False
    evidence_path: str | None = None


def replay(source: str | Path, replay_target: Mapping[str, Any], previous: str | Path | None = None) -> ReplayReport:
    try:
        bundles = load_bundles(source)
    except BundleError as exc:
        raise ReplayError(str(exc)) from exc
    if replay_target.get("status") not in {"empty", "ready", "disposable"}:
        raise ReplayError("replay target is not a verified disposable target")
    plan = plan_migrations(bundles, {}, "strict")
    if plan.errors:
        raise ReplayError("replay plan is blocked: " + "; ".join(plan.errors))
    order = plan.pending
    checksums = {migration_id: bundles[migration_id].checksum for migration_id in order}
    source_digest = hashlib.sha256(
        json.dumps(checksums, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if previous is not None:
        previous_report = replay(previous, replay_target, None)
        if not previous_report.order:
            raise ReplayError("previous replay has no canonical history")
    apply = replay_target.get("apply")
    if callable(apply):
        for migration_id in order:
            apply(bundles[migration_id])
    return ReplayReport(order, checksums, source_digest, previous is not None)


def adopt_baseline(migration_id: str, evidence: str | Path, profiles: Mapping[str, Any]) -> bool:
    if profiles.get("environment") == "production":
        raise ReplayError("production adoption is refused")
    try:
        data = json.loads(Path(evidence).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayError("adoption evidence is unreadable") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or data.get("migration_id") != migration_id or data.get("verified") is not True:
        raise ReplayError("adoption evidence is not verified for the selected migration")
    if data.get("candidate_digest") != data.get("live_digest"):
        raise ReplayError("live schema does not equal reviewed replay evidence")
    if data.get("normalizer_version") == "unknown":
        raise ReplayError("adoption evidence has unknown normalization")
    return True


def check_history_change(base_commit: str | Path, new_commit: str | Path) -> None:
    """Offline guard used by CI; callers provide two migration directories."""
    base = load_bundles(base_commit)
    new = load_bundles(new_commit)
    for migration_id, old in base.items():
        current = new.get(migration_id)
        if current is None or current.checksum != old.checksum:
            raise ReplayError(f"canonical migration was edited or removed: {migration_id}")


def main(argv: list[str] | None = None) -> int:
    import argparse
    raw_args = list(argv or [])
    if raw_args and raw_args[0] == "adopt-baseline":
        parser = argparse.ArgumentParser(prog="adopt-baseline")
        parser.add_argument("migration_id")
        parser.add_argument("--evidence", required=True)
        parser.add_argument("--profiles", default="{}")
        args = parser.parse_args(raw_args[1:])
        profiles = json.loads(Path(args.profiles).read_text(encoding="utf-8")) if Path(args.profiles).is_file() else json.loads(args.profiles)
        adopt_baseline(args.migration_id, args.evidence, profiles)
        return 0
    parser = argparse.ArgumentParser(prog="replay")
    parser.add_argument("--source", required=True)
    parser.add_argument("--replay-env", required=True)
    parser.add_argument("--previous")
    args = parser.parse_args(raw_args)
    target = json.loads(Path(args.replay_env).read_text(encoding="utf-8"))
    report = replay(args.source, target, args.previous)
    print(json.dumps({"order": report.order, "checksums": report.checksums, "source_digest": report.source_digest}, sort_keys=True))
    return 0
