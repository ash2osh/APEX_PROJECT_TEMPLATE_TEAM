# Shared Schema Migrations — Implementation Plan (Plan 2 of 3)

> **For agentic workers:** Use superpowers:executing-plans, or
> superpowers:subagent-driven-development when delegation is authorized.
> Checkboxes are implementation work, not verification already performed.

**Revision:** 3 — removes declared effects; see spec §7 "What version 1 gives up".
**Goal:** Make shared-schema changes attributable, restartable and reproducible.
**Architecture:** Immutable two-member SQL/verification bundles describe
transitions, with target and dependencies declared in a strictly parsed SQL
header. A separate shared metadata-profile store serializes participating
runners and records immutable provenance and attempt state. Expected schema
state has one source: canonical replay evidence. Shared-development history
differs from strict promotion history; fresh and upgrade replay provide that
evidence.

**Authoring requires no database.** Header scaffolding and dependency checksum
filling are offline operations over the migrations directory, because
developers in this topology may have neither a local database nor a container
runtime.
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

## Interaction with the shared application

Spec §2.1 makes APEX source and migrations behave differently under branching,
and the difference lands on this plan.

**Migrations stay branch-isolated; the schema they change does not.** A bundle is
a hand-authored file, so it travels on its branch as expected. Applying it writes
to the shared development schema, where every developer sees the result at once.
That asymmetry is already modelled: a colleague's applied but unmerged bundle is
`foreign_applied` in shared mode and does not block unrelated work.

**A page can now be merged without the migration it depends on.** This has no
equivalent in the per-developer topology. Alice applies her bundle to the shared
schema and builds a page against the new column in the shared application. Bob
exports — his capture contains Alice's page, because the application is shared —
commits it to his branch and merges. Alice's bundle is still on her branch.
Integration then deploys a page referencing a column its schema does not have.

Nothing here prevents that: the coupling runs from APEXlang bytes to SQL, and
version 1 does not parse APEXlang for column references. It is caught, not
prevented, and this plan states where:

- fresh replay builds integration's schema from merged migrations only, so the
  missing column is absent there rather than quietly present;
- deployment verification and post-deploy drift report the failure against a
  named application and target;
- review is the preventive control — a bundle merges before or with the first
  export that depends on it, and whoever applies a bundle to the shared schema
  owns merging it promptly rather than leaving it applied and unmerged.

Do not answer this by forbidding unmerged bundles on the shared development
schema. A developer cannot build the page before the column exists, so that rule
would stop development outright. The applied-but-unmerged window is deliberate;
what must be short is its duration.

## File responsibilities

| Files to create | Responsibility |
|---|---|
| scripts/teamlib/migration_bundle.py | filename/header/member validation and checksum |
| scripts/teamlib/migration_plan.py | dependency ordering, pending/foreign/conflict decisions |
| scripts/teamlib/migration_store.py; scripts/sql/migration_metadata.sql | versioned metadata and mutex |
| scripts/teamlib/fingerprints.py; scripts/sql/schema_inventory.sql | supported-object inventory/canonicalization |
| scripts/teamlib/migrate.py | state machine, target routing and verification |
| scripts/teamlib/replay.py | disposable preparation, replay, canonical evidence and adoption |
| scripts/teamlib/authoring.py | offline header scaffolding and dependency checksum filling |
| scripts/migrate.sh/.ps1; scripts/check_drift.sh/.ps1 | shared CLI launchers |
| operations/README.md | destructive-reset workflow, outside migration discovery |
| scripts/tests/test_migration_*.py; scripts/tests/live/ | behavioral and Oracle tests |
| docs/migrations.md; docs/schema-coverage.md | authoring, coverage and recovery contract |

Public commands added to team.py:

```text
migration-plan --source DIRECTORY --history FILE --mode shared|strict
migrate [--source DIRECTORY] [--dry-run] [--bootstrap]
check-drift
new-migration --author NAME --slug SLUG --target tables|code
add-dependency MIGRATION_ID --on MIGRATION_ID
replay --source DIRECTORY --replay-env FILE [--previous DIRECTORY]
snapshot --out DIRECTORY
adopt-baseline MIGRATION_ID --evidence DIRECTORY
recover-migration RUN_TOKEN [--attempt ATTEMPT_ID] --evidence DIRECTORY
export-history --out FILE
```

