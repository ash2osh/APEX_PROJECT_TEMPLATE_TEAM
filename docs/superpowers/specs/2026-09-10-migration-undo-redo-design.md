# Migration undo, redo, and destructive confirmation — Design

**Date:** 2026-09-10
**Status:** Draft
**Parent:** [Staging qualification and migration undo/redo index](2026-09-10-staging-qualification-and-undo-design.md)

## 1. Purpose and boundaries

Persistent non-production targets need an explicit recovery path for reviewed
migrations. This design adds authored down migrations, global-LIFO undo, and
dependency-safe redo.

- No reversal SQL is generated.
- Undo/redo are refused for production.
- A migration without committed down members cannot be undone.
- Database undo does not revert APEX application source.

## 2. Directional bundle

Every migration keeps its required forward pair:

```text
<id>.sql
<id>.verify.sql
```

A reversible migration adds both:

```text
<id>.down.sql
<id>.down.verify.sql
```

The down pair is optional as a unit. A lone/orphan member, symlink, invalid
UTF-8/LF file, or unexpected sibling is rejected. Verification files may be
empty but must exist for their direction.

The down SQL header contains only one `migration-version` and one `destructive`
directive. It inherits `target` and the dependency graph from the forward
member. `depends-on` and `target` are forbidden in the down header.

`Migration` gains down SQL/verification paths and bytes plus a separate
`down_destructive` flag. Forward and down SQL use `_assert_controls`; both
verification files use the SELECT-only validator.

The bundle checksum covers path, length, and SHA-256 for all present members in
this order: forward SQL, forward verification, down SQL, down verification. A
down pair cannot be added after the forward bundle has been applied because
that would change its immutable checksum.

`new-migration` still creates only the forward pair. An author adds both down
members before the migration is first applied.

## 3. Append-only lifecycle

`TEAM_MIGRATION_HISTORY` becomes an event ledger:

- add `operation VARCHAR2(4) NOT NULL CHECK (operation IN ('up','down'))`;
- make `applied_sequence` the primary key; and
- add a non-unique index on `(id, applied_sequence)`.

The latest event per ID defines current state:

| Latest operation | Status |
|---|---|
| `up` | `APPLIED` |
| `down` | `REVERTED` |

`read_history` reads scalar and CLOB data by `applied_sequence`, validates rows
in sequence order, then collapses to the latest event per ID. This avoids the
current `(id, field)` CLOB collision when an ID has multiple events.

`TEAM_MIGRATION_ATTEMPT` gains:

- `action VARCHAR2(16) NOT NULL CHECK (action IN ('migrate','undo','redo'))`;
- nullable `confirmation_digest VARCHAR2(64)`.

`action` identifies both direction and lifecycle intent for failed/unknown
attempts. Existing attempts backfill to `migrate`. The confirmation digest is
required only for destructive attempts.

Successful history already links to `attempt_id`, so the digest is not repeated
on history rows. Exported history/state includes the linked attempt evidence.
The JSON store mirrors the same event and attempt model.

## 4. Planning, undo, and redo

`REVERTED` is recognized but is neither applied nor ordinary pending. Routine
`migrate` never silently reapplies it.

A dependency is satisfied only when it is currently `APPLIED` with the declared
checksum or is never applied and scheduled earlier in the same forward plan. A
reverted dependency blocks its dependent. An already-applied migration with a
currently reverted dependency is an inconsistent lifecycle and blocks.

Undo may target only the currently `APPLIED` migration with the greatest latest
event sequence after collapse. After `A up`, `B up`, `B down`, A is the undo
top; the old B-up row is not. A request for any other ID refuses and names the
actual top. There is no force flag.

Redo requires:

1. current status `REVERTED`;
2. matching local and recorded bundle checksums;
3. a complete down pair; and
4. every declared dependency currently `APPLIED` with its declared checksum.

Redo is not a global stack. Independent reverted migrations may be redone in
either dependency-valid order. Redo runs the original forward SQL and forward
verification.

The independent release planner receives the same rules: a reverted artifact
migration is excluded from pending, independent new migrations may proceed, and
dependents of a reverted migration block. Strict targets reject foreign applied
or reverted history; shared targets report it.

## 5. One destructive-confirmation contract

