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

Use `scripts/team.sh revise <folder> [--reason TEXT]` to copy a folder into the
next revision in that same flat or `migrations/<SCHEMA>/` layout. It preserves
SQL filenames, sequence numbers, checks and existing README content, prepends
`Supersedes <old-folder>: <reason>` to the new README, and leaves the old folder
unchanged. The default reason is `follow-up revision`. Existing environment
receipts are not copied. The command refuses a source without an `-rNNN`
suffix, a revision at `r999`, a gap in the family history, or any newer family
revision. Its order hint lists the local family in ascending revision order;
include only folders that still need to be applied.

## Archiving released folders

Use `archive-migrations` after a migration is released or superseded to move
its complete folder out of the active migration tree:

```bash
scripts/team.sh archive-migrations <folder> [<folder>...] [--schema SCHEMA] \
  [--destination DIR] [--reason TEXT] [--yes]
scripts/team.sh archive-migrations --all [--keep <folder>...] [--schema SCHEMA] \
  [--destination DIR] [--reason TEXT] [--yes]
```

The default is a dry run. It prints each folder, family, revision, local
receipt or attempt evidence, and destination, then exits without changing any
files. Add `--yes` to move the folders. The command reads local files only; it
does not connect to Oracle or run Git. The default destination is
`migrations-archive/`; a schema folder is preserved, so an
`APP_DEV` migration goes to `migrations-archive/APP_DEV/`. A destination
inside `migrations/` or an existing target folder is refused. Folder contents
move byte for byte, including `status.*.json` receipts and attempt-event files.

Each archive operation adds records to
`migrations/<SCHEMA>/.archived.json`, or `migrations/.archived.json` for the
flat layout. The committed schema-version-1 index stores `folder`, `family`,
`revision`, `payloadDigest`, `archivedAt` (UTC ISO date), `reason`, and the
repository-relative `destination`. Unknown keys or malformed records make
migration discovery refuse the index.

A folder with an attempt record but no verified receipt is refused as unknown state;
`--allow-unverified-attempts` archives such failed or abandoned attempts and records the
evidence as `attempt-without-verified-receipt (accepted)` in the plan.

Archiving must remove the contiguous low prefix of a family, or its whole
visible history. A folder with an attempt record and no verified receipt for
any environment is refused because its state is unknown. Once an archived
prefix exists, visible revisions must continue directly after its highest
revision; a visible revision at or below the prefix or a later hole is still
refused. Without an archived prefix, the existing `r001` start and consecutive
revision rule is unchanged.

For example, archive `archive-family` revisions r001 through r004 from
`migrations/APP_DEV/` with `--yes`. The index records those four revisions,
their folders move under `migrations-archive/APP_DEV/`, and
`migrations/APP_DEV/2026-10-05_archive-family-r005/` stays visible. Migration
discovery accepts r005 and a later r006 because r001 through r004 are recorded
as the archived prefix. `revise` can continue from visible r005; when every
revision is archived, unarchive an archived folder or start a new family.

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

Use [`migration-plan`](migration-planning.md) for a local inventory or an
explicitly selected live preflight. Discovered folder order is for display;
only a caller-supplied batch order is preserved as a proposed sequence. The
report counts single-SQL-file folders but does not propose combining them.
Live reporting checks only the first selected folder's preconditions before
any write and labels later preconditions as deferred until their apply
boundary. It does not apply or rehearse SQL. Attempted and applied folders
remain immutable; a correction belongs in a later revision.

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

Before a migration or rollout script reaches SQLcl, the runner also checks the
exact direct-delivery source for known script-reader hazards. A quoted physical
line ending in `-` immediately before another line is refused because SQLcl may
join the lines and change the value. Whitespace-only lines inside quoted values
and code lines ending in `-` produce review warnings; those warnings do not
rewrite or reject the SQL. Source generated through explicit CLOB/character
assembly is not treated as direct payload text. This check does not replace the
normal source verification or prove transport behavior on another SQLcl release.

