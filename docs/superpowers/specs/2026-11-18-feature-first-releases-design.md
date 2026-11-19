# Feature-first database releases

## Decision

Author packages, types, procedures, functions, triggers, and views as complete
mutable definitions under `features/<slug>/<SCHEMA>/`. Git records the edits.
A release selects each final definition once, including an unchanged body
when recompiling its specification requires it. Dated migrations carry
one-time effects and required intermediate states. Put final definitions and
dependent migrations in the same ordered version-2 capsule when they ship
together.

A downstream project archived 215 remaining legacy folders in one conversion
batch. Its field notes describe a 20-line package edit that generated a 131 KB
SQL step and a 289 KB check file. An earlier development deployment measured 1.5–2 minutes
for small folders and estimated 40–60 minutes for 20 sequential folders.
These are historical observations. The design reduces the work to deliver,
and qualification measures the time on a representative target.

## Workflow

| Stage | Developer action | Result |
| --- | --- | --- |
| Author | Capture existing DEV stored source or edit the owning feature. Put each new supported view in a separate `views/<name>.sql` file. Review the Git diff and feature ownership. | One canonical source per object. No required migration revision for each edit. |
| DEV | Prepare the selected units, review the target and diff, coordinate shared writes, then explicitly execute the plan. Run the feature's relevant behavior checks. | Exact source/settings/validity evidence tied to the selected DEV target and source hashes. |
| Assemble | Select the final definitions and only the required one-time or intermediate effects. Commit the selected inputs and version-2 manifest, then freeze once. | One immutable capsule. Its order makes dependencies explicit. |
| Staging and production | Prepare a target plan for each environment. Apply and verify staging, then promote the same capsule with the matching staging report and existing production confirmation. | Shared payload digest and separate verified target evidence. |

A release can prepare a feature against existing tables or against supported
DDL from an earlier feature checkpoint. A later one-time migration can
backfill using the feature's final definition. Its declared preconditions
must pass at prepare time. Only modeled missing-prerequisite conflicts are
eligible for the existing same-schema deferral. Check a required version
of the code within the migration, before its data writes, when that version
will arrive from an earlier step. Keep other prerequisite
changes ahead of the dependent release, with their own target verification.
Existing feature checkpoint DDL keeps its reviewed order and behavior.

### Transitions requiring an intermediate definition

