# Dated migration folders and live schema comparison

Date: 2026-09-27
Status: Written specification; implementation is not started.

## Purpose and accepted decisions

Replace flat per-developer migrations with dated migration folders containing ordered SQL files. Support verified deployment to DEV, staging, and production, and compare selected live objects between environments.

Each developer has an independent repository. Repositories share the development Oracle database and APEX workspace, never downstream Git history or commits. Migration names, revisions, source, and receipts are local to each repository. No feature may depend on discovering another developer's files.

The human selected:

- No developer component in migration paths or migration identifiers.
- Multiple sequential, named SQL files inside each migration folder.
- Incremental, immutable revision folders for changes to the same intent.
- Approach A: local analysis plus live catalog preflight, with its detection limitations.
- Explicit `STAGING_SCHEMA` and `PROD_SCHEMA`, reusing existing target connections and expected users.
- Object-list or prefix/pattern selection for comparison; this specification chooses catalog comparison supplemented by normalized metadata DDL.
- A `YYYY-MM-DD_` prefix on every migration folder for newest-first display.

The user requested both this specification and its implementation plan. Creating these documents does not authorize code implementation, database writes, exports, commits, or pushes.

## Global constraints

- Independent developer repositories share database state, not migration history or commits.
- The live database is authoritative for current state; local receipts are historical evidence only.
- No custom team metadata tables, mutexes, checkout rosters, or database-backed migration ledgers.
- Python 3.10+ standard library only; preserve Bash and PowerShell 5.1 entry-point compatibility.
- Keep SQL, JSON, Bash, and PowerShell source in LF line endings.
- Never store credentials in configuration, source, receipts, reports, or logs; use SQLcl saved connection names.
- No silent commit, push, migration, APEX import, or export.
- Unavailable, incomplete, unsupported, or ambiguous checks must not be reported as passed.

## 1. Folder, ordering, and revision contract

```text
migrations/
    2026-09-28_create-customers-r002/
        001-add-status-column.sql
        002-update-view.sql
        checks.json
    2026-09-27_create-customers-r001/
        001-create-table.sql
        002-create-indexes.sql
        003-create-view.sql
        checks.json
        status.dev.json
        status.staging.json
```

### Naming and display

- Folder format: `YYYY-MM-DD_<migration-name>-rNNN`.
- Validate the prefix as a real calendar date, not only a regular expression.
- The date is the folder's creation date. It is retained when promoting to another environment; it is not the apply date.
- Migration names are lowercase kebab-case: `[a-z][a-z0-9]*(?:-[a-z0-9]+)*`.
- Revisions are `r001` through `r999`. Revision identity is `(migration-name, revision)` within the local repository, regardless of date. Duplicate local identities with different dates are rejected.
- Increase revisions sequentially for the same local migration family; do not reuse a revision. A family starts at `r001` and keeps its earlier folders.
- Display folders by descending name. The ISO date prefix makes dates sort newest first under that ordering. A filesystem/browser configured for ascending sorting will still show oldest first; naming cannot control its settings.
- Same-date folders have a deterministic name tie-break, not a guaranteed intraday creation order. No timestamp or global counter is introduced.

### SQL sequence and execution order

- SQL filename format: `NNN-<descriptive-name>.sql`, using the same kebab-case name grammar.
- Sequences are unique and consecutive from `001`, with a maximum of `999` files. Empty migrations, gaps, zero, duplicate sequences, and unnumbered root SQL files are rejected before connecting.
- Only immediate root SQL files are executable. Nested SQL includes and SQLcl client commands remain prohibited. `checks.json`, receipts, and an optional human `README.md` are not executed.
- Validate every file and every selected folder before the first connection. Reject traversal, symlinks/reparse points, out-of-repository paths, invalid encoding, and CRLF SQL/JSON.
- Multiple selected folders execute in the caller's explicit order. Never auto-apply every folder, sort execution newest first, or infer cross-family dependencies from dates.
- Within a folder, execute files in ascending numeric order in one apply session. Driver-owned substitution and transaction handling remain in force.
- If multiple revisions of one family are selected, reject a descending revision order. Live preconditions establish prerequisites when an earlier revision is not selected; local receipts cannot prove shared history.

### Meaning of revisions and immutability

