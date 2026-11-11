# Feature-oriented releases

Use mutable feature files for new object work that benefits from complete
stored-unit sources, compact Git diffs, and one frozen payload for staging and
production. Existing dated migrations and schema-version 1 rollout manifests
keep their current behavior.

## Scope and revisit criteria

Feature releases do not replace dated migrations yet. Today:

- `feature capture` reads one stored unit: a package or type (specification and
  body), or a single trigger, procedure or function. Views go in the feature's
  `ddl.sql`. Destructive DDL (`DROP`, `RENAME`, `TRUNCATE`) and anything the DDL
  forms refuse stay in dated migrations started with
  `scripts/team.sh new-migration --source`.
- Feature files have no DEV apply path. `feature capture --env dev` reads DEV into
  the repository and writes nothing to DEV. `rollout` refuses `--env dev` for a
  feature capsule and runs only `--env staging` or `--env prod`.

Revisit whether dated migrations can be retired for object work only when all of
these hold: one feature release has moved through DEV, then staging, then
production under a frozen capsule; a DEV apply path for feature files is
documented; and the data-step rule for features is settled.

## Create and review feature source

`feature init` is offline. It creates a shared README when one is absent and a
schema-level `checks.json` with a deliberately failing `replace-me`
postcondition. Later schema initializations retain the existing README and
checks for other schemas. Replace each new schema check with a reviewed,
meaningful condition before including that schema in a release.

```bash
scripts/team.sh feature init inventory --schema APP_CODE
scripts/team.sh feature capture inventory --package ITEM_LOOKUP --schema APP_CODE --env dev
scripts/team.sh feature capture inventory --trigger ORDERS_TRG --schema APP_CODE --env dev
scripts/team.sh feature diff inventory --schema APP_CODE
```

Capture takes exactly one unit option. `--package` and `--type` read the complete
live DEV specification and body, when a body exists; `--trigger`, `--procedure` and
`--function` read the single unit. Each capture stores a retained source baseline
under ignored `scratch/`. It uses the `CODE_*` profile, not `MIGRATION_*`. A feature that touches more than one schema
needs every DEV schema listed in `CODE_SCHEMA`, with aligned `CODE_SQLCL_CONNECTION`
and `CODE_EXPECTED_USER` values; otherwise the second schema's capture is refused. It
refuses to replace edited source; use `--refresh` only after reviewing the live
change and reconciling the source. `diff` is offline and writes source diffs and
review metadata under `ai_generate/YYYY-MM-DD/feature-diffs/`. Add its output to
the day's single `WALKTHROUGH.md` as described in
[daily change records](daily-change-record.md).
Review directories use bounded ASCII type/name hints and digest suffixes.
Capture preserves existing short source paths and uses a bounded hashed stem
for longer quoted identifiers; metadata and review files retain the full
case-sensitive identifier and source hashes.

One-time DDL may live in `features/<slug>/<SCHEMA>/ddl.sql`. Complete stored
units live under `features/<slug>/<SCHEMA>/`, one directory per unit group:
`packages/` (`.pks.sql`, `.pkb.sql`), `types/` (`.tps.sql`, `.tpb.sql`),
`triggers/` (`.trg.sql`), `procedures/` (`.prc.sql`) and `functions/`
(`.fnc.sql`). Keep each specification and body as whole, ordered files. Creating a type or package
specification invalidates its body, so a release that re-creates a specification also
re-creates its body in the same checkpoint, even when the body text is unchanged. Checks use
the existing schema-version 1 `checks.json` format. Version-2 SQL-script and ORDS steps name a logical
`schema`; the selected environment's migration profile supplies the connection
and login. ORDS imports must use the actual REST owner named in the frozen
export. Schema-version 1 keeps its explicit connection and expected-user fields.

### DDL a feature checkpoint accepts

