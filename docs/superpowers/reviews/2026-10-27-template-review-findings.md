# APEX 26.1 template review: findings and status

Review date: 2026-10-27. Status updated: 2026-10-30.

Scope at review: the uncommitted implementation on branch
codex/feature-release-capsules-impl, based on e48b7053c53b05e014a30d511c1f399e880460ec.
The review inspected the feature workflow and its interactions with migration
preflight, target profiles, application deployment, upgrade ownership,
archive/baseline safety, and CI. Its reproductions used isolated fixtures and a
replaced SQLcl boundary.

The 2026-10-30 status below comes from current HEAD, its regression tests, and
live runs against a local Oracle 23ai Free container (23.26.3.0.0, COMPATIBLE
23.6.0). The live runs used disposable schemas. They did not touch staging or
production, and they did not cover other Oracle versions.

Status key:

- **Fixed, live-verified**: a regression test covers it, and a live run showed it.
- **Fixed, unit-tested**: a regression test covers it. No live run reached it.
- **Partly verified**: some of the behaviour was seen live. The rest is unit-tested only.

Repair plan (historical): `docs/superpowers/plans/2026-10-27-feature-release-review-remediation.md` in the APEX 26.1 template. It is not carried into this template.

## Review findings

| ID | Priority | Finding at review | Status | Evidence |
| --- | --- | --- | --- | --- |
| F1 | P1 | Version-2 execution without `--capsule` entered the legacy executor. A production SQL-only manifest returned success without staging proof; a mixed manifest ran its SQL step before rejecting feature-migrate. | Fixed, live-verified | `test_prod_refuses_without_stage_proof_before_checkpoint_execution`. Live: production without a capsule, and without stage proof, is refused and writes nothing. A mixed manifest is refused with no rows written. |
| F2 | P1 | DDL had no dependency preflight. `CREATE INDEX` on a missing table produced a plan with zero preconditions, and earlier DDL could commit first. | Fixed, live-verified | `test_later_missing_index_table_refuses_before_first_create`. Live: preparation refuses with "missing prerequisite table". |
| F3 | P1 | A no-op checkpoint skipped durable unresolved-attempt checks, so a retry could succeed while an unknown attempt remained. | Fixed, live-verified | `test_noop_cannot_bypass_unknown_or_attempting_occupancy`, `test_noop_cannot_bypass_attempting_occupancy`. Live: `test_noop_checkpoint_is_refused_while_an_unresolved_attempt_occupies_the_scope` reaches the checkpoint-level refusal on a no-op checkpoint. An identical retry is also refused by the rollout-level check. |
| F4 | P1 | Cleanup inferred success only from feature write attempts. | Fixed, live-verified | `test_cleanup_refuses_unresolved_production_attempt`, `test_all_noop_and_mixed_production_runs_need_complete_evidence_before_cleanup`. Live: a failed production run is ineligible; a verified production no-op is eligible. |
| F5 | P1 | Abrupt termination left `attempting`, and recovery accepted only `unknown`. | Fixed, live-verified | `test_recovery_preparation_accepts_reconciled_attempting_after_writer_terminated`, `test_recovery_checkpoint_runs_while_the_killed_parent_rollout_is_still_recorded_running`. Live: a SIGKILL mid-apply, then reconciliation, then recovery to success. |
| F6 | P1 | Two schemas on one database were treated as conflicting identities. | Fixed, live-verified | `test_prepare_multiple_schemas_on_one_database_uses_each_staging_migration_profile`. Live: two-schema preparation and apply. |
| F7 | P2 | Preparation used the read profile, and execution used the migration profile. A separate migration login was refused as drift. | Fixed, live-verified | `test_prepare_and_apply_use_the_separate_migration_profile_and_reject_profile_drift`. Live (manual, proxy login): preparation and the staging run both succeed. |
| F8 | P2 | Planned target settings were omitted before `CREATE OR REPLACE`. | Fixed, live-verified | `test_partial_source_override_uses_complete_prepared_target_settings`. Live: the generated driver emits `PLSQL_CCFLAGS = 'branch:TRUE'`. |
| F9 | P2 | Stored-unit uniqueness was checked only within one checkpoint. | Fixed, live-verified | `test_capsule_construction_rejects_duplicate_stored_units_across_checkpoints`. Live: freeze refuses one unit in two checkpoints. |
| F10 | P2 | All SQL-script and ORDS steps counted as uncovered object work. | Fixed, live-verified | `test_auxiliary_step_classifier_only_accepts_top_level_dml_and_recognized_ords`. Live: a DML-only step keeps `coverageComplete` true in staging and production. |
| F11 | P2 | Initializing a second schema for one feature refused the shared README. | Fixed, live-verified | `test_init_second_schema_preserves_shared_readme_and_first_checks`. Live: a second schema initializes and keeps the README. |
| F12 | P2 | Review directory names exceeded filesystem limits for long unit names. | Fixed, live-verified | `test_review_directory_component_is_bounded_for_long_unit_names`. Live: a 128-character package name captures and writes review diffs. |
| F13 | P2 | Found live: the no-op check never matched a captured package. The rebuilt header differed in case, quoting and blanks; settings compared numbers with text and an empty `PLSQL_CCFLAGS` with NULL; and the no-op postcondition compared the rebuilt header with the live text. | Fixed, live-verified | `test_no_op_comparison_ignores_header_case_quoting_and_setting_types`. Live: `test_noop_checkpoint_is_refused_while_an_unresolved_attempt_occupies_the_scope` and `test_abrupt_termination_mid_apply_is_recovered_after_reconciliation` both reach the no-op path. |

