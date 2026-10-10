# Phase 8b result

Implemented baseline reference-data export/build and ORDS filtering for the
template. The work stays local to this worktree; no database, network, commit,
or push operation was used.

## Files changed

- `scripts/baseline.py`, `scripts/compare_env_catalog.sql`,
  `scripts/compare_schema.py`, and `scripts/schema_catalog.py`
- `scripts/team.sh` and `scripts/team.ps1`
- `README.md`, `docs/baseline.md`, `docs/baseline.example.json`, and
  `docs/migration-rules.md`
- `template-manifest.json`
- `tests/test_baseline.py`, `tests/test_compare_env.py`,
  `tests/test_documentation_contract.py`, `tests/test_template_manifest.py`,
  and `tests/test_windows_support.py`
- `tests/fixtures/baseline/data_dev.json`, `ords_modules.sql`,
  `reference_data_constraints.json`, `reference_data_source.json`, and
  `reference_data_target.json`
- `.agent/RESULT_phase8b.md`

## Decisions

- Added `baseline export-data`, `baseline build --data`, and
  `baseline filter-ords`. Export uses the table/column allow-list and writes
  complete, paged JSON under `scratch/baseline/<env>/data/<schema>/`; cap or
  completeness failures stop the export.
- The generator formats exact decimal numbers, typed full ISO dates/timestamps,
  collision-safe q-quotes in 32-character chunks, and embedded newlines with
  `CHR`. Empty text becomes `NULL`. Oracle numeric JSON values are decoded as
  `Decimal` and written back as exact JSON number tokens.
- Foreign keys between allow-listed tables resolve target parent IDs by the
  configured parent labels. Missing, duplicate, or ambiguous target labels
  block generation. Fully null optional keys remain null; partially null
  composite keys fail safely because their parent cannot be mapped.
- Inserts are guarded by natural keys, preserve existing rows, report visible
  same-key value differences, and use count-based checks. DML is grouped into
  steps capped at 100 statements and 50,000 bytes. `GENERATED ALWAYS` IDs are
  omitted; `BY DEFAULT` generators use `START WITH LIMIT VALUE` after seeding.
- The code bundle ends with a bounded, repeated invalid-object compile step and
  name-based `VALID` postconditions. ORDS filtering reuses the validated module
  parser used by rollout and refuses names absent from the export.
- Existing migration folders remain immutable. An identical build keeps bytes;
  a receipted or attempted folder is skipped and reported. Documentation shows
  how to rehearse the generated DML with `migrate --rehearse` and notes that the
  rehearsal skips identity DDL.

## Verification

- Focused suites: baseline 35, compare-env 21, documentation 19, manifest 15,
  Windows wrapper 52 (23 skipped because PowerShell is unavailable with
  `/snap/bin` removed), and rollout 24. All passed.
- Filtered full suite: 1,405 tests ran, 131 skipped, all passed. This omitted
  only the 24 Chrome daemon tests that require socket binds blocked by the
  sandbox.
- The unfiltered full suite ran 1,429 tests and reported 24 expected Chrome
  socket failures/errors plus one SQLcl timeout marker assertion under load.
  That timeout test passed when run alone and also passed in the filtered full
  suite.
- `git diff --check`, `bash -n scripts/team.sh`, and Python byte-compilation of
  `baseline.py`, `compare_schema.py`, and `schema_catalog.py` passed.

## Offline limits

The generated SQL and PL/SQL were validated with the repository's SQL/check
validators and fake SQLcl fixtures, but could not be compiled or executed by
Oracle SQLcl against a database in this offline task. Database behavior,
including the identity DDL, remains for the team's qualified environment.

## Issues found

| Class | Severity | What | Evidence | Proposed action |
|---|---|---|---|---|
| Correctness, resolved | Medium | Default JSON float decoding could round high-precision Oracle `NUMBER` values. | Added exact-decimal capture/export and SQL literal tests; compare-env and filtered full suites pass. | Fixed with Decimal parsing and exact JSON number serialization. |
| Correctness, resolved | Low | A nullable reference foreign key was treated as an unresolved parent ID. | Added a null-FK regression; generated SQL keeps the key null and omits the parent lookup. | Fixed; partially null composite keys now fail with a clear message. |
| Verification gap | Medium | Oracle SQL/PLSQL runtime behavior could not be verified offline. | No database/network was used; fake SQLcl and local SQL/check validation passed. | Run the generated capture and rehearsal through qualified SQLcl/Oracle before applying migrations. |