There is no usable destructive-migration CLI confirmation today. `apply_plan`
accepts a loose value, but `team.py migrate` never supplies it. Replace that
hook with one file option on all three commands:

```text
--destructive-confirmation <confirmation.json>
```

The closed JSON schema is:

```json
{
  "version": 1,
  "confirmations": [
    {
      "migration_id": "20260910T120000__alice__example",
      "action": "migrate",
      "bundle_checksum": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "payload_target_state_key": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "confirmed": true
    }
  ]
}
```

`action` is `migrate`, `undo`, or `redo`. Direction, executed member, and
tables/code role are derived from action plus the immutable bundle; the operator
does not repeat them in the record.

Under the mutex, the core recomputes the exact destructive operation set and
verified payload target state keys. The file must contain exactly one matching
confirmed entry per required operation and no duplicate, stale, unknown, or
extra entry. A boolean, partial match, wrong action/checksum/target, or no-longer
pending operation refuses before the first payload.

Dry-run for migrate/undo/redo emits exact entries with `confirmed: false`.
Production refuses before confirmation. Automated integration/release jobs
never pass this option and report destructive work as maintenance-required.

The canonical confirmation-document digest is stored on the attempt. A failed
or unknown destructive attempt therefore retains the authorizing record digest;
a successful history event reaches it through `attempt_id`.

## 6. Apply state machine and CLI

Factor one private operation helper in `migrate.py`:

```text
acquire mutex
  → recompute and validate lifecycle, plan, confirmation, and frontier
  → record attempt with action and confirmation digest
  → observe before
  → execute direction-specific SQL
  → run direction-specific verification
  → observe after
  → append history and observation events
  → mark attempt applied
  → release mutex
```

Expose:

- `apply_plan(...)` for never-applied forward work;
- `apply_undo(...)`; and
- `apply_redo(...)`.

`apply_plan` no longer accepts bare `True` or a loosely shaped mapping.

CLI:

```text
scripts/team.py --env <file> migrate [options] \
  [--destructive-confirmation <confirmation.json>]
scripts/team.py --env <file> undo-migration <id> [--dry-run] \
  [--destructive-confirmation <confirmation.json>]
scripts/team.py --env <file> redo-migration <id> [--dry-run] \
  [--destructive-confirmation <confirmation.json>]
```

All use the same verified profiles and reviewed drift inputs. Undo/redo run the
idempotent metadata bootstrap before acquiring the mutex. Known failure records
`FAILED`; lost acknowledgement records `UNKNOWN`. Both retain the mutex and
action for evidence-driven recovery.

## 7. Metadata version 2 rollout

Oracle DDL commits implicitly, so bootstrap converges each piece independently:

1. Add/backfill/validate history `operation`.
2. Replace the old ID primary key with `applied_sequence` primary key.
3. Add/validate the `(id, applied_sequence)` index.
4. Add/backfill/validate attempt `action`.
5. Add/validate attempt `confirmation_digest`.
6. Verify the complete shape, then advance the single project metadata row to
   version 2.

Each step inspects dictionary metadata first and is restart-safe. An unexpected
same-name object or constraint shape refuses rather than being replaced. Fresh
bootstrap creates version 2 directly. Update both embedded bootstrap SQL and
`scripts/sql/migration_metadata.sql`.

The JSON store performs an atomic versioned upgrade and retains a recoverable
preimage until replacement succeeds.

## 8. Tests and documentation

Tests cover:

- complete down-pair parsing, safety, verification, and checksum rules;
- repeated history events and multi-chunk CLOB reconstruction by sequence;
- current-state collapse to `APPLIED`/`REVERTED`;
- attempt action and confirmation digest on success/failure/unknown results;
- reverted dependency and release-planning behavior;
- true latest-event LIFO undo and dependency-safe redo;
- forward/down destructive flag independence;
- one confirmation schema across migrate/undo/redo;
- rejection of missing, boolean, partial, duplicate, stale, extra, wrong-action,
  wrong-checksum, and wrong-target confirmation;
- dry-run output and unattended automation refusal; and
- fresh, v1, and every partially upgraded metadata shape.

Update `README.md`, `docs/migrations.md`, `docs/promotion.md`, CLI help, release
packaging/planning tests, launcher tests, and the metadata SQL reference.
