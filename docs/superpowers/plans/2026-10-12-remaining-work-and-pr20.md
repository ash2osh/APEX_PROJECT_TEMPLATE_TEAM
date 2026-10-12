# Remaining Work and PR #20 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.
> Execute inline only, preserving the user's chosen method. Steps use checkbox
> (`- [ ]`) syntax. This document is a planning deliverable, not an execution record.

**Goal:** Complete the remaining deployment-tooling features, qualification and
tracking work, and safely reconcile `feature/post-import-automations-verify` / PR #20.

**Architecture:** Deliver independent changes through the existing migration,
export, lifecycle, readiness, and SQLcl adapters. Add small shared helpers for
transport diagnostics, immutable attempt evidence, provenance, and rehearsal
metrics. Rework PR #20 into strict mismatch diagnostics and develop explicit
automation operations separately; retain current publish acceptance/recovery rules.

**Tech Stack:** Python 3.10+, standard library for core commands, unittest,
Bash/PowerShell, SQLcl 26.3.0.0+, Oracle Database, APEX 26.2 public APIs, existing CI.

**Spec:** [Planning decisions and branch reconciliation](../specs/2026-10-12-remaining-work-and-pr20-design.md)
and [original follow-up design](../specs/2026-10-11-deployment-followup-ideas-design.md).

**Baseline:** observed 2026-10-12, `main`/`origin/main` at
`df22dbbfc5a53bac6dc28f2b4f4910fec6604c6e`. PR #20 head is
`9a59a855ae098243d5b9d06966f117c3777c1814`. Refresh both before execution.

**Status:** Inline implementation in progress at the user's direction; see the
ignored progress ledger for completed source slices and open gates. This plan
supersedes the older seven-task roadmap. Earlier authorization that completed a
different merge does not automatically authorize integration of this work.

## Global constraints

- Python >=3.10; standard library only for core commands; Bash/PowerShell parity.
- `main`: APEX 26.2 and SQLcl >=26.3.0.0. Release 26.1 work is a separate backport.
- Use the existing checkout and named branches; no new worktree. Preserve
  `.env`, `apps/DEMO/`, `database/DEMO/`, `uv.lock`, and unrelated work. Stage explicit paths.
- At each execution task, inspect SQLcl's native Oracle skills registry and all
  loaded Oracle roots. Automatically refresh only when due under `AGENTS.md`.
- No shared database metadata tables, mutexes, rosters, or migration ledger.
- No implicit automation enablement, workflow/task repair, DB restoration,
  export, upgrade, import, commit or push. Reads use configured profiles and
  identity-checked read-only adapters. Explicit writes retain exact target scope.
- Unknown source/version pairs keep strict byte comparison after existing
  documented projections. A second stable export cannot excuse an unsafe projection.
- Runtime/lifecycle evidence is independent of source equality. Ambiguous writes
  retain old baselines, locks and recovery artifacts; no blind retry or automatic unlock.
- Keep the narrowly qualified DBMS_METADATA internal-commit exception restricted
  to its trusted helper with unconditional guard restoration; do not extend it to
  query, metric or automation payloads.
- Keep full logs, source/data snapshots and salts private. Track sanitized summaries
  only. Never print credentials, payload lines or hidden function execution output.
- Durable attempt events survive scratch deletion only in a retained checkout;
  preserve an operator-approved archive/commit before deleting that checkout.
- Shared-app and promotion tests need exact authorization/coordination. Reuse prior
  local Docker authorization only after verifying the current local disposable target.
- Windows/native/version-pair evidence cannot be inferred from offline or Linux tests.
- Every new confirmed inherited-template issue follows `docs/template-feedback.md`;
  deduplicate upstream, record TODO evidence, and queue reporting failures while continuing.

## Review focus

1. A line inside an APEXlang/SQL literal can resemble a comment, block, or transport
   delimiter: preserve it; do not normalize meaningful whitespace/order (Tasks 3–4, 15).
2. Scratch disappears after a migration write, or an event/receipt is edited or
   symlinked: source remains frozen; no replay based on missing evidence (Task 5).
3. A same-named app/schema/automation exists on another DB/workspace or changes after
   confirmation: refuse before mutation and retain scoped evidence (Tasks 6, 8–9, 14).
4. Later migration folders depend on earlier uncommitted DML: metrics and rollback
   must use their defined folder/transaction boundaries without weakening assertions (Task 10).
5. A merge has only documentation conflicts while automatically combining a policy
   change: review every nonconflicted behavioral change, not just conflict markers (Tasks 3, 17).

## Work packages and order

| Task | Deliverable | Dependency | Execution gate |
|---|---|---|---|
| 1 | Accurate merged-status docs/tracking | None | Local files only; remote updates separately authorized |
| 2 | Native synonym/index regression and issue disposition | 1 | Fresh disposable target identity |
| 3 | PR #20 reworked into strict complete mismatch diagnostics | 1 | Plan accepted; branch updates explicitly authorized |
| 4 | SQLcl transport preflight | 1 | Offline first; native tagged cases |
| 5 | Durable migration attempt events | 4 for final payload launch order | Offline first; interrupted-write qualification |
| 6 | Export-bound version provenance/diagnostics | 3 | Qualified export observation; no tolerant acceptance |
| 7 | Template drift diagnostics | 1 | Offline default; explicit upstream comparison |
| 8 | Automation list/check | 3's preserved lifecycle contract | Read-only target qualification |
| 9 | Explicit enable/disable/run-once | 8 | Public API/native fixture and exact action confirmation |
| 10 | Rehearsal metrics schema/transaction integration | 4–5 | DML-only fixture qualification |
| 11 | Fixture/agent guidance | Accompanies 2–10 | Evidence-supported docs |
| 12 | Native Windows qualification | Features under test complete | Actual Windows host/client availability |
| 13 | APEX 26.1 backport/additional DB/ORDS pairs | Reviewed main features | Exact release environments; separate branch |
| 14 | Shared-app import and distinct-target promotion | 3, 6, 8–9 | Exact apps, targets, roster/coordination and authorization |
| 15 | Qualified tolerant profiles, conditional | 6, 14 | Explicit policy approval and authentic pair evidence |
| 16 | Custom-function security design, conditional | None; no blocker to other tasks | Research verdict before any allowance |
| 17 | Final review, PR/branch integration and status reconciliation | Each selected slice complete | Current-task commit/push/merge authority |

