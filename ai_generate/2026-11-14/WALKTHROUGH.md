# Walkthrough 2026-11-14

## Review safety fixes (26.2 port)

Ported the review-safety fixes from 26.1 (merge commit `9b7281a`, PR #68) to the APEX 26.2 template on branch `fix/review-safety-fixes-262`.

### Changes Ported

1. **Task 1: Bind rollout confirmation to writes** (`scripts/rollout.py`, `scripts/rollout_capsule.py`, `scripts/migrate.py`)
   - Rechecked confirmed target name before execution against each step's observed identity.
   - Verified that writer session identity matches the independently observed identity before executing payloads.
   - Enforced target matching across SQL-script, ORDS, migration, and app-deploy drivers.

2. **Task 2: Per-environment migration archive evidence** (`scripts/migration_archive.py`, `tests/test_migration_archive.py`)
   - Scoped migration attempts and receipts to their specific environment and target database identity.
   - Refuses default archive if unresolved attempts exist in other environments; `--preserve-unresolved` preserves unresolved folders while archiving verified environments.

3. **Task 3: Mandatory lifecycle verification** (`scripts/publish_app.sh`, `scripts/publish_app.ps1`, `tests/test_publish_lifecycle_gate.py`)
   - Refuses before import and preserves DEV baseline when lifecycle helpers are missing or unavailable.
   - Preserves baseline and retains recovery evidence on lifecycle capture or verification failure.
   - Bash and PowerShell implementations aligned, maintaining `--describe` functionality without live connection.

4. **Task 4: Baseline SQL mapping and quoting** (`scripts/baseline_bundles.py`, `scripts/baseline_sql.py`, `tests/test_baseline_generated_sql.py`)
   - Ported SQL tokenization, q-quote delimiters, table default sequence retargeting, view source schema retargeting (with clause awareness), and bitmap index retargeting.
   - Preserved 26.2 commit `55c1ec1` (`_new_table_order` and multi-statement compile blocks).

5. **Task 5: Terminal-only acknowledgments** (`scripts/terminal_confirm.py`, `docs/migration-attempt-resolution.md`, `docs/live-feature-tests.md`, `tests/test_terminal_confirm.py`, `tests/test_feature_cli.py`, `tests/test_feature_rollout.py`, `tests/test_migration_resolution.py`, `tests/test_live_feature_release.py`, `.agents/rules/agent-safety.md`)
   - Removed `TEAM_ACK_FROM_STDIN` environment variable bypass from production code and tests.
   - Redirected input is refused for all typed acknowledgments.
   - The two live reconciliation tests are skipped in the automated suite, as in 26.1. A developer checks them manually.

6. **Task 6: Public command documentation contract** (`README.md`, `docs/GETTING_STARTED.md`, `docs/publish-rules.md`, `tests/test_documentation_contract.py`)
   - Documented that publish targets DEV and deploy targets staging/production.
   - Corrected confirmation flag documentation (`-y` / `--yes`).
   - Added documentation contract tests verifying public documentation against parser arguments.

### Verification Results

Run on `fix/review-safety-fixes-262` after the port commits:

- Full suite: `CI_TEST_JOBS=4 python3 scripts/ci_run_tests.py`: `Ran 3051 tests; skipped=70; failures=0; errors=0; incomplete_modules=0`, exit 0.
- `python3 scripts/pre_push_check.py --all`: exit 0, "all checks passed".
- Focused run of the 12 changed modules (`test_rollout`, `test_feature_rollout`, `test_migrate_cli`, `test_migration_archive`, `test_publish_lifecycle_gate`, `test_publish_cli`, `test_publication_identity`, `test_baseline_generated_sql`, `test_terminal_confirm`, `test_feature_cli`, `test_migration_resolution`, `test_documentation_contract`), run with `PYTHONPATH=tests:.` as CI sets it: no test failures, 2 skipped.
- Baseline modules (`test_baseline`, `test_baseline_bundle`, `test_baseline_bundle_cli`, `test_baseline_generated_sql`, `test_baseline_settings`): `Ran 123 tests`, OK.
- The porting agent reported a pre-port baseline of `Ran 3006 tests; skipped=70` on `origin/main`. This was not rerun independently, so the "+45 tests" difference is unconfirmed.
- All tests executed offline with synthetic fixtures and in-process mocks; no database, SQLcl, or live target was accessed.

### Decisions for Review

1. `.agents/rules/agent-safety.md` is committed with the Task 5 change. Its new wording matches the 26.1 fix.
2. In 26.2, `scripts/publish_app.ps1` writes `import-transcript.log` and `scripts/publish_app.sh` writes `sqlcl-output.log`. This difference predates the port and this port does not change it.
3. The two live reconciliation tests are skipped in the automated suite, as in 26.1, so no automated test covers those reconcile paths.
