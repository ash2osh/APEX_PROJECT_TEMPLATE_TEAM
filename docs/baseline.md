# Baseline export and migration generation

`baseline` captures configured schema source, grants, and opted-in reference
rows through the template's read-only SQLcl catalog session, then builds
reviewable migration folders by comparing two configured environments. It
never applies those folders.

## Configuration

Copy [the example](baseline.example.json) to `baseline.json` in the project
root, then set the actual schema names, prefixes, exclusions, grant policy,
sequence mappings, and reference tables. Keep `baseline.json` with the project
configuration; do not put credentials in it. Commands resolve connection
aliases and expected users from the existing `.env` profiles.

`schemas` lists the schema owners to process. `PROJECT_SCHEMA`, when set,
selects one of those schemas; otherwise export commands use the full configured
list. `export-source`, `export-grants`, and `export-data` do not accept
`--schema`. `baseline build` accepts `--schema` to select one configured
schema. `prefixes` selects matching object names; an empty list selects every
name. `excludedObjects` omits exact object names after the prefix match. Both
filters apply to source, settings, views, structure, and object grants.

`grants.skipGrantees` removes grantees, `grants.includeGrantees` optionally
limits output to that list, and `grants.keepGrantOptions` controls whether
object `WITH GRANT OPTION` and system privilege `ADMIN OPTION` are retained.
System privileges are exported for the schema owner. Object grants use the
ordinary configured schema connection; system privileges require the matching
`<ENV>_DBA_SQLCL_CONNECTION`. The exporter checks that both connections reach
the same database, container, schema, and edition.

`sequenceMappings` connects an application sequence with the table and column
that hold its seeded identifiers. Each selected conventional sequence needs a
mapping so the generated migration can advance it past the target's current
maximum ID. Identity columns use Oracle's `START WITH LIMIT VALUE` support.

Each `referenceData.tables` entry must declare:

- `name`: an uppercase table name in the selected schema.
- `excludeColumns`: columns that must never leave the source database, such as
  password hashes, API or refresh tokens, internal notes, or environment-local
  values. Excluded columns are removed from the SQL projection and do not
  appear in the JSON export or generated SQL.
- `keyColumns`: the stable natural key used to decide whether a row needs an
  insert. Do not use an identity column as a natural key.
- `labelColumns`: one or more readable columns used to match reference rows
  across environments and resolve foreign keys.
- `identity`: `null` for a table without an identity, or an object naming the
  identity `column` and its `generationType` (`ALWAYS`, `BY DEFAULT`, or
  `BY DEFAULT ON NULL`).
- `rowLimit`: a per-table limit from 1 to 100,000. Exceeding it fails loudly;
  no partial export is written.

## Export stored source, grants, and reference rows

```bash
scripts/team.sh baseline export-source --from dev
scripts/team.sh baseline export-grants --from dev
scripts/team.sh baseline export-data --from dev
```

By default, source and grant output goes under
`scratch/baseline/<environment>/<schema>/`. Source units are separate UTF-8
JSON files under `stored-source/`; each stores the configured owner's
`ALL_SOURCE.TEXT` rows as returned, without adding or trimming newlines.
`settings.json` contains the corresponding `ALL_PLSQL_OBJECT_SETTINGS` rows.
Each view JSON preserves the exact `ALL_VIEWS.TEXT` value and also carries
`DBMS_METADATA.GET_DDL` output for later executable view migrations. Grant
output is under `grants/` and keeps object grantable flags and system privilege
admin-option flags. The per-schema `source-manifest.json` records the database
release observed in the same guarded catalog session. It does not infer or
record an APEX release; application exports capture APEX release provenance
through their own guarded APEX session.

Catalog sections are captured in sorted pages of 500 rows, with a 100,000-row
cap and completeness evidence. Reference rows use the same read-only
transaction, are ordered by natural key, and are paged per table. Output goes
to `scratch/baseline/<environment>/data/<schema>/<table>.json` and records
permitted column metadata, identity information, page counts, and rows. A
missing page, unreadable catalog view, connection failure, row-count change,
or cap hit stops the export. `--scratch` changes the output root. Scratch files
are local artifacts and should be reviewed before moving needed JSON into
durable project storage. Oracle numeric columns are kept as exact decimal JSON
number tokens, avoiding binary-float rounding during capture and build.