Recommended sequence: 1–2; then 3 as its own PR-sized slice; then 4–5, 6–7,
8–9, and 10. Apply 11 alongside each slice. Run 12–14 only where the exact
environment/authorization exists. Tasks 15–16 may finish as deferred design verdicts.
Use Task 17 for each integration slice rather than accumulating one giant merge.

## Source and interface map

| Unit | Existing integration points | New responsibility |
|---|---|---|
| Strict source diagnostics | `verify_publish_state.py::verify_source_bytes`, `partial_publish.py` | Enumerate sanitized raw mismatches without relaxing acceptance |
| Transport | `migrate.py`, `rollout.py::_script_driver`, `baseline.py` generators | Inspect the actual executable delivery SQL before launch |
| Attempt evidence | `migration_revision.py::inspect_migration_lock`, `migrate.py::_verify_receipts_and_attempts`, `_update_run_manifest` | Immutable linked events reduced by one lock contract |
| Provenance | `export_apps.sh/.ps1/.sql`, `record_export_state.py::marker_payload`, `baseline.py::_export_source` | Export-time observed release bound to exact canonical file set |
| Drift | `upgrade_template.py::read_lock_with_hash`, `normalized_sha256`, `verify_local.py::build_report` | Local hash status and separate explicit upstream status |
| Automations | `application_lifecycle.py::capture_lifecycle`, `db_targets.py::resolve_target` | Read-only inspection and separate explicit mutation adapters |
| Metrics | `migration_manifest.py::_load_check_file`, `migration_rehearsal.py` session/check framing | Folder-bound numeric metrics, before/after/final rollback |

Existing migration receipts are schema 1; rollout receipts are schema 2.
Do not conflate either with the proposed attempt-event schema 1 or checks schema 2.

## Remaining-item coverage

| Prior remaining item | Covered by |
|---|---|
| SQLcl transport diagnostics | Task 4 |
| Durable attempt records | Task 5 |
| Version diagnostics | Task 6; conditional tolerance in Task 15 |
| Template drift | Task 7 |
| Explicit automation operations | Tasks 8–9; PR #20's implicit route excluded by Task 3 |
| Rehearsal metrics | Task 10 |
| Fixture and agent guidance | Task 11 |
| Mixed-index native gap and issue #19 | Tasks 1–2 |
| Windows / 26.1 / other release-pair gaps | Tasks 12–13 |
| Shared-app and distinct-target promotion gaps | Task 14 |
| Custom-function allow-list | Task 16's security verdict |
| PR #20 conflicts, stale trackers and obsolete branches | Tasks 1, 3 and 17 |

## Common task verification procedure

For behavior changes, use the named focused tests below: add a meaningful failing
case, run it and record the intended failure, implement the smallest correction,
then rerun the focused module. A failure caused only by fixture setup is not RED proof.
Do not lower an assertion to obtain green. Documentation-only changes use link,
status and existing contract checks, without adding trivial implementation-mirror tests.

Run focused tests from repository root with `PYTHONPATH=tests:.`. Reuse the project's
working Python/PATH environment; if a local PowerShell wrapper hangs, diagnose it
and record the qualified runtime instead of permanently removing PowerShell coverage.

At each reviewable slice, run:

```bash
PYTHONPATH=tests:. PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
uvx --offline ruff check scripts tools tests
git diff --check
bash -n scripts/team.sh scripts/load_env.sh scripts/check_db_target.sh scripts/sqlcl_safe.sh
python3 -m json.tool template-manifest.json
```

Syntax-check additional modified `.sh` files too. Validate all added/changed Markdown
relative links and heading fragments, LF source/SQL, and manifest ownership. If the
offline Ruff cache is unavailable, report that limitation or use an approved installed
Ruff; do not label an unavailable check passed. Preserve a uniquely named ignored log.
Report observed test totals/skips, not a hardcoded promise of 1,488 tests forever.
Repeat broad checks only after changes or unresolved concerns justify them.

---

### Task 1: Reconcile current delivery status and tracking

**Files:** Modify `TODO.md`, `docs/superpowers/plans/2026-10-10-deployment-speedups-next.md`,
`docs/superpowers/reviews/2026-10-11-deployment-speedups-review.md`; link this plan from
the older follow-up plan without erasing historical approvals/results.

**Interfaces:** Retain TF-001 and its dated history. Current status is
`fix merged in main at df22dbb; native mixed-index qualification passed; issue #19 closed`.

- [x] Refresh `main`, PR #20 and issue #19 through read-only Git/GitHub calls; record exact SHAs and timestamps. Main is `df22dbb`; PR #20 remains open/dirty at `9a59a85`; issue #19 was closed on 2026-10-12 after its sanitized evidence update.
- [x] Replace present-tense uncommitted/unmerged claims with the actual merged commit; preserve historical snapshots as explicitly dated history.
- [x] Mark the old seven-task draft as superseded by this consolidated roadmap; retain its design links.
- [x] Update TF-001 with native qualification and issue closure; no duplicate issue was created.
- [x] Validate links and run documentation/manifest/upgrade contract checks (101 tests passed).

**Acceptance:** main's delivery is clearly complete; remaining features/gates have distinct statuses.
Only local docs change unless remote issue updates are explicitly authorized.

### Task 2: Qualify the mixed-index synonym fix natively

**Files:** Create `tools/qualify_synonym_namespaces.py`,
`tests/test_qualify_synonym_namespaces.py`; extend `tests/test_migration_checks.py`
where new raw-row cases require it. Modify `docs/local-qualification.md`,
`docs/apex-26.2-qualification.md`, `TODO.md`, and manifest for the new reusable tool.

**Interfaces:** Runner arguments follow the existing qualification tools: saved
`--connection`, `--expected-user`, `--schema`, observed host/service, unique
`--report-dir`, explicit `--allow-writes`. Privileged fixture setup, if needed,
uses a separately identity-verified local admin alias. `qualification.json` binds
tested commit, catalog route, actual dictionary rows, parser/preflight outcome,
fixture ownership and cleanup status; raw logs remain ignored.

- [x] Add `test_wrong_target_creates_no_fixture`, `test_preexisting_name_refuses`,
  `test_cleanup_requires_owned_inventory`, and `test_interruption_reports_cleanup_pending`.
  Assert zero write calls on wrong identity/name collision; no unowned drop on cleanup.
- [x] Run `PYTHONPATH=tests:. python3 -m unittest tests.test_qualify_synonym_namespaces -v` and record intended missing-tool/guard failures.
- [x] Implement setup/cleanup guards using the existing named-target SQLcl adapters and no credentials in scripts/reports.
- [x] Discover an unused tagged fixture prefix and verify local DEMO2 identity. Create
  a disposable target owner only if an external-owner fixture requires it; inspect
  privileges and refuse rather than borrowing an unrelated schema.
