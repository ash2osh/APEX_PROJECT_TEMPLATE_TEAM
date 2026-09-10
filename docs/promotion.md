# Promotion and production handoff

Promotion moves one immutable `release.tar`. Build it from a resolved Git
commit and verify the archive bytes offline:

```text
scripts/team.sh build-release --ref v1.2.3 --version 1.2.3 --out scratch/release
scripts/team.sh verify-release scratch/release/release.tar
```

The manifest is closed and payload-derived: migration IDs, checksums,
dependencies, target, destructive flags, reversibility, application trees,
master contract, and check declarations are reconstructed from archive bytes.
Symlinks, credentials, deployment bindings, logs, and sync state are not
packaged. Production `apply-release` remains refused.

## Protected test run

The release workflow downloads and verifies the same archive, then runs on
`runs-on: [self-hosted, team-apex, test]` in environment `test`. It materializes
the protected profile, signing key, public trust key, and production history
below `$RUNNER_TEMP` with mode 0600, and removes those bounded files in an
`always()` cleanup step. It invokes exactly one online command:

```text
scripts/team.py --env "$RUNNER_TEMP/test.env" run-release-test \
  scratch/release/release.tar --target targets/test.json \
  --out "$RUNNER_TEMP/test-evidence.json"
```

The command derives the archive source commit and aliases, verifies that the
target contract and config have `role: test`, reads live metadata history,
recomputes pending work, refuses destructive migrations before controller setup,
applies and verifies non-destructive work under the metadata mutex, deploys
packaged applications, and emits unsigned evidence. The apply result is passed
in memory; operators do not transport a plan, history, or intermediate result
file between steps.

Evidence version 2 is canonical compact UTF-8 JSON with one LF terminator. It
contains `source_commit`, `archive_digest`, `toolchain_digest`, complete
`target_identity`, `qualification_identity` with `target_kind: persistent`,
`observation_sequence`, `observation_digest` from the accepted after inventory,
`history_digest`, application-check coverage, and PASS/FAIL result fields.
Staging and test targets are persistent observations; they do not prove a fresh
installation, isolation, or arbitrary-DML coverage.

## Sign and hand off

Signing is a separate privilege and uses only the protected test private key:

```text
scripts/team.py sign-test-evidence \
  --evidence "$RUNNER_TEMP/test-evidence.json" \
  --private-key "$RUNNER_TEMP/test-signing-key.pem" \
  --out "$RUNNER_TEMP/test-evidence.sig"
```

`sign-test-evidence` rejects non-canonical, failed, incomplete, non-persistent,
or non-`role: test` evidence. The public trust key is independently supplied;
it is never derived from the private key. Missing signer or trust material
leaves diagnostics available but cannot create a handoff.

Generate the offline production-owner runbook:

```text
scripts/team.sh gen-runbook scratch/release/release.tar \
  --history "$RUNNER_TEMP/production-history.json" \
  --target targets/production.json \
  --test-evidence "$RUNNER_TEMP/test-evidence.json" \
  --signature "$RUNNER_TEMP/test-evidence.sig" \
  --trust-key "$RUNNER_TEMP/test-trust-key.pem" \
  --out scratch/PRODUCTION_RUNBOOK.md
```

The runbook is an owner-reviewed document, not a production apply switch. It
names source/archive/evidence digests, target identity, pending IDs and
checksums, application order, verification queries, backup/restore evidence,
and recovery instructions. The production owner re-reads identity and history
under the metadata mutex before any separately authorized action. Database undo
does not roll back an APEX Builder import.
