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

### Native APEX locks require cooperating import wrappers

- Trigger: adding APEX 26.2 native application locks to the team publish flow.
- Evidence: Docker APEX 26.2.0 with SQLcl 26.3.0.0 refused different-owner
  `LOCK_APPLICATION` and wrong-owner unlock, but full and page-only imports
  still succeeded over another developer's lock and preserved its metadata.
  Repeated acquisition by the same workspace developer also succeeded.
- Preferred behavior: require successful native acquisition before importing;
  refuse preexisting locks and verify owner/run comment before release. Retain
  human coordination and drift checks. Do not claim that raw SQLcl imports or
  concurrent processes sharing a developer account are excluded by the lock.
- Verification: repeat the different-owner acquisition, raw full/partial import,
  same-owner reentry and owner-filtered release probes through the production
  wrappers when qualifying changes to lock integration. Live evidence is in
  `docs/superpowers/plans/2026-10-08-apex-26.2-live-results.md`.

### Verify generated deployment effects after a version stamp

- Trigger: using the public version setter after a page-only APEXlang import.
- Evidence: in two Docker probes, `SET_APPLICATION_VERSION` changed the exported
  manifest and added/advanced generated deployment
  `app.sessionStateProtection.allowUrlsCreatedAfter`; that value matched the
  live app's `last_updated_on`. Exports without another write were stable.
- Preferred behavior: verify this precise deployment key against server state,
  keeping unrelated differences as failures. Qualify bookmark behavior and
  explicitly configured cutoffs before enabling partial publishing; do not
  infer runtime semantics from the version getter or manifest alone.
- Verification: compare whole exports before/after stamping, verify the live
  timestamp and repeat for stability, then exercise real bookmark URLs and
  explicit cutoff settings. The 2026-10-08 native runtime tests passed; the guarded
  no-notice route additionally requires an explicit live-matching salt and cutoff.

### Compare a path the code resolved with a resolved path

- Trigger: on 2026-10-19 `windows-offline` turned `main` red in both template
  repositories with `KeyboardInterrupt not raised` in
  `test_interrupt_after_skill_rename_restores_the_backup`.
- Evidence: `workflow_skills.setup` calls `root.resolve()`. On the Windows
  runner the temporary directory is the short name `RUNNER~1`, which `resolve()`
  expands, so the test's `path == source` never matched. Linux and macOS passed,
  and the pull request was merged before its Windows job finished.
- Preferred behavior: when a test compares a path the code produced, compare
  `os.path.normcase(os.path.realpath(...))` of both sides, never the raw
  `Path` it built. Merge only after all three jobs pass; branch protection now
  binds administrators, and auto-merge waits for the checks.
- Verification: the test compares resolved paths, and its Windows job passed
  in the fix's pull request.

### Give each new command its own test module

- Trigger: on 2026-10-19 three parallel pull requests (the everyday help,
  `query`/`template-status`, and `new-migration`) each conflicted with the
  others.
- Evidence: each added its wrapper test just before the same method in
  `tests/test_team_cli.py`, and its case arm just before `revise` in
  `scripts/team.sh` and `scripts/team.ps1`. Git cannot merge two insertions at
  one point, so every later pull request needed a manual merge.
- Preferred behavior: put a new command's tests in a new
  `tests/test_<command>_cli.py` (it may import helpers from
  `tests/test_team_cli.py`), and add its case arm in alphabetical order rather
  than before a fixed neighbour. Before opening several pull requests at once,
  merge their branches in a scratch worktree and resolve any conflict while the
  context is fresh.
- Verification: each `tests/test_<command>_cli.py` runs alone with
  `python3 -m unittest discover -s tests -p 'test_<command>_cli.py'`.

### A variable only CI sets must be isolated in the tests that read it

- Trigger: on 2026-10-20 the first full CI run of the parallel test runner hung
  for 30 minutes on Linux, although the suite passed locally in 163 s.
- Evidence: CI sets `CI_TEST_JOBS=4`; locally it is unset. The tests of
  `scripts/ci_run_tests.py` call `main()` with default arguments, which then read
  the variable and started the whole suite again from inside the suite. The log
  showed 67 of 92 modules reported and `test_ci_run_tests` as the first one that
  never did.
- Preferred behavior: a test that exercises code reading an environment
  variable patches that variable itself, and a script that starts child test
  processes sets the variable for them. Before pushing a change to the workflow,
  run the same command with the workflow's `env:` values set
  (`CI_TEST_JOBS=4 python3 scripts/ci_run_tests.py`).
- Verification: `test_child_processes_get_one_job` and
  `test_tests_in_a_parallel_ci_run_never_start_another_parallel_run`; the full
  suite with `CI_TEST_JOBS=4` set passes in 163 s (26.1) and 199 s (26.2).

### A test module must pass when it is run alone

- Trigger: on 2026-10-20 the first parallel CI run (one process per module)
  failed `test_source_provenance` on Windows in the 26.2 repository, and a test
  of the report file's permissions in the 26.1 repository.
- Evidence: `discover -s tests` imports every module into one process, so
  `_no_real_sqlcl` (and `_windows_lf`, which makes `write_text` write LF on
  Windows) applied to modules that never import them. Run alone,
  `test_source_provenance` wrote its marker with CRLF and the code refused it.
  The permissions test passed only because the runner's own temp folder is
  private: `runner.temp` grants the Users group read access.
- Preferred behavior: the parallel runner loads `_no_real_sqlcl` in each child
  process; a test imports what it depends on; a CI step may change the temp
  directory only to another private one (the long form of the runner's own).
- Verification: `python3 -m unittest discover -s tests -p 'test_<module>.py'`
  for each module, and the Windows job of the pull request that introduced it.
