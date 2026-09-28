# Dated Migration Folders and Schema Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Do not delegate unless the human selects delegation or another applicable instruction explicitly authorizes it.

**Goal:** Deliver dated migration folders with ordered SQL, verified environment receipts, honest live preflight checks, and selected live schema comparison.

**Architecture:** Bash loads strict configuration and delegates orchestration to small stdlib Python modules. One SQLcl transport preserves the existing isolated-working-directory protections; shared target resolution and catalog snapshots feed preflight, verification, and schema comparison. Applying a frozen ordered payload is separate from read-only checks and from writing its receipt.

**Tech Stack:** Python 3.10+ standard library, unittest, Bash, PowerShell 5.1 wrappers, Oracle SQLcl saved connections, Oracle catalog views and DBMS_METADATA. No new runtime packages or database objects.

**Spec:** [Dated migration folders and live schema comparison](../specs/2026-09-27-migration-folders-and-schema-comparison-design.md). Read the entire spec before implementation; exact schemas, naming rules, exclusions, exit codes, and accepted limitations are contractual.

## Global Constraints

- Independent developer repositories share database state, not migration history or commits.
- The live database is authoritative for current state; local receipts are historical evidence only.
- No custom team metadata tables, mutexes, checkout rosters, or database-backed migration ledgers.
- Python 3.10+ standard library only; preserve Bash and PowerShell 5.1 entry-point compatibility.
- Keep SQL, JSON, Bash, and PowerShell source in LF line endings.
- Never store credentials in configuration, source, receipts, reports, or logs; use SQLcl saved connection names.
- No silent commit, push, migration, APEX import, or export.
- Unavailable, incomplete, unsupported, or ambiguous checks must not be reported as passed.

The original request authorized the design documents only; the user has since explicitly requested implementation of this plan. Implementation is authorized on the named branch now in use. Do not run a real database migration, APEX import, export, publish, or deployment as part of implementation; those operations still need direct authorization. A fake-SQLcl test does not prove a live database check passed. Do not commit or push unless separately requested.

## Review Focus

1. Two independent repositories use the same folder name with different SQL: identify payloads by content and target, never infer shared migration history. Task 1 and Task 7 own these tests.
2. An input changes after preflight or during execution: apply the frozen payload, reject a pre-write hash change, and bind receipts to executed bytes. Task 7 owns these tests.
3. A pattern matches objects only on the target, or matches HRX_EMPLOYEES instead of HR_EMPLOYEES: use union selection and literal underscores. Task 5 owns these tests.
4. A deployer sees some owner objects but cannot retrieve full metadata: report incomplete visibility, never missing objects or a clean diff. Task 3 owns these tests.
5. File two applies DDL and then fails, or commit succeeds but receipt installation fails: retain evidence, stop later work, and create no success receipt. Task 7 owns these tests.

## File structure and module contracts

New implementation files below are planned, not created by this document.

| File | Responsibility |
| --- | --- |
| scripts/migration_manifest.py | Dated folder discovery, ordered files, checks.json validation, exact-byte hashes, receipt validation/install |
| scripts/db_targets.py | Resolve selected environment into immutable connection/user/schema/classification |
| scripts/sqlcl_session.py and scripts/sqlcl_session.sh | Run a prepared driver through existing sqlcl_safe.sh; validate transport completion and errors |
| scripts/schema_catalog.py and scripts/schema_catalog.sql | Read-only identity/visibility/catalog extraction into complete JSON snapshots |
| scripts/schema_normalization.py | logical-v1 normalization and declared exclusions, preserving literals/quoted names |
| scripts/migration_checks.py | Validated read-only check queries and results from initial/fresh committed sessions |
| scripts/check_conflicts.py and scripts/check_conflicts.sh | Selected local ordered analysis and optional live preflight; remove developer-based grouping |
| scripts/compare_schema.py and scripts/compare_schema.sh | Union selectors, supported definition comparison, text/JSON report and exit codes |
| scripts/migrate.py, existing scripts/migrate.sh and scripts/migrate.sql | Frozen ordered apply orchestration and confirmed deployment policy |
| scripts/verify_migration_access.sql | Migration-specific identity and write-deployment policy; generic production access remains read-only |
| scripts/team.sh and scripts/team.ps1 | New command routing and help; PowerShell continues using Bash for these flows |
| scripts/load_env.sh and scripts/load_env.ps1, .env.example | Optional target schemas and configuration parity |
| docs/migration-rules.md | User-facing preflight/refusal/recovery/receipt rules, owned by the template |
| Existing active guidance and tests | Revised examples, behavioral contracts, upgrade ownership, regression coverage |

