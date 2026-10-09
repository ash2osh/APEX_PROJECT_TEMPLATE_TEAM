# Template Database Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` for inline execution, or `subagent-driven-development` only if the developer explicitly chooses delegation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Support independent migration owners and verify application runtime state alongside imported source.

**Architecture:** Extend the current environment/target contract with optional migration-only triples. Capture read-only lifecycle observations through the existing SQLcl bridge, then add a second publish gate before baseline advancement and lock release.

**Tech Stack:** Python 3.10+, unittest, Bash, PowerShell, SQLcl, public release-qualified APEX views/session context.

**Spec:** [Local improvements design](../specs/2026-10-09-local-template-improvements-design.md), Release B.

## Global Constraints

- APEX 26.2 operations require SQLcl 26.3.0.0 or newer; 26.1 stays on `codex/apex-26.1`.
- No custom database metadata tables, mutexes, rosters or migration ledgers.
- Keep LF APEXlang/SQL, numeric app IDs and explicit workspace/parsing-schema mappings.
- Native application locks are not a process mutex; SQLcl imports can bypass them.
- Builder creates page locks; retain the existing qualified partial-publish coordination policy.
- Planning does not authorize implementation or database qualification writes.
- No silent commits/pushes, team messages, imports, exports or migrations.

## Review Focus

- Partial migration triples or mismatched list positions must refuse before connecting (Task 1).
- `--schema` narrowing must preserve migration profiles even when the code profile has another owner (Task 1).
- Empty lifecycle results must prove authorized application scope rather than masquerading as missing privilege/context (Task 2).
- Normal workflow completion must not be mistaken for import loss, and new suspension must remain explicit (Task 2).
- Import success/source equality followed by lifecycle uncertainty must preserve the old baseline and recovery evidence (Task 3).

---

### Task 1: Resolve migration targets independently of backup targets

**Files:** Modify `.env.example`, `scripts/load_env.sh`, `scripts/load_env.ps1`,
`scripts/db_targets.py`, `scripts/check_db_target.sh`,
`scripts/check_db_target.ps1`, `scripts/migrate.py`,
`scripts/check_conflicts.py`, `scripts/team.sh`, `scripts/team.ps1`,
`scripts/local_config.py`, `scripts/verify_local.py`,
`tests/test_db_targets.py`, `tests/test_env_schema_lists.py`,
`tests/test_migrate_cli.py`, `tests/test_check_conflicts.py`,
`tests/test_migration_schema_folders.py`, `tests/test_multi_schema_cli.py`,
`tests/test_verify_local.py`, `README.md`, `AGENTS.md`, `CLAUDE.md`,
`docs/migration-rules.md` and `migrations/README.md`.

**Interfaces:** Retain `resolve_target(values, environment, operation,
schema=None) -> Target`. Add
`configured_migration_schemas(values: Mapping[str, str], environment: str) -> tuple[str, ...]`.
For `operation='migration'`, use the complete selected migration triple when
configured, otherwise the original environment profile. Live conflict checks
must use that same resolution. Backup/read target resolution stays unchanged.

- [x] Add regressions for DEV `CUSTDATA/CODE/API` separation, all three environments, absent-profile compatibility, incomplete triples, list mismatch, duplicate schemas, production-looking DEV aliases, folder mismatch and mixed-schema batches. Assert backup/comparison selection is unaffected.
- [x] Add loader/wrapper tests selecting an API migration while `CODE_SCHEMA` lists only a code owner; assert narrowing keeps the migration triple and session-user preflight receives its expected user.
- [x] Run `python3 -m unittest discover -s tests -p 'test_db_targets.py' -v`; expect the independent-profile tests to fail before implementation.
- [x] Implement the nine exact keys from the design in both loaders and the local parser; preserve unfiltered migration schema lists for folder-layout validation. Extend target and preflight selection without relaxing identity/production guards.
- [x] Add optional DEV migration identities to `doctor` and offline validation, deduplicating identical connection/user/schema probes. Keep explicit staging/production identity selection.
- [x] Run the target, loader, migration/conflict, multi-schema and readiness regressions; expect profile parity, pre-write refusals and legacy compatibility.
- [x] Update examples using generic saved aliases, not Natrec credentials; document optional all-or-none profiles and fallback behavior.
- [x] Run the full offline suite and prepare the independently reviewable profile change when authorized.

### Task 2: Capture and compare release-qualified lifecycle snapshots

**Files:** Create `scripts/application_lifecycle.py`,
`scripts/application_lifecycle.sql`, `tests/test_application_lifecycle.py`,
`tests/fixtures/lifecycle/` JSON fixtures. Extend `tools/probe_apex_26_2.py`
and `tests/test_apex_26_2_probe.py` only for explicitly selected lifecycle scenarios.

