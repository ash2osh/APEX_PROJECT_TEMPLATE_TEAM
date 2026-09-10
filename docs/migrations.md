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
scripts/team.py --env .env migrate --source migrations --dry-run
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

Change only `confirmed: false` to `true`, save the canonical JSON, and pass it
with `--destructive-confirmation confirmation.json` to the matching command.
The same convention covers forward migrate and undo/redo; boolean shortcuts,
missing or duplicate entries, stale checksums, wrong actions, and extra entries
are refused. The orchestrator never creates an affirmative confirmation.

## Frontier and recovery

An empty metadata owner may adopt one observed sequence-zero frontier. History
without a frontier is refused; it cannot be repaired by importing over the
workspace. Drift, verification failure, unknown SQLcl results, and mutex
release uncertainty stop before the next payload. Inspect retained `.sync-state/`
evidence and the run token, then use the explicit recovery command after the
named owner reviews the target.

Applying a migration to the shared schema must be merged promptly because a
colleague's export can depend on it. Database undo does not roll back an APEX
Builder import; Builder recovery is a separate evidence-driven workflow.
Production writes remain refused, and uncaptured Builder edits or arbitrary DML
outside the supported inventory are outside the observed boundary.