Shared Python types (define in the owning modules; import rather than duplicate):

- `MigrationFile(sequence: int, name: str, path: Path, source: bytes, sha256: str)` and `QueryCheck(id: str, sql: str, expected: int)` in migration_manifest.py.
- `Migration(folder: Path, date: str, family: str, revision: int, files: tuple[MigrationFile, ...], preconditions: tuple[QueryCheck, ...], postconditions: tuple[QueryCheck, ...], checks_source: bytes, checks_sha256: str, payload_digest: str)` in migration_manifest.py.
- `Target(environment: str, connection: str, expected_user: str, schema: str, classification: str)` in db_targets.py.
- `SqlclResult(returncode: int, output: str, run_dir: Path)` in sqlcl_session.py; transport failures raise `SqlclError` and preserve diagnostics.
- `ObjectKey(owner: str, name: str, object_type: str)`, `ObjectDefinition(key: ObjectKey, attributes: dict, raw_ddl: str, dependents: tuple[ObjectKey, ...], valid: bool)`, `SchemaInventory(identity: dict, objects: dict[ObjectKey, dict], coverage: dict, started_at: str, completed_at: str)`, and `SchemaSnapshot(identity: dict, inventory: dict[ObjectKey, dict], objects: dict[ObjectKey, ObjectDefinition], coverage: dict, started_at: str, completed_at: str)` in schema_catalog.py. An inventory proves owner visibility and includes type/validity/revision evidence; a snapshot adds definitions only for selected roots/dependents. Only complete requested-scope snapshots are returned; incomplete captures raise `CatalogError` with partial observations/reasons.
- `CheckReport(results: tuple[dict, ...], passed: bool)` in migration_checks.py; each result identifies its check and observed shape/value.
- `PreflightReport(conflicts: tuple[dict, ...], coverage: dict, exit_code: int)` in check_conflicts.py.
- `Selection(keys: tuple[tuple[str, str], ...], errors: tuple[dict, ...])` and `ComparisonReport(source: dict, target: dict, selection: dict, coverage: dict, differences: tuple[dict, ...], exit_code: int)` in compare_schema.py. Logical keys are exact `(name, object_type)` after owner mapping; retain original owners in observations.
- Receipt serialization follows spec section 2; format/version constants live in migration_manifest.py. No module interprets a receipt as authoritative live state.

Tests use unittest and controlled temporary checkouts. Build the fake SQLcl fixture to distinguish identity, snapshot, checks, apply, commit, and fresh verification sessions; a single unconditional completion marker must no longer pass migration tests.

## Task 1: Dated folder and immutable payload contracts

**Files:** Create scripts/migration_manifest.py and tests/test_migration_manifest.py; preserve scripts/validate_migration.py as the SQL-only validator, modifying it only if a tested public helper is needed.

**Interfaces:** `load_migration(repo_root: Path, relative_folder: str) -> Migration`; `load_batch(repo_root: Path, relative_folders: Sequence[str]) -> tuple[Migration, ...]`; `list_migration_folders(repo_root: Path) -> tuple[Path, ...]` in descending display order; `validate_check_query(sql: str) -> None`; `validate_receipt(path: Path, migration: Migration, target_identity: Mapping[str, str]) -> dict`; `install_receipt(path: Path, receipt: dict) -> None` with no replacement of existing files. Receipt validation takes a mapping to avoid requiring Task 2's Target type while implementing Task 1.

