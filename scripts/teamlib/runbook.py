"""Offline production handoff runbook generation.

The runbook generator is intentionally a pure file operation.  It verifies the
release archive and a detached, signed test-evidence document, then produces a
human-readable handoff.  It never opens a database connection and it never
contains an automated production apply path.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from collections.abc import Mapping

from .release import ReleaseError, ReleasePlan, plan_release, verify_release


class RunbookError(RuntimeError):
    """Raised when a release cannot be handed to a production owner."""


@dataclass(frozen=True)
class Runbook:
    text: str
    pending: tuple[str, ...]
    archive_digest: str
    source_commit: str
    evidence_digest: str
    plan: ReleasePlan


def _read_file(path: str | Path, label: str) -> bytes:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise RunbookError(f"{label} is not a regular file: {candidate}")
    try:
        return candidate.read_bytes()
    except OSError as exc:
        raise RunbookError(f"could not read {label}: {candidate}") from exc


def _load_json(path: str | Path, label: str) -> tuple[bytes, dict[str, Any]]:
    raw = _read_file(path, label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RunbookError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise RunbookError(f"{label} must contain a JSON object")
    return raw, value


def _public_key(raw: bytes):
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = serialization.load_pem_public_key(raw)
    except Exception as exc:  # the precise cryptography exception varies by version
        raise RunbookError("trusted production handoff key is not a readable PEM public key") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise RunbookError("trusted production handoff key must be Ed25519")
    return key


def _signature_bytes(raw: bytes) -> bytes:
    if len(raw) == 64:
        return raw
    stripped = raw.strip()
    if len(stripped) == 64:
        return stripped
    try:
        decoded = base64.b64decode(stripped, validate=True)
    except Exception as exc:
        raise RunbookError("detached signature must be raw Ed25519 bytes or strict base64") from exc
    if len(decoded) != 64:
        raise RunbookError("detached signature must contain 64 Ed25519 signature bytes")
    return decoded


def _required_passes(value: Mapping[str, Any]) -> None:
    if value.get("version") != 1:
        raise RunbookError("test evidence has an unsupported format version")
    if value.get("final_status") != "PASS":
        raise RunbookError("test evidence is not a successful final result")
    results = value.get("results")
    if not isinstance(results, Mapping) or not results:
        raise RunbookError("test evidence has no required result set")
    failures = [str(name) for name, status in results.items() if status != "PASS"]
    if failures:
        raise RunbookError("test evidence contains non-PASS results: " + ", ".join(sorted(failures)))
    for field in ("source_commit", "archive_digest"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise RunbookError(f"test evidence is missing {field}")

    # These fields are required for the real CI handoff.  The compact fixture
    # accepted by the unit test has only the two required result keys, so the
    # checks are conditional for backwards-compatible template adoption.
    for field in ("qualification_sha", "toolchain_digest", "target_identity", "run_identity", "replay_identity"):
        if field not in value or not value[field]:
            raise RunbookError(f"test evidence has an empty {field}")
    checks = value.get("application_checks")
    if not isinstance(checks, Mapping) or checks.get("status") != "PASS":
        raise RunbookError("candidate application checks did not pass")
    if checks.get("unknown") or not checks.get("coverage") or not checks.get("checks_digest"):
        raise RunbookError("candidate application evidence is incomplete or contains UNKNOWN results")


def _target_value(target: Mapping[str, Any] | Any, name: str, default: Any = None) -> Any:
    if isinstance(target, Mapping):
        return target.get(name, default)
    return getattr(target, name, default)


def _target_document(target: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(target, Mapping):
        return dict(target)
    names = (
        "project", "role", "environment", "connection", "instance_id", "db_name",
        "service", "session_user", "current_schema", "alias", "workspace_id", "app_id",
        "parsing_schema", "ownership_mode", "binding_digest",
    )
    return {name: getattr(target, name) for name in names if hasattr(target, name)}


def _display(value: Any) -> str:
    if value is None or value == "":
        return "<not supplied>"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return str(value)


def _pending_lines(manifest: Any, plan: ReleasePlan) -> list[str]:
    by_id = {item.get("id"): item for item in manifest.migrations}
    lines: list[str] = []
    if not plan.pending:
        return ["- none (the owner-supplied history already contains every artifact migration)"]
    for migration_id in plan.pending:
        item = by_id.get(migration_id, {})
        dependencies = item.get("dependencies") or []
        lines.append(
            f"- {migration_id} checksum={item.get('checksum', '<unknown>')} "
            f"target={item.get('target', '<unknown>')} destructive={item.get('destructive', False)} "
            f"dependencies={_display(dependencies)}"
        )
    return lines


def _runbook_text(manifest: Any, plan: ReleasePlan, target: Mapping[str, Any], evidence: Mapping[str, Any], evidence_digest: str) -> str:
    app_digests = manifest.app_tree_digests or {}
    target_identity = evidence.get("target_identity", target)
    lines = [
        "# Production release handoff",
        "",
        "This document is an offline handoff. It is not an automated production apply command.",
        "A production owner must review the archive, backup/restore evidence, maintenance window,",
        "supported-object expectations and data checks before writing anything.",
        "",
        "## Immutable release identity",
        "",
        f"- Release version: {manifest.version}",
        f"- Source commit: {manifest.source_commit}",
        f"- Source tree digest: {manifest.source_tree}",
        f"- Exact release.tar SHA-256: {plan.archive_digest}",
        f"- Signed evidence SHA-256: {evidence_digest}",
        f"- Toolchain declaration: {_display(manifest.toolchain)}",
        "",
        "## Destination contract",
        "",
        f"- Environment: {_display(target.get('environment'))}",
        f"- Instance: {_display(target.get('instance_id'))}",
        f"- Database/service: {_display(target.get('db_name'))} / {_display(target.get('service'))}",
        f"- Workspace/application bindings: {_display(target.get('workspace_id'))} / {_display(target.get('app_ids', target.get('app_id'))) }",
        f"- Session/current schema: {_display(target.get('session_user'))} / {_display(target.get('current_schema'))}",
        f"- Metadata owner: {_display(target.get('metadata_schema', target.get('metadata_owner')))}",
        f"- Evidence target identity: {_display(target_identity)}",
        "",
        "## Pending migration plan",
        "",
        f"Artifact history digest: {plan.artifact_history_digest}",
        f"Destination target digest: {plan.target_digest}",
        *_pending_lines(manifest, plan),
        "",
        "## Application and master order",
        "",
        "Deploy master applications and required shared components before subscribers.",
        "Verify every packaged application tree against its manifest digest before import:",
    ]
    if app_digests:
        lines.extend(f"- {alias}: {digest}" for alias, digest in sorted(app_digests.items()))
    else:
        lines.append("- none packaged")
    lines.extend([
        "",
        "## Owner checklist",
        "",
        "1. Verify the exact release.tar SHA-256 and the detached evidence signature against the independently held trust key.",
        "2. Confirm backups, restore evidence, maintenance/destructive prerequisites and supported Oracle/APEX object types.",
        "3. Re-read destination identity and migration history under the metadata-owner mutex; stop if identity or history changed.",
        "4. Apply only the listed pending migrations in dependency order. Do not blindly replay the full migration history; DDL rollback is not assumed.",
        "5. Import applications in master-before-subscriber order, then verify re-exported bytes, subscriptions, structural checks and data checks.",
        "6. Record attempts, observations and final status through the isolated metadata schema, including success-before-log uncertainty.",
        "7. If any outcome is uncertain, stop, retain evidence and use the recovery owner procedure before retrying.",
        "",
        "Source SQL is trusted reviewed deployment code, not a sandbox. This handoff supplies no production credential and no repository-side production apply switch.",
    ])
    return "\n".join(lines) + "\n"


def gen_runbook(
    release_tar: str | Path,
    history: Mapping[str, Any],
    target: Mapping[str, Any] | Any,
    test_evidence: str | Path,
    signature: str | Path,
    trust_key: str | Path,
) -> Runbook:
    """Verify an immutable release and signed evidence, then draft a handoff."""
    try:
        manifest = verify_release(release_tar)
    except ReleaseError as exc:
        raise RunbookError(str(exc)) from exc
    evidence_bytes, evidence = _load_json(test_evidence, "test evidence")
    _required_passes(evidence)
    if evidence["archive_digest"] != manifest.archive_digest:
        raise RunbookError("test evidence is for a different release archive")
    if evidence["source_commit"] != manifest.source_commit:
        raise RunbookError("test evidence is for a different source commit")
    if evidence.get("qualification_sha") != manifest.source_commit:
        raise RunbookError("test evidence qualification SHA does not match the release source")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(evidence.get("toolchain_digest", ""))):
        raise RunbookError("test evidence toolchain digest is malformed")
    key = _public_key(_read_file(trust_key, "trust key"))
    try:
        key.verify(_signature_bytes(_read_file(signature, "detached signature")), evidence_bytes)
    except Exception as exc:
        raise RunbookError("test evidence signature verification failed") from exc
    target_document = _target_document(target)
    if target_document.get("environment") != "production":
        raise RunbookError("production handoff requires a production target contract")
    try:
        plan = plan_release(release_tar, history, target_document)
    except ReleaseError as exc:
        raise RunbookError(str(exc)) from exc
    evidence_digest = hashlib.sha256(evidence_bytes).hexdigest()
    text = _runbook_text(manifest, plan, target_document, evidence, evidence_digest)
    return Runbook(text, plan.pending, manifest.archive_digest, manifest.source_commit, evidence_digest, plan)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="gen-runbook")
    parser.add_argument("archive")
    parser.add_argument("--history", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--test-evidence", required=True)
    parser.add_argument("--signature", required=True)
    parser.add_argument("--trust-key", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(list(argv or []))
    try:
        history_raw = json.loads(Path(args.history).read_text(encoding="utf-8"))
        target_raw = json.loads(Path(args.target).read_text(encoding="utf-8"))
        history = history_raw.get("history", history_raw) if isinstance(history_raw, dict) else {}
        runbook = gen_runbook(args.archive, history, target_raw, args.test_evidence, args.signature, args.trust_key)
        Path(args.out).write_text(runbook.text, encoding="utf-8", newline="\n")
        print(json.dumps({"archive_digest": runbook.archive_digest, "source_commit": runbook.source_commit, "pending": runbook.pending}, sort_keys=True))
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, RunbookError) as exc:
        raise SystemExit(str(exc)) from exc