Offline migration-plan reads supplied history, never connects. Online dry-run
may read metadata but never bootstrap/acquire a write mutex or mutate data.
migrate derives shared/strict mode from verified target role: developer and
shared integration use shared; test/replay use strict. A user cannot weaken
a test deployment by passing --mode shared to migrate.

## Task 1: Bundle, dependency and immutability contract

**Files:** migration_bundle.py, migration_plan.py, authoring.py,
test_migration_bundle.py, test_migration_plan.py, test_authoring.py.

**Types/interfaces:**

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Migration:
    id: str
    stamp: str
    target: str                           # from the SQL header, authoritative
    checksum: str                         # over both bundle members
    dependencies: tuple[tuple[str, str], ...]  # (ID, exact bundle checksum)
    destructive: bool

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
  Validate UTC calendar values. Each has exactly one .verify.sql sibling and
  no other sibling members; an orphan .verify.sql, a missing one, or an
  unexpected third member is an error. A file that never matches the primary
  pattern at all — `.gitkeep`, `README.md` — is not discovered and is not an
  error; the sibling-completeness rule applies only to files sharing a
  matched primary's exact stem.
- [ ] Require LF/UTF-8 and a strictly parsed header block preceding any
  executable statement, containing exactly one `-- migration-version:`,
  one `-- target: tables|code`, one `-- destructive: true|false`, and zero or
  more `-- depends-on: <id> sha256:<hex>`. Reject a missing directive, a
  duplicate, one appearing after the first statement, and any unrecognised
  `-- <word>:` directive — silently ignoring an unknown directive would let a
  typo disable a declaration. Parse the header with the same qualified lexer
  used for statement boundaries, so a directive inside a string literal or a
  block comment is not mistaken for a declaration.
  Dependency checksums bind the exact bundle bytes depended upon, including
  dependencies not yet applied.
- [ ] Implement offline authoring helpers in authoring.py: `new-migration`
  writes a UTC-stamped bundle skeleton with a complete header and an empty
  verification file; `add-dependency` computes the named dependency's current
  bundle checksum and inserts or updates its `-- depends-on:` line in place,
  preserving all other bytes. Both operate on the migrations directory only —
  no connection, no subprocess. Test that a developer with no database
  configured at all can author a complete, valid bundle.
- [ ] Prohibit SQLcl control commands CONNECT/CONN, HOST, EXIT, error-policy
  changes, remote/nested includes and substitution re-enabling in migration
  members. Validate statement boundaries with a qualified lexer; do not
  inspect comments/string literals with an unanchored grep.
- [ ] Verification members are restricted SELECT assertions returning exactly
  assertion_name and PASS/FAIL. Run them through VERIFY with no mutation grants,
  no application routine execution and an allowed SQL/function grammar. Reject
  DML, DDL, PL/SQL, SQLcl controls and transaction statements in these members.
  Missing/duplicate assertion rows and unknown status refuse. A mutating verify
  fixture must be rejected before execution, not allowed to run and then be
  judged by its own output.
- [ ] Compute bundle checksum from canonical sorted member path/SHA-256 pairs.
  No normalization after hashing; execute an immutable staged copy of those bytes.
- [ ] Implement topological ordering with (stamp, full ID) ready-node tie-break.
  Applied dependencies may come from verified central history in shared mode;
  strict artifact history must be self-contained.
- [ ] A checksum conflict or unresolved attempt blocks apply. In shared mode,
  absent APPLIED history is foreign_applied; strict mode rejects it. Canonical
  deletion is enforced separately by CI diff against the protected base.
- [ ] Test equal-second tie, invalid calendar date, missing/cyclic dependency,
  edits to either bundle member, foreign applied history and incomplete
  attempts. Edit an unapplied dependency after a dependent has declared its
  checksum and prove the exact mismatch refuses, even when the resulting
  object would be unchanged. Test an unrecognised `-- foo:` directive, a
  duplicate `-- target:`, a directive after the first statement, and a
  `-- depends-on:` appearing inside a string literal.

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
Run test_authoring.py the same way — it needs no database connection at all.

