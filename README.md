# APEX project template for teams

This repository treats each APEX application as a shared physical Builder
resource. The tracked source is `apps/<alias>/`; the database and Builder
workspace are identified by the validated target profiles, not by a Git branch.

## Daily Builder loop

Edit in Builder, export the whole application, review the durable evidence, then
commit and push:

```text
scripts/team.sh export-app <alias>
git diff -- apps/<alias>/ .sync-state/
git commit -am "Describe the Builder change"
git push
```

There is no import step in the normal edit loop. `import-app` overwrites the
shared Builder workspace and requires a separately posted team pause. Transient
Builder edits and arbitrary DML outside the supported inventory remain
unobservable.

## Local setup

Copy `.env.example` to `.env`, fill the credential-free SQLcl connection names
and expected identities, and validate the profile set:

```text
scripts/team.sh doctor
scripts/team.sh setup-state
scripts/team.sh register-app <alias>
```

The online integration and test jobs run on prepared `self-hosted` runners.
`TEAM_FLOW_RUNNER` names the executable browser adapter when a declaration has a
flow check. It must return one JSON object with `status` `PASS`, `FAIL`, or
`UNKNOWN`; the runner preflight also verifies SQLcl, JDK, database/APEX version
markers, profile identity, and Ed25519 capability.

## One normal command per protected target

The offline pull-request gate runs unit tests, static checks, and `ci-doctor`.
Protected integration derives `HEAD` and the configured aliases itself:

```text
scripts/team.py --env "$RUNNER_TEMP/integration.env" run-integration \
  --out "$RUNNER_TEMP/qualification.json"
```

It captures one live inventory, adopts a sequence-zero frontier only for empty
metadata, checks drift, applies ordinary migrations, deploys every configured
application, runs declared checks, and writes canonical persistent evidence.
Destructive pending work returns `maintenance-required` with an exact template;
the command never invents confirmation.

Release testing uses the verified archive and a protected test-role profile:

```text
scripts/team.py --env "$RUNNER_TEMP/test.env" run-release-test \
  scratch/release/release.tar --target targets/test.json \
  --out "$RUNNER_TEMP/test-evidence.json"
```

The archive supplies the source commit and application aliases. The command
reads live metadata history, recomputes the plan, refuses destructive pending
work, deploys exact packaged bytes, and emits unsigned evidence. The target
must have `role: test`, environment `test`, and matching application bindings.

`qualify-target` remains the read-only diagnostic primitive when an operator
needs to inspect a persistent target directly. Version-2 evidence contains
`target_kind: persistent`, the latest `observation_digest` (the accepted
after-inventory digest), history, toolchain, target identity, and check
coverage. Persistent staging does not prove fresh installation or isolation.

## Migrations and recovery

Create migration bundles offline. A reversible migration has authored
`.down.sql` and `.down.verify.sql` members; down SQL is never generated. Undo
is global LIFO and redo is explicit for a `REVERTED` migration. All writes use
the isolated METADATA profile, retain attempt/mutex evidence under
`.sync-state/`, and refuse production.

Destructive `migrate`, `undo-migration`, and `redo-migration` share one closed
confirmation document. Start with `--dry-run`, review the exact migration ID,
action, bundle checksum, and `payload_target_state_key`, then change only
`confirmed: false` to `true` and pass:

```text
scripts/team.py --env .env migrate --source migrations --dry-run
scripts/team.py --env .env migrate --source migrations \
  --destructive-confirmation confirmation.json \
  --expected-inventory database/schema-inventory.json \
  --actual-inventory scratch/live-inventory.json
```

Stale, partial, boolean-shortcut, or extra confirmation entries are refused.
An unknown or failed attempt retains the mutex until the named recovery owner
reviews the evidence and runs an explicit recovery command. Database undo does
not roll back an APEX Builder import; use the app recovery workflow for that
separate boundary.

## Promotion

Build and verify one immutable `release.tar` offline. The release workflow then
downloads and verifies those bytes, runs `run-release-test`, signs the canonical
test evidence with the protected Ed25519 key, and generates an offline
production-owner runbook. Automated production writes remain refused.

See [docs/ci.md](docs/ci.md), [docs/migrations.md](docs/migrations.md),
[docs/promotion.md](docs/promotion.md), and
[docs/app-recovery.md](docs/app-recovery.md) for the detailed contracts.