Every folder requires a `checks.json` with read-only preconditions and
postconditions. These checks validate the expected starting state and verify
the committed result. The runner also performs structural catalog checks for
SQL forms it can analyze. Unsupported or data-changing operations need explicit
reviewed checks; incomplete verification blocks automated apply. When such an
operation has its checks, preflight passes but lists it as `REVIEW:`: the
analyzer cannot see what that SQL does, so only the checks stand for it.

The original `checks.json` format is `schemaVersion: 1` and keeps its exact
three root keys: `schemaVersion`, `preconditions`, and `postconditions`. Use
`schemaVersion: 2` only when you also need optional rehearsal-only metrics; it
adds `rehearsalMetrics` without changing how normal migration checks or apply
receipts work. Each metric has a unique kebab-case `id`, one guarded scalar
`SELECT`, and integer `expectedBefore`, `expectedAfter`, and `expectedRollback`
values in the safe JSON range `-9007199254740991..9007199254740991`. A folder
may declare at most 50 metrics. Metric queries use the same conservative
function, bind, and one-query validation as regular checks, but are evaluated
only by `migrate --rehearse`.

For example, a reviewed migration can declare a metric for rows intentionally
tagged to that migration:

```json
{
  "schemaVersion": 2,
  "preconditions": [],
  "postconditions": [
    {"id": "row-present", "sql": "SELECT COUNT(*) FROM QUALIFIED_ROWS WHERE ID = 1", "expected": 1}
  ],
  "rehearsalMetrics": [
    {
      "id": "tagged-total",
      "sql": "SELECT COALESCE(SUM(AMOUNT), 0) FROM QUALIFIED_ROWS WHERE ID = 1",
      "expectedBefore": 0,
      "expectedAfter": 25,
      "expectedRollback": 0
    }
  ]
}
```

The example is schema guidance, not permission to execute that table or query.
Choose a predicate that keeps the metric scoped to this migration; later
folders in the same rehearsal can change shared data. A metric observes a
folder boundary, not per-row changes or SQLcl affected-row feedback.

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


## Starting a code migration from DEV

A package, procedure, function, trigger or type change starts from the source that is
live on DEV, not from the mirror under `database/`: the mirror is refreshed irregularly
and teammates change live code.

```bash
scripts/team.sh new-migration add-customer-flag --source "PACKAGE:CUSTOMER_PKG" --source "PACKAGE BODY:CUSTOMER_PKG" --env dev
```

It reads the stored source read-only, through the same identity-checked capture as
`baseline export-source`, and writes `migrations/<SCHEMA>/<today>_<name>-r001/`:

- one step per object (`CREATE OR REPLACE` plus the live source) to edit;
- preconditions `unchanged-since-read-*` that compare every live source line with what was
  read, so `check-conflicts` and `migrate` refuse when someone changed the object after you
  started (take their change into your steps, or start again);
- postconditions `valid-*`: each object VALID without compile errors. Add checks for what
  your change must do.

A name that already exists as a migration family is refused; edit that folder, or use
`scripts/team.sh revise` after a write attempt.

## Rehearsal

Use `migrate --rehearse` to try data changes on the selected real target and
roll them back without writing a receipt or a migration write-attempt marker:

```bash
scripts/team.sh migrate \
  migrations/2026-09-27_seed-customers-r001 \
  migrations/2026-09-28_assign-customers-r001 \
  --env dev --rehearse --report scratch/customer-rehearsal.json
```

The command freezes the selected files, verifies the target identity, and
requires staging/production confirmation as the normal apply does. Before the
data session it sets `AUTOCOMMIT OFF` and verifies SQLcl reports it off. If
SQLcl cannot confirm that state, rehearsal stops before any migration file is
run. The data session also uses `WHENEVER SQLERROR EXIT FAILURE ROLLBACK`, the
migration identity guard, and `ALTER SESSION DISABLE COMMIT IN PROCEDURE`.

