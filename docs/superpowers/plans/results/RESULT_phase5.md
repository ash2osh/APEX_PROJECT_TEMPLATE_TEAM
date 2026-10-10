# Phase 5 result: migration revision handling and attempt state

## Files changed

- `scripts/migration_revision.py` — added `revise` and read-only `--check` behavior.
- `scripts/migrate.py` — records positively identified pre-payload failures as `apply-not-started`, while retaining locks for ambiguous or started applies.
- `scripts/team.sh`, `scripts/team.ps1` — command dispatch, help, Windows path conversion and forwarding.
- `README.md`, `docs/migration-rules.md` — command usage, exit codes, revision order and the attempt-state rule and limits.
- `tests/test_migration_revision.py` — revision, check-state, CLI and output-classification coverage.
- `tests/test_migrate_cli.py` — fake SQLcl coverage for identity refusal, connection refusal, a post-payload ORA error and a no-ORA cutoff.
- `tests/test_documentation_contract.py`, `tests/test_template_manifest.py`, `tests/test_windows_support.py` — documentation, template ownership and Windows wrapper contracts.
- `.agent/RESULT_phase5.md` — this report.

`template-manifest.json` already owns `scripts/**` and `tests/**`; the manifest test confirms both new files are covered, so the manifest itself did not need a change.

## Decisions

- `revise <folder> [--reason TEXT]` copies the validated SQL and `checks.json` bytes into the next `-rNNN` folder, keeping each filename and sequence number. It prepends `Supersedes <old-folder>: <reason>` to the new README, preserves the remaining README bytes and leaves the source folder untouched. The default reason is `follow-up revision`; environment receipts are not copied. It refuses an invalid suffix, a gap, `r999`, or any newer revision. The printed hint lists the family in ascending revision order and says to omit folders with verified receipts. Flat and `migrations/<SCHEMA>/` paths are supported.
- Apply drivers print `MIGRATION_PAYLOAD_STARTED:<filename>` immediately before each payload file. A failed apply is recorded as `apply-not-started` with `writeAttempted: false` only when the retained SQLcl output identifies a recognized identity refusal, recognized connection failure before the first payload marker, or local SQLcl start failure, and contains neither a payload-start nor completion marker. All other failures remain `apply-failed-or-unknown` and locked. In particular, the 1,856-statement cutoff after five minutes stays locked without an ORA error, and ORA errors after a payload marker stay locked because earlier statements may have run.
- Only `frozen` and `apply-not-started` attempt records with `writeAttempted: false` are unlocked. Receipts and other attempt states are locked. `revise --check` reads only local receipts and `scratch/` evidence, returning 0 for unlocked, 1 for locked, and 2 for unknown or incomplete evidence (including an attempt directory without a run manifest). It performs no SQLcl, database or network work.
- The classifier favors false locks over an unsafe retry. A truncated, missing, unreadable or unrecognized log, or a timeout without qualifying pre-payload evidence stays locked. A recognized connection error after the identity marker still qualifies only if the payload marker is absent. The per-file marker is a conservative start boundary, not a per-statement execution ledger; the local check cannot see other developers’ repositories or prove live schema state. These limits are described in `docs/migration-rules.md`.

## Verification

- `test_migration_revision`: **16 passed**.
- `test_migrate_cli`: **32 passed** using fake SQLcl.
- Documentation, manifest and Windows support groups: **71 run; 52 passed, 19 skipped**.
- Full filtered unittest discovery: **1,318 tests ran; 126 skipped; passed**. The filter excluded **25 local socket tests** whose Unix or TCP socket setup is blocked by this sandbox. `/snap/bin` was removed from `PATH` for test runs.
- `python3 -m py_compile scripts/migration_revision.py scripts/migrate.py`, `bash -n scripts/team.sh`, `scripts/team.sh --help`, `scripts/team.sh revise --help` and `git diff --check` passed.
- No files were staged or committed. The pre-existing Phase 1–4 result files were left untouched.
- Oracle skills are current; oldest relevant recorded installation date: `2026-10-08T05:13:11Z`.

## What could not be verified offline

- No database, network or real SQLcl was used, as required. The SQLcl output classification and identity refusal were exercised with `tests/fake_sqlcl.py`; real SQLcl output formatting and the reported DEV26 cases could not be reproduced here.
- The sandbox blocks the 25 excluded local socket tests. PowerShell was not available outside the blocked Snap launcher, so runtime wrapper behavior could not be exercised; static Windows wrapper tests passed and PowerShell-dependent tests skipped.

## Issues found

| Class | Severity | What | Evidence | Proposed action |
| --- | --- | --- | --- | --- |
| Offline verification | Limitation | Real SQLcl marker and ORA output formatting was not observed. | The requested tests use fake SQLcl and no database/network access. | During a coordinated DEV26 qualification, compare the retained output with the documented classifier before operational use. |
| Sandbox | Limitation | Local Unix and TCP socket tests could not run. | 25 socket tests were excluded because sandbox socket setup is blocked; the other 1,318 discovered tests passed with 126 skips. | Run the excluded tests in CI or another environment that permits local sockets. |
| Windows verification | Limitation | PowerShell runtime execution was unavailable. | PowerShell-dependent cases skipped; static path-conversion and dispatch tests passed. | Run the wrapper tests on Windows CI. |
