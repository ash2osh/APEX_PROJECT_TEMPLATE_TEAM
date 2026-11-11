# Migration Capsule Checkpoints Implementation Plan

> **For agentic workers:** One subagent per phase, in the order below. Each phase stops for a main-session review before the next one starts. Do not commit, push, switch branches, open a pull request, connect to a database, run SQLcl, or run any `scripts/team.sh` command that touches a database or uses `--live`. Offline commands and the test suite are fine.

**Goal:** Give dated migration folders the same release guarantees a feature checkpoint has inside a frozen capsule (a prepared target binding, complete coverage, staging proof before production), let a release consist of migrations only, let `feature capture` hold every stored unit type and not only packages, and give projects an offline planner for moving old object migrations into feature files.

**Architecture:** Keep the existing feature/capsule/rollout split and the existing migration engine. A `migrate` step in a schema-version 2 manifest joins the machinery that SQL-script and ORDS steps already use: a prepared target binding in the target plan, the target writer lease, an identity re-observation before the write, and a verified receipt. The migration itself is still applied by `scripts/migrate.py`, which keeps its own live preflight, postcondition verification and receipts. No second migration engine is written.

**Tech stack:** Python 3.10+ standard library, unittest with the existing fake SQLcl fixtures, Bash, PowerShell. No new runtime dependency.

**Branch:** `feat/migration-capsule-checkpoints` in the 26.1 template. The 26.2 template is ported afterwards, as a separate task.

## Global constraints

- Python 3.10+, standard library only, `ruff check scripts tools tests` clean (line length 200), LF line endings.
- Read each file before editing it. Follow the style of the file you are in.
- Write the failing test first where practical. Never weaken an existing safety check or assertion to make something pass. When an existing test pins wording or a shape this plan changes on purpose, update that test and list it in the report.
- Schema-version 1 manifests, `migrate`, `check-conflicts`, receipts, rehearsal and publish keep their behaviour exactly. `scripts/migrate.py` is not changed in phases 1 and 2.
- No database ledger, mutex or metadata table. Local state coordinates this checkout only.
- An unknown or interrupted write never becomes a success. Recovery keeps refusing to replay a writing step that already has a verified receipt.
- `scripts/team.sh` and `scripts/team.ps1` usage text must stay identical apart from the script name; a test compares them.
- Every module that the capsule engine digest covers is listed in `WORKFLOW_ENGINE_FILES` or `ENGINE_HELPER_FILES` in `scripts/release_capsule.py`. Phases 1 and 2 add no new module. If a later phase adds a module that a rollout imports, add it to the right list.
- Do not edit `docs/known-limitations.md` to remove a limitation unless this plan names the row.
- Live behaviour is unproven until the opt-in live test runs against a disposable schema. Add the live scenario where the plan asks for one, but do not run it and do not claim it passed.

## Design decisions (already made; do not reopen)

1. **A migration step is covered when it is prepared and verified.** Covered means: the target plan holds a binding for that step, the step ran under the writer lease against the prepared identity, and `migrate` returned 0 (so its own receipt was written).
2. **Migration time counts toward the database-object budget.** A dated migration is object work. Its elapsed time is added to the same `object_seconds` that feature checkpoints use, and the "stop before the next step when the budget is reached" rule applies to `migrate` steps as well as `feature-migrate` steps. Long data moves belong in a `sql-script` data step, which already has its own timing.
3. **The budget record names the migration steps.** The report's `objectBudget` gains one field, `includedMigrationSteps` (a list of step numbers). `coverageComplete` is true only when every feature checkpoint and every `migrate` step is included and `excludedObjectSteps` is empty.
4. **Prepare runs the existing read-only live preflight** (`migration_checks._live_report`) for the migration folders and stores the outcome in the target plan, so it is part of the plan digest the operator confirms. The apply-time preflight inside `migrate` still runs and still gates the write.
5. **A missing prerequisite created earlier in the same release is deferred, not refused.** See phase 1, task 1.2.
6. **`DROP`, `RENAME` and `TRUNCATE` stay in dated migrations.** They are not added to the feature DDL forms.
7. **The conversion tool is a planner.** It reads and reports. It writes no feature file and archives nothing; the existing `feature capture` and `archive-migrations` commands do the writing.