`2026-09-27_create-customers-r001` and `2026-09-28_create-customers-r002` share a local intent/family but are separate executable migrations. `r002` contains incremental follow-up changes; it is not a rewritten full replacement of `r001`. Its file sequence starts again at `001`.

After any step successfully applies anywhere, freeze SQL bytes, filenames, order, and `checks.json`. Later environment receipts may be added. A partially applied migration is also frozen. Recovery changes belong in a new reviewed revision/folder.

Local receipt hashes and retained run evidence detect accidental edits. There is no globally enforceable immutability or unique identity across independent repositories without shared history. Two repositories can legitimately contain the same folder name and different payloads; do not use a folder name as proof of shared content.

## 2. Verification specification and receipts

Every folder requires `checks.json` so arbitrary SQL/PLSQL does not acquire an invented verification result. This is local migration content, not team coordination metadata.

Contract version 1:

- `schemaVersion`: integer `1`.
- `preconditions`: array of checks against the initial live state of the folder.
- `postconditions`: nonempty array of checks against committed state after every SQL file has completed.
- Each check has a unique kebab-case `id`, one `sql` query, and `expected: 1`.
- Each query must return exactly one row and one numeric column equal to `1`; other shapes, null, missing results, or errors fail verification.
- Queries use the mapped target as current schema and may bind `:target_schema` when inspecting catalog owners. Do not embed a DEV owner that would invalidate promotion.
- Queries are restricted to a validated read-only SELECT/WITH-SELECT subset. Reject SQLcl commands, multiple statements, DML/DDL, procedural blocks, `FOR UPDATE`, sequence value access, database links, and calls to user-defined or side-effecting package functions. The initial pure built-in allowlist is COUNT, MIN, MAX, SUM, AVG, LENGTH, UPPER, LOWER, SUBSTR, NVL, COALESCE, CAST, TO_CHAR, TO_NUMBER, and REGEXP_LIKE. SYS_CONTEXT is allowed only for literal USERENV attributes used by the identity contract. Unsupported queries fail before apply.
- Run check queries in a read-only transaction. Do not claim that arbitrary SELECT text is safe merely because it starts with SELECT.
- Checks must cover every intended effect. Supported structural operations also receive generated catalog assertions; data migrations and opaque operations require explicit reviewed checks. If coverage cannot be established, refuse automated apply rather than calling it verified.

Postconditions check final folder state, so temporary objects deliberately created and removed within a folder need not remain present. The runner reports coverage and checks; it does not claim to prove all behavior of arbitrary PL/SQL.

Write `status.<env>.json` only after the real apply session exits successfully following commit and a fresh connection verifies final state and target identity. Never create pending, failed, planned, skipped, or placeholder status JSONs.

Receipt version 1 contains:

- `schemaVersion: 1`, `state: "verified"`, environment, exact folder name, creation date, family, and revision.
- Ordered SQL filename/sequence/SHA-256 entries, the SHA-256 of `checks.json`, and a payload digest.
- Payload digest definition: SHA-256 of UTF-8 canonical JSON containing `files` (ordered objects with `name`, `sequence`, `sha256`) and `checksSha256`, serialized with sorted keys and separators `(',', ':')`. Hash exact validated LF bytes; do not normalize content while applying.
- Saved connection alias and observed session user, current schema, database name, DB_UNIQUE_NAME, service, container ID/name, and current edition. No developer-name field or credential material.
- UTC apply start/completion and verification completion times; check IDs/results; verifier and normalization format versions.

Freeze validated source into a private run directory and execute that exact payload. Before the first write, confirm source hashes still match. Verify and record the staged payload's hashes so an edit during execution cannot change what the receipt describes.

Write a receipt through a temporary sibling and atomic installation without overwriting an existing receipt. Validate any existing receipt before execution. A matching target receipt blocks replay and directs the operator to read-only verification; a different digest, target, malformed receipt, or missing recorded evidence requires reconciliation. Receipt presence never substitutes for live checks.

Logs and run evidence stay in ignored `scratch/` directories. Retain run directories after an attempted write so partial execution, failed verification, or failed receipt installation can be investigated. Do not place failure records in `status.*.json`.

