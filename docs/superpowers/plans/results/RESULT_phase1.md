# Phase 1 result

## Files changed

- Migration runner and checks: `scripts/migrate.py`, `scripts/migration_checks.py`,
  `scripts/migration_manifest.py`, `scripts/schema_catalog.py`, and
  `scripts/sqlcl_session.py`.
- Configuration and command help: `scripts/local_config.py`,
  `scripts/load_env.sh`, `scripts/load_env.ps1`, `scripts/migrate.sh`,
  `scripts/team.sh`, `scripts/team.ps1`, and `.env.example`.
- Documentation: `docs/migration-rules.md`, `docs/known-limitations.md`, and
  `README.md`.
- Tests: `tests/test_local_config.py`, `tests/test_migrate_cli.py`,
  `tests/test_migration_checks.py`, `tests/test_migration_manifest.py`,
  `tests/test_ords_profile.py`, `tests/test_schema_catalog.py`, and
  `tests/test_sqlcl_session.py`.
- This report: `.agent/RESULT_phase1.md`.

No new template-owned script or source file was added, so
`template-manifest.json` did not need an entry.

## Decisions

- Apply and check/inventory SQLcl sessions retain the 300-second default.
  `MIGRATION_APPLY_TIMEOUT_SECONDS` and `MIGRATION_CHECK_TIMEOUT_SECONDS`
  accept positive finite seconds; timeout errors carry the phase, configured
  limit, elapsed time and saved SQLcl diagnostics.
- Read-only checks run in sequential batches sized by generated UTF-8 driver
  bytes, with a default `MIGRATION_CHECK_BATCH_BYTES` budget of 2 MiB
  (`2097152`). Every session retains the existing read-only and disabled-commit
  guards, and every reported session identity is checked. A single check over
  budget is rejected before SQLcl starts.
- Failed fresh postconditions print up to 20 check IDs and expected/observed
  values or Oracle/SQLcl error codes by default. `--verbose` prints all; the
  run manifest records failure IDs and verification evidence.
- Added the 14 requested deterministic, side-effect-free functions to
  `SAFE_FUNCTIONS`, with per-function justifications and accepted/rejected
  tests. The `validate_check_query` logic and `FORBIDDEN_WORDS` were not
  changed.
- Documented a `USER_SOURCE` line-count plus `SUM(ORA_HASH(...))` fingerprint,
  including its collision limits and the available cross-version field note.

## Verification

- Focused changed test groups: **146 tests passed**. The run selected
  `test_sqlcl_session.py`, `test_migration_checks.py`,
  `test_migration_manifest.py`, `test_migrate_cli.py`,
  `test_schema_catalog.py`, `test_local_config.py`, and
  `test_ords_profile.py`.
- Full suite: **1,261 tests ran; 1,216 passed, 21 skipped, 8 failed and 16
  errored**. All 24 failures/errors are in `tests/test_chrome_mcp_daemon.py`:
  the sandbox denies local Unix and TCP socket creation/bind with
  `PermissionError: Operation not permitted`. The PowerShell tests used the
  installed PowerShell binary directly because `/snap/bin/pwsh` is blocked by
  `snap-confine` here.
- `bash -n scripts/load_env.sh scripts/team.sh scripts/migrate.sh` passed.
- `git diff --check` passed.

## Not verified offline

- No database, real SQLcl, or network was used. The plan's report of matching
  `ORA_HASH` results on Oracle 19c and 26ai was documented as field evidence,
  but this phase could not reproduce it. Verify the exact expression, seed,
  bucket limit, source-line count and expected hash sum on each target Oracle
  version before relying on a fingerprint.
- The full suite cannot be entirely green in this sandbox because its Chrome
  daemon tests require local socket access, which is denied by the execution
  environment. The phase-specific tests all pass.
