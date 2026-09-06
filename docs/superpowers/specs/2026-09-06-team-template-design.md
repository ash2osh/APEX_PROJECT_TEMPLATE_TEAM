# APEX_PROJECT_TEMPLATE_TEAM — Design

**Date:** 2026-09-06
**Status:** Revised after design review; implementation and live acceptance gates remain pending
**Relationship:** Sibling of `APEX_PROJECT_TEMPLATE`, which remains unchanged
and continues to serve single-developer projects.
**Plain-language explainer:** [`docs/working-on-apex-together.html`](../../working-on-apex-together.html)
— the same material for a reader who does not need this level of detail. Keep
the two in step when a decision here changes.

---

## 1. Why a separate template

`APEX_PROJECT_TEMPLATE` treats **the database as the source of truth and files
as a derived mirror**. Every design decision in it follows correctly from that
premise: the atomic directory swap, the dirty-mirror refusal, the manifest
completeness guard, the "never hand-edit `database/`" rule.

Team development requires the **inverse premise**: git is the source of truth
and the database is a deployment target. This is a direction reversal, not a
feature flag. The two premises produce contradictory guards, so they must not
share a repository.

### The failure this fixes

In the solo template, `scripts/export_apps.sh` guards the destination with:

```
git status --porcelain --untracked-files=all -- apps/<schema>/<app-id>
```

That detects only **uncommitted** work. `scripts/replace_mirror.sh` then
performs `mv DEST BACKUP; mv STAGED DEST` — a wholesale directory swap.

Consequently: developer A commits an export containing `p00020-checkout.apx`;
developer B pulls, then exports from a database lacking page 20; the guard
passes because B's tree is clean; the swap deletes page 20. **Git records this
as an intentional deletion, so the subsequent merge raises no conflict.** The
loss is silent at every stage.

A second, deeper gap: the solo template has no import path at all. No
`apex import`, no `project deploy`, no Liquibase. The flow is strictly
database → files, one-way. Merged files are therefore unrunnable, which
defeats the purpose of merging.

That gap also creates a live contradiction in the solo template today:
`AGENTS.md` §3 instructs agents to edit APEXlang in place under `apps/`, and
the `uc-apx` workflow provides `create`/`edit`/`delete` that do exactly that —
but once committed, those edits are destroyed by the next export, because
nothing can push a file edit into a database. See §9 for the backport.

---

## 2. Target topology

Decided with the project owner:

| Dimension | Decision |
|---|---|
| APEX environment | **Each developer has their own workspace** on one shared database instance |
| Schema objects | **One shared schema set** — all workspaces parse into the same `TABLES_SCHEMA` / `CODE_SCHEMA` |
| Integration point | **Git** |

A consequence that constrains everything downstream: **APEX application IDs are
unique per instance, not per workspace.** Oracle's documentation states the
application ID must either not exist in the instance, or already belong to the
target workspace. So on one shared instance each developer's copy of the same
application necessarily carries a different numeric ID — Alice 101, Bob 201,
integration 301 — while being the same application.

The tracked source therefore must not be keyed on the application ID, nor on
the parsing schema.

---

## 3. Evidence base

Verified against the `docker-demo` connection on 2026-09-06:
database `FREEPDB1`, service `freepdb1`, `SESSION_USER=DEMO`,
`CURRENT_SCHEMA=DEMO`, **APEX 26.1.4**, application 100
(*Employee Self Service*).

### Verified

1. **APEXlang export is deterministic.** Two consecutive
   `apex export -applicationid 100 -exptype APEXLANG` runs into separate
   directories produced identical file lists and **byte-identical content**
   across all 29 files (`diff -r` clean). A clean export therefore yields an
   empty `git diff`, which is the precondition for diffs and merges to carry
   meaning.
2. **The export is environment-neutral except for subscription masters.** The
   parsing schema, workspace name, and `#OWNER#` appear **nowhere** in the
   export. The application's *own* ID appears only in
   `deployments/default.json`.

   **However** — established by S3 below — a subscribed shared component
   embeds its **master application's ID** directly in tracked source:

   ```
   subscription { master: @/500/opendoor-master }
   ```

   So master application IDs are part of the environment contract. See R1 in
   §9.
3. **`.apx` references are symbolic.** The application is identified as
   `app EMPLOYEE-SELF-SERVICE`, pages as `page: 1` / `page: LOGIN`, components
   as `@navigation-menu`, `@universal-theme`.
4. **`apex import` supports retargeting.** The locally installed SQLcl help
   documents:
   ```
   apex import -input ./projects/brookstrut -id 1001
   apex import -input ./projects/brookstrut -workspaceid 90000
   ```
   plus `-deployment|-d`, `-schema`, `-alias`, `-name`, `-workspace`.
5. **Deployment files can target different environments.** Oracle's SQLcl 26.1
   documentation states that for JSON files under `deployments/` other than
   `default.json`, the connection is not shared, allowing a different target
   connection per deployment file.

### Spike results (run 2026-09-06)

Run behind `scripts/check_db_target.sh write apex` (exit 0;
`DB_ENVIRONMENT=development`).

**S1 — Round-trip fixed point: PASS.**
`apex import -input <export> -id 900 -alias ESS-SPIKE900` returned
*"Importing application ID: 900 into workspace: DEMO / Import successful."*
Re-exporting application 900 produced an identical file list, and exactly
**two** of 29 files differed — both being bindings deliberately changed:

```
deployments/default.json:  "id" : 100   ->   "id" : 900
application.apx line 1:    app EMPLOYEE-SELF-SERVICE (  ->  app ESS-SPIKE900 (
```

After normalising those two values, `diff -r` across the whole tree was clean.
**The `-id` remap works and the round trip is a fixed point.**

The alias varied only because this spike re-imported into the *same* workspace,
where aliases must be unique. In the target topology each developer has a
separate workspace, so the alias is unchanged and **only
`deployments/default.json` differs — a file this design gitignores. Every
tracked file therefore round-trips byte-identically.**

**S2 — Binary asset round-trip: PASS.** All five PNG static files compared
byte-identical after the import/export cycle.

**S3 — Shared component subscriptions: PASS.** A master library application
(500) was created with a list-of-values, an authorization scheme, and an
authentication scheme; application 900 subscribed all three. Exporting 900 and
importing it as 901 preserved every subscription with **identical** master
linkage:

| Component | app 900 | app 901 |
|---|---|---|
| Authorization `authorization-master` | `referenced_scheme_id=10437398563172219` | identical |
| Authentication `opendoor-master` | `referenced_schema_id=10436700311161979` | identical |
| LOV `MASTER-LIST` | `IS_SUBSCRIBED='Yes'` | identical |

**APEXlang represents subscriptions explicitly**, contrary to the published
documentation, which describes no such property. The syntax is uniform across
component types:

```
subscription { master: @/<master-app-id>/<component-identifier> }
```

The `@/500/...` form embeds the **master application's ID** in tracked source.
This is safe for the target topology — the project owner confirms the master
library application is the same application, at the same ID, for every
developer, including when lists are drawn from several master applications —
but it constrains promotion. See R1 in §9.

An earlier hypothesis that APEXlang silently flattened subscriptions into local
copies was **disproved**: a pre-existing Universal Theme subscription
(`get_subscription_id(4073840274158169736, 2000, 'universal-theme', …)`)
survived the S1 round trip byte-identically in the legacy export of app 900,
including `p_version_scn_master`. Note that this master lives in a *different
workspace* from `DEMO`, so subscription resolution crosses workspace boundaries
correctly.

**Test residue:** applications 900 (`ESS-SPIKE900`) and 901 (`ESS-SPIKE901`)
are disposable spike artifacts. Application 500 (`MASTER-APP`) was created by
the project owner as a fixture.

---

## 4. Architecture and revised decisions

Git owns application source and migration intent. Developer databases are
editable workspaces; export captures their changes and reconciles them into
Git. Integration consumes an immutable commit. A shared development schema is
not proof that the schema matches that commit: unmerged branch migrations may
already exist there. Disposable CI replay supplies that proof.

The four layers remain application, reconciliation, migration, and agent
context/promotion. The shell entry points delegate to one Python implementation
so safety decisions do not diverge between Bash and PowerShell.

D1: retain independent tables/code profiles, permitted to be equal. All
migration metadata lives in a separate shared METADATA_SCHEMA and is accessed
only through its controller profile. Payloads use tables/code profiles; a
separate VERIFY profile observes postconditions with read-only privileges.
These are shared infrastructure principals, not per-developer schemas.

D2: one short-lived feature branch per ticket. Conflicts in a captured page
are reported by content reconciliation. Direct shared-schema writes are not
serialized by Git; cooperative migration runners are serialized by a database
mutex, while out-of-band changes are detected only when observed.

D3: the resolved commit SHA is canonical for deployment. Integration is
deploy-only, with an explicit target role verified against an instance and
workspace identity. A filename such as default.json is not sufficient proof
that a target is a developer workspace.

D4: build one immutable release from a commit/tag, containing all migration
history through that commit. Select pending migrations against each
destination at application time. Test and production receive identical source
bytes; production receives an offline runbook and is applied by its owner
outside the repository's automated execution path. No production credentials
are supplied to CI.

These revisions replace the original filename-only reconciliation, directory
replacement, timestamp-only drift test, and target-specific pending release.

## 5. Source ownership and target identity

```text
apps/<alias>/                    tracked APEXlang, metadata and binary assets
  deployments/default.json      ignored local SQLcl binding
  deployments/integration.json  tracked credential-free SQLcl binding
  deployments/test.json         tracked credential-free SQLcl binding
targets/integration.json         tracked expected target contract
targets/test.json                tracked expected target contract
targets/masters.json             tracked master/component identity contract
.env                            ignored saved-connection names and profiles
.sync-state/                    ignored exact baselines, blobs and journals
scratch/                        temporary staging only
migrations/                     immutable SQL and verification files
database/<schema>/              generated evidence only
app_context/<alias>/             durable application knowledge
```

Alias grammar is lowercase ASCII `[a-z][a-z0-9-]*`; aliases must be unique
case-insensitively and must agree with application.apx. Alias renames are
coordinated source-and-target changes. Bindings are never inferred from an
export directory name.

`APEX_APPS=employee-self-service:101,hr-administration:102` remains the local
alias-to-ID mapping. The APEX profile also names workspace ID, expected user,
current schema, database name and service; tables/code, metadata and verification
profiles name their own expected users, current schemas, database names and
services. A stable database/container identity is also configured and verified;
a reused service name alone is insufficient. All profiles for one shared
schema set must resolve to that same identity. A
`TARGET_ROLE` is exactly developer, integration, test or replay.
`DB_ENVIRONMENT` remains development, test, staging or production.
A target role never overrides production classification.