Inspect retained run manifests for the same local folder before another apply. A prior attempted write without a completed receipt is ambiguous and blocks automatic replay; changed source relative to that attempted payload requires reconciliation. This is diagnostic evidence, not a cross-repository execution ledger. Missing/deleted local evidence cannot establish nonapplication. Treat a failed SQL file as potentially partially applied until live inspection establishes otherwise.

If a step fails, stop that folder and all subsequent folders. Oracle DDL may already have committed earlier effects. Rollback handles remaining uncommitted work but does not undo prior DDL. Do not create a success receipt or automatically resume/replay. If commit succeeds but verification/receipt writing fails, report `apply completed; deployment verification/receipt unavailable` and require reconciliation.

Do not reconstruct a missing receipt from shape alone. A restored database can invalidate an old receipt; a missing receipt does not prove nonapplication.

## 3. Conflict detection: accepted Approach A

Interfaces:

```text
scripts/team.sh check-conflicts <migration-folder> [...] --env dev|staging|prod
scripts/team.sh check-conflicts <migration-folder> [...] --local
```

Both routes validate selected migration content and execution order. `--local` performs no connection and clearly reports local analysis only. Live mode uses the selected schema, executes read-only queries, and reports conflicts with observed live state. No argument-free scan is labeled a cross-developer check; show usage when no folders are selected.

Analyze only selected migration operations plus their declared prerequisites. Do not permanently treat historical migration files as reservations of objects. Identity resolution must use actual target schema and Oracle namespaces, preserve quoted identifier case, and compare overlapping effects regardless of author or object kind.

Live preflight checks:

| Operation | Required evidence |
| --- | --- |
| CREATE | Relevant namespace is free; `IF NOT EXISTS` does not silently accept an unrelated existing object |
| ALTER ADD column | Expected table exists and the column is absent |
| Replace/modify/rename/drop | Explicit reviewed precondition identifies the expected existing definition |
| Later file using an earlier creation | Earlier selected operation provides the prerequisite; do not reject it against the initial snapshot |
| Opaque/dynamic/data operation | Explicit checks and disclosed analysis coverage; never silently omit it |

Initially extract CREATE TABLE/VIEW/SEQUENCE and ALTER TABLE ADD COLUMN plus their order/dependencies. Conservatively mark other valid SQL as requiring explicit verification coverage; extend support only with meaningful fixtures. A table, view, or sequence collision can cross object kinds because these share an Oracle namespace. Query other occupants in that namespace through `ALL_OBJECTS`, not only the four primary catalogs.

Before writes, check the complete selected batch, including initial-state checks. Track supported staged effects for later files/folders; use live checks at each folder boundary for prerequisites that cannot be established from the initial snapshot. Do not pretend to simulate all Oracle SQL. Unsupported dependency chains must be rejected before the first write or split into separately reviewed invocations.

Recheck the next folder's live preconditions immediately before executing it. This narrows but does not remove the check/apply race. No custom lock or ledger is introduced.

Mandatory limitation text: `This checks selected local migrations against observed live state. Other repositories' pending migrations are not visible; concurrent changes can occur after preflight.`

Alice and Bob may both pass before either creates CUSTOMERS. After the first applies, the second create should be rejected by live preflight. This is an accepted limitation, not a cross-developer prospective detection guarantee. Native database errors remain the final defense for a raced duplicate create; replacements and multi-statement failures need reconciliation.

Exit statuses: `0` complete checks passed within the reported scope; `1` observed conflict/precondition failure; `2` invalid input, unsupported/incomplete coverage, or unavailable verification. The migrate runner treats either nonzero result as a refusal to start writes.

## 4. Environment targeting and confirmed deployment

```text
scripts/team.sh migrate migrations/2026-09-27_create-customers-r001 --env dev
scripts/team.sh migrate migrations/2026-09-27_create-customers-r001 --env staging
scripts/team.sh migrate migrations/2026-09-27_create-customers-r001 --env prod
```

Require exactly one explicit `--env dev|staging|prod`; reject missing, duplicate, or unsupported environment flags. Resolve connection, expected user, schema, and classification once using this mapping:

