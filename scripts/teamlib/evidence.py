"""Canonical validation for protected release-test evidence."""

from __future__ import annotations

import json
import re
from typing import Any
from collections.abc import Mapping


class EvidenceError(ValueError):
    """Raised when test evidence cannot be used as promotion evidence."""


TARGET_IDENTITY_KEYS = {
    "project", "role", "environment", "target_kind", "instance_id",
    "db_name", "service", "workspace_id", "app_ids", "state_key",
    "binding_digest",
}
_EVIDENCE_KEYS = {
    "version", "final_status", "source_commit", "archive_digest",
    "toolchain_digest", "target_identity", "run_identity",
    "qualification_identity", "application_checks", "results",
}
_DATABASE_EVIDENCE_KEYS = (_EVIDENCE_KEYS - {"source_commit"}) | {"source"}
_SCHEMA_DATABASE_SOURCE_KEYS = {
    "kind", "instance_id", "history_cut", "history_digest", "frontier_digest",
}
_APP_DATABASE_SOURCE_KEYS = _SCHEMA_DATABASE_SOURCE_KEYS | {
    "app_generation", "app_tree_digest", "app_checks_digest", "master_contract_digest",
}
_QUALIFICATION_KEYS = {
    "target_kind", "observation_sequence", "observation_digest", "history_digest",
}
_CHECKS_KEYS = {"status", "checks_digest", "coverage", "unknown", "results"}
_COVERAGE_KEYS = {"apps", "pages", "checks", "unknown"}
_RESULT_KEYS = {
    "alias", "check_id", "page_id", "kind", "status", "expected_objects",
    "diagnostic", "observed",
}
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9._-]*$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise EvidenceError(f"test evidence {label} is malformed")
    return value


def _positive_int(value: Any, label: str) -> None:
    if type(value) is not int or value <= 0:
        raise EvidenceError(f"test evidence {label} must be a positive integer")


def _nonempty_text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise EvidenceError(f"test evidence {label} is missing")


def validate_database_source(value: Any) -> dict[str, Any]:
    """Validate and copy the database-ledger source identity used by format 3."""
    if not isinstance(value, Mapping) or set(value) not in (_SCHEMA_DATABASE_SOURCE_KEYS, _APP_DATABASE_SOURCE_KEYS):
        raise EvidenceError("test evidence database source is incomplete")
    is_app_source = set(value) == _APP_DATABASE_SOURCE_KEYS
    if value["kind"] != "dev-database":
        raise EvidenceError("test evidence database source kind is invalid")
    _nonempty_text(value["instance_id"], "database source instance ID")
    history_cut = value["history_cut"]
    if type(history_cut) is not int or history_cut < (0 if is_app_source else 1):
        raise EvidenceError("test evidence database source history cut is malformed")
    _digest(value["history_digest"], "database source history digest")
    _digest(value["frontier_digest"], "database source frontier digest")
    if is_app_source:
        _positive_int(value["app_generation"], "database source application generation")
        _digest(value["app_tree_digest"], "database source application tree digest")
        for field, label in (("app_checks_digest", "application checks"), ("master_contract_digest", "master contract")):
            if value[field] is not None:
                _digest(value[field], f"database source {label} digest")
    return dict(value)


def _validate_shape(value: Mapping[str, Any]) -> None:
    version = value.get("version")
    if type(version) is not int or version not in (2, 3):
        raise EvidenceError("test evidence has an unsupported format version")
    if version == 2:
        if set(value) not in (_EVIDENCE_KEYS, _EVIDENCE_KEYS | {"kind", "alias"}):
            raise EvidenceError("test evidence has an unexpected version-2 shape")
    elif set(value) != _DATABASE_EVIDENCE_KEYS | {"kind", "alias"}:
        raise EvidenceError("test evidence has an unexpected version-3 shape")
    if value["final_status"] != "PASS":
        raise EvidenceError("test evidence is not a successful final result")
    if version == 2:
        if not isinstance(value["source_commit"], str) or not _COMMIT_RE.fullmatch(value["source_commit"]):
            raise EvidenceError("test evidence source commit is malformed")
    else:
        validate_database_source(value["source"])

    if "kind" in value:
        kind = value["kind"]
        if kind not in ("schema", "app"):
            raise EvidenceError("test evidence release kind is invalid")
        if kind == "schema":
            if value.get("alias") is not None:
                raise EvidenceError("test evidence for schema release must have null alias")
        elif kind == "app":
            alias = value.get("alias")
            if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
                raise EvidenceError("test evidence for app release must have a valid application alias")

    identity = value["target_identity"]
    if not isinstance(identity, Mapping) or set(identity) != TARGET_IDENTITY_KEYS:
        raise EvidenceError("test evidence target identity is incomplete")
    for field in ("project", "role", "environment", "target_kind", "instance_id", "db_name", "service"):
        _nonempty_text(identity[field], f"target identity {field}")
    if identity["target_kind"] != "persistent":
        raise EvidenceError("test evidence target kind must be persistent")
    if identity["role"] != "test" or identity["environment"] != "test":
        raise EvidenceError("signed promotion evidence requires the protected test target")
    _positive_int(identity["workspace_id"], "target identity workspace_id")
    app_ids = identity["app_ids"]
    if not isinstance(app_ids, Mapping) or not app_ids:
        raise EvidenceError("test evidence target application bindings are incomplete")
    for alias, app_id in app_ids.items():
        if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
            raise EvidenceError("test evidence target application alias is unsafe")
        _positive_int(app_id, f"target identity app ID for {alias}")
    _digest(identity["state_key"], "target state key")
    _digest(identity["binding_digest"], "target binding digest")

    run_identity = value["run_identity"]
    if not isinstance(run_identity, Mapping) or not run_identity:
        raise EvidenceError("test evidence run identity is incomplete")
    for key, item in run_identity.items():
        if not isinstance(key, str) or not key or not isinstance(item, str) or not item:
            raise EvidenceError("test evidence run identity is malformed")

    qualification = value["qualification_identity"]
    if not isinstance(qualification, Mapping) or set(qualification) != _QUALIFICATION_KEYS:
        raise EvidenceError("test evidence qualification identity is incomplete")
    if qualification["target_kind"] != "persistent":
        raise EvidenceError("test evidence qualification target kind must be persistent")
    _positive_int(qualification["observation_sequence"], "observation sequence")