## Task 2: Central metadata, bootstrap and persistent mutex

**Files:** migration_store.py, scripts/sql/migration_metadata.sql,
test_migration_store.py, scripts/tests/live/test_migration_store.py.

**Interface:** `bootstrap(store_target)`;
`acquire(store_target, run_token, worker_identity, host)`;
`release(store_target, run_token)`; `read_history(store_target) -> dict`;
`export_history(store_target, out) -> None`.
All Target arguments here are the isolated METADATA profile. Extend Plan 1's
control_store foundation; app and migration mutexes are distinct resources.

- [ ] Verify isolation: METADATA_SCHEMA differs from tables/code and payloads
  have no direct/role/ANY/proxy or controller-routine mutation access. VERIFY
  cannot mutate application or metadata state. Keep controller secrets/tokens
  out of staged SQL, payload processes and diagnostic logs. Live negative tests
  attempt a metadata write/drop via both payload users and expect refusal.
- [ ] Define TEAM_MIGRATION_META(version, project_id, schema_set_digest),
  TEAM_MIGRATION_MUTEX(singleton_id, owner_token, worker_identity, host,
  acquired_at), TEAM_MIGRATION_HISTORY(id, checksum, target, dependencies_json,
  payload_manifest_json, source_commit, applied_sequence, applied_at,
  applied_by, run_token), TEAM_MIGRATION_ATTEMPT(attempt_id, migration_id,
  checksum, state, run_token, worker_identity, started_at, finished_at,
  diagnostic_digest).
  TEAM_MIGRATION_MUTEX carries worker_identity and host directly rather than
  relying on TEAM_MIGRATION_ATTEMPT for them: acquisition happens before
  dependencies are checked and before RUNNING is committed (see Apply and
  failure semantics below), so a crash in that window leaves the mutex held
  with no attempt row yet to look the holder up in. Bind both at acquisition
  so the blocking message spec §7 requires can always name a worker.
  Define primary/unique keys, state/target checks and NOT NULL contracts.
  There is no expected-fingerprint table: revision 3 made canonical replay
  evidence under `database/` the single source of expected schema state, so a
  `TEAM_MIGRATION_OBJECT` store would be a second copy free to drift from it.
- [ ] Bootstrap through the write guard, verify existing definitions exactly,
  tolerate only a verified concurrent identical bootstrap, and report partial
  installation as setup-required. Metadata absence is not an empty applied set.
- [ ] Implement the mutex as a committed transition that never waits on a row
  lock, with no timeout stealing. A DDL commit in another connection cannot
  release this logical lock. A bare conditional `UPDATE` is not acceptable:
  Oracle blocks it on the row lock until the holding transaction ends, so a
  client that crashed before committing freezes every later acquirer inside
  its SQLcl subprocess. Use spec §7's acquisition block verbatim:

```sql
-- Bind values through the adapter; this is the mutex transition only.
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
     SET owner_token = :run_token, worker_identity = :worker_identity,
         host = :host, acquired_at = SYSTIMESTAMP
   WHERE singleton_id = 1;
  COMMIT;
END;
/
```

- [ ] Distinguish exactly four acquisition outcomes and test each: success;
  `ORA-00054` (another transaction holds the row — in-flight or uncommitted
  crash); `ORA-20001 MUTEX_HELD:<token>` (cleanly held, token names the holder
  for the blocking message); `NO_DATA_FOUND` (metadata not bootstrapped — a
  setup-required error, never an acquisition failure). Any other error is a
  hard failure. A subprocess wall-clock timeout is a backstop for network
  stalls only, and is never read as "not acquired": it leaves the target
  uncertain and blocked. Add a live test with two concurrent runners proving
  the second fails fast rather than hanging.

- [ ] Every state mutation predicates on the owner token. Release only after
  all workers ended and no unresolved attempt remains. Compare-and-clear the
  selected token; record recovery evidence when clearing a failed run.
