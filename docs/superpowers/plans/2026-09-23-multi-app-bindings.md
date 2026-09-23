# Multi-App Environment Bindings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind each app to one environment-specific parsing schema while retaining one shared tables/code schema pair, one database, and one workspace per environment.

**Architecture:** Keep `Config.apps` and `TargetContract.app_ids` as alias-to-ID maps for existing callers; add explicit alias-to-parsing-schema maps derived from one strict input format. `Target` remains the single physical-app identity passed to export, import, deploy, state and release code. A version-2 target contract replaces the ambiguous global parsing schema and rejects version 1 with conversion guidance.

**Tech Stack:** Python 3.10+, unittest, JSON target contracts, literal ignored `.env`, SQLcl saved connections.

**Spec:** `docs/superpowers/specs/2026-09-23-simple-multi-app-team-workflow-design.md`

## Global Constraints

- APEX **26.1 or newer** and APEXlang only; no legacy SQL-export source path.
- `apps/<stable-alias>/` remains tracked app source; IDs and parsing schemas are environment bindings.
- Each app has exactly one parsing schema; multiple apps may share one.
- One `TABLES_SCHEMA` and one `CODE_SCHEMA` per environment may be equal or different.
- One verified database instance and one APEX workspace per environment.
- Local `.env` is ignored, credential-free, and uses saved SQLcl connection names.
- Preserve qualified target identity, production-write refusal and durable `.sync-state/` recovery.
- No automatic commit, push, import or production write.

## Review Focus

1. Duplicate app IDs under different aliases must refuse before SQLcl starts (Task 1 test).
2. An alias with a malformed or absent parsing schema must refuse rather than inherit a global schema (Task 1 test).
3. Two aliases sharing one parsing schema must remain distinct physical app targets (Task 1 test).
4. A version-1 target contract must report the conversion action, not silently reinterpret its global binding (Task 2 test).
5. An app contract with a different parsing schema than the local qualified profile must refuse before deployment (Task 2 test).

## File map and sequence

- `scripts/teamlib/config.py`: sole parser and binder for local and tracked app identity.
- `scripts/tests/test_config.py`, `scripts/tests/test_release_adapter.py`, `scripts/tests/test_deploy.py`: contract and boundary regressions.
- `targets/{integration,test,production}.json`, `.env.example`: version-2 examples.
- `scripts/teamlib/local_team_e2e.py` and its tests: disposable fixture uses the new binding syntax.
- `docs/toolchain.md`: explain conversion and same/split parsing-schema examples.

Complete this plan before the publish and release plans. Preserve the unrelated untracked local E2E review plan.

### Task 1: Parse explicit local app bindings

**Files:** Modify `scripts/teamlib/config.py`, `.env.example`, `scripts/tests/test_config.py`, `scripts/teamlib/local_team_e2e.py`, `scripts/tests/test_local_team_e2e.py`.

**Interfaces:** `parse_app_bindings(value: str) -> dict[str, AppBinding]`, where `AppBinding` is frozen with `app_id: int` and `parsing_schema: str`; `Config.apps` remains `Mapping[str, int]`, and new `Config.app_parsing_schemas` is `Mapping[str, str]`. `profile_target(config, "APEX", alias)` uses the selected map entry.

