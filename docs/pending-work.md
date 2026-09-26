# Current status and remaining work

Last updated: 2026-09-26. This page tracks open live-acceptance and owner
actions; completed implementation plans and superseded design drafts have been
removed. The current release decisions are recorded in
[`2026-09-24-dev-database-release-source-design.md`](superpowers/specs/2026-09-24-dev-database-release-source-design.md).

## Current baseline

- Canonical `main` is at `8bc591b`; PR #13 was merged. There is no remaining
  PR approval action for that evidence-identity fix.
- The post-merge offline gate passed: 734 unit tests, Ruff, shell syntax,
  `ci-doctor`, JSON/CRLF checks, and `git diff --check`.
- On 2026-09-25, Docker schema release cut/verification and format-3 replay
  passed for empty and earlier-release histories. Signed evidence verified
  and generated offline runbooks included the down transition. This proves
  the schema path only; those reports contain zero APEX app checks.
- The evidence workspace `LT_RELEASE_TEST` was last inspected on 2026-09-25:
  it had no application and mapped to `LT_DATA`, which had been dropped during
  test cleanup. The local scratch `.env` also referenced a retired SQLcl
  alias. Requalify all of these before any further database action.
- Detailed retained evidence is under
  `scratch/live-test-2026-09-25/` and the source-volume snapshot under
  `.sync-state/live-test-2026-09-25/`. The reboot observation is in
  `scratch/live-test-2026-09-25/restart-continuation/restart-check/`; the
  available boot journal did not establish a cause.

## Remaining work

| Item | Status | Next step |
|---|---|---|
| Live APEX app release and deploy | **UNKNOWN / blocked by missing fixtures.** No throwaway source app, isolated target app, or matching target contract was supplied; no app payload was attempted. | Provision and verify a throwaway workspace/schema and app contract, then follow [the live test plan](live-test-plan.md). |
| METADATA backup/restore | **Not run.** | Rehearse only on an isolated copy after the owner authorizes that restore exercise; retain before/after evidence. See [metadata backup and restore](metadata-backup-restore.md). |
| Integration workflow | **UNKNOWN.** The last repository audit (2026-09-25) found no registered self-hosted runner. | When a protected runner is available, dispatch the manual integration workflow and retain its result. |
| GitHub protections | **Owner action open.** The last audit (2026-09-25) found no `main` ruleset/branch protection and no integration environment reviewers. No settings were changed. | Recheck current settings, then configure the required reviewer policy and `main` rules in GitHub. |
| Production read-only privilege audit | **UNKNOWN.** No production-like read-only profile was supplied. No production write was attempted. | Supply an authorized read-only profile and run the documented identity/privilege check. |
| Three-developer runtime/browser acceptance | **UNKNOWN.** The workflow remains fail-closed without disposable shared APEX fixtures and a protected ORDS/browser runner. | Provision those fixtures and runner, then use [the three-developer E2E runbook](local-three-developer-e2e.md). |
| Legacy format-2 target migration | **Unsupported and fail-closed.** Format-3 planning requires a source-ledger prefix; a legacy target may not have replay markers. | If such a target exists, review and perform a one-time baseline adoption before its first format-3 release. |

## Safety boundaries

- Treat Docker schema replay as schema-path evidence only. Live APEX deployment,
  fresh-install isolation, cross-instance behavior, and browser runtime each
  need their own acceptance run.
- Keep unavailable environment checks as `UNKNOWN`; do not infer a pass from
  offline tests or from a schema-only release report.
- Production writes remain refused. Do not restore or modify the retained
  source-volume snapshot as part of routine qualification.

## Operator references

- [Live Oracle/APEX acceptance procedure](live-test-plan.md)
- [Three-developer shared-app acceptance run](local-three-developer-e2e.md)
- [METADATA backup and restore procedure](metadata-backup-restore.md)
- [Release and promotion workflow](promotion.md)
