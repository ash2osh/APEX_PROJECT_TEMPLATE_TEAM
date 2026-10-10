# Phase 6 result: preflight reliability

## Files changed

- Configuration and guidance: `.env.example`, `README.md`, `docs/migration-rules.md`, `docs/known-limitations.md`, `scripts/local_config.py`, `scripts/load_env.sh`, `scripts/load_env.ps1`, `scripts/team.sh`, `scripts/team.ps1`.
- Preflight and catalog implementation: `scripts/migrate.py`, `scripts/migration_checks.py`, `scripts/schema_catalog.py`, `scripts/schema_catalog.sql`.
- Fixtures and tests: `tests/fixtures/schema_catalog/owner-inventory.json`, `tests/fixtures/schema_catalog/owner-snapshot.json`, new `tests/fixtures/schema_catalog/view-synonyms.json`, `tests/test_catalog_transport.py`, `tests/test_documentation_contract.py`, `tests/test_local_config.py`, `tests/test_migrate_cli.py`, `tests/test_migration_checks.py`, `tests/test_schema_catalog.py`, `tests/test_template_manifest.py`, `tests/test_windows_support.py`.
- This report: `.agent/RESULT_phase6.md`.

`template-manifest.json` was not changed: its existing `docs/**`, `scripts/**`, and `tests/**` ownership rules already cover these files. The manifest contract test verifies that coverage.

## Decisions

- Snapshot catalog schema version 2 records owner and PUBLIC synonyms plus direct SELECT-grant evidence from read-only `ALL_*` or `DBA_*` catalog views. An owner-owned selectable table, view, or materialized view needs no separate grant. An external target needs a direct grant to the schema owner or `PUBLIC`. Private synonyms take precedence over PUBLIC synonyms. Database links, synonym chains, and grants available only through roles remain unproven and fail closed.
- Inventory equality uses owner, object name, type, and subobject name. It ignores status, last DDL time, and other catalog metadata that can change without an object identity change. Stable captures use SHA-256 inventory fingerprints.
- `MIGRATION_PREFLIGHT_INVENTORY_RETRIES` defaults to 3 additional capture attempts; `0` disables retries. `check-conflicts` and the migration runner use the same capture/retry helper. Exhaustion reports `LIVE_PREFLIGHT_UNAVAILABLE` with fingerprints for the final mismatching pair.
- The Oracle grant queries use the view-specific owner column (`OWNER` in `DBA_TAB_PRIVS`, `TABLE_SCHEMA` in `ALL_TAB_PRIVS`); static tests assert the two branches separately.

## Verification

- Full filtered suite: 1,328 tests, `OK`, 126 skipped, in 112.929 seconds. The 33 socket-restricted Chrome daemon/lifecycle/TCP tests were excluded, and `/snap/bin` was removed from `PATH`.
- Focused catalog, preflight, documentation, manifest, and Windows-support suite: 148 tests, `OK`, 19 skipped.
- `git diff --check`, `bash -n scripts/team.sh scripts/load_env.sh`, and `python3 -m py_compile scripts/schema_catalog.py scripts/migration_checks.py scripts/migrate.py scripts/local_config.py` passed.
- Oracle skills were current; oldest relevant installed date was `2026-10-08T05:13:11.538803622Z`.
- No database, live SQLcl, network, or Windows runtime was used. Oracle execution of `schema_catalog.sql`, a live concurrent-recompilation race, and Windows PowerShell behavior could not be verified offline. Tests exercised recorded catalog fixtures and fake SQLcl only.
- No files were staged or committed.

## Issues found

| Class | Severity | What | Evidence | Proposed action |
| --- | --- | --- | --- | --- |
| Correctness | Medium | A private synonym present in the owner inventory could be mistaken for the referenced table itself, bypassing target and grant checks. | Independent review found the live-inventory branch; regression coverage now exercises private and PUBLIC synonyms, missing targets, and missing direct grants. | Resolved: route synonym-only names through target resolution and require catalog evidence for the target. |
| Reliability | Medium | Status or DDL-time changes could make a stable owner inventory look different, while real create/drop churn needed bounded retries. | Recorded fixtures cover status/DDL-only changes, a new object across captures, retries, and final fingerprints. | Resolved: compare stable object identity keys and retry the complete inventory/snapshot pair up to the configured limit. |
| SQL catalog | Medium | The grant catalog views require branch-specific owner-column names. | Static review identified the `DBA_TAB_PRIVS.OWNER` and `ALL_TAB_PRIVS.TABLE_SCHEMA` distinction; tests assert each SQL branch separately. | Resolved in source and tests; compile against Oracle remains unverified because live database access was explicitly excluded. |
| Documentation | Low | Wording could imply that owner-owned targets also require a separate SELECT grant. | Final independent review compared the documented rule with resolver behavior. | Resolved: documentation limits direct grant evidence to external targets. |
| Verification limit | Informational | Live Oracle catalog behavior and Windows runtime behavior were not exercised. | The requested run was offline and used fixture/fake-SQLcl tests. | Qualify the SQL and PowerShell branches in their respective environments when those checks are authorized and available. |
