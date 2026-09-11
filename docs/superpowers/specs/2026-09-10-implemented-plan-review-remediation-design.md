# Implemented plan review remediation — Design

**Date:** 2026-09-10
**Status:** Draft
**Reviews:** implementation of
[`2026-09-10-repo-review-p1-remediation-and-flow-simplification.md`](../plans/2026-09-10-repo-review-p1-remediation-and-flow-simplification.md)
**Related design:**
[`2026-09-10-repo-review-p1-remediation-and-flow-simplification-design.md`](2026-09-10-repo-review-p1-remediation-and-flow-simplification-design.md)

## 1. Purpose

The implemented plan substantially simplifies the protected integration and
release-test paths, but post-implementation review found one priority-one
runtime defect, three priority-two safety or operability gaps, and one
unfinished protected-environment acceptance step.

This design records the required remediation. It is intentionally narrow: fix
the reviewed paths, add regressions at their real boundaries, and complete the
existing acceptance gate. It does not reopen the broader migration or release
architecture.

## 2. Safety boundaries

The remediation must preserve these existing contracts:

- production writes remain refused;
- protected integration and release-test execution remains limited to prepared
  non-production targets;
- migration drift, target identity, mutex, attempt, verification,
  observation-chain, destructive-confirmation, and recovery gates remain
  fail-closed;
- unknown database outcomes retain their mutex until evidence-driven recovery;
- only protected test PASS evidence may be signed or used for runbook
  generation; and
- no command commits or pushes automatically.

Uncaptured or transient Builder edits and arbitrary DML outside the supported
inventory remain outside what the tooling can observe.

## 3. Required remediations

### 3.1 P1 — Repair the release-test apply-report handoff

#### Finding

`run_release_test` converts the returned `ApplyReport` into a dictionary and
passes that mapping to `qualify_target`. The qualification path then calls
`Path(apply_report)`, although `Path` accepts a filesystem path rather than a
mapping. A successful live release application can therefore be followed by a
`TypeError` before qualification evidence is produced:

```text
TypeError: argument should be a str or an os.PathLike object where __fspath__
returns a str, not 'dict'
```

This is priority one because the failure occurs after the live apply and blocks
qualification, signing, and production-runbook generation.

#### Required behavior

- Define one explicit apply-report input contract for qualification.
- The high-level release-test path must pass the already validated in-memory
  report without serializing it merely to satisfy a path-only interface.
- The low-level `qualify-target --apply-report <path>` command must continue to
  accept a JSON file for diagnostic and maintenance use.
- Both representations must enter the same validation logic and enforce source
  commit, archive digest, target state key, status, and report-shape checks.
- Invalid report types or contents must fail before candidate checks and must
  produce a controlled qualification or workflow error rather than a Python
  traceback.

#### Regression coverage

- Exercise `run_release_test` through its default dependency seam with an
  `ApplyReport` returned by the live adapter and prove that qualification
  receives and validates it.
- Retain coverage for the path-based low-level qualification command.
- Cover malformed mappings and unreadable or malformed JSON files.

### 3.2 P2 — Surface retained migration recovery context

#### Finding

After a migration attempt starts, deterministic and unknown failures correctly
retain the migration mutex for recovery. However, several failure paths expose
only the underlying message. In particular, an existing `MigrationRunError` is
re-raised unchanged, so the operator may receive no run token, attempt ID,
phase, or evidence location.

The recovery command requires a positional `run_token` and an evidence file.
If the failure output omits that context, the operator cannot reliably invoke
the documented recovery path. `export-history` does not provide a substitute
because its normal output does not expose the held mutex and complete attempt
context.

#### Required behavior

- Every high-level migration failure that retains a mutex must expose a stable,
  structured recovery context containing:
  - run token;
  - current attempt ID, when an attempt exists;
  - migration ID and operation;
  - failure phase and result classification (`FAILED` or `UNKNOWN`);
  - retained-mutex status; and
  - relevant local evidence or SQLcl log path when available.
- The CLI must render this context without losing the original diagnostic.
- No secret, credential, wallet content, or database connection string may be
  included.
- Failures before the first attempt that safely release the mutex must not claim
  that recovery is required.
- Recovery remains explicit and evidence-driven; surfacing the token must not
  weaken ownership, worker-termination, or observed-state checks.

#### Regression coverage

- Cover deterministic payload, verification, observation, inventory, and event
  failures after attempt creation.
- Cover unknown payload, event acknowledgement, attempt-state update, and mutex
  release outcomes.
- Assert that retained-mutex failures expose the correct recovery fields and
  that pre-attempt failures do not.
- Add a CLI-level test proving an operator can obtain the token needed by
  `recover-migration` from the emitted failure context.

### 3.3 P2 — Translate online preflight failures at the workflow boundary

#### Finding

