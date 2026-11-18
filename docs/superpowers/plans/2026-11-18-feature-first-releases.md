# Feature-first database releases implementation plan

> **For implementers:** Use the [design](../specs/2026-11-18-feature-first-releases-design.md) and implement the tasks in order. Ship a task only after its focused checks pass. Use the repository's branch and review process for implementation.

**Goal** Apply stored code from its canonical feature files on DEV and promote one final definition per object, together with required ordered migrations.

**Architecture** Add a selected-source DEV route in the feature CLI. Share the existing checkpoint machinery for source parsing, target checks, SQL delivery, and verification. Keep DEV plans and attempts separate from release capsules and their staging proof.

**Stack** Python 3.10+ standard library, unittest, SQLcl, Bash and PowerShell.

## Constraints

- Keep UTF-8 and LF. Use the existing release and SQLcl version checks for
  each template line. The 26.2 template requires SQLcl 26.3.0.0 or newer for
  APEX operations. Qualify the 26.1 line using its own manifest.
- For paths in `.core-stamp.json`, edit APEX_TEMPLATE_CORE first and sync the
  resulting commit into both templates. Apply release-specific guidance and
  wrappers in their own repositories. Track new shared engine files in the
  core inventory and the applicable engine digest lists.
- Maintain the existing behavior of dated migrations, version-1 rollouts,
  and version-2 staging/production confirmation, recovery and proof. Use the
  current engine-digest rules for previously frozen capsules.
  Before syncing an engine change, inventory prepared or staged capsules.
  Finish promotion in the retained checkout with the matching engine and
  original evidence. When that is unavailable, review a new release and
  obtain fresh staging proof. Preserve verified earlier effects in any
  continuation. Keep DEV-only files outside promotion digests unless the
  promotion engine actually uses them.
- Require authorization and coordination for shared DEV writes. Use named
  saved profiles, current target identity checks, and private local evidence.
  Use disposable targets for authorized live qualification. Keep archive
  bytes and downstream history intact.

## Review focus

- A DEV body changed after capture. Task 1 requires exact drift evidence
  before a new apply plan can proceed.
- A spec change leaves out its body. Task 1 distinguishes this catalog
  refusal from malformed inputs that never start SQLcl.
- Two processes execute the same plan, or the first result is unknown.
  Task 2 covers the writer lease, single-use plans and same-target recovery.
- A long view changes after prepare. Task 3 proves exact in-session
  protection and independence from earlier one-time DDL.
- A legacy revision contains an intermediate state that a backfill needs.
  Task 0 specifies the supported transition. Task 4 verifies its writes.

## Task 0 — Pin the release order and the legacy transition

**Files** The shared engine check is in `scripts/feature_release.py`. The
fixtures are in `tests/test_feature_prepare.py` and `tests/test_release_capsule.py`.
The operator examples are in `docs/feature-releases.md`. This task uses the
current capsule manifest.

- [x] Add `test_final_body_guard_refuses_after_unmodeled_migration_replacement`.
  Prepare against a body at version 1, simulate a verified migrate step
  that installs version 2, then run the version 3 checkpoint. Assert the
  existing prepared source guard refuses before that checkpoint writes.
  Preserve the earlier migration receipt. This reproduces the current
  behavior for a prepared plan whose before-state changes.
- [x] Add a read-only prepare refusal when the last directly parsed
  replacement for the same resolved owner, type and name in a preceding
  migration proves a conflicting before-source for a later feature step.
  Compare source through the existing literal-preserving comparison.
  Exercise v1 → v2 before v3, a different owner, and an equal replacement.
  Flag preceding opaque SQL when its effects are uncertain. Keep generic DDL simulation outside
  this task and preserve apply-session target checks.
- [x] Document two safe selections. For the usual case, select the final
  feature once and order any dependent backfill after it. Add a fixture
  showing that a declared precondition false at prepare is refused even
  when an earlier feature would make it true. Check the required installed
  version in the executable migration before the backfill instead. With a
  genuine intermediate-code dependency, use one reviewed migration
  with separately ordered steps for intermediate definition, source and
  validity checks before data, backfill, then final definition. Make the
  in-session assertion raise before the backfill if source, settings or
  validity differ. Check direct source equality at the release commit,
  or the verified binding to an existing trusted delivery wrapper when
  needed. Select that
  migration as the only writer of that unit in the capsule. Verify final
  source/settings/validity and data. Ship an unmodeled earlier prerequisite
  in its own verified release when the dependent checkpoint needs it
  during preparation. In the operator example, qualify the transition
  from the actual before-state and use a disposable copy if DEV already
  has the final source.
