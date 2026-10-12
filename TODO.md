# Project TODO

Keep project work and inherited-template feedback here. Follow
[the template feedback guide](docs/template-feedback.md) for confirmed issues
in inherited template files. This file belongs to the project and survives
template upgrades.

## Open items

<!-- Add one record per item. Keep evidence sanitized and useful to reproduce. -->

- [ ] **Synonym target capture counts unrelated index rows**
  - Feedback ID: TF-001
  - Component: `scripts/schema_catalog.sql`
  - Kind: inherited-template issue
  - Local fix/workaround: fix merged to `main` at `df22dbb`; native qualification of this new mixed-index case remains pending
  - Upstream report: existing issue remains open pending native qualification and a sanitized status update
  - Upstream URL: https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/issues/19
  - Template release/ref/commit: 26.2 / `main` / `df22dbb`
  - Sanitized reproduction/evidence: Same owner/name TABLE plus unrelated INDEX namespace row was treated as ambiguous and blocked a direct-SELECT-granted CREATE VIEW prerequisite; raw-query/parser/preflight regression is in `tests/test_migration_checks.py::MigrationChecksTests.test_privileged_synonym_adapter_resolves_namespace_collisions_conservatively`, with parser-field preservation covered by `tests/test_schema_catalog.py::SchemaCatalogTests.test_snapshot_preserves_verified_synonym_target_existence_and_type`.
  - History: 2026-10-11: confirmed in final review; sanitized upstream issue #19 opened; local SQL correction and regression added and passed offline checks. The new mixed-index case had not had separate native Oracle qualification.
  - History: 2026-10-12: local fix is on `main` at `df22dbb`; the issue remains open and native mixed-index qualification is still pending.

- [ ] **Read-only qualification wrapper drops SQLcl runner options**
  - Feedback ID: TF-002
  - Component: `tools/qualify_template_readonly.py`
  - Kind: inherited-template issue
  - Local fix/workaround: fixed locally on `codex/remaining-work-implementation`; pending upstream integration
  - Upstream report: reported
  - Upstream URL: https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/issues/21
  - Template release/ref/commit: 26.2 / `main` / `df22dbb`
  - Sanitized reproduction/evidence: the read-only qualification wrapper passed its guard to catalog and lifecycle adapters without accepting their `phase` keyword, raising `TypeError` before those adapter calls. The wrapper now forwards supported runner options. The catalog read completed after the local fix; the DEMO2 lifecycle read remains unavailable for its separate app-ownership reason.
  - History: 2026-10-12: reproduced in the read-only local qualification, checked known limitations and searched upstream issues/PRs; no matching report found. Opened sanitized upstream issue #21. Local fix forwards runner options; catalog read passed after the fix. DEMO2 lifecycle read remains unavailable because its existing app 100 belongs to DEMO. No APEX writes were involved.