The existing `analyze_batch` analyzer classifies every file. A file runs only
when all its statements are `INSERT`, `UPDATE`, `DELETE`, or `MERGE`, and its
folder declares both preconditions and postconditions. A file with DDL,
`TRUNCATE`, privilege changes, PL/SQL, an unrecognized statement, or a mixture
of data changes and any other statement is skipped in full and reported as
`not rehearsable` with the analyzer's reason. The command never executes the
skipped file. This limit keeps implicit-commit and unmodeled SQL outside the
rollback claim.

For each folder, rehearsal evaluates its preconditions, runs its eligible
files in input order, then evaluates its postconditions. These check queries
are validated by the same `SAFE_FUNCTIONS` check validator as ordinary
migration checks, but run as SELECTs inside the rehearsal SQLcl session without
starting a separate read-only transaction. Later folders therefore see rows
from earlier folders in the same rehearsal. SQLcl feedback is associated with
each statement where it prints a row count; otherwise that statement's
`rowsAffected` is null.

When `rehearsalMetrics` are present, the same session captures each folder's
metrics immediately before and after that folder's eligible payload, then
captures them again after the explicit final `ROLLBACK`. Each frame carries the
full database identity and exact metric IDs/order. Missing, malformed, nonnumeric,
nonintegral, or wrong-target frames are errors and expose no partial values as
verified. Before/after/rollback mismatches make rehearsal incomplete; an
unavailable rollback frame cannot be replaced by a fresh connection and cannot
claim that the original session explicitly rolled back. The JSON report records
the expected and observed metric values by phase. Regular `migrate` and `verify`
hash and preserve the schema-2 `checks.json` bytes but do not evaluate metrics.

Before any payload runs, the driver captures each folder's rollback-check
precondition values in the data session. After all selected folders, it issues
`ROLLBACK` and reruns those checks in that session. The preconditions are
re-run after `ROLLBACK`; rollback proof compares each final value with that
folder's captured starting value, not with a later folder's apply-boundary
expectation. If the data session exits
early on an SQL error or failed precondition, or SQLcl does not confirm the final
`ROLLBACK`, the command uses fresh read-only check sessions after SQLcl exits and
compares their results with the captured values. The report marks rollback proof
true only when every check is complete and its final value equals its starting
value.
An SQL error still returns 2 even when the fresh preconditions prove rollback;
inspect the error and report before deciding what to change.

This proof compares only the scalar values returned by the declared checks; it
does not establish that every effect was undone. Oracle sequence and identity
advancement may remain after a successful rollback, and autonomous trigger or
external effects are outside these checks. Review such effects separately.
Metrics have the same limit: matching a tagged aggregate before, after, and
after rollback does not prove reversal of sequence advancement, autonomous
transactions, DDL, or external effects.

`--report <file>` writes schema-version 1 JSON with each folder's files,
statements, rows affected, postcondition status, and rollback proof. It is
valid only with `--rehearse`. Exit 0 means every selected file was rehearsable,
postconditions passed, and rollback was proven. Exit 1 means a check was false
or at least one file was skipped as not rehearsable. Exit 2 means SQLcl,
identity, configuration, check evaluation, report writing, or rollback proof
could not complete. Rehearsal evidence is kept under
`scratch/migration-rehearsal-*` when SQLcl fails; successful runs remove it.

## Rollout

`rollout` runs a reviewed release as an ordered manifest. It uses the existing
migration, read-only verification and app deployment paths; standalone DBA
scripts get a private SQLcl driver with an identity check before the payload.
For the pre-window checklist, DML rehearsal, one-window execution, browser
checks, and DBA-owned recovery plan, see the
[production deployment runbook](production-deployment.md).
The [example manifest](rollout-manifest.example.json) shows every supported
step type. Replace its example folders, schema, connection alias, ORDS source
and application ID with files and descriptors in the project before running
it.

The manifest is UTF-8 JSON with no duplicate keys. Its root has exactly these
properties:

| Property | Type | Meaning |
| --- | --- | --- |
| `schemaVersion` | integer, `1` | Manifest format. |
| `steps` | nonempty array | Steps in execution order; the runner never sorts them. |

