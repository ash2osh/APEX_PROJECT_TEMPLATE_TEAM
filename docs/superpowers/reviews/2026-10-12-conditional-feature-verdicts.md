# Conditional Feature Verdicts

Date: 2026-10-12. This is a local planning result; no acceptance policy was
relaxed and no conditional feature was enabled.

## Tolerant APEX source profiles — deferred

No authentic, reviewed import/re-export byte pair for a precise source and
target APEX release was available in this implementation slice. PR #20's
normalization proposal does not qualify an acceptance projection. There is no
explicit approval to turn a strict source-byte mismatch into success. Keep
exact-byte comparison and existing documented projections; revisit only with
private raw evidence, component-level negative cases, and an approved policy
proposal.

## Custom migration-check functions — deferred

No concrete stored-function use case was provided. There is no exact owner,
name, edition, source digest or dependency graph to audit, so purity and
irreversible side effects cannot be established. Keep the built-in-only check
function contract. The detailed verdict and reopen criteria are in
[`../specs/2026-10-12-check-function-allowance-security-verdict.md`](../specs/2026-10-12-check-function-allowance-security-verdict.md).

## Evidence boundary

These are scoped deferred decisions, not compatibility or security
qualifications. No Oracle write, application import, source normalization,
function allowance, or automated profile selection occurred.