- [x] Capture actual TABLE/1 plus INDEX/4 and TABLE/1 plus MATERIALIZED VIEW/19 plus
  INDEX/4 rows. Create a direct SELECT grant and private synonym from the consumer
  schema. Exercise the actual DBA and ordinary-owner catalog queries, production
  decoder and migration preflight using an unexecuted CREATE VIEW migration.
- [x] Verify valid target cases pass; SEQUENCE and absent targets refuse. Use offline
  fixtures for impossible dictionary ambiguities instead of fabricating native rows.
- [x] Drop/purge only owned fixtures. Independently query owner objects, synonyms,
  grants, temporary schema and recycle-bin inventory; report every expected zero.
- [x] Update TF-001 with native evidence, link the merged fix/evidence in issue #19, and close the resolved issue.

**Acceptance:** native owner and privileged paths prove the mixed-index fix and all
owned cleanup. A privilege/tool outage yields unavailable, not passed or forced grants.

### Task 3: Reconcile PR #20 into complete strict mismatch diagnostics

**Files:** Inspect all 26 files in PR #20. Modify `scripts/verify_publish_state.py`,
`scripts/partial_publish.py`, publish wrappers only where diagnostics propagation
requires it; create `scripts/apexlang_diff.py`, `tests/test_apexlang_diff.py`.
Reconcile affected AGENTS/README/publish/partial/limitation/troubleshooting docs and
`tests/test_verify_publish_state.py`, `tests/test_publish_cli.py`,
`tests/test_partial_publish.py`, `tests/test_team_cli.py`, lifecycle tests.

**Interfaces:** `SourceMismatch(path: str, kind: str, first_differing_line: int | None)`;
`collect_source_mismatches(source_dir: Path, exported_dir: Path, deployment: dict | None = None) -> tuple[SourceMismatch, ...]`.
`kind` is `missing`, `unexpected`, `bytes`, or `required-missing`. Apply only the
existing documented deployment/source projections. Keep `verify_source_bytes(...) -> None`
compatible: raise on any mismatch, preserving existing refusal-message prefixes.
Diagnostics expose relative paths/line numbers only, not source contents.

- [ ] Record the original PR head and original behavior inventory. Reconfirm the two
  text conflicts and nonconflicted automatic restoration/default normalization changes.
- [ ] After execution is authorized, switch to a local tracking branch for
  `origin/feature/post-import-automations-verify` in the existing checkout; preserve
  local untracked data. Merge current `main` into it without resetting either branch.
- [ ] Resolve troubleshooting/limitations conflicts by reconciling both documented
  purposes. Review every automatically merged file too. Preserve current query CLI,
  target freezing, feedback rules, lifecycle attention, native locks and strict bytes.
- [ ] Remove implicit restore hooks and default normalized acceptance from the candidate.
  Do not retain new `--no-restore-automations` or `--strict-bytes` switches merely to
  imply unsafe defaults are an established API. Branch-only tests/docs must follow
  the corrected behavior. Keep reusable ideas as evidence for Tasks 8–9 and 15.
- [ ] Add RED cases `test_all_byte_mismatches_reported`, `test_literal_comment_line_is_meaningful`,
  `test_literal_trailing_spaces_are_meaningful`, `test_column_order_change_still_fails`,
  `test_duplicate_message_key_not_collapsed`, `test_invalid_utf8_still_fails`, and
  `test_missing_required_file_still_fails`. Assert two changed files produce two records;
  all those changed source cases fail acceptance; no source line appears in stderr.
- [ ] Add integration cases `test_lifecycle_attention_does_not_enable_automation` and
  `test_stable_second_export_does_not_accept_byte_mismatch`: mutation calls equal zero;
  old baseline and retained run-owned recovery lock remain unchanged.
- [ ] Run RED: `PYTHONPATH=tests:. python3 -m unittest tests.test_apexlang_diff tests.test_verify_publish_state tests.test_application_lifecycle -v`.
- [ ] Implement the collector and integrate whole/selected-page diagnostics without
  altering file-set exclusions, deployment remapping or revision/baseline gates.
- [ ] Run GREEN plus publish/partial/team/rollout focused tests. Run the common checks
  on the merged candidate; its old branch-head CI result is insufficient.
- [ ] At authorized integration, update PR #20's title/body around strict multi-file
  diagnostics and explicit native limits; push additional commits normally, with no
  force push. Merge only after the corrected candidate is reviewed and current checks pass.

**Acceptance:** PR #20 is a coherent strict-diagnostics slice. No automated
enablement or normalized source acceptance enters `main` through conflict resolution.

**Current status (2026-10-12):** the strict diagnostic implementation is on
`codex/remaining-work-implementation`, but this task remains open. PR #20 is still
open and marked dirty against `main`; it has not been edited or merged. The focused
source-diff regression module was not present in this checkout, so named RED/GREEN
coverage remains open pending review of the current changed-file suites.

### Task 4: Add SQLcl executable-transport preflight

**Files:** Create `scripts/sqlcl_transport.py`, `tests/test_sqlcl_transport.py`;
modify `scripts/migrate.py`, `scripts/rollout.py`, `scripts/baseline.py` at executable
handoff/generation boundaries; update migration rules/limitations and wrapper errors.

**Interfaces:** `TransportFinding(code: str, line: int, severity: str, message: str)`;
`lint_sqlcl_transport(sql: str, delivery_mode: str) -> tuple[TransportFinding, ...]`,
modes exactly `direct` or `assembled`, severity `error` or `warning`.
Inspect the emitted driver in both modes; assembled data is not itself executable SQL.
Messages include locations and safe delivery advice, never payload excerpts.

- [ ] Add RED cases for direct physical trailing-hyphen corruption, source-bearing
  blank-line risk, ordinary statement spacing, multiline q/N literals, comment-looking
  literal lines, CRLF, chunk limits/emoji, and safe hexadecimal/CHR assembly.
  Pin the previously native-qualified multiline view literal as accepted, not globally banned.
- [ ] Assert `test_proven_hazard_starts_zero_sqlcl_payloads` and
  `test_warning_does_not_change_payload_bytes`; test both direct and assembled drivers.
- [ ] Run `PYTHONPATH=tests:. python3 -m unittest tests.test_sqlcl_transport -v` for RED.
- [ ] Implement literal/comment-aware diagnostics using shared lexical primitives
  without relaxing `validate_check_query`. Encode only reproduced transport corruption
  as errors; uncertain formatting risks remain warnings. Invalid delivery modes refuse.
