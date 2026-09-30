# Self-improvement notes

When a workflow fails, preserve the exact sanitized output and recovery path,
then improve the shared Python contract or its tests. Do not add a one-off
shell bypass, a second reconciliation implementation, a numeric mirror path,
or a database-as-source shortcut. New behavior needs a focused regression test
before implementation and fresh verification before claiming completion.

## Durable lessons

Add a lesson only when observed evidence supports it. Each states the trigger,
the evidence, the preferred behavior, and the check that keeps it true.

### A fake `sql` cannot prove a SQLcl driver works

- Trigger: the live-migration and schema-comparison drivers passed every unit
  test, then failed on their first run against a real database.
- Evidence: against Oracle 26ai Free, `check-conflicts --env dev` failed with
  `Enter value for 1: / Substitution cancelled`. `_catalog_driver` wrote
  `ALTER SESSION SET CURRENT_SCHEMA = X` without a terminator, so SQLcl merged
  it with the next line. SQLcl's verify and feedback echo, and the DDL
  `INSERT` data-movement text, also polluted the output that is parsed.
- Preferred behavior: terminate every generated statement, and start generated
  drivers with `SET VERIFY OFF`, `SET FEEDBACK OFF` and `SET DDL INSERT OFF`.
  Before merging a change to a SQLcl driver, run it once against a disposable
  schema in the local Docker database; never against a shared database.
- Verification: `tests/test_sql_driver_contracts.py` asserts the terminator and
  the three `SET` lines in the generated catalog driver.

### PowerShell `&` isolates variables, not the process environment

- Trigger: a script called with `&` wrote `$env:` values (a schema selection,
  or everything a dot-sourced loader sets) and the caller kept reading them.
- Evidence: verified in PowerShell 7.6.5: after `& child.ps1`, an `$env:` value
  the child set is visible to the caller, while a plain variable is not. Code
  review found the consequence here: `check_db_target.ps1 -Schema X` would
  rewrite `TABLES_*` and `PROJECT_SCHEMA` in the caller's environment, which the
  next `doctor` or `export` iteration would read.
- Preferred behavior: a script that is called by another script and writes
  `$env:` snapshots `[Environment]::GetEnvironmentVariables("Process")` first
  and restores it exactly in a `finally` block. A script that selects a schema
  for itself (`publish_app.ps1`, `team.ps1 --schema`) does so deliberately.
- Verification: the PowerShell doctor, backup, export and publish parity tests
  in `tests/test_multi_schema_cli.py`.

### Do not `return ,@(...)` from a helper whose callers wrap it in `@( )`

- Trigger: `Split-ProjectEnvList` returned `,@($Value.Split(','))`.
- Evidence: verified in PowerShell 7.6.5: with `return ,@(...)`,
  `@(Helper 'A,B').Count` is 1, not 2, and `@(Helper '').Count` is 1, not 0.
  A function that calls `Write-Output` and then returns `$false` yields two
  objects, so `-not (f)` is `False` and the failure goes unseen.
- Preferred behavior: return the array unrolled and wrap every call in `@( )`.
  Inside a function that returns a status, print with `Write-Host`.
- Verification: the PowerShell loader and doctor tests in
  `tests/test_env_schema_lists.py` and `tests/test_multi_schema_cli.py`.

### A fake `sql` must drain stdin before it exits

- Trigger: one PowerShell publish test failed in CI and passed on the same
  commit elsewhere.
- Evidence: under CPU load `Start-Process` sometimes threw `Broken pipe` while
  feeding stdin to a fake `sql` that had already exited, or the transcript was
  read before SQLcl's output landed. Reproduced at 1 failure in 40 runs under
  load and 0 in 120 after the fix.
- Preferred behavior: start every fake `sql` with `cat > /dev/null`. Retry a
  transient start or capture failure only for a read-only lookup
  (`Get-AppParsingSchema`); never retry a write.
- Verification: the fixtures in `tests/test_multi_schema_cli.py` drain stdin,
  and `Get-AppParsingSchema` retries at most three times.
