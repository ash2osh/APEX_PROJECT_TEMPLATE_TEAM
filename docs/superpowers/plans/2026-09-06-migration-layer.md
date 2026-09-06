# Shared Schema Migrations — Implementation Plan (Plan 2 of 3)

> **For agentic workers:** Use superpowers:executing-plans, or
> superpowers:subagent-driven-development when delegation is authorized.
> Checkboxes are implementation work, not verification already performed.

**Revision:** 2 — replaces timestamp drift and atomic-migration assumptions.
**Goal:** Make shared-schema changes attributable, restartable and reproducible.
**Architecture:** Immutable SQL/verification/effects bundles describe transitions.
A separate shared metadata-profile store serializes participating runners and
records expected object fingerprints. Shared-development history differs from
strict promotion history; fresh and upgrade replay provide canonical evidence.
**Tech Stack:** Python 3.10+, qualified SQLcl/Oracle, thin Bash/PowerShell launchers.
**Spec:** [Team design](../specs/2026-09-06-team-template-design.md), §§4, 7–11.
**Dependency:** [Plan 1](2026-09-06-apex-round-trip.md), Target, run_sqlcl and
control_store's isolated metadata/bootstrap foundation.

## Global constraints

All spec §8 constraints apply. SQL payloads use the shared adapter, exact
saved connections, identity assertions and empty-file stdin. Metadata writes
always use the METADATA profile, isolated from tables/code payload users. Production writes
are refused; SELECT-only status is allowed through a verified read profile.
No DDL rollback guarantee is made. No task copies the former unsafe runner.

The shared schema is not reset for tests. Live tests receive disposable
targets with explicit provisioning and verified identities. Compiled SQL,
data assertions and metadata grants must be reviewed before any live apply.

## File responsibilities

| Files to create | Responsibility |
|---|---|
| scripts/teamlib/migration_bundle.py | filename/header/member validation and checksum |
| scripts/teamlib/migration_plan.py | dependency ordering, pending/foreign/conflict decisions |
| scripts/teamlib/migration_store.py; scripts/sql/migration_metadata.sql | versioned metadata and mutex |
| scripts/teamlib/fingerprints.py; scripts/sql/schema_inventory.sql | supported-object inventory/canonicalization |
| scripts/teamlib/migrate.py | state machine, target routing and verification |
| scripts/teamlib/replay.py | disposable preparation, effects generation, replay and adoption |
| scripts/migrate.sh/.ps1; scripts/check_drift.sh/.ps1 | shared CLI launchers |
| scripts/tests/test_migration_*.py; scripts/tests/live/ | behavioral and Oracle tests |
| docs/migrations.md; docs/schema-coverage.md | authoring, coverage and recovery contract |

Public commands added to team.py:

```text
migration-plan --source DIRECTORY --history FILE --mode shared|strict
migrate [--source DIRECTORY] [--dry-run] [--bootstrap]
check-drift
prepare-migration MIGRATION_ID --replay-env FILE
replay --source DIRECTORY --replay-env FILE [--previous DIRECTORY]
snapshot --out DIRECTORY
adopt-baseline MIGRATION_ID --evidence DIRECTORY
recover-migration RUN_TOKEN --attempt ATTEMPT_ID --evidence DIRECTORY
```

Offline migration-plan reads supplied history, never connects. Online dry-run
may read metadata but never bootstrap/acquire a write mutex or mutate data.
migrate derives shared/strict mode from verified target role: developer and
shared integration use shared; test/replay use strict. A user cannot weaken
a test deployment by passing --mode shared to migrate.

## Task 1: Bundle, dependency and immutability contract

**Files:** migration_bundle.py, migration_plan.py, test_migration_bundle.py,
test_migration_plan.py.

**Types/interfaces:**

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Migration:
    id: str
    stamp: str
    target: str
    checksum: str
    dependencies: tuple[tuple[str, str], ...]  # (ID, exact bundle checksum)
    destructive: bool
    effects: dict                         # ObjectKey -> before/after digest

@dataclass(frozen=True)
class Plan:
    pending: tuple[str, ...]
    foreign_applied: tuple[str, ...]
    errors: tuple[str, ...]