`feature freeze` and `rollout --prepare` read a `ddl.sql` with the same rules
(`scripts/feature_ddl_forms.py`), so a statement that freezes is also one that
prepare can prove. Each form below names only objects of the checkpoint's own
schema, and prepare generates a catalog check for it: a before-state check that
refuses an occupied name or a missing prerequisite, and an after-state check
that reads the effect back from the catalog.

| Form | What the after-state check proves |
| --- | --- |
| `CREATE TABLE`, `CREATE SEQUENCE`, `CREATE [UNIQUE] [BITMAP] INDEX`, `CREATE [OR REPLACE] VIEW`, `ALTER TABLE ... ADD (column)` | The object, its columns or its index columns exist as written. `CREATE VIEW` supports CTEs (`WITH cte AS ...`), inline views, comma joins, and subqueries with scope-aware dependency extraction. Function-based / expression indexes prove uniqueness, total column count, and expression count in `ALL_IND_EXPRESSIONS`. |
| `ALTER TABLE t MODIFY (column_spec, ...)` | Modified column data type, length/precision/scale (`VARCHAR2(n [BYTE\|CHAR])`, `NUMBER(p[,s])`, `DATE`, `TIMESTAMP`, `CLOB`), and nullability (`NULL` / `NOT NULL`) exist as stated. |
| `ALTER TABLE t ADD [CONSTRAINT n] PRIMARY KEY\|UNIQUE (cols)` | A constraint of that type exists on `t` with exactly those columns in that order, and its status and validation state. |
| `ALTER TABLE t ADD CONSTRAINT n FOREIGN KEY (cols) REFERENCES p [(cols)] [ON DELETE CASCADE\|SET NULL]` | The constraint exists, its parent table (and parent columns, when written) and delete rule match. `p` must be in the same schema. |
| `ALTER TABLE t ADD CONSTRAINT n CHECK (expr)` | A CHECK constraint of that name exists on `t` with the stated status. The expression text is not compared. |
| `COMMENT ON TABLE\|COLUMN ... IS '...'` | The catalog comment equals the text (an empty text proves the comment is cleared). |
| `CREATE [OR REPLACE] [PUBLIC] SYNONYM s FOR t` | The synonym exists and points at `t` in the same schema. |
| `GRANT privilege ON [schema.]object TO grantee` | The grant exists. Accepts unqualified objects defaulting to the checkpoint schema, `EXECUTE` on packages/functions/procedures, and `SELECT` on sequences. |

Constraint options are limited to `ENABLE`, `DISABLE`, `VALIDATE` and
`NOVALIDATE`. A named constraint must have a name that is free in the schema; a
`PRIMARY KEY` or `UNIQUE` name also takes the index namespace. An unnamed
`UNIQUE` or `PRIMARY KEY` is refused when an identical key already exists. Name
every `CHECK`: an unnamed one has no catalog identity to prove.

What the after-state checks do **not** compare: the text of a view's query or
of an index expression, and the text of a `CHECK` condition. A view is proven to
exist and be valid; an expression index to exist with the stated uniqueness and
the stated number of columns and expressions. Review those definitions in the
diff. The dependencies of a view (CTEs, inline views, comma joins, parenthesised
joins, subqueries at any depth) are extracted by a scanner that is compared with
Oracle's own `ALL_DEPENDENCIES` by a live test, and any view it cannot place with
certainty is refused rather than guessed.

These stay refused and belong in a reviewed migration or workflow: `DROP`,
`TRUNCATE`, `RENAME`, `DROP` or `ENABLE`/`DISABLE` of an existing object,
`USING INDEX`, deferrable constraints, a parenthesised `ADD (...)` list that mixes
columns and constraints, `MODIFY` clauses outside simple datatype/nullability
(such as `DEFAULT`, `INVISIBLE`, `VIRTUAL`, inline constraints), any reference to another
schema, a `COMMENT ON` other than `TABLE` or `COLUMN`, complex/uncertain view constructs
(`@dblink`, `TABLE(`, `LATERAL`, `JSON_TABLE`, `XMLTABLE`, `AS OF`, `VERSIONS`, `SAMPLE`,
`MODEL`, `PIVOT`, `UNPIVOT`), and any statement the rules above do not describe.