- [x] Test the resulting capsule selections offline. Assert the usual
  case contains one feature definition plus the one-time step. Assert
  the intermediate case contains the reviewed dated transition and
  leaves the same unit out of a second feature checkpoint. Verify retained
  archived revisions and receipts by hashes. Keep the `new-migration`
  split-profile generator issue in its existing separate backlog. A
  reviewed transition uses the configured migration target explicitly.
- [x] Run `PYTHONPATH=tests python3 -m unittest test_release_capsule test_rollout`.
  On disposable snapshots with the core changes overlaid, the focused
  feature and rollout suite passed 268 tests on 26.2 and 263 on 26.1.
  Full offline suites in detached worktrees reported zero failures or errors
  across 3,133 tests on 26.2 (67 skipped) and 2,747 on 26.1 (69 skipped).

## Task 1 — Validate and prepare selected DEV sources

**Files** Add `scripts/feature_dev.py` and `tests/test_feature_dev.py`. Integrate
with `scripts/feature_cli.py`, `scripts/feature_manifest.py`,
`scripts/feature_claims.py`, `scripts/feature_release.py`,
`scripts/feature_sources.py`, and `scripts/db_targets.py` as needed for reuse.

- [ ] Add `test_prepare_refuses_invalid_selection_before_sqlcl` for input
  and schema escapes, duplicate ownership and unsupported SQL. Test a
  quoted unit with a hashed file stem loads successfully. Add
  `test_prepare_refuses_changed_spec_without_live_body_selection` with
  a catalog that shows the body exists. Assert zero payload writes.
- [ ] Implement preparation as `prepare_dev_apply(repo_root, feature, schema,
  selectors, *, environ, check_paths=(), accepted_baseline_sha256=None) -> Path`. Resolve scoped claims,
  build validated checkpoint inputs, and reuse selected state capture and
  guard generation. Keep the existing staging/production guards on the
  public promotion entry points. Permit existing scoped check files when a
  feature-wide check assumes prerequisites outside this DEV selection.
- [ ] Test exact selected-source baseline and before-state with fake catalogs.
  Cover a lost DEV edit, formatting that would change a literal, missing
  or incomplete settings for a new unit, disabled trigger replacement,
  and a no-op candidate. Test same physical target through two intended
  logins and conflicting container, owner, edition, or profile. Test a
  configured migration profile that omits the code owner's schema. Test that
  onboarding an existing unit without retained evidence requires a fresh
  matching digest and that adoption preserves feature bytes. Refuse adoption
  of invalid objects and pending or unreadable target evidence. Preparation
  opens no write session. Pin exit 0 for preparation, 1 for a verified
  pre-write check refusal, and 2 for invalid input or unavailable evidence.
- [ ] Expose `feature apply ... --prepare --unit TYPE:NAME` with repeatable
  `--checks PATH` and `--accept-live-baseline SHA256` as prepare-only options.
  Emit the private plan plus a readable diff. Bind identity, selected source
  and check hashes, generated drivers, and before-state. Keep a DEV-only
  lazy import from the CLI so later changes to the DEV module leave the
  promotion digest intact. Test bad and changed
  local paths, and record the effective project env file without dumping it.
- [ ] Run `PYTHONPATH=tests python3 -m unittest test_feature_dev test_feature_cli
  test_feature_preflight test_feature_sources test_db_targets`. Confirm all
  new cases pass and previous feature behavior still passes.

## Task 2 — Execute and reconcile a prepared DEV apply

**Files** Extend `scripts/feature_dev.py` and its tests. Reuse
`scripts/feature_release.py`, `scripts/release_ownership.py`,
`scripts/release_state.py`, `scripts/release_recovery.py`, `.gitignore`,
`scripts/migration_checks.py`, and `scripts/sqlcl_session.py`.
Update `scripts/feature_cli.py` and the help/dispatch in `scripts/team.sh`
and `scripts/team.ps1`.