- [ ] **Step 1: Write failing tests** for `hr:100:HR_CODE,payroll:200:FIN_CODE`, shared `APP_CODE`, duplicate IDs, missing third field, invalid uppercase Oracle identifier, and unequal app IDs sharing one schema. Test both `TABLES_SCHEMA=APP_DATA`/`CODE_SCHEMA=APP_CODE` and both fields set to `APP_DATA`. Assert `profile_target(config, "APEX", alias="hr").parsing_schema == "HR_CODE"` and Payroll differs. Change fixture `BASE_ENV` to the new syntax and remove its `APEX_PARSING_SCHEMA` line. Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -m unittest scripts.tests.test_config -v`; expect new assertions to fail before code changes.
- [ ] **Step 2: Implement the minimal parser and target binding**. Use a single `AppBinding` representation and derive the old ID map without accepting the two-field syntax:

  ```python
  @dataclass(frozen=True)
  class AppBinding:
      app_id: int
      parsing_schema: str

  def parse_app_bindings(value: str) -> dict[str, AppBinding]:
      if not value or value.strip() != value:
          raise ValueError("APEX_APPS must use alias:id:PARSING_SCHEMA")
      result: dict[str, AppBinding] = {}
      seen_ids: set[int] = set()
      for item in value.split(","):
          parts = item.split(":")
          if len(parts) != 3:
              raise ValueError("APEX_APPS must use alias:id:PARSING_SCHEMA")
          alias, raw_id, schema = parts
          if not _ALIAS_RE.fullmatch(alias) or alias in result:
              raise ValueError(f"invalid or duplicate application alias: {alias!r}")
          if not _POSITIVE_INT_RE.fullmatch(raw_id):
              raise ValueError(f"invalid application ID for {alias}: {raw_id!r}")
          app_id = int(raw_id)
          if app_id in seen_ids:
              raise ValueError(f"duplicate application ID: {app_id}")
          if not _ORACLE_IDENTIFIER_RE.fullmatch(schema):
              raise ValueError(f"invalid parsing schema for {alias}: {schema!r}")
          result[alias] = AppBinding(app_id, schema)
          seen_ids.add(app_id)
      return result
  ```

  Remove `APEX_PARSING_SCHEMA` from `BASE_KEYS` and `Config`, add `app_parsing_schemas`, and select `config.app_parsing_schemas[alias]` in `profile_target`. Do not catch parser failures and invent a default schema.
- [ ] **Step 3: Update disposable fixture inputs and `.env.example`** to show both different and shared parsing-schema examples; keep the one APEX SQLcl saved connection only if it can access both app bindings. Run the Task 1 test file and `scripts.tests.test_local_team_e2e`; expect PASS.
- [ ] **Step 4: Run** `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v` and `ruff check scripts/`; correct downstream fixture assumptions about the retired key without altering safety assertions.
- [ ] **Step 5: Review and commit only Task 1 files** with `git diff --check`, explicit `git add -- <Task 1 files>`, and `git commit -m "feat: bind each app to one parsing schema"`.

### Task 2: Replace the tracked target contract without identity weakening

**Files:** Modify `scripts/teamlib/config.py`, `scripts/tests/test_config.py`, `scripts/tests/test_release_adapter.py`, `scripts/tests/test_deploy.py`, `targets/integration.json`, `targets/test.json`, `targets/production.json`, `docs/toolchain.md`.

**Interfaces:** `TargetContract.app_ids` stays `Mapping[str, int]`; new `TargetContract.app_parsing_schemas` is `Mapping[str, str]`. JSON version 2 uses `"apps": {"hr": {"id": 100, "parsing_schema": "HR_CODE"}}`; `contract_target(path, "hr")` returns the app's schema. Global `binding.parsing_schema` and `app_ids` are rejected in version 2.

- [ ] **Step 1: Write failing tests** for a version-2 two-app contract, same-schema contract, missing/invalid schema, duplicate IDs, secret-bearing keys, version-1 refusal message, and contract/profile schema mismatch via `_assert_binding_matches_profile`. Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -m unittest scripts.tests.test_config scripts.tests.test_release_adapter scripts.tests.test_deploy -v`; expect the new cases to fail.
- [ ] **Step 2: Implement strict version-2 parsing and binding**. The accepted app member shape is exactly `{"id": positive_int, "parsing_schema": uppercase_identifier}`; reject extra keys, booleans as IDs, absent mappings and duplicate IDs. Derive `app_ids` and `app_parsing_schemas` together; reject `version: 1` with `target contract version 1 uses a global app binding; convert app_ids to version-2 apps with id and parsing_schema`. In `contract_target`, set `parsing_schema=contract.app_parsing_schemas[alias]` and include it in the binding digest.
- [ ] **Step 3: Convert the three tracked target examples** without credentials. For example, replace `"app_ids": {"employee-self-service": 101}` with `"apps": {"employee-self-service": {"id": 101, "parsing_schema": "EXAMPLE_APP"}}`, change `version` to 2, and remove global app schema from `binding`. Document the one-time conversion in `docs/toolchain.md`.
- [ ] **Step 4: Run** the targeted tests, then the full unittest suite, `ruff check scripts/`, `python3 -m json.tool` on each converted target, and `git diff --check`; expect PASS. A failure in any release/deploy fixture is an explicit migration of that fixture, not a compatibility fallback.
- [ ] **Step 5: Review and commit only Task 2 files** with `git commit -m "feat: qualify version-two per-app target contracts"`.

### Task 3: Verify the complete binding boundary

**Files:** Modify `scripts/tests/test_config.py`, `scripts/tests/test_production_boundary.py`, `scripts/tests/test_runtime.py`, `scripts/tests/test_docs.py`, `docs/toolchain.md` only if a failed test exposes a missing contract check.

