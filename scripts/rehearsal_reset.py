"""scripts/team.sh rehearsal-reset: archive the local release state of a staging database that was restored.

Restoring staging from a snapshot or a production copy puts the database back in time, but the local ledger
(releases/.local-state.json), rollout receipts, migration receipts and attempt records still describe the writes
made before the restore. The next rollout or migrate then refuses: an unknown run still owns the target, a step
receipt path is occupied, a migration already has a verified receipt, or a prior attempt has no receipt.

Use --initialize-proof once for an operator-confirmed clean staging baseline. Before an external staging restore,
run --begin-restore with a stable operation UUID. After the restore, run --apply with that same UUID. The first
step blocks shared promotion proof; the final step archives matching evidence and advances the shared proof
generation. A durable local plan resumes the same archive after interruption. All modes read the target through
one guarded identity capture, require database and container identity, refuse production, and require
--confirm-target plus a terminal acknowledgment or --yes for shared state changes.

Reset selects legacy evidence by recorded db_unique_name and container_id. Capsule evidence without those fields
must have validated links to a local release-state rollout. Ambiguous evidence is preserved and listed. A
multi-target rollout remains intact and blocks reset until the whole operation can be handled safely. Staging
rollouts and migrations share a checkout-local coordination claim with reset; it does not coordinate separate
checkouts, so promotion proof uses the trusted shared registry.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from scripts import release_ownership, release_state, terminal_confirm
from scripts.db_targets import looks_like_production_identity, resolve_target, split_list
from scripts.local_config import ConfigError, read_project_env

PROGRAM = "scripts/team.sh rehearsal-reset"
ARCHIVE_PARTS = ("scratch", "rehearsal-reset")
ENVIRONMENT = "staging"
EXIT_ERROR = 2
LEDGER_SECTIONS = ("attempts", "checkpointVerifications", "rollouts", "recoveryLinks")
RESET_PLAN_NAME = "reset-plan.json"
RESET_RECORD_NAME = "reset-record.json"
PROOF_REGISTRY_ENV = "APEX_RELEASE_PROOF_REGISTRY"


class RehearsalResetError(RuntimeError):
    """The reset was refused; nothing was moved."""


@dataclass(frozen=True)
class ResetBlocker:
    run_id: str
    target_scopes: tuple[str, ...]
    retained_records: tuple[str, ...]


@dataclass(frozen=True)
class ResetPlan:
    db_unique_name: str
    container_id: str
    identity: tuple[tuple[str, str], ...]
    attempts: tuple[str, ...]
    verifications: tuple[str, ...]
    rollouts: tuple[str, ...]
    recovery_links: int
    lost_proofs: tuple[str, ...]
    paths: tuple[str, ...]
    file_digests: tuple[tuple[str, str], ...]
    tracked: tuple[str, ...]
    scopes: tuple[tuple[tuple[str, str], ...], ...]
    ledger_sha256: str | None
    blockers: tuple[ResetBlocker, ...] = ()
    retained: tuple[tuple[str, str], ...] = ()
    pending_archive: str | None = None
    operation_id: str | None = None

    def is_empty(self) -> bool:
        return not (self.attempts or self.verifications or self.rollouts or self.recovery_links or self.paths)


# ---- selection -------------------------------------------------------------------------------------------

def _scope_matches(scope: object, db_unique_name: str, container_id: str | None) -> bool:
    return (
        isinstance(scope, Mapping)
        and isinstance(scope.get("dbUniqueName"), str)
        and bool(scope.get("dbUniqueName"))
        and scope["dbUniqueName"] == scope["dbUniqueName"].strip()
        and scope["dbUniqueName"].upper() == db_unique_name.upper()
        and container_id is not None
        and isinstance(scope.get("containerId"), str)
        and bool(scope.get("containerId"))
        and scope["containerId"] == scope["containerId"].strip()
        and scope["containerId"] == container_id
    )


def _identity_matches(identity: object, db_unique_name: str, container_id: str | None) -> bool:
    return (
        isinstance(identity, Mapping)
        and isinstance(identity.get("db_unique_name"), str)
        and bool(identity.get("db_unique_name"))
        and identity["db_unique_name"] == identity["db_unique_name"].strip()
        and identity["db_unique_name"].upper() == db_unique_name.upper()
        and container_id is not None
        and isinstance(identity.get("container_id"), str)
        and bool(identity.get("container_id"))
        and identity["container_id"] == identity["container_id"].strip()
        and identity["container_id"] == container_id
    )


def _ledger_selection(index: Mapping, db_unique_name: str, container_id: str | None) -> dict[str, list[dict]]:
    rollouts: list[dict] = []
    blocked_run_ids: set[str] = set()
    blocked_run_rows: dict[str, Mapping] = {}
    for row in index.get("rollouts", ()):
        if row.get("environment") != ENVIRONMENT:
            continue
        scopes = list(row.get("targetScopes", ()))
        matching = [scope for scope in scopes if _scope_matches(scope, db_unique_name, container_id)]
        if matching and len(matching) != len(scopes):
            run_id = str(row.get("runId", "unknown"))
            blocked_run_ids.add(run_id)
            blocked_run_rows[run_id] = row
        elif matching:
            rollouts.append(row)
    blocked_attempt_ids: set[str] = set()
    blocked_verifications = [
        row for row in index.get("checkpointVerifications", ()) if row.get("runId") in blocked_run_ids
    ]
    blocked_attempt_ids.update(
        row["attemptId"] for row in blocked_verifications if row.get("attemptId") is not None
    )
    for run in blocked_run_rows.values():
        for attempt in index.get("attempts", ()):
            if (
                attempt.get("environment") == ENVIRONMENT
                and attempt.get("planDigest") == run.get("planDigest")
                and attempt.get("payloadDigest") == run.get("payloadDigest")
                and attempt.get("engineDigest") == run.get("engineDigest")
                and _scope_matches(attempt.get("targetScope"), db_unique_name, container_id)
            ):
                blocked_attempt_ids.add(attempt["attemptId"])
    attempts_by_id = {row["attemptId"]: row for row in index.get("attempts", ())}
    changed = True
    while changed:
        changed = False
        for attempt in index.get("attempts", ()):
            identifier = attempt["attemptId"]
            refs = {attempt.get("recoveryOf"), attempt.get("recoveredBy")} - {None}
            if identifier in blocked_attempt_ids or refs & blocked_attempt_ids:
                before = len(blocked_attempt_ids)
                blocked_attempt_ids.add(identifier)
                blocked_attempt_ids.update(refs & attempts_by_id.keys())
                changed = changed or len(blocked_attempt_ids) != before
    attempts = [
        row for row in index.get("attempts", ())
        if row.get("environment") == ENVIRONMENT
        and row["attemptId"] not in blocked_attempt_ids
        and _scope_matches(row.get("targetScope"), db_unique_name, container_id)
    ]
    verifications = [
        row for row in index.get("checkpointVerifications", ())
        if row.get("environment") == ENVIRONMENT
        and row.get("runId") not in blocked_run_ids
        and row.get("attemptId") not in blocked_attempt_ids
        and _scope_matches(row.get("targetScope"), db_unique_name, container_id)
    ]
    attempt_ids = {row["attemptId"] for row in attempts}
    verification_ids = {row["verificationId"] for row in verifications}
    links = [
        row for row in index.get("recoveryLinks", ())
        if row.get("oldAttemptId") in attempt_ids and row.get("verificationId") in verification_ids
    ]
    blocked_runs: list[ResetBlocker] = []
    for run_id, run in blocked_run_rows.items():
        retained_records = [f"rollout:{run_id}"]
        retained_records.extend(f"attempt:{item}" for item in sorted(blocked_attempt_ids))
        retained_records.extend(f"verification:{row['verificationId']}" for row in blocked_verifications)
        for link in index.get("recoveryLinks", ()):
            if link.get("oldAttemptId") in blocked_attempt_ids or link.get("verificationId") in {
                row["verificationId"] for row in blocked_verifications
            }:
                retained_records.append(f"recovery-link:{link['oldAttemptId']}:{link['verificationId']}")
        blocked_runs.append(ResetBlocker(
            run_id=run_id,
            target_scopes=tuple(_format_scope(scope) for scope in run.get("targetScopes", ())),
            retained_records=tuple(sorted(set(retained_records))),
        ))
    return {
        "attempts": attempts, "checkpointVerifications": verifications,
        "rollouts": rollouts, "recoveryLinks": links, "blockers": blocked_runs,
    }


def _format_scope(scope: object) -> str:
    if not isinstance(scope, Mapping):
        return "unknown target scope"
    return (
        f"db_unique_name={scope.get('dbUniqueName', '?')}, container={scope.get('containerId', '?')}, "
        f"schema={scope.get('schema', '?')}"
    )


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _tree_digest(path: Path, root: Path) -> str:
    """Hash a file or directory tree while refusing symbolic links and reparse points."""
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RehearsalResetError(f"cannot inspect {path.relative_to(root).as_posix()}: {error}") from error
    if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise RehearsalResetError(f"refusing symbolic link or reparse point: {path.relative_to(root).as_posix()}")
    if path.is_file():
        return hashlib.sha256(path.read_bytes()).hexdigest()
    if not path.is_dir():
        raise RehearsalResetError(f"reset path is neither a file nor directory: {path.relative_to(root).as_posix()}")
    rows: list[tuple[str, str]] = []
    for child in sorted(path.rglob("*")):
        child_metadata = child.lstat()
        if child.is_symlink() or getattr(child_metadata, "st_file_attributes", 0) & 0x400:
            raise RehearsalResetError(f"reset tree contains a symbolic link: {child.relative_to(root).as_posix()}")
        if child.is_file():
            rows.append((child.relative_to(path).as_posix(), hashlib.sha256(child.read_bytes()).hexdigest()))
        elif not child.is_dir():
            raise RehearsalResetError(f"reset tree contains a special file: {child.relative_to(root).as_posix()}")
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _direct_identity(document: object) -> Mapping | None:
    if not isinstance(document, Mapping):
        return None
    for key in ("observedIdentity", "identity", "target"):
        value = document.get(key)
        if isinstance(value, Mapping) and "db_unique_name" in value:
            return value
    if "db_unique_name" in document:
        return document
    return None


def _identities_in_tree(path: Path) -> tuple[Mapping, ...]:
    found: list[Mapping] = []
    for item in sorted(path.rglob("*.json")):
        document = _read_json(item)
        identity = _direct_identity(document)
        if identity is not None and identity not in found:
            found.append(identity)
    return tuple(found)


def _receipt_links(index: Mapping, root: Path) -> dict[str, list[dict]]:
    linked: dict[str, list[dict]] = {}
    for run in index.get("rollouts", ()):
        for step in run.get("steps", ()):
            relative = step.get("receiptPath") if isinstance(step, Mapping) else None
            if isinstance(relative, str):
                linked.setdefault(PurePosixPath(relative).as_posix(), []).append(run)
        report_path = run.get("finalReportPath")
        if not isinstance(report_path, str):
            continue
        report = _read_json(root.joinpath(*PurePosixPath(report_path).parts))
        if not isinstance(report, Mapping):
            continue
        evidence_dir = report.get("evidenceDir")
        if isinstance(evidence_dir, str):
            linked.setdefault(PurePosixPath(evidence_dir).as_posix(), []).append(run)
    return linked


def _add_selected(root: Path, selected: dict[str, str], retained: dict[str, str], path: Path) -> None:
    relative = path.relative_to(root).as_posix()
    try:
        selected[relative] = _tree_digest(path, root)
    except (OSError, RehearsalResetError) as error:
        retained[relative] = f"ownership or bytes could not be verified: {error}"


def _run_classification(
    run: Mapping | None,
    identities: Sequence[Mapping],
    db_unique_name: str,
    container_id: str,
) -> tuple[str, tuple[str, ...]]:
    scopes = list(run.get("targetScopes", ())) if isinstance(run, Mapping) else []
    if scopes:
        matched = [_scope_matches(scope, db_unique_name, container_id) for scope in scopes]
        rendered = tuple(_format_scope(scope) for scope in scopes)
        if all(matched):
            return "match", rendered
        if any(matched):
            return "mixed", rendered
        return "other", rendered
    if identities:
        valid = [
            isinstance(item.get("db_unique_name"), str) and bool(item.get("db_unique_name"))
            and item["db_unique_name"] == item["db_unique_name"].strip()
            and isinstance(item.get("container_id"), str) and bool(item.get("container_id"))
            and item["container_id"] == item["container_id"].strip()
            for item in identities
        ]
        matched = [_identity_matches(item, db_unique_name, container_id) for item in identities]
        rendered = tuple(sorted({
            f"db_unique_name={item.get('db_unique_name', '?')}, container={item.get('container_id', '?')}"
            for item in identities
        }))
        if not all(valid):
            return ("mixed" if any(matched) else "unknown"), (*rendered, "incomplete database/container identity")
        if all(matched):
            return "match", rendered
        if any(matched):
            return "mixed", rendered
        return "other", rendered
    return "unknown", ()


def _file_selection(
    root: Path,
    index: Mapping,
    db_unique_name: str,
    container_id: str,
) -> tuple[dict[str, str], dict[str, str], tuple[ResetBlocker, ...], tuple[Mapping, ...]]:
    selected: dict[str, str] = {}
    retained: dict[str, str] = {}
    blockers: list[ResetBlocker] = []
    observations: list[Mapping] = []
    scratch = root / "scratch"
    rollouts_by_id = {row.get("runId"): row for row in index.get("rollouts", ())}
    receipt_links = _receipt_links(index, root)

    for receipt in sorted((scratch / "rollout-receipts").glob("*/staging/**/*.json")):
        relative = receipt.relative_to(root).as_posix()
        document = _read_json(receipt)
        if not isinstance(document, Mapping):
            retained[relative] = "receipt is unreadable; target ownership is unknown"
            continue
        identity = _direct_identity(document)
        if identity is not None:
            if _identity_matches(identity, db_unique_name, container_id):
                observations.append(identity)
                _add_selected(root, selected, retained, receipt)
            elif not isinstance(identity.get("db_unique_name"), str) or not identity.get("db_unique_name") or not isinstance(identity.get("container_id"), str) or not identity.get("container_id"):
                retained[relative] = "recorded identity lacks a validated db_unique_name/container_id pair"
            else:
                retained[relative] = "recorded database or container differs from the restored target"
            continue
        direct_scopes = document.get("targetScopes")
        linked: Sequence[Mapping] = ()
        if isinstance(direct_scopes, list) and direct_scopes:
            classification, rendered = _run_classification({"targetScopes": direct_scopes}, (), db_unique_name, container_id)
        else:
            linked = receipt_links.get(relative, ())
            if len(linked) == 1:
                classification, rendered = _run_classification(linked[0], (), db_unique_name, container_id)
            elif len(linked) > 1:
                classification, rendered = "unknown", ()
            else:
                classification, rendered = "unknown", ()
        if classification == "match":
            _add_selected(root, selected, retained, receipt)
        elif classification == "mixed":
            run_id = str(linked[0].get("runId", "unknown")) if len(linked) == 1 else "unknown"
            blockers.append(ResetBlocker(run_id, rendered, (f"receipt:{relative}", f"rollout:{run_id}")))
            retained[relative] = f"receipt belongs to mixed-target rollout {run_id}; keep the complete run intact"
        elif classification == "other":
            retained[relative] = "validated rollout linkage belongs to another database or container"
        else:
            retained[relative] = "receipt has no validated database/container identity or ledger linkage"

    for run_dir in sorted(scratch.glob("rollout-runs/*")):
        if not run_dir.is_dir():
            continue
        relative = run_dir.relative_to(root).as_posix()
        confirmation = _read_json(run_dir / "rollout-confirmation.json")
        if not isinstance(confirmation, Mapping) or confirmation.get("environment") != ENVIRONMENT:
            continue
        run_id = confirmation.get("localRunId") or confirmation.get("runId")
        ledger_run = rollouts_by_id.get(run_id)
        identities = _identities_in_tree(run_dir)
        classification, rendered = _run_classification(ledger_run or confirmation, identities, db_unique_name, container_id)
        if classification == "match":
            _add_selected(root, selected, retained, run_dir)
            observations.extend(identities)
        elif classification == "mixed":
            run_label = str(run_id or run_dir.name)
            records = [f"rollout:{run_label}"]
            if ledger_run is not None:
                records.extend(
                    f"verification:{item['verificationId']}" for item in index.get("checkpointVerifications", ())
                    if item.get("runId") == run_id
                )
                records.extend(
                    f"attempt:{item['attemptId']}" for item in index.get("attempts", ())
                    if item.get("runId") == run_id
                )
            blockers.append(ResetBlocker(run_label, rendered, tuple(sorted(records))))
            retained[relative] = f"rollout run spans multiple targets; keep it intact: {', '.join(rendered)}"
        elif classification == "other":
            retained[relative] = "validated target identities belong to another database or container"
        else:
            retained[relative] = "run has no validated database/container identity or ledger linkage"

    for attempt in sorted(scratch.glob("migration-attempt-*")):
        document = _read_json(attempt / "run-manifest.json")
        target = document.get("target") if isinstance(document, Mapping) else None
        if not isinstance(document, Mapping) or document.get("environment", ENVIRONMENT) != ENVIRONMENT:
            continue
        relative = attempt.relative_to(root).as_posix()
        if _identity_matches(target, db_unique_name, container_id):
            observations.append(target)
            _add_selected(root, selected, retained, attempt)
        else:
            reason = "target has no complete database/container identity" if not isinstance(target, Mapping) or target.get("container_id") is None else "target belongs to another database or container"
            retained[relative] = reason

    for receipt in sorted((root / "migrations").glob("**/status.staging.json")):
        document = _read_json(receipt)
        target = document.get("target") if isinstance(document, Mapping) else None
        relative = receipt.relative_to(root).as_posix()
        if _identity_matches(target, db_unique_name, container_id):
            observations.append(target)
            _add_selected(root, selected, retained, receipt)
        elif isinstance(document, Mapping):
            retained[relative] = "receipt has no matching database/container identity"

    for event in sorted((root / "migrations").glob("**/attempt-event.*.json")):
        document = _read_json(event)
        identity = document.get("identity") if isinstance(document, Mapping) else None
        if not isinstance(document, Mapping) or document.get("environment") != ENVIRONMENT:
            continue
        relative = event.relative_to(root).as_posix()
        if _identity_matches(identity, db_unique_name, container_id):
            observations.append(identity)
            _add_selected(root, selected, retained, event)
        else:
            retained[relative] = "attempt event has no matching database/container identity"
    return selected, retained, tuple(blockers), tuple(observations)


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


def _scopes(
    root: Path,
    selection: Mapping[str, list[dict]],
    observations: Sequence[Mapping],
    db_unique_name: str,
    container_id: str,
) -> tuple:
    scopes = {
        tuple(sorted(row["targetScope"].items())) for key in ("attempts", "checkpointVerifications")
        for row in selection[key]
    }
    scopes.update(
        tuple(sorted(scope.items())) for row in selection["rollouts"] for scope in row["targetScopes"]
        if _scope_matches(scope, db_unique_name, container_id)
    )
    for identity in observations:
        if not _identity_matches(identity, db_unique_name, container_id):
            continue
        try:
            scope = release_state.target_scope_from_identity(identity)
        except (KeyError, release_state.ReleaseStateError):
            continue
        scopes.add(tuple(sorted(scope.items())))
    for lock in sorted((root / "releases").glob(".target-writer-*.lock")):
        document = _read_json(lock)
        scope = document.get("targetScope") if isinstance(document, Mapping) else None
        if _scope_matches(scope, db_unique_name, container_id):
            scopes.add(tuple(sorted(scope.items())))
    return tuple(sorted(scopes))


def plan_reset(root: Path, identity: Mapping[str, str]) -> ResetPlan:
    """What a reset of the restored target would move (offline, given the observed identity)."""
    db_unique_name = str(identity.get("db_unique_name") or "")
    if not db_unique_name or not db_unique_name.strip():
        raise RehearsalResetError("the observed target identity has no db_unique_name")
    container = identity.get("container_id")
    if container in (None, ""):
        raise RehearsalResetError("the observed target identity has no container_id; reset cannot safely choose a database container")
    container_id = str(container)
    pending = _pending_reset_archive(root, db_unique_name, container_id)
    if pending is not None:
        archive, document = pending
        return _plan_from_pending(root, archive, document)
    try:
        index = release_state._load_index(root)
    except release_state.ReleaseStateError as error:
        raise RehearsalResetError(f"local release state cannot be read: {error}") from error
    selection = _ledger_selection(index, db_unique_name, container_id)
    selected, retained, file_blockers, observations = _file_selection(root, index, db_unique_name, container_id)
    try:
        file_digests = tuple(sorted(selected.items()))
        paths = tuple(path for path, _ in file_digests)
        candidate = dict(index)
        for section in LEDGER_SECTIONS:
            chosen = {id(row) for row in selection[section]}
            candidate[section] = [row for row in index[section] if id(row) not in chosen]
        release_state._validate_index(candidate)
        ledger_blockers: tuple[ResetBlocker, ...] = ()
    except release_state.ReleaseStateError as error:
        paths = tuple(path for path, _ in sorted(selected.items()))
        file_digests = tuple(sorted(selected.items()))
        ledger_blockers = (ResetBlocker(
            run_id="ledger-dependencies",
            target_scopes=(f"db_unique_name={db_unique_name}, container={container_id}",),
            retained_records=(f"local-state: {error}",),
        ),)
    except (TypeError, KeyError) as error:
        raise RehearsalResetError(f"local release state selection is malformed: {error}") from error
    target_identity = tuple(sorted((str(key), str(value)) for key, value in identity.items() if value is not None))
    return ResetPlan(
        db_unique_name=db_unique_name,
        container_id=container_id,
        identity=target_identity,
        attempts=tuple(row["attemptId"] for row in selection["attempts"]),
        verifications=tuple(row["verificationId"] for row in selection["checkpointVerifications"]),
        rollouts=tuple(row["runId"] for row in selection["rollouts"]),
        recovery_links=len(selection["recoveryLinks"]),
        lost_proofs=tuple(sorted({row["payloadDigest"] for row in selection["rollouts"] if row.get("state") == "verified"})),
        paths=tuple(paths),
        file_digests=tuple(file_digests),
        tracked=_tracked(root, paths),
        scopes=_scopes(root, selection, observations, db_unique_name, container_id),
        ledger_sha256=_ledger_sha256(root),
        blockers=tuple(selection["blockers"]) + tuple(file_blockers) + ledger_blockers,
        retained=tuple(sorted(retained.items())),
    )


# ---- apply -----------------------------------------------------------------------------------------------

def _archive_directory(root: Path) -> Path:
    base = root.joinpath(*ARCHIVE_PARTS)
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = base / f"{stamp}-{uuid.uuid4().hex[:8]}"
    directory.mkdir(mode=0o700)
    _fsync_directory(base)
    return directory


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _write_atomic_bytes(path: Path, raw: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = -1
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        _fsync_directory(path.parent)
    except OSError as error:
        raise RehearsalResetError(f"could not durably write {path}: {error}") from error
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _write_atomic_json(path: Path, document: Mapping) -> None:
    raw = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    _write_atomic_bytes(path, raw)


def _serialize_plan(plan: ResetPlan) -> dict:
    return {
        "dbUniqueName": plan.db_unique_name,
        "containerId": plan.container_id,
        "identity": dict(plan.identity),
        "attempts": list(plan.attempts),
        "verifications": list(plan.verifications),
        "rollouts": list(plan.rollouts),
        "recoveryLinks": plan.recovery_links,
        "lostProofs": list(plan.lost_proofs),
        "paths": list(plan.paths),
        "fileDigests": {path: digest for path, digest in plan.file_digests},
        "tracked": list(plan.tracked),
        "scopes": [dict(scope) for scope in plan.scopes],
        "ledgerSha256": plan.ledger_sha256,
        "blockers": [
            {"runId": blocker.run_id, "targetScopes": list(blocker.target_scopes), "retainedRecords": list(blocker.retained_records)}
            for blocker in plan.blockers
        ],
        "retained": [{"path": path, "reason": reason} for path, reason in plan.retained],
    }


def _deserialize_plan(value: object) -> ResetPlan:
    if not isinstance(value, Mapping):
        raise RehearsalResetError("saved reset plan is malformed")
    identity = value.get("identity")
    digests = value.get("fileDigests")
    scopes = value.get("scopes")
    blockers = value.get("blockers", ())
    retained = value.get("retained", ())
    if not isinstance(identity, Mapping) or not isinstance(digests, Mapping) or not isinstance(scopes, list):
        raise RehearsalResetError("saved reset plan is missing identity, file hashes, or writer scopes")
    if not isinstance(blockers, list) or not isinstance(retained, list):
        raise RehearsalResetError("saved reset plan has malformed blockers or retained evidence")
    try:
        return ResetPlan(
            db_unique_name=str(value["dbUniqueName"]),
            container_id=str(value["containerId"]),
            identity=tuple(sorted((str(key), str(item)) for key, item in identity.items())),
            attempts=tuple(str(item) for item in value["attempts"]),
            verifications=tuple(str(item) for item in value["verifications"]),
            rollouts=tuple(str(item) for item in value["rollouts"]),
            recovery_links=int(value["recoveryLinks"]),
            lost_proofs=tuple(str(item) for item in value["lostProofs"]),
            paths=tuple(str(item) for item in value["paths"]),
            file_digests=tuple(sorted((str(path), str(digest)) for path, digest in digests.items())),
            tracked=tuple(str(item) for item in value["tracked"]),
            scopes=tuple(tuple(sorted(dict(scope).items())) for scope in scopes),
            ledger_sha256=str(value["ledgerSha256"]) if value["ledgerSha256"] is not None else None,
            blockers=tuple(ResetBlocker(
                str(item["runId"]), tuple(str(scope) for scope in item["targetScopes"]),
                tuple(str(record) for record in item["retainedRecords"]),
            ) for item in blockers),
            retained=tuple(sorted((str(item["path"]), str(item["reason"])) for item in retained)),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RehearsalResetError(f"saved reset plan is malformed: {error}") from error


def _pending_reset_archive(root: Path, db_unique_name: str, container_id: str) -> tuple[Path, Mapping] | None:
    base = root.joinpath(*ARCHIVE_PARTS)
    if not base.exists():
        return None
    if base.is_symlink():
        raise RehearsalResetError("rehearsal-reset archive root is a symbolic link")
    for archive in sorted(base.iterdir()):
        if archive.name.startswith(".restore-") or archive.is_symlink() or not archive.is_dir():
            continue
        path = archive / RESET_PLAN_NAME
        if not path.exists():
            old_record = archive / RESET_RECORD_NAME
            if old_record.is_file():
                old = _read_json(old_record)
                if isinstance(old, Mapping) and old.get("complete") is False:
                    raise RehearsalResetError(
                        f"legacy reset archive {archive.relative_to(root).as_posix()} is incomplete and lacks a saved plan; "
                        "preserve it and reconcile manually before another reset"
                    )
            continue
        document = _read_json(path)
        if not isinstance(document, Mapping):
            raise RehearsalResetError(f"saved reset plan is unreadable at {path}; preserve it and reconcile manually")
        if document.get("state") == "complete":
            continue
        target = document.get("target")
        if not isinstance(target, Mapping):
            raise RehearsalResetError(f"saved reset plan has no target at {path}; preserve it and reconcile manually")
        same_target = (
            str(target.get("db_unique_name", "")).upper() == db_unique_name.upper()
            and str(target.get("container_id", "")) == container_id
        )
        if not same_target:
            raise RehearsalResetError(
                f"pending reset for another database/container blocks this operation: "
                f"{archive.relative_to(root).as_posix()}; resume or reconcile that exact archive first"
            )
        return archive, document
    return None


def _completed_archive(root: Path, operation_id: str, db_unique_name: str, container_id: str) -> Path | None:
    base = root.joinpath(*ARCHIVE_PARTS)
    if not base.is_dir() or base.is_symlink():
        return None
    matches: list[Path] = []
    for archive in sorted(base.iterdir()):
        if archive.is_symlink() or not archive.is_dir():
            continue
        document = _read_json(archive / RESET_PLAN_NAME)
        target = document.get("target") if isinstance(document, Mapping) else None
        if (
            isinstance(document, Mapping) and document.get("state") == "complete"
            and document.get("operationId") == operation_id and isinstance(target, Mapping)
            and str(target.get("db_unique_name", "")).upper() == db_unique_name.upper()
            and str(target.get("container_id", "")) == container_id
        ):
            matches.append(archive)
    if len(matches) > 1:
        raise RehearsalResetError(
            f"operation {operation_id} has more than one completed local archive; inspect {matches[0].parent} and reconcile"
        )
    return matches[0] if matches else None


def _plan_from_pending(root: Path, archive: Path, document: Mapping) -> ResetPlan:
    plan = _deserialize_plan(document.get("resetPlan"))
    operation_id = document.get("operationId")
    if not isinstance(operation_id, str):
        raise RehearsalResetError(f"pending reset at {archive} has no operation ID; preserve it and reconcile manually")
    return ResetPlan(**{
        **plan.__dict__,
        "pending_archive": archive.relative_to(root).as_posix(),
        "operation_id": operation_id,
    })


def _build_post_index(root: Path, plan: ResetPlan) -> dict | None:
    ledger = root / "releases" / ".local-state.json"
    if plan.ledger_sha256 is None:
        return None
    try:
        if hashlib.sha256(ledger.read_bytes()).hexdigest() != plan.ledger_sha256:
            raise RehearsalResetError("local release state changed since it was listed; run the listing again")
        if not (plan.attempts or plan.verifications or plan.rollouts or plan.recovery_links):
            return None
        index = release_state._load_index(root)
    except (OSError, release_state.ReleaseStateError) as error:
        raise RehearsalResetError(f"local release state cannot be verified: {error}") from error
    selection = _ledger_selection(index, plan.db_unique_name, plan.container_id)
    if (
        tuple(row["attemptId"] for row in selection["attempts"]) != plan.attempts
        or tuple(row["verificationId"] for row in selection["checkpointVerifications"]) != plan.verifications
        or tuple(row["runId"] for row in selection["rollouts"]) != plan.rollouts
        or len(selection["recoveryLinks"]) != plan.recovery_links
    ):
        raise RehearsalResetError("saved ledger selection no longer matches the reset plan")
    updated = dict(index)
    for section in LEDGER_SECTIONS:
        chosen = {id(row) for row in selection[section]}
        updated[section] = [row for row in index[section] if id(row) not in chosen]
    return release_state._validate_index(updated)


def _atomic_move(root: Path, archive: Path, relative: str, expected_digest: str) -> None:
    relative_path = PurePosixPath(relative)
    if relative_path.is_absolute() or not relative_path.parts or any(part in {".", ".."} for part in relative_path.parts):
        raise RehearsalResetError(f"saved reset path is unsafe: {relative}")
    source = root.joinpath(*relative_path.parts)
    destination = archive / "moved" / Path(*relative_path.parts)
    for candidate in (source, destination):
        current = root if candidate == source else archive
        try:
            candidate.relative_to(current)
            components = candidate.relative_to(current).parts
            cursor = current
            for part in components:
                cursor = cursor / part
                if cursor.exists() or cursor.is_symlink():
                    metadata = cursor.lstat()
                    if cursor.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
                        raise RehearsalResetError(f"reset path contains a symbolic link: {cursor}")
        except ValueError as error:
            raise RehearsalResetError(f"saved reset path escapes its root: {relative}") from error
    source_exists = source.exists() or source.is_symlink()
    destination_exists = destination.exists() or destination.is_symlink()
    if destination_exists:
        actual = _tree_digest(destination, archive)
        if actual != expected_digest or source_exists:
            raise RehearsalResetError(
                f"reset archive bytes differ from the saved plan for {relative}; inspect {archive} and reconcile manually"
            )
        return
    if not source_exists:
        raise RehearsalResetError(
            f"both source and archive bytes are missing for {relative}; inspect {archive} and reconcile manually"
        )
    actual = _tree_digest(source, root)
    if actual != expected_digest:
        raise RehearsalResetError(
            f"source bytes differ from the saved plan for {relative}; inspect {archive} and reconcile manually"
        )
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.rename(source, destination)
    except OSError as error:
        raise RehearsalResetError(f"could not archive {relative}: {error}; inspect {archive} and retry that same reset") from error
    _fsync_directory(source.parent)
    _fsync_directory(destination.parent)
    if _tree_digest(destination, archive) != expected_digest or source.exists():
        raise RehearsalResetError(f"archived bytes did not verify for {relative}; inspect {archive} and reconcile manually")


def _commit_ledger(root: Path, archive: Path, record: dict) -> None:
    before = record.get("ledgerBeforeSha256")
    after = record.get("ledgerAfterSha256")
    ledger = root / "releases" / ".local-state.json"
    snapshot = archive / "local-state.json"
    expected_after = archive / "local-state-after.json"
    if before is None:
        if ledger.exists():
            raise RehearsalResetError(f"release ledger appeared after planning; inspect {archive} and reconcile manually")
        record["ledgerState"] = "committed"
        return
    try:
        if hashlib.sha256(snapshot.read_bytes()).hexdigest() != before:
            raise RehearsalResetError(f"saved ledger snapshot differs from its plan at {archive}; reconcile manually")
        if hashlib.sha256(expected_after.read_bytes()).hexdigest() != after:
            raise RehearsalResetError(f"saved post-reset ledger differs from its plan at {archive}; reconcile manually")
        current = ledger.read_bytes()
        current_digest = hashlib.sha256(current).hexdigest()
    except OSError as error:
        raise RehearsalResetError(f"ledger bytes are unavailable while resuming {archive}: {error}; reconcile manually") from error
    if current_digest == after:
        record["ledgerState"] = "committed"
        return
    if current_digest != before:
        raise RehearsalResetError(
            f"remaining ledger bytes differ from the saved plan; inspect {archive} and reconcile before retrying"
        )
    if before != after:
        try:
            post_index = json.loads(expected_after.read_text(encoding="utf-8"))

            def update(_index: dict) -> dict:
                if _ledger_sha256(root) != before:
                    raise RehearsalResetError("local release state changed during reset ledger commit")
                return post_index

            release_state._atomic_update(root, update)
        except (OSError, UnicodeError, json.JSONDecodeError, release_state.ReleaseStateError) as error:
            raise RehearsalResetError(f"ledger transition failed; inspect {archive} and retry that same reset: {error}") from error
    try:
        if hashlib.sha256(ledger.read_bytes()).hexdigest() != after:
            raise RehearsalResetError(f"ledger did not match the planned transition; inspect {archive} and reconcile manually")
    except OSError as error:
        raise RehearsalResetError(f"cannot verify ledger transition in {archive}: {error}; reconcile manually") from error
    record["ledgerState"] = "committed"


def _verify_ledger_before_moves(root: Path, archive: Path, record: Mapping) -> None:
    before = record.get("ledgerBeforeSha256")
    after = record.get("ledgerAfterSha256")
    ledger = root / "releases" / ".local-state.json"
    if before is None:
        if ledger.exists():
            raise RehearsalResetError(f"release ledger appeared after planning; inspect {archive} and reconcile manually")
        return
    try:
        current_digest = hashlib.sha256(ledger.read_bytes()).hexdigest()
    except OSError as error:
        raise RehearsalResetError(f"release ledger cannot be read before archival: {error}; inspect {archive}") from error
    if current_digest == before:
        return
    if current_digest == after:
        for item in record.get("files", ()):
            relative = str(item.get("path", ""))
            source = root.joinpath(*PurePosixPath(relative).parts)
            destination = archive / "moved" / Path(*PurePosixPath(relative).parts)
            if source.exists() or source.is_symlink() or not destination.exists() or _tree_digest(destination, archive) != item.get("sha256"):
                raise RehearsalResetError(
                    f"ledger is already transitioned before all planned files are archived; inspect {archive} and reconcile manually"
                )
        return
    raise RehearsalResetError(
        f"release ledger differs from the saved plan before archival; inspect {archive} and reconcile manually"
    )


def _apply_saved_archive(root: Path, archive: Path, record: dict) -> Path:
    plan = _deserialize_plan(record.get("resetPlan"))
    if plan.blockers:
        raise RehearsalResetError("reset plan has mixed-target or linked-ledger blockers; no archive was changed")
    plan_path = archive / RESET_PLAN_NAME
    _verify_ledger_before_moves(root, archive, record)
    for item in record.get("files", ()):
        if not isinstance(item, Mapping):
            raise RehearsalResetError(f"saved file plan is malformed at {archive}; reconcile manually")
        relative, digest = str(item.get("path", "")), str(item.get("sha256", ""))
        _atomic_move(root, archive, relative, digest)
        item["state"] = "archived"
        record["movedPaths"] = sorted({*record.get("movedPaths", ()), relative})
        record["state"] = "moving"
        _write_atomic_json(plan_path, record)
    record["state"] = "files-archived"
    _write_atomic_json(plan_path, record)
    _commit_ledger(root, archive, record)
    record["state"] = "ledger-committed"
    _write_atomic_json(plan_path, record)
    _write_atomic_json(archive / RESET_RECORD_NAME, {
        "schemaVersion": 2,
        "createdAt": record["createdAt"],
        "environment": ENVIRONMENT,
        "target": record["target"],
        "operationId": record["operationId"],
        "confirmedBy": record["confirmedBy"],
        "originalLedgerSha256": record["ledgerBeforeSha256"],
        "ledger": {
            "attempts": list(plan.attempts), "checkpointVerifications": list(plan.verifications),
            "rollouts": list(plan.rollouts), "recoveryLinks": plan.recovery_links,
            "lostStagingProofs": list(plan.lost_proofs),
        },
        "trackedPaths": list(plan.tracked),
        "movedPaths": sorted(record.get("movedPaths", ())),
        "complete": True,
    })
    record["state"] = "complete"
    record["completedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    _write_atomic_json(plan_path, record)
    return archive


def _prepare_archive(root: Path, plan: ResetPlan, identity: Mapping[str, str], confirmed_by: str, operation_id: str) -> tuple[Path, dict]:
    archive = _archive_directory(root)
    ledger = root / "releases" / ".local-state.json"
    ledger_raw: bytes | None = None
    if plan.ledger_sha256 is not None:
        try:
            ledger_raw = ledger.read_bytes()
        except OSError as error:
            raise RehearsalResetError(f"cannot save ledger snapshot before archival: {error}; no paths were moved") from error
        if hashlib.sha256(ledger_raw).hexdigest() != plan.ledger_sha256:
            raise RehearsalResetError("ledger changed before reset archive planning; list again; no paths were moved")
        _write_atomic_bytes(archive / "local-state.json", ledger_raw)
    post_index = _build_post_index(root, plan)
    if ledger_raw is None:
        ledger_after = None
    elif post_index is None:
        ledger_after = ledger_raw
    else:
        ledger_after = release_state._canonical_bytes(post_index) + b"\n"
    if ledger_after is not None:
        _write_atomic_bytes(archive / "local-state-after.json", ledger_after)
    file_items = [{"path": path, "sha256": digest, "state": "pending"} for path, digest in plan.file_digests]
    record = {
        "schemaVersion": 2,
        "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "environment": ENVIRONMENT,
        "operationId": operation_id,
        "target": {key: identity.get(key) for key in sorted(identity)},
        "confirmedBy": confirmed_by,
        "resetPlan": _serialize_plan(plan),
        "ledgerBeforeSha256": plan.ledger_sha256,
        "ledgerAfterSha256": hashlib.sha256(ledger_after).hexdigest() if ledger_after is not None else None,
        "ledgerState": "pending",
        "files": file_items,
        "movedPaths": [],
        "state": "moving",
    }
    _write_atomic_json(archive / RESET_PLAN_NAME, record)
    return archive, record


def _scope_from_saved(scope: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return dict(scope)


def apply_reset(
    root: Path,
    plan: ResetPlan,
    identity: Mapping[str, str],
    *,
    confirmed_by: str,
    operation_id: str,
) -> Path:
    """Resume or create one durable target archive while holding checkout and target claims."""
    if not operation_id:
        raise RehearsalResetError("--apply requires the operation ID created by --begin-restore")
    if plan.blockers:
        raise RehearsalResetError("reset is blocked by mixed-target or linked-ledger evidence; nothing was moved")
    try:
        with release_ownership.staging_coordination_lease(
            root,
            {"purpose": "rehearsal-reset", "operationId": operation_id, "dbUniqueName": plan.db_unique_name},
            allow_pending_reset=True,
        ):
            if plan.pending_archive is not None:
                archive = root / plan.pending_archive
                record = _read_json(archive / RESET_PLAN_NAME)
                if not isinstance(record, dict) or record.get("operationId") != operation_id:
                    raise RehearsalResetError(
                        f"pending archive {plan.pending_archive} requires its exact saved operation ID; preserve it and reconcile"
                    )
            else:
                fresh = plan_reset(root, identity)
                if fresh != plan:
                    raise RehearsalResetError("local release state changed since it was listed; run the listing again")
                archive = _completed_archive(root, operation_id, plan.db_unique_name, plan.container_id)
                if archive is not None:
                    return archive
                archive, record = _prepare_archive(root, plan, identity, confirmed_by, operation_id)
            if plan.pending_archive is not None and record.get("state") == "complete":
                return archive
            with contextlib.ExitStack() as stack:
                for scope in plan.scopes:
                    try:
                        stack.enter_context(release_ownership.target_writer_lease(
                            root, _scope_from_saved(scope),
                            {"purpose": "rehearsal-reset", "operationId": operation_id, "dbUniqueName": plan.db_unique_name},
                        ))
                    except release_ownership.TargetWriterBusy as error:
                        raise RehearsalResetError(
                            f"a rollout or migration is running on {plan.db_unique_name}; nothing was moved"
                        ) from error
                    except release_ownership.ReleaseOwnershipError as error:
                        raise RehearsalResetError(f"target writer lease is unavailable: {error}; nothing was moved") from error
                try:
                    archive = _apply_saved_archive(root, archive, record)
                except RehearsalResetError as error:
                    raise RehearsalResetError(f"{error}. Reset archive: {archive.relative_to(root).as_posix()}") from error
            return archive
    except release_ownership.TargetWriterBusy as error:
        raise RehearsalResetError("a staging rollout or migration is active in this checkout; nothing was moved") from error
    except release_ownership.ReleaseOwnershipError as error:
        raise RehearsalResetError(f"staging coordination is unavailable: {error}; nothing was moved") from error


# ---- command line ----------------------------------------------------------------------------------------

def _default_capture(
    repo_root: Path,
    env_file: Path | None,
    *,
    schema: str | None,
    target_profile: str | None,
) -> Callable[[], Mapping[str, str]]:
    def capture() -> Mapping[str, str]:
        from scripts import schema_catalog

        from scripts.db_targets import configured_schemas, migration_profile_keys

        values = read_project_env(env_file or Path(os.environ.get("PROJECT_ENV_FILE") or repo_root / ".env"))
        migration_keys = migration_profile_keys(values, ENVIRONMENT)
        read_profile = tuple(values.get(key, "") for key in (
            "STAGING_SCHEMA", "STAGING_SQLCL_CONNECTION", "STAGING_EXPECTED_USER",
        ))
        migration_profile = tuple(values.get(key, "") for key in migration_keys) if migration_keys else None
        read_configured = all(read_profile)
        if target_profile is None:
            if read_configured and migration_profile is not None and read_profile != migration_profile:
                raise RehearsalResetError(
                    "staging read and migration profiles differ; use --target-profile read or "
                    "--target-profile migration to choose the database to inspect"
                )
            if read_configured:
                operation = "read"
            elif migration_profile is not None:
                operation = "migration"
            else:
                raise RehearsalResetError("neither the staging read nor migration target profile is configured")
        else:
            operation = target_profile
            if operation == "read" and not read_configured:
                raise RehearsalResetError("the staging read target profile is not configured")
            if operation == "migration" and migration_profile is None:
                raise RehearsalResetError("the staging migration target profile is not configured")
        schemas = configured_schemas(values, ENVIRONMENT) if operation == "read" else split_list(migration_profile[0])
        if not schemas:
            raise RehearsalResetError(f"the staging {operation} profile has no configured schemas")
        if schema is None and len(schemas) != 1:
            raise RehearsalResetError(
                f"the staging {operation} profile lists multiple schemas ({', '.join(schemas)}); "
                "name one with --schema"
            )
        target = resolve_target(values, ENVIRONMENT, operation, schema=schema)
        run_dir = Path(tempfile.mkdtemp(prefix="identity-", dir=str(repo_root.joinpath(*ARCHIVE_PARTS))))
        return schema_catalog.capture_identity(target, run_dir)

    return capture


def _print_plan(plan: ResetPlan) -> None:
    container = f", container {plan.container_id}" if plan.container_id else ""
    print(f"Restored staging target: db_unique_name {plan.db_unique_name}{container}")
    if plan.pending_archive:
        print(f"Resuming the saved archive plan: {plan.pending_archive} (operation {plan.operation_id})")
    if plan.is_empty():
        print("Nothing to archive: no local release state belongs to this target.")
    else:
        print(f"Ledger records: {len(plan.rollouts)} run(s), {len(plan.attempts)} attempt(s), "
              f"{len(plan.verifications)} checkpoint verification(s), {plan.recovery_links} recovery link(s)")
        for path in plan.paths:
            print(f"  move {path}")
    if plan.tracked:
        print("These moved paths are tracked by Git and will show as deleted; commit or restore them:")
        for path in plan.tracked:
            print(f"  {path}")
    if plan.lost_proofs:
        print("Prior staging proof is invalidated; production needs a fresh verified staging run for these payloads:")
        for digest in plan.lost_proofs:
            print(f"  {digest}")
    for blocker in plan.blockers:
        print(f"BLOCKED: rollout {blocker.run_id} spans multiple databases or containers:")
        for scope in blocker.target_scopes:
            print(f"  target {scope}")
        for record in blocker.retained_records:
            print(f"  retain {record}")
        print("  Keep the full operation intact; its retained records still block replay on their targets.")
    for path, reason in plan.retained:
        print(f"  keep {path} - {reason}")


def _confirm(yes: bool, confirmed: str, *, action: str = "Archive this local state of staging") -> bool:
    if yes:
        print(terminal_confirm.confirmed_line(True, confirmed))
        return True
    if not terminal_confirm.may_read_yes_no():
        return False
    try:
        answer = input(f"{action} {confirmed}? [y/N]: ")
    except (EOFError, OSError):
        return False
    return answer.strip().casefold() in {"y", "yes"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=PROGRAM, description=__doc__.splitlines()[0])
    parser.add_argument("--env", required=True, help="staging (the only environment this command accepts)")
    parser.add_argument("--apply", action="store_true", help="move the listed records into the archive")
    parser.add_argument("--begin-restore", action="store_true", help="block proof before the external staging restore")
    parser.add_argument("--initialize-proof", action="store_true", help="create the first shared staging proof generation")
    parser.add_argument("--operation-id", metavar="UUID", help="stable ID for the begin-restore/apply pair")
    parser.add_argument("--schema", help="select the staging schema used for read-only identity capture")
    parser.add_argument("--target-profile", choices=("read", "migration"), help="select a distinct staging target profile")
    parser.add_argument("--confirm-target", metavar="DB_UNIQUE_NAME", help="required with a staging state change: the observed db_unique_name")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the [y/N] prompt when paired with --confirm-target")
    parser.add_argument("--repo-root", default=".", help=argparse.SUPPRESS)
    parser.add_argument("--env-file", type=Path, default=None, help=argparse.SUPPRESS)
    return parser


def _operation_id(value: str | None) -> str:
    if not isinstance(value, str):
        raise RehearsalResetError("supply the exact --operation-id created by --begin-restore")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise RehearsalResetError("--operation-id must be a canonical UUID") from error
    if str(parsed) != value:
        raise RehearsalResetError("--operation-id must be a canonical lowercase UUID")
    return value


def _proof_registry_path() -> Path:
    value = os.environ.get(PROOF_REGISTRY_ENV)
    if not value:
        raise RehearsalResetError(
            f"{PROOF_REGISTRY_ENV} must name the trusted shared proof registry used by staging and promotion checkouts"
        )
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RehearsalResetError(f"{PROOF_REGISTRY_ENV} must be an absolute path to the shared registry")
    if path.is_symlink():
        raise RehearsalResetError(f"{PROOF_REGISTRY_ENV} cannot be a symbolic link")
    return path


def _proof_generation_api():
    try:
        from scripts import release_proof_generation
    except ImportError as error:
        raise RehearsalResetError(f"shared release proof-generation support is unavailable: {error}") from error
    return release_proof_generation


def _restore_marker_path(root: Path, operation_id: str) -> Path:
    return root.joinpath(*ARCHIVE_PARTS) / f".restore-{operation_id}.json"


def _identity_same_target(left: object, right: Mapping[str, str]) -> bool:
    return (
        isinstance(left, Mapping)
        and isinstance(left.get("db_unique_name"), str)
        and isinstance(left.get("container_id"), str)
        and left["db_unique_name"].upper() == str(right.get("db_unique_name", "")).upper()
        and left["container_id"] == str(right.get("container_id", ""))
    )


def _read_restore_marker(root: Path, operation_id: str, identity: Mapping[str, str], registry: Path) -> tuple[Path, dict] | None:
    base = root.joinpath(*ARCHIVE_PARTS)
    expected = _restore_marker_path(root, operation_id)
    for marker in sorted(base.glob(".restore-*.json")) if base.exists() else ():
        if marker.is_symlink():
            raise RehearsalResetError(f"restore marker is unsafe: {marker}; preserve it and reconcile manually")
        document = _read_json(marker)
        if not isinstance(document, dict):
            raise RehearsalResetError(f"restore marker is unreadable: {marker}; preserve it and reconcile manually")
        if marker != expected and document.get("state") != "complete":
            raise RehearsalResetError(
                f"another restore operation {document.get('operationId', 'unknown')} is pending at "
                f"{marker.relative_to(root).as_posix()}; finish or reconcile it first"
            )
    if not expected.exists():
        return None
    document = _read_json(expected)
    if not isinstance(document, dict) or document.get("operationId") != operation_id:
        raise RehearsalResetError(f"restore marker is malformed at {expected}; preserve it and reconcile manually")
    if not _identity_same_target(document.get("target"), identity):
        raise RehearsalResetError(f"restore marker target differs from the observed database/container at {expected}")
    if document.get("registryPath") != str(registry):
        raise RehearsalResetError(
            f"{PROOF_REGISTRY_ENV} changed since restore operation {operation_id} began; use the original shared registry"
        )
    return expected, document


def _begin_restore(
    root: Path,
    identity: Mapping[str, str],
    operation_id: str,
    *,
    coordination_lease: release_ownership.StagingCoordinationLease | None = None,
) -> None:
    registry = _proof_registry_path()
    api = _proof_generation_api()
    marker = _restore_marker_path(root, operation_id)
    if coordination_lease is None:
        with release_ownership.staging_coordination_lease(
            root, {"purpose": "begin-restore", "operationId": operation_id}, allow_pending_reset=True,
        ) as claim:
            return _begin_restore(root, identity, operation_id, coordination_lease=claim)
    if not coordination_lease.holds(root):
        raise RehearsalResetError("the staging coordination claim does not cover this checkout")
    created = False
    pending_archive = _pending_reset_archive(
        root, str(identity["db_unique_name"]), str(identity["container_id"]),
    )
    if pending_archive is not None:
        raise RehearsalResetError(
            f"a reset archive is already pending at {pending_archive[0].relative_to(root).as_posix()}; "
            "resume that exact reset instead of beginning another restore"
        )
    existing = _read_restore_marker(root, operation_id, identity, registry)
    if existing is None:
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _write_atomic_json(marker, {
            "schemaVersion": 1, "operationId": operation_id, "state": "starting",
            "target": {key: identity.get(key) for key in sorted(identity)},
            "registryPath": str(registry),
            "createdAt": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        })
        created = True
    try:
        api.begin_restore(registry, dict(identity), operation_id)
    except Exception as error:
        if created:
            try:
                marker.unlink()
                _fsync_directory(marker.parent)
            except OSError:
                pass
        raise RehearsalResetError(f"shared proof registry refused restore start; no restore marker was kept: {error}") from error
    document = _read_json(marker)
    if not isinstance(document, dict):
        raise RehearsalResetError(f"restore began but its local marker cannot be read: {marker}; reconcile manually")
    document["state"] = "restoring"
    _write_atomic_json(marker, document)


def _validate_restore_apply(root: Path, identity: Mapping[str, str], operation_id: str) -> tuple[Path | None, dict | None]:
    registry = _proof_registry_path()
    marker = _read_restore_marker(root, operation_id, identity, registry)
    if _completed_archive(root, operation_id, str(identity["db_unique_name"]), str(identity["container_id"])) is not None:
        return marker
    if marker is not None and marker[1].get("state") != "complete":
        try:
            _proof_generation_api().begin_restore(registry, dict(identity), operation_id)
        except Exception as error:
            raise RehearsalResetError(f"shared proof registry does not confirm this pending restore: {error}") from error
        return marker
    raise RehearsalResetError(
        "no matching restore intent was recorded; run rehearsal-reset --begin-restore --operation-id <UUID> "
        "before the external restore, then use that same ID with --apply"
    )


def _complete_restore(root: Path, identity: Mapping[str, str], operation_id: str, archive: Path) -> None:
    registry = _proof_registry_path()
    try:
        result = _proof_generation_api().complete_restore(registry, dict(identity), operation_id)
    except Exception as error:
        raise RehearsalResetError(
            f"local archive is complete at {archive.relative_to(root).as_posix()}, but shared proof generation "
            f"completion failed; retry with the same --operation-id {operation_id}: {error}"
        ) from error
    record_path = archive / RESET_PLAN_NAME
    record = _read_json(record_path)
    if isinstance(record, dict):
        record["proofGenerationState"] = "complete"
        if isinstance(result, Mapping):
            record["proofGeneration"] = dict(result)
        _write_atomic_json(record_path, record)
    marker = _restore_marker_path(root, operation_id)
    if marker.exists():
        document = _read_json(marker)
        if not isinstance(document, Mapping) or document.get("operationId") != operation_id:
            raise RehearsalResetError(f"shared proof advanced, but local restore marker needs reconciliation: {marker}")
        try:
            marker.unlink()
            _fsync_directory(marker.parent)
        except OSError as error:
            raise RehearsalResetError(
                f"shared proof advanced, but local marker remains at {marker}; retry the same --operation-id: {error}"
            ) from error


def main(argv: Sequence[str] | None = None, *, capture: Callable[[], Mapping[str, str]] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    repo_root = Path(args.repo_root).resolve()
    try:
        if args.env != ENVIRONMENT:
            raise RehearsalResetError("rehearsal-reset is for staging only; production and DEV state are never reset")
        modes = sum(bool(value) for value in (args.apply, args.begin_restore, args.initialize_proof))
        if modes > 1:
            raise RehearsalResetError("choose only one of --begin-restore, --apply, or --initialize-proof")
        state_change = modes == 1
        if (args.confirm_target is not None or args.yes) and not state_change:
            raise RehearsalResetError("--confirm-target and --yes apply only to a restore or proof state change")
        if state_change and not args.confirm_target:
            raise RehearsalResetError("a state change needs --confirm-target <db_unique_name>")
        if args.apply or args.begin_restore:
            operation_id = _operation_id(args.operation_id)
        elif args.operation_id is not None:
            raise RehearsalResetError("--operation-id applies only to --begin-restore or --apply")
        else:
            operation_id = None
        if args.initialize_proof and operation_id is not None:
            raise RehearsalResetError("--initialize-proof does not use --operation-id")
        repo_root.joinpath(*ARCHIVE_PARTS).mkdir(parents=True, exist_ok=True)
        capture_identity = capture or _default_capture(
            repo_root, args.env_file, schema=args.schema, target_profile=args.target_profile,
        )

        if args.begin_restore:
            with release_ownership.staging_coordination_lease(
                repo_root,
                {"purpose": "begin-restore", "operationId": operation_id},
                allow_pending_reset=True,
            ) as claim:
                identity = capture_identity()
                _validate_reset_identity(identity)
                observed = str(identity["db_unique_name"])
                _refuse_production(identity)
                if args.confirm_target != observed:
                    raise RehearsalResetError(
                        f"--confirm-target {args.confirm_target!r} does not match the observed db_unique_name {observed!r}"
                    )
                if not _confirm(args.yes, observed, action="Begin the staging restore window and block proof for db_unique_name"):
                    print("Restore intent declined; the shared proof registry was not changed.")
                    return 1
                _begin_restore(repo_root, identity, operation_id, coordination_lease=claim)
            print(
                f"Restore operation {operation_id} is recorded for db_unique_name {observed}, container "
                f"{identity['container_id']}. Perform the external restore, then run --apply with this same ID."
            )
            return 0

        if args.initialize_proof:
            with release_ownership.staging_coordination_lease(
                repo_root, {"purpose": "initialize-staging-proof"},
            ):
                identity = capture_identity()
                _validate_reset_identity(identity)
                observed = str(identity["db_unique_name"])
                _refuse_production(identity)
                if args.confirm_target != observed:
                    raise RehearsalResetError(
                        f"--confirm-target {args.confirm_target!r} does not match the observed db_unique_name {observed!r}"
                    )
                if not _confirm(
                    args.yes, observed,
                    action="Initialize the shared proof baseline only if staging is a clean, intended baseline for db_unique_name",
                ):
                    print("Proof baseline initialization declined; the shared registry was not changed.")
                    return 1
                registry = _proof_registry_path()
                try:
                    _proof_generation_api().initialize_baseline(registry, dict(identity))
                except Exception as error:
                    raise RehearsalResetError(f"shared proof registry refused baseline initialization: {error}") from error
            print(f"Initialized the shared staging proof baseline for {observed} / container {identity['container_id']}.")
            return 0

        identity = capture_identity()
        _validate_reset_identity(identity)
        observed = str(identity.get("db_unique_name") or "")
        _refuse_production(identity)
        plan = plan_reset(repo_root, identity)
        _print_plan(plan)
        if not args.apply:
            pending = release_ownership.pending_rehearsal_reset(repo_root)
            if pending is not None:
                print(f"Staging operations are blocked by {pending[0].relative_to(repo_root).as_posix()}: {pending[1]}.")
            elif not plan.is_empty():
                print("After the external restore, begin it first with `--begin-restore --operation-id <UUID>`, "
                      "perform the restore, then use that same ID with `--apply`.")
            return 0
        if plan.blockers:
            raise RehearsalResetError("reset is blocked by mixed-target or linked-ledger evidence; no files or ledger rows were changed")
        if args.confirm_target != observed:
            raise RehearsalResetError(
                f"--confirm-target {args.confirm_target!r} does not match the observed db_unique_name {observed!r}; "
                "nothing was moved"
            )
        _validate_restore_apply(repo_root, identity, operation_id)
        if not _confirm(args.yes, observed, action="Archive the reset plan and complete the restore generation for staging"):
            print("Rehearsal reset declined; nothing was moved.")
            return 1
        archive = apply_reset(
            repo_root, plan, identity,
            confirmed_by=terminal_confirm.confirmed_by(args.yes, args.confirm_target),
            operation_id=operation_id,
        )
        _complete_restore(repo_root, identity, operation_id, archive)
        if plan.is_empty():
            print(f"No local records needed archiving; completed restore generation in {archive.relative_to(repo_root).as_posix()}.")
        else:
            print(f"Archived into {archive.relative_to(repo_root).as_posix()} and completed its restore generation.")
        return 0
    except (RehearsalResetError, ConfigError, release_state.ReleaseStateError, OSError, ValueError) as error:
        print(f"team error: {error}", file=sys.stderr)
        return EXIT_ERROR


def _validate_reset_identity(identity: Mapping[str, str]) -> None:
    if not isinstance(identity, Mapping) or not isinstance(identity.get("db_unique_name"), str) or not identity.get("db_unique_name"):
        raise RehearsalResetError("the observed staging identity has no db_unique_name")
    if not isinstance(identity.get("container_id"), str) or not identity.get("container_id"):
        raise RehearsalResetError("the observed staging identity has no container_id")


def _refuse_production(identity: Mapping[str, str]) -> None:
    observed = str(identity.get("db_unique_name") or "")
    if looks_like_production_identity(observed, str(identity.get("db_name") or ""), str(identity.get("service_name") or "")):
        raise RehearsalResetError(f"target {observed!r} looks like production; rehearsal-reset refuses it")


if __name__ == "__main__":
    raise SystemExit(main())
