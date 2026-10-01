# Migration and schema comparison rules

This template stores each migration as one dated folder containing an ordered
set of SQL files. In a multi-schema project the folder is under
`migrations/<SCHEMA>/`; a migration folder changes only that schema. The files
and receipts belong to the local repository. Each developer has an independent
Git repository; repositories share Oracle state, not commits, migration
files, or pending work. The live schema is the source of truth for what
currently exists. No team metadata table, lock, roster, or database migration
ledger is used.

## Folder and file names

Use this layout for a multi-schema code profile:

```text
migrations/
└── DEMO/
    ├── 2026-09-28_create-customers-r002/
    │   ├── 001-add-status-column.sql
    │   ├── 002-update-customer-view.sql
    │   └── checks.json
    └── 2026-09-27_create-customers-r001/
        ├── 001-create-table.sql
        ├── 002-create-index.sql
        ├── 003-create-view.sql
        └── checks.json
```

Each folder is `migrations/<SCHEMA>/YYYY-MM-DD_<migration-name>-rNNN/`; do not
add a developer name. When `CODE_SCHEMA` contains exactly one schema, the
existing flat `migrations/YYYY-MM-DD_<migration-name>-rNNN/` layout remains
valid and the schema-folder layout is also accepted. When it contains two or
more schemas, the schema folder is required and a flat migration folder is
refused. With one schema, keep each migration family in one layout: `migrate`
and `check-conflicts` refuse a family that appears both flat and under
`migrations/<SCHEMA>/` for that schema, because both folders target it and
could apply the same revision twice. The same family under another schema's
folder targets a different schema and is allowed. The date is the folder creation date and stays the same when the
migration is promoted. The ISO date prefix sorts newer dates ahead when a file
browser is sorted descending; the name cannot set the browser's sort direction.
Folders created on the same date have a name-based tie-break, not an
intraday creation order.

The migration name uses lowercase kebab-case. Revisions begin at `r001` and
increase by one for each later folder with the same migration name within the
same schema. Each `(schema, migration name)` family has its own revision
sequence. For example, `create-customers-r001` and `create-customers-r002` in
one schema are separate, sequential migrations for the same intent. `r002`
contains the incremental follow-up, not a replacement copy of `r001`; its SQL
sequence starts again at `001`.

Each immediate SQL file is named `NNN-<step-name>.sql`; sequence numbers must
be unique and consecutive from `001`. The runner executes those files in
ascending numeric order in one migration session. When several folders are
selected, it executes them in the order supplied on the command line. The
descending folder display order is for browsing only and never determines
execution order. Review dependencies and pass folders in the required order.
Before the first write, a batch checks the first folder's preconditions; each
later folder's preconditions run just before that folder is applied, so they
may depend on what the earlier folders create. `check-conflicts` names the
folders whose preconditions it could not check yet. A later SQL step or folder
may also select from a synonym, stored unit or materialized view that an
earlier one creates. A
cross-schema change such as a grant or a synonym is two coordinated migrations,
one per schema.

SQL files hold only SQL: statements ended by `;` and PL/SQL blocks, types,
libraries, Java sources and MLE modules ended by a standalone `/`. SQLcl reads
some text as something other than part of the statement, so the validator
rejects it before SQLcl connects:

- a line holding only `/` or `.` inside a SQL statement (SQLcl ends the
  statement there and runs the next line as a command);
- a line holding only `/` inside a comment;
- a line starting with `@` inside a statement or block (SQLcl splices the named
  file into it, bytes that were never reviewed or hashed);
- a quoted identifier that spans lines;
- a string or quoted name standing between two statements (SQLcl reads it as a
  command, and the statement after it may not run);
- the comment opener `/*/` (SQLcl's parser fails on it and runs the comment's
  lines as commands; write `/* /` or `/**/`);
- a statement's first word glued to what follows (`DECLARE,`, `SELECT(`: SQLcl
  reads the first word up to the next space and runs the lines after it one by
  one; put a space after it).

Blank lines inside a statement are accepted: the apply session sets
`SQLBLANKLINES ON`, so an `UPDATE` with a blank line before its `WHERE` runs as
written.

