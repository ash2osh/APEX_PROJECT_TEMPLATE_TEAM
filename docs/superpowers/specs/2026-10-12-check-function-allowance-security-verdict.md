# Check Function Allowance Security Verdict

Date: 2026-10-12. Verdict: **deferred**.

## Scope

The existing migration-check validator allows only its reviewed built-in SQL
function set. This record evaluates whether a project-owned stored function
allow-list should be added. It does not change `SAFE_FUNCTIONS`, check-query
acceptance, or rehearsal behavior.

## Evidence and decision

No concrete function, owner, edition, source digest, or project use case was
requested for this implementation. Without a named candidate there is no
auditable source or dependency graph to inspect. Package initialization,
definer/invoker rights, dynamic SQL, autonomous work, DDL, remote calls, edition
changes, and external effects therefore remain unevaluated. A claimed pure
function or an editable name list would not establish safety.

The candidate is deferred rather than approved or rejected. The current
built-in-only contract remains in force. No function source, credentials, live
data, or database effects were inspected or changed for this verdict.

## Reopen criteria

Revisit only for a concrete function request with its exact owner, name,
edition, source digest and dependencies. Any new design must account for source
and dependency invalidation, privileges, initialization and irreversible
effects. If independent purity cannot be established, retain the built-in-only
contract and reject that candidate.
