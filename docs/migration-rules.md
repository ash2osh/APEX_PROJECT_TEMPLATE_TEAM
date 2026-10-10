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
reviewed checks; incomplete verification blocks automated apply. When such an
operation has its checks, preflight passes but lists it as `REVIEW:`: the
analyzer cannot see what that SQL does, so only the checks stand for it.

Each check is one `SELECT` (or `WITH ... SELECT`) that returns the number `1`.
It may use ordinary query syntax, including parenthesized conditions and
subqueries, and these functions: `AVG`, `CAST`, `COALESCE`, `COUNT`, `LENGTH`,
`LOWER`, `MAX`, `MIN`, `NVL`, `REGEXP_LIKE`, `SUBSTR`, `SUM`, `TO_CHAR`,
`TO_NUMBER`, `UPPER`, `SYS_CONTEXT` for `USERENV` `SESSION_USER` or
`CURRENT_SCHEMA`, `CHR`, `ORA_HASH`, `STANDARD_HASH`, `DECODE`, `NULLIF`,
`INSTR`, `REPLACE`, `TRIM`, `LTRIM`, `RTRIM`, `LENGTHB`, `LPAD`, `RPAD`, and
`LISTAGG`. Its only bind is `:target_schema`. Non-ASCII text belongs
inside a string literal, a quoted name or a comment. A call to any other
function (yours or a built-in not listed) is refused when it is written with
parentheses, so is a sequence `NEXTVAL`/`CURRVAL`, `FOR UPDATE`, a database
link, or any statement that writes; express other tests with `CASE` and the
listed functions. The validator reads the text, not the database, so a stored
function named without parentheses (`my_func`, `my_pkg.is_ready`) looks like a
column to it and is not refused. The check session is read-only and cannot
commit inside PL/SQL (`ALTER SESSION DISABLE COMMIT IN PROCEDURE`), so a
function that writes, whether directly, through an autonomous transaction or
through DDL, fails the check with `ORA-00034` instead of writing. Do not call
stored functions in a check anyway: one that does something that is not
transactional, such as writing a file or sending a request, is not stopped.

The added functions are deterministic and side-effect free:

| Function | Justification |
| --- | --- |
| `CHR` | Maps an integer code to a character without reading or changing database state. |
| `ORA_HASH` | Computes a repeatable hash for an expression with fixed seed and bucket parameters. |
| `STANDARD_HASH` | Computes a repeatable digest for the same expression and algorithm. |
| `DECODE` | Selects an output value by equality comparison. |
| `NULLIF` | Returns null or its first argument based only on equality. |
| `INSTR` | Returns a matching position in its input text. |
| `REPLACE` | Substitutes text using only its input arguments. |
| `TRIM` | Removes selected edge characters from its input text. |
| `LTRIM` | Removes selected leading characters from its input text. |
| `RTRIM` | Removes selected trailing characters from its input text. |
| `LENGTHB` | Measures the byte length of its input under the current database character set. |
| `LPAD` | Adds deterministic left padding using its input arguments. |
| `RPAD` | Adds deterministic right padding using its input arguments. |
| `LISTAGG` | Aggregates input text in the declared order; use a unique order key when output must be repeatable. |

For exact stored-source checks, a fingerprint can replace a long repeated text
literal. Record the expected source-line count and hash sum when reviewing the
change, then use a check shaped like this (replace the object name, type, count
and sum with reviewed values):

```sql
SELECT CASE
         WHEN (SELECT COUNT(*)
                 FROM USER_SOURCE
                WHERE name = 'PACKAGE_NAME'
                  AND type = 'PACKAGE BODY') = 42
          AND (SELECT SUM(ORA_HASH(TO_CHAR(line) || ':' || text, 4294967295, 0))
                 FROM USER_SOURCE
                WHERE name = 'PACKAGE_NAME'
                  AND type = 'PACKAGE BODY') = 123456789
         THEN 1 ELSE 0
       END
  FROM dual
```

Including `LINE` makes the sum sensitive to source order; the row count also
detects missing or extra lines. A count plus hash sum is a compact fingerprint,
not a collision-proof proof of byte equality. The deployment field note
available with this plan reports that identical `ORA_HASH` inputs produced the
same value on Oracle 19c and 26ai; no database was available in this worktree,
so this phase could not repeat that measurement. Before relying on a
fingerprint, verify the exact expression,
seed, bucket limit, line text and expected count/sum on each target database
version. Prefer exact-source comparison when collision risk is unacceptable.

