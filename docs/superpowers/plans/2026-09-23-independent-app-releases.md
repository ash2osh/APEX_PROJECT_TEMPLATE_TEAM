# Independent App and Schema Releases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Release one selected APEX app without deploying its siblings, while applying shared schema migrations once per environment in a separate release.

**Architecture:** Reuse the deterministic archive verifier, qualified migration/deploy adapters, protected test evidence and production-owner handoff. Add explicit `kind: schema|app` to a new archive manifest version: schema archives carry migration SQL and no apps; app archives carry one APEXlang tree, its checks and exact migration requirements, but no migration SQL. The target contract supplies environment-specific app ID and parsing schema.

**Tech Stack:** Python 3.10+, unittest, deterministic tar/JSON, SQLcl, GitHub Actions protected runners.

**Spec:** `docs/superpowers/specs/2026-09-23-simple-multi-app-team-workflow-design.md`

## Global Constraints

- Requires `docs/superpowers/plans/2026-09-23-multi-app-bindings.md` first.
- APEX **26.1 or newer** and APEXlang only; one parsing schema per app.
- The shared TABLES/CODE migration stream applies once per environment, separately from apps.
- An app archive contains exactly one alias and does not apply migrations or deploy siblings.
- Required migration IDs and checksums must match accepted metadata history before an app write.
- Shared-component master dependencies are checked, never deployed implicitly.
- Production-classified writes remain refused; signed protected-test evidence does not authorize automated production writes.
- No automatic commit, push or import.

## Review Focus

1. An app with no requirements declaration must not accidentally inherit all repository migrations (Task 1 test).
2. A requirement ID absent from the selected source commit must refuse archive construction; a changed checksum must refuse against accepted target history (Tasks 1 and 2 tests).
3. A target with required migration marked `REVERTED` must refuse the app before deployment (Task 2 test).
4. A release archive containing a second app, deployment JSON, or migration SQL for `kind: app` must fail verification (Task 1 test).
5. A test target contract with a mismatched selected app ID/schema must fail before any schema/app payload (Task 2 test).

## File map and sequence

- `app_context/<alias>/release.json`: small authored list of required migration IDs for that app. The builder resolves immutable checksums from the selected Git commit; authors do not hand-copy checksums. The empty template repository documents the file in `app_context/README.md`; test fixtures create concrete HR/Payroll declarations.
- `scripts/teamlib/release.py`: select payload by kind and verify v2 manifest.
- `scripts/teamlib/release_adapter.py`, `scripts/teamlib/online_workflows.py`: schema-only or app-only protected application and qualification.
- `scripts/teamlib/evidence.py`, `scripts/teamlib/runbook.py`: retain kind/alias in signed evidence and handoff.
- `scripts/team.py`, `.github/workflows/release.yml`: one selected release type per invocation/tag.
- `scripts/tests/test_release.py`, `scripts/tests/test_release_adapter.py`, `scripts/tests/test_online_workflows.py`, `scripts/tests/test_release_workflow.py`, `scripts/tests/test_runbook.py`, `scripts/tests/test_docs.py`: TDD and CI assertions.
- `docs/promotion.md`, `docs/migrations.md`, `docs/ci.md`: operator contract.

### Task 1: Build and verify one kind of archive

**Files:** Modify `scripts/teamlib/release.py`, `scripts/team.py`, `scripts/tests/test_release.py`, `app_context/README.md`; create HR/Payroll `app_context/<alias>/release.json` files only inside the test fixture.

**Interfaces:** `build_release(repo, ref, version, out, *, kind: Literal["schema", "app"], alias: str | None = None) -> Manifest`. Manifest format version 2 adds `kind`, `alias` (null for schema), and `required_migrations: tuple[Mapping[str, str], ...]` with exact `id`/`checksum` keys. CLI: `build-release --kind schema --ref <commit> --version 1.0.0 --out <dir>` or `build-release --kind app --alias hr --ref <commit> --version 1.0.0 --out <dir>`.

