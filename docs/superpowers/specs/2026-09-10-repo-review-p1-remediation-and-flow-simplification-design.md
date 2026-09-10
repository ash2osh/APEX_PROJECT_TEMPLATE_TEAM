# Repository review P1 remediation and flow simplification — Design

**Date:** 2026-09-10
**Status:** Draft after repository-wide review
**Parent:** [Staging qualification and migration undo/redo index](2026-09-10-staging-qualification-and-undo-design.md)

## 1. Purpose

The persistent qualification and migration-lifecycle implementation has a sound
overall safety model, but repository-wide review found six priority-one gaps in
the real SQLcl, metadata, workflow, evidence, and release paths. These gaps must
be fixed before the online integration or release-test workflows are treated as
operational gates.

This design records those mandatory remediations and simplifies the normal
operator flow. Simplification must reduce duplicated arguments, files, and
workflow steps; it must not remove drift, target-identity, mutex, attempt,
verification, destructive-confirmation, signing, recovery, or production-owner
boundaries.

## 2. Mandatory P1 remediations

### 2.1 Evaluate migration verification assertions

`team.py` and the release adapter currently run a non-empty migration
verification member through SQLcl and then return `True` without reading its
rows. A valid query that returns `FAIL` is therefore recorded as a successful
`APPLIED` or `REVERTED` event.

Create one assertion-result parser shared by migration verification and
candidate SELECT checks. A non-empty verification member succeeds only when:

- SQLcl completes through the qualified read-only `VERIFY` profile;
- at least one strictly framed `TEAM_ASSERT|<name>|PASS` row is returned;
- every framed assertion is `PASS`; and
- no malformed, duplicate, `FAIL`, missing, or unexpected assertion output is
  present.

An empty verification member remains valid only because the existing bundle
contract explicitly permits it. Its success must be represented deliberately,
not inferred from discarded SQLcl output. Verification failure records a known
`FAILED` attempt, retains the migration mutex, and appends no history or
observation event.

### 2.2 Make the online workflow runtime real

The online jobs declare `ubuntu-latest` but do not install SQLcl, establish its
saved connections, materialize all required files, or expose
`TEAM_FLOW_RUNNER`. The workflows therefore cannot execute their documented
online path on a fresh GitHub-hosted runner.

Use protected, prepared self-hosted runners for integration and test. Their
contract includes the qualified SQLcl/JDK/APEX/database tuple, network access,
saved credential references, and the approved flow adapter. The workflows
must still materialize secret-bearing files beneath `RUNNER_TEMP`, set mode
`0600`, and remove them in `always()` cleanup steps. They must explicitly map
`TEAM_FLOW_RUNNER` from protected configuration.

Add an online preflight that checks the actual executables, versions, flow
adapter, profile completeness, and target identity before any metadata or
payload write. `ci-doctor` remains the offline contract-shape check and must not
claim that declared strings prove the live runtime.

### 2.3 Refuse migration metadata schema-set rebinding

`SqlMigrationStore.bootstrap` currently overwrites the existing project row's
`schema_set_digest`. This can associate established history with another
tables/code/metadata schema set after a configuration mistake.

Fresh bootstrap inserts the computed digest. Existing version-2 metadata must
match it exactly or refuse before any other write. Version-1 upgrade may fill
an empty legacy digest, but a different non-empty digest requires a separate,
explicit, evidence-producing adoption operation; ordinary bootstrap, migrate,
undo, redo, and release application never rebind it.

The SQL and JSON stores must enforce the same identity rule.

### 2.4 Preserve unknown SQLcl outcomes through metadata adapters

`SqlMigrationStore` wraps `SqlclError` in `MigrationStoreError`, while the
migration engine recognizes an unknown result only when the immediate exception
is `SqlclError`. A timeout after a committed metadata operation can consequently
be rewritten as `FAILED`, including the case where the history event committed
and its acknowledgement was lost.