def _validate_digests(value: Mapping[str, Any]) -> None:
    _digest(value["archive_digest"], "archive digest")
    _digest(value["toolchain_digest"], "toolchain digest")
    if value["version"] == 3:
        validate_database_source(value["source"])
    qualification = value["qualification_identity"]
    _digest(qualification["observation_digest"], "observation digest")
    _digest(qualification["history_digest"], "history digest")
    _digest(value["target_identity"]["state_key"], "target state key")
    _digest(value["target_identity"]["binding_digest"], "target binding digest")


def _validate_results(value: Mapping[str, Any]) -> None:
    checks = value["application_checks"]
    if not isinstance(checks, Mapping) or set(checks) != _CHECKS_KEYS:
        raise EvidenceError("test evidence application checks are incomplete")
    if checks["status"] != "PASS":
        raise EvidenceError("candidate application checks did not pass")
    if type(checks["unknown"]) is not int or checks["unknown"] != 0:
        raise EvidenceError("test evidence contains UNKNOWN application checks")
    _digest(checks["checks_digest"], "application checks digest")

    app_ids = value["target_identity"]["app_ids"]
    aliases = sorted(app_ids)
    coverage = checks["coverage"]
    if not isinstance(coverage, Mapping) or set(coverage) != _COVERAGE_KEYS:
        raise EvidenceError("test evidence application coverage is incomplete")

    kind = value.get("kind")
    if kind == "schema":
        if coverage["apps"] != []:
            raise EvidenceError("test evidence for schema release must have empty app coverage")
        if coverage["checks"] != 0:
            raise EvidenceError("test evidence for schema release must have 0 checks")
        if coverage["unknown"] != 0:
            raise EvidenceError("test evidence application coverage contains UNKNOWN results")
        if coverage["pages"] != {}:
            raise EvidenceError("test evidence for schema release must have empty page coverage")
    elif kind == "app":
        selected_alias = value.get("alias")
        if selected_alias not in app_ids:
            raise EvidenceError("test evidence application is not in target bindings")
        if coverage["apps"] != [selected_alias]:
            raise EvidenceError("test evidence application coverage does not match selected application")
        if type(coverage["checks"]) is not int or coverage["checks"] <= 0:
            raise EvidenceError("test evidence application check coverage is empty")
        if type(coverage["unknown"]) is not int or coverage["unknown"] != 0:
            raise EvidenceError("test evidence application coverage contains UNKNOWN results")
        if coverage["unknown"] != checks["unknown"]:
            raise EvidenceError("test evidence application coverage is inconsistent")
        pages = coverage["pages"]
        if not isinstance(pages, Mapping) or set(pages) != {selected_alias}:
            raise EvidenceError("test evidence application page coverage is incomplete")
        page_ids = pages[selected_alias]
        if (
            not isinstance(page_ids, list)
            or not page_ids
            or any(type(page_id) is not int or page_id <= 0 for page_id in page_ids)
            or len(set(page_ids)) != len(page_ids)
        ):
            raise EvidenceError(f"test evidence page coverage is malformed: {selected_alias}")
    else:
        if coverage["apps"] != aliases:
            raise EvidenceError("test evidence application coverage does not match target bindings")
        if type(coverage["checks"]) is not int or coverage["checks"] <= 0:
            raise EvidenceError("test evidence application check coverage is empty")
        if type(coverage["unknown"]) is not int or coverage["unknown"] != 0:
            raise EvidenceError("test evidence application coverage contains UNKNOWN results")
        if coverage["unknown"] != checks["unknown"]:
            raise EvidenceError("test evidence application coverage is inconsistent")
        pages = coverage["pages"]
        if not isinstance(pages, Mapping) or set(pages) != set(aliases):
            raise EvidenceError("test evidence application page coverage is incomplete")
        for alias in aliases:
            page_ids = pages[alias]
            if (
                not isinstance(page_ids, list)
                or not page_ids
                or any(type(page_id) is not int or page_id <= 0 for page_id in page_ids)
                or len(set(page_ids)) != len(page_ids)
            ):
                raise EvidenceError(f"test evidence page coverage is malformed: {alias}")

    results = checks["results"]
    if not isinstance(results, list) or len(results) != coverage["checks"]:
        raise EvidenceError("test evidence application check results are incomplete")
    seen: set[tuple[str, str]] = set()
    for result in results:
        if not isinstance(result, Mapping) or set(result) != _RESULT_KEYS:
            raise EvidenceError("test evidence check result shape is not closed")
        alias = result["alias"]
        check_id = result["check_id"]
        if (
            not isinstance(alias, str)
            or alias not in app_ids
            or not isinstance(check_id, str)
            or not _CHECK_ID_RE.fullmatch(check_id)
        ):
            raise EvidenceError("test evidence check result identity is malformed")
        key = (alias, check_id)
        if key in seen:
            raise EvidenceError("test evidence contains duplicate check results")
        seen.add(key)
        page_id = result["page_id"]
        if type(page_id) is not int or page_id not in pages[alias]:
            raise EvidenceError("test evidence check result page is malformed")
        if result["kind"] not in {"select", "flow"} or result["status"] != "PASS":
            raise EvidenceError("test evidence check result is not a PASS")
        expected_objects = result["expected_objects"]
        if not isinstance(expected_objects, list) or any(not isinstance(item, str) for item in expected_objects):
            raise EvidenceError("test evidence expected objects are malformed")
        if not isinstance(result["diagnostic"], str) or not isinstance(result["observed"], Mapping):
            raise EvidenceError("test evidence check result details are malformed")

    top_results = value["results"]
    if top_results != {
        "migrations": "PASS",
        "application_deploy": "PASS",
        "application_checks": "PASS",
    }:
        raise EvidenceError("test evidence result set is incomplete or contains a failure")