- [ ] `--attempt` is optional: a crash between mutex acquisition and the first
  committed RUNNING row (dependency/checksum confirmation runs under the mutex
  before RUNNING is written) leaves owner_token set with no attempt row at all.
  Requiring an attempt ID in that case would make the mutex unrecoverable. When
  omitted, clear the mutex directly from worker-termination evidence with no
  attempt-state repair, since none was ever recorded. Test this exact case:
  acquire, crash before any attempt row, recover with no `--attempt`.
- [ ] Reject manual "unlock by age". Recovery evidence must identify all workers
  and establish that no payload process/session is still live; if privileges
  cannot establish this, require the environment owner to supply evidence.
- [ ] Read the recovery-owner role for this target from its tracked
  `targets/*.json` contract (spec §7; Plan 1 Task 1 defines
  `targets/development.json` for the developer role) and include it in every
  refusal caused by a held migration mutex, mirroring Plan 1's
  `recover-app-lock`. Do not introduce a second recovery-owner file — migration
  and app mutexes read the same per-target contract.
- [ ] `export-history` writes `read_history`'s exact result to a canonical JSON
  file through a read-only metadata connection — no mutex, no write guard
  bypass needed since it changes nothing. This is what "history supplied by
  the environment owner" (plan-release, gen-runbook) actually means in
  practice: a human runs this once against the target being planned for and
  hands the file off, rather than plan-release or gen-runbook connecting to
  anything themselves. Test that it never acquires the mutex and never
  succeeds against a production-classified target.
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
  structure. A reserved TEAM_MIGRATION_*, TEAM_APP_* or TEAM_CONTROL_* object in
  an application schema is an error, not an exclusion. Refuse unsupported
  classes or unknown ownership. Explicit external verification scopes prevent
  whole-system claims.
- [ ] Test changed/deleted/added objects, invalid packages, grants, distinct
  schemas, same schemas, CRLF, SQL literals containing schema names, sequence
  advancement without definition change and schema-name-neutral replay.
- [ ] Implement check-drift against canonical replay evidence under
  `database/`, with no write. There is no expected-fingerprint store in
  metadata; that evidence is the single source of expected state. On a shared
  development target, differences attributable to known unmerged migrations
  are reported separately from unexplained drift, and the report says plainly
  which category each object falls in. LAST_DDL_TIME and compilation status
  appear only as diagnostics, never as drift on their own. A change made
  before an unrelated migration remains changed; DML requires semantic
  checks.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p test_migration_fingerprints.py -v`.

## Task 4: Existing-schema adoption

**Files:** replay.py, test_migration_adoption.py, docs/migrations.md.

**Interface:** `adopt_baseline(id, evidence, profiles) -> None`.

Revision 3 removed this task's effects-authoring half along with the effects
document (spec §7). What remains is adoption: establishing a trustworthy
starting point for a schema that already exists and was not built by
migrations. There is no `prepare_migration`, no disposable target requirement
for ordinary authoring, and no candidate-missing-effects authoring role —
authoring is offline and lives in Task 1's `authoring.py`.

Adoption still requires a disposable target, because its whole purpose is to
prove that a reviewed initial migration reproduces the live schema. That is a
one-off operation performed by whoever adopts the template, not something a
developer does to write a migration.

- [ ] For initial adoption, generate/review baseline DDL and reference-data
  assertions, prove empty replay, then compare live structural/semantic state
  under the central mutex. Stamp baseline only on exact match; never execute
  CREATE statements over existing business data.
- [ ] Metadata setup may precede adoption, but until adoption or a verified
  empty bootstrap the target has no accepted starting point and strict
  operations refuse. A nonempty schema is never blessed by sampling its live
  objects into evidence; adoption must prove a reviewed initial migration
  reproduces it on an empty disposable target.
- [ ] Test valid adoption, changed live baseline, missing reference data,
  candidate changes outside declared scope and unknown normalization.
  Confirm production adoption is refused.

Command: `PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -p 'test_migration_*tion.py' -v`.

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
- [ ] For each migration capture the live inventory, persist RUNNING, execute
  staged SQL with target routing and snapshot immediately after. There is no
  precondition gate on that inventory: version 1 declares no expected before
  state (spec §7), and a shared development schema legitimately holds a
  colleague's applied-but-unmerged migration, so requiring inventory equality
  would refuse every apply for the exact reason the design calls normal. Verify
  data/compiled-object postconditions through the observation-only VERIFY
  profile; a payload profile must not run verify SQL.
- [ ] Capture both profiles after execution and record the before/after
  inventories as observed history for recovery and audit. Version 1 has no
  declared effects to compare them against, so do not gate on a predicted
  change set. Do still compare inventories taken before and after verification
  and refuse if verification itself mutated structure.
  Commit APPLIED history and attempt state in one transaction on the metadata
  connection. Record sequence under the mutex.
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
  invalid PL/SQL compiled with warning, duplicate runner, an edited bundle
  member, destructive refusal and partial success. Include an upstream table
  change invalidating a dependent package and prove the next migration is not
  blocked by that invalidation. The fake SQLcl test must exercise
  the public migrate CLI, not just helper parsing.

State sequence to encode:

```text
READY -> mutex-owned -> dependencies/checksums verified -> RUNNING(committed)
RUNNING -> payload + postconditions verified -> APPLIED(committed)
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
  transition's verification assertions and semantic checks. Snapshot to
  scratch and compare
  complete canonical manifests with committed evidence, including added files.
  Never call backup_db against the developer working-tree database/ mirror.
