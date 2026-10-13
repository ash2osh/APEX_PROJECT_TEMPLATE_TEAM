# Project TODO

Keep project work and inherited-template feedback here. Follow
[the template feedback guide](docs/template-feedback.md) for confirmed issues
in inherited template files. This file belongs to the project and survives
template upgrades.

## Tracked feedback

<!-- Add one record per item. Keep evidence sanitized and useful to reproduce. -->

- [x] **Synonym target capture counts unrelated index rows**
  - Feedback ID: TF-001
  - Component: `scripts/schema_catalog.sql`
  - Kind: inherited-template issue
  - Local fix/workaround: fix merged to `main` at `df22dbb`; native mixed-index qualification passed on the disposable Docker target
  - Upstream report: issue #19 closed as completed after adding sanitized native qualification evidence
  - Upstream URL: https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/issues/19
  - Template release/ref/commit: 26.2 / `main` / `df22dbb`
  - Sanitized reproduction/evidence: Same owner/name TABLE plus unrelated INDEX namespace row was treated as ambiguous and blocked a direct-SELECT-granted CREATE VIEW prerequisite; raw-query/parser/preflight regression is in `tests/test_migration_checks.py::MigrationChecksTests.test_privileged_synonym_adapter_resolves_namespace_collisions_conservatively`, with parser-field preservation covered by `tests/test_schema_catalog.py::SchemaCatalogTests.test_snapshot_preserves_verified_synonym_target_existence_and_type`.
  - Native evidence: `scratch/synonym-namespace-qualification-20261012-r6/qualification.json`; TABLE/INDEX and MV/INDEX dictionary rows were captured through ordinary and privileged routes, valid TABLE/MV preflights passed, sequence/absent targets refused, and independent cleanup found zero fixture rows.
  - History: 2026-10-11: confirmed in final review; sanitized upstream issue #19 opened; local SQL correction and regression added and passed offline checks.
  - History: 2026-10-12: native owner/privileged qualification passed on Oracle 23.26.3.0.0 with SQLcl 26.3.0.0 and APEX 26.2.0; sanitized results were added to [issue #19](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/issues/19#issuecomment-6118260901), then the issue was closed as completed.

- [x] **Read-only qualification wrapper drops SQLcl runner options**
  - Feedback ID: TF-002
  - Component: `tools/qualify_template_readonly.py`
  - Kind: inherited-template issue
  - Local fix/workaround: fixed at `861b499` and integrated into `main` by PR #22
  - Upstream report: issue #21 closed as integrated by PR #22
  - Upstream URL: https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/issues/21
  - Template release/ref/commit: 26.2 / `main` / `861b499` (integrated by PR #22)
  - Sanitized reproduction/evidence: the read-only qualification wrapper passed its guard to catalog and lifecycle adapters without accepting their `phase` keyword, raising `TypeError` before those adapter calls. The wrapper now forwards supported runner options. The local Docker DEMO read completed; app 100 had no automation, workflow, or task rows, so positive lifecycle runtime coverage remains unavailable.
  - History: 2026-10-12: reproduced in the read-only local qualification, checked known limitations and searched upstream issues/PRs; no matching report found. Opened sanitized upstream issue #21. Local fix forwards runner options; catalog read passed after the fix. DEMO2 lifecycle read remains unavailable because its existing app 100 belongs to DEMO. No APEX writes were involved.
  - History: 2026-10-12: added a sanitized branch-status update to [issue #21](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/issues/21#issuecomment-6118264020); issue remains open pending integration.
  - History: 2026-10-12: reran the read-only wrapper against the correctly scoped local Docker DEMO target. SQLcl 26.3.0.0, Oracle 23.26.3.0.0 and APEX 26.2.0 identity checks passed; the owner catalog returned 81 objects and the app 100 lifecycle read passed. Automation, workflow and task arrays were empty, so positive runtime coverage remains unavailable. No APEX writes occurred. Local report: `scratch/template-readonly-qualification-20261012-pr22/qualification.json`.
  - History: 2026-10-12: integrated the fix through PR #22; issue #21 closes with the merge.