`preflight_online` raises `RuntimeError` for missing or invalid runtime
dependencies. The low-level `qualify-target` command translates that error to a
normal CLI refusal, but `run_integration` and `run_release_test` call their
preflight dependency directly. `team.py` does not catch built-in
`RuntimeError`, so a missing SQLcl executable, flow adapter, runner contract, or
runtime mismatch can escape as a traceback with exit status 1.

#### Required behavior

- `run_integration` and `run_release_test` must translate online-preflight
  failures to `OnlineWorkflowError` at their orchestration boundary.
- Preserve the actionable preflight message and its exception cause.
- The CLI must emit the normal concise refusal and exit through the established
  online-workflow failure status.
- Preflight must remain before controller setup, metadata bootstrap, payload
  execution, or any other database write.
- Unexpected programming defects must not be indiscriminately converted into
  operational refusals. Translation should cover the declared preflight error
  contract, not every exception from the workflow.

#### Regression coverage

- Cover missing SQLcl, missing flow adapter, incomplete profile, target identity
  mismatch, and unsupported runtime versions through both high-level commands.
- Assert that no write dependency is called after a failed preflight.
- Add CLI tests that reject these cases without a traceback.

### 3.4 P2 — Make the reference metadata SQL behaviorally truthful

#### Finding

`scripts/sql/migration_metadata.sql` describes the version-2 identity contract
in comments but contains only table and index DDL. It does not establish or
validate the project row, perform the allowed empty version-1 adoption, or
refuse a non-empty schema-set mismatch before writes as the embedded bootstrap
does.

The current synchronization test checks selected comments and DDL tokens. It
therefore permits the executable bootstrap and the documented reference SQL to
have different identity behavior.

#### Required behavior

- Choose and document one authoritative bootstrap contract.
- The checked-in reference SQL must either be executable with the same inputs
  and identity decisions as the runtime bootstrap, or be generated from the
  same canonical source. A comment claiming equivalent behavior is
  insufficient.
- Fresh bootstrap must record the supplied project ID and lowercase
  schema-set SHA-256 at metadata version 2.
- A version-1 row may fill only an empty legacy digest.
- A non-empty digest mismatch and a project-identity mismatch must refuse before
  any metadata mutation.
- Normal bootstrap must never overwrite an established version-2 identity.
- Avoid maintaining two independent SQL implementations of the same decision
  tree.

#### Regression coverage

- Replace token-only synchronization assertions with tests of the generated or
  shared source relationship.
- Exercise fresh creation, idempotent matching bootstrap, empty version-1
  adoption, non-empty version-1 mismatch, version-2 digest mismatch, and
  project mismatch.
- Prove refusal happens before any metadata write in every mismatch case.

## 4. Protected acceptance remains mandatory

Task 10, step 5 of the reviewed implementation plan remains incomplete. Unit
and static checks cannot replace acceptance on prepared non-production
targets.

After sections 3.1–3.4 are implemented and the offline suite passes:

1. Run `run-integration` on the protected integration runner with one non-empty
   PASS migration verification and one declared application flow check. Require
   PASS evidence containing the observed toolchain digest and latest accepted
   after-inventory digest.
2. On an isolated test target, run `run-release-test` with a disposable review
   migration whose verification returns `FAIL`. Require exit 3, no `APPLIED`
   event for the failing migration, a `FAILED` attempt, a surfaced retained
   recovery token, and no signable PASS evidence.
3. Recover only after the target owner reviews the observed state and supplies
   the required worker-termination evidence.

The plan checkbox may be marked complete only after both protected runs have
produced and retained their expected evidence. A queued, skipped, cancelled, or
runner-unavailable workflow does not satisfy this gate.

## 5. Implementation order

1. Fix and regression-test the P1 apply-report contract before another
   release-test apply is attempted.
2. Add the recovery-context model and CLI rendering.
3. Translate the high-level preflight errors.
4. Canonicalize the metadata bootstrap SQL and strengthen its tests.
5. Run all offline verification, then perform the protected acceptance in
   section 4.

The four code remediations may be reviewed independently, but protected
acceptance must use a commit containing all four.

## 6. Completion criteria

This remediation is complete only when:

- all four findings have regression tests at the boundary where each defect was
  observed;
- the full Python suite and repository static checks pass;
- release-test qualification consumes the live adapter's apply report without
  a path-type failure;
- every retained migration mutex is accompanied by sufficient safe recovery
  context;
- high-level runtime-preflight refusals are concise and traceback-free;
- the reference metadata SQL cannot drift semantically from the runtime
  bootstrap contract;
- protected integration PASS acceptance succeeds;
- the isolated protected test FAIL scenario proves the retained-mutex recovery
  contract; and
- Task 10, step 5 is checked only after its evidence has been reviewed.

No production database or APEX application write is part of this acceptance.