- [ ] Test `--execute-plan FILE` refusing changed source, checks, plan,
  generated driver, target config, observed target, and before-state
  before any SQL payload marker. Test the writer lease with two processes
  using the same plan and with another plan for the same physical target.
- [ ] Implement `execute_dev_apply(repo_root, feature, schema, plan_path,
  *, environ) -> int`. Treat `--execute-plan FILE` as the explicit write
  action, print the selected target and units, and require no second prompt.
  Verify the plan inside the lease, persist the pending marker under
  `releases/.dev-apply-attempts/`, then use the shared session driver and in-session identity and
  precondition checks. Refactor the session assembly to accept validated
  staged drivers so both capsule and DEV paths use it. DDL application
  keeps existing spec/body ordering and compiler/trigger checks.
- [ ] Test fresh postcondition and source/settings/validity verification.
  Require matching read-back for success. Cover a no-op, failure after the
  first payload marker, failed verification, timeout, process loss, and
  receipt-write failure. Persist the consumed plan ID and digest for a
  verified no-op as well as for a verified write. Repeated execution
  refuses locally before a connection, and two concurrent executions
  recheck consumption under the writer lease. Keep a pending
  attempt when the outcome cannot be proved. Test the spec's exit codes,
  including exit 130 only with proof of no write and exit 2 when a payload
  might have started. Preserve successful receipts on local baseline-refresh
  attention. Test interrupted receipt finalization.
- [ ] Add offline `feature apply <slug> --schema NAME --env dev --status`
  to list all local attempts in the schema and display their target scopes,
  run IDs and missing evidence. Give the wrappers an offline status route.
  Test removing scratch after a pending write. The private marker remains,
  status exits 1 and names the missing evidence, and a new apply still
  refuses. Test unreadable or malformed markers returning 2, and valid
  markers in independent scopes.
- [ ] Add `feature reconcile-run <run-id> --env dev --evidence <file>`.
  Validate the private attempt, acquire the lease, and use reviewed
  same-target read-only evidence for the before or intended state.
  Add tests for wrong-target evidence, unverifiable state, normal
  acknowledgment and recorded `--yes`. Require `--evidence` on DEV and
  preserve the staging/production parser and rollout state route. A fresh
  plan is required after reconciliation.
- [ ] Refresh retained DEV baselines only after verification. Keep the
  pre-apply diff. Test that feature files and later working-tree edits
  survive the refresh. Report local refresh failure separately from the
  verified database result.
- [ ] Retain before-source, settings and dependency context for an operator
  to review a restore candidate. In a fake invalid-body case, assert the
  command reports the affected object, preserves its original evidence,
  and blocks further feature applies. Document separately authorized
  same-target repair and verification before reconciliation. Leave actual
  repair under the project's existing approved delivery route. Document
  the lease boundary for standalone migration and independent checkouts.
- [ ] Run `PYTHONPATH=tests python3 -m unittest test_feature_dev test_feature_cli
  test_release_state test_rollout test_release_capsule test_migrate_cli`.
  Exercise the feature CLI from both wrappers with fake SQLcl, including
  a DEV-only target refusal, an alternate `PROJECT_ENV_FILE`, and missing
  profile refusals for prepare, execute and DEV reconciliation. The DEV
  reconciliation path loads the selected dotenv file. Keep unresolved
  attempts visible in help and error messages.

## Task 3 — Add supported view selection

**Files** Extend `scripts/feature_dev.py`, `scripts/feature_manifest.py`,
`scripts/feature_preflight.py`, and use the existing view capture in
`scripts/compare_env_catalog.sql`. Extend `tests/test_feature_dev.py` and
`tests/test_feature_preflight.py`.

- [ ] Add `test_view_apply_accepts_one_dedicated_definition` using a
  `views/customer_lookup.sql` file alongside separate one-time DDL.
  Assert the staged payload contains only the selected view and mixed
  files refuse DEV apply. Test duplicate identity, unsupported
  dependencies, incomplete catalog text, a literal-only change, and
  mutation of the view file after preparation.