- [ ] **Step 1: Write failing archive tests** in `test_release.py` using a repo fixture with HR and Payroll plus one migration. Assert schema build has migration members and zero app trees; HR build has only `release/apps/hr/`, HR's check declaration and no migration SQL or Payroll tree/check; both verify deterministically. Assert an empty schema release refuses, and an app build fails for missing `app_context/hr/release.json`, a requirement ID absent from the selected commit and duplicate ID. Explicit `{"version":1,"requires":[]}` is valid for an app with no DB prerequisites. Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -m unittest scripts.tests.test_release -v`; expect new tests to fail.
- [ ] **Step 2: Add the explicit authored declaration**:

  ```json
  {"version": 1, "requires": ["20260907T100000__alice__one"]}
  ```

  Read this file from the **selected Git commit**, not the working tree. Reject extra fields, duplicate IDs, missing declared migration bundles and non-canonical IDs. Resolve each checksum using existing `load_bundles` and the same checksum definition already used by migration history.
- [ ] **Step 3: Implement format-2 selection and verification**. `kind="schema"` selects only `migrations/*.sql` and schema evidence/contracts; `kind="app"` selects exactly one `apps/<alias>/` tree, that alias's `ci/app-checks/` members, its `app_context/<alias>/release.json`, and the master contract. Explicitly reject `deployments/` and `default.json` in both. Include `kind`, `alias`, requirements, selected source commit and payload hashes in the manifest; derive `source_tree` from the selected payload. `verify_release` recomputes every derived digest and rejects cross-kind members, additional aliases and tampering.
- [ ] **Step 4: Update CLI parser** to require `--kind` and require `--alias` iff kind is app. Retire the old all-app/all-migration artifact path with a clear error; do not silently map it to one app. Run release tests, CLI/help tests, `ruff check scripts/`, and `git diff --check`.
- [ ] **Step 5: Commit only Task 1 files** with `git commit -m "feat: build separate app and schema archives"`.

### Task 2: Apply schema once and app only after prerequisites

**Files:** Modify `scripts/teamlib/release_adapter.py`, `scripts/teamlib/online_workflows.py`, `scripts/teamlib/release.py`, `scripts/tests/test_release_adapter.py`, `scripts/tests/test_online_workflows.py`, `scripts/tests/test_release_workflow.py`.

**Interfaces:** `verify_required_migrations(required: tuple[Mapping[str, str], ...], history: Mapping[str, Any]) -> None` raises `ReleaseAdapterError` for absent/reverted/checksum-mismatched history. `run_release_test` reads manifest kind and selects schema-only migration apply or single-app deploy/qualification.

