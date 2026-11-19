# Live feature-release checks

`tests/test_live_feature_release.py` runs the feature-release path against a
real Oracle schema. It is opt-in: CI never runs it, and it skips unless the
variables below are set.

## What you need

- SQLcl on `PATH`, and a saved SQLcl connection whose login is the schema owner.
- A schema you can afford to lose. The checks create and drop their own objects,
  all named `LIVE_<random>` or `live<random>`. They touch nothing else.
- Bash. The feature path runs `scripts/team.sh`, so the module is skipped on Windows.

## Run

```bash
TEMPLATE_LIVE_DISPOSABLE=1 \
TEMPLATE_LIVE_CONNECTION=<saved-connection> \
TEMPLATE_LIVE_SCHEMA=<SCHEMA> \
python3 -m unittest discover -s tests -p test_live_feature_release.py -v
```

For the two-schema check, also set `TEMPLATE_LIVE_SECOND_CONNECTION` and
`TEMPLATE_LIVE_SECOND_SCHEMA`. Without them that one check is skipped.

To run the staging profile on a separate database, such as a local Docker
container, also set `TEMPLATE_LIVE_STAGING_CONNECTION`. It names a saved
connection that logs in as the same schema there. The staging profile then uses
it, and the production profile keeps `TEMPLATE_LIVE_CONNECTION`. Do not combine
it with the two-schema variables.

On the local Oracle AI Database 26ai Free 23.26.3 containers used for the last runs, the
module took about 18 minutes with two databases (19 tests, one skipped) and about 13 minutes
with one database and two schemas (19 tests, two skipped). Run one test with `-k <name>`.