Each step is an object with a `type` and only the properties listed below.
File and folder paths use forward slashes, are relative to the repository root,
and may not contain `.`/`..`, symbolic links, or generated `scratch/` state.

| Type | Required properties | Optional properties | Behavior |
| --- | --- | --- | --- |
| `migrate` | `folders`: ordered nonempty array of migration folder paths | — | Runs one normal migration batch with its preflight, boundary checks and receipts. |
| `verify` | `folders`, `phase`: `pre`, `post` or `both` | — | Runs the standard read-only migration checks. |
| `sql-script` | `file`, `connection`, `expectedUser`, `schema` | — | Runs a reviewed DBA SQL file after checking `SESSION_USER`, `CURRENT_SCHEMA`, and the live database/service identity. The saved-connection alias must exactly equal `expectedUser`. |
| `ords-import` | `file`, `connection`, `expectedUser`, `schema` | `excludeModules`: array of module names | Validates a generated ORDS export, rejects OAuth material, optionally removes complete calls for the named modules, then uses the guarded SQL script path. `expectedUser` and `schema` must match. |
| `app-deploy` | `appId`: positive numeric application ID | — | Runs the existing deployment command and the environment's descriptor; staging and production only. |
| `pause` | `message`: nonempty text | — | Prints the message and waits for Enter before continuing. |

Every source file used by a step is hashed before execution. The runner shows
the manifest digest, each step digest and every referenced file digest in the
staging/production confirmation. It checks the same hashes before and after
each step and stops if a source changes. The one manifest-level confirmation
is held by the running rollout process and recorded in private local evidence;
the process forwards that approval to its existing staging or production
child path. It shows each step's destination information, including an app's
workspace/schema or a standalone SQL step's connection/user/schema. There is
no `--force` or confirmation bypass. DEV uses the normal DEV identity and
migration guards.

Migration `status.<env>.json` receipts and the app export baseline file
`apex-team-export.json` are local verification metadata rather than input
payloads, so they are excluded from step input hashes. App source files and the
selected deployment descriptor are included.

Standalone `sql-script` and `ords-import` steps use a guarded SQLcl driver:
`WHENEVER SQLERROR EXIT FAILURE ROLLBACK`, `WHENEVER OSERROR EXIT FAILURE
ROLLBACK`, a `SESSION_USER`/`CURRENT_SCHEMA` check, a check of the live
`DB_NAME`, `DB_UNIQUE_NAME` and `SERVICE_NAME` against the production marker,
and a completion marker. The observed user, schema, database and service are
printed and checked in SQLcl output retained in the report evidence. The source
may not change connections, include another SQLcl file, alter the current
schema after the guard, exit SQLcl, override the error behavior, or emit
rollout verification markers. The manifest sets `connection` equal to
`expectedUser`; the session identity must also match the declared user and
schema. The reviewed SQL bytes are checked again as they are read into the
private driver payload. App deploy imports a private copy of the hashed source;
migration and verification runners compare their loaded payload/check files to
the rollout hashes. Oracle DDL may commit implicitly, so a failing script can
still have database effects; review it as a migration and reconcile the
evidence before retrying. For an ORDS step, use the committed `schema.sql`
produced by `backup-ords`; exclusions remove only whole generated API calls
tied to the listed module names.

After each successful step, a local receipt is written under
`scratch/rollout-receipts/<manifest-sha256>/<env>/`. `--from-step N` requires a
matching successful receipt for every earlier step: manifest digest,
environment, step digest, input hashes, timing and evidence fields must match.
It then starts at step N. Receipts are local recovery evidence, not a shared
database ledger; inspect the target state before resuming after an ambiguous
write. They bind the configured target and record an independently observed
database/service/container/schema/user tuple. That tuple is scoped evidence,
not proof of a globally unique physical database or clone. Resume refuses a
legacy receipt without independently observed target identity. Preserve the
original receipt, report, SQLcl evidence, and immutable migration folders;
inspect the live target independently and obtain a reviewed continuation plan
before any further writes. Never edit or fabricate receipt identity, delete
evidence to bypass the refusal, or replay completed writes merely to create a
fresh receipt.