The object budget covers feature checkpoints and `migrate` steps. A SQL-script is counted as
the separate data phase only when every statement is lexically validated,
top-level `INSERT`, `UPDATE`, `DELETE`, or `MERGE`, plus optional explicit
`COMMIT`/`ROLLBACK`. DDL, PL/SQL blocks, calls, queries, and other forms remain
uncovered object work and block complete fast-path promotion. Recognized ORDS
exports keep their own phase and timing after the existing structure, OAuth,
exclusion, and owner checks. Static DML classification describes the source
statements; it cannot rule out trigger, autonomous-transaction, or external
effects. Keep data repair that needs object-level checks in a separately
reviewed migration or workflow.

## Ownership and DEV drift

A capsule installs the feature file, not DEV. Two checks protect that, both described in
[feature ownership and DEV drift](feature-ownership.md):

```bash
scripts/team.sh feature doctor                 # offline: a unit claimed by two features, orphan checks
scripts/team.sh feature doctor --live          # also compares every captured stored unit with the DEV source (read-only)
scripts/team.sh feature capture orders --package ITEM_LOOKUP --schema APP_CODE --env dev --move-from inventory
```

`feature capture` refuses a unit another feature holds; `--move-from` names the feature it is taken from. A move
or a `--refresh` stops when the file it replaces has lines DEV does not have (`--discard-unapplied` erases them,
`--keep-file` moves the old file unchanged). `feature freeze` refuses a unit held by two selected features and
compares the selected stored-unit files with DEV when a DEV profile is configured (`--no-live "<reason>"` skips it;
the reason is kept in `scratch/freeze-checks/`).

## Freeze and prepare

Copy [the example release manifest](feature-release.example.json) to
`releases/current.json`, then select the real source paths, step order, app IDs,
and reviewed database-object budget. Version 2 defaults to 120 seconds and
accepts a budget from 1 through 300 seconds. Commit the selected source and
manifest before freezing; unrelated local edits do not block the freeze.

```bash
scripts/team.sh feature freeze releases/current.json
scripts/team.sh rollout releases/current.json --env staging --dry-run --capsule <capsule-dir>
scripts/team.sh rollout releases/current.json --env staging --prepare --capsule <capsule-dir> --report scratch/staging-prepare.json
```

`feature freeze` validates the version-2 manifest, hashes its selected sources,
checks that those bytes are committed, runs the ownership and analysis checks,
and creates an environment-neutral capsule under `scratch/release-capsules/`.
It needs no `.env`, SQLcl, or network unless a DEV profile is configured. When
one is, freeze also compares the selected stored-unit files with DEV (read-only),
and stops with exit 2 if DEV cannot be read, unless `--no-live "<reason>"` is
given; the reason is recorded. A file that a release would undo on DEV also
stops it, with exit 1. The [feature ownership and DEV drift](feature-ownership.md)
guide lists each check. Save the capsule path it prints and pass that same path
to every later command.

`--dry-run` checks only local source, manifest, capsule, and hashes. `--prepare`
uses the selected environment's guarded read-only capture to make exact
before-state, compiler-setting, structural, and descriptor checks. Review the
target plan and generated diffs before execution.

A trigger that the target has DISABLED is a refusal at prepare when the release
would replace it: `CREATE OR REPLACE TRIGGER` leaves it enabled. The refusal names
the trigger. Enable or disable it in a reviewed migration instead, and keep it out
of the release. A trigger the release leaves unchanged is not replaced, so it is
allowed in any state. Apply rechecks that a replaced trigger is still ENABLED before
it writes. The capsule payload digest is
shared by staging and production; the target-plan digest is specific to each
environment. Re-run `feature freeze` and prepare again if selected source or
the manifest changes.