The `baseline` commands have their own opt-in module, `tests/test_live_baseline.py`; see [Live check](baseline.md#live-check).

## Feature-first DEV, promotion, and timing

Use the existing disposable fixture approval with the saved connection aliases
and exact database names below. Choose one scenario with the unittest `-k`
filter. Each new scenario checks the named target before creating its fixtures.

| Scenario | Additional selection |
| --- | --- |
| `dedicated_long_view_create_and_replace_on_dev` | `TEMPLATE_LIVE_PROOF_DEV_DB_NAME` for the disposable DEV target. Creates a view with supplementary Unicode, changes a value beyond 32 KB, verifies that the writer refuses concurrent drift there, then verifies replacement and no-op detection. |
| `dev_receipt_and_capsule_match_across_three_disposable_targets` | `TEMPLATE_LIVE_STAGING_CONNECTION`, `TEMPLATE_LIVE_PROOF_PRODUCTION_CONNECTION`, and `TEMPLATE_LIVE_PROOF_DEV_DB_NAME`, `TEMPLATE_LIVE_PROOF_STAGING_DB_NAME`, `TEMPLATE_LIVE_PROOF_PRODUCTION_DB_NAME`. Use three separate disposable databases with the same owner name. The scenario checks the DEV receipt against the capsule, tests a redirected saved alias and invalid staging proof, verifies the installed source and settings on all three targets, and retains an ambiguous DEV attempt. |
| `staging_consolidation_benchmark_preserves_equivalent_effects` | `TEMPLATE_LIVE_FEATURE_BENCHMARK=1`, `TEMPLATE_LIVE_STAGING_CONNECTION`, and `TEMPLATE_LIVE_PROOF_STAGING_DB_NAME` for disposable staging separate from DEV. |

The benchmark runs three rotated trials for batched legacy migrations, the
consolidated feature capsule, and the bounded intermediate transition. Each
sample starts with equivalent objects on the same staging connection. Each
path verifies the same final body and backfilled data. The bounded migration
asserts intermediate source, compiler settings, and validity before its data
step. The report under `scratch/feature-benchmark-*.json` contains per-trial
observations, medians, database and SQLcl versions, selected steps, body writes,
connected SQLcl sessions, and authored and delivered SQL bytes. Delivered bytes
include nested scripts and count marked check sections, including in-session
checks. The report records preparation, catalog/identity sessions, apply,
independent verification and total release time alongside the capsule's
object-budget coverage and outcome. The connection probe includes a scalar round
trip. Run the suite on approved targets before using its timings to estimate a
release window.

## Safety

- Each run copies this repository to a temporary directory. The benchmark writes
  its result under the checkout's ignored `scratch/` directory.
- The product's production-name guard applies. A connection name containing a
  word such as `live`, `prod` or `prd` is refused, so name the connection and
  schema so they are clearly disposable. The module's own object names are not
  checked.
- Teardown drops every object the run created and reports any drop that fails.
  A scenario that fails early drops only what it created.
- Failed runs can leave a rollout lock in the copy's `releases/.local-state.json`.
  The copy is removed at the end, so the lock does not outlive the run.

## Scenarios covered

| Check | What it shows |
| --- | --- |
| `test_capture_freeze_then_staging_and_production_with_stage_proof` | Capture from DEV, freeze, a staging apply with checks, production with stage proof, object coverage, and cleanup eligibility. |
| `test_feature_table_then_migration_alter_through_staging_and_production` | A feature checkpoint creates a table, then a dated migration folder adds a column to it. The migration's preflight defers its missing prerequisite to that checkpoint, then staging and production apply both, with stage proof and `includedMigrationSteps` `[2]`. Needs a separate staging database (`TEMPLATE_LIVE_STAGING_CONNECTION`) and skips otherwise. |
| `test_trigger_function_and_type_with_body_ship_through_staging_and_production` | A trigger, a function and a type with its body are captured from DEV with `--trigger`, `--function` and `--type`. A changed function and a changed trigger freeze, prepare, apply to staging, then to production with stage proof. Each object's catalog state is read back, and a trigger the release replaces must still be enabled at apply. The unchanged type specification is not re-created, and its body stays VALID. It runs on one database or two. |
| `test_production_refuses_without_a_capsule_or_stage_proof` | Production refuses without a capsule or stage proof, and writes nothing either way. The release carries one DDL checkpoint and one SQL script, because a release with only a SQL script is refused at freeze. |
| `test_ddl_with_a_missing_prerequisite_is_refused_at_prepare` | A missing prerequisite table stops preparation before any write. |
| `test_abrupt_termination_mid_apply_is_recovered_after_reconciliation` | A SIGKILL mid-apply. A piped `RECONCILE` acknowledgment is refused, `--prepare --recover --yes` reconciles the attempt, and recovery runs to success. |
| `test_noop_checkpoint_is_refused_while_an_unresolved_attempt_occupies_the_scope` | An unchanged staging unit is a no-op. An unresolved attempt on the same target still refuses it at the checkpoint level, and nothing is written. |
| `test_constraints_comments_and_synonyms_ship_through_a_complete_capsule` | A `ddl.sql` with primary, unique, foreign and check constraints, modified columns, expression indexes, views with CTEs, comments (one in Arabic), sequences, private and public synonyms, and unqualified and direct grants freezes, prepares, applies to staging and then to production with stage proof, with complete object coverage. Every effect is read back from the catalog of both databases. The public synonym part needs `CREATE PUBLIC SYNONYM` and `DROP PUBLIC SYNONYM` on the schema and runs only when both databases grant them. On one database, production is refused for the one-time DDL that the staging run already applied, and the test asserts that refusal rather than applying it again. |
| `test_occupied_constraint_name_and_missing_parent_are_refused_at_prepare_before_any_write` | An occupied constraint name and a missing foreign-key parent each stop preparation, and nothing is created. |
| `test_generated_constraint_and_comment_checks_tell_a_match_from_a_mismatch` | Each generated after-state check returns 1 on a database that matches and 0 on one that differs (rule, parent, column, column order, type, status, validation, text, modified column length/type/nullability, expression index expression count and uniqueness, view existence, and grant privilege/grantee), and each before-state check refuses an occupied name or an identical key. |
| `test_view_dependencies_match_what_oracle_records` | For about 40 view shapes (CTE chains, nested and parenthesised joins, comma joins, subqueries at depth five inside function calls, set operations, `EXTRACT`/`TRIM`), the extracted dependency set equals `USER_DEPENDENCIES` once Oracle has compiled the view. A missed table fails the test. |
| `test_two_schemas_on_one_database_apply_as_two_checkpoints` | Two schemas on one database, applied as two checkpoints. Needs the second schema. |
| `test_migration_only_capsule_ships_through_staging_and_production_and_refuses_recover` | A release made of one `migrate` step and no feature checkpoint. Freeze records `migrationOnly`. Staging, then production with stage proof, apply it: `includedCheckpoints` is `[]` and `includedMigrationSteps` is `[1]`, and the table is read back on both databases. `--recover` is refused and writes nothing, and `clean-capsule` finds the capsule eligible. Needs the staging database; skips otherwise. |
| `test_migrate_step_refused_before_its_write_leaves_the_target_free` | A migrate step whose precondition holds at prepare and fails at apply. The run ends failed, the step reports `no-write-attempted`, `databaseState` is `not-started`, and the table is absent. When the precondition holds again, the same capsule is prepared and runs to success. |
| `test_unknown_run_without_a_checkpoint_is_reconciled_then_frees_the_target` | A no-op package checkpoint, then a SQL script that fails on a row another session left. The run ends `unknown`, with no checkpoint to recover, and the next run is refused. `feature reconcile-run` refuses a piped acknowledgment, even the exact text (exit 1, nothing changed), then closes the run with `--yes`, recorded as `acknowledgedBy: --yes`. The closed run stays not cleanup-eligible and production refuses the report it left. After the blocking row is removed, the same capsule is refused before any write because step 1 already has a rollout receipt; a continuation manifest frozen as a new capsule runs to success on staging, and on production too when there are two databases. |
| `test_standalone_procedure_and_function_capture_refresh_move_and_doctor_drift` | `--procedure` and `--function` capture. A refresh refuses an edit that DEV lacks, and `--discard-unapplied` erases it. A DEV change to an unedited function is reported by `feature doctor --live` with exit 1, and a refresh replaces the unedited file without `--discard-unapplied`, because the file equals its retained DEV baseline. A procedure moves to a second feature only with `--move-from`. |
| `test_disabled_trigger_is_refused_at_prepare_and_rechecked_before_apply` | A trigger DISABLED on the staging database, which the release would replace, is refused at prepare. It stays disabled, its source is unchanged, and no plan is written. Enabled at prepare and disabled again before apply, it is refused by the apply precondition, and nothing is written. |
| `test_convert_plan_reads_dated_migration_folders_offline` | `feature convert-plan` on three dated folders in the copy. Text and JSON output both exit 0, and no file in the copy changes. No database is used. |

## Confirmations and acknowledgments

The module confirms its rollouts with `-y`, and a production rollout also passes `--confirm-target`
with the name the module reads once at class setup through `SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME')`.
The production profile is the same database as DEV, so one name serves both. If
`TEMPLATE_LIVE_SECOND_CONNECTION` is a different database, a production rollout that writes to both is
refused by the name rule. The two acknowledgments (`RECONCILE` and `RECONCILE-RUN`) are typed at a terminal or supplied by `--yes`;
environment variables and redirected input do not supply them.
The two live reconciliation scenarios supply their acknowledgment with `--yes`, as an unattended agent would,
and first check that a piped acknowledgment is refused. Piped `y` answers are no longer accepted by any prompt.

## Not covered

- A migration profile that differs from the read profile. That was checked by
  hand with a proxy login.
- Oracle versions other than the one you run against.
- Real staging or production targets. These checks never touch them. The
  feature-release guide requires a separately authorized disposable rehearsal
  before any measured claim.
- A `migrate` step killed mid-apply. The kill check covers a feature checkpoint only.
- The prepare refusal for a replaced specification whose body is not in the release, and cleanup after a
  superseded reconciled run. Both are covered offline only.

## The kill check and the database's timing

The kill check waits until the killed run reaches `attempting`, kills its process
group, then waits until the package body's DDL time and status stop changing for
ten seconds. That wait matters because killing the client does not stop a DDL
the server has already started. Recovery must not capture a state that is still
moving.

The ten-second quiet period is a heuristic, not proof. The schema login cannot
read `V$SESSION`, so the check cannot see whether the server-side operation has
ended. Before reconciling a real interrupted rollout, confirm the operation has
ended with a privileged view, as the reconciliation prompt asks.