# load_bundles(directory) -> dict[str, Migration]
# plan_migrations(bundles, history, mode) -> Plan
# dependency_order(bundles) -> tuple[str, ...]
```

- [ ] Discover primary names only with anchored
  `[0-9]{8}T[0-9]{6}__[a-z0-9][a-z0-9-]*__[a-z0-9][a-z0-9-]*[.]sql`.
  Validate UTC calendar values. Each has exactly one .verify.sql and
  .effects.json sibling; orphan members are errors.
- [ ] Require LF/UTF-8, one exact target header, schema version 1 effects,
  explicit dependency ID/checksum pairs, destructive boolean and before/after
  object states. Dependency checksums bind the exact bundles used to generate
  effects, including dependencies not yet applied.
  SQLcl control commands CONNECT/CONN, HOST, EXIT, error-policy changes,
  remote/nested includes and substitution re-enabling are prohibited in
  migration members. Validate statement boundaries with a qualified lexer;
  do not inspect comments/string literals with an unanchored grep.
- [ ] Verification members are restricted SELECT assertions returning exactly
  assertion_name and PASS/FAIL. Run them through VERIFY with no mutation grants,
  no application routine execution and an allowed SQL/function grammar. Reject
  DML, DDL, PL/SQL, SQLcl controls and transaction statements in these members.
  Missing/duplicate assertion rows and unknown status refuse. A mutating verify
  fixture must fail before execution, not create its own expected effects.
- [ ] Compute bundle checksum from canonical sorted member path/SHA-256 pairs.
  No normalization after hashing; execute an immutable staged copy of those bytes.
- [ ] Implement topological ordering with (stamp, full ID) ready-node tie-break.
  Applied dependencies may come from verified central history in shared mode;
  strict artifact history must be self-contained.
- [ ] A checksum conflict or unresolved attempt blocks apply. In shared mode,
  absent APPLIED history is foreign_applied; strict mode rejects it. Canonical
  deletion is enforced separately by CI diff against the protected base.
- [ ] Test equal-second tie, invalid calendar date, missing/cyclic dependency,
  verification/effects edits, foreign applied history and incomplete attempts.
  Edit an unapplied dependency after child effects generation and prove exact
  checksum mismatch refuses even when its object output would be unchanged.

Example pure test with the defined Plan interface:

```python
def test_foreign_history_is_not_deleted_history(self):
    history = {"20260906T100000__alice__x": {
        "status": "APPLIED", "checksum": "a" * 64,
        "target": "tables", "dependencies": [], "sequence": 1}}
    shared = plan_migrations({}, history, "shared")
    strict = plan_migrations({}, history, "strict")
    self.assertEqual(shared.foreign_applied,
                     ("20260906T100000__alice__x",))
    self.assertFalse(shared.errors)
    self.assertTrue(strict.errors)
```

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p 'test_migration_*plan*.py' -v`;
run test_migration_bundle.py separately with the same discovery command.

## Task 2: Central metadata, bootstrap and persistent mutex

**Files:** migration_store.py, scripts/sql/migration_metadata.sql,
test_migration_store.py, scripts/tests/live/test_migration_store.py.

**Interface:** `bootstrap(store_target)`; `acquire(store_target, run_token)`;
`release(store_target, run_token)`; `read_history(store_target) -> dict`.
All Target arguments here are the isolated METADATA profile. Extend Plan 1's
control_store foundation; app and migration mutexes are distinct resources.

- [ ] Verify isolation: METADATA_SCHEMA differs from tables/code and payloads
  have no direct/role/ANY/proxy or controller-routine mutation access. VERIFY
  cannot mutate application or metadata state. Keep controller secrets/tokens
  out of staged SQL, payload processes and diagnostic logs. Live negative tests
  attempt a metadata write/drop via both payload users and expect refusal.
- [ ] Define TEAM_MIGRATION_META(version, project_id, schema_set_digest),
  TEAM_MIGRATION_MUTEX(singleton_id, owner_token, acquired_at),
  TEAM_MIGRATION_HISTORY(id, checksum, target, dependencies_json,
  payload_manifest_json, source_commit, applied_sequence, applied_at,
  applied_by, run_token), TEAM_MIGRATION_ATTEMPT(attempt_id, migration_id,
  checksum, state, run_token, worker_identity, started_at, finished_at,
  diagnostic_digest), TEAM_MIGRATION_OBJECT(object_key, digest, migration_id).
  Define primary/unique keys, state/target checks and NOT NULL contracts.
