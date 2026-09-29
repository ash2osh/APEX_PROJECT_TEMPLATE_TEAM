# Multi-schema support design

Date: 2026-09-29
Status: approved (design approved in conversation; implementation plan requested 2026-09-29)

## Summary

Let one project manage several standalone database schemas that share one APEX
workspace. Each schema holds its own tables and code, has its own SQLcl
connection and expected user, and owns its own applications. Configuration
stays in `.env`, using the existing keys with comma-separated values, the same
style as `APEX_APP_ID=100,200`. A project with one value per key behaves
exactly as it does today.

## Background

The motivating project is `/home/ash/projects/epromhq_all`. It has eight
schemas (`APR`, `CAREERS`, `CENTRAL`, `EPROMHQ`, `HQ_ELEVATED`, `HQ_TRAINING`,
`MED`, `TMS`) in one workspace. It reaches each schema through its own saved
connection (`42_epromhq`, `42_tms`, and so on) and keeps its own export
scripts.

The template already separates source by schema: `apps/<SCHEMA>/<app-id>/` and
`database/<SCHEMA>/`. What it lacks is a way to hold more than one schema in
configuration. Today it has a single `TABLES_*`, `CODE_*` and `APEX_*` profile.
`backup-db` can mirror at most two schemas (the tables schema and the code
schema). `export` writes to `apps/$APEX_PARSING_SCHEMA/`. `publish` connects
with the single `APEX_SQLCL_CONNECTION`. `migrate` and `compare-schema`
resolve DEV to the single `CODE_SCHEMA`. `doctor` checks one identity.

The Graphify extractor resolves a database reference only inside the
referencing app's own schema, so a cross-schema reference stays a stub.
Sixteen synonym files in `epromhq_all` point across schemas.

## Goals

- Configure N schemas in `.env` with comma-separated values in the existing
  keys.
- Keep every existing single-schema `.env` valid with unchanged behavior.
- Make `doctor`, `backup-db`, `export`, `publish`, `deploy`, `migrate`,
  `check-conflicts` and `compare-schema` schema-aware, with `--schema <NAME>`
  to narrow a run.
- Mirror each schema's private synonyms in the code scope.
- Link cross-schema references and synonym-resolved references in Graphify.
- Preserve all existing safety properties: per-target identity checks,
  production markers, read-only production, all-or-nothing mirror installs,
  the drift guard, and byte-equality publish verification.

## Non-goals

- Per-schema prefix filters. `TABLES_PREFIXES` and `CODE_PREFIXES` keep their
  meaning and apply to every schema in the profile.
- Mirroring sequences. Only synonyms are added to the code scope.
- Different schema names between DEV, staging and production. A DEV schema maps
  to the same name in the staging and production lists. One exception keeps
  today's behavior: a project with a single DEV schema may name its staging or
  production schema differently, and that mapping applies only to the project's
  one configured DEV schema. An unlisted schema is never mapped to a target.
- A registry file, per-schema env files, or new key families.
- Cross-schema migrations. One migration changes one schema.
- The one-time adoption script for `epromhq_all`. It is a separate follow-up in
  that project.

## Design

### 1. Configuration

The existing keys accept comma lists. Each profile's three keys are
position-aligned:

```
TABLES_SCHEMA=EPROMHQ,TMS,APR
TABLES_SQLCL_CONNECTION=42_epromhq,42_tms,42_apr
TABLES_EXPECTED_USER=EPROMHQ,TMS,APR

CODE_SCHEMA=EPROMHQ,TMS,APR
CODE_SQLCL_CONNECTION=42_epromhq,42_tms,42_apr
CODE_EXPECTED_USER=EPROMHQ,TMS,APR

APEX_PARSING_SCHEMA=EPROMHQ,TMS,APR
APEX_SQLCL_CONNECTION=42_epromhq,42_tms,42_apr
APEX_EXPECTED_USER=EPROMHQ,TMS,APR
APEX_APP_ID=117,301,205
```

`STAGING_*` and `PROD_*` follow the same convention.

Rules enforced by `load_env.sh` and `load_env.ps1`, which must agree:

- A profile's schema, connection and expected-user lists have equal length.
- Schemas within a list are unique. A repeated connection is allowed, for
  schemas that share one saved connection.
