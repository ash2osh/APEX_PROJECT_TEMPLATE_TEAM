"""Capsule cleanup and local completion evidence, moved out of scripts/release_state.py.

scripts/release_state.py keeps the state file, the writer lease and the rollout records. Names that stay
there are read as release_state.<name> at call time, so patches on scripts.release_state still reach this
code. release_state re-exports the moved names that tests and other scripts reference.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import stat
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from . import release_capsule
from .path_utils import relative_to_root

if TYPE_CHECKING:
    from .release_capsule import ReleaseCapsule


def _completion_core_hash(report: Mapping) -> str:
    core = dict(report)
    core.pop("completionProof", None)
    return release_state._hash(release_state._canonical_bytes(core))


def _validate_completion_proof(capsule: ReleaseCapsule, report: Mapping, *, environment: str) -> dict:
    proof_fields = {
        "schemaVersion", "runId", "environment", "sourceCommit", "payloadDigest", "engineDigest",
        "planDigest", "targetScopes", "state", "databaseState", "steps", "checkpointVerificationIds",
        "checkpointVerifications", "recoveryLinks", "budget", "finalReportPath", "finalReportSha256",
    }
    proof = report.get("completionProof")
    if not isinstance(proof, Mapping) or set(proof) != proof_fields:
        raise release_state.ReleaseStateError("rollout completion proof is missing or malformed")
    if type(proof["schemaVersion"]) is not int or proof["schemaVersion"] != 1:
        raise release_state.ReleaseStateError("rollout completion proof schemaVersion is unsupported")
    run_id = release_state._uuid(proof["runId"], "completion proof runId")
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
        raise release_state.ReleaseStateError("rollout completion proof identity or outcome does not match the report and capsule")
    release_state._commit(proof["sourceCommit"])
    for field in ("payloadDigest", "engineDigest", "planDigest"):
        release_state._digest(proof[field], f"completion proof {field}")
    scopes = proof["targetScopes"]
    if not isinstance(scopes, list) or not scopes:
        raise release_state.ReleaseStateError("completion proof target scopes are missing")
    clean_scopes = [release_state._validate_scope(scope, "completion proof target scope") for scope in scopes]
    scope_keys = [release_state._canonical_bytes(scope) for scope in clean_scopes]
    if scope_keys != sorted(scope_keys) or len(scope_keys) != len(set(scope_keys)):
        raise release_state.ReleaseStateError("completion proof target scopes must be unique and ordered")

    try:
        capsule_document = release_capsule._read_capsule_json(Path(capsule.root) / "capsule.json")[0]
    except (OSError, ValueError, release_capsule.ReleaseCapsuleError) as error:
        raise release_state.ReleaseStateError(f"cannot read capsule definition for completion proof: {error}") from error
    definition = capsule_document.get("payloadDefinition")
    step_definitions = definition.get("steps") if isinstance(definition, Mapping) else None
    if not isinstance(step_definitions, list) or not step_definitions:
        raise release_state.ReleaseStateError("capsule rollout step definitions are missing")
    checkpoint_count = len(capsule.checkpoints)
    if sum(1 for step in step_definitions if isinstance(step, Mapping) and step.get("type") == "feature-migrate") != checkpoint_count:
        raise release_state.ReleaseStateError("capsule rollout steps do not match its feature checkpoints")

    verification_rows = proof["checkpointVerifications"]
    verification_ids = proof["checkpointVerificationIds"]
    if (
        not isinstance(verification_rows, list) or len(verification_rows) != checkpoint_count
        or not isinstance(verification_ids, list) or len(verification_ids) != checkpoint_count
    ):
        raise release_state.ReleaseStateError("completion proof must contain every checkpoint verification")
    clean_verifications = [release_state._validate_checkpoint_verification(row) for row in verification_rows]
    clean_verifications.sort(key=lambda row: row["checkpoint"])
    checkpoints = list(range(1, checkpoint_count + 1))
    if [row["checkpoint"] for row in clean_verifications] != checkpoints:
        raise release_state.ReleaseStateError("completion proof checkpoint verifications are missing or duplicated")
    if verification_ids != [row["verificationId"] for row in clean_verifications]:
        raise release_state.ReleaseStateError("completion proof checkpoint verification IDs do not match their records")
    for row in clean_verifications:
        if any(row[field] != proof[field] for field in (
            "runId", "environment", "sourceCommit", "payloadDigest", "engineDigest", "planDigest",
        )) or row["targetScope"] not in clean_scopes:
            raise release_state.ReleaseStateError("checkpoint verification does not match the rollout completion proof")

    links = proof["recoveryLinks"]
    if not isinstance(links, list):
        raise release_state.ReleaseStateError("completion proof recovery links must be an array")
    clean_links = [release_state._validate_recovery_link(item) for item in links]
    if len({row["oldAttemptId"] for row in clean_links}) != len(clean_links):
        raise release_state.ReleaseStateError("completion proof contains duplicate recovery links")
    for verification in clean_verifications:
        matching = [row for row in clean_links if row["verificationId"] == verification["verificationId"]]
        if verification["mode"] == "recovery":
            if len(matching) != 1 or matching[0]["reconciliationEvidence"] not in {
                item["path"] for item in verification["evidence"]
            }:
                raise release_state.ReleaseStateError("recovery checkpoint is missing its portable reconciliation evidence link")
        elif matching:
            raise release_state.ReleaseStateError("non-recovery checkpoint cannot carry a recovery link")
    if any(row["verificationId"] not in verification_ids for row in clean_links):
        raise release_state.ReleaseStateError("completion proof recovery link references an unknown checkpoint")

    step_rows = proof["steps"]
    report_steps = report.get("steps")
    if not isinstance(step_rows, list) or len(step_rows) != len(step_definitions) or not isinstance(report_steps, list) or len(report_steps) != len(step_definitions):
        raise release_state.ReleaseStateError("completion proof must contain every rollout step")
    step_fields = {"step", "status", "receiptPath", "receiptSha256", "receiptJson"}
    manifest_digest = capsule_document.get("rolloutManifestSha256")
    expected_excluded: list[str] = []
    expected_data_seconds = 0.0
    expected_ords_seconds = 0.0
    for number, (step_row, report_step, definition_step) in enumerate(
        zip(step_rows, report_steps, step_definitions, strict=True), start=1,
    ):
        if not isinstance(step_row, Mapping) or set(step_row) != step_fields:
            raise release_state.ReleaseStateError("completion proof step receipt is malformed")
        receipt_path = release_state._relative_path(step_row["receiptPath"], "completion proof receipt path")
        receipt_raw = step_row["receiptJson"]
        if not isinstance(receipt_raw, str) or "\r" in receipt_raw or receipt_raw.startswith("\ufeff"):
            raise release_state.ReleaseStateError("completion proof receipt must be UTF-8/LF text")
        receipt_bytes = receipt_raw.encode("utf-8")
        receipt_hash = release_state._digest(step_row["receiptSha256"], "completion proof receipt hash")
        if release_state._hash(receipt_bytes) != receipt_hash:
            raise release_state.ReleaseStateError("completion proof embedded step receipt hash does not match")
        release_state._uuid(run_id, "completion proof runId")
        if step_row["step"] != number or step_row["status"] != "verified":
            raise release_state.ReleaseStateError("completion proof steps must be ordered and verified")
        try:
            receipt = json.loads(receipt_raw, object_pairs_hook=release_state._unique_pairs)
        except (json.JSONDecodeError, release_state.ReleaseStateError) as error:
            raise release_state.ReleaseStateError("completion proof embedded step receipt is invalid JSON") from error
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
            raise release_state.ReleaseStateError("completion proof step receipt does not match the frozen rollout step")
        if type(receipt["durationSeconds"]) not in {int, float} or not math.isfinite(receipt["durationSeconds"]) or receipt["durationSeconds"] < 0:
            raise release_state.ReleaseStateError("completion proof step receipt duration is invalid")
        if not isinstance(receipt["evidencePaths"], list) or any(not isinstance(item, str) for item in receipt["evidencePaths"]):
            raise release_state.ReleaseStateError("completion proof step receipt evidence paths are malformed")

        step_type = definition_step.get("type")
        expected_phase = None
        if step_type in {"sql-script", "ords-import"}:
            source_name = release_state._relative_path(definition_step.get("file"), "auxiliary rollout source path")
            try:
                frozen_source = (Path(capsule.root) / "payload" / PurePosixPath(source_name)).read_bytes()
                from . import rollout

                expected_phase = rollout.classify_auxiliary_step(definition_step, frozen_source)
                expected_reason = rollout._auxiliary_classification_reason(step_type, expected_phase)
            except (OSError, ValueError) as error:
                raise release_state.ReleaseStateError(f"cannot validate frozen auxiliary phase for step {number}: {error}") from error
            if expected_phase == "uncovered-object":
                expected_excluded.append(f"step-{number:03d}:{step_type}")
            elif expected_phase == "data":
                expected_data_seconds += release_state._number(report_step.get("phaseSeconds"), f"step {number} phaseSeconds")
            elif expected_phase == "ords":
                expected_ords_seconds += release_state._number(report_step.get("phaseSeconds"), f"step {number} phaseSeconds")
        elif step_type == "migrate":
            expected_phase = "migration"
            from . import rollout

            expected_reason = rollout._auxiliary_classification_reason("migrate", expected_phase)
        if expected_phase is not None and (
            report_step.get("phase") != expected_phase
            or report_step.get("classificationReason") != expected_reason
        ):
            raise release_state.ReleaseStateError(f"rollout step {number} phase does not match its frozen source classification")

    budget = proof["budget"]
    if not isinstance(budget, Mapping) or set(budget) != release_state.BUDGET_FIELDS:
        raise release_state.ReleaseStateError("completion proof budget is malformed")
    clean_budget = release_state._validate_budget(budget)
    report_budget = report.get("objectBudget")
    expected_seconds = getattr(capsule, "database_object_budget_seconds", None)
    expected_migrations = [number for number, step in enumerate(step_definitions, start=1) if step.get("type") == "migrate"]
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
        or report_budget.get("includedMigrationSteps") != expected_migrations
        or report_budget.get("excludedObjectSteps") != expected_excluded
        or expected_excluded != []
    ):
        raise release_state.ReleaseStateError("completion proof budget does not prove full in-budget checkpoint coverage")
    if any(step.get("type") in {"sql-script", "ords-import"} for step in step_definitions):
        data_seconds = release_state._number(report.get("dataSeconds"), "rollout dataSeconds")
        ords_seconds = release_state._number(report.get("ordsSeconds"), "rollout ordsSeconds")
        tolerance = max(0.01, len(step_definitions) * 0.01)
        if (
            abs(data_seconds - round(expected_data_seconds, 3)) > tolerance
            or abs(ords_seconds - round(expected_ords_seconds, 3)) > tolerance
        ):
            raise release_state.ReleaseStateError("rollout phase timing totals do not match the verified data and ORDS steps")
    report_path = release_state._relative_path(proof["finalReportPath"], "completion proof final report path")
    if report.get("reportFiles", {}).get("json") != report_path:
        raise release_state.ReleaseStateError("completion proof final report path does not match its report file record")
    final_hash = release_state._digest(proof["finalReportSha256"], "completion proof final report hash")
    if _completion_core_hash(report) != final_hash:
        raise release_state.ReleaseStateError("completion proof report hash does not match the report body")
    return dict(proof)


def _safe_tree(root: Path, directory: Path) -> None:
    """Refuse capsule deletion if any descendant is a symlink or special file."""
    try:
        relative = relative_to_root(directory, root).as_posix()
    except ValueError as error:
        raise release_state.ReleaseStateError("capsule directory is outside the repository") from error
    base = release_state._checked_path(root, relative, allow_missing=False)
    if not base.is_dir():
        raise release_state.ReleaseStateError("capsule cleanup target is not a directory")
    for current, dirnames, filenames in os.walk(base, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in [*dirnames, *filenames]:
            candidate = current_path / name
            try:
                info = candidate.lstat()
            except OSError as error:
                raise release_state.ReleaseStateError(f"capsule tree changed during cleanup inspection: {candidate}") from error
            if candidate.is_symlink() or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise release_state.ReleaseStateError(f"capsule tree contains a symlink or reparse point: {candidate}")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise release_state.ReleaseStateError(f"capsule tree contains a special file: {candidate}")


def _capsule_for_cleanup(root: Path, directory: Path, payload_digest: str) -> dict:
    try:
        relative = relative_to_root(directory, root).as_posix()
        manifest_path = release_state._checked_path(root, f"{relative}/capsule.json", allow_missing=False)
        document = release_capsule._read_capsule_json(manifest_path)[0]
    except (OSError, ValueError, release_capsule.ReleaseCapsuleError) as error:
        if isinstance(error, release_state.ReleaseStateError):
            raise
        raise release_state.ReleaseStateError(f"cannot validate generated capsule before cleanup: {error}") from error
    expected_fields = {
        "schemaVersion", "payloadDigest", "engineDigest", "engineFiles", "sourceCommit",
        "rolloutManifestPath", "rolloutManifestSha256", "payloadDefinition", "files", "sourceInputs", "checkpoints",
    }
    if set(document) != expected_fields or type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1:
        raise release_state.ReleaseStateError("generated capsule manifest has unsupported or missing fields")
    if document["payloadDigest"] != payload_digest:
        raise release_state.ReleaseStateError("generated capsule manifest payload does not match cleanup request")
    release_state._digest(document["engineDigest"], "capsule engineDigest")
    source_commit = release_state._commit(document["sourceCommit"])
    definition = document["payloadDefinition"]
    definition_fields = {
        "schemaVersion", "rolloutManifest", "databaseObjectBudgetSeconds", "engineDigest",
        "files", "steps", "sourceToDriver", "sourceInputs", "checkpoints",
    }
    if not isinstance(definition, Mapping) or set(definition) != definition_fields:
        raise release_state.ReleaseStateError("generated capsule payloadDefinition is malformed")
    if type(definition["schemaVersion"]) is not int or definition["schemaVersion"] != 1:
        raise release_state.ReleaseStateError("generated capsule payloadDefinition schema is unsupported")
    if release_state._hash(release_state._canonical_bytes(definition)) != payload_digest:
        raise release_state.ReleaseStateError("generated capsule payloadDefinition digest does not match")
    if definition.get("engineDigest") != document["engineDigest"]:
        raise release_state.ReleaseStateError("generated capsule engine digest does not match its payload")
    engine_files = document["engineFiles"]
    if not isinstance(engine_files, list) or not engine_files or any(not isinstance(item, str) for item in engine_files):
        raise release_state.ReleaseStateError("generated capsule engine file list is malformed")
    engine_files = [release_state._relative_path(item, "capsule engine file") for item in engine_files]
    if len(engine_files) != len(set(engine_files)):
        raise release_state.ReleaseStateError("generated capsule engine file list contains duplicates")
    rollout = definition.get("rolloutManifest")
    if not isinstance(rollout, Mapping) or set(rollout) != {"path", "sha256"}:
        raise release_state.ReleaseStateError("generated capsule rollout manifest record is malformed")
    rollout_path = release_state._relative_path(rollout["path"], "capsule rollout manifest path")
    rollout_hash = release_state._digest(rollout["sha256"], "capsule rollout manifest hash")
    if (
        document["rolloutManifestPath"] != rollout_path
        or document["rolloutManifestSha256"] != rollout_hash
        or definition["databaseObjectBudgetSeconds"] is not None
        and (type(definition["databaseObjectBudgetSeconds"]) is not int or not 1 <= definition["databaseObjectBudgetSeconds"] <= 300)
    ):
        raise release_state.ReleaseStateError("generated capsule rollout metadata or budget is malformed")
    checkpoints = document["checkpoints"]
    steps = definition.get("steps")
    if not isinstance(checkpoints, list) or definition.get("checkpoints") != checkpoints:
        raise release_state.ReleaseStateError("generated capsule checkpoint list is malformed")
    if not isinstance(steps, list) or not steps or any(not isinstance(step, Mapping) for step in steps):
        raise release_state.ReleaseStateError("generated capsule step list is malformed")
    if sum(1 for step in steps if step.get("type") == "feature-migrate") != len(checkpoints):
        raise release_state.ReleaseStateError("generated capsule steps do not match its checkpoint list")
    if not release_state._has_checkpoint_or_migration(steps):
        raise release_state.ReleaseStateError("generated capsule must contain a feature checkpoint or a migrate step")
    for field in ("files", "sourceInputs"):
        if not isinstance(document[field], list) or definition.get(field) != document[field]:
            raise release_state.ReleaseStateError(f"generated capsule {field} list does not match its payload")
    file_rows = document["files"]
    file_map: dict[str, dict] = {}
    for row in file_rows:
        if not isinstance(row, Mapping) or set(row) != {"path", "sha256", "kind"}:
            raise release_state.ReleaseStateError("generated capsule file inventory row is malformed")
        path = release_state._relative_path(row["path"], "capsule payload file path")
        if path == "capsule.json" or not isinstance(row["kind"], str) or row["kind"] not in {"driver", "source"}:
            raise release_state.ReleaseStateError("generated capsule file inventory contains an unexpected entry")
        if (row["kind"] == "driver" and not path.startswith("drivers/")) or (
            row["kind"] == "source" and not path.startswith("payload/")
        ):
            raise release_state.ReleaseStateError("generated capsule file kind does not match its path")
        release_state._digest(row["sha256"], "capsule file hash")
        if path in file_map:
            raise release_state.ReleaseStateError("generated capsule file inventory contains duplicates")
        file_map[path] = dict(row)
    if file_rows != sorted(file_rows, key=lambda row: row["path"]):
        raise release_state.ReleaseStateError("generated capsule file inventory is not ordered")
    manifest_file = f"payload/{rollout_path}"
    if manifest_file not in file_map or file_map[manifest_file]["sha256"] != rollout_hash:
        raise release_state.ReleaseStateError("generated capsule rollout manifest is missing from its file inventory")
    expected_name = f"{payload_digest}-{source_commit[:12]}"
    if directory.name != expected_name:
        raise release_state.ReleaseStateError("generated capsule directory name does not match its manifest")
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
        raise release_state.ReleaseStateError("generated capsule contains missing or unlisted files in its payload inventory")
    for relative_file, row in file_map.items():
        path = release_state._checked_path(root, f"{relative}/{relative_file}", allow_missing=False)
        try:
            if not path.is_file() or release_state._hash(path.read_bytes()) != row["sha256"]:
                raise release_state.ReleaseStateError(f"generated capsule file hash does not match: {relative_file}")
        except OSError as error:
            raise release_state.ReleaseStateError(f"cannot read generated capsule payload file {relative_file}: {error}") from error
    return {
        "path": directory.relative_to(root).as_posix(),
        "sourceCommit": source_commit,
        "engineDigest": document["engineDigest"],
        "checkpointCount": len(checkpoints),
    }


def _matching_capsules(root: Path, payload_digest: str) -> list[dict]:
    base = root / "scratch" / "release-capsules"
    release_state._checked_path(root, "scratch/release-capsules")
    if not base.exists():
        return []
    if not base.is_dir():
        raise release_state.ReleaseStateError("release capsule scratch path is not a directory")
    result = []
    for child in sorted(base.iterdir(), key=lambda item: item.name):
        if not child.name.startswith(payload_digest + "-"):
            continue
        release_state._checked_path(root, child.relative_to(root).as_posix(), allow_missing=False)
        if not child.is_dir():
            raise release_state.ReleaseStateError("matching release capsule path is not a directory")
        result.append(_capsule_for_cleanup(root, child, payload_digest))
    return result


def _cleanup_targets(root: Path, payload_digest: str, capsules: Sequence[Mapping]) -> list[str]:
    targets = [row["path"] for row in capsules]
    for environment in ("staging", "prod"):
        relative = f"scratch/feature-release-plans/{environment}/{payload_digest}.json"
        path = release_state._checked_path(root, relative)
        if path.exists():
            if not path.is_file():
                raise release_state.ReleaseStateError(f"matching target plan is not a regular file: {relative}")
            targets.append(relative)
    return list(dict.fromkeys(targets))


def _verify_evidence_hashes(root: Path, evidence: Sequence[Mapping], label: str) -> None:
    for row in evidence:
        path = release_state._checked_path(root, row["path"], allow_missing=False)
        try:
            if not path.is_file() or release_state._hash(path.read_bytes()) != row["sha256"]:
                raise release_state.ReleaseStateError(f"{label} evidence hash does not match: {row['path']}")
        except OSError as error:
            raise release_state.ReleaseStateError(f"{label} evidence is unavailable: {row['path']}") from error


def _verify_local_completion(root: Path, index: Mapping, capsule_row: Mapping, run: Mapping) -> None:
    try:
        capsule = release_capsule.load_capsule(root, root / capsule_row["path"])
    except (OSError, ValueError, release_capsule.ReleaseCapsuleError) as error:
        raise release_state.ReleaseStateError(f"production completion capsule cannot be reloaded: {error}") from error
    report_path = run["finalReportPath"]
    if not isinstance(report_path, str):
        raise release_state.ReleaseStateError("production completion has no retained final report path")
    report = release_state._read_report_json(release_state._checked_path(root, report_path, allow_missing=False), "production final report")
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
        raise release_state.ReleaseStateError("production final report does not match the durable verified run")
    proof_steps = proof["steps"]
    if len(proof_steps) != len(run["steps"]):
        raise release_state.ReleaseStateError("production final report is missing durable step outcomes")
    for saved, embedded in zip(run["steps"], proof_steps, strict=True):
        if any(saved[field] != embedded[field] for field in ("step", "status", "receiptPath", "receiptSha256")):
            raise release_state.ReleaseStateError("production step receipt does not match the durable run record")
        receipt_path = release_state._checked_path(root, saved["receiptPath"], allow_missing=False)
        try:
            receipt_raw = receipt_path.read_bytes()
        except OSError as error:
            raise release_state.ReleaseStateError(f"production step receipt is unavailable: {saved['receiptPath']}") from error
        if release_state._hash(receipt_raw) != saved["receiptSha256"] or receipt_raw.decode("utf-8") != embedded["receiptJson"]:
            raise release_state.ReleaseStateError(f"production step receipt changed after verification: {saved['receiptPath']}")
    saved_verifications = [row for row in index["checkpointVerifications"] if row["runId"] == run["runId"]]
    embedded_verifications = proof["checkpointVerifications"]
    if sorted(saved_verifications, key=lambda row: row["checkpoint"]) != sorted(embedded_verifications, key=lambda row: row["checkpoint"]):
        raise release_state.ReleaseStateError("production checkpoint verification data differs from the retained local index")
    recovery_links = [row for row in index["recoveryLinks"] if row["verificationId"] in run["checkpointVerificationIds"]]
    if sorted(recovery_links, key=lambda row: row["verificationId"]) != sorted(proof["recoveryLinks"], key=lambda row: row["verificationId"]):
        raise release_state.ReleaseStateError("production recovery links differ from the retained completion report")
    for verification in embedded_verifications:
        _verify_evidence_hashes(root, verification["evidence"], "checkpoint verification")


def _superseded_reconciliations(payload_runs: Sequence[Mapping]) -> set[str]:
    """Run ids of reconciled runs that a later verified run of the same environment supersedes.

    Runs are ordered by start time; equal timestamps keep their index order, as the production candidate choice does.
    A verified run in another environment does not supersede a reconciled run.
    """
    ordered = sorted(payload_runs, key=lambda row: row["startedAt"])
    superseded: set[str] = set()
    for position, run in enumerate(ordered):
        if run.get("reconciliation") is None:
            continue
        if any(
            later["environment"] == run["environment"] and later["state"] == "verified"
            for later in ordered[position + 1:]
        ):
            superseded.add(run["runId"])
    return superseded


def _cleanup_report(root: Path, index: Mapping, payload_digest: str) -> dict:
    blockers: list[str] = []
    capsule_rows = _matching_capsules(root, payload_digest)
    if not capsule_rows:
        blockers.append("generated capsule manifest is missing; production checkpoint coverage cannot be proven")
    attempts = [row for row in index["attempts"] if row["payloadDigest"] == payload_digest]
    if any(release_state._is_unresolved(row, index["attempts"], index["recoveryLinks"], index["checkpointVerifications"]) for row in attempts):
        blockers.append("an unresolved attempt still references this payload")
    payload_runs = [row for row in index["rollouts"] if row["payloadDigest"] == payload_digest]
    # A reconciled run that a later verified run of its environment supersedes no longer blocks cleanup.
    superseded = _superseded_reconciliations(payload_runs)
    if any(row["state"] in {"running", "unknown", "not-started"} and row["runId"] not in superseded for row in payload_runs):
        blockers.append("a rollout remains running or unresolved for this payload")
    if any(row.get("reconciliation") is not None and row["runId"] not in superseded for row in payload_runs):
        blockers.append("a rollout for this payload was closed by reconciliation, not by verification")
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
        except release_state.ReleaseStateError as error:
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
    root = release_state._root(repo_root)
    release_state._digest(payload_digest, "cleanup payload digest")
    if type(apply) is not bool:
        raise release_state.ReleaseStateError("cleanup apply must be a boolean")
    if not apply:
        return _cleanup_report(root, release_state._load_index(root), payload_digest)

    result: dict = {}

    def update(index: dict) -> dict:
        nonlocal result
        result = _cleanup_report(root, index, payload_digest)
        if not result["eligible"]:
            raise release_state.ReleaseStateError("capsule cleanup refused: " + "; ".join(result["blockers"]))
        cleanup = {
            "cleanupId": str(uuid.uuid4()),
            "payloadDigest": payload_digest,
            "requestedAt": release_state._utc_now(),
            "state": "removing",
            "targetPaths": list(result["targetPaths"]),
            "removedPaths": [],
            "error": None,
        }
        index["cleanupHistory"].append(cleanup)
        release_state._write_index(root, index)
        for relative in cleanup["targetPaths"]:
            path = release_state._checked_path(root, relative, allow_missing=False)
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
                release_state._write_index(root, index)
                return index
            if (root / PurePosixPath(relative)).exists():
                cleanup["state"] = "partial"
                cleanup["error"] = f"cleanup target still exists after removal: {relative}"
                cleanup["removedPaths"] = [item for item in cleanup["targetPaths"] if not (root / PurePosixPath(item)).exists()]
                result.update({"state": "partial", "removedPaths": list(cleanup["removedPaths"]), "error": cleanup["error"]})
                release_state._write_index(root, index)
                return index
            cleanup["removedPaths"].append(relative)
            release_state._write_index(root, index)
        cleanup["state"] = "complete"
        release_state._write_index(root, index)
        result.update({"state": "complete", "removedPaths": list(cleanup["removedPaths"]), "cleanupId": cleanup["cleanupId"]})
        return index

    release_state._atomic_update(root, update)
    return result


# Imported last: this module reads release_state.<name> only when it runs, so release_state can bind the
# moved names from this module at its own end without a load-time cycle.
from . import release_state
