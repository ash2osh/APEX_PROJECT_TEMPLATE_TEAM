# Phase 2 result

## Files changed

- Added `scripts/verify_checks.py` and `scripts/verify_checks.sh` for read-only migration-check evaluation.
- Updated `scripts/migration_checks.py` to support bounded concurrent sessions and expose batch-to-check coverage.
- Added the `verify` command to `scripts/team.sh` and `scripts/team.ps1`.
- Updated `README.md` and `docs/migration-rules.md` with command, output, and exit-status documentation.
- Added verifier and SQL-driver regressions in `tests/test_verify_checks.py` and `tests/test_migration_checks.py`; extended `tests/test_template_manifest.py` and `tests/test_windows_support.py`.
- Added this result file.

`template-manifest.json` did not need a content change: its existing `scripts/**` and `tests/**` template-owned patterns cover the new files. The manifest test now asserts that ownership for the verifier files.

## Decisions

- The command loads the selected migration folders through the existing manifest/layout and `db_targets.py` helpers, including `--schema` and environment-specific migration connection profiles.
- The verifier reuses `run_checks` and the migration runner's inventory identity checks. `--jobs` controls concurrent check sessions; the default remains one. Results stay in input order.
- Reports distinguish true, false, and error results. False checks return 1; SQL, identity, configuration, and incomplete-session errors return 2, with errors taking precedence over false checks. `--only-failed` filters displayed rows while preserving the complete summary. JSON reports use `schemaVersion: 1`.
- SQLcl drivers, inventory, and diagnostics are written under a mode-restricted `scratch/migration-verify-*` directory. Success removes it; any failed or errored verification retains it as evidence. The command writes no receipt or apply run manifest.
- The supplementary-plane character regression was already present from Phase 1 and asserts `LENGTH2` for both the CLOB append amount and output offset. Added the second regression, which reconstructs the generated query and checks that whitespace-only lines, a trailing hyphen, and a quote survive unchanged.

## Verification

- Focused groups (`test_documentation_contract`, `test_verify_checks`, `test_migration_checks`, `test_template_manifest`): **72 tests passed**.
- `test_windows_support.py` with the sandbox-blocked Snap launcher removed from `PATH`: **33 tests run, 19 passed, 14 skipped**.
- Full `python3 -m unittest discover -s tests` run with `/snap/bin` removed from `PATH` so unavailable PowerShell checks skip: **1,277 tests run; 1,131 passed, 122 skipped, 8 failed, 16 errored**. All 24 failures/errors are in Chrome daemon tests and report socket `PermissionError: Operation not permitted` or follow-on daemon-start failures in the restricted sandbox.
- `git diff --check`, `bash -n scripts/verify_checks.sh scripts/team.sh`, and `python3 -m py_compile scripts/verify_checks.py scripts/migration_checks.py` passed. Both team help routes list the new command and options.

## Not verified offline

- No database, real SQLcl, or network was used. SQL execution and identity results were exercised through `tests/fake_sqlcl.py`; target profile selection was tested with configuration fixtures, not a live environment.
- The Windows wrapper was not run on Windows. The only discovered PowerShell launcher is `/snap/bin/pwsh`, which this sandbox cannot execute; PowerShell-only checks therefore skipped in the filtered run.
- The coordinator must run the full suite outside this sandbox to verify the Chrome daemon tests that need local socket access.

## Issues found

| Class | Severity | What | Evidence | Proposed action |
| --- | --- | --- | --- | --- |
| Test environment | Low | The existing Chrome daemon tests require local Unix or TCP socket operations, which the sandbox denies. | The full suite had 8 failures and 16 errors, all in `test_chrome_mcp_daemon.py`; socket operations reported `PermissionError: Operation not permitted`. | Run the full suite in the coordinator's less-restricted environment, as planned. |
| Test environment | Low | Existing PowerShell availability checks treat a discoverable Snap launcher as runnable, although `snap-confine` blocks it in this sandbox. | The unfiltered `test_windows_support.py` run had two failures when invoking `/snap/bin/pwsh`; filtering that path made the PowerShell tests skip and the remaining module pass. | Keep PowerShell execution in the host/Windows qualification run, or make test launcher discovery account for sandbox-blocked Snap installations. |

No separate implementation defect was found during review.