def validate_test_evidence(raw: bytes) -> dict[str, Any]:
    """Validate canonical protected test evidence and return its decoded object."""
    if not isinstance(raw, bytes):
        raise EvidenceError("test evidence must be raw bytes")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError("test evidence is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or raw != canonical_json(value) + b"\n":
        raise EvidenceError("test evidence is not canonical JSON")
    _validate_shape(value)
    _validate_digests(value)
    _validate_results(value)
    return value


def validate_release_evidence_binding(
    evidence: Mapping[str, Any],
    *,
    archive_digest: str,
    source_commit: str | None = None,
    source: Mapping[str, Any] | None = None,
    checks_digest: str,
    kind: str | None = None,
    alias: str | None = None,
) -> None:
    """Require canonical evidence to describe one verified release bundle."""
    _digest(archive_digest, "verified archive digest")
    _digest(checks_digest, "verified application checks digest")
    if evidence.get("archive_digest") != archive_digest:
        raise EvidenceError("test evidence is for a different release archive")
    if (source_commit is None) == (source is None):
        raise EvidenceError("release evidence binding requires exactly one source identity")
    if source is not None:
        expected_source = validate_database_source(source)
        if evidence.get("version") != 3 or evidence.get("source") != expected_source:
            raise EvidenceError("test evidence is for a different database source")
        if "source_commit" in evidence:
            raise EvidenceError("database release evidence must not contain a source commit")
    elif "source" in evidence or evidence.get("source_commit") != source_commit:
        raise EvidenceError("test evidence is for a different source commit")
    checks = evidence.get("application_checks")
    if not isinstance(checks, Mapping) or checks.get("checks_digest") != checks_digest:
        raise EvidenceError(
            "test evidence application checks do not match the release archive"
        )
    if kind is not None:
        evidence_kind = evidence.get("kind")
        if evidence_kind != kind:
            raise EvidenceError(
                f"test evidence kind '{evidence_kind}' does not match release kind '{kind}'"
            )
    if alias is not None:
        evidence_alias = evidence.get("alias")
        if evidence_alias != alias:
            raise EvidenceError(
                f"test evidence application '{evidence_alias}' does not match release application '{alias}'"
            )
    elif kind == "schema" and evidence.get("alias") is not None:
        raise EvidenceError("test evidence application must be null for schema release")