## Build migration folders

```bash
scripts/team.sh baseline build --from dev --to staging
scripts/team.sh baseline build --to prod --schema APP_PROD
scripts/team.sh baseline build --from dev --to staging --data
scripts/team.sh baseline build --from dev --to staging --bundle customer-status
scripts/team.sh baseline build --from dev --to staging --bundle customer-status --data
```

`--from` defaults to `dev`. `build` captures complete catalogs, compares the
selected environments by object name, and writes new folders under
`migrations/<TARGET_SCHEMA>/`. Source and target must be complete, and the
builder refuses the same observed database/container/schema/edition scope even
when two configured service aliases differ. A schema's baseline folders cover
only that repository's selected observations; generation does not discover
pending migrations in other independent repositories.

Without `--bundle`, a build creates up to three separate structure, grants and
code families. Use `--bundle <name>` to put generated structure, grants, and code in that order
in one `baseline-bundle-<name>` folder. The name must be
an ASCII lowercase slug matching `[a-z][a-z0-9]*(?:-[a-z0-9]+)*` and at most
64 characters; it is validated before SQLcl starts. Bundling applies only to
the current generated components from the same complete source and target
captures. It never combines schemas, arbitrary SQL, reference data, or existing
migration folders. It creates one folder per target schema with nonempty output.

The usual three nonempty categories therefore go from 3-to-1 folders. With
`--data`, the usual four families become 4-to-2. The baseline-data folder is installed separately after the DDL bundle.
Empty categories create no folder. `--data-dir` selects
a different reference export root; by default the builder reads
`scratch/baseline/<from>/data/`.

The bundle README lists its categories, source and target, ordered original-to-generated filename mapping,
and SQL SHA-256 values. Review every
step and all checks. Oracle DDL can commit implicitly; a DDL step or later check
may fail after earlier DDL took effect. Inspect the target and use a reviewed
recovery migration; do not edit or replay an attempted or applied folder.
Identical builds preserve existing bytes and evidence, while changed inputs
create the next revision. Unknown evidence, revision gaps, or a flat/schema
layout conflict refuses generation. Staging is private and outside
`migrations/`; validation completes before exclusive installation of a final
folder.

Add `--data` to read a prior `export-data` capture and capture the configured
target tables read-only. `--data-dir` selects a different export root; by
default the builder reads `scratch/baseline/<from>/data/`. The generated
families are:

- `baseline-structure`: guarded table and sequence creates, missing columns,
  named constraints, indexes, not-null changes, and generator synchronization.
- `baseline-grants`: additive object grants grouped into steps of no more than
  100 statements or 50,000 SQL bytes.
- `baseline-code`: changed stored units with each unit's compiler settings,
  exact owner-scoped `ALL_SOURCE.TEXT` line assembly, changed views, fast
  source checks, and a bounded multi-pass compile followed by name-based
  `VALID` checks.
- `baseline-data`: natural-key-guarded inserts, target-label foreign-key
  resolution, identity-generator advancement, and count-based checks.

Plain source scripts are used when safe. A unit with a whitespace-only line or
a line ending in `-` is assembled as a CLOB to preserve the text SQLcl would
otherwise trim or continue. CLOB source chunks are at most 32 characters. The
final source line check accepts either Oracle's stored final newline or its
absence. Source checks use line counts and name-based text equality; they do
not use `IS NULL` or `NVL` on source text. Every source line, including an
empty line, is checked at its original line number. The predicate appends
`CHR(1)` before equality so Oracle's empty-string-is-NULL rule does not turn a
blank-line check into an `IS NULL` or `NVL` test. `ALL_SOURCE` is filtered by
the configured owner so it works when the saved login differs from that owner.

