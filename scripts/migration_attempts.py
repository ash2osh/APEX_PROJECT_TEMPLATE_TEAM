"""Durable, append-only evidence for migration write attempts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


EVENT_FILENAME_RE = re.compile(
    r"attempt-event\.(?P<event_id>[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})\.json\Z",
    re.ASCII,
)
RUN_ID_RE = re.compile(r"migration-attempt-[A-Za-z0-9_-]+\Z", re.ASCII)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
EVENT_FIELDS = {
    "schemaVersion", "eventId", "attemptId", "runId", "predecessorEventId",
    "recordedAt", "migrationPath", "environment", "target", "identity",
    "files", "checksSha256", "payloadDigest", "state", "writeAttempted", "evidence",
}
EVENT_STATES = {"pending", "apply-not-started", "verified"}
IDENTITY_KEYS = {
    "session_user", "current_schema", "db_name", "db_unique_name", "service_name",
    "container_id", "container_name", "edition", "database_version",
}
NO_WRITE_REASONS = {
    "identity-guard-refused", "migration-identity-guard-refused",
    "connection-failed-before-payload", "sqlcl-start-failed",
}
NOT_STARTED_CONNECTION_ERRORS = re.compile(
    r"(?m)^ORA-(?:01017|12154|12514|12537|12541|12545|12547|12560|12637)\b"
)
IDENTITY_GUARD_ERRORS = re.compile(r"(?m)^ORA-2098[0-5]\b")


class MigrationAttemptError(ValueError):
    """Attempt evidence is invalid or cannot be installed safely."""


@dataclass(frozen=True)
class AttemptState:
    status: str
    states: tuple[str, ...]


def not_started_reason(output: str) -> str | None:
    """Classify only explicit failures that prove no migration payload began."""
    if not isinstance(output, str):
        return None
    lines = {line.strip() for line in output.splitlines()}
    if "MIGRATION_APPLY_COMPLETED" in lines:
        return None
    if any(line.startswith("MIGRATION_PAYLOAD_STARTED:") for line in lines):
        return None
    if (
        "MIGRATION_IDENTITY_VERIFIED" in lines
        and "MIGRATION_IDENTITY_GUARD_REFUSED" in output
        and re.search(r"(?m)^ORA-20987\b", output)
    ):
        return "identity-guard-refused"
    if "MIGRATION_IDENTITY_VERIFIED" not in lines and IDENTITY_GUARD_ERRORS.search(output):
        return "migration-identity-guard-refused"
    if NOT_STARTED_CONNECTION_ERRORS.search(output) or re.search(r"(?m)^SP2-0640\b", output):
        return "connection-failed-before-payload"
    if "unable to start sqlcl:" in output.casefold():
        return "sqlcl-start-failed"
    return None


def _migration_relative_path(folder: Path) -> str:
    resolved = folder.resolve(strict=True)
    for parent in resolved.parents:
        if parent.name == "migrations":
            return resolved.relative_to(parent.parent).as_posix()
    raise MigrationAttemptError("migration folder is not under a repository migrations directory")


def _canonical_uuid(value: object, field: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        raise MigrationAttemptError(f"event {field} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise MigrationAttemptError(f"event {field} must be a canonical UUID") from error
    if str(parsed) != value or parsed.version not in {1, 2, 3, 4, 5}:
        raise MigrationAttemptError(f"event {field} must be a canonical UUID")
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise MigrationAttemptError("event recordedAt must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MigrationAttemptError("event recordedAt must be a UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise MigrationAttemptError("event recordedAt must include the UTC timezone")
    return parsed


def validate_attempt_event(event: object, folder: Path | None = None) -> dict:
    """Validate one exact schema-1 event, optionally binding it to its folder."""
    if not isinstance(event, dict) or set(event) != EVENT_FIELDS:
        raise MigrationAttemptError("attempt event fields do not match schemaVersion 1")
    if type(event["schemaVersion"]) is not int or event["schemaVersion"] != 1:
        raise MigrationAttemptError("attempt event schemaVersion must be integer 1")
    _canonical_uuid(event["eventId"], "eventId")
    _canonical_uuid(event["attemptId"], "attemptId")
    _canonical_uuid(event["predecessorEventId"], "predecessorEventId", nullable=True)
    if not isinstance(event["runId"], str) or RUN_ID_RE.fullmatch(event["runId"]) is None:
        raise MigrationAttemptError("attempt event runId is invalid")
    recorded = _timestamp(event["recordedAt"])
    if recorded > datetime.now(timezone.utc):
        raise MigrationAttemptError("attempt event recordedAt is in the future")
    migration_path = event["migrationPath"]
    if (
        not isinstance(migration_path, str)
        or not migration_path.startswith("migrations/")
        or Path(migration_path).as_posix() != migration_path
        or "\\" in migration_path
        or ".." in Path(migration_path).parts
    ):
        raise MigrationAttemptError("attempt event migrationPath is unsafe")
    if folder is not None and migration_path != _migration_relative_path(folder):
        raise MigrationAttemptError("attempt event migrationPath does not match its containing folder")
    environment = event["environment"]
    if not isinstance(environment, str) or environment not in {"dev", "staging", "prod"}:
        raise MigrationAttemptError("attempt event environment is invalid")
    target = event["target"]
    if (
        not isinstance(target, dict)
        or set(target) != {"environment", "connection", "expected_user", "current_schema"}
        or target.get("environment") != environment
        or not isinstance(target.get("current_schema"), str)
        or not target["current_schema"]
        or not isinstance(target.get("connection"), str)
        or not target["connection"]
        or not isinstance(target.get("expected_user"), str)
        or not target["expected_user"]
    ):
        raise MigrationAttemptError("attempt event target binding is incomplete")
    identity = event["identity"]
    if identity is not None and (
        not isinstance(identity, dict)
        or set(identity) != IDENTITY_KEYS
        or any(not isinstance(value, str) or not value for value in identity.values())
    ):
        raise MigrationAttemptError("attempt event identity must be a string map or null")
    files = event["files"]
    if not isinstance(files, list) or not files:
        raise MigrationAttemptError("attempt event files must be a nonempty array")
    seen_names: set[str] = set()
    seen_sequences: set[int] = set()
    for item in files:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "sequence", "sha256"}
            or not isinstance(item["name"], str)
            or re.fullmatch(r"[0-9]{3}-[a-z][a-z0-9]*(?:-[a-z0-9]+)*\.sql", item["name"], re.ASCII) is None
            or type(item["sequence"]) is not int
            or item["sequence"] < 1
            or int(item["name"][:3]) != item["sequence"]
            or not isinstance(item["sha256"], str)
            or SHA256_RE.fullmatch(item["sha256"]) is None
            or item["name"] in seen_names
            or item["sequence"] in seen_sequences
        ):
            raise MigrationAttemptError("attempt event SQL file digest list is invalid")
        seen_names.add(item["name"])
        seen_sequences.add(item["sequence"])
    for field in ("checksSha256", "payloadDigest"):
        if not isinstance(event[field], str) or SHA256_RE.fullmatch(event[field]) is None:
            raise MigrationAttemptError(f"attempt event {field} must be a SHA-256 digest")
    state = event["state"]
    write_attempted = event["writeAttempted"]
    if not isinstance(state, str) or state not in EVENT_STATES or type(write_attempted) is not bool:
        raise MigrationAttemptError("attempt event state/writeAttempted values are invalid")
    if state in {"pending", "verified"} and write_attempted is not True:
        raise MigrationAttemptError("pending and verified events must retain writeAttempted=true")
    if state == "apply-not-started" and write_attempted is not False:
        raise MigrationAttemptError("apply-not-started events must record writeAttempted=false")
    evidence = event["evidence"]
    if not isinstance(evidence, list):
        raise MigrationAttemptError("attempt event evidence must be an array")
    if state == "apply-not-started":
        if len(evidence) != 1:
            raise MigrationAttemptError("apply-not-started event requires one no-write evidence reference")
        item = evidence[0]
        if (
            not isinstance(item, dict)
            or set(item) != {"kind", "path", "sha256", "classification"}
            or item.get("kind") != "no-write-sqlcl-output"
            or not isinstance(item.get("path"), str)
            or not item["path"].startswith("scratch/")
            or Path(item["path"]).as_posix() != item["path"]
            or "\\" in item["path"]
            or ".." in Path(item["path"]).parts
            or not isinstance(item.get("sha256"), str)
            or SHA256_RE.fullmatch(item["sha256"]) is None
            or not isinstance(item.get("classification"), str)
            or item["classification"] not in NO_WRITE_REASONS
        ):
            raise MigrationAttemptError("attempt event no-write evidence reference is invalid")
    elif evidence:
        raise MigrationAttemptError("pending and verified events cannot carry no-write evidence")
    return event


def _decode_event(raw: bytes, label: str) -> dict:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MigrationAttemptError(f"attempt event is not UTF-8: {label}") from error
    if "\r" in text or text.startswith("\ufeff"):
        raise MigrationAttemptError(f"attempt event must use UTF-8 without BOM and LF: {label}")

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise MigrationAttemptError(f"attempt event contains duplicate JSON key {key!r}: {label}")
            result[key] = value
        return result

    try:
        event = json.loads(
            text,
            object_pairs_hook=unique_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                MigrationAttemptError(f"attempt event contains unsupported JSON value {value}")
            ),
        )
    except (json.JSONDecodeError, MigrationAttemptError) as error:
        raise MigrationAttemptError(f"attempt event is invalid JSON: {label}") from error
    return validate_attempt_event(event)


def _event_filename(event_id: str) -> str:
    return f"attempt-event.{event_id}.json"


def write_attempt_event(folder: Path, event: Mapping) -> Path:
    """Durably install a validated event without replacing an earlier event."""
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        raise MigrationAttemptError("attempt event parent folder is missing or unsafe")
    current = Path(folder.anchor)
    for part in folder.absolute().parts[1:]:
        current = current / part
        if current.is_symlink():
            raise MigrationAttemptError("attempt event parent path contains a symbolic link")
    folder.resolve(strict=True)
    normalized = validate_attempt_event(dict(event), folder)
    path = folder / _event_filename(normalized["eventId"])
    data = (json.dumps(normalized, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".attempt-event.", suffix=".tmp", dir=folder)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise MigrationAttemptError(f"attempt event already exists and will not be replaced: {path.name}") from error
        except OSError as error:
            raise MigrationAttemptError(f"atomic no-overwrite attempt event installation is unavailable: {error}") from error
        try:
            directory_fd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return path


def read_attempt_events(folder: Path) -> tuple[dict, ...]:
    """Read and validate all event sidecars in deterministic identifier order."""
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        raise MigrationAttemptError("attempt event folder is missing or unsafe")
    events: list[dict] = []
    try:
        paths = sorted(folder.iterdir(), key=lambda path: path.name)
    except OSError as error:
        raise MigrationAttemptError(f"attempt event folder cannot be read: {error}") from error
    for path in paths:
        if not path.name.startswith("attempt-event."):
            continue
        match = EVENT_FILENAME_RE.fullmatch(path.name)
        if match is None or path.is_symlink() or not path.is_file():
            raise MigrationAttemptError(f"attempt event filename or file type is unsafe: {path.name}")
        try:
            event = _decode_event(path.read_bytes(), path.name)
        except OSError as error:
            raise MigrationAttemptError(f"attempt event cannot be read: {path.name}") from error
        if event["eventId"] != match.group("event_id"):
            raise MigrationAttemptError(f"attempt event filename does not match its eventId: {path.name}")
        events.append(validate_attempt_event(event, folder))
    return tuple(events)


def reduce_attempt_events(events: Sequence[Mapping]) -> AttemptState:
    """Reduce event chains without relying on directory order or timestamps."""
    by_id: dict[str, dict] = {}
    by_attempt: dict[str, list[dict]] = defaultdict(list)
    for raw_event in events:
        event = validate_attempt_event(dict(raw_event))
        if event["eventId"] in by_id:
            raise MigrationAttemptError("duplicate attempt eventId")
        by_id[event["eventId"]] = event
        by_attempt[event["attemptId"]].append(event)

    states: list[str] = []
    locked = False
    for attempt_id, chain_events in sorted(by_attempt.items()):
        children: dict[str | None, list[dict]] = defaultdict(list)
        for event in chain_events:
            children[event["predecessorEventId"]].append(event)
        if len(children[None]) != 1:
            raise MigrationAttemptError(f"attempt {attempt_id} must have exactly one root event")
        if children[None][0]["state"] != "pending":
            raise MigrationAttemptError(f"attempt {attempt_id} root event must be pending")
        if any(len(items) > 1 for items in children.values()):
            raise MigrationAttemptError(f"attempt {attempt_id} contains a branching event chain")
        ordered: list[dict] = []
        current = children[None][0]
        seen: set[str] = set()
        while current is not None:
            event_id = current["eventId"]
            if event_id in seen:
                raise MigrationAttemptError(f"attempt {attempt_id} contains a cycle")
            seen.add(event_id)
            ordered.append(current)
            following = children.get(event_id, [])
            if following and current["state"] != "pending":
                raise MigrationAttemptError(f"attempt {attempt_id} has events after a terminal state")
            current = following[0] if following else None
        if len(seen) != len(chain_events):
            raise MigrationAttemptError(f"attempt {attempt_id} contains an unlinked predecessor")
        for previous, following in zip(ordered, ordered[1:], strict=False):
            if previous["state"] != "pending" or following["state"] == "pending":
                raise MigrationAttemptError(f"attempt {attempt_id} must contain one pending-to-terminal transition")
            if previous["runId"] != following["runId"]:
                raise MigrationAttemptError(f"attempt {attempt_id} changes runId within a chain")
            if previous["migrationPath"] != following["migrationPath"] or previous["payloadDigest"] != following["payloadDigest"]:
                raise MigrationAttemptError(f"attempt {attempt_id} changes migration source within a chain")
            if previous["target"] != following["target"]:
                raise MigrationAttemptError(f"attempt {attempt_id} changes target within a chain")
            if previous["identity"] != following["identity"]:
                raise MigrationAttemptError(f"attempt {attempt_id} changes observed identity within a chain")
            if _timestamp(previous["recordedAt"]) > _timestamp(following["recordedAt"]):
                raise MigrationAttemptError(f"attempt {attempt_id} timestamps run backwards")
        if len(ordered) > 2:
            raise MigrationAttemptError(f"attempt {attempt_id} contains more than one terminal transition")
        terminal = ordered[-1]
        states.append(f"attempt-event:{terminal['state']}:{terminal['environment']}")
        if terminal["state"] in {"pending", "verified"}:
            locked = True
    return AttemptState("locked" if locked else "unlocked", tuple(states))


def validate_no_write_evidence(repo_root: Path, event: Mapping) -> bool:
    """Require retained, hash-matching SQLcl output that proves no payload began."""
    normalized = validate_attempt_event(dict(event))
    if normalized["state"] != "apply-not-started":
        return True
    evidence = normalized["evidence"][0]
    root = Path(repo_root).resolve(strict=True)
    candidate = root.joinpath(*Path(evidence["path"]).parts)
    current = root
    for part in Path(evidence["path"]).parts:
        current = current / part
        if current.is_symlink():
            return False
    try:
        candidate.resolve(strict=True).relative_to(root)
        if not candidate.is_file():
            return False
        content = candidate.read_bytes()
    except (OSError, ValueError):
        return False
    if hashlib.sha256(content).hexdigest() != evidence["sha256"]:
        return False
    reason = not_started_reason(content.decode("utf-8", errors="replace"))
    return reason is not None and reason == evidence["classification"]
