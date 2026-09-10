# Shared schema migrations

Create migrations offline with `team.py new-migration`; the generated pair is
an immutable SQL member and a SELECT-only verification member. Add exact
dependencies with `team.py add-dependency`. The header is authoritative for
target, destructive status, and dependency checksums.

An authored reversal is a second complete pair, committed before the forward
bundle is first applied:

```text
<id>.down.sql
<id>.down.verify.sql
```

The down header contains only `migration-version` and `destructive`. Down SQL
is never generated, and its checksum is part of the immutable four-member
bundle. A lone down member, unsafe SQL, or a changed pair is refused.

`migration-plan` is safe to run without an environment file. Developer and
shared integration history may contain foreign applied or reverted migrations;
strict test targets require the complete selected history. A local `REVERTED`
migration is neither ordinary pending work nor a reason for routine `migrate`
to reapply it. Dependents of a reverted migration are blocked until the
dependency is redone.

Undo is global LIFO: only the currently applied migration with the greatest
event sequence can be undone, and it must have a down pair. Redo is explicit
and dependency-safe; it runs the original forward members. The metadata v2
event ledger appends `up` and `down` rows keyed by `applied_sequence` and
collapses the latest row per ID to `APPLIED` or `REVERTED`.

All lifecycle writes use the isolated METADATA target and retain a mutex and
attempt record. An unresolved or unknown attempt blocks later work until its
worker has ended and the recorded evidence is reviewed.

Destructive migrate, undo, and redo operations use the same closed JSON file:

```json
{
  "version": 1,
  "confirmations": [
    {
      "migration_id": "20260910T120000__alice__example",
      "action": "migrate",
      "bundle_checksum": "<64 lowercase hex characters>",
      "payload_target_state_key": "<64 lowercase hex characters>",
      "confirmed": true
    }
  ]
}
```

Use `--dry-run` first to obtain exact entries with `confirmed: false`, then pass
the reviewed file with `--destructive-confirmation <confirmation.json>`. The
operator reviews the immutable identity fields and changes only confirmation;
missing, duplicate, stale, extra, boolean, or wrong-action entries are
refused. Production database operations remain refused.

Applying a bundle to the shared schema makes it visible to every developer
immediately. A page exported after that can depend on an unmerged migration, so
the author owns merging the bundle promptly. The shared schema is not the
oracle for candidate readiness: persistent qualification and declared app
checks are separate promotion evidence.

Database undo does not revert APEX application source. Uncaptured Builder edits
and arbitrary DML outside the supported inventory remain outside the observed
boundary; use the app recovery workflow for Builder recovery.
