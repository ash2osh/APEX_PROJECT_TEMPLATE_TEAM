"""Feature-capsule rollout execution (schema-version 2), moved out of scripts/rollout.py.

scripts/rollout.py keeps the CLI, the legacy schema-version 1 path and the helpers both paths share.
Shared names are read as rollout.<name> at call time, so patches on scripts.rollout still reach this code.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from collections.abc import Callable, Mapping, Sequence

from scripts import migration_attempts, terminal_confirm
from scripts.db_targets import Target, TargetResolutionError, resolve_target
from scripts.deployment_descriptor import read_descriptor
from scripts.path_utils import relative_to_root


def migration_folder_rows(folders: Sequence[str], migrations: Sequence) -> list[dict]:
    """Payload and check digests of a migrate step's folders, in step order."""
    return [
        {"path": folder, "payloadDigest": migration.payload_digest, "checksSha256": migration.checks_sha256}
        for folder, migration in zip(folders, migrations, strict=True)
    ]


def _migration_attempt_events(folders: Sequence[str], repo_root: Path) -> dict[str, dict]:
    """Every attempt-event sidecar in these migration folders, keyed by eventId."""
    events: dict[str, dict] = {}
    for folder in folders:
        _relative, path = rollout._repo_file(folder, repo_root, "migration folder", kind="directory")
        for event in migration_attempts.read_attempt_events(path):
            events[event["eventId"]] = event
    return events


def _migration_write_state(baseline: set[str] | None, folders: Sequence[str], repo_root: Path) -> str:
    """What a migrate step wrote, read from the attempt events that appeared while it ran.

    ``no-write-attempted``: no new attempt, or every new attempt ended apply-not-started with
    retained no-write evidence. ``partially-verified``: at least one new attempt verified and
    none is unknown. Anything else, including any read or validation error, is ``unknown``.
    """
    if baseline is None:
        return "unknown"
    try:
        events = _migration_attempt_events(folders, repo_root)
        touched = {event["attemptId"] for event_id, event in events.items() if event_id not in baseline}
        outcomes = []
        for attempt_id in sorted(touched):
            chain = [event for event in events.values() if event["attemptId"] == attempt_id]
            migration_attempts.reduce_attempt_events(chain)
            terminal = [event for event in chain if event["state"] != "pending"]
            if len(terminal) != 1:
                outcomes.append("unknown")
            elif terminal[0]["state"] == "verified":
                outcomes.append("verified")
            elif terminal[0]["state"] == "apply-not-started" and migration_attempts.validate_no_write_evidence(repo_root, terminal[0]):
                outcomes.append("no-write")
            else:
                outcomes.append("unknown")
    except (migration_attempts.MigrationAttemptError, rollout.RolloutError, OSError, ValueError, LookupError, TypeError):
        # Any event that cannot be read or validated leaves the write unclassified: unknown.
        return "unknown"
    if "unknown" in outcomes:
        return "unknown"
    if "verified" in outcomes:
        return "partially-verified"
    return "no-write-attempted"


def _database_state_after_migration(write_state: str, before: str) -> str:
    """The database state a failed migrate step leaves: unknown only when its write cannot be classified."""
    if write_state == "unknown":
        return "unknown"
    if write_state == "partially-verified":
        return "verified"
    return before


def _capsule_plan_targets(target_plan, environment: str) -> list[Target]:
    """Every database the prepared capsule target plan binds, from its frozen records."""
    default_classification = "production" if environment == "prod" else "staging"
    return [
        Target(environment, record["connection"], record["expectedUser"], record["schema"], record.get("classification") or default_classification)
        for record in target_plan.targets.values()
        if isinstance(record, Mapping)
    ]


def _capsule_report_inputs(root: Path, plan_item: dict, capsule, capsule_document: Mapping, checkpoint_index: int) -> list[dict]:
    capsule_root = Path(capsule.root)
    capsule_relative = relative_to_root(capsule_root, root).as_posix()
    rows: list[dict] = []
    checkpoint = capsule_document["checkpoints"][checkpoint_index]
    for item in checkpoint["inputs"]:
        rows.append({"path": item["path"], "sha256": item["sha256"]})
    for item in checkpoint["checks"]:
        rows.append({"path": item["path"], "sha256": item["sha256"]})
    source_rows = [
        row for row in capsule_document["sourceInputs"]
        if isinstance(row, Mapping) and row.get("step") == plan_item["number"]
    ]
    for row in source_rows:
        if row.get("driverPath") is None:
            continue
        rows.append({
            "path": f"{capsule_relative}/{row['driverPath']}",
            "sha256": row["driverSha256"],
        })
    rows.extend(
        {"path": f"{capsule_relative}/{row['path']}", "sha256": row["sha256"]}
        for row in capsule.input_hashes
        if row["path"].startswith("drivers/checkpoint-")
        and row["path"].startswith(f"drivers/checkpoint-{checkpoint_index + 1:03d}-")
    )
    # Preserve the stable order while rejecting duplicate path bindings.
    result: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if row["path"] not in seen:
            seen.add(row["path"])
            result.append(row)
    return result


