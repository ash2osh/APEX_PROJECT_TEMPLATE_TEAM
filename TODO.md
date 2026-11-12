# Project TODO

Keep project work and inherited-template feedback here. Follow
[the template feedback guide](docs/template-feedback.md) for confirmed issues
in inherited template files. This file belongs to the project and survives
template upgrades.

## Tracked feedback

<!-- Add one record per item. Keep evidence sanitized and useful to reproduce. -->

## Postponed refactor follow-ups (module split, not done)

- `scripts/baseline_build.py` (2,090 lines, moved out of `scripts/baseline.py`) still holds the whole build group. Next cuts, not started: the reference-data generators (`_reference_data_bundle`, `_read_exported_reference_data`, `_reference_data_tables`, `format_sql_value` and its value helpers, about 612 lines); the structure, grant and code bundle generators with their readme, byte and snapshot-key helpers (about 342 lines in six definitions); the migration folder writers (`_build_migration_folder`, `_baseline_family_state`, `_folder_contents_match` and the staging helpers, about 171 lines). `_build_migration_folder`, `_write_migration_stage`, `_structure_bundle`, `_windows_system_tool` and `_windows_current_user_sid` are patched on `scripts.baseline` by tests, so moved callers reach them as `baseline.<name>`; keep that when cutting again. The module-level imports `capture_environment_catalog`, `capture_inventory_snapshot_with_retries`, `inspect_migration_lock`, `load_migration` and `validate_staged_migration` are patched there too: keep them bound on `scripts.baseline` and call them as `baseline.<name>` from moved code.
- `scripts/baseline.py` (690 lines) keeps the CLI, the exports and five helpers that other modules read on `scripts.baseline`: `_source_identity`, `_reference_tables`, `_validate_reference_capture`, `_source_units` and `_settings_map` (98 lines). `feature_release`, `feature_sources`, `feature_doctor` and `baseline_data` call them, so they stay.
- `scripts/release_state.py` (2,062 lines) still holds the stage gate `validate_stage_proof` (50 lines) and the retirement predicates `_rollout_retired`, `_rollout_is_reconciled`, `_rollout_is_operator_reconciled` and `_rollout_is_recovery_parent` (46 lines). Three core gates (`_assert_checkpoint_allowed_index`, `assert_auxiliary_step_allowed` and `start_rollout`) reach the predicates through `_rollout_retired`, and `recovery_parent_rollout` in `scripts/release_recovery.py` calls `_rollout_is_recovery_parent`. No existing module fits the stage gate, and `scripts/release_recovery.py` is for recovery prompts, reconcile sessions and the recovery parent lookup. Moving either needs a decision on a new module first.
