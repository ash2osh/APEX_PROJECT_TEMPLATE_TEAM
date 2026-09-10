"""Exact, canonical confirmations for destructive migration operations."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from collections.abc import Mapping, Sequence


class ConfirmationError(ValueError):
    """Raised when a destructive confirmation is missing or does not match."""


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ACTIONS = {"migrate", "undo", "redo"}
_DOCUMENT_KEYS = {"version", "confirmations"}
_ENTRY_KEYS = {
    "migration_id", "action", "bundle_checksum", "payload_target_state_key", "confirmed",
}


@dataclass(frozen=True)
class ConfirmationRequirement:
    migration_id: str
    action: str
    bundle_checksum: str
    payload_target_state_key: str


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _canonical_with_lf(value: Mapping[str, Any]) -> bytes:
    return _canonical(value) + b"\n"


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_with_lf(value)).hexdigest()


def _validate_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ConfirmationError(f"{label} must be a lowercase SHA-256")
    return value


def _requirement_key(requirement: ConfirmationRequirement) -> tuple[str, str, str, str]:
    if not isinstance(requirement.migration_id, str) or not requirement.migration_id or any(char.isspace() for char in requirement.migration_id):
        raise ConfirmationError("confirmation migration_id is malformed")
    if requirement.action not in _ACTIONS:
        raise ConfirmationError(f"confirmation action is unsupported: {requirement.action}")
    _validate_digest(requirement.bundle_checksum, "confirmation bundle_checksum")
    _validate_digest(requirement.payload_target_state_key, "confirmation payload_target_state_key")
    return (
        requirement.migration_id,
        requirement.action,
        requirement.bundle_checksum,
        requirement.payload_target_state_key,
    )


def _validate_entry(value: Any, index: int) -> tuple[tuple[str, str, str, str], bool]:
    if not isinstance(value, Mapping):
        raise ConfirmationError(f"confirmation entry {index} must be an object")
    if set(value) != _ENTRY_KEYS:
        raise ConfirmationError(f"confirmation entry {index} has unexpected fields")
    migration_id = value.get("migration_id")
    if not isinstance(migration_id, str) or not migration_id or any(char.isspace() for char in migration_id):
        raise ConfirmationError(f"confirmation entry {index} migration_id is malformed")
    action = value.get("action")
    if action not in _ACTIONS:
        raise ConfirmationError(f"confirmation entry {index} action is unsupported")
    bundle_checksum = _validate_digest(value.get("bundle_checksum"), f"confirmation entry {index} bundle_checksum")
    target_key = _validate_digest(value.get("payload_target_state_key"), f"confirmation entry {index} payload_target_state_key")
    confirmed = value.get("confirmed")
    if type(confirmed) is not bool:
        raise ConfirmationError(f"confirmation entry {index} confirmed must be a boolean")
    return (migration_id, action, bundle_checksum, target_key), confirmed


def _validate_document(value: Any) -> tuple[dict[str, Any], list[tuple[tuple[str, str, str, str], bool]]]:
    if not isinstance(value, Mapping):
        raise ConfirmationError("confirmation document must be an object")
    if set(value) != _DOCUMENT_KEYS or value.get("version") != 1:
        raise ConfirmationError("confirmation document must be version 1 with only version and confirmations")
    entries = value.get("confirmations")
    if not isinstance(entries, list):
        raise ConfirmationError("confirmation confirmations must be a list")
    normalized: list[tuple[tuple[str, str, str, str], bool]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for index, entry in enumerate(entries):
        key, confirmed = _validate_entry(entry, index)
        if key in seen:
            raise ConfirmationError("confirmation document contains duplicate entries")
        seen.add(key)
        normalized.append((key, confirmed))
    return dict(value), normalized


def load_confirmation(path: str | Path) -> tuple[dict[str, Any], str]:
    """Load and validate canonical confirmation JSON and return its digest."""
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise ConfirmationError(f"confirmation file is not a regular file: {candidate}")
    try:
        raw = candidate.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfirmationError(f"confirmation file is not valid UTF-8 JSON: {candidate}") from exc
    document, _ = _validate_document(value)
    if raw != _canonical_with_lf(document):
        raise ConfirmationError("confirmation file is not canonical JSON")
    return document, hashlib.sha256(raw).hexdigest()


def confirmation_template(requirements: Sequence[ConfirmationRequirement]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    normalized = []
    for requirement in requirements:
        key = _requirement_key(requirement)
        if key in seen:
            raise ConfirmationError("confirmation requirements contain duplicate entries")
        seen.add(key)
        normalized.append((key, requirement))
    for key, _requirement in sorted(normalized, key=lambda item: (item[0][0], item[0][1], item[0][2], item[0][3])):
        migration_id, action, bundle_checksum, target_key = key
        entries.append({
            "migration_id": migration_id,
            "action": action,
            "bundle_checksum": bundle_checksum,
            "payload_target_state_key": target_key,
            "confirmed": False,
        })
    return {"version": 1, "confirmations": entries}


def require_confirmations(
    requirements: Sequence[ConfirmationRequirement],
    document: Mapping[str, Any] | None,
) -> str:
    """Require exactly one confirmed entry per destructive operation."""
    requirement_keys: list[tuple[str, str, str, str]] = []
    seen_requirements: set[tuple[str, str, str, str]] = set()
    for requirement in requirements:
        key = _requirement_key(requirement)
        if key in seen_requirements:
            raise ConfirmationError("confirmation requirements contain duplicate entries")
        seen_requirements.add(key)
        requirement_keys.append(key)
    if document is None:
        if requirement_keys:
            raise ConfirmationError("destructive migration confirmation is required")
        return ""
    validated, entries = _validate_document(document)
    if not requirement_keys:
        if entries:
            raise ConfirmationError("confirmation document contains entries for no destructive operations")
        return ""
    entry_keys = {key for key, _confirmed in entries}
    required_keys = set(requirement_keys)
    missing = sorted(required_keys - entry_keys)
    extra = sorted(entry_keys - required_keys)
    if missing or extra:
        diagnostics: list[str] = []
        if missing:
            diagnostics.append("missing " + ", ".join("/".join(key) for key in missing))
        if extra:
            diagnostics.append("extra " + ", ".join("/".join(key) for key in extra))
        raise ConfirmationError("confirmation entries do not match required operations (" + "; ".join(diagnostics) + ")")
    unconfirmed = [key for key, confirmed in entries if not confirmed]
    if unconfirmed:
        raise ConfirmationError("confirmation entries must set confirmed=true: " + ", ".join("/".join(key) for key in sorted(unconfirmed)))
    return _digest(validated)