def _capsule_live_source_check(root: Path, capsule_document: Mapping) -> None:
    """Prove any legacy/shared file still used by an existing executor equals its frozen bytes."""
    for row in capsule_document["sourceInputs"]:
        if not isinstance(row, Mapping) or not isinstance(row.get("sourcePath"), str):
            continue
        relative = row["sourcePath"].removeprefix("payload/")
        source = root / relative
        rollout._assert_no_symlink(source, root)
        try:
            raw = source.read_bytes()
        except OSError as error:
            raise rollout.RolloutError(f"frozen rollout source is no longer available: {relative}") from error
        if hashlib.sha256(raw).hexdigest() != row.get("sourceSha256"):
            raise rollout.RolloutError(f"working source differs from the staged capsule: {relative}")


def _validate_capsule_app_targets(root: Path, target_plan, steps: Sequence[dict], environment: str) -> None:
    for step in steps:
        if step["type"] != "app-deploy":
            continue
        app_id = step["appId"]
        target = target_plan.targets.get(f"app:{app_id}")
        if not isinstance(target, Mapping):
            raise rollout.RolloutError(f"prepared target plan has no application descriptor for {app_id}")
        descriptor_path = root / target.get("descriptorPath", "")
        rollout._assert_no_symlink(descriptor_path, root)
        try:
            raw = descriptor_path.read_bytes()
            descriptor = read_descriptor(descriptor_path, app_id)
        except (OSError, ValueError) as error:
            raise rollout.RolloutError(f"application {app_id} descriptor cannot be revalidated: {error}") from error
        if hashlib.sha256(raw).hexdigest() != target.get("descriptorSha256"):
            raise rollout.RolloutError(f"application {app_id} deployment descriptor changed after target preparation")
        if (
            descriptor["workspace"]["name"] != target.get("workspace")
            or descriptor["app"]["databaseSession"]["parsingSchema"] != target.get("schema")
        ):
            raise rollout.RolloutError(f"application {app_id} descriptor target differs from the prepared target plan")
        try:
            resolved = resolve_target(os.environ, environment, "read", schema=target["schema"])
        except (KeyError, TargetResolutionError) as error:
            raise rollout.RolloutError(f"application {app_id} target profile can no longer be resolved: {error}") from error
        if resolved.connection != target.get("connection") or resolved.expected_user != target.get("expectedUser"):
            raise rollout.RolloutError(f"application {app_id} target profile changed after preparation")


def _validate_capsule_auxiliary_targets(target_plan, steps: Sequence[dict], environment: str, repo_root: Path) -> None:
    for number, step in enumerate(steps, start=1):
        if step.get("type") not in {"sql-script", "ords-import", "migrate"}:
            continue
        prepared = target_plan.targets.get(f"step:{number}")
        if not isinstance(prepared, Mapping):
            raise rollout.RolloutError(f"step {number} has no prepared SQL, ORDS or migration target identity")
        current = rollout._target_binding(step, repo_root, environment, capsule=True)
        for field in (
            "environment", "operation", "logicalSchema", "connection", "expectedUser", "schema", "classification",
        ):
            if current.get(field) != prepared.get(field):
                raise rollout.RolloutError(f"step {number} migration profile changed after target preparation")
        if step["type"] == "migrate":
            _logical, _target, migrations = rollout._migration_step_target(repo_root, step["folders"], environment, os.environ)
            if prepared.get("folders") != migration_folder_rows(step["folders"], migrations):
                raise rollout.RolloutError(f"step {number} migration folders changed after target preparation")
        observed = prepared.get("observed")
        if not isinstance(observed, Mapping) or any(
            not isinstance(observed.get(field), str) or not observed[field]
            for field in rollout.OBSERVED_IDENTITY_FIELDS
        ):
            raise rollout.RolloutError(f"step {number} prepared target identity is incomplete")


def _capsule_object_budget(
    capsule,
    elapsed: float,
    included: Sequence[int],
    excluded: Sequence[str],
    included_migrations: Sequence[int] = (),
    migration_steps: int = 0,
) -> dict:
    return {
        "seconds": capsule.database_object_budget_seconds,
        "elapsedSeconds": round(elapsed, 3),
        "coverageComplete": (
            len(included) == len(capsule.checkpoints) and not excluded and len(included_migrations) == migration_steps
        ),
        "withinBudget": elapsed <= capsule.database_object_budget_seconds,
        "includedCheckpoints": list(included),
        "includedMigrationSteps": list(included_migrations),
        "excludedObjectSteps": list(excluded),
    }


def _selected_capsule_signature(capsule) -> tuple:
    """The capsule whose hashes were prepared and shown for confirmation."""
    return (
        capsule.manifest_sha256,
        capsule.payload_digest,
        capsule.engine_digest,
        capsule.source_commit,
        tuple((row.get("path"), row.get("sha256")) for row in capsule.input_hashes),
    )


