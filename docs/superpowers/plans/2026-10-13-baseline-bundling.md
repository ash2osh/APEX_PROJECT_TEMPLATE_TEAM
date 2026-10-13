# Baseline Bundling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. Execute branch changes sequentially in the existing checkout.

**Goal:** Offer the same optional generated baseline bundle on APEX 26.1 and 26.2, reducing three structure/grant/code folders to one per target schema while retaining a separate reference-data migration.

**Architecture:** A pure composition module combines eligible generated steps/checks. The baseline CLI opts into it using --bundle; the existing granular path remains the default. Port the database catalog/generator prerequisite to 26.1 independently, then apply the shared feature without importing APEX 26.2 app tooling.

**Tech Stack:** Python 3.10 standard library, unittest/fake SQLcl, guarded catalog SQL, Bash and PowerShell.

**Spec:** ../specs/2026-10-13-migration-planning-and-bundling-design.md, sections C and D.

**Prerequisite:** [2026-10-13-migration-plan-report.md](2026-10-13-migration-plan-report.md) Task 1 for durable 26.1 attempt evidence and Task 3 for the common SQLcl runtime guard.

## Global Constraints

- Python 3.10 or newer; standard library only for the daily workflow.
- New live migration-report and baseline workflows require SQLcl 26.3.0.0 or newer; offline reporting needs no SQLcl.
- Preserve APEX 26.1 application/source/deployment behavior on codex/apex-26.1 and APEX 26.2 behavior on main.
- One migration folder changes one schema; cross-schema work uses separate folders and an explicitly ordered release.
- Preserve existing folder naming, consecutive SQL numbering, checks.json schema 1, migration digests and verified-receipt formats. Existing schema-2 rehearsal metrics remain supported where already present; these features do not require schema 2.
- Once any database write may have begun, SQL/check bytes and migration identity remain immutable. Unknown evidence refuses mutation; successful receipts are never overwritten.
- Baseline generation reads live metadata and writes new local migration folders; it never applies generated SQL.
- Keep Bash and PowerShell public arguments, output schema and exit codes equivalent; preserve existing Windows fixes.
- Preserve project-owned files and existing attempted/applied migration folders; no automatic squashing, deleting, renaming or archiving.

All remaining spec constraints apply. Reference-data behavior is retained, not bundled with DDL/code. No real catalog session, rehearsal or apply is required during implementation.

## Review Focus

1. Copying database helpers from 26.2 must not pull APEX 26.2-only code or undo 26.1 Windows fixes: Tasks 1 and 2.
2. Bundling must retain every SQL byte and assertion, including duplicate-ID handling and later-boundary preconditions: Task 3.
3. Draft regeneration and attempted-folder evidence must not produce silent replacement or false idempotence: Task 4.
4. A second schema, data-only change or empty code component must not be folded into the wrong owner/bundle: Task 4.
5. Filesystem failure, occupied destination, symlink/reparse point or revision gap must leave old migrations and evidence intact: Task 4.

---

### Task 1: Port complete catalog support to the 26.1 branch

**Branch:** APEX 26.1 only. Main already contains this subsystem.

**Files:** create scripts/compare_env_catalog.sql, tests/test_compare_env.py, tests/test_catalog_transport.py and tests/fixtures/compare_env/{dev,staging}.json; modify scripts/compare_schema.py, scripts/schema_catalog.py, scripts/schema_catalog.sql, scripts/sqlcl_session.py and their existing tests. Port scripts/path_utils.py and required scripts/windows_job.py hunks only when the selected catalog/session code depends on them. Add docs/compare-env.md and its templateOwned entry with the CLI integration below.

**Interfaces:** preserve existing selected-object comparison APIs. Add the main branch COMPARE_ENV_SECTIONS and BASELINE_INTERNAL_SECTIONS definitions, compare_environment_catalogs and capture_environment_catalog(target, environment, sections, run_dir, *, dba_capture=False, baseline_capture=False, baseline_prefixes=(), baseline_excluded=(), baseline_data_tables=(), _runner=run_sqlcl). Keep current Target/resolve_target APIs, which were identical between the inspected branches. Reuse existing same_database_scope and inventory/snapshot APIs.