- [ ] Integrate after exact frozen/generated input validation but before the first
  payload launch. Refusal must not create a false write-attempt or verified receipt.
- [ ] Run GREEN plus migration/rollout/baseline/check suites. On the authorized local
  pair, qualify direct versus assembled storage for tagged hyphen/blank/Unicode cases
  and compare exact stored source. Preserve and prove cleanup.

**Acceptance:** proven transport corruption refuses before mutation; safe literals
are unchanged and accepted; no warning or lint success replaces source verification.

### Task 5: Persist immutable local migration-attempt events

**Files:** Create `scripts/migration_attempts.py`, `tests/test_migration_attempts.py`;
modify `scripts/migration_manifest.py`, `scripts/migration_revision.py`,
`scripts/migrate.py`, `scripts/rollout.py` only where linkage is needed, baseline
revision generation and upgrade preservation; test existing revision/migrate CLI/
manifest/rollout/baseline/upgrade suites and document migration rules.

**Interfaces:** `write_attempt_event(folder: Path, event: Mapping) -> Path`;
`read_attempt_events(folder: Path) -> tuple[Mapping, ...]`;
`reduce_attempt_events(events: Sequence[Mapping]) -> AttemptState`.
`AttemptState(status: str, states: tuple[str, ...])` has status
`unlocked`, `locked`, or `unknown`; it supplies normalized
facts to the existing `inspect_migration_lock` contract, not a competing lock scanner.
Event fields: `schemaVersion=1`, UUID `eventId`, UUID `attemptId`, `runId`,
`predecessorEventId`, UTC `recordedAt`, repository-relative migration path,
environment, configured target binding, observed identity (when available), exact
SQL/check digests, `state`, `writeAttempted`, and bounded evidence references/digests.
States are `pending`, `apply-not-started`, `verified`; interrupted outcomes retain pending.
`pending` conservatively records `writeAttempted=true` before launch, `verified`
keeps it true, and only proven `apply-not-started` records false. No event is created
for offline planning/dry-run. This task preserves current rehearsal semantics rather
than silently imposing a normal-apply receipt/event contract on rollback-only runs.

- [ ] Add RED cases `test_scratch_deleted_after_write_remains_locked`,
  `test_pending_install_failure_launches_no_payload`, `test_outcome_failure_preserves_pending_bytes`,
  `test_verified_event_keeps_source_frozen`, `test_missing_no_write_evidence_cannot_unlock`,
  and `test_changed_source_or_target_refuses_resume`.
- [ ] Add schema/path cases: duplicate keys/UUIDs, broken predecessor, branching chain,
  contradictory terminals, symlinks/reparse points, bad timestamps, parallel attempts,
  race installing the same filename, and events in another schema's folder.
  Different attempt IDs are separate chains; the folder stays locked if any is
  locked/unknown. Sort dependencies by predecessor, never filesystem order/time alone.
- [ ] Run `PYTHONPATH=tests:. python3 -m unittest tests.test_migration_attempts tests.test_migration_revision tests.test_migrate_cli -v` for RED.
- [ ] Implement atomic no-replace installation using the existing receipt writer's
  durability/error pattern; flush content and validate physical parents. Never mutate
  or overwrite an earlier event; a failed terminal installation leaves pending intact.
- [ ] Extend `_validate_folder_entries` to accept only validated event filenames;
  exclude them from executable files/input hashes, frozen payload copies and generated
  new revisions. Keep SQL and checks immutable and receipt processing separate.
- [ ] Install pending after final preflight/confirmation and before launch for every
  normal migration payload. Install a terminal only from existing no-write proof or
  verified apply/receipt evidence. A verified terminal remains locked. Offline lock
  inspection never claims a fresh DB identity; live resume must reobserve it.
- [ ] Merge existing scratch facts, events and receipts in the current lock contract.
  Missing evidence needed for an apply-not-started terminal yields unknown/locked.
  Do not unlock merely because `writeAttempted` is false in user-editable JSON.
- [ ] Run focused GREEN and common checks. Qualify one owned interrupted/uncertain
  DML attempt: delete only its scratch copy, confirm edit/replay refused by durable
  evidence, reconcile externally, and clean tagged rows/objects without blindly retrying.

**Acceptance:** scratch loss cannot erase a write-attempt freeze. `revise` produces
the next clean source revision; upgrade preserves project evidence. Worktree/repo
deletion and another developer's independent repository remain outside durability.

### Task 6: Capture export-bound source releases and version diagnostics

**Files:** Create `scripts/source_provenance.py`, `tests/test_source_provenance.py`;
modify `scripts/export_apps.sh`, `scripts/export_apps.ps1`, `scripts/export_apps.sql`,
`scripts/record_export_state.py`, `scripts/source_evidence.py` only for shared
physical-path/hash validation, `scripts/verify_publish_state.py`, rollout and
promotion report integration, `scripts/baseline.py::_export_source`, and export/
source/baseline/publish tests, including `tests/test_export_cli.py` and
`tests/test_record_export_state.py`. Document marker compatibility and publish recovery.

**Interfaces:** `VersionPair(source: str | None, target: str | None)`;
`canonical_source_digest(source_dir: Path) -> str`;
`read_source_provenance(source_dir: Path) -> SourceProvenance`;
`SourceProvenance(status: str, source_release: str | None, captured_at: str | None,
canonical_digest: str | None, reason: str | None)` with status `verified`, `unknown`,
or `stale`; `describe_version_pair(pair: VersionPair) -> str`.
Add an optional nested `sourceProvenance` object to the existing schema-2
`apex-team-export.json` marker: nested `schemaVersion=1`, `sourceRelease`,
`exportedAt`, `canonicalDigest`. Do not overwrite the current `sourceFormat` /
`sourceFiles` baseline or reinterpret its major-release label as observed patch provenance.
Do not add import-override descriptor properties. Hash the canonical file set with
sorted relative paths plus lengths/content and unambiguous framing, following
`verify_publish_state.py::source_files` exclusions. Reuse physical-path validation
without changing the existing selected-page baseline's broader sourceFiles contract.

- [ ] Add RED cases: edited/deleted/added source file, old marker without provenance,
  manual tree, malformed release, changed release during export, malicious metadata,
  ignored operational-file edit, descriptor edit, and descriptor-schema remapping.
  Assert stale/unknown provenance never selects a tolerant profile or changes equality.
