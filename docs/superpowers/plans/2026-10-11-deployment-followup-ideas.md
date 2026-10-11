# Deployment Follow-up Ideas Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans after the user authorizes this plan. Steps use checkbox syntax for tracking.

**Goal:** Improve deployment preflight and durable recovery while keeping source, identity and runtime gates strict.

**Architecture:** Extend existing migration, target, lifecycle and upgrade helpers through small shared contracts. Preserve exact-byte source verification unless a separately qualified version-pair profile is explicitly approved. No shared database ledger or automatic runtime restoration.

**Tech Stack:** Python3.10+, stdlib, Bash/PowerShell, existing SQLcl and Oracle public APIs.

**Spec:** [Validity and design](../specs/2026-10-11-deployment-followup-ideas-design.md).

**Status:** Draft plan only. The current user requested planning these ideas, not their implementation. Native targets, app IDs and credentials are deliberately not invented.

## Global Constraints

- Python>=3.10, standard library only for core commands; Bash/PowerShell parity.
- APEX26.2 and SQLcl>=26.3.0.0 for this branch;26.1 qualification/backport is separate.
- No custom team metadata tables, mutexes, rosters or database migration ledgers.
- Attempt-event durability is limited to scratch deletion in a retained checkout.
  Before worktree deletion, require an explicitly preserved archive or reviewed
  commit; never auto-commit. Persistence in a Git common directory or across
  worktrees is a separately designed and qualified feature.
- No automatic enable/resume/repair, DB restores, commits, exports, upgrades or imports.
- Reads use named configured profiles and identity-checked read-only transactions.
- Unknown source-version pairs retain exact-byte comparison.
- Live/Windows checks remain unknown until actually performed on exact authorized targets.

## Review Focus

- Direct-delivery hazards inside multiline literals versus safely assembled data: classify the emitted SQL without corrupting literal semantics.
- Scratch loss, changed hashes or malformed durable attempts: writes remain refused and source remains frozen.
- Identically named app/schema on another target: identity and scoped visibility must be checked before API operations.
- Custom template files versus upstream obsolescence: report separate categories and preserve project work.
- Triggers, sequences, autonomous transactions and DDL: do not claim rollback or changed-value success beyond observed evidence.

---

### Task 1: SQLcl transport preflight diagnostics

**Files:** Create `scripts/sqlcl_transport.py`, `tests/test_sqlcl_transport.py`; modify `scripts/migration_checks.py`, `scripts/migrate.py`, `scripts/rollout.py`, `scripts/baseline.py` where they prepare executable payload; document `docs/migration-rules.md`, `docs/known-limitations.md`, `template-manifest.json`.

**Interfaces:** `lint_sqlcl_transport(sql: str, delivery_mode: str) -> tuple[TransportFinding, ...]`; `TransportFinding(code: str, line: int, severity: str, message: str)`; modes `direct` and `assembled`. Shared lexical primitives must keep their existing migration-query safety contract.

- [ ] Add RED tests for physical trailing hyphens and whitespace-only lines in direct PL/SQL, multiline q/N literals, comments, CRLF, oversized literal chunks, safe CHR(10) assembly, and classify generated view SQL by native evidence. Current native SQLcl26.3 generated multiline view literal retained its exact bytes; do not ban valid multiline q-quotes based on a general continuation assumption.
- [ ] Run `python3 -m unittest discover -s tests -p 'test_sqlcl_transport.py' -v`; require intended missing-helper failures before implementation.
- [ ] Implement shared diagnostics and invoke before any payload handoff. Proven transport corruption refuses; uncertain source-format concerns are explicit warnings, never a verification bypass.
- [ ] Run transport/baseline/rollout/migration focused modules; assert unsafe payload launches zero SQLcl calls and valid SQL bytes remain unchanged.
- [ ] With an authorized disposable target, reproduce native continuation and blank-line behavior through both direct and assembled drivers; preserve original/stored text and cleanup evidence.
- [ ] Run full offline suite, lint and wrapper checks; prepare review without silently committing/pushing.

### Task 2: Durable per-migration local attempt events

**Files:** Modify `scripts/migration_revision.py`, `scripts/migrate.py`, `scripts/migration_manifest.py`, `scripts/baseline.py`, `scripts/upgrade_template.py` only where preservation requires it; test `tests/test_migration_revision.py`, `tests/test_migrate.py`, `tests/test_baseline.py`, `tests/test_upgrade_template.py`; document migration rules.