def _assert_recovery_will_not_replay_prior_writes(plan: Sequence[dict], target_plan, parent_run: Mapping) -> None:
    """Refuse recovery when restarting the frozen order would repeat a completed write step."""
    binding = getattr(target_plan, "recovery_binding", None)
    checkpoint = binding.get("checkpoint") if isinstance(binding, Mapping) else None
    if type(checkpoint) is not int or checkpoint < 1:
        raise rollout.RolloutError("recovery plan has no valid checkpoint binding")
    checkpoint_step_number = None
    feature_checkpoint = 0
    for item in plan:
        if item["type"] == "feature-migrate":
            feature_checkpoint += 1
            if feature_checkpoint == checkpoint:
                checkpoint_step_number = item["number"]
                break
    if checkpoint_step_number is None:
        raise rollout.RolloutError("recovery checkpoint is outside the frozen rollout step list")

    # A parent killed mid-step records no outcome for that step or any later one, so only
    # the earlier writing steps must have an outcome. Those are the only ones that could
    # have completed and be replayed.
    parent_steps = parent_run.get("steps")
    if not isinstance(parent_steps, list) or len(parent_steps) > len(plan):
        raise rollout.RolloutError("recovery parent has more step outcomes than the frozen plan")
    write_types = {"migrate", "sql-script", "ords-import", "app-deploy", "feature-migrate"}
    completed_writes = []
    for item in plan[:checkpoint_step_number - 1]:
        if item["type"] not in write_types:
            continue
        if item["number"] > len(parent_steps):
            raise rollout.RolloutError(f"recovery parent has no recorded outcome for earlier writing step {item['number']}")
        outcome = parent_steps[item["number"] - 1]
        if not isinstance(outcome, Mapping) or outcome.get("step") != item["number"]:
            raise rollout.RolloutError(f"recovery parent step {item['number']} outcome is malformed")
        if outcome.get("status") != "verified":
            raise rollout.RolloutError(
                f"recovery cannot safely continue because earlier writing step {item['number']} "
                "does not have a verified receipt"
            )
        completed_writes.append(item["number"])
    if completed_writes:
        rendered = ", ".join(str(number) for number in completed_writes)
        raise rollout.RolloutError(
            "recovery refused before confirmation because restarting this capsule would replay "
            f"previously verified writing step(s): {rendered}. Preserve the receipts and use an "
            "operator-reviewed recovery path that omits those completed writes."
        )


