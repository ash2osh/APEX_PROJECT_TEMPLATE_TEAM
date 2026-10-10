# Phase 3 result

## Files changed

- Added `scripts/rollout.py` and `scripts/rollout.sh`, then wired `rollout` into
  `scripts/team.sh` and `scripts/team.ps1`.
- Updated `scripts/migrate.py`, `scripts/migration_manifest.py`, and
  `scripts/verify_checks.py` so rollout can bind the migration and verification
  runners to the input hashes displayed in its plan.
- Updated `scripts/deploy.sh` and `scripts/publish_app.sh` to let rollout deploy
  a private copy of the hashed app source through the existing deployment path.
- Added `docs/rollout-manifest.example.json`; documented the command in
  `README.md` and the manifest, receipts, confirmations, reports and recovery
  rules in `docs/migration-rules.md`.
- Added the DEV26 SQLcl line handling, five-minute step, and cross-patch APEX
  byte comparison notes to `docs/known-limitations.md`.
- Registered the example manifest in `template-manifest.json`. Added
  `tests/test_rollout.py` and extended documentation, template-manifest, and
  Windows wrapper tests.
- Added this result file.

## Decisions

- The manifest is strict UTF-8 JSON with duplicate-key and unknown-field
  rejection. Ordered steps support `migrate`, `verify`, `sql-script`,
  `ords-import`, `app-deploy`, and `pause`.
- The runner hashes the manifest and all referenced files before execution and
  rechecks them at step boundaries. SQL and ORDS payloads are checked as read;
  migration and verification compare their loaded inputs to the plan hashes;
  app deployment uses a private, hash-checked source snapshot.
- Staging and production require one manifest-wide confirmation that displays
  every step/input digest and target details. The approval is held in-process
  and forwarded to existing guarded deployment paths. I omitted the plan's
  optional `--yes-for-all` because it would bypass that confirmation. No
  `--force` path was added.
- Standalone SQL scripts require the saved connection alias to equal
  `expectedUser`; the generated driver checks session user/current schema and
  live database/service identity before executing payload SQL. It rejects
  SQLcl client commands, connection changes, current-schema changes, and
  reserved rollout markers. ORDS imports use the same guarded driver after
  export validation and optional module filtering.
- Each successful step writes a local receipt. `--from-step N` verifies that
  all earlier receipts match the manifest, environment, step digest, input
  hashes, and evidence paths. Failure stops the rollout and retains its
  evidence.
- JSON and Markdown reports include step timing, status, hashes, messages, and
  evidence paths, including for failures and declined confirmations. Report
  outputs cannot overlap inputs, rollout receipts, or command recovery trees.
- APEXlang deploy verification remains exact-byte comparison after its
  documented source projections. The patch-level differences are documented;
  no version-aware comparison was implemented.

## Verification

- Focused rollout tests: **24 passed**.
- Filtered full suite: **1,280 tests run; 1,156 passed, 124 skipped, 0 failed,
  0 errored**. The 24 socket-bound Chrome daemon tests were excluded by test ID;
  `/snap/bin` was removed from `PATH` so sandbox-blocked PowerShell checks skip.
- Documentation contract tests: **18 passed**; template-manifest tests:
  **10 passed**. Windows support group: **35 run, 19 passed, 16 skipped**
  because PowerShell/Git Bash are unavailable or blocked in this environment.
- `python3 -m py_compile` passed for the changed Python runners/tests;
  `bash -n` passed for the team, rollout, deploy, and publish shell scripts;
  `git diff --check` passed. `scripts/team.sh --help` and
  `scripts/rollout.sh --help` show the rollout options.
- A read-only code review found no remaining findings after fixes for SQLcl
  failure details, verifier failure summaries, and APEXlang upgrade evidence
  protection.

## Not verified offline

- No database, network, real SQLcl, real ORDS import, or APEX import was used.
  The script driver, SQL failure reporting, and identity markers were exercised
  with `tests/fake_sqlcl.py`; live database/service identity checks and Oracle
  execution behavior still need controlled qualification.
- Windows PowerShell/Git Bash behavior was not executed on Windows. The
  PowerShell launcher available here is blocked by the sandbox's Snap policy,
  so those checks skipped.
- The target-patch serialization differences in APEXlang exports were not
  reproduced against Oracle APEX. The existing publish verifier still fails
  closed on byte differences.

## Version-aware APEX byte comparison proposal (not implemented)

`verify_publish_state.py::verify_source_bytes` compares file sets and then
compares bytes. `source_projection` removes only the qualified application-name
line in `application.apx`; `remap_subscription_source` applies only the
deployment's qualified subscription mapping. The verifier does not currently
key comparison behavior to the source and target APEX patch levels.

A future opt-in could record source and target APEX versions during the
existing deployment observations and use a version-pair profile only for
serializer differences reproduced in import/re-export qualification. A profile
could describe narrowly scoped projections for trailing blank lines, known
generated comments, or message ordering, while preserving the raw source and
re-export for review. Same-patch and unknown-pair comparisons should remain
byte-exact; unknown versions, file-set differences, or unqualified differences
should fail closed. Qualify each profile with real exports before enabling it.

## Issues found

| Class | Severity | What | Evidence | Proposed action |
| --- | --- | --- | --- | --- |
| Test environment | Low | The sandbox denies the Unix/TCP socket operations required by 24 Chrome daemon tests. | Those 24 cases were excluded by ID; the remaining 1,280 tests passed. | Run those tests in a socket-enabled environment. |
| Test environment | Low | PowerShell wrapper tests cannot execute the blocked Snap launcher here. | 16 checks in `test_windows_support.py` skipped; the available `pwsh` resolves under `/snap/bin`. | Run Windows wrapper checks on Windows or with an unblocked PowerShell/Git Bash installation. |
| Live qualification | Medium | The new SQL identity guard and existing ORDS/APEX deployment paths have not been exercised against a live target. | The task prohibited database/network access and real SQLcl; tests used deterministic fakes. | Qualify with a reviewed staging manifest and the team's normal coordination and recovery procedures before production use. |

No implementation issues remain from the read-only review.
