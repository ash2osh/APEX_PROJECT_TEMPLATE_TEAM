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

The protected test job downloads and verifies the same archive bytes, applies
only the target's pending migrations in dependency order, deploys packaged apps
master-before-subscriber, and records structural, data, APEX, subscription and
candidate-app results. It signs canonical `TEST_EVIDENCE.json` bytes with an
Ed25519 key held outside the artifact.

The non-production release-shaped entry point is explicit and target-bound:

```text
scripts/apply_release.sh scratch/release/release.tar \
  --plan scratch/test-plan.json --history test-history.json \
  --target targets/test.json --env "$TEAM_TEST_ENV_FILE"
```

It materializes the verified archive into a private staging directory, routes
SQL through the five configured profiles, and deploys packaged application
bytes in the contracted master order. It refuses production contracts; the
production path below is a signed offline handoff only.

Generate the production handoff offline:

```text
scripts/team.sh gen-runbook scratch/release/release.tar \
  --history production-history.json --target targets/production.json \
  --test-evidence TEST_EVIDENCE.json --signature TEST_EVIDENCE.sig \
  --trust-key release-test-public.pem --out scratch/PRODUCTION_RUNBOOK.md
```

The runbook names the source/archive/evidence digests, target identity,
metadata owner, pending IDs and checksums, master order, backup and restore
evidence, verification queries, partial-failure recovery and success-before-log
uncertainty. It is a human document, not a production apply switch. The owner
must re-read identity and history under the metadata mutex and record attempts
through the isolated metadata schema. DDL rollback is not assumed and source
SQL is trusted reviewed deployment code, not a sandbox.