def _run_capsule_rollout(
    manifest_path: Path | str,
    environment: str,
    *,
    from_step: int,
    dry_run: bool,
    report_path: Path | str | None,
    repo_root: Path,
    confirm: Callable[[str], bool],
    prepare_only: bool,
    capsule_path: Path | str,
    stage_report_path: Path | str | None,
    recovery_attempt: str | None,
    recovery_confirm: Callable[[str], str] | None,
    yes: bool = False,
    confirm_target: str | None = None,
) -> int:
    from scripts import feature_release, release_capsule, release_ownership, release_state

    started = time.perf_counter()
    root = Path(repo_root).resolve(strict=True)
    report = None
    report_paths = None
    run_id = None

    def finish_local_run(*, verified: bool = False) -> None:
        if run_id is None or report is None:
            return
        index = release_state._load_index(root)
        run = next((item for item in index["rollouts"] if item["runId"] == run_id), None)
        if run is None or run["state"] != "running":
            return
        step_outcomes = []
        proof_steps = []
        for plan_item, step_report in zip(plan, report.get("steps", []), strict=True):
            number = plan_item["number"]
            if step_report.get("status") == "succeeded" and isinstance(step_report.get("receiptPath"), str):
                try:
                    receipt, receipt_path = rollout._verify_receipt(root, manifest, environment, plan_item)
                    raw = receipt_path.read_bytes()
                    relative_receipt = relative_to_root(receipt_path, root).as_posix()
                    receipt_hash = hashlib.sha256(raw).hexdigest()
                    step_outcomes.append({
                        "step": number, "status": "verified", "receiptPath": relative_receipt,
                        "receiptSha256": receipt_hash,
                    })
                    proof_steps.append({
                        "step": number, "status": "verified", "receiptPath": relative_receipt,
                        "receiptSha256": receipt_hash, "receiptJson": raw.decode("utf-8"),
                    })
                except (OSError, UnicodeError, rollout.RolloutError) as error:
                    if verified:
                        raise rollout.RolloutError(f"step {number} receipt cannot prove completion: {error}") from error
                    step_outcomes.append({"step": number, "status": "unknown", "receiptPath": None, "receiptSha256": None})
            else:
                status = step_report.get("status")
                normalized = "failed" if status == "failed" else "unknown" if status == "running" else "not-started"
                step_outcomes.append({"step": number, "status": normalized, "receiptPath": None, "receiptSha256": None})

        object_budget = report.get("objectBudget", {})
        seconds = object_budget.get("seconds", capsule.database_object_budget_seconds)
        elapsed = object_budget.get("elapsedSeconds")
        coverage = object_budget.get("coverageComplete", False)
        within = object_budget.get("withinBudget") is True and isinstance(elapsed, (int, float)) and elapsed <= seconds
        budget = {
            "seconds": seconds, "elapsedSeconds": elapsed,
            "coverageComplete": coverage is True, "passed": bool(coverage is True and within),
        }
        if verified:
            if report.get("status") != "success" or any(item["status"] != "verified" for item in step_outcomes):
                raise rollout.RolloutError("whole-rollout completion requires a successful receipt for every step")
            verification_rows = sorted(
                (row for row in index["checkpointVerifications"] if row["runId"] == run_id),
                key=lambda row: row["checkpoint"],
            )
            if [row["checkpoint"] for row in verification_rows] != list(range(1, len(capsule.checkpoints) + 1)):
                raise rollout.RolloutError("whole-rollout completion is missing a checkpoint verification")
            verification_ids = [row["verificationId"] for row in verification_rows]
            if verification_ids != run["checkpointVerificationIds"]:
                raise rollout.RolloutError("whole-rollout checkpoint verification index changed during execution")
            for row in verification_rows:
                release_state._verify_evidence_hashes(root, row["evidence"], "checkpoint verification")
            if not budget["passed"] or budget["seconds"] != capsule.database_object_budget_seconds:
                raise rollout.RolloutError("whole-rollout completion requires complete in-budget object coverage")
            if report_paths is None:
                raise rollout.RolloutError("whole-rollout completion has no final report destination")
            report["reportFiles"] = {
                "json": relative_to_root(report_paths[0], root).as_posix(),
                "markdown": relative_to_root(report_paths[1], root).as_posix(),
            }
            report["localRunId"] = run_id
            rollout._write_reports(report, report_paths)
            final_report_path = relative_to_root(report_paths[0], root).as_posix()
            final_report_sha = rollout._canonical_hash(report)
            recovery_links = [
                row for row in index["recoveryLinks"]
                if row["verificationId"] in verification_ids
            ]
            report["completionProof"] = {
                "schemaVersion": 1,
                "runId": run_id,
                "environment": environment,
                "sourceCommit": capsule.source_commit,
                "payloadDigest": capsule.payload_digest,
                "engineDigest": capsule.engine_digest,
                "planDigest": target_plan.plan_digest,
                "targetScopes": list(run["targetScopes"]),
                "state": "verified",
                "databaseState": "verified",
                "steps": proof_steps,
                "checkpointVerificationIds": verification_ids,
                "checkpointVerifications": verification_rows,
                "recoveryLinks": recovery_links,
                "budget": budget,
                "finalReportPath": final_report_path,
                "finalReportSha256": final_report_sha,
            }
            rollout._write_reports(report, report_paths)
            release_state.finish_rollout(
                root, run_id,
                outcome={
                    "state": "verified", "databaseState": "verified",
                    "steps": step_outcomes,
                    "checkpointVerificationIds": verification_ids,
                    "budget": budget,
                    "finalReportPath": final_report_path,
                    "finalReportSha256": final_report_sha,
                },
            )
            return

        database_state = report.get("databaseState", "not-started")
        if database_state not in {"not-started", "verified", "unknown", "failed"}:
            database_state = "unknown"
        run_state = "unknown" if (
            report.get("exitCode") == 130
            or database_state == "unknown"
            or any(item["status"] == "unknown" for item in step_outcomes)
        ) else "failed"
        release_state.finish_rollout(
            root, run_id,
            outcome={
                "state": run_state,
                "databaseState": database_state,
                "steps": step_outcomes,
                "checkpointVerificationIds": list(run["checkpointVerificationIds"]),
                "budget": budget,
                "finalReportPath": None,
                "finalReportSha256": None,
            },
        )

    try:
        if environment not in {"staging", "prod"}:
            raise rollout.RolloutError("feature capsule rollout supports only staging or prod")
        if from_step != 1:
            raise rollout.RolloutError("feature capsule rollout starts at step 1; use its checkpoint attempt state for recovery")
        prep_started = time.perf_counter()
        capsule = release_capsule.load_capsule(root, capsule_path)
        if recovery_attempt is not None and not capsule.checkpoints:
            raise rollout.RolloutError(
                "--recover is refused: this capsule has no feature checkpoint, so it has no checkpoint attempt to recover. "
                "Reconcile the interrupted migration as docs/migration-rules.md describes, then run a new capsule, or this "
                "one after that reconciliation"
            )
        selected_capsule_root = capsule.root
        selected_capsule_signature = _selected_capsule_signature(capsule)

        def reload_selected_capsule():
            current = release_capsule.load_capsule(root, selected_capsule_root)
            if _selected_capsule_signature(current) != selected_capsule_signature:
                raise rollout.RolloutError("selected release capsule changed after target preparation")
            return current

        capsule_document, _capsule_raw = release_capsule._read_capsule_json(capsule.root / "capsule.json")
        manifest = rollout.load_manifest(manifest_path, root, version=2)
        payload_definition = capsule_document["payloadDefinition"]
        frozen_manifest = payload_definition["rolloutManifest"]
        manifest_relative = relative_to_root(manifest.path, root).as_posix()
        if manifest_relative != frozen_manifest["path"] or manifest.sha256 != frozen_manifest["sha256"]:
            raise rollout.RolloutError("rollout manifest path or bytes do not match the selected capsule")
        if capsule.database_object_budget_seconds is None:
            raise rollout.RolloutError("feature capsule has no bounded database-object budget")
        plan = rollout._prepare_plan(manifest, environment)
        if len(capsule.checkpoints) != sum(item["step"]["type"] == "feature-migrate" for item in plan):
            raise rollout.RolloutError("capsule feature checkpoints do not match the rollout manifest")

        checkpoint_index = 0
        for item in plan:
            if item["step"]["type"] != "feature-migrate":
                continue
            item["inputHashes"] = _capsule_report_inputs(root, item, capsule, capsule_document, checkpoint_index)
            item["sha256"] = rollout._canonical_hash({"step": item["step"], "inputHashes": item["inputHashes"]})
            checkpoint_index += 1

        _capsule_live_source_check(root, capsule_document)

        if dry_run:
            preparation_seconds = round(time.perf_counter() - prep_started, 3)
            report_paths = rollout._report_paths(root, report_path)
            rollout._report_outputs_do_not_overlap(report_paths, plan, root, manifest.path, environment)
            report = {
                "schemaVersion": 2,
                "repoRoot": str(root),
                "manifestPath": manifest_relative,
                "manifestSha256": manifest.sha256,
                "environment": environment,
                "status": "planned",
                "exitCode": 0,
                "startedAt": rollout._utc_now(),
                "endedAt": None,
                "durationSeconds": None,
                "payloadDigest": capsule.payload_digest,
                "engineDigest": capsule.engine_digest,
                "sourceCommit": capsule.source_commit,
                "targetPlanDigest": None,
                "inputHashes": list(capsule.input_hashes),
                "preparationSeconds": preparation_seconds,
                "databaseState": "not-started",
                "objectPhaseSeconds": 0.0,
                "objectBudget": {
                    "seconds": capsule.database_object_budget_seconds,
                    "elapsedSeconds": 0.0,
                    "coverageComplete": False,
                    "withinBudget": True,
                    "includedCheckpoints": [],
                    "includedMigrationSteps": [],
                    "excludedObjectSteps": [],
                },
                "applicationSeconds": 0.0,
                "ordsSeconds": 0.0,
                "dataSeconds": 0.0,
                "checkpointSqlclSessions": {},
                "steps": [dict(rollout._report_step(item), status="planned") for item in plan],
                "reportFiles": {},
                "confirmationPath": None,
                "error": None,
            }
            rollout._finish_report(report, started, "planned", 0)
            rollout._write_reports(report, report_paths)
            rollout._announce_report_paths(report_paths, root)
            return 0

        if prepare_only:
            terminal_ack = None
            if recovery_attempt is not None and recovery_confirm is not None:
                try:
                    prompt = release_state.recovery_prompt(root, capsule, environment, recovery_attempt)
                    terminal_ack = recovery_confirm(prompt)
                except release_state.ReleaseStateError as error:
                    raise rollout.RolloutError(f"recovery acknowledgment refused: {error}") from error
            target_plan = feature_release.prepare_target(
                root, capsule, environment, environ=os.environ, recovery_attempt=recovery_attempt,
                terminal_ack=terminal_ack,
            )
        else:
            target_plan = feature_release.load_target_plan(root, capsule, environment)
            if recovery_attempt is not None and recovery_attempt != target_plan.recovery_attempt:
                raise rollout.RolloutError("requested recovery attempt differs from the prepared target plan")
        if target_plan.recovery_attempt is not None:
            try:
                parent_run = release_state.recovery_parent_rollout(root, capsule, target_plan)
            except release_state.ReleaseStateError as error:
                raise rollout.RolloutError(f"recovery parent validation refused: {error}") from error
            _assert_recovery_will_not_replay_prior_writes(plan, target_plan, parent_run)

        capsule = reload_selected_capsule()

        preparation_seconds = round(time.perf_counter() - prep_started, 3)
        report_paths = rollout._report_paths(root, report_path)
        rollout._report_outputs_do_not_overlap(report_paths, plan, root, manifest.path, environment)
        if prepare_only:
            report = {
                "schemaVersion": 2,
                "repoRoot": str(root),
                "manifestPath": manifest_relative,
                "manifestSha256": manifest.sha256,
                "environment": environment,
                "status": "prepared",
                "exitCode": 0,
                "startedAt": rollout._utc_now(),
                "endedAt": None,
                "durationSeconds": None,
                "payloadDigest": capsule.payload_digest,
                "engineDigest": capsule.engine_digest,
                "sourceCommit": capsule.source_commit,
                "targetPlanDigest": target_plan.plan_digest,
                "inputHashes": list(capsule.input_hashes),
                "preparationSeconds": preparation_seconds,
                "databaseState": "not-started",
                "objectBudget": {
                    "seconds": capsule.database_object_budget_seconds,
                    "elapsedSeconds": 0.0,
                    "coverageComplete": False,
                    "withinBudget": False,
                    "includedCheckpoints": [],
                    "includedMigrationSteps": [],
                    "excludedObjectSteps": [],
                },
                "objectPhaseSeconds": 0.0,
                "applicationSeconds": 0.0,
                "ordsSeconds": 0.0,
                "dataSeconds": 0.0,
                "checkpointSqlclSessions": {},
                "steps": [rollout._report_step(item) for item in plan],
                "reportFiles": {},
                "confirmationPath": None,
                "error": None,
            }
            rollout._finish_report(report, started, "prepared", 0)
            rollout._write_reports(report, report_paths)
            rollout._announce_report_paths(report_paths, root)
            return 0

        if environment == "prod":
            if stage_report_path is None:
                raise rollout.RolloutError("production capsule rollout requires --stage-report with a verified staging report")
            stage_path = Path(stage_report_path)
            if not stage_path.is_absolute():
                stage_path = root / stage_path
            rollout._assert_no_symlink(stage_path, root)
            try:
                stage_report = json.loads(stage_path.read_text(encoding="utf-8"), object_pairs_hook=rollout._object_no_duplicates)
            except (OSError, UnicodeError, json.JSONDecodeError, rollout.RolloutError) as error:
                raise rollout.RolloutError(f"staging report is missing or invalid: {error}") from error
            try:
                release_state.validate_stage_proof(capsule, stage_report)
            except release_state.ReleaseStateError as error:
                raise rollout.RolloutError(f"production promotion refused: {error}") from error

        _capsule_live_source_check(root, capsule_document)
        _validate_capsule_app_targets(root, target_plan, [item["step"] for item in plan], environment)
        _validate_capsule_auxiliary_targets(target_plan, [item["step"] for item in plan], environment, root)
        report = {
            "schemaVersion": 2,
            "repoRoot": str(root),
            "manifestPath": manifest_relative,
            "manifestSha256": manifest.sha256,
            "environment": environment,
            "status": "running",
            "exitCode": None,
            "startedAt": rollout._utc_now(),
            "endedAt": None,
            "durationSeconds": None,
            "payloadDigest": capsule.payload_digest,
            "engineDigest": capsule.engine_digest,
            "sourceCommit": capsule.source_commit,
            "targetPlanDigest": target_plan.plan_digest,
            "inputHashes": list(capsule.input_hashes),
            "preparationSeconds": preparation_seconds,
            "databaseState": "not-started",
            "objectPhaseSeconds": 0.0,
            "objectBudget": {
                "seconds": capsule.database_object_budget_seconds,
                "elapsedSeconds": 0.0,
                "coverageComplete": False,
                "withinBudget": True,
                "includedCheckpoints": [],
                "includedMigrationSteps": [],
                "excludedObjectSteps": [],
            },
            "applicationSeconds": 0.0,
            "ordsSeconds": 0.0,
            "dataSeconds": 0.0,
            "checkpointSqlclSessions": {},
            "steps": [rollout._report_step(item) for item in plan],
            "reportFiles": {},
            "confirmationPath": None,
            "confirmedBy": None,
            "error": None,
        }
        if dry_run:
            report["steps"] = [dict(step, status="planned") for step in report["steps"]]
            rollout._finish_report(report, started, "planned", 0)
            rollout._write_reports(report, report_paths)
            rollout._announce_report_paths(report_paths, root)
            return 0

        capsule_digests = (
            f"\nCapsule payload SHA-256: {capsule.payload_digest}"
            + f"\nWorkflow engine SHA-256: {capsule.engine_digest}"
            + f"\nTarget plan SHA-256: {target_plan.plan_digest}"
        )
        if confirm_target is not None:
            rollout._assert_confirm_target(root, _capsule_plan_targets(target_plan, environment), confirm_target, environment)
        if yes or confirm_target is not None:
            print(rollout._confirmation_text(manifest, plan, environment, 1, question=False) + capsule_digests)
            print(terminal_confirm.confirmed_line(yes, confirm_target))
            approved = True
        else:
            try:
                approved = bool(confirm(rollout._confirmation_text(manifest, plan, environment, 1) + capsule_digests))
            except (EOFError, OSError):
                approved = False
        if not approved:
            rollout._finish_report(report, started, "declined", 1)
            rollout._write_reports(report, report_paths)
            rollout._announce_report_paths(report_paths, root)
            return 1
        report["confirmedBy"] = terminal_confirm.confirmed_by(yes, confirm_target)

        capsule = reload_selected_capsule()
        _validate_capsule_app_targets(root, target_plan, [item["step"] for item in plan], environment)
        _validate_capsule_auxiliary_targets(target_plan, [item["step"] for item in plan], environment, root)
        scratch = rollout._safe_scratch_root(root)
        evidence_root = scratch / "rollout-runs"
        rollout._safe_directory(evidence_root, root)
        run_evidence = evidence_root / rollout._execution_id()
        rollout._assert_no_symlink(run_evidence, root)
        run_evidence.mkdir(mode=0o700, exist_ok=False)
        report["evidenceDir"] = relative_to_root(run_evidence, root).as_posix()
        confirmation_path = run_evidence / "rollout-confirmation.json"
        rollout._write_atomic(confirmation_path, json.dumps({
            "schemaVersion": 2,
            "status": "confirmed",
            "environment": environment,
            "payloadDigest": capsule.payload_digest,
            "engineDigest": capsule.engine_digest,
            "targetPlanDigest": target_plan.plan_digest,
            "confirmedAt": rollout._utc_now(),
            "confirmedBy": report["confirmedBy"],
            "inputHashes": list(capsule.input_hashes),
        }, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        report["confirmationPath"] = relative_to_root(confirmation_path, root).as_posix()
        rollout._write_reports(report, report_paths)

        run_id = release_state.start_rollout(root, capsule, target_plan)
        report["localRunId"] = run_id
        # Rollout receipts are never replaced. Refuse before any write when a step of this manifest already has
        # one for this environment, as a schema-version 1 rollout does. This comes after start_rollout so that
        # an unresolved earlier run is reported first; the run then ends failed with nothing started.
        rollout._assert_unreceipted_execution_range(root, manifest, environment, plan, 1)

        included_checkpoints: list[int] = []
        included_migration_steps: list[int] = []
        excluded_object_steps: list[str] = []
        migration_step_count = sum(item["step"]["type"] == "migrate" for item in plan)
        object_seconds = 0.0
        checkpoint_number = 0
        for index, item in enumerate(plan):
            step = item["step"]
            step_report = report["steps"][index]
            if step["type"] in {"feature-migrate", "migrate"} and object_seconds >= capsule.database_object_budget_seconds:
                report["objectPhaseSeconds"] = round(object_seconds, 3)
                report["objectBudget"] = _capsule_object_budget(
                    capsule, object_seconds, included_checkpoints, excluded_object_steps,
                    included_migration_steps, migration_step_count,
                )
                report["error"] = "database-object budget reached; stopped before the next feature or migration checkpoint"
                report["databaseState"] = "verified" if (included_checkpoints or included_migration_steps) else "not-started"
                rollout._finish_report(report, started, "failed", 2)
                break

            capsule = reload_selected_capsule()
            step_started = time.perf_counter()
            step_report["status"] = "running"
            step_report["startedAt"] = rollout._utc_now()
            evidence_dir = run_evidence / f"step-{item['number']:03d}"
            evidence_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
            auxiliary_write_invoked = False
            migration_baseline: set[str] | None = None
            database_state_before = report["databaseState"]
            result = None
            try:
                if step["type"] == "feature-migrate":
                    checkpoint_number += 1
                    checkpoint_result = feature_release.execute_checkpoint(
                        root, capsule, target_plan, checkpoint_number, run_id=run_id, environ=os.environ,
                    )
                    result = rollout.StepResult(
                        checkpoint_result.exit_code,
                        checkpoint_result.evidence_paths,
                        checkpoint_result.message,
                    )
                    step_report["objectSeconds"] = checkpoint_result.object_seconds
                    step_report["sqlclSessions"] = checkpoint_result.session_count
                    step_report["checkpointVerificationId"] = getattr(checkpoint_result, "verification_id", None)
                    report["checkpointSqlclSessions"][str(checkpoint_number)] = checkpoint_result.session_count
                    object_seconds += checkpoint_result.object_seconds
                    included_checkpoints.append(checkpoint_number)
                    report["databaseState"] = checkpoint_result.database_state
                else:
                    rollout._snapshot(manifest, plan, f"before step {item['number']}", environment)
                    expected_identity = None
                    if step["type"] in {"sql-script", "ords-import", "migrate"}:
                        binding = rollout._target_binding(step, root, environment, capsule=True)
                        prepared_binding = target_plan.targets.get(f"step:{item['number']}")
                        if not isinstance(prepared_binding, Mapping):
                            raise rollout.RolloutError(f"step {item['number']} has no prepared target identity")
                        if any(binding.get(field) != prepared_binding.get(field) for field in (
                            "environment", "operation", "logicalSchema", "connection", "expectedUser", "schema", "classification",
                        )):
                            raise rollout.RolloutError(f"step {item['number']} target profile no longer matches its prepared binding")
                        target = Target(
                            environment, binding["connection"], binding["expectedUser"],
                            binding["schema"], binding["classification"],
                        )
                        expected_identity = dict(prepared_binding["observed"])
                        scope = release_state.target_scope_from_identity(expected_identity)
                        lease_identity = {
                            "purpose": "rollout-auxiliary-step",
                            "runId": run_id,
                            "step": item["number"],
                            "planDigest": target_plan.plan_digest,
                        }
                        # migrate.main resolves its own target; the binding comparison above proves it is the prepared one.
                        bound_step = step if step["type"] == "migrate" else dict(
                            step,
                            connection=binding["connection"],
                            expectedUser=binding["expectedUser"],
                            schema=binding["schema"],
                        )
                        with release_ownership.target_writer_lease(root, scope, lease_identity) as lease:
                            release_state.assert_auxiliary_step_allowed(
                                root, capsule, target_plan, item["number"], run_id=run_id, lease=lease,
                            )
                            observed = rollout._observe_target_identity(target, evidence_dir / "target-identity")
                            if observed != expected_identity:
                                raise rollout.RolloutError(f"step {item['number']} target identity changed after preparation")
                            if step["type"] == "migrate":
                                _logical, _target, migrations = rollout._migration_step_target(root, step["folders"], environment, os.environ)
                                if prepared_binding.get("folders") != migration_folder_rows(step["folders"], migrations):
                                    raise rollout.RolloutError(f"step {item['number']} migration folders changed after target preparation")
                                migration_baseline = set(_migration_attempt_events(step["folders"], root))
                            auxiliary_write_invoked = True
                            result = rollout.execute_step(
                                bound_step,
                                environment=environment,
                                repo_root=root,
                                evidence_dir=evidence_dir,
                                step_number=item["number"],
                                manifest=manifest,
                                plan=plan,
                                manifest_confirmed=True,
                                expected_identity=expected_identity,
                                confirm_target=confirm_target,
                            )
                    else:
                        result = rollout.execute_step(
                            step,
                            environment=environment,
                            repo_root=root,
                            evidence_dir=evidence_dir,
                            step_number=item["number"],
                            manifest=manifest,
                            plan=plan,
                            manifest_confirmed=True,
                            expected_identity=expected_identity,
                            confirm_target=confirm_target,
                        )
                    rollout._snapshot(manifest, plan, f"after step {item['number']}", environment)
                    if step["type"] in {"sql-script", "ords-import"} and step_report.get("phase") == "uncovered-object":
                        excluded_object_steps.append(f"step-{item['number']:03d}:{step['type']}")
                    elapsed = time.perf_counter() - step_started
                    if step["type"] == "migrate" and result.exit_code == 0:
                        object_seconds += elapsed
                        included_migration_steps.append(item["number"])
                        step_report["objectSeconds"] = round(elapsed, 3)
                        step_report["phase"] = "migration"
                    if step["type"] == "app-deploy":
                        report["applicationSeconds"] += elapsed
                    elif step["type"] == "ords-import":
                        report["ordsSeconds"] += elapsed
                        step_report["phaseSeconds"] = round(elapsed, 3)
                    elif step["type"] == "sql-script":
                        report["dataSeconds"] += elapsed
                        step_report["phaseSeconds"] = round(elapsed, 3)
                    if step["type"] in {"migrate", "sql-script", "ords-import"} and result.exit_code == 0:
                        report["databaseState"] = "verified"

                step_report["evidencePaths"] = list(result.evidence_paths)
                step_report["message"] = result.message
                step_report["endedAt"] = rollout._utc_now()
                step_report["durationSeconds"] = round(time.perf_counter() - step_started, 3)
                report["objectPhaseSeconds"] = round(object_seconds, 3)
                report["objectBudget"] = _capsule_object_budget(
                    capsule, object_seconds, included_checkpoints, excluded_object_steps,
                    included_migration_steps, migration_step_count,
                )
                if result.exit_code != 0:
                    step_report["status"] = "failed"
                    step_report["error"] = result.message or f"step returned status {result.exit_code}"
                    report["error"] = step_report["error"]
                    if step["type"] == "migrate":
                        write_state = _migration_write_state(migration_baseline, step["folders"], root)
                        step_report["writeState"] = write_state
                        report["databaseState"] = _database_state_after_migration(write_state, database_state_before)
                    elif step["type"] in {"sql-script", "ords-import"}:
                        report["databaseState"] = "unknown"
                    rollout._finish_report(report, started, "failed", 2)
                    break
                capsule = reload_selected_capsule()
                step_report["status"] = "succeeded"
                receipt_path = rollout._write_receipt(root, environment, manifest, item, step_report)
                step_report["receiptPath"] = relative_to_root(receipt_path, root).as_posix()
                if step["type"] in {"feature-migrate", "migrate"} and object_seconds > capsule.database_object_budget_seconds:
                    report["error"] = "database-object budget exceeded after a verified feature or migration checkpoint"
                    report["databaseState"] = "verified"
                    rollout._finish_report(report, started, "failed", 2)
                    break
            except Exception as error:
                step_report["status"] = "failed"
                step_report["error"] = str(error)
                step_report["endedAt"] = rollout._utc_now()
                step_report["durationSeconds"] = round(time.perf_counter() - step_started, 3)
                step_report["evidencePaths"] = rollout._relative_paths((evidence_dir,), root)
                report["error"] = str(error)
                report["objectPhaseSeconds"] = round(object_seconds, 3)
                report["objectBudget"] = _capsule_object_budget(
                    capsule, object_seconds, included_checkpoints, excluded_object_steps,
                    included_migration_steps, migration_step_count,
                )
                if step["type"] == "migrate" and auxiliary_write_invoked:
                    if result is not None:
                        # migrate.main had already returned, so its outcome is not classified here.
                        report["databaseState"] = "unknown"
                    else:
                        write_state = _migration_write_state(migration_baseline, step["folders"], root)
                        step_report["writeState"] = write_state
                        report["databaseState"] = _database_state_after_migration(write_state, database_state_before)
                elif step["type"] in {"sql-script", "ords-import"} and auxiliary_write_invoked:
                    report["databaseState"] = "unknown"
                rollout._finish_report(report, started, "failed", 2)
                break

            rollout._write_reports(report, report_paths)

        if report["status"] == "running":
            report["objectPhaseSeconds"] = round(object_seconds, 3)
            report["objectBudget"] = _capsule_object_budget(
                capsule, object_seconds, included_checkpoints, excluded_object_steps,
                included_migration_steps, migration_step_count,
            )
            if object_seconds > capsule.database_object_budget_seconds:
                report["error"] = "database-object budget exceeded after verified work"
                report["databaseState"] = "verified"
                rollout._finish_report(report, started, "failed", 2)
            else:
                rollout._finish_report(report, started, "success", 0)
        rollout._write_reports(report, report_paths)
        finish_local_run(verified=report["status"] == "success")
        rollout._announce_report_paths(report_paths, root)
        return report["exitCode"]
    except KeyboardInterrupt:
        if report is not None and report_paths is not None:
            report["error"] = "feature capsule rollout was interrupted; inspect the last checkpoint evidence before retrying"
            rollout._finish_report(report, started, "failed", 130)
            rollout._write_reports(report, report_paths)
            finish_local_run()
            rollout._announce_report_paths(report_paths, root)
        return 130
    except Exception as error:
        print(f"rollout error: {error}", file=sys.stderr)
        if report is not None and report_paths is not None:
            report["error"] = str(error)
            rollout._finish_report(report, started, "failed", 2)
            rollout._write_reports(report, report_paths)
            finish_local_run()
            rollout._announce_report_paths(report_paths, root)
        return 2


# Imported last: scripts/rollout.py binds the capsule entry point and its re-exported helpers from this
# module at its own end, and the shared names above are only read at call time through the rollout module.
from scripts import rollout
