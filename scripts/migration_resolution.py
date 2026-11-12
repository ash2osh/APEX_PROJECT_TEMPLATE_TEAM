#!/usr/bin/env python3
"""Owner resolution of a locked migration folder: the committed sidecar, its checks and the command.

``resolution.<env>.json`` sits beside ``status.<env>.json``. It records what the owner
concluded about a pending attempt that did not verify. It never makes a folder verified,
and it never changes the payload digest.

The command (``reconcile-migration``) reads local files only. It never starts a process
and never connects to a database. Its acknowledgment is typed by the owner; an agent never
types it. Imports of the manifest and revision modules happen inside functions, because
those modules import this one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .migration_attempts import (
    RESOLUTION_STATES,
    MigrationAttemptError,
    chain_terminals,
    read_attempt_events,
    reduce_attempt_events,
    write_attempt_event,
)
from .path_utils import relative_to_root

RESOLUTION_ENVIRONMENTS = frozenset({"dev", "staging", "prod"})
RESOLUTION_OUTCOMES = tuple(sorted(RESOLUTION_STATES))
RESOLUTION_FIELDS = frozenset({
    "schemaVersion", "environment", "outcome", "attemptId", "runId", "payloadDigest",
    "checksSha256", "target", "evidence", "recordedAt",
})
TARGET_FIELDS = frozenset({"environment", "connection", "expected_user", "current_schema"})
EVIDENCE_FIELDS = frozenset({"path", "sha256"})
SUPERSEDED_BY_FIELDS = frozenset({"folder", "receiptSha256"})
RUN_ID_RE = re.compile(r"migration-attempt-[A-Za-z0-9_-]+\Z", re.ASCII)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
FOLDER_NAME_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[a-z][a-z0-9]*(?:-[a-z0-9]+)*-r[0-9]{3}\Z",
    re.ASCII,
)
SIDECAR_NAME_RE = re.compile(r"resolution\.(?P<environment>dev|staging|prod)\.json\Z", re.ASCII)
ACKNOWLEDGMENT_PREFIX = "RECONCILE-MIGRATION"
COMMAND = "scripts/team.sh reconcile-migration"
RUN_MANIFEST_NAME = "run-manifest.json"
# Run records that a retained write attempt leaves behind, and states no write began in.
NOT_STARTED_STATES = frozenset({"frozen", "apply-not-started"})


class MigrationResolutionError(ValueError):
    """A resolution record is invalid, unsafe, or cannot be installed without replacing another."""


def reconcile_command(relative_folder: str, environment: str = "<env>") -> str:
    """The command an owner runs to resolve a folder; the outcome and evidence are the owner's choice."""
    return f"{COMMAND} {relative_folder} --env {environment} --outcome <outcome> --evidence <file>"


def environment_from_sidecar_name(name: str) -> str:
    match = SIDECAR_NAME_RE.fullmatch(name)
    if match is None:
        raise MigrationResolutionError(f"resolution filename must be resolution.dev.json, resolution.staging.json or resolution.prod.json: {name}")
    return match.group("environment")


def _reject_constant(value: str) -> None:
    raise MigrationResolutionError(f"resolution record contains unsupported JSON value {value}")


def _decode(raw: bytes, label: str) -> dict:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MigrationResolutionError(f"resolution record is not UTF-8: {label}") from error
    if "\r" in text or text.startswith("\ufeff"):
        raise MigrationResolutionError(f"resolution record must be UTF-8 without BOM and LF: {label}")

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise MigrationResolutionError(f"resolution record contains duplicate JSON key {key!r}: {label}")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=unique_pairs, parse_constant=_reject_constant)
    except (json.JSONDecodeError, RecursionError) as error:
        raise MigrationResolutionError(f"resolution record is invalid JSON: {label}") from error
    if not isinstance(value, dict):
        raise MigrationResolutionError(f"resolution record must be a JSON object: {label}")
    return value