- [ ] Validate one supported `CREATE OR REPLACE VIEW` statement. Reuse
  structural and dependency checks plus the `baseline-views` LONG-to-CLOB
  capture in `scripts/compare_env_catalog.sql` for review. Make
  `add_baseline_views` handle `c_baseline_names_json` with exact matches and
  retain the existing prefix behavior when the selection is absent. Use
  the full capture for read-back. Require exact before-state protection in
  the write session.
  Add `test_view_apply_refuses_late_definition_drift_in_write_session`
  and verify reconciled view definition and validity using the same
  comparison as apply. Qualify exact selected-name capture and writer
  checks with a view definition over 32 KB and supplementary Unicode.
  Test unchanged query text with a changed column alias or read/check
  property. Require a refusal for definition drift or incomplete proof.
- [ ] Keep complete legacy `ddl.sql` files valid for capsule promotion.
  Add a fixture where a later capsule selects just the dedicated view.
  Assert it retains the changed definition and does not select or replay
  the earlier column change and grant.
- [ ] Run `PYTHONPATH=tests python3 -m unittest test_feature_dev
  test_feature_preflight test_feature_manifest test_release_capsule`.
  Treat the catalog helper and any hashed CLI/shared-driver edit as an
  engine change. Apply the in-flight capsule upgrade procedure before
  distributing each changed engine.

## Task 4 — Prove consolidation, recovery, and delivery cost

**Files** Extend `tests/test_feature_convert.py`,
`tests/test_release_capsule.py`, and `tests/test_live_feature_release.py`.
Update `docs/feature-releases.md`, `docs/feature-ownership.md`,
`docs/live-feature-tests.md`, `docs/production-deployment.md`,
`README.md`, `AGENTS.md`, and `.agents/rules/agent-safety.md` when the
corresponding command is ready.

- [ ] Extend Task 0's fixture with three immutable historical versions
  and the normal archive and evidence workflow. Use `feature convert-plan`
  for active folders and verify archived bytes and lookup separately.
  Execute the normal capsule and the bounded-transition capsule with
  fake SQLcl and in opt-in disposable staging qualification. Count body
  writes and compare with the three legacy writes. Require one final
  body write plus the backfill in the normal case, and two body writes
  around one backfill in the bounded transition. Assert the intermediate
  source and validity guard runs before data changes and the final
  source and data both verify. Keep the paired fixtures' starting
  state and verifiable end effects equivalent.
- [ ] Build an opt-in live scenario on approved disposable targets. Apply
  the feature working-tree source to DEV and verify it. Commit the
  fixture inputs, freeze once, then prepare and execute staging. Confirm
  the DEV receipt's authored source digest matches the capsule input,
  DEV and staging catalog read-backs independently verify text, settings
  and validity, and production-proof validation accepts only
  the matching verified staging report. Use a third disposable target for
  an authorized execution of the production path. Exercise a changed
  target and an ambiguous failure as refusals.
- [ ] Compare batched legacy `migrate` against the capsule on equivalent
  reset disposable targets. Verify the same final effects with comparable
  connection setup. Run repeated trials and record the sample count,
  median and observations. Count selected steps, unit executions, SQLcl
  sessions and emitted SQL/check bytes. Record connection startup, catalog
  preparation, apply and verification separately, using existing timings
  where they measure the same interval. Keep preparation and the object
  budget visible alongside total release time.
- [ ] Document the supported DEV commands, explicit execution and recovery acknowledgment.
  Include a short author → DEV → freeze → staging → production example,
  the split-owner case, and the recovery route. Update operational guidance
  when Task 2 ships for stored units, and include views after Task 3. Add
  the in-flight capsule upgrade rule to the release notes and guide.
  Use generic names in template fixtures and docs. Keep the downstream
  database and archived artifacts outside the qualification fixture.
- [ ] Run focused offline regressions, the changed-file pre-push check,
  shared core consistency checks, and the template parity report. Run
  authorized disposable qualification before advertising live correctness
  or a staging speedup. Record environment versions, skips, and evidence
  limitations in the implementation report.

## Implementation order

Task 0 pins the release selection. Tasks 1 and 2 deliver DEV apply for stored
units. Task 3 adds supported views. Task 4 qualifies the complete workflow
and publishes the end-to-end operator example. The
direct-migration replay fix and generated-baseline safety work remain tracked
in the [review inventory](../../../TODO.md#database-workflow-inventory--2026-11-18).