- Every entry passes the loaders' existing syntax validation: uppercase Oracle
  identifiers for schemas and users, and the SQLcl alias pattern for
  connections. The loaders do not check production markers today, and must not
  start to (`PROD_*` connections legitimately contain "prod"). That check stays
  where it is, on the entry actually selected (`check_db_target` and
  `resolve_target`), and every command that uses an entry selects it first.
- `APEX_APP_ID` stays a flat list of unique positive integers.
- The prefix keys keep their current format. They apply to every schema in
  their profile.
- Existing pairing rules for `STAGING_*` and `PROD_*` (connection and user
  configured together, schema requires both) apply to the lists.

A single value in any key is the current behavior. There is no conflict case,
because there is no second key family.

The profiles keep their meaning. The tables profile mirrors tables for the
schemas it lists. The code profile mirrors code for the schemas it lists. A
split project (`TABLES_SCHEMA=A`, `CODE_SCHEMA=B`) works as it does now. A
standalone-schema project lists the same schemas in both profiles.

### 2. Resolver

`scripts/db_targets.py` gains a resolver that returns one `Target` per schema
for a profile and environment, and a lookup of a single target by schema name.
`resolve_target` keeps its signature and behavior for single-schema use.

- For `staging` and `prod`, the target for a DEV schema is the entry with the
  same name in the `STAGING_SCHEMA` or `PROD_SCHEMA` list. A schema absent from
  that list is an error stating that the schema is not deployable to that
  environment.
- `check_db_target.sh` accepts an optional schema argument and applies the
  existing pre-connect classification to that schema's connection.
- A schema that is not listed in the requested profile is refused before any
  connection is opened. The message lists the configured schemas.

### 3. Commands

| Command | Behavior |
|---|---|
| `doctor` | Checks every distinct `(connection, expected user, schema)` across the three profiles with one read-only SQLcl identity check each. It reports each schema and fails if any check fails. `--schema` narrows it. |
| `export <id>` | Reads the app's parsing schema from `APEX_APPLICATIONS` using the first `APEX_SQLCL_CONNECTION` entry. It requires that schema to be in `APEX_PARSING_SCHEMA`, reconnects with that schema's own connection, and writes `apps/<SCHEMA>/<id>/`. The dirty-mirror check, scratch-copy export and baseline record are unchanged. With no argument it exports every ID in `APEX_APP_ID`, each resolved separately, and installs all of them in one all-or-nothing `replace_mirror` call. |
| `backup-db` | Runs the tables scope for each schema in the tables profile and the code scope for each in the code profile. Each schema has its own staging directory, manifest count check and dirty-mirror check. Nothing is installed until every schema has verified; one `replace_mirror` call then installs them all. `--schema` narrows the run. |
| `publish <id>` | Locates `apps/*/<id>`. It requires the folder's schema, the descriptor's `parsingSchema`, and the live app's parsing schema to agree and to be listed, then uses that schema's connection. The drift guard, version stamp and byte-equality verification are unchanged. |
| `deploy <id> --env staging\|prod` | The descriptor's `parsingSchema` selects the same-named entry in `STAGING_*` or `PROD_*`. The `[y/N]` confirmation shows the schema and connection. |
| `migrate`, `check-conflicts` | Migrations live in `migrations/<SCHEMA>/YYYY-MM-DD_<name>-rNNN/`. The folder's schema selects the target entry. Conflict scans, revision ordering and receipts are scoped to one schema, and one invocation cannot mix schemas. |
| `compare-schema` | Requires `--schema` when more than one schema is configured. It compares that schema across the selected environments. |

Migration layout compatibility: when the code profile lists exactly one schema,
the flat `migrations/YYYY-...` layout stays valid and `migrations/<SCHEMA>/` is
also accepted. When it lists two or more, `migrations/<SCHEMA>/` is required
and a flat migration folder is refused, so a migration never has an ambiguous
target. A cross-schema change such as a grant or a synonym is two coordinated
migrations, one per schema.

### 4. Layout

- `apps/<SCHEMA>/<app-id>/`, unchanged.
- `database/<SCHEMA>/{tables,views,packages,procedures,functions,triggers,synonyms}/`,
  with `synonyms/` new.
