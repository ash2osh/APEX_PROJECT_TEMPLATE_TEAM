# Simple multi-app APEX team workflow — design

**Date:** 2026-09-23

**Status:** Proposed for owner review; no behavior in this document is implemented merely by writing it.
**Scope:** This repository, not the single-developer `APEX_PROJECT_TEMPLATE` sibling.

## Purpose and success

Make the team template simple enough for an Oracle APEX developer to use daily,
while preserving its protection against overwriting colleagues in a shared
Builder application. One environment has one database and one APEX workspace,
with multiple applications and a shared schema-migration stream. Developers may
edit an app in Builder, edit its APEXlang files manually, or ask an agent to do
either. A normal task should require one obvious command for the chosen path;
dangerous publishing must remain explicit.

Success is two developers safely working on different or the same app, and one
agent able to explain and run the same documented commands, without a private
agent-only bypass. The beginner README must teach this without requiring the
reader to learn release evidence internals first.

## Invariants

- APEX **26.1 or newer** and APEXlang are the only supported application
  authoring/export/import format. No legacy SQL-export source path is added.
- `apps/<stable-alias>/` is tracked app source. IDs are environment bindings,
  not source identity. Each app has **exactly one** parsing schema in an
  environment. Different apps may use different parsing schemas; multiple apps
  may share one.
- An environment has one `TABLES_SCHEMA` and one `CODE_SCHEMA`, which may be
  equal or different. They are one shared schema-migration domain for the
  project. Parsing schemas need not be either of them.
- The development workspace/application IDs are shared by teammates. Git
  branches do not isolate Builder. An export observes the whole app, including
  colleagues' saved changes, not merely the caller's changes.
- Production-classified database writes remain refused. Local bindings stay in
  ignored `.env` files, contain no credentials, and use saved SQLcl connection
  names. Target identity and schema drift are checked before database writes.
- Recovery captures, import receipts, locks/mutexes and failure journals stay
  durable under `.sync-state/`. No command silently stages, commits, pushes,
  imports, clears recovery, or unlocks another developer's page.
- An APEX import is a whole-application overwrite. It never refreshes a Git
  branch or discards Builder work.

## Chosen approach and alternatives

**Chosen:** Retain the qualified safety core and put a small, app-scoped
workflow over it. Builder-first uses `export-app`; file-first uses an explicit
prepare/publish pair that reuses the existing guarded `import-app` operation.
Schema migrations have their own environment release; each app has an
independent release artifact that checks its schema prerequisites. This keeps
the commands developers see short while allowing genuinely redundant code to
be removed after dependency and safety tests.

**Not chosen:** automatic import after Git pull or file edit. It can overwrite
unexported Builder work. Also not chosen: a new per-developer workspace or app,
which contradicts the shared development topology. Merely hiding all existing
commands behind aliases is insufficient; duplicate mechanisms should be
removed when they have no distinct safety/recovery role.

## Environment and app bindings

The ignored local `.env` is the primary setup surface, in the style of the
single-developer template. An example binding is:

```dotenv
DB_ENVIRONMENT=development
APEX_WORKSPACE_ID=90000
APEX_APPS=hr:100:HR_CODE,payroll:200:FIN_CODE
TABLES_SCHEMA=APP_DATA
CODE_SCHEMA=APP_CODE
APEX_SQLCL_CONNECTION=dev_apex_operator
```

The third field of each `APEX_APPS` member is that app's **sole** parsing
schema. `hr:100:APP_CODE,payroll:200:APP_CODE` is also valid. All app bindings
in one environment must resolve to the same verified database instance and
workspace; app IDs must be unique there. A single qualified APEX connection
may serve multiple apps only if its access is verified for each bound app.
TABLES, CODE, APEX, METADATA and optional VERIFY remain separate qualified
profiles where privilege separation requires them. The full `.env.example`
must explain these fields without embedding credentials.

Tracked non-development target contracts explicitly map each alias to its app
ID and parsing schema. The existing global parsing-schema contract is replaced
with a versioned app-binding contract; old files receive a documented one-time
conversion and fail with a clear message rather than being guessed. Identity,
physical mutex keys, baselines, release manifests, verification and runbooks
must all bind the selected alias, ID, workspace and parsing schema.

## Everyday work

**Builder-first:** edit an app in Builder; run `export-app <alias>`; review the
source diff and any reconciliation/conflict evidence; commit and share the
app change. There is no normal import in this path. If Builder and Git changed
the same component, the existing conflict workflow stops for human review.

**File-first (manual or agent):** edit `apps/<alias>/`, validate APEXlang and
the exact source tree, commit/review it, then use a guarded app-scoped publish.
The editor must first account for teammates' Builder changes; a source commit
is not permission to replace a different observed app. The same workflow and
refusals apply to a human-run command and an agent-run command.

**Schema work:** author immutable migration and verification members, inspect
the qualified target and drift, then apply through the shared migration
workflow. A migration applied to the shared development schema must be merged
promptly so a colleague's app export does not outrun its database dependency.

## Coordinated development publish

Provide a simple two-stage command surface: a read-only `prepare-publish` for
one or more explicit aliases and one exact source commit, followed by
`publish-app` bound to that preparation. Both stages print the selected app
list and physical target. Preparation writes a local, durable, non-secret
record under `.sync-state/`; it does **not** pause people or modify Builder.
The operator posts the per-app pause notice and records each relevant
teammate's acknowledgement before apply. Acknowledgement is a human statement,
not a machine proof that every editor has stopped; the tool records the names
and refuses a missing confirmation. The agent may perform the coordination
when asked, but may not invent another person's acknowledgement.