---

## Phase 1: `migrate` steps as covered capsule checkpoints

Files: `scripts/rollout.py`, `scripts/feature_release.py`, `scripts/release_state.py`, `scripts/release_capsule.py` (only if needed), tests, docs.

### Task 1.1: Resolve the target of a `migrate` step the way `migrate` does

- A `migrate` step has `folders` and no `schema`. Its schema is derived exactly as `scripts/migrate.py` `main` and `migration_checks._live_report` derive it: `load_batch(root, folders)`, then `db_targets.batch_schema([m.schema ...], requested, values, environment)` with `requested = values.get("PROJECT_SCHEMA") or None`, then `resolve_target(values, environment, "migration", schema=chosen)`, then `assert_single_layout(...)`.
- Put this in one helper and use it from prepare, from the binding comparison at execute, and from the state validators, so the three cannot drift.
- `rollout._target_binding` currently returns a binding for `sql-script` and `ords-import` (and `None` otherwise). Extend it to return the same shape for `migrate`, with `logicalSchema` set to the derived schema.

### Task 1.2: Prepare records a binding and a preflight for every `migrate` step

In `feature_release._auxiliary_targets` (or a sibling called from the same place in `prepare_target`):

- For each `migrate` step, record `targets["step:N"]` with the existing eight fields (`environment`, `operation` = `"migration"`, `logicalSchema`, `connection`, `expectedUser`, `schema`, `classification`, `observed`) plus two more, allowed only for `migrate` steps:
  - `folders`: ordered list of `{"path", "payloadDigest", "checksSha256"}` from the loaded migrations.
  - `preflight`: `{"state", "exitCode", "conflicts", "errors", "reportSha256"}` where `reportSha256` is the SHA-256 of the canonical JSON of `PreflightReport.to_dict()`.
- Run the live preflight once per `migrate` step, over that step's folders in order, wrapped the same way `migration_checks.main` wraps it (`_with_applied_source_conflicts`).
- Outcome rules:
  - Exit code 0: `state` is `"clear"`.
  - Otherwise, when every conflict has code `MISSING_PREREQUISITE`, there are no `errors`, and an earlier step in the frozen manifest is a `feature-migrate` or `migrate` step for the same logical schema: `state` is `"deferred"`. The conflicts are recorded so the operator sees them in the plan. The apply-time preflight decides.
  - Anything else: refuse the prepare with a `FeatureReleaseError` that names the step and includes the rendered conflicts and errors. Nothing is written.
- The preflight's observed identity (when present in `coverage["observedIdentity"]`) must equal the binding's `observed` identity for the fields both have; a difference refuses the prepare.
- Bump `PLAN_SCHEMA_VERSION` (3 to 4). `load_target_plan` validates the two new fields strictly: exact key sets, digests are 64 lowercase hex characters, `state` is one of the two values, `folders` equals what the frozen step loads today.

### Task 1.3: Local state accepts the bound `migrate` step

In `scripts/release_state.py`:

- `start_rollout`: `expected_auxiliary` includes `migrate` steps, so the plan must bind every SQL, ORDS and migration step exactly once. The step's target scope joins the rollout's `targetScopes`.
- `_auxiliary_target_scope`: accept the extended field set for `migrate` steps only, and compare `logicalSchema` with the schema derived from the frozen folders (task 1.1) instead of `step["schema"]`.
- `assert_auxiliary_step_allowed`: accept `migrate` as well as `sql-script` and `ords-import`, with the same occupancy and lease checks.
- Reword the error messages that say "SQL/ORDS" to "SQL, ORDS or migration" where they now cover all three. Update tests that pin the old wording.