- [ ] Write `test_iso_dates_sort_newest_first_without_reordering_batch`, asserting descending discovery for 2026-09-28/2026-09-27 and caller-preserved batch order; verify same-day ties are deterministic rather than called chronological.
- [ ] Write `test_sequences_are_contiguous_and_named`, asserting file names `001-create-table.sql`, `002-create-indexes.sql`, `003-create-view.sql` produce sequences `(1, 2, 3)`. Reject 000, gaps, duplicate numeric prefixes, nested/unnumbered SQL, no files, invalid dates such as 2026-02-30, and revision zero.
  Pin: `self.assertEqual(tuple(file.sequence for file in migration.files), (1, 2, 3))`.
- [ ] Write path/identity cases: symlinks/reparse points, traversal, CRLF/invalid UTF-8, duplicate family/revision under different dates, missing r001/gaps, and reversed selected family revisions. Legacy developer/file paths receive an actionable conversion error without connecting.
- [ ] Write checks.json tests for required postconditions, unique IDs, expected value 1, and the read-only SQL subset. Reject SQLcl text, multi-statements, FOR UPDATE, NEXTVAL/CURRVAL, procedural/DML/DDL, database links, and custom/side-effecting function calls; allow catalog/table SELECT and WITH-SELECT with tested pure built-ins.
- [ ] Write `test_same_folder_name_in_independent_roots_has_distinct_digest`, `test_file_rename_or_checks_change_changes_digest`, and receipt cases for malformed version, target mismatch, changed payload, and no-overwrite installation. Use the spec's canonical JSON digest algorithm exactly.
- [ ] Run `python3 -m unittest discover -s tests -p 'test_migration_manifest.py' -v`; confirm the tests fail before implementing missing interfaces.
- [ ] Implement the contracts and errors in migration_manifest.py. Stage bytes in memory as part of Migration; validate every selected file with the existing SQL-only validator before any later task opens SQLcl.
- [ ] Re-run the targeted suite and existing `test_validate_migration.py`; expect all pass. Review the diff; no commit unless requested.

## Task 2: Target resolution and environment-loader parity

**Files:** Create scripts/db_targets.py and tests/test_db_targets.py; modify scripts/load_env.sh, scripts/load_env.ps1, .env.example, and tests/test_team_cli.py.

**Interfaces:** `resolve_target(values: Mapping[str, str], environment: str, operation: str) -> Target`; operation is `read` or `migration`. Wrappers must load strict configuration first; never treat ambient unvalidated process values as a replacement .env.

- [ ] Write `test_staging_owner_is_independent_of_login_and_dev_owner`: STAGING_SQLCL_CONNECTION=stage-db, STAGING_EXPECTED_USER=STAGE_DEPLOYER, STAGING_SCHEMA=APP_STAGE, CODE_SCHEMA=APP_DEV must resolve exactly to that staging triple and classification staging.
  Pin: `self.assertEqual((target.connection, target.expected_user, target.schema, target.classification), ("stage-db", "STAGE_DEPLOYER", "APP_STAGE", "staging"))`.
- [ ] Add the analogous PROD mapping, DEV mapping, missing/blank/malformed schema, unsupported environment, and DEV write classification tests. Assert absent target schema never resolves to CODE_SCHEMA.
- [ ] Extend Bash/PowerShell loader fixtures: old target connection/user pairs load without schema; schema with no pair is rejected; inherited optional schema values are cleared; schema identifiers accept supported `$`/`#` characters literally. Preserve publish-tag DEVELOPER_NAME validation.
- [ ] Run `python3 -m unittest discover -s tests -p 'test_db_targets.py' -v` and the new relevant TeamCliTests cases; confirm the new behavior fails initially.
- [ ] Implement resolve_target and both loader changes. Add commented STAGING_SCHEMA/PROD_SCHEMA examples explaining owner versus login. Keep schema requirement specific to migration/comparison, not all app deploys.
- [ ] Run the two targeted suites. Execute PowerShell parity tests when pwsh is available; report an unavailable runtime as skipped/unavailable, not passed. Review the diff.

## Task 3: SQLcl transport and complete read-only snapshots

