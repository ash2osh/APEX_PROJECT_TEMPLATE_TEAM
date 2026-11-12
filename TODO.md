# Project TODO

Keep project work and inherited-template feedback here. Follow
[the template feedback guide](docs/template-feedback.md) for confirmed issues
in inherited template files. This file belongs to the project and survives
template upgrades.

## Tracked feedback

<!-- Add one record per item. Keep evidence sanitized and useful to reproduce. -->

## Postponed refactor follow-ups (module split, not done)

- `scripts/release_state.py` is 2,076 lines after the split. Still in core: the stage gate `validate_stage_proof` and the recovery-retirement predicates (`_rollout_retired`, `_rollout_is_reconciled`, `_rollout_is_operator_reconciled`, `_rollout_is_recovery_parent`) because core gates call them. Leaf that could move next: `recovery_parent_rollout` (its only caller is `scripts/rollout_capsule.py`). Moving more needs a review of the patch targets in `tests/test_feature_rollout.py` and `tests/test_release_completion.py` first.
- `scripts/baseline.py` is 2,700 lines after the split. The `build` bundle generators (`_reference_data_bundle` through `_build`, about 1,740 lines) are used by the build alone, but they share `_schema_filter`, `_source_lines`, `_reference_tables` and `_validate_reference_capture` with the export subcommands. Tests also reference many of them on `baseline` (for example `_column_definition_check` and `_exact_settings_check`). They need their own plan, including the test patch targets.
- `python3 scripts/baseline.py --help` fails with an ImportError because `baseline.py` uses relative imports. `scripts/team.sh baseline --help` works. This predates the split and is unchanged.