**Interfaces:** `write_attempt_event(folder: Path, event: Mapping) -> Path`; `read_attempt_events(folder: Path) -> tuple[Mapping, ...]`. Store one immutable file per unique event ID, such as `attempt-event.<event-id>.json`; every record includes stable `attemptId` and `predecessorEventId` (null only for the initial pending event), with `runId` linking folders when a rollout spans several migrations. Use schemaVersion1 and fields defined by the spec. Merge these events with existing scratch attempts in the single lock-state contract. Do not add a second independent unlocked-state classifier.

- [ ] Add RED regressions: delete scratch after an attempted apply in a retained checkout and remain locked; malformed/duplicate/conflicting events refuse; target/source hash mismatch refuses; events cannot follow symlinks; failure to install the pending event launches no payload; failure to write an outcome leaves original pending bytes intact and remains locked.
- [ ] Add tests for unique event IDs and stable attempt/run linking; reject duplicate IDs, missing/out-of-order predecessors, branches and contradictory outcomes. Only `pending -> apply-not-started` with current no-write evidence or `pending -> verified receipt` with exact source/target matching and fresh identity may reduce to a terminal state. `revise` and generated new revisions exclude old events.
- [ ] Run revision/migrate tests, verify intended failures, then implement immutable event creation with the atomic no-replace primitive at `scripts/migration_manifest.py:689–721`. Never mutate or replace an existing event.
- [ ] Run all affected focused modules. Confirm source hashes ignore operational event files while frozen SQL/check hashes remain exact. Upgrade preserves project-owned events.
- [ ] Before worktree deletion, require an operator-preserved archive or reviewed commit. No automatic commit or cross-worktree persistence is in scope; test the explicit preservation route chosen by the eventual operator workflow.
- [ ] Qualify interrupted/ambiguous writes only on an explicitly approved disposable target; never retry blindly to manufacture a clean receipt.
- [ ] Run full suite and prepare review; document that separate repositories' attempts are invisible.

### Task 3: Version diagnostics without tolerant acceptance

**Files:** Modify `scripts/deployment_descriptor.py`, `scripts/verify_publish_state.py`, `scripts/rollout.py`, publish/deploy wrappers; test deployment/source/publish/rollout modules; document publish rules and recovery.

**Interfaces:** `VersionPair(source: str | None, target: str | None)` in observed deployment/recovery data; `describe_version_pair(pair: VersionPair) -> str`. Observe source release at export time from a qualified APEX metadata field such as `APEX_RELEASE.VERSION_NO`; do not infer it from SQLcl, the template or a deployment descriptor. Persist provenance beside the source export, outside strict import override properties, and bind it to a digest of the canonical exported file set. Legacy/manual trees and source trees changed after export have unknown/stale provenance and remain exact-byte-only.

**Future integration points:** `scripts/baseline.py::_export_source` and its `source-manifest.json`; a small `scripts/source_provenance.py` serializer/validator; `tests/test_baseline.py` and `tests/test_source_provenance.py`. This is a future plan, not a change in this delivery.

- [ ] Add RED tests: unknown source, same-patch, different-patch, wrong major release, malicious release text, stale provenance, source-edited-after-export, legacy/manual source without metadata and unchanged raw mismatch behavior.
- [ ] Run focused deployment/source tests, implement sanitized diagnostics and preserve every existing byte/file-set failure.
- [ ] Prove only the qualified export-time metadata observation is recorded, with source-file-set digest equality required before any profile lookup. A source edit after export invalidates metadata; never rewrite the descriptor with provenance.
- [ ] Verify manual promotion states that no target version was observed when no connection occurred.
- [ ] Qualify the observation fields and exact import/re-export mismatch report on an authorized target before runtime claims.
- [ ] Keep serializer profile implementation deferred until authentic version-pair evidence exists; draft its fixtures, projections and acceptance gates separately.

### Task 4: Explicit template drift diagnostics

**Files:** Modify `scripts/verify_local.py`, doctor wrappers and `scripts/upgrade_template.py` reuse helpers; create `tests/test_template_drift.py`; update README and troubleshooting.

**Interfaces:** `inspect_template_drift(repo_root: Path) -> dict` returning separate `localFiles` and `upstreamComparison` statuses. Optional explicit upstream ref inspection must report the observed commit and never run from routine offline checks.