Before execution, the rollout checks every selected step's canonical receipt
path and refuses any existing file, directory, malformed receipt, or symbolic
link. Successful receipts are installed with an atomic no-overwrite link. If a
receipt appears after preflight or the filesystem cannot install it, the step
may have completed without a receipt: preserve the report and SQLcl evidence,
inspect the target independently, and use only a separately reviewed
continuation manifest for further writes. This receipt check does not serialize
cross-process runs or database writes; two processes can both pass preflight
before either installs its receipt.

`--dry-run` validates the manifest and local step inputs, prints all hashes,
and writes the report pair without running the steps. `--report <file>` writes
JSON and Markdown siblings; a `.json` or `.md` suffix is replaced to select the
pair's common base name. By default, both reports go under `scratch/`. Reports
include the overall and per-step start/end times, durations, status, hashes,
messages and evidence paths. The command prints both report paths on success,
decline and step failure, and prints a concise error for a failed step. A report
cannot overlap rollout inputs, receipts or recovery evidence. Exit status is 0
for success or a dry run, 1 for a declined confirmation or step status 1, 2 for
a refusal or other failure, and 130 for an interrupt.

Each SQLcl apply session is limited by `MIGRATION_APPLY_TIMEOUT_SECONDS`, and
check plus inventory sessions use `MIGRATION_CHECK_TIMEOUT_SECONDS`. Both
default to 300 seconds to preserve existing behavior. Set either to a positive
finite number of seconds (fractional values are accepted). An apply timeout
reports its phase and cutoff and may leave committed DDL; stop and reconcile
the retained attempt evidence before retrying. A check or inventory timeout
means verification did not finish, so no receipt is written. The timeout
settings and check-driver budget are optional `.env` keys; see `.env.example`.
The optional `MIGRATION_PREFLIGHT_INVENTORY_RETRIES` key accepts a
non-negative integer and controls inventory-change retries, independently of
the SQLcl timeout.

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

Every `migrate` that reaches the apply boundary leaves private SQLcl logs and a
run manifest in `scratch/migration-attempt-*`. It also installs immutable
`attempt-event.<UUID>.json` sidecars in the migration folder. Those sidecars are
operational evidence, not SQL payload: they do not enter the migration digest,
frozen SQL copy, or a new revision. The runner installs a `pending` event before
starting SQLcl. Before each payload file it prints
`MIGRATION_PAYLOAD_STARTED:<filename>`; successful completion prints
`MIGRATION_APPLY_COMPLETED` and verified receipt installation adds a terminal
`verified` event.

A failed apply becomes `apply-not-started` and is not locked only when the
retained SQLcl output proves a pre-payload refusal: a migration identity guard
error (`ORA-20980` through `ORA-20985` before `MIGRATION_IDENTITY_VERIFIED`,
or `ORA-20987` with both `MIGRATION_IDENTITY_VERIFIED` and
`MIGRATION_IDENTITY_GUARD_REFUSED`), a recognized connection error before
the first `MIGRATION_PAYLOAD_STARTED` marker (`ORA-01017`,
`ORA-12154`, `ORA-12514`, `ORA-12537`, `ORA-12541`, `ORA-12545`, `ORA-12547`,
`ORA-12560`, `ORA-12637`, or `SP2-0640`), or a local SQLcl start failure. The
output must not contain `MIGRATION_PAYLOAD_STARTED` or
`MIGRATION_APPLY_COMPLETED`. A terminal `apply-not-started` event records the
classification and a digest-bound reference to the retained SQLcl output. The
folder can be retried only while that no-write evidence remains readable and
matches its digest. If the output is missing or changed, the durable pending
event keeps the folder locked.