def _canonical_uuid(value: object, field: str) -> None:
    try:
        parsed = uuid.UUID(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or str(parsed) != value or parsed.version not in {1, 2, 3, 4, 5}:
        raise MigrationResolutionError(f"resolution {field} must be a canonical UUID")


def _utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise MigrationResolutionError("resolution recordedAt must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MigrationResolutionError("resolution recordedAt must be a UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise MigrationResolutionError("resolution recordedAt must include the UTC timezone")
    if parsed > datetime.now(timezone.utc):
        raise MigrationResolutionError("resolution recordedAt is in the future")
    return parsed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def repository_path_is_safe(value: object) -> bool:
    """Relative, posix, no parent steps, and neither under migrations/ nor under the ignored scratch/."""
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    # Judged as a POSIX path on every platform; see migration_attempts._owner_evidence_path_is_safe.
    parsed = PurePosixPath(value)
    if value.startswith("/") or ":" in value or parsed.as_posix() != value or ".." in parsed.parts:
        return False
    return not (value.startswith(("migrations/", "scratch/")) or value in {"migrations", "scratch"})


def _check_shape(record: object, environment: str) -> dict:
    if not isinstance(record, dict):
        raise MigrationResolutionError("resolution record must be a JSON object")
    fields = set(RESOLUTION_FIELDS)
    if record.get("outcome") == "superseded":
        fields.add("supersededBy")
    if set(record) != fields:
        unknown = sorted(set(record) - fields)
        detail = f"; unexpected key {unknown[0]!r}" if unknown else ""
        raise MigrationResolutionError(f"resolution fields do not match schemaVersion 1 for this outcome{detail}")
    if type(record["schemaVersion"]) is not int or record["schemaVersion"] != 1:
        raise MigrationResolutionError("resolution schemaVersion must be integer 1")
    if environment not in RESOLUTION_ENVIRONMENTS or record["environment"] != environment:
        raise MigrationResolutionError("resolution environment does not match its file name")
    if record["outcome"] not in RESOLUTION_STATES:
        raise MigrationResolutionError("resolution outcome is not one of the three owner outcomes")
    # Null only for the scratch-only case: the attempt's pending event is not in this checkout.
    if record["attemptId"] is not None:
        _canonical_uuid(record["attemptId"], "attemptId")
    if not isinstance(record["runId"], str) or RUN_ID_RE.fullmatch(record["runId"]) is None:
        raise MigrationResolutionError("resolution runId is invalid")
    for field in ("payloadDigest", "checksSha256"):
        if not isinstance(record[field], str) or SHA256_RE.fullmatch(record[field]) is None:
            raise MigrationResolutionError(f"resolution {field} must be a SHA-256 digest")
    target = record["target"]
    if (
        not isinstance(target, dict)
        or set(target) != TARGET_FIELDS
        or target["environment"] != environment
        or any(not isinstance(target[field], str) or not target[field] for field in TARGET_FIELDS)
    ):
        raise MigrationResolutionError("resolution target binding is incomplete or names another environment")
    evidence = record["evidence"]
    if (
        not isinstance(evidence, dict)
        or set(evidence) != EVIDENCE_FIELDS
        or not repository_path_is_safe(evidence["path"])
        or not isinstance(evidence["sha256"], str)
        or SHA256_RE.fullmatch(evidence["sha256"]) is None
    ):
        raise MigrationResolutionError("resolution evidence must name one repository file outside migrations/ and scratch/ with its SHA-256")
    _utc_timestamp(record["recordedAt"])
    if "supersededBy" in record:
        superseded = record["supersededBy"]
        if (
            not isinstance(superseded, dict)
            or set(superseded) != SUPERSEDED_BY_FIELDS
            or not isinstance(superseded["folder"], str)
            or FOLDER_NAME_RE.fullmatch(superseded["folder"]) is None
            or not isinstance(superseded["receiptSha256"], str)
            or SHA256_RE.fullmatch(superseded["receiptSha256"]) is None
        ):
            raise MigrationResolutionError("resolution supersededBy must name a later revision folder and its receipt digest")
    return record


def read_resolution(path: Path) -> dict:
    """Read and shape-check one sidecar. Binding to a folder is checked by validate_resolution."""
    path = Path(path)
    environment = environment_from_sidecar_name(path.name)
    if path.is_symlink():
        raise MigrationResolutionError(f"resolution sidecar cannot be a symbolic link: {path.name}")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise MigrationResolutionError(f"resolution sidecar could not be read: {error}") from error
    return _check_shape(_decode(raw, path.name), environment)


def validate_resolution(path: Path, migration) -> dict:
    """Bind a sidecar to its folder: the current digests, and no verified receipt for the same environment.

    ``migration`` is a loaded migration (its folder, payload_digest and checks_sha256).
    """
    path = Path(path)
    record = read_resolution(path)
    environment = record["environment"]
    folder = Path(migration.folder)
    if path.parent.resolve() != folder.resolve():
        raise MigrationResolutionError("resolution sidecar is not inside the migration folder it binds to")
    if record["payloadDigest"] != migration.payload_digest or record["checksSha256"] != migration.checks_sha256:
        raise MigrationResolutionError("resolution digests do not match the current migration folder")
    receipt = folder / f"status.{environment}.json"
    if receipt.is_symlink() or receipt.exists():
        raise MigrationResolutionError(f"folder has both a receipt and a resolution for {environment}; a verified folder is never resolved")
    return record


def _checked_evidence_path(repo_root: Path, relative: object) -> Path:
    """The evidence file's path, after the repository, symlink and regular-file checks. Nothing is read."""
    if not repository_path_is_safe(relative):
        raise MigrationResolutionError("evidence must be a repository-relative file outside migrations/ and scratch/")
    try:
        root = Path(repo_root).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise MigrationResolutionError(f"repository root is unavailable: {error}") from error
    parts = Path(relative).parts
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise MigrationResolutionError(f"evidence path contains a symbolic link: {relative}")
    candidate = root.joinpath(*parts)
    try:
        resolved = candidate.resolve(strict=True)
        relative_to_root(resolved, root)
    except (OSError, RuntimeError, ValueError) as error:
        raise MigrationResolutionError(f"evidence file is missing or outside the repository: {relative}") from error
    if not candidate.is_file():
        raise MigrationResolutionError(f"evidence is not a regular file: {relative}")
    return candidate


def _evidence_digest(candidate: Path, relative: str) -> str:
    try:
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError as error:
        raise MigrationResolutionError(f"evidence file could not be read: {relative}") from error


def validate_evidence_file(repo_root: Path, evidence: Mapping) -> None:
    """Require the evidence file to be a regular repository file with the recorded digest.

    Symbolic links anywhere on the path are refused. The file is read, never copied.
    """
    if not isinstance(evidence, Mapping) or set(evidence) != EVIDENCE_FIELDS:
        raise MigrationResolutionError("evidence reference must contain exactly path and sha256")
    candidate = _checked_evidence_path(repo_root, evidence["path"])
    if _evidence_digest(candidate, evidence["path"]) != evidence["sha256"]:
        raise MigrationResolutionError(f"evidence file changed since it was reviewed: {evidence['path']}")


def evidence_reference(repo_root: Path, relative: str) -> dict:
    """The record's evidence entry for one repository file: its path and current SHA-256."""
    candidate = _checked_evidence_path(repo_root, relative)
    return {"path": relative, "sha256": _evidence_digest(candidate, relative)}


def repository_relative(repo_root: Path, value: str) -> str:
    """The repository-relative spelling of a path typed on the command line, without following links.

    The lexical path is compared with the repository root as given and as resolved, so a
    path typed under either spelling (for example /var and /private/var) is accepted.
    """
    given = Path(repo_root).absolute()
    raw = Path(value)
    lexical = Path(os.path.normpath(raw if raw.is_absolute() else given / raw))
    for base in (given, Path(repo_root).resolve()):
        try:
            return lexical.relative_to(Path(os.path.normpath(base))).as_posix()
        except ValueError:
            continue
    raise MigrationResolutionError(f"evidence must be a file inside the repository: {value}")


def write_resolution(path: Path, record: Mapping) -> None:
    """Atomically install one validated sidecar without replacing an existing path."""
    path = Path(path)
    environment = environment_from_sidecar_name(path.name)
    if path.is_symlink() or not path.parent.is_dir():
        raise MigrationResolutionError("resolution directory is missing or unsafe")
    try:
        normalized = _check_shape(json.loads(json.dumps(dict(record), ensure_ascii=False)), environment)
    except (TypeError, ValueError) as error:
        if isinstance(error, MigrationResolutionError):
            raise
        raise MigrationResolutionError("resolution record is not JSON-serializable") from error
    data = (json.dumps(normalized, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise MigrationResolutionError(f"resolution already exists and will not be replaced: {path.name}") from error
        except OSError as error:
            raise MigrationResolutionError(f"atomic no-overwrite resolution installation is unavailable: {error}") from error
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            # The sidecar is installed already. Directory fsync is unavailable on some filesystems.
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# The reconcile-migration command
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """One attempt the owner may resolve: a pending event chain, or a retained run with no event here."""

    attempt_id: str | None
    run_id: str
    pending_event: dict | None
    recorded_state: str | None
    payload_digest: object
    checks_sha256: object
    target: dict


@dataclass(frozen=True)
class _Plan:
    folder: Path
    relative_folder: str
    environment: str
    outcome: str
    recorded_state: str | None
    pending_event: dict | None
    record: dict
    acknowledgment: str
    superseded_by: str | None


def _comparable(record: Mapping) -> dict:
    """The record without its timestamp: two checks of the same inputs agree on everything else."""
    return {key: value for key, value in record.items() if key != "recordedAt"}


def _run_record(root: Path, run_id: str, folder_name: str) -> tuple[dict, dict] | None:
    """The retained run manifest and its record for one folder, if the run is still in scratch/."""
    from .migration_manifest import decode_json, MigrationManifestError

    run_dir = root / "scratch" / run_id
    manifest_path = run_dir / RUN_MANIFEST_NAME
    if run_dir.is_symlink() or not run_dir.is_dir() or manifest_path.is_symlink() or not manifest_path.is_file():
        return None
    try:
        manifest = decode_json(manifest_path.read_bytes(), str(manifest_path))
    except (OSError, MigrationManifestError) as error:
        raise MigrationResolutionError(f"retained run manifest cannot be read: {manifest_path.relative_to(root).as_posix()} ({error})") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("migrations"), list):
        raise MigrationResolutionError(f"retained run manifest is malformed: {manifest_path.relative_to(root).as_posix()}")
    for record in manifest["migrations"]:
        if isinstance(record, dict) and record.get("folder") == folder_name:
            return manifest, record
    return None


def _scratch_candidates(root: Path, folder_name: str, environment: str, schema: str | None, event_run_ids: set[str]) -> list[_Candidate]:
    scratch = root / "scratch"
    if scratch.is_symlink() or not scratch.is_dir():
        return []
    candidates: list[_Candidate] = []
    for run_dir in sorted(scratch.iterdir(), key=lambda path: path.name):
        if not run_dir.name.startswith("migration-attempt-") or run_dir.name in event_run_ids:
            continue
        if run_dir.is_symlink() or not run_dir.is_dir() or RUN_ID_RE.fullmatch(run_dir.name) is None:
            continue
        found = _run_record(root, run_dir.name, folder_name)
        if found is None:
            continue
        manifest, record = found
        target = manifest.get("target")
        if not isinstance(target, dict) or target.get("environment") != environment:
            continue
        if schema is not None and target.get("current_schema") != schema:
            continue
        state = record.get("state")
        write_attempted = record.get("writeAttempted")
        if state == "verified" or not (write_attempted is True or (write_attempted is False and state not in NOT_STARTED_STATES)):
            continue
        candidates.append(_Candidate(
            attempt_id=None,
            run_id=run_dir.name,
            pending_event=None,
            recorded_state=state if isinstance(state, str) else None,
            payload_digest=record.get("payloadDigest"),
            checks_sha256=record.get("checksSha256"),
            target=dict(target),
        ))
    return candidates


def _no_candidate_message(relative: str, environment: str, other: Mapping[str, tuple[str, str]]) -> str:
    if any(state == "apply-not-started" for state, _env in other.values()):
        attempt = next(key for key, (state, _env) in other.items() if state == "apply-not-started")
        return (
            f"no pending attempt for {relative} on {environment}: attempt {attempt} is apply-not-started, "
            "so it wrote nothing and there is nothing to resolve"
        )
    if other:
        attempt, (state, _env) = next(iter(sorted(other.items())))
        return f"no pending attempt for {relative} on {environment}: attempt {attempt} is {state}, not pending; only a pending attempt can be resolved"
    return (
        f"no pending attempt for {relative} on {environment} in this checkout, and no retained run manifest under scratch/ names it; "
        "there is no attempt to resolve here"
    )


def _select_candidate(root: Path, folder: Path, relative: str, folder_name: str, environment: str, schema: str | None, wanted: str | None) -> _Candidate:
    try:
        events = read_attempt_events(folder)
        reduce_attempt_events(events)
        terminals = chain_terminals(events)
    except (MigrationAttemptError, OSError, ValueError) as error:
        raise MigrationResolutionError(f"attempt events for {relative} are unreadable: {error}") from error
    env_terminals = {attempt: item for attempt, item in terminals.items() if item[1] == environment}
    other = {attempt: item for attempt, item in env_terminals.items() if item[0] != "pending"}
    candidates: list[_Candidate] = []
    for event in sorted((e for e in events if e["environment"] == environment and e["state"] == "pending"), key=lambda e: e["attemptId"]):
        if env_terminals.get(event["attemptId"], ("", ""))[0] != "pending":
            continue
        found = _run_record(root, event["runId"], folder_name)
        candidates.append(_Candidate(
            attempt_id=event["attemptId"],
            run_id=event["runId"],
            pending_event=event,
            recorded_state=found[1].get("state") if found is not None and isinstance(found[1].get("state"), str) else None,
            payload_digest=event["payloadDigest"],
            checks_sha256=event["checksSha256"],
            target=dict(event["target"]),
        ))
    event_run_ids = {event["runId"] for event in events}
    candidates.extend(_scratch_candidates(root, folder_name, environment, schema, event_run_ids))

    if wanted is not None:
        matches = [item for item in candidates if wanted in (item.attempt_id, item.run_id)]
        if len(matches) > 1:
            names = ", ".join(item.attempt_id or item.run_id for item in matches)
            raise MigrationResolutionError(f"several pending attempts for {relative} on {environment} share {wanted} ({names}); name one attempt id")
        if matches:
            return matches[0]
        if wanted in other:
            state = other[wanted][0]
            raise MigrationResolutionError(f"attempt {wanted} for {relative} on {environment} is {state}, not pending; only a pending attempt can be resolved")
        raise MigrationResolutionError(f"no pending attempt named {wanted} for {relative} on {environment}")
    if not candidates:
        raise MigrationResolutionError(_no_candidate_message(relative, environment, other))
    if len(candidates) > 1:
        names = ", ".join(item.attempt_id or item.run_id for item in candidates)
        raise MigrationResolutionError(
            f"several pending attempts for {relative} on {environment} ({names}); name one with --attempt <attempt-id>"
        )
    return candidates[0]


def _superseded_reference(root: Path, migration, schema: str | None, environment: str, name: str) -> dict:
    """D5: a later revision of the same family and schema whose receipt for this environment validates."""
    from .migration_manifest import (
        MigrationManifestError,
        _folder_parts,
        decode_json,
        load_archived_index,
        load_archived_migration,
        load_migration,
        validate_receipt,
    )

    refusal = f"--superseded-by {name} does not name a later verified revision for {environment}"
    try:
        _date, family, revision = _folder_parts(name)
    except MigrationManifestError as error:
        raise MigrationResolutionError(f"--superseded-by is not a migration folder name: {error}") from error
    if family != migration.family or revision <= migration.revision:
        raise MigrationResolutionError(f"{refusal}: it must be a later revision of {migration.family}")
    migrations_dir = root / "migrations"
    visible_parent = migrations_dir / schema if schema is not None else migrations_dir
    later_folder = visible_parent / name
    try:
        if later_folder.exists() or later_folder.is_symlink():
            later = load_migration(root, f"migrations/{schema}/{name}" if schema is not None else f"migrations/{name}")
        else:
            index_path = (migrations_dir / schema / ".archived.json") if schema is not None else migrations_dir / ".archived.json"
            matches = [item for item in load_archived_index(index_path, schema) if item["folder"] == name]
            if not matches:
                raise MigrationResolutionError(f"{refusal}: it is neither visible nor in the archive index")
            later = load_archived_migration(root, matches[0]["destination"], schema)
            if matches[0]["payloadDigest"] != later.payload_digest:
                raise MigrationResolutionError(f"{refusal}: the archived bytes differ from the archive index")
            later_folder = root.joinpath(*matches[0]["destination"].split("/"))
    except MigrationManifestError as error:
        raise MigrationResolutionError(f"{refusal}: {error}") from error
    receipt = later_folder / f"status.{environment}.json"
    if receipt.is_symlink() or not receipt.is_file():
        raise MigrationResolutionError(f"{refusal}: it has no status.{environment}.json")
    try:
        receipt_bytes = receipt.read_bytes()
        raw = decode_json(receipt_bytes, str(receipt))
        target = raw.get("target") if isinstance(raw, dict) else None
        if not isinstance(target, dict) or target.get("environment") != environment or (schema is not None and target.get("current_schema") != schema):
            raise MigrationResolutionError(f"{refusal}: its receipt names another environment or schema")
        validate_receipt(receipt, later, target)
    except (OSError, MigrationManifestError, TypeError, ValueError) as error:
        if isinstance(error, MigrationResolutionError):
            raise
        raise MigrationResolutionError(f"{refusal}: its receipt does not validate ({error})") from error
    return {"folder": name, "receiptSha256": hashlib.sha256(receipt_bytes).hexdigest()}


def _prepare(repo_root: Path, arguments: argparse.Namespace) -> _Plan:
    """Every check that needs no typed input. Nothing is written here; it runs again after the acknowledgment."""
    from .migration_manifest import (
        MigrationManifestError,
        _validate_relative_folder,
        decode_json,
        load_migration,
        validate_receipt,
    )

    root = Path(repo_root).resolve()
    environment = arguments.env
    outcome = arguments.outcome
    if outcome == "superseded" and not arguments.superseded_by:
        raise MigrationResolutionError("--outcome superseded needs --superseded-by <folder>, the later revision that carries the fix")
    if outcome != "superseded" and arguments.superseded_by is not None:
        raise MigrationResolutionError("--superseded-by is only valid with --outcome superseded")
    try:
        parts = _validate_relative_folder(arguments.folder)
        relative = "/".join(parts)
        schema = parts[1] if len(parts) == 3 else None
        folder = root.joinpath(*parts)
        migration = load_migration(root, relative)
    except (MigrationManifestError, OSError, ValueError) as error:
        raise MigrationResolutionError(f"migration folder is not usable: {error}") from error

    receipt = folder / f"status.{environment}.json"
    if receipt.is_symlink() or receipt.exists():
        try:
            raw = decode_json(receipt.read_bytes(), str(receipt))
            validate_receipt(receipt, migration, raw["target"])
        except (OSError, MigrationManifestError, KeyError, TypeError, ValueError) as error:
            raise MigrationResolutionError(f"{relative} has a status.{environment}.json that does not validate; it is not a resolution target ({error})") from error
        raise MigrationResolutionError(f"{relative} has a verified {environment} receipt (status.{environment}.json); a verified folder is never resolved")
    sidecar = folder / f"resolution.{environment}.json"
    if sidecar.is_symlink() or sidecar.exists():
        raise MigrationResolutionError(f"{relative} already has resolution.{environment}.json; a resolution is written once and is never replaced")

    evidence = evidence_reference(root, repository_relative(Path(repo_root), arguments.evidence))
    superseded = _superseded_reference(root, migration, schema, environment, arguments.superseded_by) if outcome == "superseded" else None
    candidate = _select_candidate(root, folder, relative, folder.name, environment, schema, arguments.attempt)

    if candidate.payload_digest != migration.payload_digest or candidate.checks_sha256 != migration.checks_sha256:
        if candidate.payload_digest is None or candidate.checks_sha256 is None:
            raise MigrationResolutionError(f"the retained record for {candidate.run_id} has no payload digest; the attempt cannot be bound to these bytes")
        raise MigrationResolutionError(
            f"folder bytes changed since attempt {candidate.attempt_id or candidate.run_id}: the folder's digests no longer match the attempt; "
            "inspect the folder history before resolving"
        )
    if candidate.pending_event is None:
        missing = [field for field in ("connection", "expected_user", "current_schema", "environment") if not isinstance(candidate.target.get(field), str) or not candidate.target.get(field)]
        if missing:
            raise MigrationResolutionError(f"the retained run manifest {candidate.run_id} does not name {', '.join(missing)}; the attempt cannot be bound to a target")
    target = {field: candidate.target[field] for field in sorted(TARGET_FIELDS)}
    record = {
        "schemaVersion": 1,
        "environment": environment,
        "outcome": outcome,
        "attemptId": candidate.attempt_id,
        "runId": candidate.run_id,
        "payloadDigest": migration.payload_digest,
        "checksSha256": migration.checks_sha256,
        "target": target,
        "evidence": evidence,
        "recordedAt": _now(),
    }
    if superseded is not None:
        record["supersededBy"] = superseded
    try:
        _check_shape(json.loads(json.dumps(record)), environment)
    except (TypeError, ValueError) as error:
        raise MigrationResolutionError(str(error)) from error
    return _Plan(
        folder=folder,
        relative_folder=relative,
        environment=environment,
        outcome=outcome,
        recorded_state=candidate.recorded_state,
        pending_event=candidate.pending_event,
        record=record,
        acknowledgment=f"{ACKNOWLEDGMENT_PREFIX} {candidate.attempt_id or candidate.run_id}",
        superseded_by=arguments.superseded_by,
    )


def _print_plan(plan: _Plan) -> None:
    record = plan.record
    print(f"Folder: {plan.relative_folder}")
    print(f"Environment: {plan.environment}")
    print(f"Attempt: {record['attemptId'] or 'none in this checkout (retained scratch run only)'}")
    print(f"Run: {record['runId']}")
    print(f"Recorded failure state: {plan.recorded_state or 'not recorded'}")
    print(f"Outcome: {plan.outcome}")
    print(f"Evidence: {record['evidence']['path']} sha256={record['evidence']['sha256']}")
    if plan.superseded_by is not None:
        print(f"Superseded by: {plan.superseded_by} (receipt sha256={record['supersededBy']['receiptSha256']})")
    print(f"Type {plan.acknowledgment} to record this owner resolution. Only the owner types it.")


def _closing_event(pending: Mapping, outcome: str, evidence: Mapping) -> dict:
    return {
        "schemaVersion": 1,
        "eventId": str(uuid.uuid4()),
        "attemptId": pending["attemptId"],
        "runId": pending["runId"],
        "predecessorEventId": pending["eventId"],
        "recordedAt": _now(),
        "migrationPath": pending["migrationPath"],
        "environment": pending["environment"],
        "target": dict(pending["target"]),
        "identity": None if pending["identity"] is None else dict(pending["identity"]),
        "files": [dict(item) for item in pending["files"]],
        "checksSha256": pending["checksSha256"],
        "payloadDigest": pending["payloadDigest"],
        "state": outcome,
        "writeAttempted": True,
        "evidence": [{"kind": "owner-resolution", "path": evidence["path"], "sha256": evidence["sha256"]}],
    }


def main(argv: Sequence[str] | None = None, *, repo_root: Path | None = None) -> int:
    """Exit 0 when the resolution is recorded; 2 when refused or only partly written. Nothing is written on a refusal."""
    parser = argparse.ArgumentParser(
        prog="scripts/team.sh reconcile-migration",
        description="Record the owner's resolution of a pending migration attempt from local files. It never connects to a database.",
    )
    parser.add_argument("folder", help="migrations/<SCHEMA>/<dated-folder> (or migrations/<dated-folder> in a flat layout)")
    parser.add_argument("--env", required=True, choices=sorted(RESOLUTION_ENVIRONMENTS), help="the environment whose attempt is resolved")
    parser.add_argument("--outcome", required=True, choices=RESOLUTION_OUTCOMES, help="what the owner established about the target")
    parser.add_argument("--evidence", required=True, metavar="FILE", help="repository file recording the owner's check; read, never copied; no secrets or personal data")
    parser.add_argument("--attempt", metavar="ID", help="attempt id (or scratch run id) to resolve when more than one is pending")
    parser.add_argument("--superseded-by", metavar="FOLDER", help="for --outcome superseded: the later revision of the same family that carries the fix")
    parser.add_argument("--repo-root", type=Path, default=repo_root or Path(__file__).resolve().parents[1], help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    try:
        plan = _prepare(Path(arguments.repo_root), arguments)
    except MigrationResolutionError as error:
        print(f"reconcile-migration refused: {error}", file=sys.stderr)
        return 2
    _print_plan(plan)

    # Imported here so that this module stays importable in checkouts without the terminal helper.
    from . import terminal_confirm

    if not terminal_confirm.may_read_acknowledgment():
        print("reconcile-migration refused: nothing was written", file=sys.stderr)
        return 2
    print("Acknowledgment (type it, then Enter):", file=sys.stderr)
    try:
        answer = input()
    except (EOFError, OSError):
        answer = ""
    if answer.strip() != plan.acknowledgment:
        print(f"reconcile-migration refused: the acknowledgment must be exactly {plan.acknowledgment}; nothing was written", file=sys.stderr)
        return 2

    sidecar = plan.folder / f"resolution.{plan.environment}.json"
    try:
        # Checked again after the typed answer: the folder, the evidence and the attempt must be the ones shown above.
        again = _prepare(Path(arguments.repo_root), arguments)
        if _comparable(again.record) != _comparable(plan.record) or again.acknowledgment != plan.acknowledgment:
            raise MigrationResolutionError("the folder, the evidence or the attempt changed while the acknowledgment was typed; nothing was written; run the command again")
        # The sidecar is written first: it is the durable record that other checkouts,
        # archive and the lock report read. A failed event write leaves the record in place.
        write_resolution(sidecar, plan.record)
    except (MigrationResolutionError, OSError) as error:
        print(f"reconcile-migration refused: {error}", file=sys.stderr)
        return 2
    print(f"Recorded owner resolution: {plan.relative_folder} {plan.environment} {plan.outcome}")
    print(f"  resolution record: {plan.relative_folder}/resolution.{plan.environment}.json")
    if plan.pending_event is None:
        print("  scratch-only attempt: no closing event (its pending event is not in this checkout)")
        return 0
    closing = _closing_event(plan.pending_event, plan.outcome, plan.record["evidence"])
    try:
        write_attempt_event(plan.folder, closing)
    except (MigrationAttemptError, OSError, ValueError) as error:
        print(
            f"reconcile-migration: the resolution record is written but the closing attempt event is not: {error}; "
            "the record governs this folder, so do not run the command again",
            file=sys.stderr,
        )
        return 2
    print(f"  closing attempt event: {closing['eventId']} (attempt {closing['attemptId']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