- [ ] Add RED tests for missing lock, LF/CRLF normalized equality, customized files, .template-new conflicts, release mismatch, preserved project-owned files and unreachable upstream.
- [ ] Verify failures, implement read-only hash comparison using existing validated lock helpers and no duplicate upgrade writer.
- [ ] Add an explicit read-only upstream-ref comparison only if authorized; no automatic network/startup fetching and no unsupported latest-version claim.
- [ ] Run readiness/upgrade/doctor/wrapper/manifest tests and full suite. Document that a local customization is not proof the template is outdated.

### Task 5: Scoped automation operations

**Files:** Create `scripts/automation_operations.py`, wrapper and tests; reuse `scripts/application_lifecycle.py`, identity/target/SQLcl helpers; update team wrappers, manifest and runtime docs.

**Interfaces:** CLI `automations list|check|enable|disable|run-once`, exact numeric app ID and environment/profile selection. `list/check` read only. A write command requires exact selection and confirmation showing its action; no implicit actions after import.

- [ ] Read `apex-workflow-lifecycle` and `apex-session-context` skills. Inspect public API/view contracts on the exact authorized release without guessing columns.
- [ ] Add RED fixtures for missing/wrong app, workspace/user/schema mismatch, incomplete visibility, selection ambiguity, preexisting states, failed API calls and retained recovery evidence.
- [ ] Implement list/check through existing scoped observations and run offline tests.
- [ ] Implement explicit write adapters only after public APIs/APEX-session requirements are qualified; confirm exact target/actions, retain before/after observations, never restore states automatically.
- [ ] Qualify enable/disable/run-once against tagged disposable fixtures, prove cleanup and keep Linux/Windows evidence separate.
- [ ] Run wrappers/full suite and document what runtime observations can and cannot prove.

### Task 6: Migration-owned rehearsal metrics

**Files:** Modify `scripts/migration_manifest.py`, `scripts/migration_rehearsal.py`, `scripts/migration_checks.py` through shared strict validation; tests manifest/rehearsal modules; document checks schema.

**Interfaces:** Optional `rehearsalMetrics` list of `id`, `sql`, `expectedBefore`, `expectedAfter`, `expectedRollback`; queries return one numeric scalar through existing guarded checks. The capture boundary is the complete folder/batch before DML, after that folder/batch payload, and after rollback, using author-selected tagged keys and predicates. These aggregates are not generic per-row or per-statement changed-value counts. No captured row values or inferred update columns.

- [ ] Add RED tests where affected-row count is positive but a folder's trigger-derived aggregate is unchanged, explicitly successful folder-level metrics, incomplete results, wrong identity and rollback-metric failure. Do not imply per-statement results without a separately designed statement-boundary contract.
- [ ] Add schema/input-cap/refusal tests; run RED before extending the optional contract.
- [ ] Capture metrics in the same transaction before/after DML and after rollback. A metric failure preserves diagnostics and prevents success; raw feedback remains informational.
- [ ] Run manifest/check/rehearsal and legacy-folder tests. Old checks.json remains compatible; metrics never permit DDL or autonomous-transaction claims.
- [ ] Native trigger/rollback qualification needs disposable tables and explicit fixture/cleanup authorization; keep unknown until proven.

### Task 7: Fixture and agent operational guidance

**Files:** Update `AGENTS.md`, `.agents/rules/agent-safety.md`, `docs/local-qualification.md`, `docs/production-deployment.md`, `self_improve.md` only for newly observed lessons; test documentation contracts.

- [ ] Explain tagged fixture ownership, target-derived expectations, DEFINE OFF, DML savepoint limits and explicit DDL/sequence/autonomous cleanup.
- [ ] Explain PID/process-group targeting and reading actual socket/network restrictions; remove blanket claims that all sandboxes block sockets.
- [ ] Document source/offline/native runtime/Windows evidence separately and forbid lowering assertions merely to pass.
- [ ] Run documentation/manifest tests, check links and LF; record any native gaps explicitly.

## Deferred idea6: custom function allow-list

Do not implement a simple project-editable purity list. First require a separate
security design that binds exact owner/name/edition/source digest and explains
how autonomy, package initialization, definer rights, remote calls and dependency
changes are independently audited. If these guarantees cannot be established,
keep the builtin-only query contract. This limitation is deliberate.

## Handoff

Review this draft and the source validity table before authorizing implementation.
Suggested order: Tasks1–2, then3–4, then5–6; Task7 accompanies each. Commit/push,
26.1 backport, native Windows, database qualification and downstream rollout are
separate operations requiring exact authorized scope.
