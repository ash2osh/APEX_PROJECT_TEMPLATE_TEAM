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

## Protected release-test

The release workflow triggers on push tags formatted as `schema/v<semver>` (shared schema)
or `app/<alias>/v<semver>` (single application). It builds the selected canonical
archive offline using `build-release --kind schema` or `build-release --kind app --alias <alias>`,
verifies the archive bytes, and passes them to a prepared `self-hosted` test runner.
Its `example-team-apex-test` concurrency group never cancels an in-flight protected run.
The test command derives source commit, release kind, aliases, declarations, SELECT SQL,
and flow members from the verified archive bytes and reads live metadata history:

```text
PYTHONPATH=scripts python3 scripts/team.py \
  --env "$RUNNER_TEMP/test.env" run-release-test \
  scratch/release/release.tar --target targets/test.json \
  --out "$RUNNER_TEMP/test-evidence.json"
```

It requires `role: test` and environment `test`, refuses destructive pending
migrations before payload execution, and passes an in-memory apply result into
qualification. No external test-history, plan, or apply-report file is part
of this flow. Evidence is signed in a separate step with the protected test
key before the production-owner runbook is generated:

```text
scripts/team.py sign-test-evidence \
  --evidence "$RUNNER_TEMP/test-evidence.json" \
  --archive scratch/release/release.tar \
  --private-key "$RUNNER_TEMP/test-signing-key.pem" \
  --out "$RUNNER_TEMP/test-evidence.sig"
```

Signing binds `archive_digest`, `source_commit`, `kind`, `alias`, and `app_checks_digest`.
The runbook generator verifies that binding again against the same archive before
creating the production handoff. A signed report for one application cannot be reused
for another application or a schema release.

Persistent staging is observational and does not prove a fresh installation,
isolation, or arbitrary-DML coverage.