Prepare reports locked pages grouped by selected app: app alias/ID, page
ID/name, lock owner, lock time and comment when available. The report is
read-only and informational. A known page lock alone is **not** a new hard
block, and the tool never unlocks pages automatically. If lock information is
unavailable, it displays `UNKNOWN`, never `none`, and refuses publishing until
an authorized operator obtains/reviews the Builder Page Locks report or fixes
the read path. The exact SQL dictionary view and columns must be verified on
an APEX 26.1 target before relying on an automated query. Oracle documents
the Page Locks report and lock-owner UI, but those facts alone do not establish
the SQL view contract. A page-lock report cannot see open editors, unsaved
Builder changes, or arbitrary out-of-band DML.

Before **any** import, apply rechecks the exact commit, preparation binding,
teammate acknowledgements, target identity, app source cleanliness, baseline
or reviewed receipt, and a fresh Builder capture for **every** selected app.
It compares each current app with its approved pre-import state. Any surprise
change stops all selected imports for reconciliation, without touching
Builder. It refreshes the locked-page report immediately before the first
write and before each selected app's write. Only selected apps are paused:
publishing HR pauses HR, not Payroll; publishing HR and Payroll pauses both.

Once all selected apps pass preflight, imports run sequentially in declared
order using the existing per-app mutex, double capture, exact Git tree and
verified post-import re-export. Multiple app imports are **not atomic**. If a
later app fails, no automatic rollback or all-clear is issued: the report names
which apps verified, which remain unchanged, which are unknown, and the
retained recovery path. All selected apps remain paused until a person reviews
that state and issues the appropriate app-scoped all-clear. A non-selected app
continues normally.

## Independent releases and shared migrations

A schema release artifact contains the selected immutable migration bundles
and their verification/dependency metadata. It applies **once per environment**
through the qualified migration path, separately from any app deployment. An
app release artifact contains **one selected app** and its checks, source
commit, stable alias, and required migration IDs/checksums or frontier. Its
environment-specific app ID and parsing schema come from the qualified target
contract at deployment, not from the portable artifact.
An app release refuses when its requirements are absent or drifted. It does
not silently apply schema migrations or deploy the repository's other apps.
An app may declare a shared-component master dependency; that dependency is
checked, not deployed implicitly. Shared DB changes must be compatible with
apps still on earlier releases; destructive changes need staged migrations
and explicit compatibility checks.

Example: add optional `EMPLOYEE.PRONOUNS` in a schema release, then release HR
v2 that uses it. Payroll v1 stays deployed and working. Payroll's own later
release is independent. Test/staging verification proves only the environment
actually exercised; production handoff remains read-only and owner-operated.

## Simplicity and documentation contract

The root `README.md` becomes a short start-here guide: copy `.env.example`,
run `doctor`, then follow copyable Builder-first, file-first, two-teammate
publish, migration and independent-release examples. It links to advanced
runbooks rather than leading with protected-run internals. `AGENTS.md` is
updated to state the same three authoring routes, per-app pause, exact publish
guard, no automatic Git actions and evidence limits. Update `.env.example`,
`docs/import-pause.md`, `docs/app-recovery.md`, `docs/migrations.md`,
`docs/promotion.md`, `docs/ci.md`, `docs/toolchain.md`, and the plain-language
team guide wherever their current claims become stale. Historical specs/plans
remain historical and are not silently rewritten.

Audit all current public commands and modules. Classify each as (a) daily,
(b) protected/recovery, or (c) redundant. Remove category (c) only after a
test proves its behavior is covered elsewhere and callers/documentation are
updated. Keep low-level safety primitives even when only advanced runbooks
expose them. One command must not be renamed solely to lower the visible count.

This design is implemented in independently verifiable slices, not one giant
code change: (1) app/schema binding and local setup; (2) guarded development
publish and locked-page report; (3) separate schema/app release artifacts;
(4) command audit and beginner documentation. A slice can land only when its
own tests and the retained safety regressions pass. The later implementation
plan may be split at these boundaries rather than force unrelated changes
through one review gate.

## Acceptance and evidence limits

- Offline tests cover parsing/target bindings, same and split schema,
  multiple apps sharing a parsing schema, selected-app releases, stale/forged
  preparation, missing acknowledgements, known/unknown lock reports, preflight
  drift refusal, partial multi-app import, recovery and production refusal.
- A local disposable two- or three-developer exercise uses independent Git
  clones against one non-production workspace: Builder edit/export, manual or
  agent-style APEXlang edit, lock-owner display, teammate pause, guarded
  publish, and unrelated-app continuity. Fixture identity and cleanup are
  checked before any destructive test action.
- Protected test/integration runs verify real APEX 26.1+ SQLcl import/export,
  per-app schema qualification, schema-once/app-independent release and
  post-import equality. Browser checks are reported separately; unavailable
  protected or runtime evidence stays `UNKNOWN`, never `PASS`.
- The tool cannot observe unsaved/transient Builder work or arbitrary DML
  outside its inventory. Teammate acknowledgement is coordination evidence,
  not a technical lock. No production import or write is authorized here.

## Two-teammate example for the future README

Omar finishes HR Page 12 in Builder, exports HR and commits it. Layla edits
HR Page 20 in APEXlang and includes Omar's committed change in her reviewed
source. Layla prepares an HR publish; the report says Page 12 is locked by
Omar. Omar acknowledges the HR pause and confirms his Builder work is saved.
Payroll work continues. Apply captures HR again: if the Builder app differs
from the approved baseline, it stops and they reconcile; otherwise it imports
Layla's exact commit, re-exports HR, and verifies equality. Only after a
verified result do they issue the HR all-clear. If Layla selected HR and
Payroll, both would pause and both would preflight before either import.