- [ ] When --previous is supplied, create another fresh target, replay previous
  release and representative data fixtures, then upgrade with the new release.
  Check postconditions and compare final structure with fresh replay.
- [ ] Provide explicit snapshot --out for generating reviewable evidence. Only
  an explicit evidence-update workflow stages that output into database/.
  Its metadata records source commit/digest, normalizer version and coverage.
- [ ] Test earlier timestamp merged after a later migration, incompatible
  same-object changes, missing canonical member, untracked extra snapshot file,
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

- [ ] Document offline migration authoring, dependency declaration,
  adoption, foreign branch history, failed attempts, mutex recovery and
  unsupported scope. Do not promise rollback or detection of every manual write.
  State the shared-application coupling plainly for authors: applying a bundle
  to the shared schema makes it visible to everyone immediately, any colleague's
  export can carry a page that depends on it, and merging the bundle promptly is
  the author's responsibility rather than a review formality.
- [ ] Exercise shared schema with Alice's applied unmerged bundle and Bob's
  unrelated bundle; both work, a declared dependency whose checksum no longer
  matches refuses, and an unknown attempt blocks. A same-object change between
  the two bundles with no declared dependency is not caught here — spec §7
  states plainly that version 1 has no object-level precondition, so that case
  is closed by review, not by this test. Verify integration reports foreign
  IDs explicitly.
- [ ] Exercise the shared-application coupling above: a page exported from the
  shared application depends on a column created by a bundle that is still
  unmerged, and the page merges first. Fresh replay must not contain the column,
  and the integration deployment must fail with a report naming the application
  and the missing object rather than deploying a broken page. Record it as a
  detected-not-prevented case, since review is the preventive control.
- [ ] Run fresh replay and previous-release upgrade, separate and equal schema
  profiles, two real concurrent runners, partial DDL and lost-log acknowledgement.
- [ ] Preserve versioned results for Plan 3 CI; mark unavailable live checks
  unrun and block release readiness. Offline checks alone are insufficient.
- [ ] Review source and tests against all interfaces above before authorized
  delivery. No new dependency or runtime installation is assumed from this plan.

## Completion checklist

- [ ] Every bundle member is immutable and canonical deletions fail CI.
- [ ] Shared foreign history does not block unrelated work; strict targets refuse it.
- [ ] A page merged ahead of the bundle it depends on fails the integration gate
  with a report naming both, rather than deploying.
- [ ] Metadata ownership, no-steal mutex and uncertain attempts work with split users.
- [ ] Drift compares structure and cannot be hidden by a later migration timestamp.
- [ ] Fresh/upgrade replay and existing-schema adoption have live evidence.
- [ ] Production refusal and no-op dry-run tests cover every public command.
