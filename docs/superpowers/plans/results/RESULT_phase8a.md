# Phase 8a Result

Implemented the requested baseline export and build workflow for sub-steps 8.1
and 8.2. Reference-data export/build and ORDS filtering remain out of scope.

## Files changed

- Modified `README.md`, `docs/migration-rules.md`, `scripts/team.sh`,
  `scripts/team.ps1`, `scripts/compare_schema.py`,
  `scripts/compare_env_catalog.sql`, and `template-manifest.json`.
- Added `scripts/baseline.py`, `docs/baseline.md`,
  `docs/baseline.example.json`, and `tests/test_baseline.py`.
- Added recorded fixtures under `tests/fixtures/baseline/` for source,
  settings, grants, system privileges, and two-environment builds.
- Updated `tests/test_documentation_contract.py`,
  `tests/test_template_manifest.py`, and `tests/test_windows_support.py`.
- Updated the Phase 8a rows in `.superpowers/sdd/2026-10-10-deployment-speedups/progress.md`.

## Decisions

- Reused the existing read-only compare-env SQLcl session and catalog capture.
  Baseline-only source, compiler-settings, and exact view-text sections are
  paged at 500 rows with a 100,000-row cap; incomplete or capped captures fail
  before export output is accepted.
- `baseline.json` configures schemas, prefixes, exclusions, the reserved
  reference-data allow-list, grant policy, and conventional sequence mappings.
  Exports preserve owner-filtered source lines and compiler settings, view
  text/DDL, object grantable flags, and direct system privilege admin options.
  System privilege export uses the configured read-only DBA connection and
  verifies database scope against the ordinary connection.
- `baseline build` creates structure, grouped object-grant, and exact-source
  migration families under the target schema. New folders use the next free
  revision and pass `load_migration`, SQL validation, and check-query
  validation. Existing folders are left untouched; matching receipted or
  attempted folders are reported as skipped.
- The code generator uses per-unit compiler settings, CLOB assembly for
  whitespace-only and trailing-hyphen lines, source checks with plain text
  equality and final-line newline tolerance, and multi-pass invalid-unit
  compilation. An empty prefix list means all names, including for the
  compile-all step.
- Review found that an empty prefix list selected every name but previously
  omitted the compile-all step. The behavior is fixed and covered by a new
  regression test.

## Verification

- Filtered full suite: **1,413 tests run; 0 failures; 0 errors; 155 skipped**.
  Twenty-five socket-dependent daemon tests were explicitly skipped after the
  sandbox returned `PermissionError: [Errno 1] Operation not permitted` for
  local socket bind/send operations. `/snap/bin` was removed from `PATH`.
- Focused baseline, compare-env, documentation, manifest, and Windows-support
  suite: **127 tests; 0 failures; 23 platform skips**.
- `python3 -m py_compile scripts/baseline.py scripts/compare_schema.py`,
  `bash -n scripts/team.sh`, and `git diff --check` passed.
- Tests used `tests/fake_sqlcl.py` and recorded fixtures. No real SQLcl,
  database, external network, or migration execution was used; the local socket
  attempts were blocked by the sandbox as described above.

## Not verified offline

- Oracle compilation and runtime behavior of the new SQL catalog capture,
  DBMS_METADATA view DDL extraction, generated DDL, and generator synchronization
  could not be checked without a database and real SQLcl.
- PowerShell wrapper runtime checks could not run here: the available `pwsh`
  launcher is under `/snap/bin`, which was removed from `PATH` as requested.
  Static and platform-independent Windows wrapper contract tests ran.

## Issues found

No open implementation defect remains. Findings and verification limits:

| Class | Severity | What | Evidence | Proposed action |
|---|---|---|---|---|
| Implementation finding | Low, resolved | Empty `prefixes` means all names, but compile-all was initially omitted. | The example/config contract defines an empty prefix list as selecting every name; the new regression test asserts the generated compile-all query covers all configured invalid units. | Fixed: emit unfiltered owner-scoped compile passes when prefixes are empty. |
| Verification limit | Informational | Oracle SQL and generated migration runtime behavior were not exercised against a live database. | The task prohibited database/network use; all SQLcl calls used the fake executable and recorded payloads. | Validate with the normal authorized database workflow before relying on generated migrations. |
| Sandbox limit | Informational | Local Unix/TCP socket operations were blocked, so 25 socket-dependent tests were skipped. | An unfiltered test attempt failed with `PermissionError: [Errno 1] Operation not permitted` during socket bind/send. | Run those socket tests in an environment that permits local socket operations. |
| Platform limit | Informational | PowerShell runtime execution was unavailable in this test environment. | The only `pwsh` command was `/snap/bin/pwsh`, excluded from `PATH` for the requested test run. | Run the wrapper tests on Windows or with an approved non-snap PowerShell runtime. |