Today, preparation sees the target before any step writes. A later feature's
exact source guard is based on that observation. A migration that replaces
the same unit first makes the guard stale after the migration has run.
Structural simulation of earlier feature checkpoints cannot prove the
result of an earlier migration. See [target preparation](../../../scripts/feature_release.py#L1076)
and [execution revalidation](../../../scripts/feature_release.py#L1780).

For this delivery, use one reviewed dated transition when a backfill
genuinely needs an intermediate version of the same unit. Order the
intermediate definition, a SQL step that raises on an incorrect source,
compiler settings or validity, the backfill, and the final definition in
that folder. The guard runs in the same session as the backfill, since
`checks.json` runs at folder boundaries. Include required spec/body pairs
in order. Take the final definition from the canonical feature file and
use those bytes directly when supported by SQLcl transport. If a trusted
generated delivery wrapper is needed, bind its bytes to the canonical
source and verify the parsed definition and settings. Check this mapping
at the release commit before freezing. Use fresh final source, settings,
validity, and data checks. For that capsule, the migration delivers
the unit. A separate feature checkpoint for it would start from the wrong
prepared state. Keep independent feature objects in the same capsule when
their prerequisites can be proved. The normal release selects one source
write per changed body. This exception selects exactly two body writes.
Qualify it from the required starting state in DEV or a disposable clone.
If DEV already has the final version, qualify the transition on a
disposable copy of the intended starting state. Refresh the retained DEV
baseline from a verified installed final definition. Oracle DDL can commit
before a later step fails. An interrupted transition follows the existing
migration reconciliation rules.

General simulation of migration-produced source or DDL belongs to a later
design with verified predecessor receipts. Add a narrow prepare-time check
for a directly identifiable final replacement in an earlier migration on
the same resolved owner. Refuse when the replacement changes the prepared
before-source of a later feature checkpoint. Flag preceding unmodeled
SQL for operator review when its effects cannot be proved. Use the reviewed transition
above or ship the prerequisite in an earlier verified release. Existing
capsules continue to use their matching engine and evidence.

## Guarded DEV apply

Use a DEV-specific command that reuses the release engine's selected-source
checks, SQL delivery, and read-back. Prepare working-tree edits directly.
Keep the existing staging/production command interfaces intact. Allowing
full version-2 rollouts on DEV would also expose migration, ORDS and APEX
steps to this daily workflow. Generating private dated migrations would
reintroduce a second stored-source representation. The selected-source
command keeps the everyday path small.

Proposed interface for Bash and PowerShell:

```bash
scripts/team.sh feature apply inventory --schema APP_CODE --env dev \
  --unit "PACKAGE BODY:ITEM_LOOKUP" --prepare
scripts/team.sh feature apply inventory --schema APP_CODE --env dev \
  --execute-plan scratch/feature-dev/<run-id>/plan.json
scripts/team.sh feature apply inventory --schema APP_CODE --env dev --status
scripts/team.sh feature reconcile-run <run-id> --env dev --evidence <file>
```

- Require exactly one mode (`--prepare`, `--execute-plan`, or `--status`),
  an explicit `--env dev` and one feature/schema.
  Preparation requires one or more `--unit TYPE:NAME` values. Execution takes
  only the saved selection. Match unquoted Oracle names case-insensitively and
  preserve quoted spellings. Resolve identities from parsed SQL, including
  files with hashed names. Reject missing, ambiguous, or duplicate selections
  and SQL owned by another feature. Body-only selections leave the spec alone.
  A changed package or type specification must select its corresponding body
  when the body exists on DEV or is being introduced by the feature.
  Repeatable, prepare-only `--checks <repo-relative-path>` selects existing
  checks under the same feature and schema. Default to that schema's
  `checks.json` and require meaningful postconditions in either case.
- Support the existing stored-source formats and the supported
  `CREATE OR REPLACE VIEW` form. DEV selection of a view requires a dedicated
  feature file with exactly one supported statement. This also lets future
  capsules select a changed view independently of any one-time DDL. Legacy
  mixed `ddl.sql` keeps its current promotion behavior. An explicit source
  conversion may move an individual view out of that file, with one owner
  and the existing migrations and promotion sequence reviewed. Keep views
  with unsupported dependency forms on the reviewed compatibility route.
  Qualify stored source first and views before declaring both supported.
- Use the DEV migration profile for preparation, write, and verification,
  with the existing code profile fallback when the migration profile is
  absent. If a configured migration profile omits the selected schema,
  refuse. Filter catalog reads by the verified owner even when the login
  acts as that owner. Where capture and DEV apply use
  different logins, accept a capture baseline only for the same observed
  database, container, schema and edition. Bind each actual session to its
  expected login and identity. Keep split-owner and owner-only ORDS policy
  intact. The command accepts DEV classifications `development` and `test`.
- Preparation connects read-only. Show the effective profile, selected target,
  scope, before/after diff, dependency and compiler-settings decisions, and
  the plan digest. For existing stored units, use exact capture or last
  verified DEV evidence so an old feature file cannot silently erase newer
  DEV work. When that baseline disagrees, reconcile the source first. For
  any existing unit or view with no retained baseline, prepare shows the
  complete observed definition, the proposed diff, and a target-bound
  before-state digest. A second preparation can record that reviewed
  baseline using `--accept-live-baseline <before-state-sha256>`. The digest
  must match a fresh observation. This option is prepare-only, applies
  only to absent baselines, and preserves edited feature files. Refuse this
  route when the observed unit is invalid, its state is incomplete, or the
  target scope has unresolved write evidence.
  New objects require verified absence and the existing explicit compiler
  settings where applicable. For a view, use an untruncated catalog definition
  and preserve literal text when comparing it. Unavailable source, settings,
  or dependencies stop preparation.
- Store a private, versioned plan under `scratch/feature-dev/`. Bind selected
  source bytes, generated SQL drivers, the existing feature/schema
  `checks.json` or explicitly selected existing scoped checks, exact target
  before-state, effective configuration, and execution-engine identity.
  Resolve all paths inside the checkout, reject links and altered bytes, and
  regenerate guard SQL from validated input. The explicit
  `--execute-plan FILE` invocation is the execution action. Print the target
  and selected units before writing, with no second interactive prompt.
  Shared DEV execution still requires separate write authorization.
- Execution acquires the existing checkout-local writer lease for the
  physical target and checks pending attempts while holding it. Re-read
  selected input hashes and revalidate target identity and before-state
  immediately before the first payload. Reuse the in-session source,
  settings and trigger guards. A view replacement requires an in-session
  full-definition guard in addition to validity and dependency checks.
  Reuse the full `baseline-views` LONG-to-CLOB reader with exact owner and
  name selection through the baseline names filter. Compare query text and
  definition attributes, including
  column aliases and order, CHECK OPTION or READ ONLY, editioning,
  BEQUEATH and explicit default collation. When captured DDL includes a
  default collation, require the source to name the intended value. Qualify the
  complete read and guard for long definitions and supplementary Unicode.
  Refuse a view when complete evidence is
  unavailable. A no-op requires
  fresh evidence of the desired state and runs
  selected checks without recompiling. Preserve existing dependency,
  compiler-setting, trigger ENABLED and spec/body safeguards. Keep
  coordination with teammates and other tools that write to the same schema.
- Verify on a fresh connection using the same scope and bound target.
  Verify installed stored text and settings, object validity, view definition
  and the selected assertions. Keep readable pre-apply diffs in the daily
  change record. Advance retained DEV baselines from the verified live bytes
  without rewriting feature files. A local baseline-refresh failure reports
  attention with the receipt preserved, and can be repaired with a read.

### CLI outcomes

| Exit | Prepare or execute | Status or DEV reconciliation |
| --- | --- | --- |
| 0 | Plan prepared, verified apply, or verified no-op. A local baseline refresh failure after verification emits a repair notice and retains the successful receipt. | Status found no unresolved attempt for the displayed schema, or reconciliation recorded closure. |
| 1 | A reviewed pre-write comparison or assertion declined the operation. No payload started. | Status found an unresolved attempt, or the operator declined acknowledgment. |
| 2 | Invalid arguments, unresolved state, unavailable evidence, or execution/verification failure. Any failure after a payload may have started leaves a pending attempt and uses 2. | Invalid or unreadable evidence, identity mismatch, or a reconciliation that cannot prove closure. |
| 130 | Interrupted before payload start, with affirmative no-write evidence or before the write boundary. | Interrupted before recording closure. |

`--status` reads local attempt markers without dotenv or SQLcl. It displays
run IDs, the observed target scopes and missing evidence for the selected
schema, including attempts from other features in that schema. An intact
pending marker with missing scratch files is unresolved and returns 1.
A malformed or unreadable marker returns 2. Execution
and DEV reconciliation load the selected dotenv file from both wrappers.
For `reconcile-run --env dev` the evidence option is required. Staging and
production continue to use the current rollout-state route and arguments.

## Recovery and adoption

Before any potential write, persist a small private pending marker under
ignored `releases/.dev-apply-attempts/`, with its target scope, plan digest
and evidence paths. Keep full SQL, before-state and logs under
`scratch/feature-dev/`. Scratch cleanup leaves the marker visible and the
target blocked. Unreadable evidence cannot authorize a write. Retain the
marker on timeout, ambiguous interruption, partial DDL, or failed read-back.
It blocks feature apply on the same target scope. The DEV variant of
`feature reconcile-run` requires
same-target read-only evidence and the normal recorded acknowledgment.
Close a proven before-state or intended state as reconciled. A partial or
unclassifiable state needs separately authorized repair before closing it.
Reconciliation records an outcome and never creates staging proof. A
verified plan is single-use. After a refusal or reconciliation, prepare a
fresh plan before any new write.

Record verified no-ops as consumed using the same plan ID and digest as a
verified write. A repeat rejects from local evidence before connecting.
Recheck the consumption state under the writer lease for concurrent runs.

The checkout-local feature lease covers participating feature commands.
Standalone `migrate` and independent checkouts can write to the same target.
Coordinate those changes and resolve known overlapping attempts before
starting a feature apply. The direct-migration writer lease remains its own
tracked change.

An invalid `CREATE OR REPLACE` can leave the shared object invalid. Before
execution, retain verified before-source and settings, validity, and
dependency context for an operator-reviewed restore candidate. If the
candidate fails, report the exact unit, retain the pending marker, and point
the operator to the reviewed same-target repair route. Repair restores the
prior definition or installs a reviewed correction and includes fresh
catalog and behavior checks before reconciliation. The DEV command only
retains evidence and records reconciliation. For critical shared units,
use the project's authorized isolated compile or behavior check when
available. Dropping a newly created object needs its own approved repair.

An existing project inventories active legacy folders with
`feature convert-plan`. Use archive indexes, path maps and retained evidence
for already archived folders. For each family, select the final definition
and review retained
effects, ordering, and target evidence. An intermediate definition needed
for a data transition stays with that bounded transition. A mixed immutable folder
stays intact until it has an explicit disposition. Archive through the
normal checksum and eligibility workflow, preserving receipts, attempt
events, and old-to-new path provenance. Archived history alone provides no
evidence that an effect reached staging or production. Template upgrades
leave the archive and existing feature ownership in place.

### In-flight capsules and engine upgrades

Before syncing a release-engine update, identify capsules already prepared
or verified on staging. Complete them with the matching engine in their
retained checkout, with the original capsule, plan, local state and staging
report intact. If that engine or evidence is unavailable, use a separately
reviewed release and obtain staging proof for its new capsule before
production. Inspect committed effects and omit verified writes from any
continuation plan. Keep engine-digest checks in place. Keep DEV-only modules
outside the promotion digest unless promotion actually uses them.

## Acceptance criteria

1. A sanitized fixture with three historical revisions of one
   package body and one required one-time migration yields a capsule with
   one final body and that migration. The historical SQL and receipts stay
   readable and unchanged. A fixture needing an intermediate body executes
   one bounded migration in intermediate → backfill → final order, verifies
   two body writes and proves final source and data. Exercise the current
   unsafe mixed shape in a synthetic regression so the supported alternative
   and its reason are visible.
2. Working-tree edits can be prepared and applied from the owning DEV feature.
   Changed source, target, checks, or before-state after prepare produces
   zero writes. Tests cover repeat execution, missing body, disabled trigger,
   duplicate ownership, no-op, split profiles and ambiguous writes.
3. A disposable qualification run matches the DEV receipt's authored source
   digest to the capsule input. It independently verifies installed catalog
   text and settings on DEV and staging, obtains verified staging proof, and
   exercises production-proof validation using an approved disposable target.
   A different digest or absent proof still refuses production.
4. Compare the existing batched `migrate` path with the capsule from
   equivalent starting states on reset disposable targets. Use the same
   connection class and independently verified final effects. Record the
   number of runs and median timings. Count selected steps, package writes,
   SQLcl sessions, and generated SQL and check bytes. Break out connection
   startup, catalog prepare, apply, and verification wall time. Report
   preparation and the existing object budget alongside total release time.
   Any live speed claim requires the measured results.
5. Preserve version-1 rollouts, migration receipts and rehearsal, version-2
   manifest and staging-proof rules, profile overrides, and release-specific
   SQLcl requirements. Capsules already frozen under another engine follow
   the existing engine-digest checks. This design adds no database metadata
   tables or cross-checkout mutex.

The current review inventory retains the separate direct-migration replay
and generated-baseline safety fixes. They can be delivered independently of
the feature authoring guidance.