The selected profile and deployment JSON must agree on application ID,
workspace, schema and connection. Reject any embedded alternative connection
or mismatch before import. Local default.json is an adapter generated from
the validated local profile; an existing conflicting file causes refusal.
Never change it as a side effect of export.

Deployment directories, logs, and connection data are outside the
export-owned source set. Reconciliation and application of an export never
delete or overwrite them. Maintain a tested allowlist of SQLcl-exported source
classes, including .apex/apexlang.json and binaries. Unexpected metadata or
paths cause refusal rather than silent loss or accidental tracking of secrets.

## 6. Content reconciliation and recoverable imports

### Exact state

A baseline is the exact normalized source tree last verified in this target.
Store a versioned JSON manifest and SHA-256-addressed source blobs under
`.sync-state/baselines/`. Its identity contains project ID, alias, instance,
service, workspace ID, application ID, parsing schema, target role and binding
digest. It records the source commit as provenance, but reconciliation uses
the actual blob manifest. Changing PROJECT_ENV_FILE or app ID cannot reuse a
baseline belonging to another target.

Missing baseline means unknown state, not an empty application. Capture the
database into a recovery bundle and refuse automatic reconciliation until
explicit bootstrap/import establishes a baseline. Corrupt Git refs, missing
blobs, incomplete exports, unsupported state versions and uncertain identities
also fail closed.

### Pure decision contract

Each tree maps POSIX relative path to normalized bytes; absence is a distinct
state and differs from a zero-byte file. Binary files use exact bytes. APEXlang
is normalized to LF before hashing; no broad text rewriting is allowed.

For each path in the union of base, head and mine:

| Condition, evaluated in order | Reconciled value |
|---|---|
| mine equals head | that value |
| mine equals base | head |
| head equals base | mine |
| otherwise | explicit conflict; no automatic application |

This includes add/add, modify/delete, delete/modify and binary conflicts.
A colleague-only addition is preserved from HEAD. A colleague-only deletion
is preserved as deletion. A stale export cannot recreate or erase either.
A rename is conservatively handled as deletion plus addition. No automatic
line merge is required in version 1; overlapping changes retain all versions
for a human to resolve.

The former test name `test_colleague_addition_blocks` is replaced by
`test_colleague_addition_is_preserved`: the dangerous deletion is prevented
by retaining HEAD, so refusal is unnecessary for an unambiguous case.

### Export sequence

1. Verify developer target role, source ownership, app alias and local state.
   Lock the local app operation; refuse staged, unstaged, untracked or ignored
   files overlapping the source ownership set. Deployment bindings are exempt
   from source cleanliness checks but validated separately.
2. Export into scratch using a verified read session. Require a complete
   versioned export manifest, expected source classes, application.apx, and
   positive completion evidence. Normalize .apx and hash binary bytes.
3. Persist the capture, base, HEAD manifest, target identity and diagnostics in
   `.sync-state/recovery/<operation-id>/` BEFORE changing any source.
   Recovery is durable local state, never an EXIT-trap scratch directory.
4. Resolve HEAD once and load exact base/head/mine. On conflict retain the
   bundle, print paths and recovery instructions, and change no source.
5. Recheck HEAD and source preimage under the operation lock. Apply only the
   calculated changed paths. Journal preimages and intended postimages before
   writes; use per-file temporary siblings and atomic replace. Explicit
   deletes must match their expected preimage. Reject traversal, symlinks,
   case collisions, device names and unsupported file modes.
6. Verify the complete resulting source manifest. An interrupted patch blocks
   subsequent operations until recovery finishes or restores preimages.
   Recovery must refuse to overwrite edits made after the interruption.

Export never advances the verified database baseline: reconciliation can
preserve Git changes not yet present in the database. When export successfully
places all Builder work into source, also record a capture receipt binding
that database-export digest to the result tree. This receipt lets a subsequent
import distinguish captured Builder edits from new uncaptured edits.

### Import, bootstrap and conflict recovery

Developer import accepts a clean application source at a resolved commit.
Materialize that commit into scratch and stage only the validated local
binding. Do not import a changing working directory.

Before every whole-application import, capture the existing database app into
durable recovery. If its digest differs from the verified baseline, allow
replacement only if it matches a receipt whose reconciled source is present
in the selected commit, or an explicit resolution receipt binds this capture
and selected source digest. Otherwise refuse and direct the developer to
export/reconcile first. Recheck the current capture before the destructive
step; Builder edits during the operation remain a documented race requiring
an operational pause.

`bootstrap-app <alias>` is only for creating source from an existing app when
that alias has no tracked source. It captures and writes a candidate source
tree without database writes. After the developer reviews and commits it,
`adopt-app <alias>` verifies equality by re-export before stamping a baseline.
Creating an absent database application uses ordinary import with a verified
"app absent" result. Import into an existing app with no baseline requires
explicit `--replace-from <recovery-id>`, bound to its exact captured digest;
the default is refusal.

Import validates master/component references and APEX source, guards the exact
write target, verifies identity in the write session and imports from staging.
A second export must match all owned source bytes and expected subscription
linkage before the baseline is committed atomically. Failure retains recovery
and marks the target uncertain; the old baseline is not claimed to be current.
An import timeout is not proof of rollback.