- [ ] **Step 1: Write failing adapter tests** with `deploy_application` and `apply_migrations` spies. A schema archive calls only migration apply once (or no-op when already applied); an HR archive calls only HR deploy after exact requirement history and live contract/profile identity checks. Missing/reverted/wrong-checksum history, wrong app ID/schema and foreign target identity call neither writer. Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -m unittest scripts.tests.test_release_adapter scripts.tests.test_online_workflows -v`; expect failures.
- [ ] **Step 2: Implement exact history verification** before any app write:

  ```python
  def verify_required_migrations(required, history):
      for item in required:
          row = history.get(item["id"])
          if row is None or row.get("status") != "APPLIED" or row.get("checksum") != item["checksum"]:
              raise ReleaseAdapterError(f"required migration unavailable: {item['id']}")
  ```

  Adapt the lookup to the repository's canonical history shape without weakening `APPLIED` or checksum equality. Preserve existing drift and target preflight before schema migration apply.
- [ ] **Step 3: Split the live adapter by manifest kind**. Schema kind loads only packaged migration files and invokes the isolated METADATA migration path; app kind binds exactly its alias through `contract_target`, `_assert_binding_matches_profile`, master/component validation, `deploy_app`, and the selected app's check bundle. A whole target contract may list other apps; the release selects one and must not require equality with the whole app set. `run_release_test` emits the kind/alias and checks only what was exercised.
- [ ] **Step 4: Run** adapter, online workflow, migration-runner, deployment, and full offline tests. A protected non-production SQLcl/ORDS test is required before claiming live success; when credentials/runners are absent, state `UNKNOWN` and do not call the fake-runner tests a live E2E. Run `ruff check scripts/` and `git diff --check`.
- [ ] **Step 5: Commit only Task 2 files** with `git commit -m "feat: apply schema and app releases independently"`.

### Task 3: Carry selected-release identity through CI and handoff

**Files:** Modify `.github/workflows/release.yml`, `scripts/team.py`, `scripts/teamlib/evidence.py`, `scripts/teamlib/runbook.py`, `scripts/tests/test_release_workflow.py`, `scripts/tests/test_runbook.py`, `scripts/tests/test_docs.py`, `docs/promotion.md`, `docs/migrations.md`, `docs/ci.md`.

**Interfaces:** Release tags are `schema/v<semver>` and `app/<alias>/v<semver>`; artifact/evidence/runbook identity includes `kind`, optional alias, source commit and archive digest. A schema runbook contains migration steps only; an app runbook contains one app deployment and prerequisite checks only.

- [ ] **Step 1: Write failing tests** for both tag forms, kind/alias-bound archive signature and test evidence, a schema-only runbook, a single-app runbook, and rejection of a signed HR test report reused for Payroll or a schema archive. Run release workflow and runbook tests; expect failures.
- [ ] **Step 2: Update release record identity** to namespace by kind and alias, so `schema/v1.0.0` and `app/hr/v1.0.0` do not collide. Bind signed canonical evidence and production handoff to manifest kind/alias; preserve archive digest and source commit equality, protected test role, trust-key verification, history and target identity checks.
- [ ] **Step 3: Update the protected workflow** to parse only the two tag shapes, pass `--kind` and selected `--alias` to `build-release`, verify downloaded bytes, run only that artifact on the protected test target, sign its evidence, and generate a matching offline production-owner runbook. Keep protected secrets in runner temp files and remove them on completion. Update operator docs with copyable HR and schema release examples and the explicit no-production-write boundary.
- [ ] **Step 4: Run** release, evidence, runbook, CI, docs and full unittest suites, `ruff check scripts/`, `python3 -m json.tool` on changed contracts, and `git diff --check`. Inspect the generated schema/HR runbooks offline; do not claim protected SQLcl/browser checks passed unless actually run.
- [ ] **Step 5: Commit only Task 3 files** with `git commit -m "feat: qualify and hand off selected releases"`.

### Task 4: Prove one schema release does not force sibling app deployment

**Files:** Modify `scripts/tests/test_release_workflow.py`, `scripts/tests/test_online_workflows.py`, `docs/promotion.md`.

**Interfaces:** The Task 1 `build_release(..., kind="schema"|"app", alias=...)` and Task 2 `run_release_test` paths; no new public API.

- [ ] **Step 1: Write a failing two-app fixture test** with HR v1 and Payroll v1 sharing the same TABLES/CODE stream. Build a schema archive adding an optional `EMPLOYEE.PRONOUNS` column, then an HR v2 app archive whose `app_context/hr/release.json` requires that migration. Assert the schema artifact applies once, HR v2 deploys, Payroll's deploy spy is never called, and Payroll's app tree/generation remains v1. Add a refusal case where the migration is not applied and HR deploy is never called.
- [ ] **Step 2: Add a master-component regression**: if HR declares a Payroll master dependency in `targets/masters.json`, an absent or wrong-target master refuses HR; a present qualified master allows HR without deploying Payroll. Reuse the existing master resolver and add no automatic dependency deployment.
- [ ] **Step 3: Run** both named test modules, full unittest discovery, and `ruff check scripts/`; then run the protected two-app SQLcl/browser scenario only on an owned non-production fixture whose identity and cleanup are preflighted. Record a missing protected runner or unavailable browser check as `UNKNOWN`, not a fake PASS. Commit only Task 4 files with `git commit -m "test: prove independent app release on shared schema"`.

**Handoff:** Demonstrate the sequence `schema/v1.0.0` once, then `app/hr/v2.0.0` while Payroll remains unchanged. Report app and schema evidence separately, including any `UNKNOWN` protected/browser layer.
