# Migration Improvements for APEX 26.1 and 26.2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. Recommended execution is inline, sequentially in this checkout, with review at the phase boundaries.

**Goal:** Deliver clearer migration grouping, a read-only migration report and optional generated baseline bundles on both release branches.

**Architecture:** Two independently testable feature plans share existing migration loaders and evidence checks. The 26.1 track receives only the required database prerequisites; APEX source/import behavior remains release-specific. Generated structure/grants/code can be bundled per schema, while reference data keeps its own verification/rehearsal boundary.

**Tech Stack:** Python 3.10 standard library, SQLcl 26.3.0.0+, Bash, PowerShell, unittest and GitHub Actions.

**Spec:** [Migration planning and bundling design](../specs/2026-10-13-migration-planning-and-bundling-design.md).

## Global Constraints

- Python 3.10 or newer; standard library only for the daily workflow.
- New live migration-report and baseline workflows require SQLcl 26.3.0.0 or newer; offline reporting needs no SQLcl.
- Preserve APEX 26.1 application/source/deployment behavior on codex/apex-26.1 and APEX 26.2 behavior on main.
- One migration folder changes one schema; cross-schema work uses separate folders and an explicitly ordered release.
- Once any database write may have begun, SQL/check bytes and migration identity remain immutable. Unknown evidence refuses mutation; successful receipts are never overwritten.
- No shared migration ledger, database mutex, team table or cross-repository pending-work discovery.
- Keep Bash and PowerShell public arguments, output schema and exit codes equivalent; preserve existing Windows fixes.
- Preserve project-owned files and existing attempted/applied migration folders; no automatic squashing, deleting, renaming or archiving.

The complete constraints and output contracts are in the spec. Creation of this plan authorizes planning only. Each implementation phase is carried out after plan review; commit, PR and push instructions require authorization covering this work. Do not carry the earlier remediation's commit/push instruction into this new scope automatically.

## Review Focus

1. The 26.1 branch has no baseline subsystem: prerequisite ports are required and must be qualified independently.
2. The shared checkout contains unrelated untracked work: never stage all files or reset the checkout to switch tracks.
3. A folder count is not a readiness or risk score: the report must retain unknown evidence and deferred live checks.
4. Bundling shifts checks to one folder boundary: only eligible generated components can share it; data stays separate.
5. Passing main CI does not prove 26.1 or Windows parity: verify the exact tips and branch-specific suites separately.

These review cases have owning tests in the component plans; final phase verifies their combined behavior.

---

## Branch baselines and implementation tracks

| Track | Recorded source tip, fetched 2026-10-13 | Proposed implementation branch | Integration target |
| --- | --- | --- | --- |
| APEX 26.2 | 6770f5523bf687788b639f20f8d1ab9ad21799ed | codex/migration-improvements-26.2 | main |
| APEX 26.1 | 0bb72a356aa8b6cb6242e4e20b4653801037679a | codex/migration-improvements-26.1 | codex/apex-26.1 |

Re-fetch and reread the target branch contract at execution start; these hashes are planning evidence, not a permanent dependency pin. If either tip changes, review the intervening relevant files and adapt the plan before editing. No worktree is needed under the project's execution skill; execute one branch at a time.

