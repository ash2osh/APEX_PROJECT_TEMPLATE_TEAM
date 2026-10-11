# Deployment follow-up ideas: validity and design

Date: 2026-10-11. Status: draft design for later implementation, not implemented.
Scope: evaluate the nine ideas in [Evidence and validity](#evidence-and-validity).

## Intended outcome

Make deployment preparation and recovery more reliable without weakening identity,
immutable-source, exact-source, human coordination, or runtime-observation gates.
The user requested a plan for valid ideas, separately from current phases9–10
implementation. This design authorizes no live operations, branch integrations,
commits, pushes, downstream upgrades, or implementation of these ideas.

## Evidence and validity

| Idea | Verdict | Source evidence and correction |
|---|---|---|
| 1. SQLcl trap linting | Valid; high priority | `baseline.py` uses CLOB source assembly and `known-limitations.md` documents hyphen continuation/blank-line trimming. Inspect actual generated delivery SQL as well as stored source. Warnings alone cannot justify exact-source success; a proven transport corruption must refuse before payload. Oracle-version-specific query diagnostics need a qualified release pair. |
| 2. Durable write-attempt records | Valid; high priority | `migrate.py::_verify_receipts_and_attempts` and `migration_revision.py::inspect_migration_lock` scan ignored scratch attempt directories. A local marker can preserve the freeze after scratch deletion while the checkout remains. It cannot survive deletion/recreation of an uncommitted worktree unless an operator explicitly preserves an archive or reviewed commit. It cannot see another developer's repository or serve as database migration history. |
| 3. Automation operations and automatic restoration | Partly valid | `application_lifecycle.py`, qualification app940269, and `docs/apex-26.2-qualification.md` already observe automation disablement. Explicit list/check and separately authorized enable/disable/run-once operations are useful. Automatic restoration conflicts with AGENTS runtime rules and is excluded. Never infer the cause of a state transition. |
| 4. Target patch checks/version-aware verification | Conditionally valid | Existing `apex_compatibility.py` enforces26.2; `verify_publish_state.py::verify_source_bytes` keeps exact bytes after current qualified projections. A source/target version diagnostic can improve errors. Relaxed comparison needs authentic import/re-export evidence for each exact version pair; no pair is qualified by this review. |
| 5. Template drift warning | Valid with limits | `verify_local.py` validates lock metadata, and `upgrade_template.py` stores per-file hashes, commit, release and ref. Local file-versus-lock drift differs from upstream obsolescence. Check locally first; a fresh upstream claim requires explicit read-only fetch/compare. Never overwrite project customizations. |
| 6. Project function allow-list | Deferred, conditional | The migration query contract has a narrow `SAFE_FUNCTIONS` list. Naming a custom function "pure" cannot establish that it has no autonomous transactions, remote calls or side effects. Phase9 should stay conservative. A later design must bind exact schema/name/edition/source digest and independently audited side effects; a simple editable allow-list is insufficient. |
| 7. Changed-value rehearsal metrics | Valid with explicit scope | `migration_rehearsal.py` already distinguishes SQLcl row feedback from postcondition checks. SQL affected rows do not prove expected final values after triggers. Use migration-owned pre/post aggregate metrics with strict queries and explicit keys; do not guess affected columns or record row contents. |
| 8. Fixture/test-authoring guidance | Valid | `self_improve.md` distinguishes fake SQLcl from native driver evidence. Use approved disposable targets, tagged fixtures and observed target-specific invariants. Savepoints cover DML only; DDL/sequence/autonomous side effects need explicit ownership and cleanup evidence. |
| 9. Agent operational notes | Valid after correction | Signal tests in `test_sqlcl_session.py` exercise process-tree cleanup. Never terminate a process by a pattern containing the invoking shell. Socket availability depends on the actual environment: the2026-10-11 baseline ran socket tests successfully. Do not assert every Codex sandbox blocks sockets or recommend changing sandbox/network policy as a universal workaround. |

## Constraints

- Python>=3.10, standard library only for core commands; Bash/PowerShell parity.
- APEX26.2 and SQLcl>=26.3.0.0 for this branch;26.1 qualification/backport is separate.
- No custom team metadata tables, mutexes, rosters or database migration ledgers.
- No automatic enable/resume/repair, DB restores, commits, exports, upgrades or imports.
- Reads use named configured profiles and identity-checked read-only transactions.
- Preserve expected user/schema/environment and refuse missing/incomplete evidence.
- Keep raw source/re-export and diagnostics private; never log passwords, task payloads,
  checksum salts or sensitive reference-data rows into tracked reports.
- Unknown source-version pairs retain exact-byte comparison.
- No assertion of live or native Windows success from offline tests/Linux runs.

## Design decisions

### Preflight transport diagnostics

Add a shared `lint_sqlcl_transport(sql, delivery_mode)` helper. It tokenizes literals,
comments and PL/SQL boundaries; recognizes physical line continuation and source
blank-line risks in direct delivery. For CLOB assembly, inspect the emitted SQL,
not the string it will construct. Refuse a proven executable-delivery hazard;
report an uncertain formatting risk with location and recommended safe assembly.
Do not reject a trailing hyphen that exists only as data assembled via CHR(10).
Version-specific database parser warnings are informational until reproduced.

### Durable local attempt evidence

Write immutable `attempt-event.<event-id>.json` files next to the migration
source, using a unique event ID for every record. Each event carries a stable
`attemptId` and a `predecessorEventId` (null only for the initial pending event);
a `runId` links folders when one rollout spans several migrations.
Include schemaVersion, target identity, relative migration path, source hash,
state, write-start uncertainty and a private recovery pointer; never credentials
or query text. Atomically install a conservative `pending` event before handing
payload to SQLcl. Legal terminal transitions are `pending` to either explicit
`apply-not-started` evidence or a verified receipt. `apply-not-started` needs
current evidence that no payload was launched; receipt completion needs exact
source/target matching and fresh identity. The reducer accepts one complete
chain only: duplicate event IDs, missing or out-of-order predecessors,
multiple branches, contradictory outcomes, malformed/symlinked events, or a
failed outcome write all remain locked/unknown. Reuse the atomic no-replace
writer at `scripts/migration_manifest.py:689–721`; never replace an earlier
event. Test byte preservation of existing events and refusal after outcome
write failure. A marker freezes source, not coordination across independent
repositories.

Durability is scoped to scratch deletion in a retained checkout. Before deleting
a worktree, require an explicitly preserved archive or reviewed commit; never
auto-commit evidence. Automatic persistence in a Git common directory or across
worktrees needs a separate design and qualification. `revise` does not copy
attempts or receipts, and no shared database ledger is introduced.

### Source version diagnostics and future profiles

Record explicitly observed source/target releases in recovery diagnostics and
print exact-byte mismatch context. Source release provenance must be captured
from the qualified export-time APEX metadata observation (for example,
`APEX_RELEASE.VERSION_NO`), stored with the source export outside strict import
descriptors, and bound to a digest of the canonical exported files. A mismatch
after export makes that provenance stale; legacy, manual, edited, or unbound
source trees have unknown provenance. Do not guess a source version from
templates or the installed client. An opt-in serializer profile must name a qualified
version pair and exact path/token projections, preserve raw files, reject file
set changes, and distinguish raw equality from qualified semantic equality.
Unknown versions/pairs, unsupported projections and extra differences fail.
No tolerant profile should ship enabled until its real exports, importer settings
and downstream bookmark/runtime implications are independently reviewed.

### Explicit automation operations

Start with read-only list/check that reuses existing scoped lifecycle adapters.
Write commands need exact app/workspace/schema/connection and explicit operator
selection. Preserve before/after evidence; acquire required APEX session context
only for APIs whose native behavior was qualified. Refuse ambiguous ownership,
missing visibility and unexpected lifecycle transitions. Do not provide a rollout
step that silently restores every pre-import state. If a future rollout step is
added, show the explicit reviewed action list in the existing confirmation.

### Drift and rehearsal observability

`doctor` can report locally customized, missing and conflicting template files
against the lock's hashes. Upstream comparison is explicit and names the chosen
ref and observed commit. Rehearsal metrics are pure scalar aggregate queries
supplied by the migration author, captured within the same transaction before
and after the complete folder/batch DML payload and again after rollback, using
author-selected tagged keys and predicates. These are folder/batch aggregate
observations, not generic per-row or per-statement changed-value counts. They
supplement postconditions; they cannot turn an unverified rollback into success.
Report counts/statuses only.

## Implementation order

1. Fix confirmed current-phase correctness findings and finish phases9–10.
2. Add transport linting and durable attempt evidence.
3. Add version diagnostics and template drift diagnostics.
4. Add explicit automation reads/writes and scoped rehearsal metrics after native qualification.
5. Document fixture/process/environment guidance alongside the relevant task.
6. Revisit qualified serializer profiles and custom functions only after their evidence gates.