- [ ] Bootstrap through the write guard, verify existing definitions exactly,
  tolerate only a verified concurrent identical bootstrap, and report partial
  installation as setup-required. Metadata absence is not an empty applied set.
- [ ] Implement the mutex as a committed conditional update, no timeout stealing.
  A DDL commit in another connection cannot release this logical lock.

```sql
-- Bind values through the adapter; this is the mutex transition only.
UPDATE team_migration_mutex
   SET owner_token = :run_token, acquired_at = SYSTIMESTAMP
 WHERE singleton_id = 1 AND owner_token IS NULL;
-- Assert SQL%ROWCOUNT = 1 in the controlling PL/SQL block, then COMMIT.
-- No payload may start until the caller verifies ownership by SELECT.
```

- [ ] Every state mutation predicates on the owner token. Release only after
  all workers ended and no unresolved attempt remains. Compare-and-clear the
  selected token; record recovery evidence when clearing a failed run.
- [ ] Reject manual "unlock by age". Recovery evidence must identify all workers
  and establish that no payload process/session is still live; if privileges
  cannot establish this, require the environment owner to supply evidence.
- [ ] Test concurrent acquire (one winner), DDL commits, wrong-token updates,
  killed parent/live child, partial bootstrap and absent metadata dry-run.
  Verify neither tables nor code users have mutation grants on log tables.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_migration_store.py -v`.
Live suite is required before claiming concurrency correctness.

## Task 3: Supported inventory and structural fingerprints

**Files:** fingerprints.py, scripts/sql/schema_inventory.sql,
test_migration_fingerprints.py, docs/schema-coverage.md.

**Types/interfaces:** ObjectKey is a canonical string encoding logical owner,
object type and name. Inventory maps ObjectKey to digest; a missing object
has no entry. `snapshot(profiles, out) -> Inventory`;
`diff_inventory(expected, actual) -> dict[str, tuple]` with added/missing/changed/invalid.

- [ ] Implement spec §7 coverage adapters for tables/constraints, indexes,
  views, package specs/bodies, routines, triggers, sequence definitions,
  types/bodies, private synonyms and object grants.
- [ ] Inventory all supported objects through both profiles, deduplicating
  equal schemas. Require begin/count/end manifests and independent object
  counts. Malformed or empty unframed SQLcl output is unknown, not "no drift".
- [ ] Use DBMS_METADATA structured output/dictionary fields to canonicalize
  environment owner identities and storage. Preserve quoted identifiers,
  SQL literals, logical constraints and code bytes. Exclude runtime sequence
  counters, physical storage and volatile timestamps by explicit field rules.
- [ ] Logical owner is "shared" when profiles coincide, otherwise tables/code.
  Include the ownership topology in manifest format; reject comparing different
  topologies until a new replay baseline is reviewed. Configuration equality
  alone must not collapse two distinct objects.
- [ ] Snapshot application schemas only; independently verify metadata schema
  structure. A reserved TEAM_MIGRATION_* or TEAM_APP_* object in an application
  schema is an error, not an exclusion. Refuse unsupported classes or unknown
  ownership. Explicit external verification scopes prevent whole-system claims.
- [ ] Test changed/deleted/added objects, invalid packages, grants, distinct
  schemas, same schemas, CRLF, SQL literals containing schema names, sequence
  advancement without definition change and schema-name-neutral replay.
- [ ] Implement check-drift from expected central inventory with no write.
  LAST_DDL_TIME appears only in diagnostics. A change before an unrelated
  migration remains changed; DML requires semantic checks.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_migration_fingerprints.py -v`.

## Task 4: Effects authoring and existing-schema adoption

**Files:** replay.py, test_migration_effects.py, test_migration_adoption.py,
docs/migrations.md.

**Interface:** `prepare_migration(id, source, replay_target) -> Effects`;
`adopt_baseline(id, evidence, profiles) -> None`.

Effects JSON Schema fragment (the complete schema also disallows unknown keys
and validates ObjectKey names and dependency IDs):

```json
{
  "type": "object",
  "required": ["version", "dependencies", "destructive", "objects"],
  "properties": {
    "version": {"const": 1},
    "dependencies": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "bundle_checksum"],
        "additionalProperties": false,
        "properties": {
          "id": {"type": "string"},
          "bundle_checksum": {"type": "string", "pattern": "^[0-9a-f]{64}$"}
        }
      },
      "uniqueItems": true
    },
    "destructive": {"type": "boolean"},
    "objects": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "required": ["before", "after"],
        "additionalProperties": false,
        "properties": {
          "before": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
          "after": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"}
        }
      }
    }
  },
  "additionalProperties": false
}
```