Any payload-start marker, a missing or unreadable log, an unrecognized error,
an interrupt, or a timeout remains locked by its `pending` event as
`apply-failed-or-unknown`.
`write-attempted`, `committed-unverified`, `committed-verification-failed`,
`committed-receipt-failed`, `committed-source-or-payload-changed`, and
`verified` attempt/receipt states are locked too. Only `frozen` and
`apply-not-started` records with `writeAttempted: false` are unlocked;
inconsistent or unknown evidence does not unlock a folder. A
timeout after a payload marker stays locked even when SQLcl printed no ORA
error: this covers the DEV26 1,856-statement file cut off after five minutes.
ORA errors after a payload marker also stay locked because earlier statements
may have run. A verified receipt locks the folder as well. Use
`scripts/team.sh revise --check <folder>` to see the local attempt and receipt
states; exit 0 means unlocked, 1 means locked, and 2 means evidence is unknown
or unreadable. A `pending` or `verified` sidecar remains a lock even if scratch
is deleted. If scratch is deleted after a no-write outcome, the event remains
but its required evidence is gone, so the result is unknown and locked. A
`run-manifest.json` reference to a missing event also makes the result unknown.
An attempt directory without `run-manifest.json` is incomplete evidence and
returns 2 because its target folder cannot be established.

This rule is intentionally narrow. It recognizes the identity refusal and
connection/start errors above only when no payload marker was printed; a
recognized connection error after the identity marker but before the first
payload marker still qualifies. SQLcl does not provide a durable per-statement
execution ledger, so a truncated, missing, or ambiguous output cannot prove
that the database was untouched and stays locked. A connection loss after
SQLcl prints a payload marker is also ambiguous and stays locked, even if the
first statement may not have reached Oracle. The local check sees only this
checkout's receipts, migration-folder attempt events and ignored `scratch/`
evidence; it does not see another developer's repository or prove current live
schema state. `scratch/` is
ignored by Git and is not shared with teammates.

The runner creates `status.dev.json`, `status.staging.json`, or
`status.prod.json` only after SQLcl confirms the apply session completed and a
fresh connection verifies the target identity and postconditions. It records
the exact SQL and checks hashes, target identity, and verification evidence.
These files are evidence from this checkout; they are not a shared ledger.
Never create a planned, pending, failed, or aspirational status file, and do
not treat a receipt by itself as proof of today's live state.

### Evidence of other targets

A folder can hold evidence for several targets (environment and schema). `migrate` for one target checks
that target's receipt and attempt-event chain strictly and ignores damaged evidence that belongs to another
target, so a teammate's broken record for production does not block a DEV apply. Reports and
`revise --check` without a target check everything and stay fail-closed. The 26.1 template is stricter
(see its migration rules): there, damaged evidence of any target blocks every apply of the folder.

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

For unqualified view dependencies, live preflight captures private synonyms in
the selected owner and PUBLIC synonyms, plus direct `SELECT` grants to that
owner or `PUBLIC`. An owner synonym takes precedence over a PUBLIC synonym. A
reference through either synonym is satisfied only when its direct target is a
live table, view or materialized view, an earlier-staged table or view, or a
matching direct `SELECT` grant and catalog evidence proves that an external
target exists as a table, view or materialized view. A missing target, missing
target-type evidence, missing grant, sequence target, synonym chain or
database-link target remains a missing prerequisite. These catalog reads are
read-only; local-only analysis cannot establish synonym or grant reachability.

Live preflight compares the owner inventory by object owner, object name,
subobject name and type. Status, `LAST_DDL_TIME`, object IDs and other volatile
attributes that can change without an object or subobject name/type change are
ignored. If an object or subobject name or type changes between discovery and
selected-definition capture, preflight retries the full inventory/snapshot pair up to
`MIGRATION_PREFLIGHT_INVENTORY_RETRIES` times after the first attempt. The
default is `3` retries; `0` disables them. After the retries are exhausted it
returns `LIVE_PREFLIGHT_UNAVAILABLE` with SHA-256 fingerprints for both
inventories; wait for catalog activity to settle and rerun the check.

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

## Comparing full environments

