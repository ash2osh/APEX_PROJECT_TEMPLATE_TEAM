# Baseline export and migration generation

`baseline` captures configured schema source and grants through the template's
read-only SQLcl catalog session, then builds reviewable migration folders by
comparing two configured environments. It never applies those folders.

## Configuration

Copy [the example](baseline.example.json) to `baseline.json` in the project
root, then set the actual schema names, prefixes, exclusions, grant policy and
sequence mappings. Keep `baseline.json` with the project configuration; do not
put credentials in it. The command resolves connection aliases and expected
users from the existing `.env` profiles.

`schemas` lists the schema owners to process. `prefixes` selects matching
object names; an empty list selects every name. `excludedObjects` omits exact
object names after the prefix match. Both filters apply to source, settings,
views, structure and object grants. The optional `--schema` wrapper option
selects one configured schema.

`grants.skipGrantees` removes grantees, `grants.includeGrantees` optionally
limits output to that list, and `grants.keepGrantOptions` controls whether
object `WITH GRANT OPTION` and system privilege `ADMIN OPTION` are retained.
System privileges are exported for the schema owner. Object grants use the ordinary configured schema connection; system
privileges require the matching `<ENV>_DBA_SQLCL_CONNECTION`. The exporter
checks that both connections reached the same database, container, schema and
edition.

`sequenceMappings` connects an application sequence with the table and column
that hold its seeded identifiers. Each selected conventional sequence needs a
mapping so the generated migration can advance it past the target's current
maximum ID. Identity columns use Oracle's `START WITH LIMIT VALUE` support.

`referenceData.tables` is reserved configuration for a later phase. These
commands do not export or build reference rows. ORDS export filtering is also
not part of this command set.

## Export stored source and grants

```bash
scripts/team.sh baseline export-source --from dev
scripts/team.sh baseline export-grants --from dev
scripts/team.sh baseline export-source --from staging --schema APP_STAGE
```

By default, output goes under `scratch/baseline/<environment>/<schema>/`.
Source units are separate UTF-8 JSON files under `stored-source/`; each stores
the configured owner's `ALL_SOURCE.TEXT` rows as returned, without adding or
trimming newlines.
`settings.json` contains the corresponding `ALL_PLSQL_OBJECT_SETTINGS` rows.
Each view JSON preserves the exact `ALL_VIEWS.TEXT` value and also carries
`DBMS_METADATA.GET_DDL` output for later executable view migrations. A source
manifest records the completed row counts. Grant output is under `grants/` and
keeps object grantable flags and system privilege admin-option flags.

Catalog sections are captured in sorted pages of 500 rows, with a 100,000-row
cap and completeness evidence. Missing pages, an unreadable catalog view,
connection failure, or a cap hit stops the export with an error. `--scratch`
changes the output root. Scratch files are local artifacts and should be
reviewed before moving any needed JSON into durable project storage.

## Build migration folders

```bash
scripts/team.sh baseline build --from dev --to staging
scripts/team.sh baseline build --to prod --schema APP_PROD
```

`--from` defaults to `dev`. `build` captures complete catalogs, compares the
selected environments by object name, and writes new folders under
`migrations/<TARGET_SCHEMA>/`. A build can create up to three families:

- `baseline-structure`: guarded table and sequence creates, missing columns,
  named constraints, indexes, not-null changes, and generator synchronization.
- `baseline-grants`: additive object grants grouped into steps of no more than
  100 statements or 50,000 SQL bytes.
- `baseline-code`: changed stored units with each unit's compiler settings,
  exact owner-scoped `ALL_SOURCE.TEXT` line assembly, changed views, fast source checks and
  a multi-pass compile of remaining configured invalid units.

Plain source scripts are used when safe. A unit with a whitespace-only line or
a line ending in `-` is assembled as a CLOB to preserve the text SQLcl would
otherwise trim or continue. CLOB source chunks are at most 32 characters. The
final source line check accepts either Oracle's stored final newline or its
absence. Source checks use line counts and name-based text equality; they do
not use `IS NULL` or `NVL` on source text. `ALL_SOURCE` is filtered by the
configured owner so it works when the saved login differs from that owner.

The generator skips a same-named target object, except a named check constraint
with a different condition is replaced. An index is skipped when a target
index on the same table has the same ordered column list. Not-null changes
handle ORA-01442 by enabling the existing not-null check. Sequence mappings
move ordinary sequences beyond seeded IDs, while identities use
`START WITH LIMIT VALUE`; generated SQL does not use
`OVERRIDING SYSTEM VALUE`.

The next free `rNNN` is selected per schema and migration family. Existing
folders are immutable. An unchanged folder is left byte-identical; a folder
with a receipt or write-attempt evidence is reported as skipped. Changed output
gets a later revision. Every new folder is loaded through `load_migration` and
the normal SQL/check validators before the command succeeds. Review the SQL,
checks and target identity, then use the ordinary `check-conflicts` and
`migrate` workflow when application is authorized.

Generation is additive and intentionally bounded. It does not drop objects,
reconcile arbitrary table attributes or column defaults, change existing
column types, or grant system privileges. Type changes fail with a review
message. Unmodeled compare-env differences still need manual review. Reference
data and ORDS filtering are not generated here.