Introduce a typed unknown-result error or classify the complete exception cause
chain. Timeout, lost acknowledgement, and explicitly unknown-state failures
remain `UNKNOWN`; deterministic Oracle, validation, and assertion failures are
`FAILED`. If recording the attempt state also has an unknown result, do not make
a second state claim. Retain the mutex and require evidence-driven recovery.

Tests must cover lost acknowledgement for payload execution, verification,
inventory writes, history/event commit, attempt-state update, and mutex release.

### 2.5 Require protected test identity for signed promotion evidence

Version-2 PASS validation currently checks only that `target_identity` is
non-empty. Evidence from an integration-role target can therefore be signed and
accepted by `gen-runbook` as release-test evidence.

Both `sign-test-evidence` and `gen-runbook` must require a complete target
identity with `role: test`, an allowed non-production test environment, and the
persistent target kind. They also validate source commit, archive digest,
application bindings, metadata state key, observation frontier, history digest,
and toolchain digest. Staging/integration reports remain useful unsigned
diagnostics but are never promotion evidence.

### 2.6 Bind release manifest metadata to archived payload bytes

`verify_release` validates generic payload hashes but does not reconstruct the
packaged migration bundles or compare them with `manifest.migrations`. Planning
and runbook generation can therefore trust IDs, checksums, dependencies,
targets, destructive flags, or reversibility that do not describe the SQL bytes
the test adapter executes.

During archive verification:

- reconstruct all top-level migration members with `load_bundles`;
- derive the canonical migration metadata from those members;
- require exact equality with `manifest.migrations`;
- recompute `source_tree`, application tree digests, master-contract digest,
  and application-check digest from the payload records; and
- validate a closed, versioned manifest shape before planning or extraction.

`plan_release`, `apply_release`, qualification, and runbook generation consume
only this verified manifest object.

## 3. Simplification options

### Option A — Documentation-only cleanup

Keep every command and workflow handoff, but improve names and instructions.
This has the smallest implementation cost and does not solve duplicated inputs,
stale intermediate files, or workflow/runtime drift.

### Option B — Workflow wrappers

Keep the current public commands and hide their sequence in shell or composite
GitHub Actions wrappers. This shortens YAML but leaves correctness spread across
processes and temporary JSON files. It also creates another orchestration layer
without reducing the underlying state transitions.

### Option C — Two high-level orchestration commands (recommended)

Keep low-level commands for diagnosis, maintenance, undo/redo, and recovery,
but make the normal integration and release-test paths each one fail-closed
Python orchestration command. This reduces operator surface while keeping the
existing safety components independently testable.

## 4. Recommended normal flow

### 4.1 Offline pull-request gate

Keep the current offline gate conceptually unchanged:

```text
unit tests -> lint/static checks -> ci-doctor
```

It uses no Oracle, SQLcl connection, target credentials, or flow adapter.

### 4.2 Manual integration qualification

Replace the six-command workflow sequence with one explicitly write-capable
high-level command:

```text
team.py --env <materialized-profile> run-integration --out <report>
```

It derives the exact commit from `HEAD` and aliases from the validated config;
the operator does not repeat either value. Internally it performs, in order:

1. prepared-runner and target preflight;
2. idempotent application-controller and migration-metadata setup;
3. one live inventory capture used for canonical drift and frontier checks;
4. initial-frontier adoption of that same capture only when no history or
   observation exists;
5. recomputation and application of ordinary non-destructive pending migrations;
6. deployment of every configured application from the exact checkout;
7. post-write inventory/frontier validation and declared application checks; and
8. canonical persistent-target report output, including structured failure when
   enough identity is available.

If destructive pending work exists, it stops before payload execution and
returns the exact confirmation template as maintenance-required. The existing
`migrate --dry-run` and explicit `migrate --destructive-confirmation` path stays
available for that reviewed maintenance operation.

### 4.3 Protected release-test qualification

Keep release build and post-download archive verification separate, then use one
online test command:

```text
team.py --env <materialized-profile> run-release-test <release.tar> \
  --target targets/test.json --out <test-evidence.json>
```