Long check lists are split so each generated UTF-8 SQL driver stays within
`MIGRATION_CHECK_BATCH_BYTES` (default `2097152`, or 2 MiB). `migrate` runs its
check sessions sequentially; `verify --jobs N` can run up to N sessions in
parallel and defaults to one. Each session retains the read-only transaction
and disabled-commit guards, and the runner checks the observed database
identity for every session before accepting the merged report. Set the limit to
another positive integer number of bytes when a large reviewed check requires
it. A single check that exceeds the limit is refused before SQLcl starts, with
its check ID; an individual check is never split. `migrate` cannot produce a
receipt when postcondition verification is incomplete.
When fresh postconditions fail, `migrate` prints up to 20 failing IDs with
expected and observed values or Oracle/SQLcl error codes. `--verbose` prints all
failed checks; the run manifest retains their IDs and verification evidence.
If an attempted verification session returned no result frame, the error says
that verification produced no output, names likely timeout/size-limit/SQLcl
failure causes, and gives the saved evidence path.

Use `verify` to evaluate declared checks without running the migration SQL or
writing a receipt:

```bash
scripts/team.sh verify migrations/2026-09-27_create-customers-r001 \
  --env dev --phase both --format text
scripts/team.sh verify migrations/DEMO/2026-09-27_create-customers-r001 \
  --env staging --schema DEMO --phase post --only-failed --jobs 2
```

`--phase pre`, `--phase post` and `--phase both` select preconditions,
postconditions or both; each selected folder's checks are evaluated. The
default is `both`. `--only-failed` hides rows that returned their expected
value, and `--format json` emits a versioned JSON report. `--jobs N` allows up
to N batched SQLcl check sessions to run concurrently; its default is 1. Each
session keeps the same read-only transaction, disabled-commit guard and
per-session target identity verification used by `migrate`. The command runs
no migration SQL and writes no receipt or apply run manifest. Temporary files
live in a private `scratch/migration-verify-*` directory, which is removed on
success and retained with diagnostics when any check is false or verification
errors.

`verify` exits 0 when every requested check is true, 1 when at least one check
is false, and 2 when a check errors, a session is incomplete, or the selected
target identity cannot be verified. A SQL or identity error takes precedence
over false results and returns 2.

Each SQLcl apply session is limited by `MIGRATION_APPLY_TIMEOUT_SECONDS`, and
check plus inventory sessions use `MIGRATION_CHECK_TIMEOUT_SECONDS`. Both
default to 300 seconds to preserve existing behavior. Set either to a positive
finite number of seconds (fractional values are accepted). An apply timeout
reports its phase and cutoff and may leave committed DDL; stop and reconcile
the retained attempt evidence before retrying. A check or inventory timeout
means verification did not finish, so no receipt is written. The timeout
settings and check-driver budget are optional `.env` keys; see `.env.example`.

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
recover or retry. An interrupted `migrate` says which case it is: interrupted
while a SQL step runs, it prints "was interrupted and may be partially applied"
and exits 2, because the result is unknown; interrupted anywhere else it exits
130 and says whether a write was attempted ("no writes were attempted", or
"interrupted after a write was attempted; stop and reconcile").

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


Optional migration profiles let a project mirror code from `CODE` while applying
migrations to `CUSTDATA` or `API`. Configure `MIGRATION_SCHEMA`,
`MIGRATION_SQLCL_CONNECTION` and `MIGRATION_EXPECTED_USER` together; use the
`STAGING_` and `PROD_` prefixes for those environments. Each triple accepts
position-aligned lists. `--schema API` selects that migration owner even when
it is absent from `CODE_SCHEMA`. A single DEV migration owner can map to a
renamed single staging/production owner; unlisted owners never map. If a whole
triple is absent, migration commands retain the original environment target.
Partial, empty or misaligned triples refuse before connecting. `check-conflicts`
uses the same migration identity, and `doctor` includes explicit DEV migration
identities once per connection/user/schema. Backups and schema comparison keep
their existing profiles. Flat migration folders require one configured migration
owner; otherwise use `migrations/<SCHEMA>/…` and keep each batch in one schema.