Use `compare-env` for a paged readiness comparison of full schema catalogs:

```bash
scripts/team.sh compare-env --from dev --to staging
scripts/team.sh compare-env --from dev --to prod \
  --section tables --section columns --section constraints --section triggers
scripts/team.sh compare-env --from dev --to staging --format markdown
scripts/team.sh compare-env --from dev --to staging \
  --emit-dba-script scratch/dev-to-staging-dba.sql
```

The tool extends `scripts/compare_schema.py`; the existing `compare-schema`
selectors and output remain available. `--section` is repeatable. The section
names are `tables`, `columns`, `constraints`, `indexes`, `triggers`,
`sequences`, `synonyms`, `views`, `stored-code`, `invalid-objects`,
`identity-columns`, `object-grants`, `system-privileges`, `roles`,
`network-aces`, `ords`, `java-mle`, `installed-options`, and `versions`.
Catalog rows are paged in sorted order, 500 at a time. A section over 100,000
rows, a compressed payload over 128 MiB, missing page evidence, or a SQLcl
capture error fails closed. The report groups blockers, differences, identical
records and deliberate exclusions. Each difference is `missing on target`,
`different`, or `only on target`.

All sections match records by names and stable keys, then compare the listed
catalog fields. The implementation does not compare catalog query output as a
whole line. System-generated `SYS_C...` constraints with different names can
pair when their table, type, ordered columns, referenced columns and condition
match. Triggers, views and stored code compare line counts and
`SUM(ORA_HASH(text))` after ignoring source line 1; the hash can collide and
does not replace an exact source export.

The optional read-only connection aliases are `DEV_DBA_SQLCL_CONNECTION`,
`STAGING_DBA_SQLCL_CONNECTION` and `PROD_DBA_SQLCL_CONNECTION`. Each accepts
one saved SQLcl connection alias. Configure the alias only if it can read the
selected DBA catalogs. It is used only for system privileges, roles, network
ACEs, ORDS catalogs and installed options; its session disables commit in
stored code and uses `SET TRANSACTION READ ONLY`. Without an alias, a selected
DBA section is reported as `not compared (no DBA connection)` under blockers
and returns exit 2. An unreadable required view also blocks readiness.

`--emit-dba-script <file>` writes additive object, system-privilege and role
GRANTs, network ACE and ORDS schema-enable statements from complete catalog
evidence. Review the file and target before adding it to a rollout `sql-script`
step. The DBA read-only alias is never used to apply this script. Generated
output does not change default-role settings, create schema/code objects or
ORDS modules, templates and handlers, and it does not revoke or drop privileges.

Reference rows are out of scope: ERP and camp data changes do not count as
structure differences. Foreign-key definitions compare referenced table and
column names; compare foreign-key reference data **by label, not id**. Other
limits include data rows, sequence `LAST_NUMBER`, identity runtime values,
comments, storage placement, optimizer statistics and exact source bytes. See
[the compare-env section list, output contract and limits](compare-env.md).

## Baseline generation

`scripts/team.sh baseline` uses the project's root `baseline.json` and the
existing read-only SQLcl catalog sessions. `export-source --from <env>` writes
the selected owner-scoped `ALL_SOURCE.TEXT` rows, compiler settings and exact view text to
`scratch/baseline/<env>/<schema>/`. `export-grants --from <env>` records object
grantable flags and schema-owner system privilege admin options. The latter
requires the matching `<ENV>_DBA_SQLCL_CONNECTION`; it verifies the DBA
connection reached the same database scope as the ordinary schema connection.
Capture is paged at 500 rows and refuses incomplete evidence or sections over
100,000 rows.

`baseline build --to <env> [--from <env>] [--bundle <name>] [--data]` defaults
its source to DEV. It compares complete environment catalogs and creates new
migration families under `migrations/<TARGET_SCHEMA>/` for supported structure
deltas, grouped object grants and changed source units or views. It never
applies a migration. Source and target identities must be complete, and the
builder refuses the same observed database/container/schema/edition scope even
if configured service aliases differ. The local baseline covers only the
selected repository and observed scopes; it cannot see pending migrations in
other independent repositories.

