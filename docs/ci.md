# Persistent CI and candidate application checks

The default `database-checks` workflow is deliberately offline. It checks the
repository with the unit suite and runs:

```text
PYTHONPATH=scripts python3 scripts/team.py ci-doctor --contract ci/runner-contract.json
```

`ci/runner-contract.json` contains no credentials or target provisioner. The
doctor validates version 1, the declared Python/SQLcl/JDK/APEX/database and
Ed25519 toolchain requirements, the five profiles (`TABLES`, `CODE`, `APEX`,
`METADATA`, `VERIFY`), both production-safety booleans set to `false`, and the
absence of populated secret-like fields. The offline job does not require
Oracle, Docker, SQLcl, or a secret.

## Manual integration qualification

The `integration` workflow is started with `workflow_dispatch` and runs one
serial job in the protected `integration` environment. It checks out the exact
`github.sha`, prepares the credential-free environment profile, initializes
metadata state, adopts an observed frontier when needed, checks drift, applies
reviewed pending migrations, deploys the selected applications, and then runs:

```text
PYTHONPATH=scripts python3 scripts/team.py --env "$TEAM_ENV_FILE" qualify-target \
  --source-commit "$GITHUB_SHA" \
  --aliases "$TEAM_APP_ALIASES" \
  --out "$RUNNER_TEMP/qualification.json"
```

The protected environment supplies `TEAM_ENV_FILE` (a path),
`TEAM_ENV_CONTENT` (the file contents), and `TEAM_APP_ALIASES` (the complete
comma-separated binding set). The profile must expose the five SQLcl roles,
including an observation-only `VERIFY` connection, and must identify the same
non-production instance across them. The workflow never supplies a destructive
migration confirmation. It uploads `qualification.json` with `if: always()`;
application-check failures carry a structured `FAIL` report when available.

The report binds one source commit, target identity, current migration history,
and the latest accepted observation sequence/digest. It explicitly records
`target_kind: persistent`. Persistent staging does not prove fresh installation;
it is observational shared state and does not prove isolation or a clean
upgrade path.

## Candidate application declarations

Store one version-1 JSON declaration at `ci/app-checks/<alias>.json` for every
selected application. A declaration must include at least one restricted
observation-only `select` check and one declarative `flow` check. Every check
names its page, expected objects (for SELECT checks), and a safe relative
fixture. Missing declarations, fixtures, SQL members, adapters, or structured
results fail closed; an unavailable check is never promoted to PASS.

SELECT checks run through the read-only `VERIFY` profile and must return framed
rows in the form `TEAM_ASSERT|assertion_name|PASS` (a FAIL row or no row fails).
Flow checks are sent to the executable named by `TEAM_FLOW_RUNNER`:

```text
TEAM_FLOW_RUNNER --alias <alias> --check-json <path>
```

The adapter must return one JSON object with `status` equal to `PASS`, `FAIL`,
or `UNKNOWN`, plus optional diagnostic and observed fields. The qualification
report records deterministic declaration/check digests, app/page/check IDs,
coverage, expected objects, and the unknown count.

## Release-test qualification

The protected `test` job applies the exact release archive and writes its
canonical report below `RUNNER_TEMP`. It then supplies both files to
`qualify-target`:

```text
PYTHONPATH=scripts python3 scripts/team.py --env "$TEAM_TEST_ENV_FILE" qualify-target \
  --source-commit "$GITHUB_SHA" --aliases "$TEAM_APP_ALIASES" \
  --release-archive scratch/release/release.tar \
  --apply-report "$RUNNER_TEMP/apply-report.json" \
  --out "$RUNNER_TEMP/test-evidence.json"
```

The command verifies the archive digest, source commit, target state key,
history/observation frontier, and application checks before emitting evidence
version 2. The report contains `final_status`, `archive_digest`,
`toolchain_digest`, `target_identity`, `run_identity`, `qualification_identity`,
`application_checks`, and the three PASS/FAIL result fields. It is canonical
compact UTF-8 JSON with one LF terminator. Release handoff signing and trust
key handling are described in [docs/promotion.md](promotion.md).
