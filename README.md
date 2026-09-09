# APEX project template for teams

This repository treats one development APEX application as a shared physical
resource. The stable identity is the lowercase application alias under
`apps/<alias>/`; developers do not receive separate Builder applications.
Integration, test and production bind that same alias to different workspace
and application IDs.

## Daily development loop

1. Edit in App Builder and export the whole current application with
   `scripts/team.sh export-app <alias>`.
2. Review the durable reconciliation/recovery evidence, then commit the
   resulting `apps/<alias>/` tree.
3. Pull and continue from the shared source.

There is deliberately no daily import step: there is no import step in the
ordinary loop. Import overwrites the shared
Builder workspace and requires a team pause. A Git branch does not isolate
Builder state; exporting on a branch does not preserve an individual agent's
private APEX changes.

SQL intent belongs in two-member migration bundles under `migrations/`. The
database is qualified through disposable replay and declared application
checks, not by treating a live mirror as the source of truth.

## First setup

Copy `.env.example` to a local `.env`, fill the credential-free SQLcl profile
names and expected identities, then run:

```text
scripts/team.sh doctor
scripts/team.sh setup-state
scripts/team.sh register-app <alias>
```

The daily workflow needs no third-party packages. Two commands do:
`gen-runbook` needs `cryptography` to verify signed test evidence, and the
graphify tooling needs `graphify` and `tree-sitter-sql`. Install what you need:

```text
python3 -m pip install -e '.[promotion]'
```

The example target contracts are binding specifications, not credentials. Keep
`TABLES`, `CODE`, `APEX`, `METADATA` and observation-only `VERIFY` profiles
explicit. `METADATA_SCHEMA` must be isolated from application schemas.

## Migrations and promotion

Create and validate bundles offline:

```text
scripts/team.sh new-migration --author alice --slug add-status --target tables
scripts/team.sh migration-plan --source migrations --history history.json --mode shared
scripts/team.sh replay --source migrations --replay-env replay.json
```

See [docs/migrations.md](docs/migrations.md)
and [docs/promotion.md](docs/promotion.md)
for shared-history, disposable replay and release rules.

## Recovery and safety

`.sync-state/` contains retained baselines, checkpoints, manifests, mutex
state and recovery captures. It is not scratch data. An uncertain app or
migration remains held until the named recovery owner reviews evidence.
Use [docs/app-recovery.md](docs/app-recovery.md)
and [docs/conflict-resolution.md](docs/conflict-resolution.md).

`scratch/` is disposable working space, not evidence. Recovery records under
`.sync-state/` retain their own copy of the captured tree, so scratch can be
pruned at any time:

```text
scripts/team.sh prune-scratch --keep 5
```

Add `--dry-run` to see what would go. Capture directories a recovery record
still references are never removed.

Production writes are not exposed by the automated commands. `gen-runbook`
verifies an immutable archive and signed test evidence and produces an offline
handoff for the production owner.