By default, structure, grants and code remain separate families. `--bundle
<name>` combines the generated structure, grants, and code in that order into
`baseline-bundle-<name>`. The name must be an ASCII lowercase slug matching
`[a-z][a-z0-9]*(?:-[a-z0-9]+)*`, no longer than 64 characters, and is checked
before SQLcl starts. Composition uses the same complete source and target
captures, preserves every eligible SQL step and check, and writes a bundle
README with the ordered original-to-generated filename mapping and SHA-256
values. The generator creates one folder per target schema with nonempty output.
`--data` retains separate preconditions, natural-key and foreign-key handling,
DML limits, and rehearsal instructions. The baseline-data folder is installed separately after the DDL bundle.
Empty output categories do not create folders.

For example, this creates the optional bundle without applying it:

```bash
scripts/team.sh baseline build --from dev --to staging --bundle customer-status
```

New folders pass the normal migration loader and SQL/check validators before
exclusive installation from private staging outside `migrations/`. Identical
builds preserve folder bytes and evidence; changed input creates the next
revision. Unknown evidence, revision gaps, and flat/schema layout conflicts
refuse generation. Existing attempted or applied folders remain immutable; a
later generation error may leave earlier fully validated folders in place, and
the command reports their paths rather than claiming multi-schema atomicity.
On macOS, final installation uses `renamex_np(RENAME_EXCL)` and requires
filesystem support for exclusive renaming; if unavailable, generation refuses
without a replacing-rename fallback. See [baseline export and migration
generation](baseline.md#folder-safety-and-limits) for platform details.
Oracle DDL can commit implicitly, so a DDL step or later check can fail after
earlier DDL took effect. Inspect the target and use a reviewed recovery
migration; never edit or replay an attempted or applied folder.

`baseline export-data --from <env>` captures only the tables and columns named
in `referenceData.tables`, under `scratch/baseline/<env>/data/<schema>/`. Each
table declares excluded columns, natural-key columns, matching label columns,
identity handling, and a row limit. Export uses the read-only SQLcl catalog
session and refuses incomplete paging or a cap hit. The `baseline build`
command accepts `--to <env> [--from <env>] --data`; it reads a prior source
export and observes the target tables before generating a `baseline-data`
folder. It inserts only rows with
missing natural keys, groups DML into bounded steps, preserves existing rows,
and creates count-based checks. Use `migrate <folder> --env <env> --rehearse`
to rehearse and roll back the eligible DML; identity advancement is a separate
DDL step and is skipped during rehearsal.

### Reference data: never copy foreign keys by id

When a foreign key points to another allow-listed table, the generated insert
resolves the target row through the parent's configured label columns. Source
IDs never enter the child insert. A missing or duplicate target parent label
blocks generation; different non-key values on a same-key row are reported as
a precondition-visible difference and the row is left untouched. This avoids
copying a policy row's source reason ID into a target schema where that ID
belongs to a different reason. See [baseline configuration and limits](baseline.md).

`baseline filter-ords --exclude-module NAME [...] --input <file> --output
<file>` validates an ORDS schema export, removes the named module together
with its templates and handlers, and refuses a name that is not present. Use
the filtered output as the file of an `ords-import` rollout step. Filtering is
local and does not change the source export.

The generator handles guarded creates, missing columns, selected named
constraints and indexes, not-null transitions, sequence/identity advancement,
additive object grants and exact stored source. It does not generate drops,
system privilege grants or general column/table alteration migrations. Review
all reported environment differences that fall outside these generated
classes. `baseline.json` can carry a `referenceData.tables` allow-list for a
schema-specific export and additive reference-data build. See
[baseline configuration and limits](baseline.md).

Exit status: **0** means all selected sections were complete and identical;
**1** means complete capture with differences; **2** means capture error,
readiness blocker, missing DBA connection or cap/incomplete-page refusal.


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