For a conflict, provide base/head/mine and a candidate result directory in the
bundle. `resolve-export <recovery-id> --resolved <path>` verifies conflict
coverage, paths and original HEAD/preimage, applies the reviewed result using
the same journal and records a resolution receipt. The developer commits the
result, then imports it. If HEAD moved, restart reconciliation using the
retained capture; do not discard Builder work or tell the developer simply
to "import first." No command commits or pushes automatically.

## 7. Migration intent, shared history and schema evidence

### Files and ordering

A migration consists of two immutable LF/UTF-8 files:

```text
migrations/20260906T143000__bob__delegation.sql
migrations/20260906T143000__bob__delegation.verify.sql
```

**Version 1 has no effects document.** Declared before/after object
fingerprints were specified and then removed: generating them honestly
requires replaying the dependency closure into a disposable database, and
developers in this topology may have no local database and no container
runtime at all. Requiring them would have blocked a developer from applying
their own change to the shared development schema — and therefore from
continuing to build the application that depends on it — until external
infrastructure produced a file. That cost was not worth the two checks it
bought. See "What version 1 gives up" below, which states the loss precisely
so it is a known trade rather than an oversight.

Structured metadata therefore lives in a strictly parsed header block at the
top of the primary SQL, before any executable statement:

```sql
-- migration-version: 1
-- target: tables
-- depends-on: 20260906T101500__alice__leave-balance sha256:9f2c…
-- destructive: false
```

`migration-version`, `target` and `destructive` appear exactly once;
`depends-on` repeats once per dependency and carries the dependency's exact
bundle checksum. No other `--` directive is recognised, and an unrecognised
one refuses rather than being ignored, so a typo cannot silently disable a
declaration. The header is covered by the bundle checksum, and on first apply
the resolved target and dependency set are recorded immutably in central
metadata: a later edit to either is a checksum conflict, so the header cannot
be quietly re-pointed after the fact.

Writing the header needs no database. `team.py new-migration` scaffolds it and
`team.py add-dependency <id> --on <id>` fills in a dependency's current
checksum, both operating offline on the migrations directory alone.

The verification file contains observation-only SELECT assertions returning
named PASS/FAIL rows, including data effects and compiled-object validity.
It executes through VERIFY, which owns no application or metadata objects,
has only required read grants and cannot execute application mutator routines.
Use a restricted statement/function allowlist; reject DML, DDL, PL/SQL,
transaction commands, user functions and SQLcl controls in verification members. A zero exit code without completion evidence is
not a successful verification. Hash a canonical manifest of both paths
and file hashes; changing either member after application is a conflict.

Order a dependency DAG with deterministic ready-node ordering
`(UTC timestamp, full migration ID)`. Reject cycles, missing dependencies,
duplicate IDs and dependency checksum mismatch. Timestamps reduce filename
collisions; they do not prove dependency order or safe late application.
Dependents changing the same object declare the dependency, and the declared
checksum binds the exact bytes depended upon, so editing an unapplied
dependency invalidates its dependents even when the resulting object would be
unchanged.

With effects preconditions gone, **the dependency DAG and post-apply
verification are the only ordering protections**. A migration that assumes an
earlier one has run must declare it; an undeclared assumption is no longer
caught before execution, only by its verification failing afterwards. Authors
must therefore treat `depends-on` as load-bearing rather than documentation,
and review must check for missing declarations.

Destructive resets belong in `operations/zz_*.sql`, outside migration discovery,
release payloads and ordinary CI. Destructive forward migrations declare
`-- destructive: true` in the header and require a separate, exact
target/filename/checksum confirmation. Automated integration/test pipelines
refuse such work and report a reviewed maintenance requirement; they never
silently omit it while declaring the release deployed.

### Central metadata and serialization

All metadata belongs to a distinct shared METADATA_SCHEMA and is accessed ONLY
through its controller profile. Payload and VERIFY principals cannot change
its objects or rows, execute controller routines or assume its identity.
Setup verifies effective privileges (including roles, ANY privileges and proxy
access); missing privilege-isolation evidence blocks writes. Controller
credentials and mutex tokens are never passed to payload processes or logs.
Reserve `TEAM_MIGRATION_*` names for infrastructure, reject them in application
schemas, and verify metadata structure independently of application snapshots. Store:
- an exact metadata schema version and project/schema-set identity;
- a persistent mutex with owner run token and state;
- immutable migration payload/checksum/target provenance and execution sequence;
- the target and dependency set resolved from each migration's header at first
  apply, recorded immutably so a later header edit is a detectable conflict;
- attempt records: RUNNING, APPLIED, FAILED or UNKNOWN.

Version 1 stores **no** expected object fingerprints. Expected schema state has
exactly one source — the canonical replay evidence under `database/` — rather
than a second, independently drifting copy in metadata.

Bootstrap is an explicit guarded non-production setup operation, idempotent
only if the existing metadata structure matches exactly. Normal apply can
bootstrap when requested with `--bootstrap`; planning/dry-run never writes.
Absent metadata otherwise produces a specific setup-required error.

**Acquire without ever waiting on a row lock.** A bare
`UPDATE ... WHERE owner_token IS NULL` is not acceptable: Oracle evaluates the
predicate against the reader's snapshot, then blocks on the row lock until the
holding transaction ends. A client that crashed or stalled before committing
therefore freezes every subsequent acquirer inside its SQLcl subprocess until
PMON cleanup or dead-connection detection — an indefinite hang presented as a
hung command. Acquisition must fail fast instead:

```sql
DECLARE
  v_token VARCHAR2(64);
BEGIN
  -- NOWAIT converts contention into ORA-00054 rather than an unbounded wait.
  SELECT owner_token INTO v_token
    FROM team_migration_mutex
   WHERE singleton_id = 1
     FOR UPDATE NOWAIT;

  IF v_token IS NOT NULL THEN
    RAISE_APPLICATION_ERROR(-20001, 'MUTEX_HELD:' || v_token);
  END IF;

  UPDATE team_migration_mutex
     SET owner_token = :run_token, acquired_at = SYSTIMESTAMP
   WHERE singleton_id = 1;
  COMMIT;
END;
/
```

The caller distinguishes four outcomes exactly: success; `ORA-00054`
(another transaction holds the row — an in-flight acquisition or an
uncommitted crash); `ORA-20001 MUTEX_HELD:<token>` (cleanly held, and the
token identifies the holder for the §7 blocking message); and `NO_DATA_FOUND`
(metadata not bootstrapped — a setup-required error, never an acquisition
failure). Any other error is a hard failure. A wall-clock timeout on the SQLcl
subprocess is a backstop for network-level stalls, never the primary mechanism,
and a timeout is never interpreted as "not acquired": it leaves the target
uncertain and blocked.

The same NOWAIT discipline applies to the Plan 1 app-target mutex.

Having acquired, check exactly one affected row before planning any
writes. The token persists
across DDL commits and across tables/code SQLcl processes. All metadata
transitions compare the token. Do not use a transaction-scoped row lock
that DDL releases. Do not expire or steal this mutex automatically.
A crashed runner leaves the target blocked; recovery requires evidence that
the owning worker has ended, its attempts have been inspected, and the exact
run token has been selected. Uncertain worker state remains blocked.
This serializes participating tooling, not manual SQL clients.

**The mutex has no automatic expiry, so it must have a human owner.** On a
shared development schema a single crashed client blocks every other developer
until someone clears it. An unowned blocking mechanism is one people route
around, which is worse than not having it, so the following is part of the
contract rather than operational advice:

- **A named recovery owner** is recorded in `targets/*.json` for each shared
  target — a role, not an individual, with at least two people in it. Only that
  role runs `recover-migration` or `recover-app-lock`.
- **Blocking must be self-explaining.** Every refusal caused by a held mutex
  prints the holding run token, the checkout UUID and host that acquired it,
  the acquisition timestamp and elapsed time, the migration or alias in flight,
  the attempt state, the recovery owner for that target, and the exact command
  the owner must run. A message that says only "another runner holds the lock"
  fails this requirement and is a defect.
- **The block is announced, not discovered.** Any hold exceeding a configured
  threshold is surfaced by the integration job as a visible failure naming the
  holder, so the team learns from CI rather than from a colleague's blocked
  export.
- **Recovery is evidence-driven and logged.** The owner records worker-
  termination evidence, the inspected attempt state and the selected run token;
  the metadata store retains that record. Clearing a lock never stamps a
  verified baseline and never marks an attempt applied.
- **Escalation is bounded.** If the owner cannot establish worker state, the
  documented path is to rebuild a disposable environment or coordinate a
  scheduled maintenance window — never to widen mutex privileges, add an
  expiry, or delete metadata rows.

Automatic expiry remains prohibited: a lease that expires while the original
worker is mid-DDL produces exactly the concurrent partial application the mutex
exists to prevent.

Read metadata through the metadata connection; execute a code payload through
the code connection; verify through VERIFY; record its result through metadata. There
is no cross-schema atomic transaction. On lost acknowledgement or partial
DDL, persist UNKNOWN if possible, otherwise leave RUNNING; never mark applied
or rerun it automatically.

### Apply and failure semantics

Under the mutex, confirm dependencies are satisfied, all known checksums match
and no attempt is RUNNING or UNKNOWN. Commit RUNNING before the payload.
Execute its exact staged bytes, capture the schema inventory before and after,
then run SELECT-only verification through VERIFY. Require no structural
mutation during verification. Mark APPLIED through the metadata owner, then
proceed to the next migration.

The captured inventories are **recorded as observed history, not compared
against a declared prediction** — version 1 has no such declaration. They serve
recovery and audit: after a failure they show exactly what the interrupted
migration did, which is what a human needs to repair it. Recovery inspects
actual objects and data against the captured before inventory and the
verification assertions; a corrective migration or an explicitly selected
restartable attempt can repair it. No automatic checksum repair or mark-ran.

### What version 1 gives up

Removing declared effects removes two checks, and no other. Stating them
plainly so the gap is deliberate:

1. **Wrong starting point is not detected before execution.** A migration
   applied to a schema that is not in the state its author assumed will run
   rather than refuse. Its verification should then fail, but the SQL has
   already executed and Oracle has already committed any DDL within it.
2. **Unintended scope is not detected.** A migration that changes more objects
   than its author expected is not refused; the before/after inventories record
   what happened, but nothing compares that against an expectation.

Everything else is retained: the mutex, bundle immutability and checksums, the
dependency DAG with checksum binding, the attempt state machine, target
routing, post-apply verification through VERIFY, shared-versus-strict history,
drift detection against canonical replay evidence, and the fresh and upgrade
replay gates.