Null means absent; an empty file or empty object definition still has a digest.
Reject entries where before equals after: effects describe actual changes.
Reject duplicate dependency IDs even when their checksum fields differ.

- [ ] Provision an empty disposable target, replay dependency closure, snapshot
  before, apply candidate SQL, snapshot immediately after, run SELECT-only
  verification through VERIFY, and confirm the inventory is unchanged by
  verification. Generate the exact changed-object effects. Review and commit the effects
  with SQL and verification; they are never learned from shared dev after apply.
- [ ] Preparation runs only on role replay with a provisioned instance token
  and empty-target proof. It must have an isolated authoring execution path
  that accepts a candidate missing effects only in that role; ordinary migrate
  still requires complete bundles.
- [ ] For initial adoption, generate/review baseline DDL and reference-data
  assertions, prove empty replay, then compare live structural/semantic state
  under the central mutex. Stamp baseline only on exact match; never execute
  CREATE statements over existing business data.
- [ ] Metadata setup may precede adoption, but expected state remains uninitialized
  until adoption or verified empty bootstrap. A nonempty schema cannot initialize
  its expected inventory by silently sampling live objects.
- [ ] Test valid adoption, changed live baseline, missing reference data,
  candidate changes outside declared scope and unknown normalization.
  Confirm production adoption is refused.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p 'test_migration_*tion.py' -v`;
also run test_migration_effects.py explicitly.

## Task 5: Apply state machine, verification and uncertainty

**Files:** migrate.py, test_migration_runner.py,
scripts/tests/live/test_migration_failures.py.

**Interface:** `apply_plan(source, profiles, bootstrap=False,
confirmation=None, expected_plan=None) -> RunReport`. RunReport includes run_token, applied,
foreign_applied, blocked_attempt and verified inventory digest.

- [ ] Stage immutable bundles; verify profile identities and metadata version;
  optionally bootstrap only when explicitly requested. Acquire mutex BEFORE
  reading final history/plan and live fingerprints.
- [ ] When expected_plan is supplied by apply-release, compare its artifact,
  target, history and pending-set digests to the recomputed plan under this
  same mutex. Any difference refuses before RUNNING or payload execution.
- [ ] Preflight the whole plan, including all destructive flags, dependencies,
  checksum conflicts and unresolved attempts before the first application.
- [ ] For each migration enforce full live inventory equality and declared
  before states, persist RUNNING, execute staged SQL with target routing and
  snapshot immediately after. Verify data/compiled-object postconditions through
  the observation-only VERIFY profile; a payload profile must not run verify SQL.
- [ ] Capture both profiles after execution. Every observed structural change
  must equal the declared effects; compare inventories before/after verification
  and ensure unchanged objects remained unchanged.
  Commit updated expected inventory, APPLIED history and attempt state in one
  transaction on the metadata connection. Record sequence under the mutex.
- [ ] On known SQL error record FAILED; on timeout/lost acknowledgement record
  UNKNOWN if possible. If recording fails, leave RUNNING. Stop later migrations
  and retain mutex. Never auto-retry a possibly committed payload.
- [ ] Model interruption at every boundary, including success-before-log and
  metadata-commit acknowledgement loss. Recovery first rereads state; it must
  not execute already recorded SQL a second time.
- [ ] Restrict recovery to selected run/attempt and verified evidence. A restart
  uses exact immutable bytes after a human-reviewed restartability check;
  completion without replay requires structural AND data postconditions.
  Failed immutable source needing correction uses a separately reviewed
  corrective transition with a durable link to the failed attempt.
- [ ] Test separate-schema routing, rollback-independent DDL partial state,
  invalid PL/SQL compiled with warning, duplicate runner, edited effects,
  destructive refusal and partial success. The fake SQLcl test must exercise
  the public migrate CLI, not just helper parsing.

State sequence to encode:

```text
READY -> mutex-owned -> live/preconditions-verified -> RUNNING(committed)
RUNNING -> payload+postconditions+effects verified -> APPLIED(committed)
RUNNING -> SQL failure -> FAILED; stop and retain ownership
RUNNING -> unknown worker/result/ack -> UNKNOWN or still RUNNING; stop
FAILED/UNKNOWN -> reviewed recovery -> verified terminal state
```

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_migration_runner.py -v`.
Oracle partial-DDL and concurrent-process cases are live acceptance gates.