### Task 1.4: Execute a `migrate` step under the lease, against the prepared identity

In `rollout._run_capsule_rollout`:

- `_validate_capsule_auxiliary_targets` also checks `migrate` steps.
- In the per-step loop, a `migrate` step takes the same path as `sql-script` and `ords-import`: compare the current binding with the prepared one field by field, take `release_ownership.target_writer_lease`, call `release_state.assert_auxiliary_step_allowed`, re-observe the identity with `_observe_target_identity` and require it to equal the prepared `observed`, then call `execute_step`. Do not add `connection`, `expectedUser` or `schema` keys to the step passed to `execute_step` for a migration: `migrate.main` resolves its own target, and the binding comparison has already proven it resolves to the prepared one.
- Before the write, re-load the step's folders and require their `payloadDigest` and `checksSha256` to equal the prepared `folders` list.
- A `migrate` step whose `execute_step` result is 0 is appended to a new `included_migration_steps` list and its elapsed time is added to `object_seconds`. It is no longer appended to `excluded_object_steps`. Set `step_report["objectSeconds"]` and `step_report["phase"] = "migration"`.
- The pre-step budget stop (`object_seconds >= budget`) and the post-step overrun check apply to `migrate` steps as they do to `feature-migrate` steps. Reword the two messages so they say "feature or migration checkpoint".
- `_prepare_plan`: for schema-version 2, a `migrate` step gets `phase = "migration"` and a classification reason that says it runs as a prepared migration checkpoint whose time counts toward the database-object budget. `sql-script` steps classified `uncovered-object` are unchanged and still make coverage incomplete.

### Task 1.5: The budget record and the staging proof

- `_capsule_object_budget` takes the migration list and the number of `migrate` steps in the frozen manifest, and returns the extra field `includedMigrationSteps`. `coverageComplete` follows decision 3. Every literal `objectBudget` dictionary in `rollout.py` (dry run, prepared, running) gains `"includedMigrationSteps": []`.
- `release_state.OBJECT_BUDGET_FIELDS` gains the field. `validate_stage_proof` requires `includedMigrationSteps` to equal the step numbers of the `migrate` steps in the capsule's frozen step list, in order. `_validate_completion_proof` gets the matching check where it compares `includedCheckpoints` and `excludedObjectSteps`.
- `finish_local_run` needs no rule change: it already requires complete, in-budget coverage and a verified receipt for every step.
- The Markdown report (`_markdown_report`) shows the migration steps beside the checkpoints.

### Task 1.6: Recovery stays strict

- No change to `_assert_recovery_will_not_replay_prior_writes`. Add a test that a recovery is refused before confirmation when an earlier `migrate` step has a verified outcome, and one that a parent killed before the `migrate` step ran is still recoverable.

### Task 1.7: Tests

Add to the existing modules (`tests/test_feature_rollout.py`, `tests/test_feature_prepare.py`, `tests/test_release_state.py`, `tests/test_release_promotion.py`, `tests/test_release_completion.py`), using their fixtures:

- Prepare: a clear preflight is recorded; a deferred one is recorded with its conflicts; a conflict that is not a missing prerequisite refuses; a missing prerequisite with no earlier step for that schema refuses; the plan digest changes when the preflight outcome changes.
- Plan loading: a missing, extra or malformed `folders` or `preflight` field is refused; a plan written with schema version 3 is refused.
- Execute: a changed profile, a changed observed identity and a changed folder digest each stop the step before `migrate` is called; a successful step is in `includedMigrationSteps`, its time is in `objectPhaseSeconds`, and coverage is complete; a release that goes over budget on a migration step fails with the database state `verified`.
- Promotion: production refuses a staging report whose `includedMigrationSteps` is missing, empty when the capsule has a migration step, or names a different step.
- A release with one feature checkpoint and one migration step reaches the verified local run state. This is the case that fails today in `finish_local_run`.
- Add one opt-in scenario to `tests/test_live_feature_release.py` (a feature checkpoint that creates a table, then a migration folder that alters it, staging then production with stage proof) and a row for it in `docs/live-feature-tests.md`. Do not run it.

