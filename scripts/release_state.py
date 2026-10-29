#!/usr/bin/env python3
"""Durable, repository-local attempt state for feature release capsules."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from . import migration_attempts, release_capsule, release_ownership
from .migration_checks import complete_observed_identity
from .path_utils import relative_to_root

if TYPE_CHECKING:
    from .feature_release import TargetPlan
    from .release_capsule import ReleaseCapsule


INDEX_SCHEMA_VERSION = 2
LEGACY_INDEX_SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
COMMIT_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z", re.ASCII)
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z", re.ASCII)
ENVIRONMENTS = {"staging", "prod"}
ATTEMPT_STATES = {"attempting", "verified", "unknown", "not-started"}
INDEX_FIELDS = {
    "schemaVersion", "attempts", "cleanupHistory", "checkpointVerifications",
    "recoveryLinks", "rollouts", "migrationProvenance",
}
LEGACY_INDEX_FIELDS = {"schemaVersion", "attempts", "cleanupHistory"}
ATTEMPT_FIELDS = {
    "attemptId", "environment", "targetScope", "target", "checkpoint", "sourceCommit",
    "payloadDigest", "engineDigest", "planDigest", "state", "writePossible", "startedAt",
    "finishedAt", "evidence", "metrics", "budget", "recoveryOf", "recoveredBy",
}
SCOPE_FIELDS = {"dbUniqueName", "containerId", "schema", "edition"}
TARGET_FIELDS = {"environment", "connection", "expectedUser", "schema", "identity"}
IDENTITY_FIELDS = {
    "session_user", "current_schema", "db_name", "db_unique_name", "service_name",
    "container_id", "container_name", "edition", "database_version",
}
EVIDENCE_FIELDS = {"path", "sha256"}
METRIC_FIELDS = {
    "durationSeconds", "preparationSeconds", "objectSeconds", "sqlclSessions",
    "budgetSeconds", "budgetCoverageComplete", "budgetPassed", "includedCheckpoints",
    "excludedObjectSteps", "noWriteClassification",
}
BUDGET_FIELDS = {"seconds", "elapsedSeconds", "coverageComplete", "passed"}
CLEANUP_FIELDS = {"cleanupId", "payloadDigest", "requestedAt", "state", "targetPaths", "removedPaths", "error"}
CLEANUP_STATES = {"removing", "complete", "partial"}
VERIFICATION_FIELDS = {
    "verificationId", "runId", "environment", "checkpoint", "targetScope", "sourceCommit",
    "payloadDigest", "engineDigest", "planDigest", "mode", "attemptId", "verifiedAt",
    "evidence", "objectSeconds", "sessionCount",
}
RECOVERY_LINK_FIELDS = {"oldAttemptId", "verificationId", "reconciliationEvidence", "linkedAt"}
ROLLOUT_FIELDS = {
    "runId", "environment", "sourceCommit", "payloadDigest", "engineDigest", "planDigest",
    "targetScopes", "state", "databaseState", "startedAt", "finishedAt", "expectedCheckpointCount",
    "expectedStepCount", "steps", "checkpointVerificationIds", "budget", "finalReportPath", "finalReportSha256",
}
ROLLOUT_STATES = {"running", "verified", "failed", "unknown", "not-started"}
STEP_OUTCOME_FIELDS = {"step", "status", "receiptPath", "receiptSha256"}
STEP_OUTCOME_STATES = {"verified", "failed", "unknown", "not-started"}
MIGRATION_PROVENANCE_FIELDS = {"schema1Sha256", "retainedPath", "migratedAt"}
NO_WRITE_REASONS = {
    "connection-failed-before-payload", "sqlcl-start-failed",
    "identity-guard-refused", "migration-identity-guard-refused",
}
REPORT_FIELDS = {
    "schemaVersion", "environment", "status", "databaseState", "payloadDigest",
    "engineDigest", "sourceCommit", "targetPlanDigest", "inputHashes", "objectBudget",
    "localRunId", "completionProof",
}
OBJECT_BUDGET_FIELDS = {
    "seconds", "elapsedSeconds", "coverageComplete", "withinBudget",
    "includedCheckpoints", "excludedObjectSteps",
}
PAYLOAD_MARKERS = (
    "MIGRATION_PAYLOAD_STARTED:", "ROLLOUT_PAYLOAD_STARTED:",
    "FEATURE_RELEASE_PAYLOAD_STARTED:", "FEATURE_PAYLOAD_STARTED:",
    "RELEASE_PAYLOAD_STARTED:", "MIGRATION_APPLY_COMPLETED",
    "ROLLOUT_APPLY_COMPLETED", "FEATURE_RELEASE_APPLY_COMPLETED",
    "RELEASE_APPLY_COMPLETED",
)


class ReleaseStateError(ValueError):
    """Local release state is corrupt, conflicting, or unsafe to update."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp(value: object, label: str, *, nullable: bool = False) -> datetime | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str):
        raise ReleaseStateError(f"{label} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ReleaseStateError(f"{label} must be an ISO-8601 UTC timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ReleaseStateError(f"{label} must include the UTC timezone")
    if parsed > datetime.now(timezone.utc):
        raise ReleaseStateError(f"{label} cannot be in the future")
    return parsed


def _uuid(value: object, label: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or UUID_RE.fullmatch(value) is None:
        raise ReleaseStateError(f"{label} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise ReleaseStateError(f"{label} must be a canonical UUID") from error
    if str(parsed) != value or parsed.version not in {1, 2, 3, 4, 5}:
        raise ReleaseStateError(f"{label} must be a canonical UUID")
    return value


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise ReleaseStateError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _commit(value: object) -> str:
    if not isinstance(value, str) or COMMIT_RE.fullmatch(value) is None:
        raise ReleaseStateError("attempt sourceCommit must be a lowercase Git commit hash")
    return value


def _root(repo_root: Path) -> Path:
    requested = Path(repo_root)
    if requested.is_symlink():
        raise ReleaseStateError("repository root cannot be a symlink")
    try:
        root = requested.resolve(strict=True)
    except OSError as error:
        raise ReleaseStateError(f"repository root is unavailable: {error}") from error
    if not root.is_dir():
        raise ReleaseStateError("repository root must be a directory")
    return root


def _relative_path(value: object, label: str) -> str:
    if (
        not isinstance(value, str) or not value or "\\" in value
        or PurePosixPath(value).is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ReleaseStateError(f"{label} must be a safe repository-relative POSIX path")
    return value


def _checked_path(root: Path, relative: str, *, allow_missing: bool = True) -> Path:
    relative = _relative_path(relative, "release state path")
    path = root
    missing = False
    try:
        info = root.lstat()
        if root.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise ReleaseStateError("repository root is a symlink or reparse point")
        for part in PurePosixPath(relative).parts:
            path = path / part
            if missing:
                continue
            try:
                info = path.lstat()
            except FileNotFoundError as error:
                if not allow_missing:
                    raise ReleaseStateError(f"release state path is missing: {relative}") from error
                missing = True
                continue
            if path.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise ReleaseStateError(f"release state path contains a symlink or reparse point: {relative}")
    except ReleaseStateError:
        raise
    except OSError as error:
        raise ReleaseStateError(f"release state path is unavailable: {relative}: {error}") from error
    return path


def _release_directory(root: Path, *, create: bool) -> Path:
    path = root / "releases"
    created = False
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            return path
        try:
            path.mkdir(mode=0o700)
            info = path.lstat()
            created = True
        except OSError as error:
            raise ReleaseStateError(f"cannot create local release state directory: {error}") from error
    if path.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400) or not stat.S_ISDIR(info.st_mode):
        raise ReleaseStateError("releases directory is unsafe")
    if created and os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError as error:
            raise ReleaseStateError(f"cannot protect local release state directory: {error}") from error
    return path


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseStateError(f"local state contains duplicate JSON key {key!r}")
        result[key] = value
    return result

def _read_json(path: Path, label: str) -> dict:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ReleaseStateError(f"cannot read {label}: {error}") from error
    if raw.startswith(b"\xef\xbb\xbf") or "\r" in text:
        raise ReleaseStateError(f"{label} must be UTF-8/LF without a BOM")
    try:
        value = json.loads(text, object_pairs_hook=_unique_pairs, parse_constant=lambda token: (_ for _ in ()).throw(ReleaseStateError(f"{label} contains invalid number {token}")))
    except ReleaseStateError:
        raise
    except json.JSONDecodeError as error:
        raise ReleaseStateError(f"{label} is invalid JSON: {error}") from error
    if not isinstance(value, dict) or _canonical_bytes(value) + b"\n" != raw:
        raise ReleaseStateError(f"{label} must be a canonical JSON object")
    return value


def _read_report_json(path: Path, label: str) -> dict:
    try:
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ReleaseStateError(f"cannot read {label}: {error}") from error
    if raw.startswith(b"\xef\xbb\xbf") or "\r" in text:
        raise ReleaseStateError(f"{label} must be UTF-8/LF without a BOM")
    try:
        value = json.loads(
            text, object_pairs_hook=_unique_pairs,
            parse_constant=lambda token: (_ for _ in ()).throw(ReleaseStateError(f"{label} contains invalid number {token}")),
        )
    except ReleaseStateError:
        raise
    except json.JSONDecodeError as error:
        raise ReleaseStateError(f"{label} is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ReleaseStateError(f"{label} must contain a JSON object")
    return value


def _number(value: object, label: str, *, nullable: bool = False) -> float | None:
    if nullable and value is None:
        return None
    if type(value) not in {int, float} or not math.isfinite(float(value)) or value < 0:
        raise ReleaseStateError(f"{label} must be a finite nonnegative number")
    return float(value)


def _validate_metrics(value: object) -> dict:
    if not isinstance(value, Mapping) or set(value) - METRIC_FIELDS:
        raise ReleaseStateError("attempt metrics contain unsupported fields")
    result = dict(value)
    for field in ("durationSeconds", "preparationSeconds", "objectSeconds"):
        if field in result:
            result[field] = _number(result[field], f"metric {field}")
    if "sqlclSessions" in result and (type(result["sqlclSessions"]) is not int or result["sqlclSessions"] < 0):
        raise ReleaseStateError("metric sqlclSessions must be a nonnegative integer")
    if "budgetSeconds" in result and (type(result["budgetSeconds"]) is not int or not 1 <= result["budgetSeconds"] <= 300):
        raise ReleaseStateError("metric budgetSeconds must be an integer from 1 through 300")
    for field in ("budgetCoverageComplete", "budgetPassed"):
        if field in result and type(result[field]) is not bool:
            raise ReleaseStateError(f"metric {field} must be boolean")
    if "includedCheckpoints" in result and (
        not isinstance(result["includedCheckpoints"], list)
        or any(type(item) is not int or item < 1 for item in result["includedCheckpoints"])
        or len(result["includedCheckpoints"]) != len(set(result["includedCheckpoints"]))
    ):
        raise ReleaseStateError("metric includedCheckpoints must contain unique positive integers")
    if "excludedObjectSteps" in result and (
        not isinstance(result["excludedObjectSteps"], list)
        or any(not isinstance(item, str) or not item for item in result["excludedObjectSteps"])
    ):
        raise ReleaseStateError("metric excludedObjectSteps must contain nonempty strings")
    if "noWriteClassification" in result and (
        not isinstance(result["noWriteClassification"], str)
        or result["noWriteClassification"] not in NO_WRITE_REASONS
    ):
        raise ReleaseStateError("metric noWriteClassification is unsupported")
    try:
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ReleaseStateError("attempt metrics are not JSON-safe") from error
    return result


def _validate_budget(value: object) -> dict:
    if not isinstance(value, Mapping) or set(value) != BUDGET_FIELDS:
        raise ReleaseStateError("attempt budget is malformed")
    seconds = value["seconds"]
    if seconds is not None and (type(seconds) is not int or not 1 <= seconds <= 300):
        raise ReleaseStateError("attempt budget seconds must be null or an integer from 1 through 300")
    elapsed = _number(value["elapsedSeconds"], "attempt budget elapsedSeconds", nullable=True)
    coverage = value["coverageComplete"]
    passed = value["passed"]
    if coverage is not None and type(coverage) is not bool:
        raise ReleaseStateError("attempt budget coverageComplete must be null or boolean")
    if passed is not None and type(passed) is not bool:
        raise ReleaseStateError("attempt budget passed must be null or boolean")
    if passed is True and (seconds is None or elapsed is None or coverage is not True or elapsed > seconds):
        raise ReleaseStateError("attempt budget cannot pass without complete in-budget coverage")
    return {"seconds": seconds, "elapsedSeconds": elapsed, "coverageComplete": coverage, "passed": passed}


def _validate_evidence(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise ReleaseStateError("attempt evidence must be an array")
    result = []
    seen = set()
    for row in value:
        if not isinstance(row, Mapping) or set(row) != EVIDENCE_FIELDS:
            raise ReleaseStateError("attempt evidence entry is malformed")
        path = _relative_path(row["path"], "attempt evidence path")
        if path in seen:
            raise ReleaseStateError("attempt evidence path is duplicated")
        seen.add(path)
        digest = row["sha256"]
        if digest is not None:
            _digest(digest, "attempt evidence hash")
        result.append({"path": path, "sha256": digest})
    return result


def _validate_target(row: Mapping) -> tuple[dict, dict]:
    if not isinstance(row.get("targetScope"), Mapping) or set(row["targetScope"]) != SCOPE_FIELDS:
        raise ReleaseStateError("attempt targetScope is malformed")
    scope = dict(row["targetScope"])
    for field in SCOPE_FIELDS:
        if not isinstance(scope[field], str) or not scope[field]:
            raise ReleaseStateError(f"attempt targetScope {field} is missing")
    if not isinstance(row.get("target"), Mapping) or set(row["target"]) != TARGET_FIELDS:
        raise ReleaseStateError("attempt target is malformed")
    target = dict(row["target"])
    if not isinstance(target["environment"], str) or target["environment"] not in ENVIRONMENTS:
        raise ReleaseStateError("attempt target environment is invalid")
    for field in ("connection", "expectedUser", "schema"):
        if not isinstance(target[field], str) or not target[field]:
            raise ReleaseStateError(f"attempt target {field} is missing")
    identity = target["identity"]
    if not isinstance(identity, Mapping) or set(identity) != IDENTITY_FIELDS:
        raise ReleaseStateError("attempt target identity is incomplete")
    identity = dict(identity)
    if any(not isinstance(value, str) or not value for value in identity.values()):
        raise ReleaseStateError("attempt target identity contains missing values")
    if identity["db_unique_name"].upper() != scope["dbUniqueName"]:
        raise ReleaseStateError("attempt target scope does not match observed database identity")
    if identity["container_id"] != scope["containerId"]:
        raise ReleaseStateError("attempt target scope does not match observed container identity")
    if identity["current_schema"].upper() != scope["schema"] or identity["edition"].upper() != scope["edition"]:
        raise ReleaseStateError("attempt target scope does not match observed schema or edition")
    if target["schema"].upper() != scope["schema"] or target["expectedUser"].upper() != identity["session_user"].upper():
        raise ReleaseStateError("attempt target descriptor does not match observed identity")
    return scope, {**target, "identity": identity}


def _validate_attempt(row: object) -> dict:
    if not isinstance(row, Mapping) or set(row) != ATTEMPT_FIELDS:
        raise ReleaseStateError("local attempt entry has unsupported or missing fields")
    result = dict(row)
    result["attemptId"] = _uuid(result["attemptId"], "attemptId")
    if not isinstance(result["environment"], str) or result["environment"] not in ENVIRONMENTS:
        raise ReleaseStateError("attempt environment must be staging or prod")
    scope, target = _validate_target(result)
    result["targetScope"] = scope
    result["target"] = target
    if target["environment"] != result["environment"]:
        raise ReleaseStateError("attempt environment does not match its target descriptor")
    if type(result["checkpoint"]) is not int or result["checkpoint"] < 1:
        raise ReleaseStateError("attempt checkpoint must be a positive one-based integer")
    result["sourceCommit"] = _commit(result["sourceCommit"])
    for field in ("payloadDigest", "engineDigest", "planDigest"):
        result[field] = _digest(result[field], f"attempt {field}")
    if not isinstance(result["state"], str) or result["state"] not in ATTEMPT_STATES:
        raise ReleaseStateError("attempt state is invalid")
    expected_write_possible = result["state"] != "not-started"
    if type(result["writePossible"]) is not bool or result["writePossible"] != expected_write_possible:
        raise ReleaseStateError("attempt writePossible does not match its state")
    started = _timestamp(result["startedAt"], "attempt startedAt")
    finished = _timestamp(result["finishedAt"], "attempt finishedAt", nullable=True)
    if result["state"] == "attempting" and finished is not None:
        raise ReleaseStateError("attempting state cannot have a finishedAt timestamp")
    if result["state"] != "attempting" and (finished is None or finished < started):
        raise ReleaseStateError("terminal attempt state must have a valid finishedAt timestamp")
    result["evidence"] = _validate_evidence(result["evidence"])
    result["metrics"] = _validate_metrics(result["metrics"])
    result["budget"] = _validate_budget(result["budget"])
    result["recoveryOf"] = _uuid(result["recoveryOf"], "recoveryOf", nullable=True)
    result["recoveredBy"] = _uuid(result["recoveredBy"], "recoveredBy", nullable=True)
    return result


def _validate_cleanup(row: object) -> dict:
    if not isinstance(row, Mapping) or set(row) != CLEANUP_FIELDS:
        raise ReleaseStateError("cleanup history entry has unsupported or missing fields")
    result = dict(row)
    result["cleanupId"] = _uuid(result["cleanupId"], "cleanupId")
    result["payloadDigest"] = _digest(result["payloadDigest"], "cleanup payloadDigest")
    _timestamp(result["requestedAt"], "cleanup requestedAt")
    if not isinstance(result["state"], str) or result["state"] not in CLEANUP_STATES:
        raise ReleaseStateError("cleanup history state is invalid")
    for field in ("targetPaths", "removedPaths"):
        if not isinstance(result[field], list):
            raise ReleaseStateError(f"cleanup {field} must be an array")
        paths = [_relative_path(item, f"cleanup {field}") for item in result[field]]
        if len(paths) != len(set(paths)):
            raise ReleaseStateError(f"cleanup {field} contains duplicate paths")
        result[field] = paths
    if not set(result["removedPaths"]).issubset(result["targetPaths"]):
        raise ReleaseStateError("cleanup removedPaths must be contained in targetPaths")
    if result["error"] is not None and (not isinstance(result["error"], str) or not result["error"]):
        raise ReleaseStateError("cleanup error must be null or a nonempty string")
    return result


def _validate_scope(value: object, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != SCOPE_FIELDS:
        raise ReleaseStateError(f"{label} is malformed")
    result = {}
    for field in sorted(SCOPE_FIELDS):
        item = value.get(field)
        if not isinstance(item, str) or not item or item != item.strip():
            raise ReleaseStateError(f"{label} {field} is missing")
        result[field] = item
    return result


def _validate_checkpoint_verification(row: object) -> dict:
    if not isinstance(row, Mapping) or set(row) != VERIFICATION_FIELDS:
        raise ReleaseStateError("checkpoint verification has unsupported or missing fields")
    result = dict(row)
    result["verificationId"] = _uuid(result["verificationId"], "verificationId")
    result["runId"] = _uuid(result["runId"], "verification runId")
    if result["environment"] not in ENVIRONMENTS:
        raise ReleaseStateError("checkpoint verification environment is invalid")
    if type(result["checkpoint"]) is not int or result["checkpoint"] < 1:
        raise ReleaseStateError("checkpoint verification number is invalid")
    result["targetScope"] = _validate_scope(result["targetScope"], "checkpoint verification target scope")
    result["sourceCommit"] = _commit(result["sourceCommit"])
    for field in ("payloadDigest", "engineDigest", "planDigest"):
        result[field] = _digest(result[field], f"checkpoint verification {field}")
    if result["mode"] not in {"applied", "no-op", "recovery"}:
        raise ReleaseStateError("checkpoint verification mode is invalid")
    result["attemptId"] = _uuid(result["attemptId"], "checkpoint verification attemptId", nullable=True)
    if result["mode"] == "no-op" and result["attemptId"] is not None:
        raise ReleaseStateError("no-op verification cannot claim a write attempt")
    if result["mode"] == "applied" and result["attemptId"] is None:
        raise ReleaseStateError("applied verification requires its write attempt")
    _timestamp(result["verifiedAt"], "checkpoint verification verifiedAt")
    result["evidence"] = _validate_evidence(result["evidence"])
    if not result["evidence"] or any(row["sha256"] is None for row in result["evidence"]):
        raise ReleaseStateError("checkpoint verification requires hashed evidence")
    result["objectSeconds"] = _number(result["objectSeconds"], "checkpoint verification objectSeconds")
    if type(result["sessionCount"]) is not int or result["sessionCount"] < 0:
        raise ReleaseStateError("checkpoint verification sessionCount is invalid")
    return result


def _validate_recovery_link(row: object) -> dict:
    if not isinstance(row, Mapping) or set(row) != RECOVERY_LINK_FIELDS:
        raise ReleaseStateError("recovery link has unsupported or missing fields")
    result = dict(row)
    result["oldAttemptId"] = _uuid(result["oldAttemptId"], "recovery link oldAttemptId")
    result["verificationId"] = _uuid(result["verificationId"], "recovery link verificationId")
    result["reconciliationEvidence"] = _relative_path(result["reconciliationEvidence"], "reconciliation evidence")
    _timestamp(result["linkedAt"], "recovery link linkedAt")
    return result


def _validate_step_outcome(row: object) -> dict:
    if not isinstance(row, Mapping) or set(row) != STEP_OUTCOME_FIELDS:
        raise ReleaseStateError("rollout step outcome has unsupported or missing fields")
    result = dict(row)
    if type(result["step"]) is not int or result["step"] < 1:
        raise ReleaseStateError("rollout step number is invalid")
    if result["status"] not in STEP_OUTCOME_STATES:
        raise ReleaseStateError("rollout step outcome status is invalid")
    result["receiptSha256"] = _digest(result["receiptSha256"], "rollout step receipt hash") if result["receiptSha256"] is not None else None
    result["receiptPath"] = _relative_path(result["receiptPath"], "rollout step receipt path") if result["receiptPath"] is not None else None
    if result["status"] == "verified" and (result["receiptSha256"] is None or result["receiptPath"] is None):
        raise ReleaseStateError("verified rollout step requires its receipt path and hash")
    if result["status"] != "verified" and (result["receiptSha256"] is not None or result["receiptPath"] is not None):
        raise ReleaseStateError("unverified rollout step cannot claim a successful receipt")
    return result


def _validate_rollout(row: object) -> dict:
    if not isinstance(row, Mapping) or set(row) != ROLLOUT_FIELDS:
        raise ReleaseStateError("rollout record has unsupported or missing fields")
    result = dict(row)
    result["runId"] = _uuid(result["runId"], "rollout runId")
    if result["environment"] not in ENVIRONMENTS:
        raise ReleaseStateError("rollout environment is invalid")
    result["sourceCommit"] = _commit(result["sourceCommit"])
    for field in ("payloadDigest", "engineDigest", "planDigest"):
        result[field] = _digest(result[field], f"rollout {field}")
    if not isinstance(result["targetScopes"], list) or not result["targetScopes"]:
        raise ReleaseStateError("rollout targetScopes must be a nonempty array")
    result["targetScopes"] = [_validate_scope(scope, "rollout target scope") for scope in result["targetScopes"]]
    scope_keys = [_canonical_bytes(scope) for scope in result["targetScopes"]]
    if scope_keys != sorted(scope_keys) or len(scope_keys) != len(set(scope_keys)):
        raise ReleaseStateError("rollout targetScopes must be unique and sorted")
    if result["state"] not in ROLLOUT_STATES:
        raise ReleaseStateError("rollout state is invalid")
    if result["databaseState"] not in {"not-started", "verified", "unknown", "failed"}:
        raise ReleaseStateError("rollout databaseState is invalid")
    started = _timestamp(result["startedAt"], "rollout startedAt")
    finished = _timestamp(result["finishedAt"], "rollout finishedAt", nullable=True)
    if result["state"] == "running" and finished is not None:
        raise ReleaseStateError("running rollout cannot have finishedAt")
    if result["state"] != "running" and (finished is None or finished < started):
        raise ReleaseStateError("terminal rollout must have a valid finishedAt")
    for field in ("expectedCheckpointCount", "expectedStepCount"):
        if type(result[field]) is not int or result[field] < 1:
            raise ReleaseStateError(f"rollout {field} must be a positive integer")
    if not isinstance(result["steps"], list):
        raise ReleaseStateError("rollout steps must be an array")
    result["steps"] = [_validate_step_outcome(item) for item in result["steps"]]
    step_numbers = [item["step"] for item in result["steps"]]
    if step_numbers != sorted(set(step_numbers)) or (step_numbers and step_numbers != list(range(1, len(step_numbers) + 1))):
        raise ReleaseStateError("rollout step outcomes must be ordered and consecutive")
    if not isinstance(result["checkpointVerificationIds"], list):
        raise ReleaseStateError("rollout checkpointVerificationIds must be an array")
    result["checkpointVerificationIds"] = [
        _uuid(item, "rollout checkpointVerificationId") for item in result["checkpointVerificationIds"]
    ]
    if len(result["checkpointVerificationIds"]) != len(set(result["checkpointVerificationIds"])):
        raise ReleaseStateError("rollout checkpoint verification IDs are duplicated")
    result["budget"] = _validate_budget(result["budget"])
    result["finalReportPath"] = _relative_path(result["finalReportPath"], "rollout final report path") if result["finalReportPath"] is not None else None
    result["finalReportSha256"] = _digest(result["finalReportSha256"], "rollout finalReportSha256") if result["finalReportSha256"] is not None else None
    if result["state"] == "verified":
        if (
            len(result["checkpointVerificationIds"]) != result["expectedCheckpointCount"]
            or len(result["steps"]) != result["expectedStepCount"]
            or any(step["status"] != "verified" for step in result["steps"])
            or result["databaseState"] != "verified"
            or result["budget"]["passed"] is not True
            or result["finalReportPath"] is None
            or result["finalReportSha256"] is None
        ):
            raise ReleaseStateError("verified rollout lacks complete checkpoint, step, budget, or report evidence")
    return result


def _validate_migration_provenance(value: object) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != MIGRATION_PROVENANCE_FIELDS:
        raise ReleaseStateError("schema migration provenance has unsupported or missing fields")
    result = dict(value)
    digest = _digest(result["schema1Sha256"], "schema migration hash")
    expected_path = f"releases/.local-state-history/{digest}.json"
    if result["retainedPath"] != expected_path:
        raise ReleaseStateError("schema migration provenance path does not match the original hash")
    _timestamp(result["migratedAt"], "schema migration timestamp")
    return result


def _validate_legacy_index(value: object) -> dict:
    if not isinstance(value, Mapping) or set(value) != LEGACY_INDEX_FIELDS:
        raise ReleaseStateError("legacy local release state index has unsupported or missing fields")
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != LEGACY_INDEX_SCHEMA_VERSION:
        raise ReleaseStateError("legacy local release state schemaVersion is unsupported")
    if not isinstance(value["attempts"], list) or not isinstance(value["cleanupHistory"], list):
        raise ReleaseStateError("legacy local release state arrays are malformed")
    attempts = [_validate_attempt(item) for item in value["attempts"]]
    ids = [row["attemptId"] for row in attempts]
    if len(ids) != len(set(ids)):
        raise ReleaseStateError("local release state contains duplicate attempt IDs")
    by_id = {row["attemptId"]: row for row in attempts}
    for row in attempts:
        if row["recoveryOf"] is not None:
            old = by_id.get(row["recoveryOf"])
            if old is None or old["state"] != "unknown":
                raise ReleaseStateError("attempt recoveryOf must reference an existing unknown attempt")
            if old["environment"] != row["environment"] or old["targetScope"] != row["targetScope"] or old["payloadDigest"] != row["payloadDigest"]:
                raise ReleaseStateError("attempt recovery link does not match the old target and payload")
        if row["recoveredBy"] is not None:
            recovery = by_id.get(row["recoveredBy"])
            if row["state"] != "unknown" or recovery is None or recovery["recoveryOf"] != row["attemptId"] or recovery["state"] != "verified":
                raise ReleaseStateError("attempt recoveredBy must reference a verified recovery")
    cleanup = [_validate_cleanup(item) for item in value["cleanupHistory"]]
    cleanup_ids = [row["cleanupId"] for row in cleanup]
    if len(cleanup_ids) != len(set(cleanup_ids)):
        raise ReleaseStateError("local release state contains duplicate cleanup IDs")
    return {"schemaVersion": LEGACY_INDEX_SCHEMA_VERSION, "attempts": attempts, "cleanupHistory": cleanup}


def _validate_index(value: object) -> dict:
    if not isinstance(value, Mapping) or set(value) != INDEX_FIELDS:
        raise ReleaseStateError("local release state index has unsupported or missing fields")
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != INDEX_SCHEMA_VERSION:
        raise ReleaseStateError("local release state schemaVersion is unsupported")
    array_fields = ("attempts", "cleanupHistory", "checkpointVerifications", "recoveryLinks", "rollouts")
    if not all(isinstance(value[field], list) for field in array_fields):
        raise ReleaseStateError("local release state arrays are malformed")
    attempts = [_validate_attempt(item) for item in value["attempts"]]
    cleanup = [_validate_cleanup(item) for item in value["cleanupHistory"]]
    verifications = [_validate_checkpoint_verification(item) for item in value["checkpointVerifications"]]
    recovery_links = [_validate_recovery_link(item) for item in value["recoveryLinks"]]
    rollouts = [_validate_rollout(item) for item in value["rollouts"]]
    provenance = _validate_migration_provenance(value["migrationProvenance"])
    for rows, key, label in (
        (attempts, "attemptId", "attempt"), (cleanup, "cleanupId", "cleanup"),
        (verifications, "verificationId", "checkpoint verification"),
        (rollouts, "runId", "rollout"),
    ):
        values = [row[key] for row in rows]
        if len(values) != len(set(values)):
            raise ReleaseStateError(f"local release state contains duplicate {label} IDs")
    attempt_by_id = {row["attemptId"]: row for row in attempts}
    verification_by_id = {row["verificationId"]: row for row in verifications}
    rollout_by_id = {row["runId"]: row for row in rollouts}
    for row in attempts:
        if row["recoveryOf"] is not None:
            old = attempt_by_id.get(row["recoveryOf"])
            if old is None or old["state"] not in {"unknown", "attempting"}:
                raise ReleaseStateError("attempt recoveryOf must reference an unresolved attempt")
            if old["environment"] != row["environment"] or old["targetScope"] != row["targetScope"] or old["payloadDigest"] != row["payloadDigest"]:
                raise ReleaseStateError("attempt recovery link does not match the old target and payload")
        if row["recoveredBy"] is not None:
            recovery = attempt_by_id.get(row["recoveredBy"])
            if row["state"] not in {"unknown", "attempting"} or recovery is None or recovery["recoveryOf"] != row["attemptId"] or recovery["state"] != "verified":
                raise ReleaseStateError("attempt recoveredBy must reference a verified recovery")
    for row in verifications:
        run = rollout_by_id.get(row["runId"])
        if run is None or any(
            row[field] != run[field]
            for field in ("environment", "sourceCommit", "payloadDigest", "engineDigest", "planDigest")
        ) or row["targetScope"] not in run["targetScopes"]:
            raise ReleaseStateError("checkpoint verification does not match its rollout record")
        if row["checkpoint"] > run["expectedCheckpointCount"]:
            raise ReleaseStateError("checkpoint verification exceeds its rollout checkpoint count")
        if row["attemptId"] is not None:
            attempt = attempt_by_id.get(row["attemptId"])
            if (
                attempt is None or attempt["state"] != "verified"
                or attempt["environment"] != row["environment"]
                or attempt["targetScope"] != row["targetScope"]
                or attempt["checkpoint"] != row["checkpoint"]
                or attempt["payloadDigest"] != row["payloadDigest"]
                or attempt["engineDigest"] != row["engineDigest"]
                or attempt["planDigest"] != row["planDigest"]
            ):
                raise ReleaseStateError("checkpoint verification does not match its verified write attempt")
    for run in rollouts:
        ids = run["checkpointVerificationIds"]
        linked = [verification_by_id.get(identifier) for identifier in ids]
        if any(row is None or row["runId"] != run["runId"] for row in linked):
            raise ReleaseStateError("rollout checkpoint verification references are incomplete")
        if len({row["checkpoint"] for row in linked if row is not None}) != len(linked):
            raise ReleaseStateError("rollout contains duplicate checkpoint verification numbers")
        all_run_verifications = {row["verificationId"] for row in verifications if row["runId"] == run["runId"]}
        if set(ids) != all_run_verifications:
            raise ReleaseStateError("rollout checkpoint verification references do not cover its saved receipts")
        if run["state"] == "verified" and (
            len(ids) != run["expectedCheckpointCount"]
            or {row["checkpoint"] for row in linked} != set(range(1, run["expectedCheckpointCount"] + 1))
            or len(run["steps"]) != run["expectedStepCount"]
        ):
            raise ReleaseStateError("verified rollout has incomplete checkpoint or step records")
    link_old_ids = []
    for link in recovery_links:
        old = attempt_by_id.get(link["oldAttemptId"])
        verification = verification_by_id.get(link["verificationId"])
        if (
            old is None or old["state"] not in {"unknown", "attempting"}
            or verification is None or verification["mode"] != "recovery"
            or verification["targetScope"] != old["targetScope"]
            or verification["payloadDigest"] != old["payloadDigest"]
            or verification["checkpoint"] != old["checkpoint"]
            or link["reconciliationEvidence"] not in {item["path"] for item in verification["evidence"]}
        ):
            raise ReleaseStateError("recovery link does not match its old attempt and verification")
        link_old_ids.append(link["oldAttemptId"])
    if len(link_old_ids) != len(set(link_old_ids)):
        raise ReleaseStateError("an old attempt has more than one recovery link")
    linked_verifications = {link["verificationId"] for link in recovery_links}
    if any(
        row["mode"] == "recovery" and row["verificationId"] not in linked_verifications
        for row in verifications
    ):
        raise ReleaseStateError("recovery checkpoint verification is missing its reconciliation link")
    return {
        "schemaVersion": INDEX_SCHEMA_VERSION,
        "attempts": attempts,
        "cleanupHistory": cleanup,
        "checkpointVerifications": verifications,
        "recoveryLinks": recovery_links,
        "rollouts": rollouts,
        "migrationProvenance": provenance,
    }


def _index_path(root: Path, *, create: bool = False) -> Path:
    directory = _release_directory(root, create=create)
    path = directory / ".local-state.json"
    _checked_path(root, "releases/.local-state.json")
    return path


def _empty_index() -> dict:
    return {
        "schemaVersion": INDEX_SCHEMA_VERSION,
        "attempts": [], "cleanupHistory": [], "checkpointVerifications": [],
        "recoveryLinks": [], "rollouts": [], "migrationProvenance": None,
    }


def _load_index_with_migration(root: Path) -> tuple[dict, bytes | None]:
    path = _index_path(root)
    if not path.exists():
        return _empty_index(), None
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ReleaseStateError(f"cannot read local release state index: {error}") from error
    value = _read_json(path, "local release state index")
    try:
        if path.read_bytes() != raw:
            raise ReleaseStateError("local release state index changed while it was being read")
    except OSError as error:
        raise ReleaseStateError(f"cannot reread local release state index: {error}") from error
    if value.get("schemaVersion") == LEGACY_INDEX_SCHEMA_VERSION:
        legacy = _validate_legacy_index(value)
        digest = _hash(raw)
        index = {
            "schemaVersion": INDEX_SCHEMA_VERSION,
            "attempts": legacy["attempts"],
            "cleanupHistory": legacy["cleanupHistory"],
            "checkpointVerifications": [],
            "recoveryLinks": [],
            "rollouts": [],
            "migrationProvenance": {
                "schema1Sha256": digest,
                "retainedPath": f"releases/.local-state-history/{digest}.json",
                "migratedAt": _utc_now(),
            },
        }
        return _validate_index(index), raw
    index = _validate_index(value)
    provenance = index["migrationProvenance"]
    if provenance is not None:
        retained = _checked_path(root, provenance["retainedPath"], allow_missing=False)
        try:
            retained_bytes = retained.read_bytes()
        except OSError as error:
            raise ReleaseStateError(f"retained schema-1 release state is unavailable: {error}") from error
        if _hash(retained_bytes) != provenance["schema1Sha256"]:
            raise ReleaseStateError("retained schema-1 release state hash does not match its provenance")
        _validate_legacy_index(_read_json(retained, "retained schema-1 release state"))
    return index, None


def _load_index(root: Path) -> dict:
    return _load_index_with_migration(root)[0]


def _retain_legacy_index(root: Path, index: Mapping, raw: bytes) -> None:
    provenance = index.get("migrationProvenance")
    if not isinstance(provenance, Mapping):
        raise ReleaseStateError("schema-1 migration provenance is missing")
    relative = provenance.get("retainedPath")
    if not isinstance(relative, str):
        raise ReleaseStateError("schema-1 retained path is malformed")
    destination = _checked_path(root, relative)
    directory = destination.parent
    try:
        directory.mkdir(mode=0o700, parents=False, exist_ok=True)
        metadata = directory.lstat()
    except OSError as error:
        raise ReleaseStateError(f"cannot create retained state history: {error}") from error
    if directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise ReleaseStateError("retained state history directory is unsafe")
    if os.name != "nt":
        directory.chmod(0o700)
    if destination.exists():
        try:
            existing = destination.read_bytes()
        except OSError as error:
            raise ReleaseStateError(f"cannot verify retained schema-1 state: {error}") from error
        if existing != raw or _hash(existing) != provenance["schema1Sha256"]:
            raise ReleaseStateError("retained schema-1 state already exists with different bytes")
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(destination, flags, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        existing = destination.read_bytes()
        if existing != raw:
            raise ReleaseStateError("retained schema-1 state raced with different bytes") from None
    except OSError as error:
        raise ReleaseStateError(f"cannot retain original schema-1 state: {error}") from error
    if _hash(destination.read_bytes()) != provenance["schema1Sha256"]:
        raise ReleaseStateError("retained schema-1 state failed its hash check")


def _write_index(root: Path, index: Mapping) -> None:
    checked = _validate_index(index)
    directory = _release_directory(root, create=True)
    path = directory / ".local-state.json"
    _checked_path(root, "releases/.local-state.json")
    fd = -1
    temp_name = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=".local-state.", suffix=".tmp", dir=directory)
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(_canonical_bytes(checked) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        temp_name = None
        if os.name != "nt":
            path.chmod(0o600)
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except OSError as error:
        raise ReleaseStateError(f"cannot atomically write local release state: {error}") from error
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


@contextlib.contextmanager
def _writer_lock(root: Path) -> Iterator[None]:
    directory = _release_directory(root, create=True)
    lock_path = directory / ".local-state.lock"
    _checked_path(root, "releases/.local-state.lock")
    token = str(uuid.uuid4())
    payload = _canonical_bytes({"pid": os.getpid(), "token": token, "createdAt": _utc_now()}) + b"\n"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise ReleaseStateError("local release state writer lock exists; reconcile it explicitly before retrying") from error
    except OSError as error:
        raise ReleaseStateError(f"cannot acquire local release state writer lock: {error}") from error
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        try:
            current = _read_json(lock_path, "local release state writer lock")
            if current.get("token") != token:
                raise ReleaseStateError("local release state writer lock ownership changed; leave it for reconciliation")
            lock_path.unlink()
        except FileNotFoundError as error:
            raise ReleaseStateError("local release state writer lock disappeared; leave state for reconciliation") from error


def _atomic_update(root: Path, update) -> dict:
    with _writer_lock(root):
        index, legacy_raw = _load_index_with_migration(root)
        updated = update(index)
        if legacy_raw is not None:
            _retain_legacy_index(root, updated, legacy_raw)
        _write_index(root, updated)
        return updated


def _physical_scope(identity: Mapping[str, str]) -> dict[str, str]:
    return {
        "dbUniqueName": identity["db_unique_name"].upper(),
        "containerId": identity["container_id"],
        "schema": identity["current_schema"].upper(),
        "edition": identity["edition"].upper(),
    }


def target_scope_from_identity(identity: Mapping[str, str]) -> dict[str, str]:
    observed = complete_observed_identity(identity)
    if observed is None:
        raise ReleaseStateError("target writer lease requires a complete observed target identity")
    return _physical_scope(observed)


def _attempt_plan(capsule: ReleaseCapsule, plan: TargetPlan, checkpoint: int) -> tuple[dict, dict, dict]:
    if not isinstance(getattr(plan, "environment", None), str) or plan.environment not in ENVIRONMENTS:
        raise ReleaseStateError("attempt plan environment must be staging or prod")
    if getattr(plan, "payload_digest", None) != capsule.payload_digest:
        raise ReleaseStateError("attempt plan payload does not match the capsule")
    for field, value in (
        ("payloadDigest", capsule.payload_digest), ("engineDigest", capsule.engine_digest),
        ("planDigest", getattr(plan, "plan_digest", None)),
    ):
        _digest(value, f"attempt {field}")
    _commit(capsule.source_commit)
    if type(checkpoint) is not int or checkpoint < 1 or checkpoint > len(capsule.checkpoints):
        raise ReleaseStateError("attempt checkpoint is outside the capsule")
    guards = getattr(plan, "checkpoint_guards", None)
    if not isinstance(guards, (list, tuple)):
        raise ReleaseStateError("attempt plan checkpoint guards are malformed")
    matches = [guard for guard in guards if isinstance(guard, Mapping) and guard.get("checkpoint") == checkpoint]
    if len(matches) != 1:
        raise ReleaseStateError("attempt plan must contain exactly one matching checkpoint guard")
    guard = matches[0]
    checkpoint_schema = getattr(capsule.checkpoints[checkpoint - 1], "schema", None)
    if not isinstance(checkpoint_schema, str) or not checkpoint_schema:
        raise ReleaseStateError("capsule checkpoint schema is missing")
    if not isinstance(guard.get("schema"), str) or guard["schema"].upper() != checkpoint_schema.upper():
        raise ReleaseStateError("attempt plan checkpoint schema does not match the capsule")
    key = guard.get("targetKey")
    targets = getattr(plan, "targets", None)
    if not isinstance(key, str) or not key.startswith("schema:") or not isinstance(targets, Mapping) or key not in targets:
        raise ReleaseStateError("attempt checkpoint target descriptor is missing")
    target = targets[key]
    if not isinstance(target, Mapping) or target.get("environment") != plan.environment:
        raise ReleaseStateError("attempt target descriptor does not match its plan")
    observed = complete_observed_identity(guard.get("targetIdentity"))
    target_observed = complete_observed_identity(target.get("observed"))
    if observed is None or target_observed is None or observed != target_observed:
        raise ReleaseStateError("attempt target identity is incomplete or differs from its checkpoint guard")
    expected_user = target.get("expectedUser")
    schema = target.get("schema")
    if (
        not isinstance(expected_user, str) or not isinstance(schema, str)
        or observed["session_user"].upper() != expected_user.upper()
        or observed["current_schema"].upper() != schema.upper()
        or schema.upper() != checkpoint_schema.upper()
        or key != f"schema:{schema}"
    ):
        raise ReleaseStateError("attempt target descriptor does not match its observed schema identity")
    scope = _physical_scope(observed)
    descriptor = {
        "environment": plan.environment,
        "connection": target.get("connection"),
        "expectedUser": expected_user,
        "schema": schema,
        "identity": observed,
    }
    if not isinstance(descriptor["connection"], str) or not descriptor["connection"]:
        raise ReleaseStateError("attempt target connection alias is missing")
    recovery = getattr(plan, "recovery_attempt", None)
    recovery_id = None
    if recovery is not None:
        _uuid(recovery, "recovery attempt")
        binding = getattr(plan, "recovery_binding", None)
        if not isinstance(binding, Mapping) or binding.get("attemptId") != recovery:
            raise ReleaseStateError("recovery attempt is missing its terminal reconciliation binding")
        binding_checkpoint = binding.get("checkpoint")
        if type(binding_checkpoint) is not int or binding_checkpoint < 1 or binding_checkpoint > len(capsule.checkpoints):
            raise ReleaseStateError("recovery plan checkpoint binding is malformed")
        if checkpoint == binding_checkpoint:
            recovery_id = recovery
    return scope, descriptor, {"checkpoint": checkpoint, "recoveryOf": recovery_id}


def _assert_owned_lease(root: Path, capsule: ReleaseCapsule, plan: TargetPlan, checkpoint: int, scope: Mapping, run_id: str, lease) -> None:
    expected_identity = {
        "runId": run_id,
        "environment": plan.environment,
        "payloadDigest": capsule.payload_digest,
        "engineDigest": capsule.engine_digest,
        "planDigest": plan.plan_digest,
        "checkpoint": checkpoint,
        "recoveryAttempt": getattr(plan, "recovery_attempt", None),
    }
    if (
        not isinstance(lease, release_ownership.TargetWriterLease)
        or lease.root != root
        or lease.target_scope != dict(scope)
        or lease._fd is None
        or lease.execution_identity != expected_identity
    ):
        raise ReleaseStateError("checkpoint operation requires its active run-bound target writer lease")


def _assert_checkpoint_allowed_index(
    index: Mapping,
    capsule: ReleaseCapsule,
    plan: TargetPlan,
    checkpoint: int,
    run_id: str,
    scope: Mapping,
    attempt_id: str | None = None,
) -> None:
    run = next((row for row in index["rollouts"] if row["runId"] == run_id), None)
    if (
        run is None or run["state"] != "running"
        or run["environment"] != plan.environment
        or run["sourceCommit"] != capsule.source_commit
        or run["payloadDigest"] != capsule.payload_digest
        or run["engineDigest"] != capsule.engine_digest
        or run["planDigest"] != plan.plan_digest
        or scope not in run["targetScopes"]
    ):
        raise ReleaseStateError("checkpoint does not belong to the active rollout run and prepared target")
    if any(row["runId"] == run_id and row["checkpoint"] == checkpoint for row in index["checkpointVerifications"]):
        raise ReleaseStateError("this checkpoint already has a durable verification in the active rollout")
    matching = [row for row in index["rollouts"] if row["state"] == "running" and row["runId"] != run_id and scope in row["targetScopes"]]
    if matching:
        raise ReleaseStateError("another active rollout owns this physical target scope")
    recovery_id = _attempt_plan(capsule, plan, checkpoint)[2]["recoveryOf"]
    unresolved = [
        row for row in index["attempts"]
        if row["targetScope"] == scope
        and _is_unresolved(row, index["attempts"], index["recoveryLinks"], index["checkpointVerifications"])
        and row["attemptId"] not in {recovery_id, attempt_id}
    ]
    if unresolved:
        raise ReleaseStateError("an unresolved attempt occupies this physical target; reconcile before checkpoint verification")


def assert_checkpoint_allowed(
    repo_root: Path,
    capsule: ReleaseCapsule,
    plan: TargetPlan,
    checkpoint: int,
    *,
    run_id: str,
    lease: release_ownership.TargetWriterLease,
) -> None:
    """Check run ownership and durable occupancy before applying or accepting a no-op."""
    root = _root(repo_root)
    _uuid(run_id, "rollout runId")
    scope, _target, _details = _attempt_plan(capsule, plan, checkpoint)
    _assert_owned_lease(root, capsule, plan, checkpoint, scope, run_id, lease)
    with _writer_lock(root):
        index, _legacy_raw = _load_index_with_migration(root)
        _assert_checkpoint_allowed_index(index, capsule, plan, checkpoint, run_id, scope)


def _is_unresolved(
    row: Mapping,
    attempts: Sequence[Mapping],
    recovery_links: Sequence[Mapping] = (),
    verifications: Sequence[Mapping] = (),
) -> bool:
    if row["state"] not in {"attempting", "unknown"}:
        return False
    recovery_id = row.get("recoveredBy")
    if recovery_id is not None:
        recovery = next((item for item in attempts if item["attemptId"] == recovery_id), None)
        if recovery is not None and recovery["state"] == "verified" and recovery.get("recoveryOf") == row["attemptId"]:
            return False
    recovery = next((item for item in attempts if item.get("recoveryOf") == row["attemptId"]), None)
    if recovery is not None and recovery["state"] == "verified":
        return False
    links = [link for link in recovery_links if link.get("oldAttemptId") == row["attemptId"]]
    return not any(
        any(verification.get("verificationId") == link.get("verificationId") for verification in verifications)
        for link in links
    )


def record_attempt(
    repo_root: Path,
    capsule: ReleaseCapsule,
    plan: TargetPlan,
    checkpoint: int,
    *,
    run_id: str,
    lease: release_ownership.TargetWriterLease,
) -> str:
    """Persist a guarded checkpoint attempt before any possible database write."""
    root = _root(repo_root)
    scope, target, details = _attempt_plan(capsule, plan, checkpoint)
    _uuid(run_id, "rollout runId")
    _assert_owned_lease(root, capsule, plan, checkpoint, scope, run_id, lease)
    recovery_id = details["recoveryOf"]
    if recovery_id is not None:
        validate_recovery_plan(root, capsule, plan)
    attempt_id = str(uuid.uuid4())
    budget_seconds = getattr(capsule, "database_object_budget_seconds", None)
    if budget_seconds is not None and (type(budget_seconds) is not int or not 1 <= budget_seconds <= 300):
        raise ReleaseStateError("capsule database object budget must be an integer from 1 through 300")

    def update(index: dict) -> dict:
        _assert_checkpoint_allowed_index(index, capsule, plan, checkpoint, run_id, scope)
        rows = index["attempts"]
        if recovery_id is not None:
            old = next((row for row in rows if row["attemptId"] == recovery_id), None)
            if (
                old is None or old["state"] not in {"unknown", "attempting"} or old["recoveredBy"] is not None
                or old["targetScope"] != scope or old["environment"] != plan.environment
                or old["payloadDigest"] != capsule.payload_digest
                or old["engineDigest"] != capsule.engine_digest
                or old["sourceCommit"] != capsule.source_commit
                or old["checkpoint"] != checkpoint
                or old["state"] != plan.recovery_binding["attemptState"]
            ):
                raise ReleaseStateError("recovery must name one unresolved attempt for the same target, payload, and checkpoint")
            other = [row for row in rows if row["targetScope"] == scope and _is_unresolved(row, rows, index["recoveryLinks"], index["checkpointVerifications"]) and row["attemptId"] != recovery_id]
            if other:
                raise ReleaseStateError("another unresolved attempt occupies the recovery target")
        row = {
            "attemptId": attempt_id,
            "environment": plan.environment,
            "targetScope": scope,
            "target": target,
            "checkpoint": checkpoint,
            "sourceCommit": capsule.source_commit,
            "payloadDigest": capsule.payload_digest,
            "engineDigest": capsule.engine_digest,
            "planDigest": plan.plan_digest,
            "state": "attempting",
            "writePossible": True,
            "startedAt": _utc_now(),
            "finishedAt": None,
            "evidence": [],
            "metrics": {},
            "budget": {"seconds": budget_seconds, "elapsedSeconds": None, "coverageComplete": None, "passed": None},
            "recoveryOf": recovery_id,
            "recoveredBy": None,
        }
        rows.append(row)
        return index

    _atomic_update(root, update)
    return attempt_id


def assert_auxiliary_step_allowed(
    repo_root: Path,
    capsule: ReleaseCapsule,
    plan: TargetPlan,
    step_number: int,
    *,
    run_id: str,
    lease: release_ownership.TargetWriterLease,
) -> None:
    """Require the active rollout, prepared target, and writer lease for SQL/ORDS writes."""
    root = _root(repo_root)
    _uuid(run_id, "rollout runId")
    if type(step_number) is not int or step_number < 1:
        raise ReleaseStateError("auxiliary rollout step number must be positive")
    step_definitions = _capsule_step_definitions(capsule)
    if step_number > len(step_definitions):
        raise ReleaseStateError("auxiliary rollout step is outside the frozen step list")
    step = step_definitions[step_number - 1]
    if step.get("type") not in {"sql-script", "ords-import"}:
        raise ReleaseStateError("writer lease was requested for a non-SQL/ORDS step")
    scope = _auxiliary_target_scope(plan, step_number, step)
    execution_identity = {
        "purpose": "rollout-auxiliary-step",
        "runId": run_id,
        "step": step_number,
        "planDigest": plan.plan_digest,
    }
    if (
        not isinstance(lease, release_ownership.TargetWriterLease)
        or lease.root != root or lease.target_scope != scope or lease._fd is None
        or lease.execution_identity != execution_identity
    ):
        raise ReleaseStateError("SQL/ORDS operation requires its active run-bound target writer lease")

    def update(index: dict) -> dict:
        run = next((row for row in index["rollouts"] if row["runId"] == run_id), None)
        if (
            run is None or run["state"] != "running"
            or run["environment"] != plan.environment
            or run["sourceCommit"] != capsule.source_commit
            or run["payloadDigest"] != capsule.payload_digest
            or run["engineDigest"] != capsule.engine_digest
            or run["planDigest"] != plan.plan_digest
            or scope not in run["targetScopes"]
        ):
            raise ReleaseStateError("SQL/ORDS step does not belong to the active rollout and prepared target")
        if any(
            row["runId"] != run_id and row["state"] == "running" and scope in row["targetScopes"]
            for row in index["rollouts"]
        ):
            raise ReleaseStateError("another active rollout owns this physical target scope")
        if any(
            attempt["targetScope"] == scope
            and _is_unresolved(attempt, index["attempts"], index["recoveryLinks"], index["checkpointVerifications"])
            for attempt in index["attempts"]
        ):
            raise ReleaseStateError("an unresolved attempt occupies this physical target; reconcile before auxiliary write")
        return index

    _atomic_update(root, update)


def record_checkpoint_verification(
    repo_root: Path,
    capsule: ReleaseCapsule,
    plan: TargetPlan,
    checkpoint: int,
    *,
    run_id: str,
    attempt_id: str | None,
    lease: release_ownership.TargetWriterLease,
    evidence: Sequence[str],
    metrics: Mapping,
    finish_attempt_metrics: Mapping | None = None,
) -> str:
    """Persist successful checkpoint checks, including no-op and recovery observations."""
    root = _root(repo_root)
    _uuid(run_id, "rollout runId")
    scope, _target, details = _attempt_plan(capsule, plan, checkpoint)
    _assert_owned_lease(root, capsule, plan, checkpoint, scope, run_id, lease)
    recovery_id = details["recoveryOf"]
    mode = "recovery" if recovery_id is not None else ("applied" if attempt_id is not None else "no-op")
    if not isinstance(metrics, Mapping) or set(metrics) != {"objectSeconds", "sessionCount"}:
        raise ReleaseStateError("checkpoint verification metrics must contain objectSeconds and sessionCount")
    object_seconds = _number(metrics["objectSeconds"], "checkpoint verification objectSeconds")
    session_count = metrics["sessionCount"]
    if type(session_count) is not int or session_count < 0:
        raise ReleaseStateError("checkpoint verification sessionCount is invalid")
    if mode == "applied" and attempt_id is None:
        raise ReleaseStateError("applied checkpoint verification requires its write attempt")
    if mode != "applied" and mode != "recovery" and attempt_id is not None:
        raise ReleaseStateError("no-op checkpoint verification cannot name a write attempt")
    clean_attempt_metrics = None
    if finish_attempt_metrics is not None:
        if attempt_id is None:
            raise ReleaseStateError("attempt finalization metrics require a write attempt")
        clean_attempt_metrics = _validate_metrics(finish_attempt_metrics)
    verification_id = str(uuid.uuid4())

    def update(index: dict) -> dict:
        _assert_checkpoint_allowed_index(index, capsule, plan, checkpoint, run_id, scope, attempt_id)
        attempt = None
        if attempt_id is not None:
            _uuid(attempt_id, "checkpoint verification attemptId")
            attempt = next((row for row in index["attempts"] if row["attemptId"] == attempt_id), None)
            if clean_attempt_metrics is not None:
                if attempt is None or attempt["state"] != "attempting":
                    raise ReleaseStateError("atomic checkpoint verification requires its attempting write record")
                _finish_attempt_in_index(
                    root, index, attempt, state="verified", evidence=evidence,
                    clean_metrics=clean_attempt_metrics,
                )
            if (
                attempt is None or attempt["state"] != "verified"
                or attempt["environment"] != plan.environment or attempt["targetScope"] != scope
                or attempt["checkpoint"] != checkpoint or attempt["sourceCommit"] != capsule.source_commit
                or attempt["payloadDigest"] != capsule.payload_digest
                or attempt["engineDigest"] != capsule.engine_digest
                or attempt["planDigest"] != plan.plan_digest
            ):
                raise ReleaseStateError("checkpoint verification does not match its verified write attempt")
        evidence_rows = _evidence_rows(root, evidence, require_files=True)
        if attempt is not None and evidence_rows != attempt["evidence"]:
            raise ReleaseStateError("checkpoint verification evidence differs from the terminal write-attempt evidence")
        recovery_references = []
        if recovery_id is not None:
            old = next((row for row in index["attempts"] if row["attemptId"] == recovery_id), None)
            verified_recovery = (
                attempt is not None and attempt.get("recoveryOf") == recovery_id
                and attempt["state"] == "verified"
            )
            if (
                old is None or old["state"] not in {"unknown", "attempting"}
                or (
                    not _is_unresolved(old, index["attempts"], index["recoveryLinks"], index["checkpointVerifications"])
                    and not verified_recovery
                )
            ):
                raise ReleaseStateError("recovery target changed before its checkpoint verification")
            if not evidence_rows:
                raise ReleaseStateError("recovery verification requires reconciliation evidence")
            recovery_references.append({
                "oldAttemptId": recovery_id,
                "verificationId": verification_id,
                "reconciliationEvidence": evidence_rows[0]["path"],
                "linkedAt": _utc_now(),
            })
        verification = {
            "verificationId": verification_id,
            "runId": run_id,
            "environment": plan.environment,
            "checkpoint": checkpoint,
            "targetScope": scope,
            "sourceCommit": capsule.source_commit,
            "payloadDigest": capsule.payload_digest,
            "engineDigest": capsule.engine_digest,
            "planDigest": plan.plan_digest,
            "mode": mode,
            "attemptId": attempt_id,
            "verifiedAt": _utc_now(),
            "evidence": evidence_rows,
            "objectSeconds": object_seconds,
            "sessionCount": session_count,
        }
        index["checkpointVerifications"].append(verification)
        index["recoveryLinks"].extend(recovery_references)
        run = next(row for row in index["rollouts"] if row["runId"] == run_id)
        run["checkpointVerificationIds"].append(verification_id)
        return index

    _atomic_update(root, update)
    return verification_id


def recovery_attempt_details(repo_root: Path, capsule: ReleaseCapsule, environment: str, attempt_id: str) -> dict:
    """Return an unresolved attempt after binding it to the selected release."""
    root = _root(repo_root)
    _uuid(attempt_id, "recovery attempt")
    index = _load_index(root)
    old = next((row for row in index["attempts"] if row["attemptId"] == attempt_id), None)
    if (
        old is None or old["state"] not in {"unknown", "attempting"}
        or not _is_unresolved(old, index["attempts"], index["recoveryLinks"], index["checkpointVerifications"])
        or old["environment"] != environment or old["payloadDigest"] != capsule.payload_digest
        or old["engineDigest"] != capsule.engine_digest or old["sourceCommit"] != capsule.source_commit
        or old["checkpoint"] > len(capsule.checkpoints)
    ):
        raise ReleaseStateError("recovery attempt is missing, resolved, or does not match this capsule and environment")
    return dict(old)


def recovery_prompt(repo_root: Path, capsule: ReleaseCapsule, environment: str, attempt_id: str) -> str:
    old = recovery_attempt_details(repo_root, capsule, environment, attempt_id)
    evidence = old["evidence"]
    evidence_text = "\n".join(
        f"  - {item['path']} (sha256: {item['sha256'] or 'unavailable'})" for item in evidence
    ) or "  - no saved file evidence"
    return (
        f"Recovery candidate {attempt_id} is {old['state']} for {old['environment']} checkpoint {old['checkpoint']}.\n"
        f"Target scope: {json.dumps(old['targetScope'], sort_keys=True)}\n"
        f"Saved evidence:\n{evidence_text}\n"
        "Independently verify that the original SQLcl process and its database operation have ended. "
        "A free local lock or a stale PID file does not establish that.\n"
        f"To bind that reconciliation into the recovery plan, type exactly: RECONCILE {attempt_id}"
    )


def _validate_recovery_plan_index(
    index: Mapping,
    capsule: ReleaseCapsule,
    plan: TargetPlan,
    *,
    observed_state_sha256: str | None = None,
) -> None:
    """Bind a recovery plan to the unresolved attempt and reconciled partial state."""
    recovery_id = getattr(plan, "recovery_attempt", None)
    if recovery_id is None:
        if getattr(plan, "recovery_binding", None) is not None:
            raise ReleaseStateError("normal plan cannot contain a recovery binding")
        return
    _uuid(recovery_id, "recovery attempt")
    if (
        getattr(plan, "payload_digest", None) != capsule.payload_digest
        or not isinstance(getattr(plan, "environment", None), str)
        or plan.environment not in ENVIRONMENTS
    ):
        raise ReleaseStateError("recovery plan does not match the selected capsule and environment")
    rows = index["attempts"]
    old = next((row for row in rows if row["attemptId"] == recovery_id), None)
    if (
        old is None or old["state"] not in {"unknown", "attempting"}
        or not _is_unresolved(old, rows, index["recoveryLinks"], index["checkpointVerifications"])
        or old["environment"] != plan.environment or old["payloadDigest"] != capsule.payload_digest
        or old["engineDigest"] != capsule.engine_digest or old["sourceCommit"] != capsule.source_commit
    ):
        raise ReleaseStateError("recovery attempt is missing, resolved, or does not match this capsule and environment")
    scope, _target, _details = _attempt_plan(capsule, plan, old["checkpoint"])
    if scope != old["targetScope"]:
        raise ReleaseStateError("recovery plan observed a different physical target scope")
    others = [row for row in rows if row["targetScope"] == scope and _is_unresolved(row, index["attempts"], index["recoveryLinks"], index["checkpointVerifications"]) and row["attemptId"] != recovery_id]
    if others:
        raise ReleaseStateError("another unresolved attempt occupies the recovery target")
    binding = getattr(plan, "recovery_binding", None)
    binding_fields = {
        "attemptId", "attemptState", "checkpoint", "targetScope", "terminalAcknowledgment",
        "acknowledgedAt", "oldEvidence", "observedStateSha256",
    }
    if not isinstance(binding, Mapping) or set(binding) != binding_fields:
        raise ReleaseStateError("recovery plan is missing its terminal reconciliation binding")
    if (
        binding.get("attemptId") != recovery_id or binding.get("attemptState") != old["state"]
        or binding.get("checkpoint") != old["checkpoint"]
        or _validate_scope(binding.get("targetScope"), "recovery target scope") != old["targetScope"]
        or binding.get("terminalAcknowledgment") != f"RECONCILE {recovery_id}"
        or binding.get("oldEvidence") != old["evidence"]
    ):
        raise ReleaseStateError("recovery plan does not match the acknowledged attempt and saved evidence")
    _timestamp(binding.get("acknowledgedAt"), "recovery acknowledgment timestamp")
    _digest(binding.get("observedStateSha256"), "recovery observed state fingerprint")
    if observed_state_sha256 is not None and binding["observedStateSha256"] != observed_state_sha256:
        raise ReleaseStateError("observed partial target state changed after recovery preparation")


def validate_recovery_plan(
    repo_root: Path,
    capsule: ReleaseCapsule,
    plan: TargetPlan,
    *,
    observed_state_sha256: str | None = None,
) -> None:
    """Bind a recovery plan to the unresolved attempt and reconciled partial state."""
    root = _root(repo_root)
    _validate_recovery_plan_index(
        _load_index(root), capsule, plan, observed_state_sha256=observed_state_sha256,
    )


def _evidence_rows(root: Path, evidence: Sequence[str], *, require_files: bool) -> list[dict]:
    if isinstance(evidence, (str, bytes)) or not isinstance(evidence, Sequence):
        raise ReleaseStateError("attempt evidence must be a sequence of repository-relative paths")
    result = []
    seen = set()
    for raw_path in evidence:
        relative = _relative_path(raw_path, "attempt evidence path")
        if relative in seen:
            raise ReleaseStateError("attempt evidence path is duplicated")
        seen.add(relative)
        path = _checked_path(root, relative, allow_missing=not require_files)
        try:
            if path.is_file():
                digest = _hash(path.read_bytes())
            elif require_files:
                raise ReleaseStateError(f"required attempt evidence is not a readable file: {relative}")
            else:
                digest = None
        except OSError as error:
            raise ReleaseStateError(f"cannot read attempt evidence {relative}: {error}") from error
        result.append({"path": relative, "sha256": digest})
    if require_files and not result:
        raise ReleaseStateError("verified attempt requires readable evidence files")
    return result


def _no_write_classification(root: Path, evidence: list[dict]) -> str:
    if len(evidence) != 1 or evidence[0]["sha256"] is None:
        raise ReleaseStateError("not-started requires exactly one existing SQLcl output log")
    path = _checked_path(root, evidence[0]["path"], allow_missing=False)
    try:
        output = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ReleaseStateError(f"not-started SQLcl output log is not readable UTF-8: {error}") from error
    if any(marker in output for marker in PAYLOAD_MARKERS):
        raise ReleaseStateError("not-started cannot be claimed after a payload or completion marker")
    classification = migration_attempts.not_started_reason(output)
    if classification not in NO_WRITE_REASONS:
        raise ReleaseStateError("not-started requires recognized pre-payload no-write evidence")
    return classification


def _finish_attempt_in_index(
    root: Path,
    index: dict,
    row: dict,
    *,
    state: str,
    evidence: Sequence[str],
    clean_metrics: Mapping,
) -> None:
    """Apply one terminal attempt transition inside an existing atomic index update."""
    if row["state"] != "attempting":
        raise ReleaseStateError("attempt is missing or already terminal; transitions cannot be overwritten")
    evidence_rows = _evidence_rows(root, evidence, require_files=state == "verified")
    classification = _no_write_classification(root, evidence_rows) if state == "not-started" else None
    now = _utc_now()
    started = _timestamp(row["startedAt"], "attempt startedAt")
    finished = _timestamp(now, "attempt finishedAt")
    duration = max(0.0, (finished - started).total_seconds())
    result_metrics = dict(clean_metrics)
    result_metrics.setdefault("durationSeconds", duration)
    if classification is not None:
        result_metrics["noWriteClassification"] = classification
    budget_seconds = row["budget"]["seconds"]
    metric_seconds = result_metrics.get("budgetSeconds")
    if metric_seconds is not None and budget_seconds is not None and metric_seconds != budget_seconds:
        raise ReleaseStateError("attempt metric budgetSeconds does not match the capsule")
    seconds = budget_seconds if budget_seconds is not None else metric_seconds
    elapsed = result_metrics.get("objectSeconds")
    coverage = result_metrics.get("budgetCoverageComplete")
    passed = None
    if seconds is not None and elapsed is not None and coverage is not None:
        passed = coverage is True and elapsed <= seconds
    result_metrics["budgetPassed"] = passed if passed is not None else False
    row.update({
        "state": state,
        "writePossible": state != "not-started",
        "finishedAt": now,
        "evidence": evidence_rows,
        "metrics": _validate_metrics(result_metrics),
        "budget": {
            "seconds": seconds,
            "elapsedSeconds": elapsed,
            "coverageComplete": coverage,
            "passed": passed,
        },
    })
    if state == "verified" and row["recoveryOf"] is not None:
        old = next(item for item in index["attempts"] if item["attemptId"] == row["recoveryOf"])
        if old["state"] not in {"unknown", "attempting"} or old["recoveredBy"] is not None:
            raise ReleaseStateError("recovery target changed before the recovery was verified")
        if old["state"] == "unknown":
            old["recoveredBy"] = row["attemptId"]


def finish_attempt(
    repo_root: Path,
    attempt_id: str,
    *,
    state: str,
    evidence: Sequence[str],
    metrics: Mapping,
) -> None:
    """Write one immutable terminal transition for a previously recorded attempt."""
    root = _root(repo_root)
    _uuid(attempt_id, "attemptId")
    if not isinstance(state, str) or state not in {"verified", "unknown", "not-started"}:
        raise ReleaseStateError("finish_attempt state must be verified, unknown, or not-started")
    clean_metrics = _validate_metrics(metrics)

    def update(index: dict) -> dict:
        row = next((item for item in index["attempts"] if item["attemptId"] == attempt_id), None)
        if row is None:
            raise ReleaseStateError("attempt is missing or already terminal; transitions cannot be overwritten")
        _finish_attempt_in_index(
            root, index, row, state=state, evidence=evidence, clean_metrics=clean_metrics,
        )
        return index

    _atomic_update(root, update)


def _capsule_step_count(capsule: ReleaseCapsule) -> int:
    checkpoint_count = len(getattr(capsule, "checkpoints", ()))
    root = Path(getattr(capsule, "root", "."))
    path = root / "capsule.json"
    if path.is_file():
        document, _raw = release_capsule._read_capsule_json(path)
        definition = document.get("payloadDefinition")
        steps = definition.get("steps") if isinstance(definition, Mapping) else None
        if not isinstance(steps, list) or not steps:
            raise ReleaseStateError("capsule rollout step list is missing")
        if sum(1 for step in steps if isinstance(step, Mapping) and step.get("type") == "feature-migrate") != checkpoint_count:
            raise ReleaseStateError("capsule rollout steps do not match the selected feature checkpoints")
        return len(steps)
    if checkpoint_count < 1:
        raise ReleaseStateError("rollout must contain at least one checkpoint")
    return checkpoint_count


def _capsule_step_definitions(capsule: ReleaseCapsule) -> list[dict]:
    path = Path(getattr(capsule, "root", ".")) / "capsule.json"
    if not path.is_file():
        return []
    try:
        document = release_capsule._read_capsule_json(path)[0]
    except (OSError, ValueError, release_capsule.ReleaseCapsuleError) as error:
        raise ReleaseStateError(f"cannot read rollout step definitions: {error}") from error
    definition = document.get("payloadDefinition")
    steps = definition.get("steps") if isinstance(definition, Mapping) else None
    if not isinstance(steps, list) or any(not isinstance(step, Mapping) for step in steps):
        raise ReleaseStateError("capsule rollout step definitions are malformed")
    return [dict(step) for step in steps]


def _attempt_matches_rollout(attempt: Mapping, run: Mapping) -> bool:
    attempt_started = _timestamp(attempt["startedAt"], "attempt startedAt")
    run_started = _timestamp(run["startedAt"], "rollout startedAt")
    run_finished = _timestamp(run["finishedAt"], "rollout finishedAt", nullable=True)
    return (
        attempt["environment"] == run["environment"]
        and attempt["sourceCommit"] == run["sourceCommit"]
        and attempt["payloadDigest"] == run["payloadDigest"]
        and attempt["engineDigest"] == run["engineDigest"]
        and attempt["planDigest"] == run["planDigest"]
        and attempt["targetScope"] in run["targetScopes"]
        and run_started <= attempt_started
        and (run_finished is None or attempt_started <= run_finished)
    )


def _run_uncertainty_is_checkpoint_only(
    run: Mapping,
    capsule: ReleaseCapsule,
    authorized_attempts: Sequence[Mapping],
) -> bool:
    if run["state"] not in {"running", "unknown"} or run["databaseState"] not in {"not-started", "unknown"}:
        return False
    checkpoint_by_step = {}
    checkpoint = 0
    for number, step in enumerate(_capsule_step_definitions(capsule), start=1):
        if step.get("type") == "feature-migrate":
            checkpoint += 1
            checkpoint_by_step[number] = checkpoint
    for outcome in run["steps"]:
        if outcome["status"] not in {"failed", "unknown"}:
            continue
        step_checkpoint = checkpoint_by_step.get(outcome["step"])
        if step_checkpoint is None or step_checkpoint not in {
            attempt["checkpoint"] for attempt in authorized_attempts
        }:
            return False
    return True


def _attempt_has_recovery_verification(index: Mapping, attempt: Mapping, *, excluding_run_id: str | None = None) -> bool:
    verifications = {row["verificationId"]: row for row in index["checkpointVerifications"]}
    for link in index["recoveryLinks"]:
        if link["oldAttemptId"] != attempt["attemptId"]:
            continue
        verification = verifications.get(link["verificationId"])
        if verification is None or verification["mode"] != "recovery":
            continue
        if excluding_run_id is not None and verification["runId"] == excluding_run_id:
            continue
        if (
            verification["environment"] == attempt["environment"]
            and verification["checkpoint"] == attempt["checkpoint"]
            and verification["targetScope"] == attempt["targetScope"]
            and verification["sourceCommit"] == attempt["sourceCommit"]
            and verification["payloadDigest"] == attempt["payloadDigest"]
            and verification["engineDigest"] == attempt["engineDigest"]
        ):
            return True
    return False


def _rollout_is_recovery_parent(index: Mapping, run: Mapping, capsule: ReleaseCapsule, plan: TargetPlan) -> bool:
    attempt_id = getattr(plan, "recovery_attempt", None)
    if attempt_id is None:
        return False
    attempt = next((row for row in index["attempts"] if row["attemptId"] == attempt_id), None)
    parents = [row for row in index["rollouts"] if attempt is not None and _attempt_matches_rollout(attempt, row)]
    return (
        attempt is not None
        and len(parents) == 1 and parents[0]["runId"] == run["runId"]
        and _attempt_matches_rollout(attempt, run)
        and _is_unresolved(attempt, index["attempts"], index["recoveryLinks"], index["checkpointVerifications"])
        and _run_uncertainty_is_checkpoint_only(run, capsule, [attempt])
    )


def recovery_parent_rollout(repo_root: Path, capsule: ReleaseCapsule, plan: TargetPlan) -> dict:
    """Return the one rollout whose unresolved checkpoint is bound by this recovery plan."""
    root = _root(repo_root)
    recovery_id = getattr(plan, "recovery_attempt", None)
    if recovery_id is None:
        raise ReleaseStateError("recovery parent lookup requires a recovery plan")
    index = _load_index(root)
    _validate_recovery_plan_index(index, capsule, plan)
    attempt = next((row for row in index["attempts"] if row["attemptId"] == recovery_id), None)
    parents = [row for row in index["rollouts"] if attempt is not None and _attempt_matches_rollout(attempt, row)]
    if len(parents) != 1 or not _rollout_is_recovery_parent(index, parents[0], capsule, plan):
        raise ReleaseStateError("recovery attempt does not identify one eligible parent rollout")
    return dict(parents[0])


def _rollout_is_reconciled(index: Mapping, run: Mapping, capsule: ReleaseCapsule) -> bool:
    matching = [attempt for attempt in index["attempts"] if _attempt_matches_rollout(attempt, run)]
    if any(
        sum(1 for candidate in index["rollouts"] if _attempt_matches_rollout(attempt, candidate)) != 1
        for attempt in matching
    ):
        return False
    recovered = [
        attempt for attempt in matching
        if attempt["state"] in {"attempting", "unknown"}
        and _attempt_has_recovery_verification(index, attempt, excluding_run_id=run["runId"])
    ]
    if not recovered or any(
        attempt["state"] in {"attempting", "unknown"}
        and attempt not in recovered
        for attempt in matching
    ):
        return False
    return _run_uncertainty_is_checkpoint_only(run, capsule, recovered)


def _auxiliary_target_scope(plan, number: int, step: Mapping) -> dict:
    targets = getattr(plan, "targets", None)
    key = f"step:{number}"
    target = targets.get(key) if isinstance(targets, Mapping) else None
    fields = {
        "environment", "operation", "logicalSchema", "connection", "expectedUser",
        "schema", "classification", "observed",
    }
    if not isinstance(target, Mapping) or set(target) != fields:
        raise ReleaseStateError(f"rollout plan is missing the prepared target binding for step {number}")
    if (
        target.get("environment") != getattr(plan, "environment", None)
        or target.get("operation") != "migration"
        or target.get("logicalSchema") != step.get("schema")
        or not all(isinstance(target.get(field), str) and target[field] for field in (
            "connection", "expectedUser", "schema", "classification",
        ))
    ):
        raise ReleaseStateError(f"rollout plan target binding differs from frozen step {number}")
    identity = complete_observed_identity(target.get("observed"))
    if identity is None or identity["session_user"].upper() != target["expectedUser"].upper() or identity["current_schema"].upper() != target["schema"].upper():
        raise ReleaseStateError(f"rollout plan target identity for step {number} is incomplete or inconsistent")
    if step.get("type") == "ords-import" and target["expectedUser"].upper() != target["schema"].upper():
        raise ReleaseStateError(f"ORDS step {number} is not bound to its actual REST owner login")
    return target_scope_from_identity(identity)


def start_rollout(repo_root: Path, capsule: ReleaseCapsule, plan) -> str:
    """Persist a new immutable-identity rollout run before executing any step."""
    root = _root(repo_root)
    if getattr(plan, "payload_digest", None) != getattr(capsule, "payload_digest", None):
        raise ReleaseStateError("rollout plan payload does not match the capsule")
    environment = getattr(plan, "environment", None)
    if environment not in ENVIRONMENTS:
        raise ReleaseStateError("rollout environment must be staging or prod")
    for label, value in (
        ("payloadDigest", getattr(capsule, "payload_digest", None)),
        ("engineDigest", getattr(capsule, "engine_digest", None)),
        ("planDigest", getattr(plan, "plan_digest", None)),
    ):
        _digest(value, f"rollout {label}")
    source_commit = _commit(getattr(capsule, "source_commit", None))
    checkpoints = getattr(capsule, "checkpoints", None)
    guards = getattr(plan, "checkpoint_guards", None)
    if not isinstance(checkpoints, (tuple, list)) or not checkpoints or not isinstance(guards, (tuple, list)):
        raise ReleaseStateError("rollout capsule checkpoints or prepared guards are malformed")
    scopes = []
    for number in range(1, len(checkpoints) + 1):
        scope, _target, _details = _attempt_plan(capsule, plan, number)
        scopes.append(scope)
    step_definitions = _capsule_step_definitions(capsule)
    expected_auxiliary = {
        f"step:{number}"
        for number, step in enumerate(step_definitions, start=1)
        if step.get("type") in {"sql-script", "ords-import"}
    }
    supplied_auxiliary = {
        key for key in getattr(plan, "targets", {})
        if isinstance(key, str) and key.startswith("step:")
    }
    if supplied_auxiliary != expected_auxiliary:
        raise ReleaseStateError("rollout target plan must bind every SQL/ORDS step exactly once")
    for number, step in enumerate(step_definitions, start=1):
        if step.get("type") in {"sql-script", "ords-import"}:
            scopes.append(_auxiliary_target_scope(plan, number, step))
    unique_scopes = {_canonical_bytes(scope): scope for scope in scopes}
    target_scopes = [unique_scopes[key] for key in sorted(unique_scopes)]
    budget_seconds = getattr(capsule, "database_object_budget_seconds", None)
    if budget_seconds is not None and (type(budget_seconds) is not int or not 1 <= budget_seconds <= 300):
        raise ReleaseStateError("capsule database object budget must be an integer from 1 through 300")
    run_id = str(uuid.uuid4())
    run = {
        "runId": run_id,
        "environment": environment,
        "sourceCommit": source_commit,
        "payloadDigest": capsule.payload_digest,
        "engineDigest": capsule.engine_digest,
        "planDigest": plan.plan_digest,
        "targetScopes": target_scopes,
        "state": "running",
        "databaseState": "not-started",
        "startedAt": _utc_now(),
        "finishedAt": None,
        "expectedCheckpointCount": len(checkpoints),
        "expectedStepCount": _capsule_step_count(capsule),
        "steps": [],
        "checkpointVerificationIds": [],
        "budget": {"seconds": budget_seconds, "elapsedSeconds": None, "coverageComplete": None, "passed": None},
        "finalReportPath": None,
        "finalReportSha256": None,
    }

    def update(index: dict) -> dict:
        try:
            current_capsule = release_capsule.load_capsule(root, Path(capsule.root))
        except (OSError, ValueError, release_capsule.ReleaseCapsuleError) as error:
            raise ReleaseStateError(f"rollout capsule changed or is unavailable before run start: {error}") from error
        if (
            current_capsule.manifest_sha256 != capsule.manifest_sha256
            or current_capsule.payload_digest != capsule.payload_digest
            or current_capsule.engine_digest != capsule.engine_digest
            or current_capsule.source_commit != capsule.source_commit
            or tuple((item.schema, tuple((source.path, source.sha256) for source in item.inputs)) for item in current_capsule.checkpoints)
            != tuple((item.schema, tuple((source.path, source.sha256) for source in item.inputs)) for item in capsule.checkpoints)
        ):
            raise ReleaseStateError("rollout capsule identity changed before its run record was created")
        recovery_id = getattr(plan, "recovery_attempt", None)
        if recovery_id is not None:
            _validate_recovery_plan_index(index, capsule, plan)
        target_scope_keys = {_canonical_bytes(scope) for scope in target_scopes}
        active = []
        for existing in index["rollouts"]:
            overlaps = bool(
                {_canonical_bytes(scope) for scope in existing["targetScopes"]} & target_scope_keys
            )
            if not overlaps or not (
                existing["state"] in {"running", "unknown"} or existing["databaseState"] == "unknown"
            ):
                continue
            if _rollout_is_reconciled(index, existing, capsule):
                continue
            if _rollout_is_recovery_parent(index, existing, capsule, plan):
                continue
            active.append(existing)
        if active:
            raise ReleaseStateError("a running or unresolved rollout owns one of these target scopes; reconcile before reuse")
        if any(
            attempt["targetScope"] in target_scopes and _is_unresolved(attempt, index["attempts"], index["recoveryLinks"], index["checkpointVerifications"])
            and attempt["attemptId"] != recovery_id
            for attempt in index["attempts"]
        ):
            raise ReleaseStateError("an unresolved attempt occupies a rollout target; reconcile before starting")
        index["rollouts"].append(run)
        return index

    _atomic_update(root, update)
    return run_id


def finish_rollout(repo_root: Path, run_id: str, *, outcome: Mapping) -> None:
    """Persist a terminal run result; verified requires complete durable evidence."""
    root = _root(repo_root)
    _uuid(run_id, "rollout runId")
    expected = {"state", "databaseState", "steps", "checkpointVerificationIds", "budget", "finalReportPath", "finalReportSha256"}
    if not isinstance(outcome, Mapping) or set(outcome) != expected:
        raise ReleaseStateError("rollout outcome has unsupported or missing fields")
    state = outcome.get("state")
    if state not in {"verified", "failed", "unknown", "not-started"}:
        raise ReleaseStateError("rollout outcome state is invalid")

    def update(index: dict) -> dict:
        row = next((item for item in index["rollouts"] if item["runId"] == run_id), None)
        if row is None or row["state"] != "running":
            raise ReleaseStateError("rollout run is missing or already terminal")
        steps = [_validate_step_outcome(item) for item in outcome["steps"]] if isinstance(outcome["steps"], list) else None
        verification_ids = outcome["checkpointVerificationIds"]
        if not isinstance(verification_ids, list):
            raise ReleaseStateError("rollout checkpoint verification IDs must be an array")
        if state == "verified":
            if outcome["databaseState"] != "verified" or len(steps or []) != row["expectedStepCount"]:
                raise ReleaseStateError("verified rollout outcome requires every successful step and verified database state")
            report_path = _relative_path(outcome["finalReportPath"], "rollout final report path")
            report = _read_report_json(_checked_path(root, report_path, allow_missing=False), "final rollout report")
            matches = [item for item in _matching_capsules(root, row["payloadDigest"]) if (
                item["engineDigest"] == row["engineDigest"] and item["sourceCommit"] == row["sourceCommit"]
            )]
            if len(matches) != 1:
                raise ReleaseStateError("verified rollout requires one matching frozen capsule on disk")
            try:
                frozen_capsule = release_capsule.load_capsule(root, root / matches[0]["path"])
                proof = _validate_completion_proof(frozen_capsule, report, environment=row["environment"])
            except (OSError, ValueError, release_capsule.ReleaseCapsuleError) as error:
                if isinstance(error, ReleaseStateError):
                    raise
                raise ReleaseStateError(f"verified rollout completion proof is invalid: {error}") from error
            expected_step_rows = [
                {"step": item["step"], "status": item["status"], "receiptPath": item["receiptPath"], "receiptSha256": item["receiptSha256"]}
                for item in steps or []
            ]
            proof_step_rows = [
                {"step": item["step"], "status": item["status"], "receiptPath": item["receiptPath"], "receiptSha256": item["receiptSha256"]}
                for item in proof["steps"]
            ]
            if (
                report.get("localRunId") != run_id
                or report.get("environment") != row["environment"]
                or report.get("sourceCommit") != row["sourceCommit"]
                or report.get("payloadDigest") != row["payloadDigest"]
                or report.get("engineDigest") != row["engineDigest"]
                or report.get("targetPlanDigest") != row["planDigest"]
                or proof["targetScopes"] != row["targetScopes"]
                or proof["checkpointVerificationIds"] != verification_ids
                or expected_step_rows != proof_step_rows
                or proof["budget"] != outcome["budget"]
                or proof["finalReportPath"] != report_path
                or proof["finalReportSha256"] != outcome["finalReportSha256"]
                or _completion_core_hash(report) != outcome["finalReportSha256"]
            ):
                raise ReleaseStateError("verified rollout report does not match its durable completion outcome")
        row.update({
            "state": state,
            "databaseState": outcome["databaseState"],
            "finishedAt": _utc_now(),
            "steps": steps if steps is not None else outcome["steps"],
            "checkpointVerificationIds": verification_ids,
            "budget": outcome["budget"],
            "finalReportPath": outcome["finalReportPath"],
            "finalReportSha256": outcome["finalReportSha256"],
        })
        return index

    _atomic_update(root, update)


def _completion_core_hash(report: Mapping) -> str:
    core = dict(report)
    core.pop("completionProof", None)
    return _hash(_canonical_bytes(core))


def _validate_completion_proof(capsule: ReleaseCapsule, report: Mapping, *, environment: str) -> dict:
    proof_fields = {
        "schemaVersion", "runId", "environment", "sourceCommit", "payloadDigest", "engineDigest",
        "planDigest", "targetScopes", "state", "databaseState", "steps", "checkpointVerificationIds",
        "checkpointVerifications", "recoveryLinks", "budget", "finalReportPath", "finalReportSha256",
    }
    proof = report.get("completionProof")
    if not isinstance(proof, Mapping) or set(proof) != proof_fields:
        raise ReleaseStateError("rollout completion proof is missing or malformed")
    if type(proof["schemaVersion"]) is not int or proof["schemaVersion"] != 1:
        raise ReleaseStateError("rollout completion proof schemaVersion is unsupported")
    run_id = _uuid(proof["runId"], "completion proof runId")
    if (
        proof["environment"] != environment or proof["state"] != "verified"
        or proof["databaseState"] != "verified"
        or report.get("localRunId") != run_id
        or report.get("environment") != environment
        or proof["sourceCommit"] != capsule.source_commit
        or proof["payloadDigest"] != capsule.payload_digest
        or proof["engineDigest"] != capsule.engine_digest
        or proof["planDigest"] != report.get("targetPlanDigest")
    ):
        raise ReleaseStateError("rollout completion proof identity or outcome does not match the report and capsule")
    _commit(proof["sourceCommit"])
    for field in ("payloadDigest", "engineDigest", "planDigest"):
        _digest(proof[field], f"completion proof {field}")
    scopes = proof["targetScopes"]
    if not isinstance(scopes, list) or not scopes:
        raise ReleaseStateError("completion proof target scopes are missing")
    clean_scopes = [_validate_scope(scope, "completion proof target scope") for scope in scopes]
    scope_keys = [_canonical_bytes(scope) for scope in clean_scopes]
    if scope_keys != sorted(scope_keys) or len(scope_keys) != len(set(scope_keys)):
        raise ReleaseStateError("completion proof target scopes must be unique and ordered")

    try:
        capsule_document = release_capsule._read_capsule_json(Path(capsule.root) / "capsule.json")[0]
    except (OSError, ValueError, release_capsule.ReleaseCapsuleError) as error:
        raise ReleaseStateError(f"cannot read capsule definition for completion proof: {error}") from error
    definition = capsule_document.get("payloadDefinition")
    step_definitions = definition.get("steps") if isinstance(definition, Mapping) else None
    if not isinstance(step_definitions, list) or not step_definitions:
        raise ReleaseStateError("capsule rollout step definitions are missing")
    checkpoint_count = len(capsule.checkpoints)
    if sum(1 for step in step_definitions if isinstance(step, Mapping) and step.get("type") == "feature-migrate") != checkpoint_count:
        raise ReleaseStateError("capsule rollout steps do not match its feature checkpoints")

    verification_rows = proof["checkpointVerifications"]
    verification_ids = proof["checkpointVerificationIds"]
    if (
        not isinstance(verification_rows, list) or len(verification_rows) != checkpoint_count
        or not isinstance(verification_ids, list) or len(verification_ids) != checkpoint_count
    ):
        raise ReleaseStateError("completion proof must contain every checkpoint verification")
    clean_verifications = [_validate_checkpoint_verification(row) for row in verification_rows]
    clean_verifications.sort(key=lambda row: row["checkpoint"])
    checkpoints = list(range(1, checkpoint_count + 1))
    if [row["checkpoint"] for row in clean_verifications] != checkpoints:
        raise ReleaseStateError("completion proof checkpoint verifications are missing or duplicated")
    if verification_ids != [row["verificationId"] for row in clean_verifications]:
        raise ReleaseStateError("completion proof checkpoint verification IDs do not match their records")
    for row in clean_verifications:
        if any(row[field] != proof[field] for field in (
            "runId", "environment", "sourceCommit", "payloadDigest", "engineDigest", "planDigest",
        )) or row["targetScope"] not in clean_scopes:
            raise ReleaseStateError("checkpoint verification does not match the rollout completion proof")

    links = proof["recoveryLinks"]
    if not isinstance(links, list):
        raise ReleaseStateError("completion proof recovery links must be an array")
    clean_links = [_validate_recovery_link(item) for item in links]
    if len({row["oldAttemptId"] for row in clean_links}) != len(clean_links):
        raise ReleaseStateError("completion proof contains duplicate recovery links")
    for verification in clean_verifications:
        matching = [row for row in clean_links if row["verificationId"] == verification["verificationId"]]
        if verification["mode"] == "recovery":
            if len(matching) != 1 or matching[0]["reconciliationEvidence"] not in {
                item["path"] for item in verification["evidence"]
            }:
                raise ReleaseStateError("recovery checkpoint is missing its portable reconciliation evidence link")
        elif matching:
            raise ReleaseStateError("non-recovery checkpoint cannot carry a recovery link")
    if any(row["verificationId"] not in verification_ids for row in clean_links):
        raise ReleaseStateError("completion proof recovery link references an unknown checkpoint")

    step_rows = proof["steps"]
    report_steps = report.get("steps")
    if not isinstance(step_rows, list) or len(step_rows) != len(step_definitions) or not isinstance(report_steps, list) or len(report_steps) != len(step_definitions):
        raise ReleaseStateError("completion proof must contain every rollout step")
    step_fields = {"step", "status", "receiptPath", "receiptSha256", "receiptJson"}
    manifest_digest = capsule_document.get("rolloutManifestSha256")
    expected_excluded: list[str] = []
    expected_data_seconds = 0.0
    expected_ords_seconds = 0.0
    for number, (step_row, report_step, definition_step) in enumerate(
        zip(step_rows, report_steps, step_definitions, strict=True), start=1,
    ):
        if not isinstance(step_row, Mapping) or set(step_row) != step_fields:
            raise ReleaseStateError("completion proof step receipt is malformed")
        receipt_path = _relative_path(step_row["receiptPath"], "completion proof receipt path")
        receipt_raw = step_row["receiptJson"]
        if not isinstance(receipt_raw, str) or "\r" in receipt_raw or receipt_raw.startswith("\ufeff"):
            raise ReleaseStateError("completion proof receipt must be UTF-8/LF text")
        receipt_bytes = receipt_raw.encode("utf-8")
        receipt_hash = _digest(step_row["receiptSha256"], "completion proof receipt hash")
        if _hash(receipt_bytes) != receipt_hash:
            raise ReleaseStateError("completion proof embedded step receipt hash does not match")
        _uuid(run_id, "completion proof runId")
        if step_row["step"] != number or step_row["status"] != "verified":
            raise ReleaseStateError("completion proof steps must be ordered and verified")
        try:
            receipt = json.loads(receipt_raw, object_pairs_hook=_unique_pairs)
        except (json.JSONDecodeError, ReleaseStateError) as error:
            raise ReleaseStateError("completion proof embedded step receipt is invalid JSON") from error
        expected_receipt_fields = {
            "schemaVersion", "status", "environment", "manifestSha256", "stepNumber", "stepType",
            "stepSha256", "inputHashes", "startedAt", "endedAt", "durationSeconds", "evidencePaths",
        }
        if (
            not isinstance(receipt, Mapping) or set(receipt) != expected_receipt_fields
            or receipt["schemaVersion"] != 1 or receipt["status"] != "succeeded"
            or receipt["environment"] != environment or receipt["manifestSha256"] != manifest_digest
            or receipt["stepNumber"] != number or receipt["stepType"] != definition_step.get("type")
            or receipt["stepSha256"] != report_step.get("sha256")
            or receipt["inputHashes"] != report_step.get("inputHashes")
            or report_step.get("number") != number or report_step.get("type") != definition_step.get("type")
            or report_step.get("status") != "succeeded"
            or report_step.get("receiptPath") != receipt_path
        ):
            raise ReleaseStateError("completion proof step receipt does not match the frozen rollout step")
        if type(receipt["durationSeconds"]) not in {int, float} or not math.isfinite(receipt["durationSeconds"]) or receipt["durationSeconds"] < 0:
            raise ReleaseStateError("completion proof step receipt duration is invalid")
        if not isinstance(receipt["evidencePaths"], list) or any(not isinstance(item, str) for item in receipt["evidencePaths"]):
            raise ReleaseStateError("completion proof step receipt evidence paths are malformed")

        step_type = definition_step.get("type")
        expected_phase = None
        if step_type in {"sql-script", "ords-import"}:
            source_name = _relative_path(definition_step.get("file"), "auxiliary rollout source path")
            try:
                frozen_source = (Path(capsule.root) / "payload" / PurePosixPath(source_name)).read_bytes()
                from . import rollout

                expected_phase = rollout.classify_auxiliary_step(definition_step, frozen_source)
                expected_reason = rollout._auxiliary_classification_reason(step_type, expected_phase)
            except (OSError, ValueError) as error:
                raise ReleaseStateError(f"cannot validate frozen auxiliary phase for step {number}: {error}") from error
            if expected_phase == "uncovered-object":
                expected_excluded.append(f"step-{number:03d}:{step_type}")
            elif expected_phase == "data":
                expected_data_seconds += _number(report_step.get("phaseSeconds"), f"step {number} phaseSeconds")
            elif expected_phase == "ords":
                expected_ords_seconds += _number(report_step.get("phaseSeconds"), f"step {number} phaseSeconds")
        elif step_type == "migrate":
            expected_phase = "uncovered-object"
            from . import rollout

            expected_reason = rollout._auxiliary_classification_reason("migrate", expected_phase)
            expected_excluded.append(f"step-{number:03d}:{step_type}")
        if expected_phase is not None and (
            report_step.get("phase") != expected_phase
            or report_step.get("classificationReason") != expected_reason
        ):
            raise ReleaseStateError(f"rollout step {number} phase does not match its frozen source classification")

    budget = proof["budget"]
    if not isinstance(budget, Mapping) or set(budget) != BUDGET_FIELDS:
        raise ReleaseStateError("completion proof budget is malformed")
    clean_budget = _validate_budget(budget)
    report_budget = report.get("objectBudget")
    expected_seconds = getattr(capsule, "database_object_budget_seconds", None)
    if (
        clean_budget["seconds"] != expected_seconds or clean_budget["coverageComplete"] is not True
        or clean_budget["passed"] is not True or clean_budget["elapsedSeconds"] is None
        or clean_budget["elapsedSeconds"] > expected_seconds
        or not isinstance(report_budget, Mapping)
        or report_budget.get("seconds") != clean_budget["seconds"]
        or report_budget.get("elapsedSeconds") != clean_budget["elapsedSeconds"]
        or report_budget.get("coverageComplete") is not True
        or report_budget.get("withinBudget") is not True
        or report_budget.get("includedCheckpoints") != checkpoints
        or report_budget.get("excludedObjectSteps") != expected_excluded
        or expected_excluded != []
    ):
        raise ReleaseStateError("completion proof budget does not prove full in-budget checkpoint coverage")
    if any(step.get("type") in {"sql-script", "ords-import"} for step in step_definitions):
        data_seconds = _number(report.get("dataSeconds"), "rollout dataSeconds")
        ords_seconds = _number(report.get("ordsSeconds"), "rollout ordsSeconds")
        tolerance = max(0.01, len(step_definitions) * 0.01)
        if (
            abs(data_seconds - round(expected_data_seconds, 3)) > tolerance
            or abs(ords_seconds - round(expected_ords_seconds, 3)) > tolerance
        ):
            raise ReleaseStateError("rollout phase timing totals do not match the verified data and ORDS steps")
    report_path = _relative_path(proof["finalReportPath"], "completion proof final report path")
    if report.get("reportFiles", {}).get("json") != report_path:
        raise ReleaseStateError("completion proof final report path does not match its report file record")
    final_hash = _digest(proof["finalReportSha256"], "completion proof final report hash")
    if _completion_core_hash(report) != final_hash:
        raise ReleaseStateError("completion proof report hash does not match the report body")
    return dict(proof)


def validate_stage_proof(capsule: ReleaseCapsule, report: Mapping) -> None:
    """Validate portable staging completion data for this exact frozen capsule."""
    if not isinstance(report, Mapping) or not REPORT_FIELDS.issubset(report):
        raise ReleaseStateError("stage report is missing required proof fields")
    if type(report.get("schemaVersion")) is not int or report["schemaVersion"] != 2:
        raise ReleaseStateError("stage report schemaVersion must be 2")
    if report.get("environment") != "staging" or report.get("status") != "success" or report.get("databaseState") != "verified":
        raise ReleaseStateError("stage report must prove successful verified staging")
    if report.get("payloadDigest") != capsule.payload_digest or report.get("engineDigest") != capsule.engine_digest:
        raise ReleaseStateError("stage report payload or engine does not match the capsule")
    if report.get("sourceCommit") != capsule.source_commit:
        raise ReleaseStateError("stage report source commit does not match the capsule")
    _digest(report.get("targetPlanDigest"), "stage report targetPlanDigest")
    expected_hashes = [{"path": row["path"], "sha256": row["sha256"]} for row in capsule.input_hashes]
    supplied = report.get("inputHashes")
    if not isinstance(supplied, list) or supplied != expected_hashes:
        raise ReleaseStateError("stage report shared input hashes do not match the capsule")
    for row in supplied:
        if not isinstance(row, Mapping) or set(row) != {"path", "sha256"}:
            raise ReleaseStateError("stage report input hash entry is malformed")
        _relative_path(row["path"], "stage report input path")
        _digest(row["sha256"], "stage report input hash")
    budget = report.get("objectBudget")
    if not isinstance(budget, Mapping) or set(budget) != OBJECT_BUDGET_FIELDS:
        raise ReleaseStateError("stage report object budget is malformed")
    seconds = budget.get("seconds")
    expected_seconds = getattr(capsule, "database_object_budget_seconds", None)
    if type(seconds) is not int or not 1 <= seconds <= 300 or seconds != expected_seconds:
        raise ReleaseStateError("stage report budget seconds do not match the capsule")
    elapsed = _number(budget.get("elapsedSeconds"), "stage report elapsedSeconds")
    if budget.get("coverageComplete") is not True or budget.get("withinBudget") is not True or elapsed > seconds:
        raise ReleaseStateError("stage report must have complete object coverage within budget")
    checkpoints = list(range(1, len(capsule.checkpoints) + 1))
    included = budget.get("includedCheckpoints")
    excluded = budget.get("excludedObjectSteps")
    if (
        not isinstance(included, list) or any(type(item) is not int for item in included)
        or included != checkpoints or not isinstance(excluded, list) or excluded != []
    ):
        raise ReleaseStateError("stage report object budget does not cover every capsule checkpoint")
    _validate_completion_proof(capsule, report, environment="staging")


def _safe_tree(root: Path, directory: Path) -> None:
    """Refuse capsule deletion if any descendant is a symlink or special file."""
    try:
        relative = relative_to_root(directory, root).as_posix()
    except ValueError as error:
        raise ReleaseStateError("capsule directory is outside the repository") from error
    base = _checked_path(root, relative, allow_missing=False)
    if not base.is_dir():
        raise ReleaseStateError("capsule cleanup target is not a directory")
    for current, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in [*dirnames, *filenames]:
            candidate = current_path / name
            try:
                info = candidate.lstat()
            except OSError as error:
                raise ReleaseStateError(f"capsule tree changed during cleanup inspection: {candidate}") from error
            if candidate.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise ReleaseStateError(f"capsule tree contains a symlink or reparse point: {candidate}")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ReleaseStateError(f"capsule tree contains a special file: {candidate}")


def _capsule_for_cleanup(root: Path, directory: Path, payload_digest: str) -> dict:
    try:
        relative = relative_to_root(directory, root).as_posix()
        manifest_path = _checked_path(root, f"{relative}/capsule.json", allow_missing=False)
        document = release_capsule._read_capsule_json(manifest_path)[0]
    except (OSError, ValueError, release_capsule.ReleaseCapsuleError) as error:
        if isinstance(error, ReleaseStateError):
            raise
        raise ReleaseStateError(f"cannot validate generated capsule before cleanup: {error}") from error
    expected_fields = {
        "schemaVersion", "payloadDigest", "engineDigest", "engineFiles", "sourceCommit",
        "rolloutManifestPath", "rolloutManifestSha256", "payloadDefinition", "files", "sourceInputs", "checkpoints",
    }
    if set(document) != expected_fields or type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1:
        raise ReleaseStateError("generated capsule manifest has unsupported or missing fields")
    if document["payloadDigest"] != payload_digest:
        raise ReleaseStateError("generated capsule manifest payload does not match cleanup request")
    _digest(document["engineDigest"], "capsule engineDigest")
    source_commit = _commit(document["sourceCommit"])
    definition = document["payloadDefinition"]
    definition_fields = {
        "schemaVersion", "rolloutManifest", "databaseObjectBudgetSeconds", "engineDigest",
        "files", "steps", "sourceToDriver", "sourceInputs", "checkpoints",
    }
    if not isinstance(definition, Mapping) or set(definition) != definition_fields:
        raise ReleaseStateError("generated capsule payloadDefinition is malformed")
    if type(definition["schemaVersion"]) is not int or definition["schemaVersion"] != 1:
        raise ReleaseStateError("generated capsule payloadDefinition schema is unsupported")
    if _hash(_canonical_bytes(definition)) != payload_digest:
        raise ReleaseStateError("generated capsule payloadDefinition digest does not match")
    if definition.get("engineDigest") != document["engineDigest"]:
        raise ReleaseStateError("generated capsule engine digest does not match its payload")
    engine_files = document["engineFiles"]
    if not isinstance(engine_files, list) or not engine_files or any(not isinstance(item, str) for item in engine_files):
        raise ReleaseStateError("generated capsule engine file list is malformed")
    engine_files = [_relative_path(item, "capsule engine file") for item in engine_files]
    if len(engine_files) != len(set(engine_files)):
        raise ReleaseStateError("generated capsule engine file list contains duplicates")
    rollout = definition.get("rolloutManifest")
    if not isinstance(rollout, Mapping) or set(rollout) != {"path", "sha256"}:
        raise ReleaseStateError("generated capsule rollout manifest record is malformed")
    rollout_path = _relative_path(rollout["path"], "capsule rollout manifest path")
    rollout_hash = _digest(rollout["sha256"], "capsule rollout manifest hash")
    if (
        document["rolloutManifestPath"] != rollout_path
        or document["rolloutManifestSha256"] != rollout_hash
        or definition["databaseObjectBudgetSeconds"] is not None
        and (type(definition["databaseObjectBudgetSeconds"]) is not int or not 1 <= definition["databaseObjectBudgetSeconds"] <= 300)
    ):
        raise ReleaseStateError("generated capsule rollout metadata or budget is malformed")
    checkpoints = document["checkpoints"]
    if not isinstance(checkpoints, list) or not checkpoints or definition.get("checkpoints") != checkpoints:
        raise ReleaseStateError("generated capsule checkpoint list is malformed")
    for field in ("files", "sourceInputs"):
        if not isinstance(document[field], list) or definition.get(field) != document[field]:
            raise ReleaseStateError(f"generated capsule {field} list does not match its payload")
    file_rows = document["files"]
    file_map: dict[str, dict] = {}
    for row in file_rows:
        if not isinstance(row, Mapping) or set(row) != {"path", "sha256", "kind"}:
            raise ReleaseStateError("generated capsule file inventory row is malformed")
        path = _relative_path(row["path"], "capsule payload file path")
        if path == "capsule.json" or not isinstance(row["kind"], str) or row["kind"] not in {"driver", "source"}:
            raise ReleaseStateError("generated capsule file inventory contains an unexpected entry")
        if (row["kind"] == "driver" and not path.startswith("drivers/")) or (
            row["kind"] == "source" and not path.startswith("payload/")
        ):
            raise ReleaseStateError("generated capsule file kind does not match its path")
        _digest(row["sha256"], "capsule file hash")
        if path in file_map:
            raise ReleaseStateError("generated capsule file inventory contains duplicates")
        file_map[path] = dict(row)
    if file_rows != sorted(file_rows, key=lambda row: row["path"]):
        raise ReleaseStateError("generated capsule file inventory is not ordered")
    manifest_file = f"payload/{rollout_path}"
    if manifest_file not in file_map or file_map[manifest_file]["sha256"] != rollout_hash:
        raise ReleaseStateError("generated capsule rollout manifest is missing from its file inventory")
    expected_name = f"{payload_digest}-{source_commit[:12]}"
    if directory.name != expected_name:
        raise ReleaseStateError("generated capsule directory name does not match its manifest")
    _safe_tree(root, directory)
    expected_files = {"capsule.json", *file_map}
    expected_directories = {""}
    for relative_file in expected_files:
        parent = PurePosixPath(relative_file).parent
        while parent.as_posix() not in {".", ""}:
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    actual_files: set[str] = set()
    actual_directories: set[str] = {""}
    for current, _dirnames, filenames in os.walk(directory, topdown=True, followlinks=False):
        current_path = Path(current)
        current_rel = current_path.relative_to(directory).as_posix()
        if current_rel != ".":
            actual_directories.add(current_rel)
        for name in filenames:
            actual_files.add((current_path / name).relative_to(directory).as_posix())
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ReleaseStateError("generated capsule contains missing or unlisted files in its payload inventory")
    for relative_file, row in file_map.items():
        path = _checked_path(root, f"{relative}/{relative_file}", allow_missing=False)
        try:
            if not path.is_file() or _hash(path.read_bytes()) != row["sha256"]:
                raise ReleaseStateError(f"generated capsule file hash does not match: {relative_file}")
        except OSError as error:
            raise ReleaseStateError(f"cannot read generated capsule payload file {relative_file}: {error}") from error
    return {
        "path": directory.relative_to(root).as_posix(),
        "sourceCommit": source_commit,
        "engineDigest": document["engineDigest"],
        "checkpointCount": len(checkpoints),
    }


def _matching_capsules(root: Path, payload_digest: str) -> list[dict]:
    base = root / "scratch" / "release-capsules"
    _checked_path(root, "scratch/release-capsules")
    if not base.exists():
        return []
    if not base.is_dir():
        raise ReleaseStateError("release capsule scratch path is not a directory")
    result = []
    for child in sorted(base.iterdir(), key=lambda item: item.name):
        if not child.name.startswith(payload_digest + "-"):
            continue
        _checked_path(root, child.relative_to(root).as_posix(), allow_missing=False)
        if not child.is_dir():
            raise ReleaseStateError("matching release capsule path is not a directory")
        result.append(_capsule_for_cleanup(root, child, payload_digest))
    return result


def _cleanup_targets(root: Path, payload_digest: str, capsules: Sequence[Mapping]) -> list[str]:
    targets = [row["path"] for row in capsules]
    for environment in ("staging", "prod"):
        relative = f"scratch/feature-release-plans/{environment}/{payload_digest}.json"
        path = _checked_path(root, relative)
        if path.exists():
            if not path.is_file():
                raise ReleaseStateError(f"matching target plan is not a regular file: {relative}")
            targets.append(relative)
    return list(dict.fromkeys(targets))


def _verify_evidence_hashes(root: Path, evidence: Sequence[Mapping], label: str) -> None:
    for row in evidence:
        path = _checked_path(root, row["path"], allow_missing=False)
        try:
            if not path.is_file() or _hash(path.read_bytes()) != row["sha256"]:
                raise ReleaseStateError(f"{label} evidence hash does not match: {row['path']}")
        except OSError as error:
            raise ReleaseStateError(f"{label} evidence is unavailable: {row['path']}") from error


def _verify_local_completion(root: Path, index: Mapping, capsule_row: Mapping, run: Mapping) -> None:
    try:
        capsule = release_capsule.load_capsule(root, root / capsule_row["path"])
    except (OSError, ValueError, release_capsule.ReleaseCapsuleError) as error:
        raise ReleaseStateError(f"production completion capsule cannot be reloaded: {error}") from error
    report_path = run["finalReportPath"]
    if not isinstance(report_path, str):
        raise ReleaseStateError("production completion has no retained final report path")
    report = _read_report_json(_checked_path(root, report_path, allow_missing=False), "production final report")
    proof = _validate_completion_proof(capsule, report, environment="prod")
    if (
        report.get("status") != "success" or report.get("databaseState") != "verified"
        or report.get("localRunId") != run["runId"]
        or proof["runId"] != run["runId"]
        or proof["sourceCommit"] != run["sourceCommit"]
        or proof["payloadDigest"] != run["payloadDigest"]
        or proof["engineDigest"] != run["engineDigest"]
        or proof["planDigest"] != run["planDigest"]
        or proof["targetScopes"] != run["targetScopes"]
        or proof["checkpointVerificationIds"] != run["checkpointVerificationIds"]
        or proof["finalReportPath"] != report_path
        or proof["finalReportSha256"] != run["finalReportSha256"]
        or _completion_core_hash(report) != run["finalReportSha256"]
    ):
        raise ReleaseStateError("production final report does not match the durable verified run")
    proof_steps = proof["steps"]
    if len(proof_steps) != len(run["steps"]):
        raise ReleaseStateError("production final report is missing durable step outcomes")
    for saved, embedded in zip(run["steps"], proof_steps, strict=True):
        if any(saved[field] != embedded[field] for field in ("step", "status", "receiptPath", "receiptSha256")):
            raise ReleaseStateError("production step receipt does not match the durable run record")
        receipt_path = _checked_path(root, saved["receiptPath"], allow_missing=False)
        try:
            receipt_raw = receipt_path.read_bytes()
        except OSError as error:
            raise ReleaseStateError(f"production step receipt is unavailable: {saved['receiptPath']}") from error
        if _hash(receipt_raw) != saved["receiptSha256"] or receipt_raw.decode("utf-8") != embedded["receiptJson"]:
            raise ReleaseStateError(f"production step receipt changed after verification: {saved['receiptPath']}")
    saved_verifications = [row for row in index["checkpointVerifications"] if row["runId"] == run["runId"]]
    embedded_verifications = proof["checkpointVerifications"]
    if sorted(saved_verifications, key=lambda row: row["checkpoint"]) != sorted(embedded_verifications, key=lambda row: row["checkpoint"]):
        raise ReleaseStateError("production checkpoint verification data differs from the retained local index")
    recovery_links = [row for row in index["recoveryLinks"] if row["verificationId"] in run["checkpointVerificationIds"]]
    if sorted(recovery_links, key=lambda row: row["verificationId"]) != sorted(proof["recoveryLinks"], key=lambda row: row["verificationId"]):
        raise ReleaseStateError("production recovery links differ from the retained completion report")
    for verification in embedded_verifications:
        _verify_evidence_hashes(root, verification["evidence"], "checkpoint verification")


def _cleanup_report(root: Path, index: Mapping, payload_digest: str) -> dict:
    blockers: list[str] = []
    capsule_rows = _matching_capsules(root, payload_digest)
    if not capsule_rows:
        blockers.append("generated capsule manifest is missing; production checkpoint coverage cannot be proven")
    attempts = [row for row in index["attempts"] if row["payloadDigest"] == payload_digest]
    if any(_is_unresolved(row, index["attempts"], index["recoveryLinks"], index["checkpointVerifications"]) for row in attempts):
        blockers.append("an unresolved attempt still references this payload")
    payload_runs = [row for row in index["rollouts"] if row["payloadDigest"] == payload_digest]
    if any(row["state"] in {"running", "unknown", "not-started"} for row in payload_runs):
        blockers.append("a rollout remains running or unresolved for this payload")
    for capsule in capsule_rows:
        candidates = [row for row in payload_runs if (
            row["environment"] == "prod" and row["engineDigest"] == capsule["engineDigest"]
            and row["sourceCommit"] == capsule["sourceCommit"]
        )]
        candidates.sort(key=lambda row: row["startedAt"])
        if not candidates or candidates[-1]["state"] != "verified":
            blockers.append(
                f"a complete verified production rollout is missing for source {capsule['sourceCommit'][:12]}"
            )
            continue
        try:
            _verify_local_completion(root, index, capsule, candidates[-1])
        except ReleaseStateError as error:
            blockers.append(f"production completion evidence is incomplete or changed: {error}")
    for row in index["cleanupHistory"]:
        if row["payloadDigest"] == payload_digest and row["state"] == "removing":
            blockers.append("a prior cleanup is still marked removing; reconcile its recorded paths")
    targets = _cleanup_targets(root, payload_digest, capsule_rows)
    return {
        "eligible": not blockers,
        "blockers": blockers,
        "targetPaths": targets,
        "removedPaths": [],
        "state": "dry-run",
    }


def cleanup_capsule(repo_root: Path, payload_digest: str, *, apply: bool = False) -> dict:
    """Dry-run by default; remove only verified generated capsule and plan files."""
    root = _root(repo_root)
    _digest(payload_digest, "cleanup payload digest")
    if type(apply) is not bool:
        raise ReleaseStateError("cleanup apply must be a boolean")
    if not apply:
        return _cleanup_report(root, _load_index(root), payload_digest)

    result: dict = {}

    def update(index: dict) -> dict:
        nonlocal result
        result = _cleanup_report(root, index, payload_digest)
        if not result["eligible"]:
            raise ReleaseStateError("capsule cleanup refused: " + "; ".join(result["blockers"]))
        cleanup = {
            "cleanupId": str(uuid.uuid4()),
            "payloadDigest": payload_digest,
            "requestedAt": _utc_now(),
            "state": "removing",
            "targetPaths": list(result["targetPaths"]),
            "removedPaths": [],
            "error": None,
        }
        index["cleanupHistory"].append(cleanup)
        _write_index(root, index)
        for relative in cleanup["targetPaths"]:
            path = _checked_path(root, relative, allow_missing=False)
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            except OSError as error:
                cleanup["state"] = "partial"
                cleanup["error"] = str(error)[:500] or type(error).__name__
                cleanup["removedPaths"] = [item for item in cleanup["targetPaths"] if not (root / PurePosixPath(item)).exists()]
                result.update({"state": "partial", "removedPaths": list(cleanup["removedPaths"]), "error": cleanup["error"]})
                _write_index(root, index)
                return index
            if (root / PurePosixPath(relative)).exists():
                cleanup["state"] = "partial"
                cleanup["error"] = f"cleanup target still exists after removal: {relative}"
                cleanup["removedPaths"] = [item for item in cleanup["targetPaths"] if not (root / PurePosixPath(item)).exists()]
                result.update({"state": "partial", "removedPaths": list(cleanup["removedPaths"]), "error": cleanup["error"]})
                _write_index(root, index)
                return index
            cleanup["removedPaths"].append(relative)
            _write_index(root, index)
        cleanup["state"] = "complete"
        _write_index(root, index)
        result.update({"state": "complete", "removedPaths": list(cleanup["removedPaths"]), "cleanupId": cleanup["cleanupId"]})
        return index

    _atomic_update(root, update)
    return result
