"""scripts/team.sh rehearsal-reset: archive the local release state of a staging database that was restored.

Restoring staging from a snapshot or a production copy puts the database back in time, but the local ledger
(releases/.local-state.json), rollout receipts, migration receipts and attempt records still describe the writes
made before the restore. The next rollout or migrate then refuses: an unknown run still owns the target, a step
receipt path is occupied, a migration already has a verified receipt, or a prior attempt has no receipt.

This command lists, and with --apply moves unchanged into scratch/rehearsal-reset/<time>/, only what belongs to
the restored target: its ledger runs, attempts, checkpoint verifications and recovery links (matched on the
observed db_unique_name and container), its migration receipts, attempt events and attempt folders, and every
staging rollout receipt and run folder (those record no database). Production and DEV records stay. Nothing is
deleted. It reads the target identity with one read-only query, refuses production and a production-looking
name, requires --confirm-target to equal the observed db_unique_name for --apply, and holds the target writer
lease of every matching scope while it moves anything, so a rollout or migration cannot run at the same time.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from scripts import release_ownership, release_state, terminal_confirm
from scripts.db_targets import looks_like_production_identity, resolve_target, split_list
from scripts.local_config import ConfigError, read_project_env

PROGRAM = "scripts/team.sh rehearsal-reset"
ARCHIVE_PARTS = ("scratch", "rehearsal-reset")
ENVIRONMENT = "staging"
EXIT_ERROR = 2
LEDGER_SECTIONS = ("attempts", "checkpointVerifications", "rollouts", "recoveryLinks")


class RehearsalResetError(RuntimeError):
    """The reset was refused; nothing was moved."""


@dataclass(frozen=True)
class ResetPlan:
    db_unique_name: str
    container_id: str | None
    attempts: tuple[str, ...]
    verifications: tuple[str, ...]
    rollouts: tuple[str, ...]
    recovery_links: int
    lost_proofs: tuple[str, ...]
    paths: tuple[str, ...]
    tracked: tuple[str, ...]
    scopes: tuple[tuple[tuple[str, str], ...], ...]
    ledger_sha256: str | None

    def is_empty(self) -> bool:
        return not (self.attempts or self.verifications or self.rollouts or self.recovery_links or self.paths)


# ---- selection -------------------------------------------------------------------------------------------

def _scope_matches(scope: object, db_unique_name: str, container_id: str | None) -> bool:
    return (
        isinstance(scope, Mapping)
        and str(scope.get("dbUniqueName", "")).upper() == db_unique_name.upper()
        and (container_id is None or str(scope.get("containerId", "")) == container_id)
    )


def _identity_matches(identity: object, db_unique_name: str, container_id: str | None) -> bool:
    return (
        isinstance(identity, Mapping)
        and str(identity.get("db_unique_name", "")).upper() == db_unique_name.upper()
        and (container_id is None or identity.get("container_id") is None or str(identity.get("container_id")) == container_id)
    )


def _ledger_selection(index: Mapping, db_unique_name: str, container_id: str | None) -> dict[str, list[dict]]:
    attempts = [
        row for row in index.get("attempts", ())
        if row.get("environment") == ENVIRONMENT and _scope_matches(row.get("targetScope"), db_unique_name, container_id)
    ]
    verifications = [
        row for row in index.get("checkpointVerifications", ())
        if row.get("environment") == ENVIRONMENT and _scope_matches(row.get("targetScope"), db_unique_name, container_id)
    ]
    rollouts = [
        row for row in index.get("rollouts", ())
        if row.get("environment") == ENVIRONMENT
        and any(_scope_matches(scope, db_unique_name, container_id) for scope in row.get("targetScopes", ()))
    ]
    attempt_ids = {row["attemptId"] for row in attempts}
    verification_ids = {row["verificationId"] for row in verifications}
    links = [
        row for row in index.get("recoveryLinks", ())
        if row.get("oldAttemptId") in attempt_ids or row.get("verificationId") in verification_ids
    ]
    return {"attempts": attempts, "checkpointVerifications": verifications, "rollouts": rollouts, "recoveryLinks": links}


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _file_selection(root: Path, db_unique_name: str, container_id: str | None) -> list[str]:
    selected: list[Path] = []
    scratch = root / "scratch"
    selected.extend(path for path in sorted(scratch.glob("rollout-receipts/*/staging")) if path.is_dir())
    for run in sorted(scratch.glob("rollout-runs/*")):
        document = _read_json(run / "rollout-confirmation.json")
        if run.is_dir() and isinstance(document, Mapping) and document.get("environment") == ENVIRONMENT:
            selected.append(run)
    for attempt in sorted(scratch.glob("migration-attempt-*")):
        document = _read_json(attempt / "run-manifest.json")
        target = document.get("target") if isinstance(document, Mapping) else None
        if (
            attempt.is_dir() and isinstance(target, Mapping)
            and target.get("environment", ENVIRONMENT) == ENVIRONMENT
            and _identity_matches(target, db_unique_name, container_id)
        ):
            selected.append(attempt)
    for receipt in sorted((root / "migrations").glob("**/status.staging.json")):
        document = _read_json(receipt)
        if isinstance(document, Mapping) and _identity_matches(document.get("target"), db_unique_name, container_id):
            selected.append(receipt)
    for event in sorted((root / "migrations").glob("**/attempt-event.*.json")):
        document = _read_json(event)
        if (
            isinstance(document, Mapping) and document.get("environment") == ENVIRONMENT
            and _identity_matches(document.get("identity"), db_unique_name, container_id)
        ):
            selected.append(event)
    return [path.relative_to(root).as_posix() for path in selected]


def _tracked(root: Path, paths: Sequence[str]) -> tuple[str, ...]:
    if not paths or not (root / ".git").exists():
        return ()
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", *paths], cwd=root, capture_output=True, check=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    names = {name for name in result.stdout.decode("utf-8", "replace").split("\0") if name}
    return tuple(path for path in paths if path in names or any(name.startswith(path + "/") for name in names))


def _ledger_sha256(root: Path) -> str | None:
    path = root / "releases" / ".local-state.json"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _scopes(root: Path, selection: Mapping[str, list[dict]], db_unique_name: str, container_id: str | None) -> tuple:
    scopes = {
        tuple(sorted(row["targetScope"].items())) for key in ("attempts", "checkpointVerifications")
        for row in selection[key]
    }
    scopes.update(
        tuple(sorted(scope.items())) for row in selection["rollouts"] for scope in row["targetScopes"]
        if _scope_matches(scope, db_unique_name, container_id)
    )
    for lock in sorted((root / "releases").glob(".target-writer-*.lock")):
        document = _read_json(lock)
        scope = document.get("targetScope") if isinstance(document, Mapping) else None
        if _scope_matches(scope, db_unique_name, container_id):
            scopes.add(tuple(sorted(scope.items())))
    return tuple(sorted(scopes))


def plan_reset(root: Path, identity: Mapping[str, str]) -> ResetPlan:
    """What a reset of the restored target would move (offline, given the observed identity)."""
    db_unique_name = str(identity.get("db_unique_name") or "")
    if not db_unique_name:
        raise RehearsalResetError("the observed target identity has no db_unique_name")
    container = identity.get("container_id")
    container_id = str(container) if container not in (None, "") else None
    try:
        index = release_state._load_index(root)
    except release_state.ReleaseStateError as error:
        raise RehearsalResetError(f"local release state cannot be read: {error}") from error
    selection = _ledger_selection(index, db_unique_name, container_id)
    paths = _file_selection(root, db_unique_name, container_id)
    return ResetPlan(
        db_unique_name=db_unique_name,
        container_id=container_id,
        attempts=tuple(row["attemptId"] for row in selection["attempts"]),
        verifications=tuple(row["verificationId"] for row in selection["checkpointVerifications"]),
        rollouts=tuple(row["runId"] for row in selection["rollouts"]),
        recovery_links=len(selection["recoveryLinks"]),
        lost_proofs=tuple(sorted({row["payloadDigest"] for row in selection["rollouts"] if row.get("state") == "verified"})),
        paths=tuple(paths),
        tracked=_tracked(root, paths),
        scopes=_scopes(root, selection, db_unique_name, container_id),
        ledger_sha256=_ledger_sha256(root),
    )


# ---- apply -----------------------------------------------------------------------------------------------

def _archive_directory(root: Path) -> Path:
    base = root.joinpath(*ARCHIVE_PARTS)
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = base / f"{stamp}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(mode=0o700)
    return directory


def _remove_ledger_records(root: Path, plan: ResetPlan) -> None:
    def update(index: dict) -> dict:
        if _ledger_sha256(root) != plan.ledger_sha256:
            raise RehearsalResetError("local release state changed since it was listed; run the listing again")
        selection = _ledger_selection(index, plan.db_unique_name, plan.container_id)
        updated = dict(index)
        for section in LEDGER_SECTIONS:
            chosen = {id(row) for row in selection[section]}
            updated[section] = [row for row in index[section] if id(row) not in chosen]
        return updated

    try:
        release_state._atomic_update(root, update)
    except release_state.ReleaseStateError as error:
        raise RehearsalResetError(f"local release state was not changed: {error}") from error


def apply_reset(root: Path, plan: ResetPlan, identity: Mapping[str, str], *, confirmed_by: str) -> Path:
    """Move the plan's records into a new archive folder; refuse if anything changed or a writer is active."""
    with contextlib.ExitStack() as stack:
        for scope in plan.scopes:
            try:
                stack.enter_context(release_ownership.target_writer_lease(
                    root, dict(scope), {"purpose": "rehearsal-reset", "dbUniqueName": plan.db_unique_name},
                ))
            except release_ownership.TargetWriterBusy as error:
                raise RehearsalResetError(
                    f"a rollout or migration is running on {plan.db_unique_name}; nothing was moved"
                ) from error
            except release_ownership.ReleaseOwnershipError as error:
                raise RehearsalResetError(f"the target writer lease is unavailable: {error}; nothing was moved") from error
        fresh = plan_reset(root, identity)
        if fresh != plan:
            raise RehearsalResetError("local release state changed since it was listed; run the listing again")
        archive = _archive_directory(root)
        ledger = root / "releases" / ".local-state.json"
        if plan.ledger_sha256 is not None:
            shutil.copy2(ledger, archive / "local-state.json")
        if plan.attempts or plan.verifications or plan.rollouts or plan.recovery_links:
            _remove_ledger_records(root, plan)
        moved: list[str] = []
        record = {
            "schemaVersion": 1,
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "environment": ENVIRONMENT,
            "target": {key: identity.get(key) for key in sorted(identity)},
            "confirmedBy": confirmed_by,
            "originalLedgerSha256": plan.ledger_sha256,
            "ledger": {
                "attempts": list(plan.attempts), "checkpointVerifications": list(plan.verifications),
                "rollouts": list(plan.rollouts), "recoveryLinks": plan.recovery_links,
                "lostStagingProofs": list(plan.lost_proofs),
            },
            "trackedPaths": list(plan.tracked),
            "movedPaths": moved,
            "complete": False,
        }
        try:
            for relative in plan.paths:
                destination = archive / "moved" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(root / relative), str(destination))
                moved.append(relative)
            record["complete"] = True
        finally:
            record["movedPaths"] = sorted(moved)
            (archive / "reset-record.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return archive


# ---- command line ----------------------------------------------------------------------------------------

def _default_capture(repo_root: Path, env_file: Path | None) -> Callable[[], Mapping[str, str]]:
    def capture() -> Mapping[str, str]:
        from scripts import schema_catalog

        values = read_project_env(env_file or Path(os.environ.get("PROJECT_ENV_FILE") or repo_root / ".env"))
        schemas = split_list(values.get("STAGING_SCHEMA"))
        if not schemas:
            raise RehearsalResetError("STAGING_SCHEMA is not configured")
        target = resolve_target(values, ENVIRONMENT, "read", schema=schemas[0])
        run_dir = Path(tempfile.mkdtemp(prefix="identity-", dir=str(repo_root.joinpath(*ARCHIVE_PARTS))))
        return schema_catalog.capture_identity(target, run_dir)

    return capture


def _print_plan(plan: ResetPlan) -> None:
    container = f", container {plan.container_id}" if plan.container_id else ""
    print(f"Restored staging target: db_unique_name {plan.db_unique_name}{container}")
    if plan.is_empty():
        print("Nothing to archive: no local release state belongs to this target.")
        return
    print(f"Ledger records: {len(plan.rollouts)} run(s), {len(plan.attempts)} attempt(s), "
          f"{len(plan.verifications)} checkpoint verification(s), {plan.recovery_links} recovery link(s)")
    for path in plan.paths:
        print(f"  move {path}")
    if plan.tracked:
        print("These moved paths are tracked by Git and will show as deleted; commit or restore them:")
        for path in plan.tracked:
            print(f"  {path}")
    if plan.lost_proofs:
        print("Staging proof is removed for these capsule payloads; production needs a new staging run of them:")
        for digest in plan.lost_proofs:
            print(f"  {digest}")


def _confirm(yes: bool, confirmed: str) -> bool:
    if yes:
        print(terminal_confirm.confirmed_line(True, confirmed))
        return True
    if not terminal_confirm.may_read_yes_no():
        return False
    try:
        answer = input(f"Archive this local state of staging {confirmed}? [y/N]: ")
    except (EOFError, OSError):
        return False
    return answer.strip().casefold() in {"y", "yes"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM, description=__doc__.splitlines()[0])
    parser.add_argument("--env", required=True, help="staging (the only environment this command accepts)")
    parser.add_argument("--apply", action="store_true", help="move the listed records into the archive")
    parser.add_argument("--confirm-target", metavar="DB_UNIQUE_NAME", help="required with --apply: the observed db_unique_name")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the [y/N] prompt (with --apply and --confirm-target)")
    parser.add_argument("--repo-root", default=".", help=argparse.SUPPRESS)
    parser.add_argument("--env-file", type=Path, default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None, *, capture: Callable[[], Mapping[str, str]] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    repo_root = Path(args.repo_root).resolve()
    try:
        if args.env != ENVIRONMENT:
            raise RehearsalResetError("rehearsal-reset is for staging only; production and DEV state are never reset")
        if (args.confirm_target is not None or args.yes) and not args.apply:
            raise RehearsalResetError("--confirm-target and --yes apply only to --apply")
        if args.apply and not args.confirm_target:
            raise RehearsalResetError("--apply needs --confirm-target <db_unique_name>; nothing was moved")
        repo_root.joinpath(*ARCHIVE_PARTS).mkdir(parents=True, exist_ok=True)
        identity = (capture or _default_capture(repo_root, args.env_file))()
        observed = str(identity.get("db_unique_name") or "")
        if looks_like_production_identity(observed, str(identity.get("db_name") or ""), str(identity.get("service_name") or "")):
            raise RehearsalResetError(f"target {observed!r} looks like production; rehearsal-reset refuses it")
        plan = plan_reset(repo_root, identity)
        _print_plan(plan)
        if not args.apply:
            if not plan.is_empty():
                print(f"Run `{PROGRAM} --env staging --apply --confirm-target {observed}` to archive it.")
            return 0
        if args.confirm_target != observed:
            raise RehearsalResetError(
                f"--confirm-target {args.confirm_target!r} does not match the observed db_unique_name {observed!r}; "
                "nothing was moved"
            )
        if plan.is_empty():
            return 0
        if not _confirm(args.yes, observed):
            print("Rehearsal reset declined; nothing was moved.")
            return 1
        archive = apply_reset(repo_root, plan, identity, confirmed_by=terminal_confirm.confirmed_by(args.yes, args.confirm_target))
        print(f"Archived into {archive.relative_to(repo_root).as_posix()} (reset-record.json lists what moved).")
        return 0
    except (RehearsalResetError, ConfigError, release_state.ReleaseStateError, OSError, ValueError) as error:
        print(f"team error: {error}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