Every folder requires a `checks.json` with read-only preconditions and
postconditions. These checks validate the expected starting state and verify
the committed result. The runner also performs structural catalog checks for
SQL forms it can analyze. Unsupported or data-changing operations need explicit
reviewed checks; incomplete verification blocks automated apply.

Each check is one `SELECT` (or `WITH ... SELECT`) that returns the number `1`.
It may use ordinary query syntax, including parenthesized conditions and
subqueries, and these functions: `AVG`, `CAST`, `COALESCE`, `COUNT`, `LENGTH`,
`LOWER`, `MAX`, `MIN`, `NVL`, `REGEXP_LIKE`, `SUBSTR`, `SUM`, `TO_CHAR`,
`TO_NUMBER`, `UPPER`, and `SYS_CONTEXT` for `USERENV` `SESSION_USER` or
`CURRENT_SCHEMA`. Its only bind is `:target_schema`. Non-ASCII text belongs
inside a string literal, a quoted name or a comment. A call to any other function (yours
or a built-in not listed) is refused when it is written with parentheses, so is
a sequence `NEXTVAL`/`CURRVAL`, `FOR UPDATE`, a database link, or any statement
that writes; express other tests with `CASE` and the listed functions. The
validator reads the text, not the database: a stored function named without
parentheses (`my_func`, `my_pkg.is_ready`) looks like a column to it and is not
refused. Do not call one in a check; one that writes through an autonomous
transaction would break the read-only promise of `check-conflicts`.

SQLcl reports a PL/SQL or view compilation error as a warning and carries on,
so the apply session ends with its own check. Every package, package body,
type, type body, procedure, function, trigger, view, library, Java source or
MLE module that the migration's own `CREATE` or `ALTER ... COMPILE` statements
name is looked up in `ALL_OBJECTS` and `ALL_ERRORS`. One with errors, or one
the session cannot find or see, fails the apply with `ORA-20986: Migration left
objects with compilation errors or that it cannot see`. A name without a schema
prefix belongs to the session's current schema, including after an
`ALTER SESSION SET CURRENT_SCHEMA` earlier in the migration; a unit the
migration drops again is not checked. A plain `ALTER PACKAGE` or `ALTER TYPE
... COMPILE` also checks the body when one exists. Objects the migration does
not name, including a teammate's unrelated work, are never checked. The DDL
has already committed by then; no receipt is written, and the next revision
fixes the object. Units created through dynamic SQL (`EXECUTE IMMEDIATE`) are
not named in the file; cover those with an explicit postcondition, for example
a count of `INVALID` objects in `ALL_OBJECTS`.

## Immutability and status receipts

Once a migration has been applied or a write attempt may have begun, keep its
SQL bytes, names, order, and `checks.json` unchanged. A correction or recovery
belongs in the next revision folder. A failed or ambiguous attempt may have
left Oracle DDL committed, so inspect the live schema before deciding how to
recover or retry.

Every `migrate` that reaches the database leaves its record in
`scratch/migration-attempt-*` (SQL, SQLcl log and outcome). It is what lets
`migrate` refuse a folder you edited or that is half applied, so do not delete
it until you have reconciled that folder with the database. `scratch/` is
ignored by Git and is not shared with teammates.

The runner creates `status.dev.json`, `status.staging.json`, or
`status.prod.json` only after SQLcl confirms the apply session completed and a
fresh connection verifies the target identity and postconditions. It records
the exact SQL and checks hashes, target identity, and verification evidence.
These files are evidence from this checkout; they are not a shared ledger.
Never create a planned, pending, failed, or aspirational status file, and do
not treat a receipt by itself as proof of today's live state.

## Conflict checks and the independent-repository limit

Run the conflict checker on the folders you intend to apply:

```bash
scripts/team.sh check-conflicts \
  migrations/2026-09-27_create-customers-r001 --env dev
scripts/team.sh check-conflicts \
  migrations/2026-09-27_create-customers-r001 --local
```