- [ ] Review the pinned 26.2 catalog/session diffs and their import closure. Catalogue only database/transport dependencies, with explicit exclusions for app normalization, app lifecycle, partial publish, source_evidence.py and APEX release adapters. Preserve the Windows behavior qualified at 26.1 tip 0bb72a3.
- [ ] Port failing catalog tests for complete paged metadata, gzip/base64 and legacy framing, row/byte caps, mismatched scope, unreadable dictionary sections, and precision-qualified timezone values. Assert incomplete capture exits 2 and cannot produce a ready comparison. Add same-database/different-schema positive and same-observed-scope negative cases.
- [ ] Run `PYTHONPATH=tests:. python3 -m unittest -v test_compare_env test_catalog_transport test_schema_catalog test_sqlcl_session`; new capture/API cases must fail against the old 26.1 code before porting implementation.
- [ ] Add read-only catalog/session support from the reviewed database-only source. Preserve identity guards, read-only transaction, commit-disable policy, rollback exit, safe diagnostics, 500-row paging and 100000-row section cap. Port the parser and SQL transport together, not one side of their protocol.
- [ ] Run the focused modules plus test_compare_schema, test_schema_normalization and test_windows_support. Recheck that imports contain no APEX 26.2-only dependency. Proposed future commit: `feat: add complete database catalogs to APEX 26.1`.

**Done:** the 26.1 branch can capture the baseline generator's complete metadata through fake-SQLcl-tested read-only paths. Live catalog coverage on an actual target remains a separate qualification.

### Task 2: Add the existing granular baseline workflow to 26.1