## Task 6: Canonical history, replay and evidence generation

**Files:** replay.py, test_migration_replay.py,
scripts/tests/live/test_migration_replay.py, database/.gitkeep.

**Interfaces:** `replay(source, replay_target, previous=None) -> ReplayReport`;
`check_history_change(base_commit, new_commit) -> None`.

- [ ] Reject edits/removals of any complete canonical bundle relative to the
  protected base commit (or previous release for release validation). Check all
  members, not just .sql. Development applied-history checks remain separate.
- [ ] Obtain a new disposable target with explicit role, exact identities,
  provision token and supported toolchain. Assert zero application inventory
  before bootstrap; never accept a caller's claim that a shared schema is empty.
- [ ] Replay complete source in deterministic dependency order. Verify each
  transition's effects and semantic checks. Snapshot to scratch and compare
  complete canonical manifests with committed evidence, including added files.
  Never call backup_db against the developer working-tree database/ mirror.
- [ ] When --previous is supplied, create another fresh target, replay previous
  release and representative data fixtures, then upgrade with the new release.
  Check postconditions and compare final structure with fresh replay.
- [ ] Provide explicit snapshot --out for generating reviewable evidence. Only
  an explicit evidence-update workflow stages that output into database/.
  Its metadata records source commit/digest, normalizer version and coverage.
- [ ] Test earlier timestamp merged after a later migration, incompatible
  same-object effects, missing canonical member, untracked extra snapshot file,
  drift hidden by a later timestamp, absent DB provisioning and unsafe replay target.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_migration_replay.py -v`.
Plan 3 wires the live replay suite as a required CI job.

## Task 7: Destructive operations and portable entry points

**Files:** operations/README.md, scripts/migrate.sh/.ps1,
scripts/check_drift.sh/.ps1, test_migration_cli.py.

- [ ] Keep operations/zz_* outside bundle discovery and artifact allowlists.
  Destructive forward changes require exact target identity, bundle checksum
  and migration ID in an explicit confirmation record.
- [ ] Release automation refuses plans containing such pending transitions;
  it reports maintenance-required rather than success. A maintenance operator
  uses the reviewed non-production workflow and records evidence; production
  uses its separate owner-run process.
- [ ] Wire thin launchers to team.py. Test .env and PROJECT_ENV_FILE behavior
  in Bash, Git Bash, PowerShell 5.1 and 7, including Path/PATH coexistence.
- [ ] Test public dry-run makes no bootstrap/lock/DML call, production refusal
  for migrate/bootstrap/adopt/recover, exact code/tables routing, and no
  secret-bearing command line or logs.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_migration_cli.py -v`.

## Task 8: Qualification and migration author workflow

**Files:** docs/migrations.md, docs/schema-coverage.md,
scripts/tests/live/test_migration_acceptance.py.

- [ ] Document migration-first authoring, dependency/effects generation,
  adoption, foreign branch history, failed attempts, mutex recovery and
  unsupported scope. Do not promise rollback or detection of every manual write.
- [ ] Exercise shared schema with Alice's applied unmerged bundle and Bob's
  unrelated bundle; both work, a conflicting precondition refuses, and an
  unknown attempt blocks. Verify integration reports foreign IDs explicitly.
- [ ] Run fresh replay and previous-release upgrade, separate and equal schema
  profiles, two real concurrent runners, partial DDL and lost-log acknowledgement.
- [ ] Preserve versioned results for Plan 3 CI; mark unavailable live checks
  unrun and block release readiness. Offline checks alone are insufficient.
- [ ] Review source and tests against all interfaces above before authorized
  delivery. No new dependency or runtime installation is assumed from this plan.

## Completion checklist

- [ ] Every bundle member is immutable and canonical deletions fail CI.
- [ ] Shared foreign history does not block unrelated work; strict targets refuse it.
- [ ] Metadata ownership, no-steal mutex and uncertain attempts work with split users.
- [ ] Drift compares structure and cannot be hidden by a later migration timestamp.
- [ ] Fresh/upgrade replay and existing-schema adoption have live evidence.
- [ ] Production refusal and no-op dry-run tests cover every public command.