### Task 1.8: Documentation

- `docs/feature-releases.md`: the "Timing, attempts, and cleanup" section, the production paragraph and the recovery paragraph describe migration checkpoints, the deferred preflight and the new budget field. Remove the statement that legacy migrations are always excluded.
- `docs/known-limitations.md`, row "A feature capsule's database-object budget is an overrun check...": drop "legacy database work" from the excluded list; keep the rest.
- `AGENTS.md` (Feature releases paragraph) and `.agents/rules/agent-safety.md`: one sentence each, replacing "excluded legacy object steps".
- `README.md`: the feature release section, briefly.
- `docs/feature-release.example.json`: leave unchanged unless a test requires otherwise.

---

## Phase 2: migration-only capsules

A schema-version 2 release may contain no `feature-migrate` step, as long as it has at least one `migrate` step. This is the direct "capsule for migrations".

- Find every place that requires at least one feature checkpoint and decide each one explicitly. Known places: `release_state._capsule_step_count`, `release_state.start_rollout` (`not checkpoints`), `release_state._capsule_for_cleanup` (`not checkpoints`), `release_state._validate_completion_proof`, `feature_release._manifest_steps`, `feature_cli` freeze (`build_capsule` with an empty checkpoint tuple), `release_capsule._validate_capsule_directory`, and `rollout._run_capsule_rollout` `finish_local_run` (the `checkpointVerifications` range check). Search for others.
- The rule everywhere becomes: at least one feature checkpoint **or** at least one `migrate` step. A capsule with neither is still refused.
- With no feature checkpoint: `includedCheckpoints` is `[]`, `checkpointVerificationIds` is `[]`, the target scopes come from the migration bindings only, and `--recover` is refused with a clear message because there is no checkpoint attempt to recover. An interrupted migration is reconciled through the migration rules (`docs/migration-rules.md`), and the next run uses a new capsule or the same one after reconciliation, as those rules say.
- `feature freeze` builds such a capsule: migration bytes are frozen and hashed as they are today, the freeze gate runs with no selected feature, and the freeze record says so.
- `feature clean-capsule` works for it.
- Tests: freeze, prepare, staging execute, production with stage proof, cleanup eligibility, recovery refusal, and that a manifest with no feature and no migration step is refused at freeze.
- Docs: `docs/feature-releases.md` gets a short "Migration-only releases" section with the commands; `docs/migration-rules.md` points to it.

**Stop rule for this phase:** if making a guard accept zero checkpoints would also let a malformed or truncated capsule pass, keep the guard and report it. A narrower, correct result is better than a complete one.

---

## Phase 3: `feature capture` for every stored unit type

The checkpoint engine already accepts `TRIGGER`, `FUNCTION`, `PROCEDURE`, `TYPE` and `TYPE BODY` as stored-source inputs (`feature_manifest.STORED_SOURCE_TYPES`; a trigger is already exercised in `tests/test_feature_prepare.py`). What is missing is capture, diff, the DEV comparison, and one trigger hazard.

### Task 3.1: Capture

- `feature capture <slug>` takes exactly one of `--package NAME`, `--trigger NAME`, `--procedure NAME`, `--function NAME`, `--type NAME`. `--refresh`, `--move-from`, `--discard-unapplied` and `--keep-file` work for all of them.
- Generalise `scripts/feature_sources.py` around a small "unit group": `package` is (`PACKAGE` required, `PACKAGE BODY` optional), `type` is (`TYPE` required, `TYPE BODY` optional), and `trigger`, `procedure`, `function` are single units.
- File layout under `features/<slug>/<SCHEMA>/`:

  | Group | Directory | Suffix |
  | --- | --- | --- |
  | package | `packages/` | `.pks.sql`, `.pkb.sql` (unchanged) |
  | type | `types/` | `.tps.sql`, `.tpb.sql` |
  | trigger | `triggers/` | `.trg.sql` |
  | procedure | `procedures/` | `.prc.sql` |
  | function | `functions/` | `.fnc.sql` |