**Files:** create scripts/baseline.py, tests/test_baseline.py, tests/fixtures/baseline/**, docs/baseline.md and docs/baseline.example.json on 26.1; modify its scripts/team.sh, scripts/team.ps1, scripts/compare_schema.py CLI exposure, tests/test_team_cli.py, README.md and template-manifest.json. On both branches, add the new runtime guard to baseline live-entry paths.

**Interfaces:** preserve current main baseline commands export-source/export-grants/export-data/build/filter-ords, generated family names baseline-structure/baseline-grants/baseline-code/baseline-data, _build_migration_folder and _migration_bytes. The 26.1 rollout.exclude_ords_modules helper already exists; test/reuse it without porting unrelated app rollout code.

- [ ] Port the existing main baseline tests and fixtures as failing acceptance cases on 26.1. Pin the three-folder granular case, idempotent regeneration, immutable receipt/attempt evidence, stored compiler settings, exact source, natural-key reference data, foreign-key label mapping, timezone-preserving values, sensitive-column exclusions and bounded data steps.
- [ ] Run `PYTHONPATH=tests:. python3 -m unittest -v test_baseline`; verify the absent generator is the failure, while fake SQLcl remains mandatory.
- [ ] Port the generator with only Task 1 catalog dependencies and Task 1 of the report plan's attempt readers. Keep checks.json schema 1 and all exact source/compiler/data policies. Use require_migration_sqlcl_version before live export/build capture on both branches; filter-ords and parser help remain offline. Test the new runtime guard without changing the no-bundle generated byte contract.
- [ ] Expose baseline and compare-env in the 26.1 wrappers; apply the new runtime-version guard before the added compare-env catalog path as well as baseline capture. Add only the owned docs/config examples needed by these workflows; preserve manifest schemaVersion 1 and its existing qualification templateOnly entries. Do not copy main's release declarations, app descriptors or publish commands.
- [ ] Run test_baseline, test_compare_env, test_team_cli, test_template_manifest and test_rollout. Add test_26_1_database_tools_have_no_26_2_app_dependency to tests/test_baseline.py: import and exercise the generator with the excluded 26.2 app modules unavailable. Proposed future commit: `feat: add guarded baseline generation to APEX 26.1`.

**Done:** both branches have a working granular baseline before the common bundling feature is enabled. This is a prerequisite deliverable, not evidence that bundling itself is done.

### Task 3: Implement pure eligible-component composition

**Files:** create scripts/baseline_bundle.py and tests/test_baseline_bundle.py on both branches. This module performs no filesystem, SQLcl or environment operations.

**Interfaces:**

    @dataclass(frozen=True)
    class BaselineComponent:
        kind: str
        steps: tuple[tuple[str, str], ...]
        preconditions: tuple[dict, ...]
        postconditions: tuple[dict, ...]

    @dataclass(frozen=True)
    class BundledBaseline:
        steps: dict[str, str]
        checks: dict
        provenance: tuple[dict, ...]

    validate_bundle_name(name: str) -> str
    combine_baseline_components(components: Sequence[BaselineComponent]) -> BundledBaseline

kind is structure, grants or code. Reject duplicate/unknown kinds. Order categories structure, grants, code independently of input container order; preserve each category's valid consecutive numeric file order. Name validation follows the spec's ASCII slug rule and 64-character limit. Errors use a focused BaselineBundleError(ValueError), translated by baseline.main to its existing exit 2.

- [ ] Write test_three_components_become_one_ordered_payload. With original steps 001-table.sql, 001-permissions.sql and 001-package.sql, assert output names exactly 001-structure-table.sql, 002-grants-permissions.sql and 003-code-package.sql. Assert SQL strings and source SHA-256 values are preserved.
- [ ] Write test_all_checks_survive_with_namespaced_ids: each category has a postcondition named ready; assert structure-ready, grants-ready and code-ready all exist with unchanged SQL/expected values. Duplicate IDs inside one component refuse; check query allowlist remains unchanged.
- [ ] Write test_data_and_changed_boundary_requirements_refuse: data/unknown categories, grants/code preconditions, nonconsecutive filenames and invalid check queries fail without returning a partial bundle. The component type has no rehearsal-metrics field; the adapter accepts only existing schema-1 generated components. Structure preconditions remain initial preconditions; code compile steps remain at their original category-end positions.
- [ ] Write test_empty_components_and_limits: empty categories add no steps; all-empty composition has empty steps; 999 steps are accepted, 1000 refuse; invalid slug case/path/control characters and 65-character names refuse, 64-character valid slugs pass.
- [ ] Run `PYTHONPATH=tests:. python3 -m unittest -v test_baseline_bundle`; the new interface/tests must fail before implementation.
- [ ] Implement the dataclasses and pure composition function. Use the existing SQL/check validators. Do not merge SQL statements, infer arbitrary dependencies, delete checks or rewrite SQL bodies. Include kind, originalName, generatedName and sqlSha256 in each provenance entry.
- [ ] Rerun the focused tests on both branches. Share this version-neutral commit between tracks. Proposed future commit: `feat: compose generated baseline change bundles`.

**Done:** one pure function establishes deterministic step/check preservation with no database or filesystem effects.

### Task 4: Integrate bundle generation and safe installation

**Files:** modify scripts/baseline.py, scripts/migration_manifest.py and tests/test_baseline.py; create scripts/directory_install.py, tests/test_directory_install.py and tests/test_baseline_bundle_cli.py; update scripts/team.sh and scripts/team.ps1 help, README.md, docs/baseline.md, docs/migration-rules.md, docs/EXAMPLES.md and tests/test_documentation_contract.py.

**Interfaces:** add --bundle NAME to build_parser's build subcommand. Family is baseline-bundle-<validated-name>. Adapt _build to assemble all per-schema components before selecting granular or bundled output. Refine _build_migration_folder for validated staging/final installation; keep its signature and existing external behavior. Add validate_staged_migration(repo_root: Path, stage: Path, relative_destination: str) -> None in migration_manifest.py, reusing the existing entry/SQL/check validators with logical destination naming and schema/layout checks, without requiring a final folder to exist. Add install_directory_exclusive(stage: Path, destination: Path) -> None in directory_install.py: same-filesystem, destination-exclusive directory rename; use a no-replace primitive for the platform and refuse when unavailable, with no replacing/copy fallback.

- [ ] Write test_no_bundle_preserves_existing_generated_bytes: run granular fixtures before/after integration and compare SQL, checks and README bytes, family names and counts. The runtime-version fake must be controlled, not a real SQLcl invocation.
- [ ] Write test_bundle_reduces_three_folders_to_one and test_bundle_with_data_keeps_two_folders: call baseline.main with --bundle customer-status, assert one baseline-bundle-customer-status folder per nonempty schema, and assert --data adds a separate baseline-data folder. Load generated folders through load_migration and check every expected source/check. A data-only change creates only baseline-data; no empty bundle is installed.
- [ ] Write test_bundle_multi_schema_never_crosses_owner: two target schemas produce distinct owner-specific folders, SQL and checks; capture scopes must not mix. Retain new same-observed-scope refusal and incomplete capture refusal from the port.
- [ ] Write test_bundle_repeat_and_revision_evidence: identical rerun leaves bytes/folder count unchanged; changed input creates next revision; valid pending/verified events and receipts remain untouched; unknown evidence, revision gaps, r999 exhaustion and flat/schema-family duplication refuse. Recheck evidence just before installation. Recognize validated attempt-event sidecars when comparing payload files so identical attempted output does not manufacture another revision.
- [ ] Write test_generation_failure_never_exposes_partial_final_folder: fail validation, file creation and final installation at controlled points; assert preexisting files/evidence byte-identical and no partially populated final migration folder. Stage in private ignored scratch outside the migrations tree, verify the stage and destination parent share a filesystem, and validate with validate_staged_migration before exposing the final path. Add directory-installer tests for an occupied empty destination, nonempty destination, same-filesystem requirement, unsupported primitive and symlink/reparse path refusal; occupied destinations remain byte-identical. Windows tests must prove the no-overwrite behavior rather than assuming POSIX rename semantics.
- [ ] Run `PYTHONPATH=tests:. python3 -m unittest -v test_baseline test_baseline_bundle test_baseline_bundle_cli test_directory_install`; verify failure against the old generator before integration.
- [ ] Validate bundle names before live capture. After complete source/target capture, require complete identity and refuse same_database_scope(source identity, target identity) before generation; labels or service aliases cannot bypass this guard. Call the pure composer on eligible categories, preserve granular mode, and generate the README mapping/recovery guidance. Reference-data components retain their original checks, DML and separate installation. Do not modify existing arbitrary migrations or create a second apply runner.
- [ ] Stage and validate new folders, then install using a destination-exclusive mechanism with explicit cross-platform refusal if occupied. Cleanup only staging owned by this operation. A later-schema/data generation failure may leave earlier fully validated folders; report their paths accurately and never claim the whole multi-schema generation was atomic.
- [ ] Document default output, the optional bundle flag, expected 3-to-1 / 4-to-2 counts, separate data ordering, immutability, independent-repository coverage limits and Oracle DDL recovery semantics. No hard migration-count limit is introduced.
- [ ] Run focused modules plus test_migration_manifest, test_migration_revision, test_migration_rehearsal, test_team_cli and test_rollout. Compare wrapper output/contracts under fake SQLcl, with real PowerShell execution in Windows CI. Proposed future commit: `feat: add optional baseline bundle generation`.

**Done:** both branches have the same optional bundling feature; default output remains compatible, generated folders validate, and data/recovery boundaries remain explicit.

## Scope boundary

This plan ports only the database prerequisites needed for feature parity on 26.1. It does not add main's APEX 26.2 app locks, partial publish, runtime lifecycle or source-normalization behavior to that branch. Unknown legacy evidence remains unknown. Any actual live qualification requires a separate named-target authorization and its own evidence.
