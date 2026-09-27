---
name: optimizing-apex-task-inboxes
description: Use when Oracle APEX task inboxes are slow from repeated task API/view calls or task-count/action lookups and a performance fix is being evaluated.
---

# Optimize slow APEX task inboxes

Keep APEX task and workflow state authoritative. Treat any application cache as a derived read model that can be checked, rebuilt, and discarded.

## Diagnose before designing

- Measure page, region, AJAX, and SQL timings; count calls to task APIs and views. Separate repeated work from one expensive query.
- Record realistic task counts, state distribution, participant counts, query plans, and concurrent users. Do not transfer benchmark numbers from another app or APEX release.
- Inspect which task states, potential owners, actions, and workflow parameters each component needs. Establish the target APEX session/workspace context before using metadata views.

**REQUIRED BACKGROUND:** Use `apex-session-context` for task or workflow metadata lookups outside a normal page request.

## Decide whether a read model is justified

Prefer one well-scoped query and normal indexes when those meet the response target. Add an app-owned cache only when measurements show that repeated APEX task resolution or joins are the bottleneck and the app can maintain acceptable freshness.

If a cache is justified:

- Store only fields needed by the inbox. Key rows by task ID and retain the application/workflow identifiers, state, and refresh version or timestamp needed for validation.
- Represent owner membership in an indexed relation or another safely searchable structure. Avoid delimiter-based usernames and substring matching for authorization filters.
- Refresh through application-owned task lifecycle code or a supported event/outbox path. Do not add triggers to Oracle-owned APEX tables or assume every task transition passes through one page.
- Make refreshes idempotent, define transaction ownership, log failures without unnecessary personal data, and provide retry plus full reconciliation/backfill.
- Filter every read by the intended application, current user, and allowed task state. If owner or permission data can lag, confirm visibility against an authoritative source before returning sensitive task details, or guarantee invalidation before stale rows can be served. Cached membership must not be the sole authority for visibility or actions; revalidate visibility and current task state at the read and action boundaries.
- Treat client-side preloaded counts or action metadata as display hints only; the server must authorize each operation.

## Verify the result

Compare cached counts, states, and owner membership with the authoritative APEX task source for representative users and transitions. Exercise duplicate refresh events, retries, stale rows, reassignment, completion, and cache rebuilds. Load-test with production-like cardinality before promotion, and report runtime or freshness checks as unknown when they were not run.

Follow this repository's migration and deployment rules for any schema change. Do not apply migrations or import an app unless requested.

## Evidence boundary

This pattern was observed in one source APEX application where multiple task regions repeated workflow lookups. It is a profiling-led option, not a guarantee that APEX task views are slow or that caching is appropriate for every application.
