# Walkthrough 2026-11-17

## Parity refresh 3 against the 26.1 main

- Branch `chore/parity-refresh-3` from `origin/main` (`6f36132`). Only `docs/superpowers/template-parity.json` and this file changed. The 26.1 side was read at its committed HEAD `2572abd`, which includes 26.1 PRs 74-79. The 26.2 side includes PRs 108-115.
- Counts before: 1 unreviewed, 23 changed, 0 missed, 30 feature, 58 intended, 13 stale (112 differing, exit 1). After: 0 unreviewed, 0 changed, 3 missed, 34 feature, 75 intended, 0 stale (112 differing, exit 0).
- Stale, removed (13): all are now byte-identical on both lines, so the core stamp covers them: `scripts/compare_schema.py`, `scripts/invoke_sqlcl.ps1`, `scripts/load_env.ps1`, `scripts/load_env.sh`, `scripts/local_config.py`, `scripts/sqlcl_safe.sh`, `scripts/workflow_skills.py`, `tests/fake_ords_sql.py`, `tests/test_backup_ords_cli.py`, `tests/test_compare_schema.py`, `tests/test_env_schema_lists.py`, `tests/test_live_feature_release.py`, `tests/test_local_config.py`.
- Missed (3), to port or decide:
  - `scripts/rollout.py`: 26.2 `_snapshot` refuses a resumed migrate, sql-script or ords-import step whose target binding changed after planning. Its legacy run also applies the writer-identity guard on DEV. 26.1 has neither: it keeps no frozen binding for those steps and guards the writer only on staging and prod. Minor: 26.1 uses `Path.relative_to` where 26.2 uses `relative_to_root` for directory inputs. The app-deploy binding stays intended, per the recorded decision.
  - `tests/test_rollout.py`: 26.2 tests the resume refusal after a configuration change and the sql-script refusals for SQLcl abbreviations and comment hazards. 26.1 has no rollout-level test for either. 26.1 has one schema-1 receipt resume test that 26.2 lacks.
  - `tests/test_windows_support.py`: tests only, the tested code is identical. 26.2 tests that Wait-SqlclProcess finishes pending output and the optional DBA alias in `load_env.ps1`. 26.1 has neither test.
- Changed records, re-classified:
  - To feature (4): `tests/test_export_cli.py`, `tests/test_multi_schema_cli.py`, `tests/test_team_cli.py`, `tests/test_verify_local.py`. Each holds 26.2-only extras and their tests.
  - To intended: `scripts/validate_app_source.py` (wording only; the autoInstall refusal is identical on both lines) and `TODO.md` (was unreviewed; a 26.2 parked dry-run item with no 26.1 equivalent).
  - Kept intended, with refreshed reasons (15): `README.md`, `ai_generate/2026-11-16/WALKTHROUGH.md`, `docs/feature-releases.md`, `docs/migration-rules.md`, `docs/publish-rules.md`, `scripts/dev_test_core_migrations.py`, `scripts/publish_app.ps1`, `scripts/publish_app.sh`, `tests/known_flaky.txt`, `tests/test_backup_db_cli.py`, `tests/test_baseline_bundle_cli.py`, `tests/test_migration_revision.py`, `tests/test_path_utils.py`, `tests/test_publish_cli.py`, `tests/test_publish_lifecycle_gate.py`.
- Conventions used (the owner should confirm):
  - A wording-only difference with identical behaviour is `intended`.
  - A test that pins a behaviour on identical code, which the other line does not test, is `missed`.
  - Per-line test names for the same behaviour are `intended`.
- Verification (offline, on this branch):
  - `python3 tools/template_parity.py ../APEX_PROJECT_TEMPLATE_26_1`: exit 0, 0 unreviewed, 0 changed, 0 stale (3 missed, 34 feature, 75 intended).
  - `python3 -m unittest discover -s tests -p 'test_template_parity*.py'`: 4 tests, OK.
  - `python3 scripts/pre_push_check.py --all`: exit 0, "all checks passed".
- Not verified:
  - No database, SQLcl, network, publish or export command ran. The template update check was not run because it uses network Git.
  - Oracle skills: `~/.dbtools/skills/skills.json` shows the repository `lastSync` and all installations at 2026-11-15, within seven days, so no `skills sync -force` was due. No sync was run.
  - The 88 entries whose blobs did not change (feature and intended) keep the reasons reviewed at those blobs. They were not re-read hunk by hunk.
  - The Windows test files were classified from test names and diffs. They were not run.
- Open:
  - Decide whether the migrate, sql-script and ords-import resume binding refusal in `scripts/rollout.py` and `tests/test_rollout.py` is a port (missed) or an intended difference. The recorded decision names only the app-deploy binding, so it stays missed.