## Execute staging and promote

Execute staging only after reviewing its target plan:

```bash
scripts/team.sh rollout releases/current.json --env staging --capsule <capsule-dir> --report scratch/staging-rollout.json
```

The command requires the matching prepared staging plan and one confirmation
that includes the payload, workflow-engine, and target-plan digests. It
revalidates target identity, selected source and metadata before writing, then
applies each feature checkpoint in one SQLcl session and proves source,
settings, validity, and checks afterward. A lost or ambiguous write remains
unresolved. Recovery preparation displays the attempt's target scope and saved
evidence, refuses while a local writer lease is active, and requires the exact
typed acknowledgment `RECONCILE <attempt-uuid>`. That acknowledgment asserts
the original SQLcl process and database operation have ended; a free local lock
or stale PID file does not establish this.

```bash
scripts/team.sh rollout releases/current.json --env staging --prepare --capsule <capsule-dir> --recover <attempt-uuid>
```

Recovery restarts the frozen step order from the beginning. It refuses before
confirmation when an earlier migration, SQL script, ORDS import, or APEX import
already has a verified receipt, because replaying that completed write could
duplicate data changes or overwrite intervening application work. The refusal
keeps the attempt, receipt, and capsule intact. Do not rerun that capsule; retain
the evidence while preparing a separately reviewed recovery plan that does not
repeat the completed writes. The runner will not infer that replay is safe.
A `migrate` step counts as a writing step here too: a parent killed before it ran
can be recovered, and a parent whose migration verified cannot.

A rollout killed mid-step may record no outcome for that step or any later one.
Recovery accepts such a parent, provided every earlier writing step has a
verified outcome. Once an acknowledged recovery has verified the interrupted
checkpoint, the killed run's record no longer blocks later runs on the same
target. Those runs must still pass the same occupancy checks as any other.

Production needs its own prepared production plan and a successful staging
report for the same capsule:

```bash
scripts/team.sh rollout releases/current.json --env prod --prepare --capsule <capsule-dir> --report scratch/prod-prepare.json
scripts/team.sh rollout releases/current.json --env prod --capsule <capsule-dir> --stage-report scratch/staging-rollout.json --report scratch/prod-rollout.json
```

Production validates the staging report's payload and engine digests, shared
source hashes, complete in-budget coverage of every feature checkpoint and every
`migrate` step (`includedMigrationSteps`), and matching data/ORDS phase
classifications before it asks for confirmation. It uses the staged capsule
payload and independently revalidates the production target plan and app
descriptors. SQL, ORDS and migration targets are observed during preparation and
must still match before their run-bound writer lease allows a write. A changed
capsule, target profile, migration folder, or source blocks success receipts.

A `migrate` step in a schema-version 2 manifest is a prepared checkpoint. Prepare
binds it to its target like a SQL or ORDS step, records the payload and check
digests of its folders, and runs the read-only live migration preflight. A clear
preflight is recorded as `clear`. When the only conflicts are missing prerequisites
that an earlier feature or migrate step of the same schema creates, it is recorded
as `deferred`, with those conflicts, and the apply-time preflight inside `migrate`
decides. Only prerequisites the analyser reports as missing are deferred, and the
comparison uses the resolved target schema, not the logical name, so a flat migration
folder and a DEV schema mapped to a differently named staging schema both defer. Any
other conflict or error refuses preparation. A failing explicit precondition in the
migration's own `checks.json` (code `CHECK_FAILED`) refuses preparation even when an
earlier step would make it pass, so write such a condition to hold before the release,
or make it a postcondition. At execution the step
takes the target writer lease only after the binding, target identity and folder
digests match the prepared plan. `migrate` still runs its own preflight, verification
and receipt.

