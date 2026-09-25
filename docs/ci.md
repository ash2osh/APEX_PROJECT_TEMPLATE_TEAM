# CI and qualification

## Offline pull-request gate

`database-checks.yml` is the whole offline gate and never opens Oracle or loads
a target profile. One job runs `ruff check scripts/`, `bash -n` over the shell
launchers, the stdlib test suite, the JSON and line-ending checks, and the
closed contract doctor:

```text
PYTHONPATH=scripts python3 scripts/team.py ci-doctor \
  --contract ci/runner-contract.json
```

`ci/runner-contract.json` describes the required Python, SQLcl, JDK, APEX,
database, and Ed25519 capability. `ci-doctor` validates that shape only; it
does not claim the live runner is installed or reachable.

## Protected integration

The manually dispatched integration job runs on
`runs-on: [self-hosted, team-apex, integration]` in the protected integration
environment. It checks out the exact `github.sha`, materializes the
`TEAM_ENV_CONTENT` secret at `$RUNNER_TEMP/integration.env` with mode 0600,
maps `TEAM_FLOW_RUNNER`, and invokes one command:

```text
PYTHONPATH=scripts python3 scripts/team.py \
  --env "$RUNNER_TEMP/integration.env" run-integration \
  --out "$RUNNER_TEMP/qualification.json"
```

The command first loads migrations, canonical inventory, application trees,
and the application-check bundle from one exact Git commit. It performs
observed runtime/profile preflight before writes, then
sets up controller state, captures one live inventory, adopts a frontier only
for empty metadata, checks drift, applies ordinary migrations, deploys all
configured apps, runs checks, and emits the canonical report. A missing or
unusable flow adapter, profile identity mismatch, old version, or production
classification is a refusal. The profile file is removed in a bounded
`always()` cleanup step; report upload is also `always()` and warns when no
report exists because preflight may refuse early.

`qualify-target` remains available for read-only diagnosis. It derives neither
write state nor a fresh installation claim. Version-2 reports contain
`target_kind: persistent`, `observation_digest` from the accepted after
inventory, history and toolchain digests, target identity, and structured
application-check results.

## Candidate checks

Each configured alias has `ci/app-checks/<alias>.json`. SELECT checks run only
through the observation-only `VERIFY` profile and must return framed rows such
as:

```text
TEAM_ASSERT|employee_table_exists|PASS
```

Missing, malformed, duplicate, FAIL, or empty assertion output fails closed.
Flow checks invoke `TEAM_FLOW_RUNNER --alias <alias> --check-json <path>` and
accept only a structured `PASS`, `FAIL`, or `UNKNOWN` result. Unknown checks
are counted and never silently promoted.

## Local release workflow

CI does not build or publish release archives. The operator cuts the archive from the shared development database, then runs test qualification, signing and runbook generation locally; the complete commands and key-handling steps are in [promotion.md](promotion.md).

```text
scripts/team.sh --env .env build-release --kind schema --version 1.1.0 --out scratch/release
scripts/team.sh verify-release scratch/release/release.tar
scripts/team.sh --env .env.test run-release-test \
  scratch/release/release.tar --target targets/test.json --out scratch/test-evidence.json
scripts/team.sh sign-test-evidence --evidence scratch/test-evidence.json \
  --archive scratch/release/release.tar --private-key /secure/local/test-signing-key.pem \
  --out scratch/test-evidence.sig
scripts/team.sh gen-runbook scratch/release/release.tar \
  --history scratch/production-history.json --target targets/production.json \
  --test-evidence scratch/test-evidence.json --signature scratch/test-evidence.sig \
  --trust-key /secure/local/test-trust-key.pem --out scratch/PRODUCTION_RUNBOOK.md
```

Use `--kind app --alias <alias>` to cut a single-application archive. Test evidence and the generated production-owner runbook bind the same archive digest, target identity and app-check digest. `verify-release` remains offline and supports format 2 signed handoffs already in circulation. The local process never authorizes a production write.

Persistent staging is observational and does not prove a fresh installation, isolation, or arbitrary-DML coverage.