- [ ] Run source-provenance, export, baseline and verifier focused RED tests.
- [ ] Observe APEX release through the guarded export session's qualified metadata;
  compare before/after observation if the export spans a release transition. Record
  provenance only after successful export/source review guards. Read marker compatibility
  safely; never infer APEX release from SQLcl/template/deployment descriptor.
- [ ] For baseline stored-code export, record observed database release separately;
  only include an APEX release when its own qualified observation actually occurred.
- [ ] Add sanitized version context to recovery/report data and keep all strict failures.
  A manual deployment reports target release unknown when no connection occurred.
- [ ] Run GREEN including fixtures for legacy downstream markers. Qualify metadata
  fields on the actual target and a disposable export; shared app exports require their
  separate user request because they refresh tracked source.

**Acceptance:** a release claim is bound to the exact observed export. Diagnostics
explain mismatch context while acceptance remains strict.

### Task 7: Add explicit template drift diagnostics

**Files:** Create `scripts/template_drift.py`, `tests/test_template_drift.py`;
modify readiness/doctor integration and team wrappers; reuse upgrade lock/hash
helpers; document README/troubleshooting. Add helpers to upgrade code only where
sharing removes duplicate validation, not a second upgrade writer.

**Interfaces:** `inspect_template_drift(repo_root: Path) -> dict`, schema version 1,
separate `localFiles` and `upstreamComparison`. Local file states: `matches-lock`,
`customized`, `missing`, `unsafe`; absent downstream lock is `unknown`; original
template is `not-applicable`. CLI proposal: `team.sh template-status --format text|json`
and explicit `--upstream --ref <ref>`. Default upstream status is `not-checked`.

- [ ] Add RED cases for LF/CRLF-normalized equality, edited file, absent/unsafe lock,
  missing file, `.template-new` conflicts, original repo, release mismatch, project-owned
  TODO/AGENTS.project preservation, and upstream outage/ref injection.
- [ ] Assert default readiness/doctor/template-status performs zero fetches and zero writes.
- [ ] Run template-drift/readiness/upgrade focused RED tests.
- [ ] Implement local inspection with `read_lock_with_hash`, validated ownership and
  existing normalized hashes. Keep customization separate from upstream obsolescence.
- [ ] Implement explicit verified-upstream comparison in a new ignored temporary
  directory using argument arrays and validated refs. Report observed commit and
  network failure; never install files, repair locks or modify source as a diagnostic.
- [ ] Run GREEN and wrapper parity tests. Verify a downstream fixture preserves
  project-owned files and routine offline commands stay offline.

**Acceptance:** developers can see what changed locally and what is known about
upstream without an automatic fetch/upgrade or a false outdated warning.

### Task 8: Add scoped automation list/check commands

**Files:** Create `scripts/automation_operations.py`,
`tests/test_automation_operations.py`; update team wrappers, docs and manifest.
Reuse `application_lifecycle.py::capture_lifecycle` without adding writes to it.

**Interfaces:** `team.sh automations list|check <numeric-app-id> --env <env>
--schema <schema> [--automation <static-id> ...] [--expected-status <literal>]
--format text|json`.
Resolve scope through the numeric application's deployment descriptor and existing
configured target; static IDs are exact, unique and belong to that app/workspace.
`list_automations(snapshot: Mapping) -> tuple[dict, ...]`;
`select_automations(rows: Sequence[Mapping], static_ids: Sequence[str]) -> tuple[dict, ...]`.
`check` requires an explicit expected status and exact selection; unavailable visibility
is distinct from a mismatching state. Use the actual qualified public status vocabulary.

- [x] Read release-matching workflow-lifecycle/session-context skills before adapters.
- [ ] Add RED cases for wrong descriptor/app/workspace/user/schema, same static ID in
  another app, absent/duplicate selection, incomplete coverage, bad status, arbitrary
  target override, output caps, and credential/payload leakage. Assert zero write calls.
- [ ] Run automation-operations/lifecycle/multi-schema/team CLI RED tests.
- [x] Implement read-only commands through current lifecycle/target/SQLcl helpers;
  do not create an APEX session or database metadata ledger unless the qualified read
  API actually requires the existing supported context.
- [ ] Run GREEN and qualify app-scoped observations on an owned disposable app or an
  expressly authorized existing read target. Empty rows are not positive API coverage.

**Acceptance:** exact read-only inventory/check works identically from Bash and
PowerShell; no caller sees another app's automation or an implicit restore suggestion
misrepresented as an action already taken.

### Task 9: Add explicit automation writes and qualify their APIs

**Files:** Extend `scripts/automation_operations.py`; create
`scripts/automation_operations.sql`, `tools/qualify_automation_operations.py`,
`tests/test_qualify_automation_operations.py`; update Task 8 tests, qualification
docs, runbook and manifest. Reference PR #20's approach as unqualified evidence,
not an approved API contract to copy without verification.

**Interfaces:** CLI `automations enable|disable|run-once <app-id> --env <env>
--schema <schema> --automation <static-id>`; initially one automation per write.
`execute_automation_action(target: Target, workspace: str, app_id: int,
static_id: str, action: str, expected_snapshot: Mapping, run_dir: Path) -> dict`.
Report schema 1: action/scope, before/after observations, API outcome and
`status=verified|refused|unknown`. Confirmation displays environment, saved alias,
observed identity, app/workspace/schema, static ID, current state and exact action.

- [x] Inspect installed public API signatures, privileges, security-group/session
  requirements and commit semantics on APEX 26.2. Record unavailable prerequisites;
  do not guess from an APEX 26.1 downstream report.
- [x] Add RED cases for no/negative confirmation, changed identity/app/selection after
  confirmation, another operation's native lock, failed API, missing read-back,
  asynchronous run, interrupted commit and repeated run-once requests.
  Assert no automatic inverse action, no workflow/task repair, and no publish call site.
- [ ] Run automation/lifecycle/publish/partial/promotion focused RED tests.
- [x] Implement writes in the separate adapter. Recheck live scope immediately before
  mutation; preserve before evidence; invoke the qualified public API; read back the
  same static ID. A timeout/ambiguous run is unknown and never automatically replayed.
- [x] For `run-once`, success requires qualified execution evidence, not merely an
  unchanged ACTIVE status or a completed client process. Pending asynchronous work
  remains pending/unknown until the defined observation proves completion.
- [x] Qualify enable/disable and a no-op run on a freshly identified unused app in an
  existing local workspace. Assert unselected disabled automations stay disabled and
  no supporting code/endpoint executes outside owned fixture scope. Clean app/session/
  run artifacts through the verified owned cleanup route.