Both lost checks can be restored later by adding an effects document without
changing anything else, because nothing else was built on top of them.
Retrofitting predictions onto migrations already applied is the real cost, so
revisit this before the migration history grows large. Reopen it if
authors start relying on undeclared ordering assumptions, or if a
wrong-starting-point incident reaches a strict target.

Oracle DDL commits implicitly; a failed multi-statement migration can leave
durable partial changes. The unit is a restartable, self-verifying transition,
not an atomic transaction. See Oracle's
[COMMIT reference](https://docs.oracle.com/en/database/oracle/oracle-database/19/sqlrf/COMMIT.html).
There is a real interruption window between payload success and metadata
recording, and the attempt state models it explicitly.

### Shared development versus promotion

For developer and shared integration targets, APPLIED records absent from the
current branch are reported as `foreign_applied`, not treated as proof of
deleted history. Allow unrelated migrations only if all local known checksums
match, dependencies are satisfied, and no attempt is RUNNING or UNKNOWN.
Without declared preconditions there is no object-level precondition check, so
a colleague's unmerged migration touching the same object is **not** detected
automatically here; same-object conflicts require coordination and updated
dependency declarations, and review is the control that catches them. Unknown/failed attempts block all new
work. Integration reports foreign IDs and cannot claim its shared schema is
exactly HEAD.

Test and replay targets enforce strict history: any applied ID absent from
the selected release is a refusal. CI compares committed history to the
protected base branch and rejects mutation/removal of canonical migrations.
Abandoned development migrations remain in the shared log: integrate their
original intent plus a forward correction, or rebuild a disposable environment;
do not delete history to make the branch appear clean.

### Drift, baselines and replay

`check-drift` compares complete supported object inventories and canonical
fingerprints across TABLES_SCHEMA and CODE_SCHEMA. Report added, missing,
changed and invalid objects. LAST_DDL_TIME is a diagnostic hint only.
Unrelated migrations cannot hide existing drift. Structural fingerprints do
not detect DML or a transient edit restored before observation; data checks
belong in migration verification scripts. Polling does not prove every
concurrent write was observed.

Version 1 coverage is explicitly tables (columns and constraints), indexes,
views, package specs/bodies, standalone routines, triggers, sequences
(definition only, excluding current counters), types/bodies, private synonyms
and object grants. Normalize physical storage, timestamps and environment
owner mappings through parsed metadata fields, never regex substitutions in
SQL literals.

**Compilation status is excluded from every fingerprint.** Oracle invalidates
dependent objects as a side effect of upstream DDL: adding a column to a table
in `TABLES_SCHEMA` immediately marks dependent views, package specs and bodies,
and triggers in `CODE_SCHEMA` as `INVALID`, without their definitions changing.
If `STATUS` contributed to a fingerprint, a migration that altered a table
would change the fingerprints of objects it never touched, and the very next
migration would be refused by the precondition check — one migration blocking
its own successor. Fingerprints therefore hash the canonical object
definition only. Concretely:

- `STATUS`, `LAST_DDL_TIME` and any other volatile dictionary column are
  excluded from fingerprint input.
- Between migrations, the runner performs **targeted** recompilation of
  objects on the declared dependency chain (`ALTER … COMPILE`), not a
  schema-wide sweep, so a genuine compilation failure is attributable to the
  object that caused it rather than masked by a bulk operation.
- Validity is not thereby ignored: post-apply verification asserts that every
  object expected to be valid actually compiles. An object that is `INVALID`
  and **fails** to recompile is a migration failure, reported with its
  compilation errors.
- `check-drift` reports invalid objects as **diagnostics**, separately from
  structural drift. An invalidation with an unchanged definition is expected
  after upstream DDL and is not by itself drift. Unsupported object classes, unresolved owners or unstable
normalization fail the replay gate until a reviewed adapter is added.
ORDS metadata, scheduler jobs, public synonyms and external services require
separate declared verification; no whole-system equivalence claim is made.

For an existing schema, author an initial migration from reviewed metadata and
explicit reference-data SQL, then prove it replays into an empty disposable
schema set. A separate `adopt-baseline` operation compares live structure and
verification results to that replay evidence under the mutex before recording
the baseline. It never executes initial CREATE statements over existing data
or silently blesses the live schema.

Generate committed `database/` evidence from canonical replay, not the shared
development schema with unmerged changes. Live diagnostic snapshots go to
scratch. Replay uses a fresh disposable instance/schema set with separate
exact saved connections and an explicit empty-target assertion. It compares
complete manifests, including additions, against committed evidence without
rewriting the working tree. Test both a fresh replay and an upgrade from the
previous release; schema equality alone does not validate data migrations.

## 8. Agent context and portability

Keep Graphify optional. Carry its tested APEXlang extractor and alias-keyed
apps/context allowlist; exclude deployments, sync/recovery state, migrations
as execution history, and generated logs. `database/` supplies canonical
object definitions, while context documents known shared-schema limitations.

Rewrite inherited instructions that conflict with team semantics. New SQL
intent belongs in migrations; source is editable under apps; database is
evidence; recovery bundles are never temporary scratch. Keep production,
secrets and exact-target gates. Do not install Graphify or uc-apx implicitly.

Project-wide constraints for all three plans:

- APEXlang uses LF; migration SQL, verification SQL and JSON use LF/UTF-8.
- SQLcl receives an empty regular stdin file and an @driver.sql argument.
  Never pipe payloads, use /dev/null, or override stdin with a heredoc.
- Generated SQL terminators use CHR(59); strip CR when parsing spooled output.
- Every SQLcl session has identity verification and strict error handling.
  Production sessions run SELECT statements only; no PL/SQL identity block.
- SET DEFINE OFF during source payloads; UTF-8 session and JDBC handling.
- Fail on missing completion records, incomplete manifests and unknown output.
- PowerShell uses -cmatch/-cnotmatch for case contracts and
  [Environment]::GetEnvironmentVariable for dynamic environment reads.
- No case-colliding paths; reject symlinks and unsafe path components.
- One Python 3.10+ core; Bash and PowerShell 5.1/7 launchers share behavior.
- Qualification baseline: APEX 26.1+, SQLcl with verified APEXlang import/export
  capabilities. Pin the exact SQLcl/JDK/APEX tuple after acceptance testing;
  the original proposed SQLcl 26.2+ floor is not an experimentally proven fact.
- No automatic commits, pushes, production writes or installed optional tools.
- METADATA_SCHEMA is distinct from tables/code; only the controller can mutate
  it. VERIFY is observation-only. Setup privileges are explicit prerequisites.

### Executable content in plans

Removing runnable examples was correct for anything that touches a database:
documentation must never be a copy-paste path to a live write, and a reader
must never be able to execute a step by accident. That rule was then applied
uniformly, which over-corrected — the plans lost the concrete test and function
bodies for pure, offline logic, where no such hazard exists and where precision
matters most. Both properties are wanted, so the rule is split by hazard rather
than by document:

- **Pure offline logic carries runnable code.** Modules that touch no database
  and no network — reconciliation, tree reading and digests, config parsing,
  bundle and manifest validation, fingerprint canonicalization, archive
  serialization — state their exact signatures and include real, runnable
  tests. These are the components whose subtle cases (missing versus empty,
  binary bytes, path splitting, ordering) are unrecoverable if specified in
  prose and got wrong.
- **Anything reaching a database is a directive, never a transcript.** No plan
  contains a runnable connect, export, import, apply or deploy command line.
  Such steps describe required behaviour, guards and evidence, and the
  implementation supplies the invocation behind the verified adapter.
  Illustrative SQL appears only where it defines a contract that cannot be
  stated in prose — the acquisition block in §7 — and is explicitly marked as
  a contract, not a command to run.

A plan step that cannot be executed without inventing an interface is
underspecified regardless of which category it falls into.

## 9. Master subscriptions and qualification boundaries

### R1 — Master application IDs are part of the environment contract

A subscribed shared component records its master as
`subscription { master: @/<master-app-id>/<identifier> }`, so the master's
numeric application ID is present in tracked source (§3). Every environment
hosting an application must therefore host its master library applications at
the same application IDs. This requirement is referenced from §3 and is the
reason source ID rewriting is rejected below.

Keep master IDs fixed across environments, with a tracked contract listing
master app alias/workspace and required component type/symbol. Existence of
app ID 500 alone is insufficient if it belongs to the wrong app or lacks the
component. Validate identity and component resolution in the actual target
workspace before import and linkage after import. Treat built-in theme
masters explicitly; a workspace-scoped view that cannot see a master is
inconclusive, not permission to skip it.

**No source ID rewriting in version 1.** Rewriting `@/<id>/` references per
target environment was considered and rejected: it would make tracked bytes
differ from deployed bytes, defeating the artifact-digest verification that
§11 depends on, and it introduces a transformation into a pipeline whose
principal virtue is performing none. The consequence is that fixed master IDs
across environments are a hard prerequisite, not a preference — record them in
`targets/masters.json` and reserve them deliberately. Reopen this decision only
if an environment is encountered where master IDs demonstrably cannot be
aligned; the replacement would be a build-time rewrite with its own digest,
never an import-time one.

The experiments in §3 demonstrate round-trip/linkage on one instance. They do
not establish that a same-numbered master on a different instance resolves
correctly, nor that missing masters always detach silently. Add cross-instance
positive and wrong/missing-component negative tests before claiming portable
promotion. Use the APEX skill and qualified SQLcl help for actual APIs;
do not guess undocumented dictionary columns or reservation procedure names.

Register one checkout UUID per developer target during explicit setup and
verify that registration through shared metadata before app mutations. A
second checkout targeting the same app refuses until an explicit transfer
captures current state and confirms the previous worker has stopped.

**The old UUID must never be required from local state.** A developer who
re-clones, moves machine, adds a worktree or loses `.sync-state` no longer
holds the previous checkout UUID, while the registry still records it. If
`--transfer-from` could only be satisfied from local state, that developer
would be permanently locked out of their own application — a deadlock created
entirely by the safety mechanism. The registry is authoritative and readable:

- `team.py app-status ALIAS` (also surfaced by `team.py doctor`) reads
  `TEAM_APP_REGISTRY` through the metadata profile and prints the registered
  checkout UUID, its host and user, the registration timestamp, and whether an
  app-target mutex is currently held. This is a read-only operation.
- `--transfer-from` is then always satisfiable, because its value is
  discoverable. It remains mandatory and exact: transfer is never implicit.
- Where no operation is in flight — no held app mutex, no incomplete journal,
  no uncertain baseline — transfer is **self-service**: capture current
  database state, record the transfer, rebind. This is the ordinary re-clone
  case and must not require the recovery owner.
- Where an operation *is* in flight, or worker liveness cannot be established,
  transfer escalates to the §7 named recovery owner under the same
  evidence rules. There is no `--force-takeover`: a flag that skips proof of a
  stopped worker reintroduces exactly the concurrent-overwrite failure the
  registry prevents.
- Every transfer is recorded in metadata with old UUID, new UUID, actor and
  the capture taken beforehand. Use a
persistent app-operation mutex keyed by instance/workspace/app ID for every
cooperating import/deploy; it survives client crashes and has no automatic
expiry. A file lock alone is not cross-checkout serialization. Plan 1 therefore
implements the small shared metadata bootstrap/app-lock foundation before
Plan 2 extends that same metadata store for migrations.

Concurrent Builder changes during export/import require a short edit pause.
Whole-application imports can partially fail. Local recovery journals and
database captures mitigate loss; they do not provide a database transaction
or recover edits never captured before another client overwrote them.

The solo-template import backport remains a separate scoped change.

## 10. Delivery and test gates

### Staged adoption and prerequisite ladder

This design asks for real infrastructure: four database principals, a separate
metadata schema, a disposable CI database and a signing key. Requiring all of
it before anything works would stall adoption, and the plans are ordered so
that it is not required. Each plan states exactly what it needs, and each
delivers standalone value.

| | Plan 1 | Plan 2 | Plan 3 |
|---|---|---|---|
| TABLES / CODE / APEX profiles | required | required | required |
| METADATA schema + controller | required (app mutex, checkout registry) | extended for migrations | extended for deployment |
| VERIFY observation-only profile | not used | required | required |
| Disposable CI database | acceptance only, manual | required for replay gate | required |
| Release signing key | not used | not used | required for production handoff |
| Graphify | never required — optional throughout | | |

**Plan 1 alone is a coherent deliverable.** It ends with developers exporting
and importing safely, with content reconciliation, durable recovery and
cross-checkout serialization. That already removes the silent-deletion failure
in §1, which is the reason this template exists. A team may run Plan 1 in
production use for as long as it likes before starting Plan 2.

The METADATA schema is a Plan 1 prerequisite and not deferrable: without it
there is no cross-checkout mutex, and two clones of the same developer target
can still overwrite each other. It is small at this stage — three tables — and
Plan 2 extends the same schema rather than introducing a second controller.

Do not treat the ladder as permission to ship Plan 2 without the disposable
database. The replay gate is what makes `database/` evidence trustworthy; a
skipped manual script is not a gate (see below).

Plan 1 implements capture/reconcile/import with state and target verification.
Plan 2 implements migration bundles, metadata, shared-history planning,
fingerprints and fresh/upgrade replay. Plan 3 supplies agent rules and actual
integration, release, test and offline production paths.

Required acceptance cases include stale existing-page export, colleague
add/delete, divergent text/binary edits, dirty source refusal, HEAD movement,
failed Git reads, target rebinding, uncaptured Builder changes, interrupted
file patch, failed import verification, split-schema metadata, concurrent
runners, lost acknowledgement, destructive refusal, foreign branch history,
older migration arrival, persistent drift, wrong subscription identity,
artifact tampering, and production refusal at every public write entry point.

The required CI database is a disposable verification environment, not a
schema-per-developer topology change. Provision it explicitly on an isolated
runner with the qualified toolchain and named connections. Missing resources
must fail the required gate; an optional skipped manual script is not CI.

## 11. Worked development and promotion flow

Alice imports a clean commit into her own workspace, edits page 6, exports,
reviews and commits. Bob's workspace still has the old page 6; after he pulls,
his export preserves Alice's content while carrying his page-7 edit. If both
changed page 6, export retains a recovery bundle and changes no tracked files.
Bob resolves base/head/mine, commits that result, and imports it. Import checks
that no additional Builder edits appeared since the saved capture.

A database change starts as SQL plus verification and replay-generated
its declared dependencies. The runner checks dependencies and known checksums
under its mutex, applies and verifies through the declared target, captures
before and after inventories for the record, and records history centrally.
A colleague's unrelated unmerged migration is reported without blocking Bob;
a conflicting object precondition or unexplained drift blocks both.

After merge, CI proves fresh/upgrade replay and deploys the selected commit to
integration. Shared integration reports any foreign migrations explicitly.
A release tag builds an immutable source artifact, applies its pending set to
test and verifies APEX round-trip/subscriptions plus schema/data checks.
Production gets the same canonical release.tar bytes, hashes, verified test
evidence and a runbook. The archive uses sorted POSIX ustar entries, fixed
uid/gid/mtime, explicit modes and no compression/PAX headers; unsupported
paths refuse. Hash that exact archive file, not a directory or CI wrapper zip.
The protected release record binds refs/tags/vX.Y.Z, manifest X.Y.Z, source
commit and archive digest and refuses version reuse or moved tags.
A signed test-evidence attestation binds archive digest, source SHA, qualified
toolchain, test target/run and successful verification results. Offline
runbook generation verifies that evidence with an explicitly trusted key before
labeling a release ready. The signed evidence remains separate from the archive.
Its owner obtains current history, selects pending work, verifies identity,
handles DDL failure/recovery and records results through the isolated metadata
owner using a separately controlled
manual procedure. The repository supplies no production apply switch.