F8 follows Oracle's documented compilation model: an explicit `CREATE OR REPLACE`
takes its compilation settings from the session. See
[PL/SQL units and compilation parameters](https://docs.oracle.com/en/database/oracle/oracle-database/19/lnpls/architecture-pl-sql.html).

The F10 repair must also address target binding before enabling mixed-capsule
promotion, because current version-2 standalone SQL and ORDS steps embed one
connection and login in the frozen manifest. Relaxing coverage alone would keep
the staging binding during production promotion. That binding is still in place.

## Defects found by the live runs

These were not in the 2026-10-27 review. They appeared when the feature path ran
against a real database, and the mocked tests had missed them. All are fixed on
the branch, each with a regression test.

| # | Defect | Test |
| --- | --- | --- |
| 1 | `feature-dependencies` and `feature-synonym-access` bound `:scope` twice, so the positional `USING` list raised ORA-01008 on every run. | `test_every_compare_env_query_binds_scope_once_for_its_positional_arguments` |
| 2 | The identity `database_version` came from `PRODUCT_COMPONENT_VERSION` (23.0.0.0.0), while the checks use `DBMS_DB_VERSION` (23.0). Every staging or production feature run was refused. | `test_compare_env_identity_reports_the_database_version_the_migration_checks_report` |
| 3 | The validity check id kept the underscores from the unit name. Check ids must be kebab-case. | `test_validity_check_ids_are_kebab_case_for_underscored_unit_names` |
| 4 | An empty `PLSQL_CCFLAGS` was compared with `= ''`, which is never true. | `test_empty_ccflags_matches_the_null_flags_oracle_stores_for_it` |
| 5 | The expected source header kept the owner qualifier. `ALL_SOURCE` blanks it, so every source postcondition failed. | `test_stored_source_header_blanks_the_owner_as_oracle_stores_all_source` |
| 6 | The identity guard checked `v_db_name`, which the driver never declares. Every SQL-script rollout failed. | `test_rollout_identity_guard_only_checks_variables_the_driver_declares` |
| 7 | A new stored unit's guard was rejected, so new units could not be created. | `test_new_unit_guard_has_no_before_digest_and_still_validates` |
| 8 | Recovery required an outcome for every step. A rollout killed in its first step records none, so recovery was impossible for that case. Only earlier writing steps need one. | `test_recovery_accepts_a_parent_killed_before_recording_its_checkpoint_or_later_steps` |
| 9 | After an acknowledged recovery, the killed parent run still counted as occupying the target. The checkpoint and SQL/ORDS checks blocked the recovered checkpoint and every later run on that scope. | `test_recovery_checkpoint_runs_while_the_killed_parent_rollout_is_still_recorded_running`, `test_a_later_run_is_not_blocked_by_a_killed_parent_that_a_recovery_reconciled` |

## Open

- Only one Oracle version (23.26.3.0.0 Free) has been run. Other targets have not. The two production clones run 23.26.1.0.0 and have not been tested.
- The staging rehearsal ran against disposable local schemas only, on 2026-10-30. No real staging or production system has been run.
- The repair plan's checklist (`docs/superpowers/plans/`) has not been ticked off against these results.

