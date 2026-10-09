# Local Template Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` for inline execution, or `subagent-driven-development` only if the developer explicitly chooses delegation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the seven agreed template improvement priorities in three independently reviewable releases.

**Architecture:** Keep the existing wrappers, Python helpers and native Oracle safeguards. Ship maintenance/readiness first, then explicit migration targets and lifecycle verification, then catalog/tooling improvements. Shared interfaces are specified in the linked release plans.

**Tech Stack:** Python 3.10+, unittest, Bash, PowerShell, SQLcl, Oracle Database, release-matched APEXlang; optional isolated Graphify.

**Spec:** [Local improvements design](../specs/2026-10-09-local-template-improvements-design.md).

## Global Constraints

- APEX 26.2 operations require SQLcl 26.3.0.0 or newer; 26.1 stays on `codex/apex-26.1`.
- Keep LF APEXlang/SQL, numeric app IDs and explicit workspace/parsing-schema mappings.
- No custom database metadata tables, mutexes, rosters or migration ledgers.
- Weekly skills refresh uses SQLcl's native registry only; no extra date file or scheduled automation.
- UC-APX remains retired; working copies remain deferred.
- Planning does not authorize implementation or database qualification writes.
- No silent commits/pushes, team messages, imports, exports or migrations.

## Review Focus

- An old 26.1 project must not receive 26.2 behavior through an unpinned upgrade (Release A, Task 2).
- Offline checks must not start SQLcl or modify a project/registry (Release A, Task 3).
- A split-schema migration must use its selected identity without altering backups (Release B, Task 1).
- Verified source bytes must not conceal unavailable or changed runtime state (Release B, Tasks 2–3).
- Large Unicode payloads and customized downstream files must survive optimization/upgrades (Release C, Tasks 1–2).

---

## Release order

| Release | Scope | Plan | Completion gate |
| --- | --- | --- | --- |
| A | Reviewed fixes, safe 26.1 maintenance, local readiness | [Maintenance plan](2026-10-09-template-maintenance-readiness.md) | Offline suite, upgrade fixtures, reviewed branch-specific changes |
| B | Migration profiles and publish lifecycle verification | [Database operations plan](2026-10-09-template-database-operations.md) | Offline regressions and authorized disposable Docker qualification |
| C | Catalog scaling, isolated Graphify, cleanup and qualification tooling | [Performance/tooling plan](2026-10-09-template-performance-tooling.md) | Exact catalog equivalence, tooling isolation and qualification report |

## Execution checkpoints

- [x] Developer reviews the design and plans; confirm inline or delegated execution before implementation.
- [x] Implement/review Release A on a named `codex/` branch; preserve the existing twelve-path correction set.
- [ ] Integrate authorized changes through the normal template PR process; backport only qualified release-independent work to `codex/apex-26.1`.
- [x] Implement/review Release B after A's target/readiness interfaces exist.
- [x] Implement/review Release C independently after the maintenance corrections land.
- [x] Update release documentation with actual verification and remaining limitations.

Implementation and qualification results are recorded in
[the inline review](../reviews/2026-10-09-local-template-improvements.md).
The 26.1 maintenance patch is prepared and tested; branch integration, commits,
pushes and PR merging remain pending explicit authorization. Natrec is reference evidence only and is not an implementation target
for these plans. No commit, push, PR merge or live operation is part of writing
this plan.