**Files:** Create scripts/sqlcl_session.py, scripts/sqlcl_session.sh, scripts/schema_catalog.py, scripts/schema_catalog.sql, tests/test_schema_catalog.py, and tests/fixtures/schema_catalog/*.json; reuse scripts/sqlcl_safe.sh. Modify tests/test_sql_driver_contracts.py to include new read-only drivers.

**Interfaces:** `run_sqlcl(target: Target, driver_path: Path, run_dir: Path) -> SqlclResult`; `capture_inventory(target: Target, run_dir: Path) -> SchemaInventory`; `capture_snapshot(target: Target, inventory: SchemaInventory, keys: Sequence[tuple[str, str]], run_dir: Path) -> SchemaSnapshot`; `parse_snapshot(output: str, target: Target) -> SchemaSnapshot`. Capture complete owner inventory before selectors so target-only matches and visibility failures cannot be hidden by filtering; retrieve full definitions only for selected roots and included dependents, verifying identity/inventory revisions against the discovery capture.

- [ ] Write fixtures for owner-session success, authorized metadata-reader success, schema-visible-but-incomplete deployer, wrong session user/schema/identity, unsupported DB capabilities, missing end marker, truncated CLOB, Unicode/newlines, and concurrent DDL between capture boundaries.
- [ ] Write `test_partial_catalog_visibility_never_becomes_empty_success` and transport tests asserting isolated cwd/SQLPATH/ORACLE_PATH, empty stdin, timeout/error diagnostics, no caller login.sql execution, and no unsafe shell interpolation. Force a metadata error for a selected object and expect CatalogError with known observations retained; an unselected unsupported object must not trigger full DDL extraction or invalidate otherwise complete selected coverage.
  Pin: `with self.assertRaises(CatalogError): parse_snapshot(partial_output, target)`.
- [ ] Write extraction tests requiring full DDL, table component relationships, identity-generated dependents, package/body relationships, and before/after inventory evidence. Ensure JSON strings preserve quoted names and expressions containing literal newlines/punctuation.
- [ ] Run `python3 -m unittest discover -s tests -p 'test_schema_catalog.py' -v`; confirm failure before implementing the transport and parser.
- [ ] Implement the trusted Bash bridge to sqlcl_safe.sh, argv-based Python calls, timeout handling, stable nonzero SQLcl errors, and fully framed JSON extraction. SQL uses catalog SELECT/metadata retrieval only and exits with rollback. Session settings and DBMS_METADATA transforms affect only extraction, not database objects.
- [ ] Implement identity/full-visibility checks and capability reporting. Do not assume Oracle Database 26 from APEX 26.1. A missing catalog/property needed for complete supported comparison is incomplete, not a silently omitted field.
  Identity fixtures include DB_UNIQUE_NAME, container ID/name, edition, and two service aliases for the same database/schema; do not equate service aliases with distinct databases.
- [ ] Re-run schema_catalog and SQL-driver contract tests; expect pass. Do not run a live apply. If a user authorizes a read-only smoke check later, record actual database/version/visibility results separately.

## Task 4: Logical definition normalization

**Files:** Create scripts/schema_normalization.py and tests/test_schema_normalization.py; extend tests/fixtures/schema_catalog/*.json with normalization examples.

**Interfaces:** `normalize_definition(definition: ObjectDefinition, target_owner: str) -> dict`; `normalization_coverage() -> dict` returns version logical-v1, supported types/properties, and exact exclusions. Both preflight and comparison reuse these definitions where needed.

- [ ] Write `test_owner_mapping_preserves_literals_and_external_owners`: APP_DEV.T versus APP_STAGE.T maps to one logical owner, while string literals containing APP_DEV and references to OTHER_SCHEMA stay distinct. Quoted mixed-case names remain case-sensitive.
- [ ] Write equality cases for formatting/storage/tablespace differences and generated identity sequence names; difference cases for column precision, character semantics, defaults, nullability, virtual/invisible/identity attributes, column order, constraints enabled/validated, indexes, trigger text, package body text, and stable sequence flags.
- [ ] Write `test_runtime_sequence_position_is_excluded_but_increment_is_not` and `test_ambiguous_generated_names_are_not_erased`. Assert raw DDL remains available and unsupported forms are incomplete.
  Pin equality for LAST_NUMBER-only changes with `self.assertEqual(normalize_definition(source_sequence, "APP_DEV"), normalize_definition(target_sequence, "APP_STAGE"))`; changing increment must make the same comparison unequal.
- [ ] Run `python3 -m unittest discover -s tests -p 'test_schema_normalization.py' -v`; confirm initial failure.
- [ ] Implement structured attribute comparison and token-aware DDL normalization. Use transforms/token processing rather than global regex replacement of owner names or SQL whitespace. Publish the logical-v1 exclusions from the spec.
- [ ] Re-run the targeted suite; expect pass. Review normalization fixtures for false equality before moving on.

## Task 5: Object/pattern comparison command

**Files:** Create scripts/compare_schema.py, scripts/compare_schema.sh, tests/test_compare_schema.py, and tests/test_compare_schema_cli.py. Team wrapper wiring is completed in Task 8.

**Interfaces:** `select_objects(source: SchemaInventory, target: SchemaInventory, objects: Sequence[str], patterns: Sequence[str]) -> Selection`; `compare_snapshots(source: SchemaSnapshot, target: SchemaSnapshot, selection: Selection) -> ComparisonReport`; `render_report(report: ComparisonReport, output_format: str) -> str`; `main(argv: Sequence[str] | None = None) -> int`.

- [ ] Write `test_patterns_use_union_and_literal_underscore`: HR_EMPLOYEES only in target is EXTRA_ON_TARGET, HR_DEPARTMENTS only in source is MISSING_ON_TARGET, and HRX_EMPLOYEES is not selected by HR_*; GL_* combines with HR_* without duplicate entries.
  Pin: `self.assertNotIn(("HRX_EMPLOYEES", "TABLE"), selection.keys)` and `self.assertIn(("HR_EMPLOYEES", "TABLE"), selection.keys)`.
- [ ] Write exact/quoted/type-qualified selector cases, unsupported pattern characters, nonexistent exact names, unmatched patterns, target-only selectors, package/body inclusion, table components with nonmatching names, and external-reference nonexpansion. Unsupported matched types return exit 2.
- [ ] Write CLI cases for `--from dev --to staging`, `--from staging --to prod`, and `--env prod`; reject duplicate/conflicting flags, no selectors, equal labels, and equal observed database/schema identities. Assert absent config/incomplete snapshots cannot yield exit 0.
- [ ] Write text/JSON output assertions for five difference classes, identity/capture windows, coverage/exclusions, and exit priority: drift=1, incomplete=2 even with known drift. Assert no receipt writes or apply calls; read-only sessions rollback.
- [ ] Run both new suites with unittest discovery; confirm failure before implementation.
- [ ] Implement selector union over complete inventories, selected-only definition capture with normalization/dependent expansion through Tasks 3-4, and deterministic report rendering. Do not implement migration attribution or apply automation.
- [ ] Re-run both suites; expect pass. Inspect representative missing-column, changed-view, changed-code, target-only table, and sequence reports for usefulness.

## Task 6: Explicit checks and honest ordered preflight

**Files:** Create scripts/migration_checks.py, scripts/check_conflicts.sh, tests/test_migration_checks.py; modify scripts/check_conflicts.py and tests/test_check_conflicts.py.

**Interfaces:** `run_checks(target: Target, checks: Sequence[QueryCheck], run_dir: Path) -> CheckReport`; `analyze_batch(migrations: Sequence[Migration], target_schema: str) -> tuple[dict, ...]`; `preflight(migrations: Sequence[Migration], snapshot: SchemaSnapshot, checks: CheckReport) -> PreflightReport`; main supports selected folders plus exactly one of `--env` or `--local`.

- [ ] Replace developer-based fixtures with dated folder fixtures. Write `test_table_and_sequence_share_namespace`, `test_quoted_identifiers_preserve_case`, and explicit owner-resolution cases. Other namespace occupants in ALL_OBJECTS must also block a create.
- [ ] Write ordered dependency cases for create-table then create-index/view/add-column, deliberate later replacement, duplicate creates in one selected batch, and historically unrelated local folders excluded from selection. Reject dependency chains the analyzer cannot safely establish before writes.
- [ ] Write check result tests: one numeric 1 succeeds; zero/null/multiple rows/multiple columns/error fails. Checks run in read-only transactions and cannot alter schema or receipts.
  Pin for a zero result: `self.assertFalse(report.passed)`; a complete observed precondition conflict pins `self.assertEqual(preflight_report.exit_code, 1)`.
- [ ] Write `test_independent_pending_creates_can_both_pass_empty_live_catalog`, preserving the selected limitation. With a live CUSTOMERS object, the second CREATE fails. `--local` labels scope explicitly; live mode prints the spec's mandatory limitation text.
- [ ] Write unsupported-operation coverage tests requiring explicit reviewed checks and refusing incomplete coverage. `IF NOT EXISTS` must not silently accept conflicting existing definitions. No-argument invocation gives usage instead of the former cross-developer clean message.
- [ ] Run check_conflicts and migration_checks targeted suites; confirm failures before changes.
- [ ] Implement source parsing with safe literal/comment handling, schema/namespace-aware keys, ordered supported effects, and disclosed coverage. Keep it a conservative analyzer, not a full SQL simulator. Live mode uses Tasks 2-4 and checks; local mode requires no .env or SQLcl.
- [ ] Re-run both suites and existing validator tests; expect pass. Confirm unknown/unavailable results use exit 2 and observed conflicts use exit 1.

## Task 7: Frozen multi-file apply and verified receipts

**Files:** Create scripts/migrate.py and scripts/verify_migration_access.sql; modify scripts/migrate.sh and scripts/migrate.sql; rewrite/extend tests/test_migrate_cli.py and tests/test_sql_driver_contracts.py; add tests/test_migration_receipts.py. Preserve generic scripts/check_db_target.sh/.ps1 and scripts/verify_db_access.sql policy unless a tested shared pure identity helper is extracted without changing their behavior.

**Interfaces:** `apply_batch(repo_root: Path, migrations: Sequence[Migration], target: Target, confirm: Callable[[str], bool]) -> int`; `apply_folder(migration: Migration, target: Target, run_dir: Path) -> dict` returns execution evidence only, not a receipt; `build_receipt(migration: Migration, target: Target, apply_evidence: dict, verification_snapshot: SchemaSnapshot, checks: CheckReport) -> dict` requires completed commit and successful fresh checks.

- [ ] Build a fake SQLcl integration harness that records ordered includes and distinguishes preflight, apply, commit completion, and fresh verification. Retain the current SQL-only/client-command rejection fixtures while changing their paths to dated folders.
- [ ] Write DEV/staging/prod routing tests with different owners/login accounts; reject missing/duplicate env, missing target schema, and production-classified DEV writes. Decline/EOF staging/prod confirmation may make read-only calls but must make zero apply calls.
- [ ] Write `test_second_file_failure_stops_third_and_next_folder_without_receipt`, plus failed commit, failed postconditions, truncated verification, changed observed target identity, and receipt filesystem failure. Assert retained evidence and nonzero status; never pretend DDL rollback restores prior state.
- [ ] Write successful receipt assertions for exact ordered names/hashes, checks hash, payload digest, target observations, UTC times, format versions, and absence of developer-name/credential fields. Fresh verification must occur after successful committed apply.
- [ ] Write replay/immutability cases: matching receipt blocks reapply, malformed/mismatched receipt refuses, source mutation before writes refuses, source mutation during apply cannot change staged bytes or recorded payload, and absent receipts with matching live shape do not cause automatic receipt fabrication.
  Include retained prior-attempt evidence without a receipt: reject replay even when the prior SQL file never emitted a completion marker, because it may have partially applied. Pin: `self.assertFalse((folder / "status.dev.json").exists())` and assert no additional apply calls.
- [ ] Write the same-named/different-payload independent-repo integration case and a raced live create case; assert conflicts are based on observed state and receipt hashes, not global folder identity.
- [ ] Run migrate_cli, migration_receipts, and SQL-driver suites; confirm failures before implementation.
- [ ] Implement wrappers, frozen staging, complete pre-write batch validation/preflight, per-folder live rechecks, driver-owned SQL substitution, ascending file execution, stable error markers, commit evidence, and fresh read-only verification. Stop all subsequent work on ambiguity/failure.
- [ ] Implement the migration-specific confirmed production identity policy. Retain generic production read-only tests and APEX deployment regressions; never print generic READ ONLY instructions in a confirmed apply session. A read-only snapshot/check is not evidence of write privileges.
- [ ] Install success receipts only through Task 1's atomic no-overwrite path. Keep partial/run logs under ignored scratch, not status JSON. Re-run the targeted suites and existing publish/deploy tests; expect pass.

## Task 8: CLI integration, guidance, and final verification

**Files:** Modify scripts/team.sh, scripts/team.ps1, AGENTS.md, CLAUDE.md, README.md, migrations/README.md, .agents/rules/agent-safety.md, .agents/workflows/team-flow.md, app_context/README.md, template-manifest.json, tests/test_team_cli.py, tests/test_documentation_contract.py, tests/test_template_manifest.py, tests/test_upgrade_template.py; create docs/migration-rules.md. Search active guidance for additional stale flat-path examples; do not rewrite historical docs/superpowers documents.

**Interfaces:** team.sh and team.ps1 expose the exact spec command forms and preserve exit codes. PowerShell routes migration, preflight, and comparison to their Bash wrappers using its existing argument-array approach. New guides are template-owned; project migration payload/check/receipt paths remain unowned.

- [ ] Write help/routing tests for dated folder migrate, explicit env, check-conflicts --local/live, compare-schema object/pattern/from/to/env/format flags, and legacy input conversion errors. Update the existing argument-free conflict test to the explicit no-connection --local fixture.
  Pin help with `self.assertIn("compare-schema", result.stdout)` and assert each routed nonzero result retains the underlying command's exit code.
- [ ] Update documentation assertions to remove developer-based migration guidance and add descending display versus explicit apply order, partial failure recovery, independent-repo limits, target schema mapping, real verification receipts, and selected comparison scope/exclusions. Keep APEX developer publish tags and descriptor rules intact.
- [ ] Extend template ownership and upgrade fixtures: docs/migration-rules.md is owned, while migrations/2026-09-27_create-customers-r001/{001-create-table.sql,checks.json,status.dev.json} cannot be overwritten by upgrade. Preserve existing legacy-data protection tests for old projects too.
- [ ] Run targeted team/documentation/template/upgrade tests and confirm new contracts fail before wrapper/docs updates.
- [ ] Implement wrapper routing/help, user guide, manual byte-preserving conversion instructions, agent rules, and template ownership. Documentation must explicitly say names cannot force the file browser's sort direction and same-day order is not intraday creation order.
- [ ] Run `python3 -m unittest discover -s tests -v`; expect all available tests pass with explicit skips for unavailable optional runtimes. Run `git diff --check` and Bash syntax checks on changed/new .sh files. Run Python lint with the configured rules if the lint tool is available; report unavailability honestly.
- [ ] Review for accidental connection/owner fallback, global migration identities, fabricated status, lost partial evidence, unsupported clean diffs, and weakened generic production rules. Record coverage against spec acceptance criteria 1-10.
- [ ] If the human separately authorizes real integration checks, use disposable schemas for DDL apply and independently scoped read-only catalog comparison. Include a three-file migration, partial failure, changed column/view, target-only HR_* object, and incomplete-visibility account; record actual results. Otherwise label live integration unverified and do not run it.
- [ ] Present the final diff, tests, material limitations, and any unavailable live verification. Commit/push only on an explicit instruction; do not merge or deploy as part of finishing this plan.

## Execution handoff

Implement tasks sequentially because catalog, normalization, preflight, and receipt interfaces are shared. Each task gets its targeted failing/passing test cycle before moving on. The final suite verifies integration once; repeat/broaden checks only when changes or failures justify it.

Execution method: native execution in this session, selected by the user's direct instruction to implement. The plan's user review is complete. A fresh whole-branch review remains required before considering the work complete.