The engine digest binds the ordered release modules and their runtime helpers,
including SQLcl transport, target/source checks, trusted SQL templates, and the
existing APEX deploy-and-verify path. Capsule preparation refuses if a required
engine file is missing; a helper change makes an older capsule unusable.

## Migration-only releases

A schema-version 2 release may contain no `feature-migrate` step, as long as it has at
least one `migrate` step. Such a release freezes the migration folders' bytes and runs
them as prepared checkpoints, as in a mixed release. It has no feature checkpoint, so:

- `includedCheckpoints` is `[]`, and no checkpoint verification is recorded.
- The target scopes come from the migration bindings only.
- `--recover` is refused, because there is no checkpoint attempt to recover. A
  migration step that refused before its write leaves the target free: once the cause
  is fixed and the target is prepared again, the same capsule can be run again, provided no step of it has a
  rollout receipt for that environment yet (receipts are never replaced; see below). A
  migration step whose outcome is unknown is reconciled live as
  [migration rules](migration-rules.md) describe. The run is then closed with
  `feature reconcile-run` (see below), the interrupted folder stays locked by the
  migration rules, and the follow-up goes into the next revision folder (`revise`)
  with a new capsule.

```bash
scripts/team.sh feature freeze releases/current.json
scripts/team.sh rollout releases/current.json --env staging --prepare --capsule <capsule-dir> --report scratch/staging-prepare.json
scripts/team.sh rollout releases/current.json --env staging --capsule <capsule-dir> --report scratch/staging-rollout.json
scripts/team.sh rollout releases/current.json --env prod --prepare --capsule <capsule-dir> --report scratch/prod-prepare.json
scripts/team.sh rollout releases/current.json --env prod --capsule <capsule-dir> --stage-report scratch/staging-rollout.json --report scratch/prod-rollout.json
scripts/team.sh feature clean-capsule <payload-sha256>
```

A manifest with neither a feature step nor a migrate step is refused at freeze. The
freeze record under `scratch/freeze-checks/` states `migrationOnly` and `migrationSteps`.

## Closing a run that has no checkpoint to recover

A run that ended unknown, with no checkpoint attempt to recover, keeps its target
occupied until an operator closes it. This also applies to a failed SQL-script or ORDS
step, whose unknown outcome blocks the target in the same way.

```bash
scripts/team.sh feature reconcile-run <run-id> --env staging
```

The command reads only `releases/.local-state.json`. It needs no capsule, manifest,
`.env` or database. It shows the run id, environment, payload and plan digests, state,
database state, start and finish times, each target scope, each recorded step outcome,
and the evidence location (`scratch/rollout-runs/`). It then asks for the typed
acknowledgment `RECONCILE-RUN <run-id>`. No flag supplies it, and an empty answer or a
closed input is a refusal.

It refuses, and changes nothing, when the run id is unknown, when `--env` differs from
the run's environment, when the run is verified or failed with a database state other
than unknown, when the run already has a reconciliation record, when a checkpoint
attempt is still unresolved (recover that with `rollout --prepare --recover <attempt-id>`
instead), or when a target writer lease cannot be taken for every target scope, because
a process may still be writing.

The acknowledgment asserts that the original process and the database operation have
ended. A free lease alone does not establish that. On success, a running run becomes
`unknown` with a finish time, the row gains a `reconciliation` record with the
acknowledgment and the prior state, and the database state is left as it was. The run
then no longer occupies its targets. It is never proof of completion, and it keeps its
capsule ineligible for cleanup.

Exit codes: 0 reconciled; 1 declined, or the acknowledgment was wrong; 2 refused or an
error.

Rollout receipts are never replaced. When an earlier step of the closed run succeeded, it
has a receipt under `scratch/rollout-receipts/<manifest-sha256>/<env>/`, and a new run of
the same capsule on that environment is refused before any
write. Keep the receipts and evidence, inspect the target, and continue with a reviewed
continuation manifest (a changed manifest, frozen as a new capsule), as
[migration rules](migration-rules.md) describe for rollout receipts.