| Environment | Saved connection | Expected user | Schema | Classification |
| --- | --- | --- | --- | --- |
| dev | CODE_SQLCL_CONNECTION | CODE_EXPECTED_USER | CODE_SCHEMA | DB_ENVIRONMENT |
| staging | STAGING_SQLCL_CONNECTION | STAGING_EXPECTED_USER | STAGING_SCHEMA | staging |
| prod | PROD_SQLCL_CONNECTION | PROD_EXPECTED_USER | PROD_SCHEMA | production |

DEV is the existing code profile. For migration writes, require its classification to be `development` or `test`; do not route a production-classified profile through `--env dev`. Preserve existing identity/name safeguards.

STAGING_SCHEMA and PROD_SCHEMA are optional global configuration keys. When present they must be nonempty uppercase Oracle identifiers and have their corresponding connection/user pair configured. Existing connection/user pairs remain valid without a schema for app deployment. Migration and comparison require all three values for a selected staging/prod environment, with no fallback to DEV.

Update both environment loaders, including removal of inherited optional variables before loading configuration. The login user may differ from the owner. Setting current schema neither grants write privileges nor establishes full metadata visibility.

Before staging/prod writes, display environment, configured alias, observed database/service/container, expected user, target schema, selected folders, ordered files, and payload digests. Read-only identity/preflight connections may happen before confirmation. Require `Migrating to STAGING. Proceed? [y/N]` or `Migrating to PROD. Proceed? [y/N]`; only affirmative input authorizes that displayed frozen batch. Decline, EOF, or noninteractive execution without a response performs no writes. No general `--force` or implicit yes option is added.

Separate identity validation from operation policy. Introduce a narrowly scoped confirmed migration-deployment path. Keep generic production sessions read-only and app deployment descriptor behavior intact. Never relabel production as DEV or globally bypass the existing production guard. Compare/preflight use read-only identity handling; apply uses migration-specific deployment handling. Do not print a contradictory READ ONLY banner in an authorized apply session.

Initial scope is one mapped migration schema per environment. Separate DEV table/code schemas are not automatically combined. Additional schema mappings require a later explicit design; app parsing schemas continue to come from app deployment descriptors.

## 5. Selected live schema comparison

```text
scripts/team.sh compare-schema --from dev --to staging --object CUSTOMERS --object ORDERS
scripts/team.sh compare-schema --from staging --to prod --pattern 'HR_*' --pattern 'GL_*'
scripts/team.sh compare-schema --env prod --pattern 'HR_*'
```

The short form means `--from dev --to prod`. Reject conflicting `--env`/`--to`, duplicate source/target flags, missing target, or identical source/target labels. Require at least one selector. Detect self-comparison using observed DB_UNIQUE_NAME, container ID/name, schema, and edition, regardless of saved connection/service aliases. Refuse when these identify the same target or are insufficient to establish distinct target scope; do not treat a different service alias as proof of another database.

### Selection

- Repeatable `--object NAME` and `--pattern PATTERN` form a union over objects from both environments. Lists supplied by a human become repeated exact selectors.
- Unquoted identifiers fold to uppercase. Double-quoted exact identifiers preserve case. `--object 'TABLE:HR_EMPLOYEES'` optionally qualifies type; include PACKAGE BODY as a distinct supported type.
- Patterns accept ordinary identifier characters and `*` only, fold to uppercase, and target ordinary uppercase names. `*` matches zero or more characters; `_` is literal. `%`, `?`, brackets, dots/schema qualification, and quoted-case patterns are rejected. Case-sensitive objects can be selected exactly.
- Match locally against a complete captured catalog instead of interpolating patterns into SQL LIKE. Thus HR_* must not match HRX_EMPLOYEES.
- Owner scope comes only from the resolved environment mapping. Cross-schema owner overrides are outside this version.
- Exact selectors absent from both environments produce NOT_FOUND; patterns matching nothing produce NO_MATCH. Either yields exit `2`, not a clean comparison. Broad `*` is allowed when explicitly requested.

### Definitions and coverage

Capture each environment independently through SQLcl into local metadata, then compare in Python. First capture complete owner inventories and establish visibility; form the selector union over those inventories, then retrieve full definitions only for selected roots and included dependents. Unselected objects do not need full DDL extraction. Recheck inventory/revision evidence around definition retrieval so changes since selection invalidate the observation. No database links, custom schema objects, DDL execution, migration execution, or receipt updates.

