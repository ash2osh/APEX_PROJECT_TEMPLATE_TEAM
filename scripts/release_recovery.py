"""Operator recovery prompts and reconcile-run sessions, moved out of scripts/release_state.py.

scripts/release_state.py keeps the attempt records, the recovery-plan checks and the rollout gates. Names that
stay there are read as release_state.<name> at call time, so patches on scripts.release_state still reach this
code. release_state re-exports the moved names that tests and other scripts reference.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from . import release_ownership

if TYPE_CHECKING:
    from .release_capsule import ReleaseCapsule


def recovery_attempt_details(repo_root: Path, capsule: ReleaseCapsule, environment: str, attempt_id: str) -> dict:
    """Return an unresolved attempt after binding it to the selected release."""
    root = release_state._root(repo_root)
    release_state._uuid(attempt_id, "recovery attempt")
    index = release_state._load_index(root)
    old = next((row for row in index["attempts"] if row["attemptId"] == attempt_id), None)
    if (
        old is None or old["state"] not in {"unknown", "attempting"}
        or not release_state._is_unresolved(old, index["attempts"], index["recoveryLinks"], index["checkpointVerifications"])
        or old["environment"] != environment or old["payloadDigest"] != capsule.payload_digest
        or old["engineDigest"] != capsule.engine_digest or old["sourceCommit"] != capsule.source_commit
        or old["checkpoint"] > len(capsule.checkpoints)
    ):
        raise release_state.ReleaseStateError("recovery attempt is missing, resolved, or does not match this capsule and environment")
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


def _reconcile_refusal(index: Mapping, run: Mapping | None, environment: str) -> str | None:
    """Why this run cannot be closed by reconciliation now, or None when it can."""
    if run is None:
        return "no rollout run has this run id"
    if run["environment"] != environment:
        return f"the run belongs to {run['environment']}, not {environment}"
    if run.get("reconciliation") is not None:
        return "the run already has a reconciliation record"
    if run["state"] in {"verified", "not-started"} or (run["state"] == "failed" and run["databaseState"] != "unknown"):
        return f"the run is {run['state']} with databaseState {run['databaseState']}, so there is nothing to reconcile"
    for attempt in index["attempts"]:
        if release_state._attempt_matches_rollout(attempt, run) and release_state._is_unresolved(
            attempt, index["attempts"], index["recoveryLinks"], index["checkpointVerifications"],
        ):
            return (
                f"checkpoint attempt {attempt['attemptId']} is unresolved; recover it with "
                f"rollout --prepare --recover {attempt['attemptId']} instead"
            )
    return None


class ReconciliationSession:
    """One operator's closure of an unfinished run, while every target writer lease of the run is held."""

    def __init__(self, root: Path, run_id: str, environment: str, run: Mapping) -> None:
        self.root = root
        self.run_id = run_id
        self.environment = environment
        self.run = dict(run)

    def reconcile(self, acknowledgment: str) -> dict:
        """Close the run if the acknowledgment is exactly RECONCILE-RUN <run id>; otherwise change nothing."""
        if acknowledgment != release_state.RUN_RECONCILE_PREFIX + self.run_id:
            raise release_state.AcknowledgmentError("the acknowledgment does not match RECONCILE-RUN for this run; nothing was changed")
        closed: dict = {}

        def update(index: dict) -> dict:
            row = next((item for item in index["rollouts"] if item["runId"] == self.run_id), None)
            reason = _reconcile_refusal(index, row, self.environment)
            if reason is not None:
                raise release_state.ReleaseStateError(f"run {self.run_id} cannot be reconciled: {reason}")
            prior_state, prior_database = row["state"], row["databaseState"]
            if prior_state == "running":
                row["state"] = "unknown"
                row["finishedAt"] = release_state._utc_now()
            row["reconciliation"] = release_state._validate_reconciliation({
                "acknowledgedAt": release_state._utc_now(), "acknowledgment": acknowledgment,
                "priorState": prior_state, "priorDatabaseState": prior_database,
            }, row)
            closed.update(runId=row["runId"], state=row["state"], databaseState=row["databaseState"],
                          reconciliation=row["reconciliation"])
            return index

        release_state._atomic_update(self.root, update)
        return closed


@contextlib.contextmanager
def run_reconciliation(repo_root: Path, run_id: str, environment: str) -> Iterator[ReconciliationSession]:
    """Hold every target writer lease of an unfinished run, then yield the session that may close it.

    The leases are the proof that no process is still writing for those targets; a held lease refuses.
    """
    root = release_state._root(repo_root)
    release_state._uuid(run_id, "rollout runId")
    index = release_state._load_index(root)
    run = next((row for row in index["rollouts"] if row["runId"] == run_id), None)
    reason = _reconcile_refusal(index, run, environment)
    if reason is not None:
        raise release_state.ReleaseStateError(f"run {run_id} cannot be reconciled: {reason}")
    with contextlib.ExitStack() as stack:
        for scope in run["targetScopes"]:
            try:
                stack.enter_context(
                    release_ownership.target_writer_lease(root, scope, {"purpose": "run-reconciliation", "runId": run_id}),
                )
            except release_ownership.ReleaseOwnershipError as error:
                raise release_state.ReleaseStateError(
                    f"run {run_id} cannot be reconciled: a target writer may still be active: {error}",
                ) from error
        yield ReconciliationSession(root, run_id, environment, run)


# Imported last: this module reads release_state.<name> only when it runs, so release_state can bind the
# moved names from this module at its own end without a load-time cycle.
from . import release_state