- [x] Run GREEN and common checks. Document a manual recovery example using exact
  static ID, followed by independent lifecycle reconciliation; it cannot advance a
  prior failed publish's baseline or release its recovery lock by itself.

**Acceptance:** automation mutations are intentional, scoped, evidenced operations.
Publish/deploy behavior continues to observe and refuse lifecycle attention.

**Focused integration tests still open:** the automation/lifecycle/publish/partial/
promotion regression set has not all completed in this run. The 99-test automation/
native feature suite and 46-test team CLI suite passed.

### Task 10: Add migration-owned rehearsal metrics

**Files:** Create `scripts/rehearsal_metrics.py`, `tests/test_rehearsal_metrics.py`;
modify migration-manifest/check framing/rehearsal/frozen-input integration; test
`tests/test_migration_manifest.py`, `test_migration_checks.py`,
`test_migration_rehearsal.py`, `test_migrate_cli.py`, `test_rollout.py`,
`test_migration_revision.py`; document checks schema and report fields.

**Interfaces:** `RehearsalMetric(id: str, sql: str, expectedBefore: int,
expectedAfter: int, expectedRollback: int)`; add
`Migration.rehearsal_metrics: tuple[RehearsalMetric, ...] = ()`.
Schema 1 remains exact. Schema 2 has the original keys plus optional
`rehearsalMetrics`; reject other keys. Limit 50 metrics, CHECK_ID_RE IDs unique
within metrics, safe JSON integers as specified in the design, one guarded SELECT
per metric. Example:

```json
{"schemaVersion":2,"preconditions":[],"postconditions":[{"id":"row-present","sql":"SELECT COUNT(*) FROM QUALIFIED_ROWS WHERE ID = 1","expected":1}],"rehearsalMetrics":[{"id":"tagged-total","sql":"SELECT COALESCE(SUM(AMOUNT), 0) FROM QUALIFIED_ROWS WHERE ID = 1","expectedBefore":0,"expectedAfter":25,"expectedRollback":0}]}
```

The example is source-format guidance, not permission to execute this table/query.
COALESCE and SUM are already present in `migration_manifest.py::SAFE_FUNCTIONS`;
this example requires no function allowance expansion.

Metric-specific helpers in `scripts/rehearsal_metrics.py` are
`render_metric_driver(target: Target, metrics: Sequence[RehearsalMetric], phase: str) -> str`
and `parse_metric_frame(output: str, metrics: Sequence[RehearsalMetric], phase: str,
expected_identity: Mapping[str, str]) -> MetricCapture`.
`MetricCapture(complete: bool, identity: dict[str, str], values: dict[str, int],
errors: tuple[str, ...])` represents a fully checked frame; incomplete frames must
not expose partial values as verified. The new phase names are defined below.

- [x] Add RED schema cases: v1 byte/digest compatibility, unknown schema/keys,
  duplicate IDs, 51 metrics, boolean/float/out-of-range expected values, invalid query,
  operational sidecars excluded but metrics bytes included in frozen checks digest.
- [x] Add RED runtime cases `test_metrics_observe_trigger_effect_not_row_feedback`,
  `test_dependent_folders_capture_each_apply_boundary`, `test_metric_mismatch_fails`,
  `test_missing_or_wrong_identity_frame_fails`, `test_interrupt_never_claims_rollback`,
  and `test_final_rollback_matches_declared_value`.
- [x] Run metrics/manifest/rehearsal/check focused RED tests.
- [x] Extend schema loading without relaxing pre/post `expected == 1` or normal
  migrate/verify checks. Metrics are rehearsal-only; regular apply hashes/preserves
  them but never silently evaluates them as postconditions. Older loaders refuse v2.
- [x] Reuse the bounded DBMS_SQL scalar transport through a small explicit generic
  capture interface; keep existing check-phase semantics unchanged. New metric
  frames name `metric-before`, `metric-after`, and `metric-rollback`, carry complete
  identity and exact requested IDs/order, and never print rows or metric SQL.
- [x] Capture each folder's before/after metrics in the current rehearsal transaction
  around its payload; capture all rollback metrics after final ROLLBACK. Refuse
  incomplete/nonnumeric results. If the session dies, a fresh identity-verified read
  may observe final values but cannot claim the original session's explicit rollback.
- [x] Preserve dependent-folder starting-value rollback proof. A metrics failure does
  not reinterpret an unrelated assertion or turn nonrehearsable DDL into DML.
- [x] Run GREEN and qualify a tagged INSERT plus reviewed trigger that changes
  the aggregate independently of row feedback. Verify in-session and fresh-connection
  results; separately drop/purge owned table/trigger, prove inventory zero.

**Acceptance:** declared folder metrics are trustworthy within their stated
transaction scope. Sequence/autonomous/external effects remain outside rollback proof.

### Task 11: Consolidate fixture and agent operational guidance

**Files:** Modify AGENTS/safety rules, `self_improve.md`, local qualification,
production runbook and relevant result summaries; update existing documentation
contracts only when a supported interface/refusal message actually changes.

**Interfaces:** Every report labels `source`, `offline`, `nativeOracle`,
`nativeWindows`, and `historical` evidence separately; qualification states use
`passed`, `failed`, `unavailable`, or `not-run`, with exact evidence paths.

- [x] Document fresh identity, unused numeric app IDs, tagged ownership, before-write
  inventory, action confirmation, independently observed cleanup and retained recovery.
- [x] Document SQLcl DEFINE handling, correct script paths and supplementary-character
  CLOB lengths only from current code/native evidence; include direct-versus-assembled limits.
- [x] Document that DML savepoints/rollback cannot reverse DDL, sequences, autonomous
  transactions or endpoints. Do not infer API completion from empty observations.
- [x] Document exact PID/process-group cleanup; avoid pattern-killing the invoking
  shell. Socket availability is environment-specific, never a universal sandbox claim.
- [x] Run documentation/manifest/upgrade contract checks and link validation. Preserve
  project-owned TODO and instructions across upgrades.

**Acceptance:** the guide reflects newly proven behavior and explicit unknowns;
it neither manufactures approvals nor duplicates stale historical completion claims.

### Task 12: Qualify native Windows behavior

**Files:** Modify `.github/workflows/database-checks.yml` for a Windows offline
job as part of the accepted task; tests/fake launchers only for observed portability failures;
update `scripts/windows_job.py`, PowerShell wrappers or SQLcl helpers only from
reproduced faults. Record results in platform qualification docs.

