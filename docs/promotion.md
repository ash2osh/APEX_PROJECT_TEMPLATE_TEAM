# Promotion and production handoff

Promotion uses one immutable `release.tar`. The archive is built from a
resolved Git commit, not from the working directory, and contains complete
migration bundles plus owned application bytes. Its external SHA-256 is the
release identity; the manifest does not hash itself. Build and verify are
offline:

```text
scripts/team.sh build-release --ref v1.2.3 --version 1.2.3 --out scratch/release
scripts/team.sh verify-release scratch/release/release.tar
scripts/team.sh plan-release scratch/release/release.tar \
  --history history.json --target targets/test.json --out scratch/plan.json
```

The archive uses sorted POSIX ustar entries with fixed metadata and is checked
for complete member hashes before use. Dirty/untracked files, symlinks,
deployment bindings, logs, credentials and sync state never enter it. The
destination owner supplies exact history; a changed target or history requires
a new plan. Production `apply-release` is refused.

Each packaged migration retains all authored members, including an optional
`.down.sql` and `.down.verify.sql` pair. Release application is forward-only:
an artifact whose latest metadata event is `REVERTED` is not pending, and a
dependent of a reverted artifact blocks the plan. Use the non-production
`undo-migration`/`redo-migration` commands with the shared destructive
confirmation document when a reviewed reversal is needed; release tooling
never generates rollback SQL or runs database undo.

## Release-test evidence

The protected test job downloads and verifies the same archive bytes, applies
only the target's pending migrations, deploys packaged apps in master-before-
subscriber order, and records structural, data, APEX, subscription and
candidate-app results. The adapter writes a canonical apply report to
`$RUNNER_TEMP/apply-report.json`:

```text
scripts/apply_release.sh scratch/release/release.tar \
  --plan scratch/test-plan.json --history test-history.json \
  --target targets/test.json --env "$TEAM_TEST_ENV_FILE" \
  --out "$RUNNER_TEMP/apply-report.json"
```

The report binds version, status, one source commit, archive digest, target
state key, target digest, history digest and pending IDs. The test job then
qualifies the persistent target with that report:

```text
scripts/team.py --env "$TEAM_TEST_ENV_FILE" qualify-target \
  --source-commit "$GITHUB_SHA" --aliases "$TEAM_APP_ALIASES" \
  --release-archive scratch/release/release.tar \
  --apply-report "$RUNNER_TEMP/apply-report.json" \
  --out "$RUNNER_TEMP/test-evidence.json"
```

Evidence is version 2 and has exactly these top-level fields:

```text
version: 2
final_status: PASS | FAIL
source_commit: exact archive source commit
archive_digest: exact archive digest
toolchain_digest: digest of the declared toolchain contract
target_identity: verified non-secret target identity
run_identity: workflow/run identifiers
qualification_identity:
  target_kind: persistent
  observation_sequence: latest accepted sequence
  observation_digest: latest accepted observation digest
  history_digest: collapsed-history digest
application_checks:
  status: PASS | FAIL
  checks_digest: declaration digest
  coverage: explicit apps and checks
  unknown: integer count
results:
  migrations: PASS | FAIL
  application_deploy: PASS | FAIL
  application_checks: PASS | FAIL
```

`source_commit` is the single source-SHA field. Version 2 has no legacy
disposable-run or freshness fields. The evidence is canonical compact UTF-8
JSON with one LF terminator; persistent staging evidence remains observational
and does not prove a fresh installation.

## Detached signature and handoff

The protected test environment supplies the Ed25519 private key as the
`TEAM_TEST_SIGNING_KEY_CONTENT` secret. The workflow writes it only to
`$RUNNER_TEMP/test-signing-key.pem` with mode 0600, signs the exact evidence
bytes, and removes that file in an `always()` cleanup step:

```text
scripts/team.py sign-test-evidence \
  --evidence "$RUNNER_TEMP/test-evidence.json" \
  --private-key "$RUNNER_TEMP/test-signing-key.pem" \
  --out "$RUNNER_TEMP/test-evidence.sig"
```

The public trust key is configured independently as `TEAM_TRUST_KEY`; it is
not generated from the private secret during the run. Missing signer/trust
configuration uploads unsigned diagnostics but produces no handoff. Unsigned
evidence, non-canonical bytes, failed evidence, and version-1 evidence are
rejected by `gen-runbook`.

Generate the production handoff offline:

```text
scripts/team.sh gen-runbook scratch/release/release.tar \
  --history production-history.json --target targets/production.json \
  --test-evidence "$RUNNER_TEMP/test-evidence.json" \
  --signature "$RUNNER_TEMP/test-evidence.sig" \
  --trust-key "$TEAM_TRUST_KEY" --out scratch/PRODUCTION_RUNBOOK.md
```

The runbook names source/archive/evidence digests, target identity, metadata
owner, pending IDs and checksums, application order, backup and restore
evidence, verification queries, partial-failure recovery and
success-before-log uncertainty. It is a human document, not a production
apply switch. The owner must re-read identity and history under the metadata
mutex and record attempts through the isolated metadata schema. Source SQL is
trusted reviewed deployment code, not a sandbox. Database migration undo does
not roll back an APEX Builder import; Builder recovery remains a separate
evidence-driven workflow, and uncaptured edits or arbitrary DML are outside
the inventory boundary.