Initial supported selected root types: TABLE, VIEW, SEQUENCE, PACKAGE, PACKAGE BODY, PROCEDURE, FUNCTION, TRIGGER, SYNONYM, INDEX, and TYPE/TYPE BODY. Any other matched type is reported as unsupported/incomplete, never silently skipped.

Use ALL_OBJECTS for object identity/validity, ALL_TABLES and ALL_TAB_COLUMNS/ALL_TAB_COLS for relational detail, ALL_VIEWS for view inventory, ALL_SEQUENCES for stable sequence settings, and ALL_CONSTRAINTS/ALL_CONS_COLUMNS/ALL_INDEXES/ALL_IND_COLUMNS for table dependents. Use DBMS_METADATA.GET_DDL and dependent metadata retrieval to obtain complete supported definitions, avoiding truncated LONG/VARCHAR previews.

Selecting a table includes its columns, constraints, and indexes, even when their names do not match the selector. Related triggers are reported as dependents and compared when supported. Identity-generated sequences are represented through the table's identity definition instead of compared twice as anonymous standalone sequences. Selecting an index alone does not recursively select its parent table. Package selection includes its body if present. Other referenced objects do not recursively expand the comparison scope; report external references as context only.

Compare column type/owner, precision/scale, character semantics, nullability, defaults, identity/virtual/invisible properties, collation, and column order where supported. Include constraint definitions and enabled/validated state, index definitions, and invalid object status. Unsupported database-version properties or table forms yield disclosed incomplete coverage; APEX 26.1 does not imply a particular Oracle Database version.

Default normalization policy `logical-v1`:

- Map only the explicitly configured owner pair to one logical owner using token-aware handling or metadata transforms; preserve references to other schemas.
- Normalize formatting outside literals/quoted identifiers; preserve literal contents and meaningful SQL expressions.
- Exclude storage/segment/tablespace attributes, object IDs, DDL timestamps, statistics, and sequence runtime LAST_NUMBER/restart position from logical equality. Show the exclusion list in every report.
- Preserve stable sequence configuration, including increment, bounds, cycle/order/cache and supported version-specific flags.
- Reconcile generated dependent names only when the underlying definitions and relationships identify them unambiguously. Otherwise report the difference; never remove names globally.
- Security grants and application data are outside logical-v1. Every report states these exclusions. Do not describe this as complete environment equivalence.

Store raw DDL alongside normalized data locally for inspectable differences. Do not truncate extraction or use delimiter parsing that corrupts quotes/newlines/Unicode. A malformed, incomplete, or missing extraction sentinel is unknown, not empty metadata.

### Visibility and consistency

Prove complete visibility for the mapped owner before making absence claims: use an owner session or an explicitly validated enabled metadata privilege path. A visible schema or table grant alone is insufficient for full ALL_* inventory and DBMS_METADATA access. Do not grant privileges automatically.

Record identity, database version, capture start/end, normalization version, selected/dependent types, and exclusions. Capture and compare object inventories/DDL revision evidence before and after extraction. Observed concurrent DDL invalidates a clean result. These checks are best effort; separate environments are not a globally atomic snapshot. A clean result is conditional on capture intervals and no observed change.

Capture the current edition explicitly. Different editions are distinct observed scope and must be identified in the report; this version compares those observed editions without changing them or inferring all-edition equality.

Report MISSING_ON_TARGET, EXTRA_ON_TARGET, DIFFERENT_DEFINITION, INVALID_OBJECT, and UNKNOWN/UNSUPPORTED. Return `0` only for complete, valid observations matching within logical-v1; `1` for complete observations showing drift/invalidity; `2` for invalid input, selection failures, unavailable/incomplete/unstable observations. Exit `2` takes priority if both drift and incomplete coverage occur, while retaining known differences in the report.

Default output is a readable summary plus actionable definition differences. `--format json` emits one complete JSON report for automation using the same comparison result and exit code. Read-only SQLcl sessions exit with rollback and never commit.

### Migration attribution

The command's reliable output is current selected structural drift. It does not establish which file ran, identify every historical collision, automatically choose migrations, or create deployment receipts.