**Interfaces:** Matrix binds Windows version, PowerShell, Python, Bash provider,
SQLcl executable and tested commit. Offline Windows CI and live Windows-to-Oracle
qualification are separate statuses. CI contains no saved aliases/secrets/live writes.

- [ ] Confirm an actual Windows executor and required runtime installations exist;
  the workflow now targets `windows-latest`, but its first result is pending push.
- [ ] Run native Windows offline tests including paths with spaces/Unicode,
  console Ctrl-C, Job Object descendant shutdown, deadlines/EOF, byte overflow,
  stdout withholding, environment quoting and source/config freezing.
- [ ] For each failure, reproduce a meaningful RED case on Windows, apply the smallest
  cross-platform fix, rerun that test and both platform-focused suites.
- [ ] With an explicitly configured authorized Oracle target from Windows, qualify
  client identity/encoding/read-only guards plus selected disposable DML/cancellation
  scenarios; qualify automation writes only where their exact fixture scope is authorized.
- [ ] Confirm no descendant SQLcl remains after interruption and independently prove
  DB cleanup; report offline skipped cases and native unknowns distinctly.

**Acceptance:** a Linux PowerShell executable is never presented as native Windows
proof. A Windows offline CI pass alone does not prove native SQLcl/database behavior.

**Current status:** the offline Windows job is implemented; its first CI run is pending
push. Native Windows-to-Oracle qualification remains not-run.

**Current status:** the offline Windows job is implemented; its first CI run is pending
push. Native Windows-to-Oracle qualification remains not-run.

### Task 13: Qualify APEX 26.1 backport and other release pairs

**Files:** Separate release branch for approved backport; release-specific manifest,
APEX adapters/skills/docs/tests only for supported APIs. Main qualification matrix
gets summaries, not silently weakened version floors.

**Interfaces:** Each row names DB/APEX/ORDS/SQLcl release, OS, branch/commit,
tested command scope, outcome and owned cleanup. An absent pair is `not-run`.

- [x] Inventory actual available installations and remote branches. Remote branch
  `codex/apex-26.1` is absent; only the APEX 26.2 local Docker pair was available
  for this task. Do not assume an APEX 26.1 baseline.
- [ ] When backport is requested, establish the documented release branch from an
  identified 26.1-compatible baseline. Use release-matching skills and establish its
  client floor from that contract; never blindly cherry-pick 26.2 API calls.
- [ ] Select target-independent reviewed fixes, then add release-specific RED fixtures
  for unavailable lifecycle/API/export metadata and preserve refusal paths.
- [ ] Run offline/backport checks, then qualify only exact available release pairs
  using named identities and owned fixtures. No APEX/ORDS install/upgrade is implied.
- [ ] Compare schema/code/ORDS catalogs on identified DB/ORDS pairs, including missing
  public columns/privileges, empty captures, paging boundaries and Unicode source.
  Unsupported metadata is unavailable/unsupported, never automatically equal.
- [ ] Record per-pair conclusions and cleanup. Keep unsupported or inaccessible rows
  explicit; downstream upgrades require their own later authorization.

**Acceptance:** compatibility claims are branch/release/pair-specific. If targets
are absent, the deliverable is an accurate blocked qualification matrix.

### Task 14: Qualify shared-app imports and distinct-target promotion

**Files:** Extend `tools/probe_apex_26_2.py` and its existing tests only where
new scenarios require it; create `tools/qualify_distinct_promotion.py` and
`tests/test_qualify_distinct_promotion.py` for explicit source/destination ownership.
Update publish/partial/production qualification docs and manifest.

**Interfaces:** Reports bind both independently observed targets, numeric app IDs,
workspace/parsing-schema mappings, source file-set hashes and lifecycle observations.
An identity tuple alone is not universal clone-uniqueness proof; include verified
local installation ownership to demonstrate actual distinct qualification targets.

- [ ] Obtain exact source and destination profiles, workspaces/schemas, unused fixture
  IDs, existing Builder developer identities and coordination evidence. Refuse missing
  information and preexisting app IDs. Existing DEMO apps are not automatic fixtures.
- [ ] Add RED guards for swapped target, same physical target presented as distinct,
  another operation's locks, missing before-lifecycle observation, changed live source,
  ambiguous import and cleanup inventory mismatch.
- [ ] Use owned disposable apps first to exercise full publish, guarded selected pages,
  automation status transitions, workflow/task observations, exact source re-export,
  and negative post-read/lifecycle scenarios. Confirm baseline/lock retention on failure.
- [ ] Execute promotion through the actual deploy/rollout wrappers with displayed
  confirmation on an authorized distinct staging-like target; manual mode must make
  no DB calls. Do not infer production readiness from DEV-to-the-same-schema comparison.
- [ ] If an existing shared app is specifically selected, coordinate saved/unsaved
  Builder work and follow the exact native lock/page policy. Otherwise mark shared
  existing-app coordination/runtime as not-run even if disposable imports pass.
- [ ] Independently prove owned source/destination app/session/lock/workflow/task cleanup.
  Retain ambiguous writes for inspection rather than claiming cleanup or restoring DB state.

**Acceptance:** app import, selected-page policy, promotion and shared-human workflow
have distinct evidence rows. One passed scenario does not qualify all environments.

**Current status:** not-run; the available local target is one Docker DEV database,
with no identified distinct promotion target or selected shared-app coordination
window.

**Current status:** not-run; the available local target is one Docker DEV database,
with no identified distinct promotion target or selected shared-app coordination
window.

### Task 15: Design and optionally implement qualified tolerant profiles

**Files:** Conditional `scripts/apexlang_profiles.py`,
`tests/test_apexlang_profiles.py`, `tests/fixtures/apexlang_profiles/<qualified-pair>/`;
provenance/verifier integration and publish/partial/runbook documentation.
PR #20's `apexlang_normalization.py` is research input, not the acceptance authority.

**Interfaces:** If explicitly approved, `select_profile(provenance: SourceProvenance,
target_release: str, profile_id: str) -> QualifiedProfile | None` and
`compare_with_profile(expected: bytes, observed: bytes, profile: QualifiedProfile,
*, relative_path: str) -> ProfileComparison`.
The relative path makes component/path scope explicit instead of guessed from bytes.
`QualifiedProfile(profile_id: str, source_release: str, target_release: str,
component_types: tuple[str, ...], evidence_sha256: tuple[str, ...], implementation_version: int)`
is an immutable approved profile. `ProfileComparison(matches: bool,
changed_files: tuple[str, ...], reason: str | None)` is diagnostic output;
it contains relative paths only. Selection is explicit opt-in; unknown/stale
provenance refuses profile use and keeps strict default behavior.

