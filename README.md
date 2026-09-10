# APEX project template for teams

This repository treats one development APEX application as a shared physical
resource. The stable identity is the lowercase application alias under
`apps/<alias>/`; developers do not receive separate Builder applications.
Integration, test and production bind that alias to different workspace and
application IDs.

## Daily development loop

1. Edit in App Builder and export the whole current application with
   `scripts/team.sh export-app <alias>`.
2. Review the durable reconciliation/recovery evidence, then commit the
   resulting `apps/<alias>/` tree.
3. Pull and continue from the shared source.

There is no import step in the ordinary daily loop. Import overwrites the shared Builder workspace
and requires a team pause; a Git branch does not isolate Builder state.

SQL intent belongs in two-member migration bundles under `migrations/`. Shared
schema application and persistent-target qualification are separate concerns:
qualification observes a protected non-production target and never treats it as
a fresh installation.

## First setup

Copy `.env.example` to a local `.env`, fill the credential-free SQLcl profile
names and expected identities, then run:

```text
scripts/team.sh doctor
scripts/team.sh setup-state
scripts/team.sh register-app <alias>
```

The default workflow is stdlib-only. `gen-runbook` and
`sign-test-evidence` need `cryptography`; graphify tooling has its own optional
dependencies:

```text
python3 -m pip install -e '.[promotion]'
```

Keep `TABLES`, `CODE`, `APEX`, `METADATA` and observation-only `VERIFY` profiles
explicit. `METADATA_SCHEMA` must be isolated from application schemas.

## Migrations and qualification

Create and validate bundles offline:

```text
scripts/team.sh new-migration --author alice --slug add-status --target tables
scripts/team.sh migration-plan --source migrations --history history.json --mode shared
```

The manual integration workflow checks an exact commit after applying reviewed
migrations and deploying the selected applications:

```text
scripts/team.py --env "$TEAM_ENV_FILE" qualify-target \
  --source-commit "$GITHUB_SHA" \
  --aliases "$TEAM_APP_ALIASES" \
  --out "$RUNNER_TEMP/qualification.json"
```

The report is evidence version 2 with `target_kind: persistent`, the observed
frontier and declared application-check coverage. Persistent staging evidence
does not prove fresh installation or isolation.

For a release-test target, qualify the exact applied archive and report, then
sign the canonical evidence bytes with the protected test key:

```text
scripts/team.py --env "$TEAM_TEST_ENV_FILE" qualify-target \
  --source-commit "$GITHUB_SHA" --aliases "$TEAM_APP_ALIASES" \
  --release-archive scratch/release/release.tar \
  --apply-report "$RUNNER_TEMP/apply-report.json" \
  --out "$RUNNER_TEMP/test-evidence.json"
scripts/team.sh sign-test-evidence --evidence "$RUNNER_TEMP/test-evidence.json" \
  --private-key "$RUNNER_TEMP/test-signing-key.pem" \
  --out "$RUNNER_TEMP/test-evidence.sig"
```

See [docs/ci.md](docs/ci.md), [docs/migrations.md](docs/migrations.md) and
[docs/promotion.md](docs/promotion.md) for workflow, lifecycle and handoff
rules.

## Recovery and safety

`.sync-state/` contains retained baselines, checkpoints, manifests, mutex
state and recovery captures. It is not scratch data. An uncertain app or
migration remains held until the named recovery owner reviews evidence. Use
[docs/app-recovery.md](docs/app-recovery.md) and
[docs/conflict-resolution.md](docs/conflict-resolution.md).

Production writes remain refused by automated commands. `gen-runbook` verifies
an immutable archive and signed test evidence and produces an offline handoff
for the production owner.
