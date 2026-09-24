# Shared schema migrations

Migration authoring is offline. `new-migration` creates a forward SQL member
and a SELECT-only verification member; `add-dependency` records exact checksum
edges. A reversible bundle must include authored pairs:

```text
migrations/<id>.sql
migrations/<id>.verify.sql
migrations/<id>.down.sql
migrations/<id>.down.verify.sql
```

Down SQL is reviewed deployment code and is never generated. The metadata v2
ledger stores every event with an immutable `applied_sequence`; the latest
event makes an ID `APPLIED` or `REVERTED`. Routine migrate does not silently
reapply a reverted migration. Undo is global LIFO and redo is explicit and
dependency-safe.

## Safe lifecycle

All migration writes use the non-production isolated `METADATA` profile. A
qualified drift gate and accepted observation frontier are required before a
payload. Every attempt records identity, checksum, action, verification, and
before/after inventory evidence. A known failure is `FAILED`; a lost result is
`UNKNOWN`. Either state retains the mutex for the recovery owner.

Destructive `migrate`, `undo-migration`, and `redo-migration` share one
structured destructive-confirmation convention. Begin with a dry run:

```text
scripts/team.py --env .env migrate --source migrations --dry-run \
  --confirmation-out scratch/confirmation.json
```

Review the exact migration ID, action, bundle checksum, and
`payload_target_state_key`. The output is a version-1 document such as:

```json
{
  "version": 1,
  "confirmations": [
    {
      "migration_id": "20260910T120000__alice__example",
      "action": "migrate",
      "bundle_checksum": "<64 lowercase hex characters>",
      "payload_target_state_key": "<64 lowercase hex characters>",
      "confirmed": false
    }
  ]
}
```

`--confirmation-out` atomically writes the canonical review document and never
overwrites different existing bytes. The generated document remains false; a
human reviews it and changes only `confirmed: false` to `true`, then passes it
with `--destructive-confirmation confirmation.json` to the matching command.
The same convention covers forward migrate and undo/redo; boolean shortcuts,
missing or duplicate entries, stale checksums, wrong actions, and extra entries
are refused. The orchestrator never creates an affirmative confirmation.

## Migration files are kept in the shared database

Each developer has a separate repository, so a colleague's migration files are
not in yours. Every `migrate`, `undo-migration` and `redo-migration` therefore
stores the exact bytes of the bundle it runs (forward pair and, when authored,
the down pair) in the METADATA tables `TEAM_MIGRATION_BUNDLE` and
`TEAM_MIGRATION_MEMBER`, keyed by the bundle checksum:

- The files are stored under the migration mutex **before** the payload runs,
  then read back and checked against the checksum. If storing fails, nothing
  runs and the mutex is released.
- Storage is immutable: a checksum already stored must hold identical bytes.
- A history event is refused (`MIGRATION_MEMBERS_MISSING`) unless its bundle
  is stored, so from now on every applied or reverted migration can be rebuilt
  from the database alone.

Metadata created before this change gets the two tables from the idempotent
bootstrap (`migrate --bootstrap`, `adopt-frontier` or the backfill below).
Migrations applied before the upgrade have history but no stored files; each
developer backfills the ones whose files they hold:

```text
scripts/team.py --env .env adopt-migration-members --source migrations --dry-run
scripts/team.py --env .env adopt-migration-members --source migrations
```

Only a local bundle whose checksum equals the recorded one is stored. The
report lists what was `stored`, `already_stored` and still `missing` (someone
else holds those files). Local files that disagree with recorded history are
refused outright.

## Frontier and recovery

An empty metadata owner may adopt one observed sequence-zero frontier. History
without a frontier is refused; it cannot be repaired by importing over the
workspace. Drift, verification failure, unknown SQLcl results, and mutex
release uncertainty stop before the next payload. Inspect retained `.sync-state/`
evidence and the run token, then use the explicit recovery command after the
named owner reviews the target.

Applying a migration to the shared schema must be merged promptly because a
colleague's export can depend on it. In promotion, shared migrations are released
independently via `schema/v<semver>` tags (`--kind schema`) and apply once per
environment through the qualified migration path, separately from any application.
Individual application releases declare their migration prerequisites in
`app_context/<alias>/release.json`, and those prerequisites are verified against
target history before any application deployment.
Database undo does not roll back an APEX Builder import; Builder recovery is a
separate evidence-driven workflow. Production writes remain refused, and
uncaptured Builder edits or arbitrary DML outside the supported inventory are
outside the observed boundary.