- `migrations/<SCHEMA>/...`, new rule for multi-schema projects.
- Schema folders are uppercase, as the identifier validation already requires.

### 5. Synonyms in the code scope

- `backup_db.sql` exports each schema's own private synonyms with
  `DBMS_METADATA.GET_DDL('SYNONYM', …)` into `database/<SCHEMA>/synonyms/`.
  `PUBLIC` synonyms are excluded.
- The unsafe-filename check, the manifest object counts, and the fail-closed
  install cover the new folder. `scope_directories` for `code` gains
  `synonyms`.
- Existing mirrors that lack a `synonyms/` folder stay valid until the next
  `backup-db`, which adds the folder.

### 6. Graphify

- `DatabaseMirror` indexes every `database/*` schema instead of only the
  referencing file's own schema.
- An unqualified name resolves in the app's own schema first. A qualified name
  resolves in the schema it names.
- A name that is not a local object resolves through a mirrored synonym, one
  hop, by reading the synonym's `FOR "SCHEMA"."OBJECT"` clause. A target over a
  database link (`@`), or one not present in the mirror, stays a stub.
- The existing rule stands: an object the mirror does not hold is never given
  an invented edge.
- `.agents/rules/graphify.md` and `.agents/workflows/graphify.md` document the
  cross-schema behavior and its limits.

### 7. Failure behavior

- The loader fails with the key name for unequal list lengths, duplicate
  schemas, invalid entries, or a staging or production list that lacks a schema
  a command needs.
- `--schema X`, or a live app whose parsing schema is not listed, is refused
  before any connection to that schema's connection is opened.
- `backup-db` and multi-app `export` install nothing unless every schema
  verified.
- `doctor` reports every schema, then fails if any failed.
- Identity is verified per schema, so a wrong or leaked connection for one
  schema cannot export or publish as another.
- Production markers, read-only production and `DB_ENVIRONMENT`
  classification apply to each entry when it is selected for use, as today.

## Testing

The existing single-schema suite must pass unchanged and is the regression
gate.

- Unit: `db_targets` list parsing, by-name lookup across environments, and
  `load_env.sh` and `load_env.ps1` parity on list validation.
- CLI, using the existing deterministic fake-SQLcl fixtures: multi-schema
  `doctor`, `backup-db` (all-or-nothing, `--schema`, synonyms), `export`
  lookup, `publish` schema agreement, `deploy`, and `migrate`,
  `check-conflicts` and `compare-schema` per schema folder.
- Graphify: extend `tests/test_graphify_pipeline.py`, which runs the real
  `graphify update`, with a second schema, one cross-schema qualified
  reference, and one synonym-resolved reference.
- Live check against `docker-demo`. That connection has only the `DEMO`
  schema, so the run needs a second schema created there. Creating it needs the
  user's approval at that time.

## Delivery order

Each step is independently mergeable and tested:

1. Loader, resolver, `--schema` plumbing, `.env.example` and README.
2. `doctor` and `backup-db`, including the synonyms scope.
3. `export`, `publish` and `deploy`.
4. `migrate`, `check-conflicts` and `compare-schema` with schema folders.
5. Graphify cross-schema and synonym resolution.
6. Docs (`AGENTS.md`, `docs/migration-rules.md`, the `initialize-project`
   skill) and the port to the standalone `APEX_PROJECT_TEMPLATE`.

## Assumptions to verify during planning

- The first `APEX_SQLCL_CONNECTION` entry can see every configured app in
  `APEX_APPLICATIONS`. `epromhq_all`'s export scripts query the workspace with
  one connection, which suggests it can. If it cannot, `export` would need a
  fallback across the listed connections.
- `DBMS_METADATA.GET_DDL('SYNONYM', …)` output is stable enough for the
  existing normalization and byte-comparison of mirrors.
- The PowerShell scripts (`*.ps1`) need the same changes as the Bash scripts.
  The plan must cover both, and the parity tests must fail if they diverge.

## Known limits

- Positional lists rely on order. The loader validates length and uniqueness,
  but cannot detect two entries swapped consistently across a profile's keys.
- Schemas that share one connection all share that connection's privileges.
  The identity check confirms the session user, not that it can reach only its
  own schema.
- The graph is undirected and package-level, as documented in the Graphify
  workflow, so a synonym hop shows a dependency, not its direction.