`--local` checks the selected files and their ordered effects without a
database connection. Live mode additionally checks those effects against the
selected environment's observed catalog. `migrate` repeats the live
preconditions before applying. The checker handles common object namespace
collisions, table columns, and supported dependencies; it reports when an SQL
form needs explicit checks or cannot be analyzed safely.

A table, view or sequence name that is already taken is a conflict, whether the
database holds it or an earlier selected folder creates it. The one exception is
a `CREATE OR REPLACE VIEW` that replaces a view, in the database or created by
an earlier folder of the same batch, and whose folder declares a precondition
for the expected prior view; the report lists it as needing review.

This is selected-batch analysis plus live-state preflight, not discovery of
other developers' pending files. Because independent repositories do not share
migration files, two developers can both pass while the shared catalog is
still empty. When one applies, the other's later live preflight should detect
the resulting object. A concurrent change after preflight can still race the
apply. The database's own DDL checks remain the final defense, and failures
that may have partially committed require live reconciliation.

## Environment targets

Apply with an explicit environment:

```bash
scripts/team.sh migrate \
  migrations/2026-09-27_create-customers-r001 --env dev
scripts/team.sh migrate \
  migrations/2026-09-27_create-customers-r001 --env staging
scripts/team.sh migrate \
  migrations/2026-09-27_create-customers-r001 --env prod
```

DEV uses `CODE_SQLCL_CONNECTION`, `CODE_EXPECTED_USER`, and `CODE_SCHEMA`.
Staging and production reuse `STAGING_SQLCL_CONNECTION` /
`STAGING_EXPECTED_USER` and `PROD_SQLCL_CONNECTION` / `PROD_EXPECTED_USER`,
with explicit `STAGING_SCHEMA` and `PROD_SCHEMA`. Set the schema separately
because the owner can differ between environments even when the saved
connection and deployment roles are already configured. Do not assume staging
or production has the same schema name as DEV, and do not fall back to DEV when
a target is incomplete. The SQLcl login user may differ from the schema owner.

Staging and production applies show the resolved target and require an
interactive confirmation. The runner verifies the observed database identity
and current schema before writing and verifies the committed result afterward.
The apply session compares its database name, unique name, service, container,
edition and version with the preflight observation before it runs the first
SQL file (`ORA-20987`), and every precondition and postcondition session
reports its identity too, so results observed on a different database are
refused. Receipts are written only for verified applies.

A connection name, database name, unique name or service name that looks like
production (`prod`, `prd`, `production` or `live` as a word, `PRODDB`,
`ERPPROD`, `erpprod.example.com`) is refused unless the selected target is
production. Pre-production names such as `PREPROD`, `NONPROD`, `pre-prod` and
`non_prd` do not count as production. This is a naming heuristic, not a guarantee: name non-production
databases so they do not match it, and rely on the expected-user check and the
team's review for the rest.

## Comparing selected live objects

Compare a list of objects or prefixes across two live environments:

```bash
scripts/team.sh compare-schema --from dev --to staging \
  --object CUSTOMERS --object ORDERS
scripts/team.sh compare-schema --from staging --to prod \
  --pattern 'HR_*' --pattern 'GL_*'
scripts/team.sh compare-schema --env prod --pattern 'HR_*'
```

The short form compares DEV with the selected `--env`. Repeat `--object` for
exact object names and `--pattern` for uppercase names using `*` as the only
wildcard. `_` is literal, so `HR_*` does not select `HRX_EMPLOYEES`. Selection
is the union of objects found in either catalog. Comparison captures catalog
inventory, table columns, views, sequences, and normalized metadata DDL for
selected objects. It reports missing target objects/columns, changed
definitions, target-only objects, and incomplete catalog visibility. It does not
compare table or column comments, grants, storage settings, or data.

This is a live schema drift check, not reliable migration-file attribution.
Local `status.<env>.json` receipts can help an operator identify migrations to
review, but each repository sees only its own receipts. Equivalent resulting
DDL can come from different migration files, and an out-of-band manual change
can mimic a migration. Conversely, normalization intentionally excludes some
environment-specific metadata. Treat the output as a coarse comparison of
selected current schema state, then review the implicated SQL and receipts.