**Interfaces:**
- `capture_lifecycle(target: Target, workspace: str, app_id: int, run_dir: Path) -> dict`: version-1 snapshot containing verified identity/release/context, timestamps, `automations`, `workflows`, `tasks`, and coverage flags.
- `compare_lifecycle(before: Mapping, after: Mapping) -> dict`: `status='pass'|'attention'|'unavailable'`, `changes`, and explicit reasons; validate identical target scope first.
- Workflow/task entries include instance ID, definition ID, state and whether the instance is terminal; automation entries include static ID and status. Do not include task payloads or business data.

- [x] Read the `apex-workflow-lifecycle` and `apex-session-context` skills; inspect public view columns/privileges read-only on the explicitly selected local Docker release before implementing the SQL adapter. Record qualified state labels and coverage tests; do not borrow unverified 26.1 queries.
- [x] Add fixtures for verified empty apps, wrong workspace/app/identity, missing coverage, malformed/duplicate IDs, automation disablement, natural completion, lost nonterminal instances, new workflow/task errors, temporary suspension followed by verified resumption and remaining suspension.
- [x] Run `python3 -m unittest discover -s tests -p 'test_application_lifecycle.py' -v`; expect failure before the helpers exist.
- [x] Implement strict snapshots through `run_sqlcl`, with identity/release/application-visibility sentinels and rollback-only exit. Use public API context only when qualified; refuse incomplete visibility instead of returning empty arrays.
- [x] Implement instance-aware comparison and a bounded second observation for suspension: at most two post-import snapshots, separated by a 5-second poll; after that report attention/unknown rather than indefinitely polling. Classify normal terminal transitions as informational, missing previously nonterminal IDs and new error states as attention. Do not infer causality.
- [x] Run fixture tests and a read-only Docker visibility check when that operation is authorized; expect exact scope and no persistent writes. Empty apps cannot establish positive workflow visibility alone; use an existing suitable fixture or request explicit disposable fixture creation for later qualification.
- [ ] Review snapshots for data minimization and preserve diagnostics under ignored recovery directories; prepare the observation helper change when authorized.

### Task 3: Gate import completion on source and lifecycle verification

**Files:** Modify `scripts/publish_app.sh`, `scripts/publish_app.ps1`,
`scripts/partial_publish.py`, `scripts/deploy.sh`,
`scripts/verify_publish_state.py`, `tests/test_publish_cli.py`,
`tests/test_partial_publish.py`, `tests/test_verify_publish_state.py`,
`tests/test_windows_support.py`, `docs/publish-rules.md`,
`docs/partial-publish.md`, `docs/apex-26.2-qualification.md`, `README.md`,
`AGENTS.md`, `CLAUDE.md`; extend the existing disposable probe/tests.

**Interfaces:** Consume Task 2's capture/compare functions. Add
`require_verified_lifecycle(report: Mapping) -> None` in `application_lifecycle.py`;
raise `ValueError` on attention/unavailable. Source byte verification remains
independent; every recovery summary includes `sourceVerified` and
`lifecycleStatus`. No lifecycle failure records a success baseline.

- [x] Add full/partial/promotion tests for: pre-read unavailable means no import; normal lifecycle means existing source success; changed automation/new persistent suspension/missing instance means exit 2; post-read failure retains original DEV baseline/lock and evidence; natural completion permits success; selected-page no-notice rules remain unchanged.
- [x] Run publish and partial-publish test modules; expect lifecycle gate regressions to fail before hooks exist.
- [x] Capture pre-import lifecycle under the current DEV native-lock workflow before the final drift check/import. Capture post-import lifecycle after exact source verification and before lock release/baseline advancement; connect partial mode to the same gate. Stage/prod imports use their exact descriptor target and existing confirmation without claiming DEV locks.
- [x] Preserve actionable recovery evidence after ambiguous writes and distinguish source equality from runtime readiness in messages. Never automatically restore automation states or resume workflows.
- [x] Run wrapper tests, source-verification tests and the full offline suite; expect existing drift/lock/partial guarantees plus lifecycle refusals.
- [x] With explicit live-write authorization, qualify full and selected-page import on a disposable Docker app with an automation and workflow/task fixture. Record before/after states and cleanup only run-created fixtures. Test unavailable post-read through controlled failure injection; never target Natrec.
- [ ] Document actual release behavior, visibility limits, recovery actions and independent-source verification; prepare Release B integration when authorized.