The generator skips a same-named target object. For constraints, it replaces a
named CHECK constraint only when its complete condition is the sole difference
and all other captured constraint attributes match. A same-name difference in
constraint type, columns, foreign-key target, status, validation, or another
captured attribute refuses generation and requires a reviewed migration; the
generator does not claim general constraint reconciliation. An index is
skipped when a target index on the same table has the same ordered column
list. Not-null changes handle ORA-01442 by enabling the existing not-null
check. Sequence mappings move ordinary sequences beyond seeded IDs; identity
generators use `START WITH LIMIT VALUE`. Generated SQL does not use
`OVERRIDING SYSTEM VALUE`.

### Reference data: never copy foreign keys by id

An allow-listed table's `keyColumns` defines insert identity; `labelColumns`
defines cross-environment identity. When a selected foreign key points to
another allow-listed table, the generated insert resolves the parent key on the
target from the parent's configured label. It never carries the source foreign
key ID into the child insert. A missing or duplicated target label stops
generation with the table, foreign key, and label in the error. This avoids
policy rows pointing at unrelated target reason IDs.

Rows are inserted only when their natural key is absent. Existing rows are
never updated or deleted. A same-key row with different non-key values is
reported in the build output and migration README; its count-by-key
precondition makes the existing row visible to review. An existing label with
a different natural key is reported and creates a precondition that blocks the
migration until the label conflict is resolved. Checks count by natural key or
parent label; they do not compare whole-row text. Inserts are grouped into
steps of at most 100 statements and 50,000 SQL bytes.

Text values use collision-safe q-quotes in 32-character chunks, represent
embedded newlines with `CHR(10)`, and use `NULL` for empty strings. DATE values
use `TO_DATE`; plain TIMESTAMP values use `TO_TIMESTAMP`. Dictionary spellings
with no precision or precision `(0)` through `(9)` are supported. TIMESTAMP
WITH TIME ZONE values require an ISO timestamp with `Z` or a numeric UTC offset
and use `TO_TIMESTAMP_TZ`, preserving the offset and fractional seconds. A
date-only or offset-free value for that type is refused. A date-looking value
in a text column remains text. TIMESTAMP WITH LOCAL TIME ZONE remains a distinct
type and keeps the formatter's existing behavior; live transfer semantics
depend on Oracle serialization and session timezone and are not qualified here.
`GENERATED ALWAYS` identity columns are omitted and assigned by Oracle. `BY DEFAULT`
identities retain seeded IDs, then get an `ALTER TABLE ... START WITH LIMIT VALUE`
step to move the generator beyond them.

Rehearse generated data DML with the rollback mode:

```bash
scripts/team.sh migrate migrations/APP_STAGE/2026-10-10_baseline-data-r001 --env staging --rehearse
```

The rehearsal rolls back eligible DML in one transaction. It skips DDL, so the
identity advancement step must be reviewed separately.

## Filter ORDS exports

`filter-ords` removes the complete module, template, and handler API calls for
each named module, validates the filtered script, and refuses a module name
that was not present:

```bash
scripts/team.sh baseline filter-ords --exclude-module legacy.api \
  --input scratch/ords/schema.sql --output scratch/ords/schema-filtered.sql
```

Use the filtered file as the `file` for an `ords-import` rollout step. Filtering
is local: it does not connect to a database or change the original export.

## Folder safety and limits

The next free `rNNN` is selected per schema and migration family. Existing
folders are immutable. An unchanged folder is left byte-identical; a folder
with a receipt or write-attempt evidence is reported as skipped. Changed output
gets a later revision. Every new folder is loaded through `load_migration` and
the normal SQL/check validators before the command succeeds. Review SQL,
checks, and target identity, then use the ordinary `check-conflicts` and
`migrate` workflow when application is authorized.

Generation is additive and intentionally bounded. It does not drop objects,
reconcile arbitrary table attributes or column defaults, change existing
column types, grant system privileges, or update/delete reference rows. Type
changes fail with a review message. Unmodeled compare-env differences still
need manual review.