## Timing, attempts, and cleanup

Reports separate preparation, selected feature database-object time, APEX app
imports, ORDS imports, and data scripts. Object time covers checkpoint
revalidation, preconditions, apply, and postconditions for feature checkpoints,
and the elapsed time of each `migrate` step, which counts toward the same budget.
It is not a hard stop: Oracle DDL may commit before the command can report a
budget overrun. Data, ORDS, preparation, and manual pauses have separate times.
Unclassified SQL-script object work is listed as excluded, and its presence means
coverage is incomplete. A complete, within-budget report covers every capsule
checkpoint, every `migrate` step (`includedMigrationSteps`) and every classified
phase, not the whole database or deployment window.

Attempt state is local in `releases/.local-state.json`; it is not a shared
database ledger and cannot see another developer's repository. Its schema-2
index retains the original bytes and SHA-256 when upgrading a schema-1 index.
Target writer leases are OS-backed local locks; their metadata is diagnostic,
not proof that a database operation ended. Cleanup is a dry-run by default:

```bash
scripts/team.sh feature clean-capsule <payload-sha256>
scripts/team.sh feature clean-capsule <payload-sha256> --yes
```

Apply cleanup only after verified production and when no unresolved attempt
references the capsule. It removes generated capsule and plan artifacts, keeps
compact receipts and review diffs, and records which paths were removed.

## Converting an existing project later

Template upgrade does not convert or archive downstream migrations. In a later
project task, inventory and reconcile every old attempt first. Derive each
final package spec/body once, keep DML separate, then checksum-archive the exact
legacy SQL, checks, receipts, and attempt evidence. Verify the archive checksum
and contents before removing converted active folders. Retire an old archive
index only together with its entire converted legacy tree. Never leave active
revision families pointing into a removed archive. Keep the archive and
conversion review in project-owned paths; do not put customer source in this
template guide or example.

### Plan the conversion offline

`feature convert-plan` reads the named migration folders and prints, per folder, the stored units with the
`feature capture` command that takes their current DEV source, every statement with its verdict (accepted by the DDL
forms, refused with the reason, or data), the data-only files, and whether local receipt or attempt evidence locks the
folder. Each folder is convertible, partly convertible, or kept as a migration. It writes nothing, reads no database,
and needs no `.env`:

```bash
scripts/team.sh feature convert-plan migrations/APP_CODE/2026-01-05_items-r001 migrations/APP_CODE/2026-01-06_orders-r001 --feature inventory
scripts/team.sh feature convert-plan migrations/APP_CODE/2026-01-05_items-r001 --format json
```

Exit 0 when every folder was analysed; 2 when any could not be, including a folder whose local evidence is unreadable.
Read the plan with these rules:

- Reconcile every old attempt first (`scripts/team.sh revise --check <folder>`).
- A folder with local receipt or attempt evidence has been applied or attempted: its one-time DDL is archived, not copied into a
  feature. Its stored units are still captured from DEV.
- Capture reads DEV when it runs. Run it after the folder's change is applied to DEV, or the captured source is older
  than the migration.
- A partly convertible folder stays in `migrations/`, because folders are not split. Anything copied or captured from it
  would also run from the migration.
- A flat `migrations/` folder's schema is unknown offline. Its verdict is provisional and its capture commands show
  `<SCHEMA>`.
- The plan ends with the `archive-migrations` command for the fully convertible folders. `archive-migrations` archives
  only a contiguous low prefix of each family, so a later revision is left out while an earlier one stays a migration.
  It is a dry run unless `--yes` is added.

Offline tests and hosted CI do not prove live staging performance. A measured
claim needs a separately authorized disposable staging rehearsal with the
actual selected payload and final source/settings/validity read-back.

To exercise this path against a disposable schema, run the opt-in live checks
described in [live feature-release checks](live-feature-tests.md). CI does not
run them.