The command derives source commit and application aliases from the verified
archive and validated bindings. It reads live migration history from the
metadata owner instead of accepting `TEAM_TEST_HISTORY_JSON`, plans and applies
the archive, deploys its applications, validates the final frontier and checks,
and emits the canonical unsigned test evidence. Planning is recomputed under
the migration mutex immediately before migration execution.

Signing remains a separate command because access to the protected private key
is a distinct privilege. Runbook generation remains separate because it is the
production-owner handoff and still requires independently supplied production
target history and the public trust key:

```text
verify downloaded archive
  -> run-release-test
  -> sign-test-evidence
  -> gen-runbook
```

The immediate `verify-release` directly after `build-release` may be removed;
the build function must self-verify its emitted archive, and the independent
post-download verification remains mandatory.

## 5. Interfaces retained

Do not collapse safety and recovery operations into the happy-path commands.
Retain:

- `migrate`, `undo-migration`, and `redo-migration`, including their shared
  destructive-confirmation document;
- `check-drift`, `export-history`, and explicit frontier adoption for diagnosis
  and controlled recovery;
- migration and application recovery commands;
- `verify-release`, `plan-release`, and `gen-runbook` as offline owner tools;
- the isolated `METADATA` and read-only `VERIFY` profiles; and
- production write refusal in every executable path.

Low-level commands remain the inspectable primitives. Workflows call the two
high-level commands so normal operators do not manually transport values that
the system can derive and verify.

## 6. Evidence simplification

Do not introduce a new evidence version solely to shorten field names. Remove
duplication only where semantics are already redundant:

- derive `source_commit` from the checkout or verified archive;
- derive aliases from config/archive and compare their binding sets;
- replace the external test-history input with a live metadata read;
- use the accepted observation `after` digest as `observation_digest`;
- retain one canonical apply/qualification result inside the high-level command
  instead of passing an apply-report file between workflow steps; and
- keep the detached signature rather than embedding it in the evidence.

The final signed evidence remains independently readable and contains enough
identity to verify the archive, test target, accepted frontier, history, checks,
and actual toolchain.

## 7. Failure and recovery rules

- Every write is preceded by target identity and production refusal.
- No automatic initial-frontier adoption is allowed when history exists.
- Drift or frontier mismatch stops before the next payload.
- Destructive work stops with a confirmation template; the orchestrator never
  creates or edits an affirmative confirmation.
- A known post-attempt failure records `FAILED`; an uncertain outcome records
  `UNKNOWN`; both retain the mutex.
- The high-level command prints the recovery token and evidence locations but
  never clears unresolved state automatically.
- Signing occurs only after complete protected test PASS evidence exists.
- No production credentials or production execution are added to CI.

## 8. Test and acceptance requirements

Mandatory regression tests cover each P1 failure before its fix. In addition:

- migration `PASS`, `FAIL`, empty, missing, malformed, duplicated, and noisy
  assertion output;
- exception cause-chain classification and commit-with-lost-result scenarios;
- fresh, matching, empty-legacy, and mismatched schema-set bootstrap;
- integration-role evidence rejected by signing and runbook generation;
- manifest/payload disagreement for every migration metadata field;
- prepared-runner preflight failure before the first write;
- missing flow adapter and missing materialized secret files;
- high-level integration execution order and destructive maintenance stop;
- high-level release-test live-history revalidation under the mutex;
- exact derivation of commit and aliases without duplicate CLI inputs; and
- preservation of all production, recovery, and unobservable-state boundaries.

The complete offline suite, Ruff, ShellCheck, PowerShell parsing, JSON parsing,
and `git diff --check` must pass. A real protected non-production SQLcl/APEX run
is required before declaring the online workflows operational.

## 9. Boundaries

This design does not add automatic production deployment, generated rollback
SQL, automatic destructive approval, or a claim of fresh-install qualification.
Persistent integration/test evidence remains observational. Uncaptured or
transient Builder edits and arbitrary DML outside the supported inventory remain
unobservable.
