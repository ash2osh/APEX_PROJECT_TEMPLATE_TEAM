# Persistent staging qualification — Design

**Date:** 2026-09-10
**Status:** Draft
**Parent:** [Staging qualification and migration undo/redo index](2026-09-10-staging-qualification-and-undo-design.md)

## 1. Purpose

The shipped database workflow attempts to create Oracle Free + ORDS in Docker
and replay every migration. It cannot run on the configured GitHub-hosted
runner because real SQLcl is unavailable, and its `command -v sql` guard can
match an unrelated executable. A template must not advertise a required gate
that its default runner cannot execute.

This design replaces that mechanism with optional qualification of a real,
persistent staging/UAT target. It preserves application checks and signed
release-test evidence without claiming the target is fresh or isolated.

## 2. Removed disposable surface

Delete:

- `ci/provisioners/docker_pdb.sh`;
- `scripts/ci_replay_runner.py` after moving its reusable check adapters;
- the `replay` job in `database-checks.yml`;
- the polling `qualification` job in `integration.yml`;
- `ci-replay`, `_invoke_provisioner`, and disposable runner/provisioner checks;
- disposable fields in `ci/runner-contract.json`; and
- disposable replay and live Docker tests.

Removal includes `OFFLINE_COMMANDS`, CLI dispatch, launcher tests,
`test_release_workflow.py`, `docs/ci.md`, and `ci/app-checks/README.md` references.
The offline model helper in `scripts/teamlib/replay.py` is separate and stays
unless an implementation audit proves it has no documented caller.

`ci-doctor` stays. It validates workflow Python pins, required non-secret
toolchain/profile declarations, and secret-like fields; it no longer validates
a Docker provisioner, image, runner, or isolation claim.

## 3. Qualification component

Move declaration loading and the SQL/flow adapters from the disposable runner
to `scripts/teamlib/qualification.py`. Preserve their fail-closed behavior:

- every declared alias/check is represented in coverage;
- missing SQL files or flow adapters fail rather than become `UNKNOWN`;
- SQL checks use the read-only VERIFY profile; and
- report identity and declaration digests are deterministic.

Add one read-only online command:

```text
scripts/team.py --env <file> qualify-target \
  --source-commit <sha> \
  --aliases <comma-separated-aliases> \
  --out <qualification.json> \
  [--release-archive <release.tar> --apply-report <apply-report.json>]
```

The two optional arguments appear together or not at all. Without them, the
command produces staging evidence. With them, it verifies the release archive
and canonical `apply-release` report before producing release-test evidence.
It does not migrate or deploy.

Before checking applications, it verifies:

- a non-production environment and expected role;
- exact source commit and application bindings;
- target identity;
- collapsed migration history and its digest; and
- the latest accepted observation sequence and digest.

## 4. Workflows

### Default database checks

`database-checks.yml` contains only its fast offline job: unit tests plus
`ci-doctor`. It requires no Oracle, Docker, SQLcl, or secrets.

### Manual staging qualification

`integration.yml` uses `workflow_dispatch` and one serial job:

1. Checkout the selected exact `github.sha`.
2. Validate the protected integration environment and bindings.
3. Set up controller state and adopt a frontier only when none exists.
4. Check canonical and observed-frontier drift.
5. Apply ordinary non-destructive pending migrations.
6. Deploy every configured APEX application from the exact SHA.
7. Run `qualify-target`.
8. Upload the report, including a well-formed failure report when available.

The job retains `cancel-in-progress: false`. It never passes destructive
migration confirmation. A push does not trigger or wait for this workflow.

The report explicitly says `target_kind: persistent` and lists foreign current
history. It is an observation of shared staging, not fresh-install evidence.

## 5. Release-test evidence

After applying an exact release archive to the protected test target,
`release.yml` saves a canonical apply report and invokes `qualify-target` with
the archive and report. The command produces evidence version 2:

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
  observation_digest: latest accepted after digest
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

`source_commit` is the single source-SHA field. Version 2 has no
`qualification_sha`, replay identity, fresh result, or upgrade result.

The updated `gen-runbook` accepts only version 2. In-flight version-1 handoffs
must finish before this toolchain upgrade or be re-qualified. The repository
contains no tracked real signed version-1 evidence requiring an automatic
migration path.

Add an offline `sign-test-evidence` command. It validates canonical PASS
evidence, signs its exact bytes with an Ed25519 private-key file, writes only a
detached signature, and never prints or copies key material.

The protected test environment supplies the signing key as a secret.
`release.yml` writes it only under `RUNNER_TEMP` with mode 0600 after environment
approval, signs the evidence, removes the key in an `always()` cleanup step, and
passes evidence, signature, and public trust key to `gen-runbook`. Missing
signer/trust configuration uploads unsigned diagnostics but produces no
production handoff.

Unsigned staging evidence can never satisfy `gen-runbook`.

## 6. Tests and documentation

Tests cover:

- complete removal of disposable paths and CLI commands;
- retained SQL/flow adapter behavior and coverage accounting;
- manual, serial, exact-SHA workflow structure;
- persistent target/frontier/history binding;
- archive/apply-report identity mismatches;
- version-2-only evidence validation and Ed25519 signatures;
- signing refusal for non-PASS or non-canonical evidence; and
- unsigned staging evidence rejected for production handoff.

Update `README.md`, `docs/ci.md`, `ci/app-checks/README.md`,
`docs/promotion.md`, CLI help, and workflow contract tests. Documentation must
state the lost fresh-install guarantee and the external configuration required
for staging, test, flow checks, and signing.
