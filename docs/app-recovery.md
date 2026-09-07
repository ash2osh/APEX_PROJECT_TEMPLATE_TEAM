# Application recovery contract

`export-app` reads the shared Builder application without taking its mutex. It
brackets the export with the persistent application generation and mutex state;
if an import begins, completes, or is recovered during that window, the
capture is discarded and no tracked file is changed.

An export conflict retains an immutable recovery bundle below
`.sync-state/recovery/<id>/`. Review the four trees in that bundle, write a
complete resolved tree, then run:

```text
team.py resolve-export <recovery-id> --resolved <directory>
```

The command applies only the reviewed owned-file delta and records a resolution
receipt. Commit the result before retrying an import. An import never replaces a
shared application merely because a command was rerun: the current export must
match the verified baseline, a receipt must account for the selected commit, or
an explicit `--replace-from` recovery capture must bind the replacement.

Before a destructive import the command prints a team pause message. After the
payload begins, a known failure leaves the target uncertain and an unknown
SQLcl result retains the mutex. A recovery owner must establish worker
termination, retain a current capture, and run `recover-app-lock` with the
exact held token when one exists. Recovery increments the generation, so a
capture cannot silently straddle a crash-and-clear cycle.

File-level interruptions leave journals in `.sync-state/journals/`. Use only
the explicit actions below; later user edits are never overwritten:

```text
team.py recover-files <operation-id> --action finish
team.py recover-files <operation-id> --action restore
```