New planning documents belong to templateOnly via docs/superpowers/**. Shipped migration-planning/baseline docs must be added to templateOwned on both branches. Preserve the 26.1 manifest's existing format and branch-specific qualification entry.

## Component plans

1. [Migration inventory, local counts, explicit live preflight and grouping guidance](2026-10-13-migration-plan-report.md).
2. [Database-only 26.1 baseline prerequisites and optional baseline bundling](2026-10-13-baseline-bundling.md).

The report plan provides durable 26.1 evidence and the shared SQLcl runtime guard used by the bundling plan. The baseline plan does not require the report's renderer or JSON output for generation.

### Task 1: Establish the execution baseline

**Files:** read AGENTS.md, AGENTS.project.md, .agents/rules/project.md, both component plans and the spec; maintain this plan's results section during execution.

**Interfaces:** preserve the two branch tracks and record source-tip to implementation-tip/commit mapping. No product behavior is changed in this task.

- [ ] Check the native Oracle skills registry/catalog/installation freshness under the task-start contract. If refresh is due, run the authorized SQLcl skills sync, verify native dates/files and report its actual outcome.
- [ ] Inspect git status and the upstream tips. Preserve .env, apps/DEMO, database/DEMO, existing remediation documents and uv.lock. Resolve checkout conflicts without deleting or committing unrelated work.
- [ ] After implementation authorization, create the named 26.2 feature branch from the verified current main tip. Read branch-specific instructions before editing. Do not start concurrent branch-switching agents in this shared checkout.
- [ ] Record the source tips, available Bash/PowerShell/Python versions and actual working tree baseline below. Do not connect to Oracle as part of startup planning.

**Done:** a reproducible source/branch baseline exists and unrelated files are preserved.

### Task 2: Deliver the report on both branches

**Files:** those named in the report component plan; maintain the result/port map below.

**Interfaces:** identical migration-plan CLI and schemaVersion 1 report on both tracks.

- [ ] Implement report-plan Tasks 2 through 4 on 26.2. Task 1 is not needed there because durable events already exist.
- [ ] At an authorized review/commit checkpoint, switch safely to the named 26.1 feature branch created from its current remote tip. Do not assume the local maintenance branch matches the remote tip.
- [ ] Implement report-plan Task 1 on 26.1, then transfer only reviewed version-neutral report/runtime changes. Adapt router/docs hunks to that branch instead of merging main.
- [ ] Run the report plan's focused tests on both exact feature tips and record their results and platform skips. A single green track cannot complete this task.
- [ ] Review grouping guidance and the offline/live report against the spec before beginning bundling.

**Done:** both branches expose the common report without baseline-generator dependency.

### Task 3: Deliver optional bundling on both branches

**Files:** those named in the bundling component plan; maintain the result/port map below.

**Interfaces:** identical --bundle behavior, schema-separated generated folders and unchanged default granular output.

- [ ] On 26.2 implement baseline-plan Tasks 3 and 4 using its existing generator and the common runtime guard.
- [ ] On 26.1 implement baseline-plan Tasks 1 and 2 as independently reviewed prerequisites, then transfer Tasks 3 and 4 using selective version-neutral commits.
- [ ] Execute the shared fixtures on both tracks: three eligible categories become one, --data yields a separate data folder, no changes produce no empty bundle, and identical reruns create no additional revision.
- [ ] Review the target/schema boundaries, unchanged default outputs, durable evidence and filesystem-failure behavior. Record actual code differences between tracks; do not claim identical live qualification.

**Done:** both branches have the requested complete workflow; 26.1 prerequisite work is included in the delivery, not left as a hidden follow-up.

### Task 4: Qualify each branch and its CI separately

**Files:** modify .github/workflows/database-checks.yml on 26.1 to add the current project's Windows offline job adapted to that branch; extend tests/test_windows_support.py and tests/test_documentation_contract.py as needed; create docs/superpowers/plans/results/RESULT_migration-improvements.md during implementation.

**Interfaces:** same public commands/report contract and equivalent generated results across branch/platform matrices. Preserve Linux CI and existing action permissions/pinned revisions. Do not replace 26.1 qualification evidence with 26.2 evidence.

- [ ] Add a meaningful regression that invokes the real PowerShell/Bash routers with fake SQLcl for new offline report and bundle cases. Windows offline CI must execute the new PowerShell path; a skipped local PowerShell run is not platform qualification.
- [ ] Run focused feature and affected integration suites from the component plans. Then, once per completed branch tip, run Python compileall, Ruff, Bash syntax, PowerShell parser checks, the full unittest discovery suite, example configuration validation and existing JSON/LF checks. Repeat broader checks only after new changes or failures warrant it.
- [ ] Run existing branch-specific APEX source/descriptor/publish tests to detect accidental release contamination. Confirm the 26.1 qualification docs and manifest ownership still validate and the 26.2 floor/release contracts remain intact.
- [ ] With authorized push/PR actions, inspect GitHub Actions for each exact pushed SHA on its intended integration target. Compare local/remote SHA before treating CI as current. Fix failures on the failing track, rerun affected tests, and record the resulting tip/run URLs.
- [ ] Write a concise results report with implemented/remaining items, exact branch/SHAs, focused/full/Windows evidence, count-reduction fixtures, preserved default behavior and live qualification marked not performed unless separately authorized.

**Done:** independently verified 26.1 and 26.2 releases are reviewable. Tests/CI do not authorize importing apps, applying migrations, rehearsing DML or merging a PR.

### Task 5: Integrate under the requested Git workflow

**Files:** authorized focused commits/PR descriptions and results report only.

**Interfaces:** main receives the 26.2 implementation; codex/apex-26.1 receives the 26.1 implementation. Integration remains distinct from shared database state.

- [ ] Review each diff against its own base, including new-file ownership and unrelated-file preservation.
- [ ] Commit only explicit intended paths when authorized. Create/push PRs or direct pushes only according to current instructions for these changes; attach any created PRs to the chat.
- [ ] Verify the final remote tips and exact CI outcomes separately. Record integration state as proposed/pushed/open/merged based on actual evidence.

**Done:** both integration outcomes are accurately reported; no promised branch is silently omitted.

## Acceptance checklist

- [ ] Grouping policy and examples available on both branches.
- [ ] Equivalent offline migration-plan commands/counts/diagnostics, no .env/SQLcl/network/write effects.
- [ ] Live report guards, partial/deferred verification and unknown evidence preserved.
- [ ] 26.1 database catalog, durable evidence and granular baseline prerequisites implemented and tested.
- [ ] Both branches support --bundle NAME with validated per-schema output.
- [ ] Fixture reduction: 3 eligible folders to 1; --data: 4 category/data folders to 2.
- [ ] Default granular output, data policies, old migration bytes and existing receipt formats preserved.
- [ ] Focused/full suites and Linux/Windows CI recorded at each exact delivery tip.
- [ ] Release-specific app behavior and unrelated local work preserved.
- [ ] Git integration and any outstanding live qualification reported truthfully.

## Results to record during implementation

| Track | Source tip | Implementation tip / port map | Focused/full tests | Linux/Windows CI | Integration | Live qualification |
| --- | --- | --- | --- | --- | --- | --- |
| 26.2 | 6770f5523bf687788b639f20f8d1ab9ad21799ed at planning | Not implemented | Not run for these features | Not run for these features | Planned | Not performed |
| 26.1 | 0bb72a356aa8b6cb6242e4e20b4653801037679a at planning | Not implemented | Not run for these features | Not run for these features | Planned | Not performed |

## Planning self-review

The plan covers the accepted grouping/report/bundling ideas, the verified branch capability gap, common contracts, explicit dependency order, offline/read-only limits, immutable migration evidence, generated folder safety and branch/platform qualification. Required tests are assigned to the component owning the behavior. No live target name, workspace, receipt, migration reduction measurement or execution result has been invented.