**Interfaces:** Existing `profile_target`, `contract_target`, `Target.state_key` and `Target.physical_key`; no new public API.

- [ ] **Step 1: Add failing regression tests** proving `state_key` changes when only an app's parsing schema changes, `physical_key` still binds instance/workspace/app ID, two apps with one parsing schema have distinct physical keys, non-APEX profiles cannot take an alias, and production-classified config cannot reach a write command. Run the three named test modules; expect at least the new schema-identity assertion to fail if any binder still uses a global schema.
- [ ] **Step 2: Fix only the specific binder or test fixture that fails**; do not relax identity comparisons or production refusal. The target identity formula must include `alias`, `app_id`, `workspace_id`, and selected `parsing_schema` in `binding_digest`, while `physical_key` remains the shared physical-app mutex key.
- [ ] **Step 3: Run the full offline suite, `ruff check scripts/`, `bash -n scripts/*.sh`, and `git diff --check`. Record SQLcl/APEX live verification as unavailable unless a qualified non-production `.env` and required saved connections are actually present; never substitute direct unqualified `sql` calls.
- [ ] **Step 4: Commit only Task 3 changes** with `git commit -m "test: protect per-app binding identity"` if changes were needed; otherwise record the green verification in the handoff without an empty commit.

### Task 4: Enforce APEX 26.1 and live app identity before app writes

**Files:** Create `scripts/teamlib/app_identity.py`, `scripts/tests/test_app_identity.py`; modify `scripts/teamlib/apex.py`, `scripts/teamlib/deploy.py`, `scripts/teamlib/runtime.py`, `scripts/tests/test_import_app.py`, `scripts/tests/test_deploy.py`, `scripts/tests/test_runtime.py`, `docs/toolchain.md`.

**Interfaces:** `observe_app_identity(target: Target, runner=run_sqlcl) -> AppIdentity` returns `status: PRESENT|ABSENT|UNKNOWN`, observed `app_id`, `workspace_id`, `parsing_schema`, workspace-schema assignment and `apex_version` from a qualified read; `require_app_identity(target, observed: AppIdentity, *, allow_absent: bool) -> None` refuses mismatch/unknown or APEX below 26.1. Import/export require `PRESENT`; a first deploy may accept `ABSENT` only when the target workspace and its parsing-schema assignment are verified.

- [ ] **Step 1: Write failing tests** for APEX `24.2`, `26.0`, `26.1`, `26.1.4`, malformed/unknown version, wrong app ID, wrong workspace and wrong parsing schema. Test an absent app in a verified workspace/schema as allowed for a first deploy but refused for import/export, and an absent app with an unassigned schema as refused. Assert import/deploy fake runners receive no write call on each mismatch; assert the qualified read uses the selected app's target, not a global schema. Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -m unittest scripts.tests.test_app_identity scripts.tests.test_import_app scripts.tests.test_deploy -v`; expect failures.
- [ ] **Step 2: Inspect the APEX 26.1 dictionary on a qualified non-production profile** for actual application-ID, workspace-ID, parsing-schema and workspace-schema-assignment columns. Use `APEX_DICTIONARY`/documented APEX metadata through the repository's `run_sqlcl(target, "read", driver, work_root)` only; record the observed view/column names and APEX version in ignored `scratch/` evidence. If the live profile is unavailable, implement an `UNKNOWN` refusal and state live qualification as pending; do not guess a view's shape or weaken the write guard.
- [ ] **Step 3: Implement the read adapter and match guard** with fixed/whitelisted SQL identifiers from the observed dictionary, numeric target app ID, at most one app row, checked SQLcl identity/completion, and exact `Target` comparisons. An absent app requires positive workspace/schema assignment evidence and `allow_absent=True`; no row plus unknown assignment is `UNKNOWN`, not safe absence. Call it before `import_app` and `deploy_app` mark payload starting; call the version check on daily export. Keep production refusal before SQLcl writes.
- [ ] **Step 4: Run** the named tests, full unittest discovery, `ruff check scripts/`, and `git diff --check`. Where qualified SQLcl is available, test one read-only app identity and APEX version observation; do not create/import an app in this task.
- [ ] **Step 5: Commit only Task 4 files** with `git commit -m "feat: require qualified APEX app identity before writes"`.

**Handoff:** Deliver this slice independently with exact offline and qualified-live evidence. A synthetic pass is not proof that a live app belongs to the configured workspace/schema; if live metadata access is unavailable, publishing and deploying remain refused rather than guessed.
