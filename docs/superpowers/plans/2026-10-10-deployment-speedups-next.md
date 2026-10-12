# Deployment speedups: historical status and remaining work

## Current status — 2026-10-12

The phase 1–10 delivery and the final review fixes were merged into `main` at
`df22dbbfc5a53bac6dc28f2b4f4910fec6604c6e`. This file preserves the prior
2026-10-11 handoff and its evidence as history. Its seven-item follow-up list is
superseded by the [consolidated remaining-work plan](2026-10-12-remaining-work-and-pr20.md),
which also tracks PR #20 and the open native qualification for TF-001.

## Historical snapshot — 2026-10-11

The original phases 1–8 implementation was merged in PR #18 before the current
branch. This checkout is based on `9aed8a2` on
`codex/deployment-review-and-completion`; it contains uncommitted review fixes,
phase 9/10 delivery, and the approved upstream-feedback instructions. See the
[2026-10-11 review](../reviews/2026-10-11-deployment-speedups-review.md) for
phase verdicts, native evidence, and qualification limits. Detailed task
reports and independent reviews are under
`.superpowers/sdd/2026-10-10-deployment-speedups/`.

## Delivered in this checkout

- **Phases 1–4 review repairs:** strict rollout SQL validation, configured and
  observed target binding for receipts/resume, cancellation of parallel check
  workers, rollback proof against saved folder-start values, and protection for
  retained recovery reports. Native disposable-DML rollout/resume and rehearsal
  passed; this is not APEX app-import evidence.
- **Phases 5–8 review repairs:** owner-safe catalog and comparison adapters,
  typed external synonym checks, native ORDS 26.3 source capture with bounded
  CLOB and paging evidence, exact source checks for empty lines, and explicit
  refusal for unsupported constraint deltas. Only the documented same-name
  CHECK-condition replacement is generated.
- **Phase 9:** bounded `team.sh query` and PowerShell dispatch using a named
  configured SQLcl profile, with literal-aware checks, output caps, identity
  framing, and commit guards. Its limits are documented; lexical validation
  cannot prove all database object behavior pure.
- **Phase 10:** production runbook, current limitations/troubleshooting,
  release qualification notes, and agent guidance. Version-aware byte
  acceptance remains a proposal; strict exact-byte comparison applies to
  unqualified source/version pairs.
- **Task 13:** sanitized upstream feedback workflow, `docs/template-feedback.md`,
  and project-owned `TODO.md` scaffold. The upgrade test proves existing local
  feedback bytes survive an upstream scaffold change. No upstream issue or PR
  was opened during verification.

All deliverables in this branch remain uncommitted. Do not apply them to a
downstream project until the ordinary template review/merge and a separate
authorized `upgrade-template` operation.

## Qualification and open gates

The scoped native Docker qualification used Oracle Database `23.26.3.0.0`,
APEX `26.2.0`, and SQLcl `26.3.0.0`; its authoritative summary and private
artifacts are in ignored `scratch/deployment-review-live-20261011/`. It
qualified query guards, owner catalog/compare/source/ORDS capture, DML rehearsal,
committed rollout, receipt-based resume, separate post-verify, and disposable
fixture cleanup. The receipt's configured binding and observed identity tuple
are target evidence, not a globally unique physical-database/clone identity.
The comparison profiles intentionally pointed to the same schema.

Still unknown: native Windows behavior, APEX 26.1 runtime/backport, other
database/ORDS release pairs, shared-app import/runtime, promotion to a distinct
target, and tolerant APEX patch-pair profiles. Do not report them as passed.
An approved future idea also does not count as delivered functionality.

The follow-up design and plan are drafts only:
[validity and design](../specs/2026-10-11-deployment-followup-ideas-design.md)
and [implementation plan](2026-10-11-deployment-followup-ideas.md). They include
the corrected retained-checkout limit for attempt events, immutable linked
event transitions, export-bound source-release provenance, and folder/batch
rehearsal metric scope. No follow-up idea implementation is part of this
delivery. Automatic automation restoration remains rejected; custom function
purity allow-listing remains deferred.

## Verification record

Task 9's pre-review full run reported 1,446 tests: 1,313 passed and 133
skipped. Task 12 reported 1,472 tests: 1,341 passed and 131 skipped. Task 11's
earlier full suite exited successfully but did not preserve a readable count.
The final cumulative offline suite, Ruff, documentation links, wrappers, and
manifest checks are recorded in
`.superpowers/sdd/2026-10-10-deployment-speedups/task-10-report.md`.

Do not claim socket tests cannot run in a Codex sandbox as a universal rule;
socket availability depends on the actual environment. The final suite here
uses a durable log and removes `/snap/bin` from `PATH` to avoid PowerShell
wrapper hangs observed in prior runs.

## Next steps

1. Review the current uncommitted change set using the phase report above.
2. Use the template repository's normal review and integration process when
   separately authorized; no commit, push, merge, or downstream upgrade is
   implied by this handoff.
3. Qualify the unknown platform/version/application gates only on their exact
   authorized targets and record independent evidence before changing any
   acceptance boundary.
