# Staging qualification and migration undo/redo — Design index

**Date:** 2026-09-10
**Status:** Draft, split after review
**Supersedes:** only the disposable CI replay/provisioner portions of
`docs/superpowers/plans/2026-09-09-review-remediation.md` and
`docs/superpowers/plans/2026-09-10-guard-and-workflow-remediation.md`
**Parent:** [Team design](2026-09-06-team-template-design.md)

This file is the short decision record for two independent implementation
specifications:

1. [Persistent staging qualification](2026-09-10-staging-qualification-design.md)
2. [Migration undo, redo, and destructive confirmation](2026-09-10-migration-undo-redo-design.md)

Repository-wide review and the proposed slimmer operator flow are recorded in
[Repository review P1 remediation and flow simplification](2026-09-10-repo-review-p1-remediation-and-flow-simplification-design.md).

They are split because qualification/promotion and migration lifecycle have
different state, failure modes, and test surfaces. They may be planned and
implemented separately.

## Decisions

- Delete the non-working disposable Oracle/ORDS replay workflow.
- Keep default CI offline and configuration-free.
- Make real staging qualification manual and optional.
- Preserve candidate-application checks on persistent staging and release-test
  targets.
- Replace replay evidence with one signed persistent-target evidence format.
- Add optional authored down migrations with direction-specific verification.
- Record migration lifecycle as append-only `up` and `down` events.
- Permit only global-LIFO undo and dependency-safe explicit redo.
- Introduce one exact destructive-confirmation file contract shared by forward
  migrate, undo, and redo.

## Guarantees retained

- Production writes remain refused; production application remains an
  owner-operated runbook procedure.
- Migration bundles remain immutable and checksum-bound.
- Drift, target identity, mutex, attempt, verification, observation-chain, and
  unknown-result recovery gates remain fail-closed.
- Candidate checks and signed promotion evidence remain required where the
  configured release workflow uses them.

## Guarantees intentionally removed

- The shipped workflow no longer proves a fresh installation or upgrade on an
  empty disposable database.
- Persistent staging is evidence about one observed shared target, not proof
  that the selected commit alone created its complete schema.

## Boundaries

- Down SQL is authored and reviewed; it is never generated automatically.
- Undo/redo are non-production database operations only.
- Database undo does not roll back an APEX application import. Application
  recovery redeploys reviewed earlier source through the existing coordinated
  non-production path.
- Uncaptured Builder changes and arbitrary DML outside the supported inventory
  remain unobservable.

The two child specifications are the implementation authority. If this index
and a child differ, the relevant child specification wins.
