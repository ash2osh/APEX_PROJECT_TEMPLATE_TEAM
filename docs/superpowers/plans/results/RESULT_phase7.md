# Phase 7 result: `compare-env`

## Files changed

- Catalog and command implementation: `scripts/compare_schema.py` (extended with `compare-env`; existing `compare-schema` path retained), `scripts/schema_catalog.py` (framed payload transport), and new `scripts/compare_env_catalog.sql` (read-only paged catalog capture).
- Configuration and wrappers: `.env.example`, `scripts/local_config.py`, `scripts/load_env.sh`, `scripts/load_env.ps1`, `scripts/team.sh`, and `scripts/team.ps1`.
- Documentation: `README.md`, new `docs/compare-env.md`, `docs/migration-rules.md`, and `docs/known-limitations.md`.
- Manifest and tests: `template-manifest.json`, new `tests/test_compare_env.py`, new `tests/fixtures/compare_env/dev.json` and `staging.json`, and updates to `tests/test_documentation_contract.py`, `tests/test_local_config.py`, `tests/test_team_cli.py`, `tests/test_template_manifest.py`, and `tests/test_windows_support.py`.
- This report: `.agent/RESULT_phase7.md`.

## Decisions

- `compare-env` extends the existing `compare_schema.py` tool and uses the existing command route. With no `--section`, it captures all 19 documented sections; repeated `--section` values narrow the capture. Records are matched by section keys/names and then compared field by field. Differences are `missing on target`, `different`, or `only on target`.
- Catalog reads are ordered in pages of 500. Capture fails closed above 100,000 rows per section, above a 128 MiB compressed payload, on missing/incomplete page evidence, or on a truncated CHECK condition. A short terminal page, including an empty page after an exact multiple, proves completion.
- System-generated `SYS_C...` constraints pair by table, type, ordered columns, referenced table/columns, and condition. Named CHECK constraints compare their condition. Triggers, views, and stored code compare line count and `SUM(ORA_HASH(text))` after skipping source line 1.
- DBA-only sections use the optional per-environment `*_DBA_SQLCL_CONNECTION` alias in a read-only session and require the DBA connection to match the base connection's database, container, schema, and edition. Missing or unusable access is a readiness blocker.
- The emitted DBA script is a review artifact containing additive object/system/role grants, supported network ACE additions, and ORDS schema enablement. It does not revoke privileges, change default-role settings, or copy ORDS module/template/handler source. Unsupported, denied, or time-bounded ACEs remain marked for manual review. The script is written only from complete evidence.
- Reference/application data (including ERP and camp rows) is out of scope. Foreign-key definitions compare table/column labels; the docs explicitly say to compare reference values by label, not id.

## Verification

- Full filtered suite: **1,351 tests ran; 130 skipped; `OK`** in 113.668 seconds. **32 socket-focused test IDs** were excluded because this sandbox blocks the relevant socket tests. `/snap/bin` was removed from `PATH`.
- Focused `test_compare_env.py`: **20 passed**.
- `git diff --check`, Python compilation for the changed Python modules, `bash -n` for the changed shell scripts, and JSON parsing of `template-manifest.json` passed.
- `scripts/team.sh --help` and `python3 -m scripts.compare_schema compare-env --help` displayed the updated command and options. The wrapper's command-specific help requires a local `.env`, which is absent in this worktree; no `.env` was created.
- No files were staged or committed. Existing Phase 1–6 result files were left unchanged.
- Oracle skills were current; oldest relevant recorded installation date: `2026-10-08T05:13:11.538803622Z` (10 distinct Oracle skill roots).

## What could not be verified offline

- No database, network, or real SQLcl was used. Fake SQLcl and recorded fixtures exercised capture framing and comparisons, but Oracle dictionary compatibility, actual SQL execution on 19c/26ai, live identity behavior, and real rollout execution remain unverified.
- PowerShell runtime behavior could not be exercised in this environment. Static Windows wrapper tests ran; PowerShell-dependent tests were skipped after removing `/snap/bin` from `PATH`.
- The Oracle `ORA_HASH` comparison can collide and is not an exact source export. The first source line is intentionally ignored.

## Issues found

| Class | Severity | What | Evidence | Proposed action |
| --- | --- | --- | --- | --- |
| Completeness | Medium | An exact full page could not prove that no later rows were omitted. | The final parser requires a short terminal page; SQL emits an empty terminal page after an exact multiple. Tests cover multi-page catalogs and missing terminal evidence. | Resolved in the page protocol and tests. |
| Correctness | Medium | A wildcard object-grant prefix needed to include every selected object name. | Fixture and regression tests cover `*`; the initial matching path treated it as a literal prefix. | Resolved by treating `*` as an all-name match. |
| SQL catalog | Medium | Stored source is exposed as Oracle `LONG`, so hashing it directly in SQL is not portable. | Static SQL review and tests cover fetched source-line hashing, excluding line 1. Live Oracle execution was unavailable. | Resolved in the capture code; qualify against supported Oracle releases before operational use. |
| Scope safety | Medium | A configured DBA alias could point at a different database or container than the base profile. | The merge path checks database, container, selected schema, and edition identities; fixtures exercise a scope mismatch refusal. | Resolved with fail-closed identity matching. |
| DBA script | Medium | System-privilege and role differences also need additive grants in the generated review script. | Tests cover system grants, role grants, grant-option upgrades, ACEs, and ORDS enablement. | Resolved; review generated SQL and target identity before using it in a rollout. |
| Offline verification | Informational | Oracle dictionary/ORDS variation and PowerShell runtime behavior were not exercised. | The requested environment is offline; tests used recorded fixtures and fake SQLcl, with PowerShell-dependent cases skipped. | Qualify catalog SQL and Windows wrappers in their respective supported environments. |