Do not add migration attribution heuristics to the initial implementation. Local receipts may be inspected separately as historical evidence; missing receipts mean unknown history. Identical final DDL from different migrations, manual changes that mimic a migration, later changes that replace earlier effects, data-only changes, missing colleague source, restored environments, and partial execution all defeat reliable file attribution.

DEV is a moving reference. Use `--from staging --to prod` for promotion comparisons when staging is the agreed release baseline; a DEV difference alone does not prove a failed release.

## 6. Compatibility and rollout

New folder-based commands intentionally reject legacy developer/file paths with an actionable conversion message. Do not silently auto-convert migrations or invent dates/status evidence. Provide manual byte-preserving conversion instructions; dates must be deliberately chosen and historical verified receipts must not be fabricated.

Retain the existing SQL-only validator's support for standalone-slash Oracle statements. Its permission to execute valid SQL is separate from preflight/verification coverage. Update active docs, agent instructions, both team wrappers, environment examples, tests, and template ownership coverage together when implementation occurs. Do not rewrite historical specifications/plans.

DEVELOPER_NAME remains part of the existing APEX publish-tag feature, not migration naming or receipts. Removing developer grouping from migrations does not remove that unrelated configuration.

The template upgrade must continue to leave project migration folders, checks, and receipts unowned/unmodified. New implementation scripts/tests are covered by template-owned globs. Any new user guide needs explicit template ownership.

## 7. Acceptance criteria

1. Valid dated folders list in descending display order; invalid dates, duplicate revisions, and file sequence errors fail before connecting. Display order never silently changes apply order.
2. A folder with three files executes all three in sequence; a failure in file two stops file three and subsequent folders, with no success receipt and retained evidence.
3. DEV/staging/prod route to their explicit connection/user/schema values; old app-only target pairs still load; absent migration schema never falls back.
4. Staging/prod decline or EOF performs no writes; confirmed migration uses its explicit deployment policy without weakening generic production access.
5. Existing conflicting live names/columns block apply; earlier selected creates satisfy later supported prerequisites; quoted names and shared namespaces are handled correctly.
6. Independent repos with unseen competing creates may both pass initial preflight; tests and documentation preserve this acknowledged limitation.
7. Receipts appear only after successful commit and fresh-session verification; hash mismatches, partial applies, failed verification, and failed receipt installation never produce a success claim.
8. HR_* and GL_* select the union on both environments, respecting literal underscores and finding target-only objects; unmatched exact/pattern selectors cannot pass silently.
9. Table components, view text, supported code objects, and stable sequence definitions are compared; exclusions and supported scope are visible.
10. Insufficient visibility, truncation, unsupported types, identity mismatch, concurrent changes, and unavailable SQLcl result in unknown/incomplete, never an empty clean diff.

## References and evidence

Repository basis: AGENTS.md, AGENTS.project.md, .agents/rules/project.md, migrations/README.md, scripts/check_conflicts.py, scripts/migrate.sh, scripts/team.sh, scripts/team.ps1, scripts/load_env.sh, scripts/load_env.ps1, .env.example, scripts/deploy.sh, scripts/check_db_target.sh, scripts/verify_db_access.sql, and existing migration/CLI tests.

- [Oracle COMMIT and implicit DDL commits](https://docs.oracle.com/en/database/oracle/oracle-database/26/sqlrf/COMMIT.html)
- [Oracle object names, namespaces, and quoted identifiers](https://docs.oracle.com/en/database/oracle/oracle-database/19/sqlrf/Database-Object-Names-and-Qualifiers.html)
- [DBMS_METADATA visibility and security](https://docs.oracle.com/en/database/oracle/oracle-database/26/arpls/DBMS_METADATA.html)
- [ALL_TAB_COLUMNS coverage and hidden columns](https://docs.oracle.com/en/database/oracle/oracle-database/26/refrn/ALL_TAB_COLUMNS.html)
- [ALL_SEQUENCES and runtime LAST_NUMBER](https://docs.oracle.com/en/database/oracle/oracle-database/21/refrn/ALL_SEQUENCES.html)
- [SYS_CONTEXT database/container/session identity](https://docs.oracle.com/en/database/oracle/oracle-database/19/sqlrf/SYS_CONTEXT.html)

These documents establish design constraints, not successful live checks. No live database access was performed while writing this specification.
