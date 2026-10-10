# Comparing environments

`compare-env` captures selected schema catalogs from two environments and
reports a deployment readiness view. It reuses `scripts/compare_schema.py` and
does not write to either database.

```bash
scripts/team.sh compare-env --from dev --to staging
scripts/team.sh compare-env --from dev --to prod \
  --section tables --section columns --section constraints --section triggers
scripts/team.sh compare-env --from staging --to prod --format markdown
scripts/team.sh compare-env --from dev --to staging \
  --emit-dba-script scratch/dev-to-staging-dba.sql
```

Use `--section` more than once to narrow the report. With no section option the
command captures every section listed below. The team wrapper's global
`--schema <NAME>` selects one configured schema when a profile has several.
Environment profiles must map to the same configured position; a single DEV
schema may map to a differently named single staging or production schema.
Object grants are limited to names matching `TABLES_PREFIXES` or
`CODE_PREFIXES`; `*` includes every object grant in the selected schema.

## Sections

| Section | Compared fields |
| --- | --- |
| `tables` | Table name, temporary, partitioned, IOT, nested, secondary, compression and logging attributes. |
| `columns` | Table and column names, type and type owner/modifier, byte and character lengths, precision, scale, length semantics, nullability, hidden and virtual status. Column order is not a key. |
| `constraints` | Constraint name where user-defined, type, table, ordered columns, referenced table and columns, condition, status, validation, delete rule and deferrability. Generated `SYS_C...` names can pair by table, type, columns, referenced columns and condition. A condition at the catalog's 4,000-byte boundary blocks readiness because it may be truncated. |
| `indexes` | Index name, table, uniqueness, ordered columns and sort direction, type, visibility and status. |
| `triggers` | Trigger name, table, type, event, status, source line count and `SUM(ORA_HASH(text))` for source lines after line 1. |
| `sequences` | Name, increment, minimum, maximum, cycle, order and cache settings. |
| `synonyms` | Public or schema synonym name, target owner/name and database link. |
| `views` | Name, status, source line count and line hash sum. The first generated DDL line is ignored. |
| `stored-code` | Unit name/type, status, line count and `SUM(ORA_HASH(text))` after line 1. |
| `invalid-objects` | Invalid object names, types and status. Any invalid object is a readiness blocker. |
| `identity-columns` | Table/column name, generation type and identity options. Generated sequence names are excluded. |
| `object-grants` | Object, grantee, privilege and grantable status for selected prefixes. |
| `system-privileges` | Direct system privilege and admin option for the selected schema. |
| `roles` | Direct role grants, admin option and default-role status for the selected schema. |
| `network-aces` | Host, ports, principal, principal type, privilege, grant/deny, order and time bounds for the schema, `PUBLIC`, and roles directly granted to the schema. |
| `ords` | ORDS schema enablement/mapping, module names and metadata, template URI/priority, and handler method/source type/format metadata. |
| `java-mle` | Java and MLE object names, types and status. |
| `installed-options` | JVM and Spatial option values and installed component status. |
| `versions` | Database and APEX versions. |

Each record is matched by its section key and then compared field by field.
Reports classify records as `missing on target`, `different`, or `only on
target`. A `different` item includes the field names that changed in JSON and
text/Markdown output. An identical record appears under `identical`.

## Optional DBA connections

The following optional `.env` values name saved SQLcl aliases. Configure at
most one alias for each environment; the aliases are not required for ordinary
schema sections.

```dotenv
DEV_DBA_SQLCL_CONNECTION=dev-dba-readonly
STAGING_DBA_SQLCL_CONNECTION=stage-dba-readonly
PROD_DBA_SQLCL_CONNECTION=prod-dba-readonly
```

The command uses a configured DBA alias only for `system-privileges`, `roles`,
`network-aces`, `ords`, and `installed-options`. Those sessions set
`ALTER SESSION DISABLE COMMIT IN PROCEDURE` and `SET TRANSACTION READ ONLY`.
The alias is never used for writes. Without a DBA alias, each selected DBA
section is listed as `not compared (no DBA connection)` under blockers and the
command exits 2 because readiness is incomplete. A configured alias that
cannot read a required view also leaves a blocker and retains SQLcl evidence
under `scratch/`.

## DBA script output

`--emit-dba-script <file>` writes additive object grants, direct system
privilege and role grants, network ACEs and `ORDS.ENABLE_SCHEMA` statements
identified by the captured source/target gap. The script is a review artifact,
not an automatic apply. It does not revoke or drop privileges, change default
role settings, create tables or code, or copy ORDS modules, templates or handler
source. It is written only when the comparison has complete evidence.
Review it and the selected target before adding it as a `sql-script` step in a
rollout manifest.

## Output and exit codes

Text, JSON (`--format json`) and Markdown (`--format markdown`) outputs group
the result as **blockers**, **differences**, **identical**, and **not compared**.
When `--section` narrows capture, the report names every unselected section
under **not compared**.

| Exit | Meaning |
| --- | --- |
| 0 | Every selected section was compared and no difference or blocker was found. |
| 1 | Catalog coverage was complete and one or more differences were found. |
| 2 | Capture failed or was incomplete, a row/payload cap was reached, an object is invalid, or a selected DBA section had no usable DBA connection. |

Each section is read in ordered pages of 500 rows. The capture fails if a
section exceeds 100,000 rows or if the compressed payload exceeds 128 MiB; it
does not report a partial catalog as complete. A short final page (including a
verified empty page after an exact multiple) proves that paging reached the
end.

## Limits

- This is a structure comparison. Reference rows and application data,
  including ERP and camp data, are outside scope. Foreign-key definitions
  compare referenced table and column names; reference values must be compared
  **by label, not id**.
- Object grants use the configured prefix filters. System privileges, roles
  and network ACE principals follow direct grants only; nested role grants are
  not expanded.
- Column default expressions, function-based index expressions, partition and
  storage details, and comments are outside this comparison. Object grants are
  scoped by `TABLES_PREFIXES` and `CODE_PREFIXES`; `*` includes every name.
- Sequence `LAST_NUMBER`, identity runtime values, comments, storage placement,
  optimizer statistics, and exact source bytes are not compared. Source hashes
  use Oracle `ORA_HASH`, which can collide; the first source line is omitted to
  ignore generated headers. Qualify Oracle catalog behavior on the target
  database versions before using the report for a production rollout.
- Oracle catalog columns and ORDS metadata views vary by database/ORDS release.
  Unsupported or unreadable sections are blockers, not identical results.
- A source hash identifies changed stored logic but is not a source export.
  Use an exact source baseline and review the deployment migration separately.