- Package behaviour, paths, retained baselines and capture metadata stay byte-identical; every existing package test passes unchanged. Other groups use their own metadata file name (`capture-<group>-<stem>.json`) so a trigger and a package with the same name cannot collide, and their metadata records the group.
- Source text comes from the same `baseline` capture and `new_migration.step_sql` path packages use.
- `feature diff` covers every captured group.
- `team.sh` and `team.ps1`: the `feature capture` usage string names the five options.

### Task 3.2: Doctor and freeze compare every stored unit with DEV

- `feature_doctor.STORED_PACKAGE_KINDS` becomes the full stored-source set; `read_dev_packages` and `live_findings` read and compare all of them. Rename internals where "package" is no longer accurate, and keep the user-facing finding codes.
- The orphan-check and the accepted-differences file already use (schema, type, name) and need no format change.

### Task 3.3: A disabled trigger must not be silently enabled

`CREATE OR REPLACE TRIGGER` creates the trigger enabled. In `feature_release` prepare, when a selected `TRIGGER` unit exists on the target with catalog status `DISABLED` (the `triggers` section's `status` column, not `object_status`) and the unit is not a no-op, refuse the prepare with a message that names the trigger and says to handle it in a reviewed migration. Add tests for disabled (refused), enabled (allowed), absent (allowed) and disabled-but-no-op (allowed).

### Task 3.4: Tests and docs

- Tests in `tests/test_feature_sources.py`, `tests/test_feature_cli.py`, `tests/test_feature_ownership.py`, `tests/test_feature_diffs.py` and `tests/test_feature_prepare.py` for each new group: capture, refresh guard, move, diff, doctor comparison, and a freeze that selects each type.
- One opt-in live scenario (a trigger, a function and a type with a body through staging and production) and its row in `docs/live-feature-tests.md`. Do not run it.
- `docs/feature-releases.md` "Scope and revisit criteria": capture now covers these types; what still belongs in dated migrations is destructive DDL and anything the DDL forms refuse. Update `docs/feature-ownership.md`, `README.md` and the `AGENTS.md` Feature releases paragraph to match.

---

## Phase 4: offline conversion planner

`scripts/team.sh feature convert-plan <migration-folder> [...] [--feature <slug>] [--format text|json]`

- Offline and read-only. It loads the folders with the existing migration loader and analyser and prints, per folder:
  - each stored unit it creates, with the `feature capture` command that would take the current DEV source into the named feature;
  - each DDL statement, marked as accepted by the feature DDL forms (`feature_ddl_forms`) or refused, with the refusal reason;
  - each file that is data only;
  - whether local receipt or attempt evidence locks the folder (reuse `migration_revision` / `migration_plan` evidence helpers);
  - a final line per folder: convertible, partly convertible, or stays a migration.
- It ends with the `archive-migrations` command for the folders that are fully convertible, and repeats the rule from `docs/feature-releases.md`: reconcile every old attempt first, and already-released one-time DDL is archived, not copied into a feature.
- It writes nothing, captures nothing and archives nothing. Exit code 0 when it could analyse every folder, 2 when it could not.
- New module `scripts/feature_convert.py`, wired through `feature_cli.py`, `team.sh` and `team.ps1`. It is not part of the capsule engine.
- Tests in a new `tests/test_feature_convert.py`; a short section in `docs/feature-releases.md` under "Converting an existing project later".

---

## Verification for every phase

Run after the last edit, and copy the real final lines into the report:

```bash
python3 scripts/pre_push_check.py --all
```

```bash
python3 scripts/ci_run_tests.py --jobs 8
```

```bash
git status --short
```

Report, per task: done, partly or skipped; files changed; one sentence. List every existing test whose assertion changed, every place the result differs from this plan and why, and anything unverified. Do not claim a check passed unless it ran and passed.
