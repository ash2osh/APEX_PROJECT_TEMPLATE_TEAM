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
  - Local fix/workaround: fixed in this branch; native qualification of this new mixed-index case remains unverified
  - Upstream report: reported
  - Upstream URL: https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/issues/19
  - Template release/ref/commit: 26.2 / `codex/deployment-review-and-completion` / `9aed8a2`
  - Sanitized reproduction/evidence: Same owner/name TABLE plus unrelated INDEX namespace row was treated as ambiguous and blocked a direct-SELECT-granted CREATE VIEW prerequisite; raw-query/parser/preflight regression is in `tests/test_migration_checks.py::MigrationChecksTests.test_privileged_synonym_adapter_resolves_namespace_collisions_conservatively`, with parser-field preservation covered by `tests/test_schema_catalog.py::SchemaCatalogTests.test_snapshot_preserves_verified_synonym_target_existence_and_type`.
  - History: 2026-10-11: confirmed in final review; sanitized upstream issue #19 opened; local SQL correction and regression added and pass offline checks. The new mixed-index case has not had separate native Oracle qualification.
