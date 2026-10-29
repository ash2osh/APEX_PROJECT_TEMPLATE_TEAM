# Feature-oriented releases

Use mutable feature files for new object work that benefits from complete
package sources, compact Git diffs, and one frozen payload for staging and
production. Existing dated migrations and schema-version 1 rollout manifests
keep their current behavior.

## Create and review feature source

`feature init` is offline. It creates a shared README when one is absent and a
schema-level `checks.json` with a deliberately failing `replace-me`
postcondition. Later schema initializations retain the existing README and
checks for other schemas. Replace each new schema check with a reviewed,
meaningful condition before including that schema in a release.

```bash
scripts/team.sh feature init inventory --schema APP_CODE
scripts/team.sh feature capture inventory --package ITEM_LOOKUP --schema APP_CODE --env dev
scripts/team.sh feature diff inventory --schema APP_CODE
```

Capture reads the complete live DEV package specification and body, when a body
exists, and stores a retained source baseline under ignored `scratch/`. It
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
units live under `features/<slug>/<SCHEMA>/packages/`; keep each package spec
and body as whole, ordered files. Checks use the existing schema-version 1
`checks.json` format. Version-2 SQL-script and ORDS steps name a logical
`schema`; the selected environment's migration profile supplies the connection
and login. ORDS imports must use the actual REST owner named in the frozen
export. Schema-version 1 keeps its explicit connection and expected-user fields.

The object budget covers feature checkpoints only. A SQL-script is counted as
the separate data phase only when every statement is lexically validated,
top-level `INSERT`, `UPDATE`, `DELETE`, or `MERGE`, plus optional explicit
`COMMIT`/`ROLLBACK`. DDL, PL/SQL blocks, calls, queries, and other forms remain
uncovered object work and block complete fast-path promotion. Recognized ORDS
exports keep their own phase and timing after the existing structure, OAuth,
exclusion, and owner checks. Static DML classification describes the source
statements; it cannot rule out trigger, autonomous-transaction, or external
effects. Keep data repair that needs object-level checks in a separately
reviewed migration or workflow.

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
checks that those bytes are committed, and creates an environment-neutral
capsule under `scratch/release-capsules/`. It is offline and requires no `.env`,
SQLcl, or network. Save the capsule path it prints and pass that same path to
every later command.

`--dry-run` checks only local source, manifest, capsule, and hashes. `--prepare`
uses the selected environment's guarded read-only capture to make exact
before-state, compiler-setting, structural, and descriptor checks. Review the
target plan and generated diffs before execution. The capsule payload digest is
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

Production needs its own prepared production plan and a successful staging
report for the same capsule:

```bash
scripts/team.sh rollout releases/current.json --env prod --prepare --capsule <capsule-dir> --report scratch/prod-prepare.json
scripts/team.sh rollout releases/current.json --env prod --capsule <capsule-dir> --stage-report scratch/staging-rollout.json --report scratch/prod-rollout.json
```

Production validates the staging report's payload and engine digests, shared
source hashes, complete in-budget feature-checkpoint coverage, and matching
data/ORDS phase classifications before it asks for confirmation. It uses the
staged capsule payload and independently revalidates the production target plan
and app descriptors. SQL/ORDS targets are observed during preparation and
must still match before their run-bound writer lease allows a write. A changed
capsule, target profile, or source blocks success receipts.

The engine digest binds the ordered release modules and their runtime helpers,
including SQLcl transport, target/source checks, trusted SQL templates, and the
existing APEX deploy-and-verify path. Capsule preparation refuses if a required
engine file is missing; a helper change makes an older capsule unusable.

## Timing, attempts, and cleanup

Reports separate preparation, selected feature database-object time, APEX app
imports, ORDS imports, and data scripts. Object time covers checkpoint
revalidation, preconditions, apply, and postconditions. It is not a hard stop:
Oracle DDL may commit before the command can report a budget overrun. Data,
ORDS, preparation, and manual pauses have separate times. Legacy migrations and
unclassified object scripts are listed as excluded; they make object coverage
incomplete, so coverage is incomplete. A complete, within-budget report covers every capsule checkpoint
and every classified phase, not the whole database or deployment window.

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

Offline tests and hosted CI do not prove live staging performance. A measured
claim needs a separately authorized disposable staging rehearsal with the
actual selected payload and final source/settings/validity read-back.