- [ ] Collect authentic reviewed import/re-export examples for an exact available
  source/target pair, preserving raw evidence privately. Classify every difference;
  a downstream 26.1 anecdote cannot qualify 26.2.
- [ ] Produce a separate acceptance-policy proposal and get explicit approval before
  enabling any projection that can turn a byte mismatch into success.
- [ ] Add negative cases for whitespace in literal/fenced SQL/JavaScript, comment-looking
  literal lines, Unicode and CRLF values, significant column/displayColumn sequence,
  duplicate text-message keys/languages, nested/quoted/escaped declarations, missing
  files, unsupported components, stale provenance and unqualified pair.
  All semantic mutations must fail even when the profile is selected.
- [ ] Implement component-aware bounded projections only for proven serializer behavior;
  ambiguous parsing refuses. Do not use blanket rstrip/splitlines/comment stripping/sorting.
- [ ] Run profile/verifier/publish/partial tests and a new native import/re-export on
  the exact pair. Keep lifecycle/identity/revision/file-set checks independent.
- [x] Record a deferred verdict in `docs/superpowers/reviews/2026-10-12-conditional-feature-verdicts.md`.
  Preserve strict default and explain unsupported transformations; no automatic
  profile choice from version guesses.

**Acceptance:** conditional work may conclude with no tolerant profile shipped.
Evidence and policy approval precede any relaxation; stable normalized exports alone
are insufficient.

### Task 16: Evaluate custom-function allowance through a security design

**Files:** Create a dated security design/result under `docs/superpowers/specs/`
and `reviews/`; do not modify `SAFE_FUNCTIONS` or query acceptance in this task.

**Interfaces:** Verdict is `deferred`, `rejected`, or `eligible-for-separate-plan`,
with exact audited owner/name/edition/source digest, dependencies and invalidation
conditions if eligibility can actually be established.

- [x] Identify a concrete requested function/use case; none was supplied, so the
  item is recorded as deferred in `docs/superpowers/specs/2026-10-12-check-function-allowance-security-verdict.md`.
- [ ] Trace package initialization, definer/invoker rights, dynamic SQL, dependency
  changes, autonomous transactions, DDL, remote calls, external/network/file effects,
  edition changes and source mutability. A developer assertion of purity is not evidence.
- [ ] Use only read-only analysis or explicitly authorized tagged malicious fixtures;
  demonstrate what commit guards do and do not prevent, including irreversible
  effects without commit. Preserve fixture cleanup and private source boundaries.
- [ ] Write the threat model and rejection conditions. If independent purity cannot
  be established, retain the builtin-only contract and close the research task as rejected.
- [ ] If eligible, create a separately reviewed design/plan with digest/dependency
  invalidation and bounded capability scope; never implement a simple editable name list.

**Acceptance:** this item is accounted for even when the final decision is to defer
or reject. It cannot block safe implementation of the other features.

### Task 17: Review and integrate each completed slice; finish branch/tracker cleanup

**Files:** Task-specific explicit source/docs/test paths; reviewed sanitized result
summaries and TODO history. No protected local files or full logs are staged.

**Interfaces:** Integration record names candidate SHA, base SHA, test/CI result,
native/platform gate statuses, PR URL and final upstream main SHA. PR #20 title/body
describes its final corrected scope; automation and other independent slices use
focused PRs or another explicitly authorized integration route.

- [ ] Before each slice, refresh remote state and check for concurrent local changes.
  Create/switch a named execution branch only after the plan is accepted; do not edit
  `main` directly for product work. Re-review any newly merged external behavior.
- [ ] Perform inline source/spec self-review, verify every Review Focus case has its
  named regression, and check exact file/descriptor/target semantics. No subagents
  are dispatched while inline-only remains the user's preference.
- [ ] Run the common checks on the final candidate. Separate native, Windows and
  conditional gates; unqualified optional features remain excluded or explicitly
  unavailable instead of lowering tests. Review protected-file staging exclusions.
- [ ] With current-task commit authority, make focused explicit-path commits. For
  PR #20, add reconciliation commits preserving the original head as ancestry;
  with push authority, update its remote normally. Do not force-push or silently
  merge the old unqualified implementation.
- [ ] With merge authority, refresh main, resolve reviewed conflicts, inspect both
  conflict and nonconflict changes, merge the approved candidate, verify the resulting
  tree, then push only the authorized ref. Verify remote main points to the result.
- [ ] If PR #20 is superseded instead of reworked, obtain issue/PR-mutation authority,
  link replacement PRs/results and close it explicitly; do not leave two contradictory
  merge candidates unexplained.
- [ ] Update TODO/history and handoff status for each merged item, issue #19 disposition,
  remaining blocked targets/policies and qualification evidence paths. Read GitHub
  state back after authorized updates. Leave unresolved qualifications visible.
- [ ] Delete obsolete branches only if separately requested and their unique commits
  are accounted for. Local `codex/deployment-review-and-completion` already has no
  unique commits; its removal is optional housekeeping, not missing implementation.
- [ ] Apply downstream `upgrade-template` only in a separately selected/authorized
  downstream project after reviewing protected/customized files. Never assume shared
  DEV implies shared Git history or automatically apply another developer's changes.

**Acceptance:** each merged slice is traceable to tested behavior and scoped evidence;
main/remote parity is verified, and no old draft status is mistaken for remaining code.

## Completion checklist and progress record

- [ ] Tasks 1–11: implemented/reviewed or explicitly scoped with a concrete blocker.
- [ ] PR #20: corrected and integrated, or explicitly superseded with links; no silent policy change.
- [x] Issue #19: native result recorded and issue closed as completed with sanitized evidence.
- [x] Tasks 12–14: per-platform/release/app/target outcomes recorded; absent targets remain unknown.
- [x] Tasks 15–16: explicit deferred verdicts are recorded; strict defaults remain in force.
- [ ] Integration: selected refs pushed only when authorized; protected local files unchanged.
- [x] Downstream adoption remains separately authorized; no automatic upgrades were run.

Track execution in a dated ignored progress file with task ID, branch/commit, exact
files, RED/GREEN evidence, native identity, remaining uncertainty, review disposition,
and integration status. Keep this plan's checkboxes truthful; a drafted task is not done.
No fixed time estimate is promised because platform availability and native policy
gates are not yet established. The critical path is 3 → 6 → 8/9 → 14 → optional 15;
4 → 5 → 10 is an independently reviewable safety/metrics path.
