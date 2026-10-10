# Phase 4 result: migration rehearsal

## Files changed

- `scripts/migration_rehearsal.py` — new rehearsal flow, analyzer-based file classification, transaction driver, rollback proof, console and JSON reports.
- `scripts/migration_checks.py` — added a same-session check-driver mode that keeps `SAFE_FUNCTIONS` validation and can stop on a false precondition.
- `scripts/migrate.py`, `scripts/migrate.sh`, `scripts/team.sh`, `scripts/team.ps1` — CLI routing, usage/help text, and Windows argument forwarding for `--report`.
- `README.md`, `docs/migration-rules.md`, `docs/known-limitations.md` — usage, behavior, exit codes, and deliberate rehearsal limits.
- `tests/test_migration_rehearsal.py` — fake-SQLcl integration coverage for ordered folders, DDL skipping, row counts, failed statements, preconditions, rollback proof, autocommit, environment confirmation, and JSON output.
- `tests/test_documentation_contract.py`, `tests/test_template_manifest.py`, `tests/test_windows_support.py` — documentation, manifest ownership, and Windows wrapper contracts.
- `.agent/RESULT_phase4.md` — this result.

## Decisions

- Rehearsal freezes the selected migration inputs and classifies each SQL file with `analyze_batch`. A file executes only when all analyzed statements are reviewed DML and its folder has preconditions and postconditions. DDL, implicit-commit, opaque, and mixed files are skipped with a reason.
- All selected folders run in order in one SQLcl data session. The generated check drivers execute validated plain SELECT checks in that transaction, so later folders can observe earlier uncommitted rows. The driver rolls back and checks each folder's preconditions again before exiting.
- If SQLcl exits on an error or false precondition, `WHENEVER SQLERROR ... ROLLBACK` ends that session and the command checks each folder's preconditions again in fresh read-only sessions. The report distinguishes this proof from the explicit in-session rollback path.
- SQLcl must report `AUTOCOMMIT OFF` in a probe session before the payload session starts. The payload driver also sets autocommit off, keeps SQL error rollback handling, and verifies the target identity before executing migration files.
- Rehearsal writes no receipt or write-attempt marker. Staging and production retain their interactive confirmation. `--report` writes schema-version 1 JSON and is rejected unless `--rehearse` is present.
- An identity mismatch while checking rollback is reported as an unverified rollback proof; it cannot be treated as a pass.

## Verification

- Focused migration, CLI, documentation, manifest, and Windows suites: **140 tests, 18 skipped, passed**.
- Full filtered suite: **1,282 tests, 126 skipped, passed**. The 24 known Unix-socket tests were excluded. A first run also found 13 TCP loopback daemon tests that fail during socket setup with `PermissionError: Operation not permitted` in this sandbox; those 13 were excluded from the passing run. The platform-skipped control-break test remained in the run.
- Additional checks passed: `py_compile` for the changed Python modules, `bash -n` for `team.sh` and `migrate.sh`, `git diff --check`, and both Bash help commands.
- `/snap/bin` was removed from `PATH` for test runs, as requested. PowerShell-dependent test cases skipped in this environment.
- No database or external network calls were made. Migration tests use `tests/fake_sqlcl.py`; local loopback socket setup for 13 daemon tests was denied by the sandbox.

## Offline verification limits

The real SQLcl output for `SHOW AUTOCOMMIT`, per-statement feedback, and the final rollback marker was not observed against a database. The transaction and rollback behavior was verified through fake SQLcl output only. PowerShell wrapper execution was not available; its static wrapper tests ran, while PowerShell-dependent cases skipped.

## Issues found

| Class | Severity | What | Evidence | Proposed action |
|---|---|---|---|---|
| Offline verification | Limitation | Real SQLcl and DEV26 transaction behavior could not be exercised. | The user constrained this task to fake SQLcl with no database or network. | Before operational use, run a coordinated rehearsal against DEV26 and review the JSON report and rollback proof. |
| Sandbox | Limitation | Thirteen TCP loopback daemon tests cannot create their local sockets here. | The unfiltered run reported `PermissionError: Operation not permitted` during socket setup; the remaining selected suite passed. | Run those TCP tests in CI or another environment that permits loopback sockets. |
| Windows verification | Limitation | PowerShell runtime behavior was not exercised. | `/snap/bin` was removed from `PATH`; PowerShell-dependent tests skipped, while static wrapper checks passed. | Run the PowerShell suite on Windows CI. |

No outstanding implementation defect was found by the offline test run.
