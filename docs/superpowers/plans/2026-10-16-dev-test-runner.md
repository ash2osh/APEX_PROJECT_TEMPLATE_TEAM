# DEV Test Runner Implementation Plan

> **For agentic workers:** Execute task-by-task through AGY CLI implementers, with the primary Codex agent reviewing and fixing the implementation, as explicitly requested by the user after planning. No nested agents or native collaboration subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add repeatable real-DEV template tests and selected project SQL scenarios using existing schema-owner connections, retaining fixtures by default with optional inventory-based cleanup.

**Architecture:** A strict suite loader, typed SQL bindings, local evidence store and target inspector support a sequential runner shared by both team wrappers. Built-in scenarios call the existing baseline, rollout, migration and check implementations; project scenarios execute reviewed SQL through the same SQLcl infrastructure. Each run freezes inputs, records intent before writes and reports functional results separately from retained artifacts and cleanup.

**Tech Stack:** Python 3.10+ standard library, unittest, existing SQLcl 26.3+ bridge, Bash and PowerShell; no additional runtime package or database metadata table.

**Spec:** [DEV test runner design](../specs/2026-10-16-dev-test-runner-design.md). Read the spec and this plan together. This is an unimplemented plan: no checkbox below represents completed product work or authorization for a particular live target.

## Global Constraints

- One selected DEV owner per invocation, sequential execution, Bash and PowerShell entry points with equivalent behavior.
- Developers should not need a separate database, another schema, SYS access, or a copy of their application's dependency tree.
- Fixture objects and data are retained by default for inspection. Retention is a normal result, not a failure.
- Exactly one action is required: `--plan`, `--run`, `--report` or `--cleanup`. No arguments print usage without connecting.
- Planning/running require explicit `--env dev`. Other environments are refused.
- The observed session user must equal the selected schema owner for first-release execution; no DBA fallback or automatic grant/recompile/repair.
- Project authors place suites under `dev-tests/`. This directory is project-owned, survives template upgrades and is outside inherited template-maintenance `tests/` cleanup.
- No automatic resume/replay of a write attempt. New execution creates a new run.
- Built-in object identifiers stay within a 30-byte budget; observations have `maxRows` from 1 to 100; suite/report schemaVersion is integer 1.
- Scenario states: `PASSED`, `FAILED`, `BLOCKED`, `NOT_RUN`, `UNKNOWN`. Cleanup states: `NOT_REQUESTED`, `COMPLETE`, `INCOMPLETE`, `REFUSED`, `UNSUPPORTED`.
- Exit 0 means complete success/eligible plan; 1 means an established functional failure; 2 means refusal, incomplete/blocked/unknown coverage or unsuccessful requested cleanup; 130 is interruption before any write may have started. Exit 2 takes precedence over 1.
- APEX imports, workflow/task execution, ORDS/HTTP/external-effect adapters, global dependency copying and automatic historical receipt classification are outside v1.
- Preserve each template release's upstream/manifest/APEX contract. No live target or workspace is inferred from this plan.
- Use LF, preserve unrelated tracked/untracked work, do not stash/reset the whole checkout, and do not commit/push without explicit authorization for this feature.

## Review Focus

1. Windows path aliases, symlinks and traversal must not escape a suite/run directory: Tasks 1, 2 and 10 pin these cases.
2. A saved SQLcl alias repointed to another database must not authorize cleanup there: Tasks 3 and 8 pin full observed-identity mismatch.
3. Process loss after Oracle changed state but before observation must remain UNKNOWN without replay: Tasks 2, 5 and 8 pin this window.
4. A retained object modified or adopted by another developer must not be removed based only on its prefix: Task 8 pins changed definitions and unexpected dependents.
5. Unicode/quoted binding values and expected Oracle errors must not permit injection or turn a transport failure into a passing negative test: Tasks 4 and 5 pin both.

## Baseline and execution preparation

Planning observed the following local checkouts; these are not a future branch-tip or remote-parity claim:

| Track | Checkout | Observed HEAD |
| --- | --- | --- |
| 26.2 | `/home/ash/projects/APEX_PROJECT_TEMPLATE_TEAM` | `8c0c3c4e1474f77d6a4a301491ec88bcad2b18e5` |
| 26.1 | `/home/ash/projects/APEX_PROJECT_TEMPLATE_26_1` | `e08b2bcd7d0f4e8795066603ec0371759ff8441a` |

The shared spec is in the 26.2 checkout and covers both releases. Both this plan and its spec are newly written documents, not implementation commits. At approved implementation start, inspect current repository instructions, known limitations, branch/upstream and dirty state again. Use a named feature branch in the existing checkout as required by the local executing/writing-plan workflow; do not create a worktree for this plan. Preserve existing app exports, `.env`, mirrors, scratch evidence and planning documents. Use the same feature branch name in each independent repository if convenient, without assuming shared history.

Each task has an optional commit checkpoint. Make explicit-path commits only when the user has authorized committing this feature; otherwise keep the checkpoint as reviewed local changes. A task's test PASS does not authorize database operations or a push.

## File and interface map

| File | Responsibility |
| --- | --- |
| `scripts/dev_test_manifest.py` | Strict suite/step/effect/fixture contract and deterministic scenario selection |
| `scripts/dev_test_bindings.py` | Typed values, lexer-aware object-token rendering and bounded binding validation |
| `scripts/dev_test_evidence.py` | Private run directories, frozen inputs, durable intent/outcome events, inventory and atomic reports |
| `scripts/dev_test_report.py` | Schema-1 report validation, aggregate exit status and text/JSON/Markdown rendering |
| `scripts/dev_test_target.py` | Existing profile resolution plus controlled identity/prerequisite/fixture-collision inspection |
| `scripts/dev_test_runtime.py` | SQL steps, bounded selectors/observations and scenario orchestration through existing SQLcl modules |
| `scripts/dev_test_core.py` | Built-in suite declaration and adapter dispatch, with no project-specific rules |
| `scripts/dev_test_core_types.py` | Datatype roundtrip and compiler-settings scenarios |
| `scripts/dev_test_core_migrations.py` | SQLcl-delivery, migration-success/refusal and invalid-view scenarios |
| `scripts/dev_test_cleanup.py` | Saved-target and ownership verification plus exact-list optional cleanup |
| `scripts/dev_test.py`, `scripts/dev_test.sh` | Public parser/action orchestration and standard Python launcher integration |
| `scripts/team.sh`, `scripts/team.ps1` | Thin public entry points; argument/exit parity |
| `scripts/migration_checks.py` | Narrow opt-in named-bind support; preserve ordinary migration defaults |
| `scripts/read_only_query.py` | Narrow opt-in bindings for bounded selector/observation queries; preserve public query defaults |
| `docs/dev-test.md`, `docs/dev-test-examples/` | User guide and a complete, explicitly selected sample suite |
| `scripts/upgrade_template.py`, `template-manifest.json` | Protect project suites and own runtime/docs; preserve release-specific metadata |
| `tests/test_dev_test_*.py` | Offline behavior/negative cases, using existing fake-SQLcl and no-real-SQLcl protection |

Do not add all files as empty scaffolding. Introduce each with the task that delivers tested behavior. Use frozen dataclasses for suite/binding/run value objects and dictionaries for report schema records; do not invent a second schema framework dependency.

## Task 1: Strict suite contract and selection

**Files:** Create `scripts/dev_test_manifest.py`, `tests/test_dev_test_manifest.py`.

**Interfaces:** Produce frozen `Suite(id: str, root: Path, scenarios: tuple[Scenario, ...])` and `Scenario` carrying the exact fields from spec section 6; `load_suite(path: Path) -> Suite`; `select_scenarios(suite: Suite, ids: Sequence[str]) -> tuple[Scenario, ...]`. Raise `DevTestManifestError(ValueError)` with a structured code and safe message. Required direct dependencies must be selected explicitly; a missing selected dependency refuses rather than silently widening requested writes.

- [x] **Step 1 — Write failing contract tests.** `test_loads_spec_example` checks schemaVersion 1, one scenario, `cleanup is None` and expected integer 2. Table cases reject boolean schemaVersion/expected, unknown fields, duplicate IDs/step/check/binding names, missing checks, cyclic/forward dependencies, unsupported effects/enums, observation limits 0/101, missing files, absolute/drive/UNC/traversal paths and symlink escape. `test_selection_never_adds_project_writes` checks omitted dependencies refuse and caller selection retains manifest order.
- [x] **Step 2 — Run RED.** `python3 -m unittest discover -s tests -p 'test_dev_test_manifest.py' -v`; expect the new tests to fail from missing implementation, not fixture/path mistakes.
- [x] **Step 3 — Implement the interfaces.** Match the strict root/nested key sets in the spec; reject duplicate JSON keys with `object_pairs_hook`. Resolve files once under the suite root, retain original bytes/hashes and refuse symlink escapes. Validate all suite scenarios before selecting any writes. Do not import project Python or execute SQL while loading.
- [x] **Step 4 — Run GREEN.** Repeat the Task 1 command; all cases pass. Include Windows path strings even when running on Linux.
- [x] **Step 5 — Checkpoint.** Review the contract against the spec's JSON example; optional explicit-path commit: `feat: validate DEV test suite contracts`.

## Task 2: Durable evidence, inventory and result model

**Files:** Create `scripts/dev_test_evidence.py`, `scripts/dev_test_report.py`, `tests/test_dev_test_evidence.py`, `tests/test_dev_test_report.py`.

**Interfaces:** Produce `RunEvidence(run_id: str, root: Path)` with `create_run(repo_root: Path) -> RunEvidence`, `open_run(repo_root: Path, run_id: str) -> RunEvidence`, `freeze_inputs(run: RunEvidence, paths: Sequence[Path]) -> dict[str, str]`, `assert_inputs_unchanged(run: RunEvidence) -> None`, `append_event(run: RunEvidence, event: Mapping) -> Path`, `save_inventory(run: RunEvidence, inventory: Mapping) -> None`; report functions `validate_report(value: object) -> dict`, `aggregate_exit(report: Mapping) -> int`, `render_report(report: Mapping, format: str) -> str`, `save_report(run: RunEvidence, report: Mapping) -> None`.

- [x] **Step 1 — Write failing evidence tests.** `test_intent_survives_missing_outcome` leaves a pre-write intent with no result and expects UNKNOWN/exit 2, never an inferred retry. `test_run_path_cannot_escape_or_follow_symlink` covers traversal, existing-run overwrite and malformed IDs. `test_frozen_inputs_detect_sql_and_runtime_changes` changes each frozen class and expects refusal before another intent. `test_report_retention_is_not_failure` asserts six passes plus `NOT_REQUESTED` cleanup => 0. Exhaustive result cases prove 2 > 1, blocked coverage => 2, pre-write interrupt => 130, post-write interrupt => 2, cleanup does not rewrite execution results, corrupt/truncated JSON never passes.
- [x] **Step 2 — Run RED.** `python3 -m unittest discover -s tests -p 'test_dev_test_e*.py' -v` and `python3 -m unittest discover -s tests -p 'test_dev_test_report.py' -v`; expect the new interfaces missing.
- [x] **Step 3 — Implement storage/report functions.** Use UTC timestamp plus UUID for run IDs, an uppercase random `DT_<12 hex>_` object prefix, private scratch creation and atomic replace for snapshots. Intent events are separate exclusively created JSON files, flushed before SQL dispatch; never overwrite a prior event. Store manifest/SQL/check/runtime hashes and rendered SQL hashes separately. Preserve append-only cleanup history. Validate the complete report's required fields/types, not just a success flag.
- [x] **Step 4 — Run GREEN.** Repeat Task 2 tests. Confirm a report with a deliberate retained INVALID fixture is valid and not conflated with an unexplained invalid application object.
- [x] **Step 5 — Checkpoint.** Optional commit: `feat: record durable DEV test evidence and outcomes`.

## Task 3: Existing-owner target planning and prerequisite inspection

**Files:** Create `scripts/dev_test_target.py`, `tests/test_dev_test_target.py`; read `scripts/db_targets.py`, `scripts/local_config.py`, `scripts/migration_checks.py`, `scripts/schema_catalog.py`.

**Interfaces:** `inspect_suite(values: Mapping[str, str], schema: str | None, suite: Suite, run: RunEvidence, *, runner: Callable = run_sqlcl) -> dict` returns an eligible/incomplete plan, observed identity and per-scenario requirement/collision evidence. `assert_saved_target(saved: Mapping, observed: Mapping) -> None` compares all required identity fields; missing fields are not wildcards. Both helpers raise `DevTestTargetError` for target-level refusal.

- [x] **Step 1 — Write failing target tests.** Verify multi-owner explicit selection, DEV classification and production-looking target refusal, owner-login equality and no DBA fallback. `test_alias_repoint_is_not_same_target` changes database unique name/service/container/edition in separate cases. `test_no_dba_space_visibility_is_advisory` remains eligible without DBA space views; `test_missing_dependency_is_not_auto_repaired` yields BLOCKED with no write. Member invisibility is unknown, not fabricated absence. Existing fixture name collision blocks creation. Plan spies assert no suite SQL, APEX session creation, grants, DDL or DML is executed.
- [x] **Step 2 — Run RED.** `python3 -m unittest discover -s tests -p 'test_dev_test_target.py' -v`; expect the new target module missing.
- [x] **Step 3 — Implement planning.** Use existing `resolve_target(..., operation="migration")` and identity fields, not another profile map. Inspect only declared prerequisites/planned fixture names; use bound catalog parameters and controlled SELECTs. Record installed/visible APEX and SQLcl versions without making APEX session availability a database-fixture requirement. Read-only catalog/check failures must preserve safe diagnostics and visibility limits.
- [x] **Step 4 — Run GREEN.** Repeat Task 3 tests and `python3 -m unittest discover -s tests -p 'test_db_targets.py' -v`; inspect that no automatic repair statement appears in generated planning drivers.
- [x] **Step 5 — Checkpoint.** Optional feature commit intentionally omitted; no commit authorization for this feature.

## Task 4: Typed binding and bounded read support

**Files:** Create `scripts/dev_test_bindings.py`, `tests/test_dev_test_bindings.py`; modify `scripts/migration_checks.py`, `scripts/migration_manifest.py`, `scripts/read_only_query.py`; extend `tests/test_migration_checks.py`, `tests/test_migration_manifest.py` and `tests/test_read_only_query.py`.

**Interfaces:** `BindValue = int | str`; frozen `RenderContext(owner: str, run_id: str, objects: Mapping[str, str], values: Mapping[str, BindValue])`; `render_object_tokens(sql: str, context: RenderContext) -> str`; `validate_bindings(values: Mapping[str, BindValue]) -> dict[str, BindValue]`. Add keyword-only `bindings: Mapping[str, int | str] | None = None` to `run_checks` and its internal driver chain. None preserves the current `:target_schema`-only contract. Extend `prepare_query(sql: str, *, bindings: Mapping[str, int | str] | None = None) -> str` and its driver path for bounded selectors, without changing `query` CLI semantics. Reuse `DBMS_SQL.BIND_VARIABLE`; do not replace values into SQL string literals.

- [x] **Step 1 — Write failing binding tests.** `test_opt_in_binds_do_not_widen_migration_defaults` accepts `:DEVTEST_RUN_ID` only with an explicit validated map; ordinary calls still reject it. `test_unicode_quotes_and_comment_payload_are_values` uses Arabic, an apostrophe, newline and `'); DROP TABLE X; --` and checks a single bound value with unchanged parsed SQL. Refuse bool/float/null, unbound names, reserved-name overrides, positional binds and arbitrary namespaces. Use signed JSON-safe integers and strings <= 4,000 UTF-8 bytes. Object-token tests reject unresolved tokens, over-budget identifiers, tokens inside quoted/q-quoted strings or comments, SQLcl ampersand substitution, and fake tokens hidden inside escaped delimiters.
- [x] **Step 2 — Run RED.** Run new bindings tests plus the named new shared-module cases; expected failure is unsupported opt-in binding or missing renderer, with legacy tests still green.
- [x] **Step 3 — Implement narrow opt-in helpers.** Use existing Oracle-aware lexer/tokenization, with a shared lexer helper extraction only if necessary. Add a narrow explicit allowed-bind-name parameter to `migration_manifest.validate_check_query` for the shared driver route; existing manifest loading and ordinary callers retain only `:target_schema`. Bind per cursor and rebind in every fresh session. Bound selectors/observations use the existing conservative SELECT validator, row framing and `DBMS_SQL` machinery; enforce exactly one selector row and declared integer/string columns. Fetch at most `maxRows + 1` for observations and report overflow; do not silently hide rows needed for inventory.
- [x] **Step 4 — Run GREEN.** `python3 -m unittest discover -s tests -p 'test_dev_test_bindings.py' -v`, then full `test_migration_checks.py` and `test_read_only_query.py` discovery runs. Existing migrations still require expected=1 in their manifest loader; DEV suite checks may directly construct `QueryCheck` with another integer, such as 2, without changing `checks.json` schema.
- [x] **Step 5 — Checkpoint.** Optional commit: `feat: support scoped typed bindings for DEV scenario checks`.

## Task 5: Sequential project scenario execution and ambiguous outcomes

**Files:** Create `scripts/dev_test_runtime.py`, `tests/test_dev_test_runtime.py`; extend shared-module tests only where helpers were extracted in Task 4.

**Interfaces:** Produce `read_rows(target: Target, sql: str, context: RenderContext, run: RunEvidence, *, max_rows: int, runner: Callable = run_sqlcl) -> dict` with explicit columns/rows/truncated/identity evidence; `execute_step(target: Target, scenario: Scenario, step: Mapping, context: RenderContext, run: RunEvidence, *, runner: Callable = run_sqlcl) -> dict`; `execute_project_scenario(target: Target, scenario: Scenario, context: RenderContext, run: RunEvidence, *, runner: Callable = run_sqlcl) -> dict`. Each returns a report-schema record, not a boolean. Shared infrastructure failures carry phase/write-started information and evidence paths.

- [x] **Step 1 — Write failing runtime tests.** Fake SQLcl records call order: frozen-input check, durable intent, verified identity/step start, execution, fresh-session checks and inventory observation. `test_exact_error_needs_readback_not_substring` rejects a timeout containing the expected ORA text; expected ORA with matching independent checks passes. `test_crash_between_write_and_observation_is_unknown` proves no automatic second execution. Selector zero/two rows block without step invocation; missing columns, null/float/bool values and Unicode overflow refuse. Unexpected error/failed mandatory assertion fails; missing frames/incomplete execution is UNKNOWN. Optional observation failure is recorded without masking functional failure or inventing cleanup inventory. Controller added regressions for target identity drift, malformed local SQL, and an unexpected runner failure after intent.
- [x] **Step 2 — Run RED.** Initial runtime contract tests failed against missing/stub behavior; controller review later captured a two-case RED for malformed local SQL and generic runner failure (`task-5-controller-red.log`).
- [x] **Step 3 — Implement execution.** Reuse `rollout.validate_sql_script`, its production driver and `sqlcl_session.run_sqlcl`; extract a shared driver extension only for the explicit DEV bind preamble and step-start frame. Preserve `SET SQLBLANKLINES ON`, `SET DEFINE OFF`, current production-target refusals, timeout/byte budgets and platform process handling. Expected errors inspect structured `SqlclError` evidence plus verified start/identity; they do not disable global error recognition. Mandatory checks use Task 4 `run_checks(..., bindings=...)` with fresh sessions. Never commit scenario DML implicitly: reviewed steps own their declared commit behavior. Controller review requires stable identity across selectors, steps, mandatory-check sessions, and observations; local preflight failures return before write intent, while ambiguous runner failures after intent return UNKNOWN.
- [x] **Step 4 — Run GREEN.** Runtime tests: 42 passed. Regression tests: `test_rollout.py` 38, `test_sqlcl_session.py` 11, `test_sqlcl_transport.py` 6, `test_migration_checks.py` 54 passed. Ruff and `py_compile` passed. Fake runners only; no live DB/SQLcl connection. Evidence: `.superpowers/sdd/2026-10-16-dev-test-runner/task-5-controller-review.md` and the `task-5-parent-*.log` files.
- [ ] **Step 5 — Checkpoint.** Optional commit: `feat: execute reviewed DEV SQL scenarios with durable outcomes`.

## Task 6: Built-in datatype and compiler-settings qualification

**Files:** Create `scripts/dev_test_core.py`, `scripts/dev_test_core_types.py`, `tests/test_dev_test_core_types.py`.

**Interfaces:** `builtin_suite() -> Suite` produces the spec's six IDs in their declared display order. `run_builtin(id: str, target: Target, context: RenderContext, run: RunEvidence) -> dict` dispatches only runner-owned built-ins; a project suite using a matching ID is still a project suite. Task 6 supplies `run_datatype_roundtrip(target, context, run) -> dict` and `run_compiler_settings(target, context, run) -> dict`; Task 7 supplies the other dispatch functions. Generate fixtures and frozen derived payloads inside the current run before dispatching each write.

- [x] **Step 1 — Write failing generator tests.** `test_roundtrip_uses_production_formatter_and_catalog` spies on production capture/generator functions and checks BOOLEAN, NUMBER(10,2), NUMBER(*,3), FLOAT(63), VARCHAR2(8 CHAR), RAW(16); exact definitions accept 1 and deliberately wrong definitions reject 0. `test_compiler_settings_execute_generated_query` covers real generated settings SQL including the conjunction repair, null/non-null CCFLAGS, wrong owner/name/type and each wrong setting expectation. Assert generated fixture names fit 30 bytes and no business table data is selected/copied. Extra regressions cover capture failure, metadata identity drift, compiler settings scope drift and generated-input freezing.
- [x] **Step 2 — Run RED.** `python3 -m unittest discover -s tests -p 'test_dev_test_core_types.py' -v`; controller reproduced ignored-capture/unsafe-fixture behavior and a non-NULL-to-NULL mutation regression before fixes. See `.superpowers/sdd/2026-10-16-dev-test-runner/task-6-controller-red-3.log` and `task-6-ccflags-mutation-red.log`.
- [x] **Step 3 — Implement adapters.** Use `schema_catalog.capture_inventory/capture_snapshot` and `baseline._capture` with explicit fixture prefixes/sections when the richer capture is needed; keep any configuration narrowing inside the scratch project/in-memory values. Use `_table_column_type`, `_column_definition_check`, `_structure_bundle` and `_exact_settings_check` rather than reproducing expected SQL. Capture source metadata, generate/execute the copy, then independently capture it. Compiler tests query captured Oracle settings rather than assuming current database defaults. Record core requirements such as CREATE TABLE/PROCEDURE and accessible metadata, without adding grants. Generated SQL is frozen and hashed before dispatch; full catalog identity is retained across captures, checks and compiler settings readback.
- [x] **Step 4 — Run GREEN.** `test_dev_test_core_types.py`: 18 passed; `test_baseline*.py`: 85 passed; `test_schema_catalog*.py`: 37 passed. Ruff, `py_compile` and `git diff --check` passed. Logs: `.superpowers/sdd/2026-10-16-dev-test-runner/task-6-final-*.log`. The Task 7 adapters remain explicitly unavailable behind internal dispatch; no incomplete public command is exposed.
- [x] **Step 5 — Checkpoint.** Reviewed inline; optional commit `feat: add live datatype and compiler-settings scenario adapters` was not made.

## Task 7: Built-in transport, migration and invalid-view qualification

**Files:** Create `scripts/dev_test_core_migrations.py`, `tests/test_dev_test_core_migrations.py`; complete `scripts/dev_test_core.py` dispatch.

**Interfaces:** `run_sqlcl_delivery(target, context, run) -> dict`, `run_migration_success(target, context, run) -> dict`, `run_migration_refusal(target, context, run) -> dict`, `run_invalid_view(target, context, run) -> dict`, all consumed by Task 6 `run_builtin`. Any private adapter refactoring preserves public migration/rollout behavior.

- [x] **Step 1 — Write failing production-path tests.** Delivery asserts fresh rows `{1: 'UPDATED', 2: 'UNCHANGED'}` after a blank line before WHERE. Migration success verifies actual generated nullable-column checks and a validated scratch receipt. Refusal asserts non-success verification, pending event, no receipt and retry stopped before payload execution count can increase. Invalid-view checks observe 1 before mutation and 0 afterward, plus receipt refusal, observed status and bounded ALL_ERRORS; zero error rows never become invented text. Assert all local receipts/events are under the run's scratch project, never the project's migration root.
- [x] **Step 2 — Run RED.** Initial focused test discovery failed because the Task 7 adapter module had not been created; later red investigations exposed incomplete fake catalog coverage and missing explicit review checks for generated guarded DDL.
- [x] **Step 3 — Implement through existing production paths.** Use `rollout.run_rollout(..., repo_root=scratch_project)` for delivery and `migrate.apply_batch` for migration behavior, with a DEV-only scratch confirmation callback tied to the selected suite request. Use production migration loading and receipt/attempt validators. Use baseline `_structure_bundle`, `_code_bundle` where needed and `_view_validity_check`; do not simulate receipt files by writing success JSON. Recheck target before each independently invoked production phase; retain expected-invalid/wrong-definition fixtures and the failure evidence.
- [x] **Step 4 — Run GREEN.** `test_dev_test_core_migrations.py`: 6 passed; `test_migrate_cli.py`: 36; `test_migration_attempts.py`: 4; `test_rollout.py`: 38; `test_baseline_bundle.py`: 15. Ruff, `py_compile` and `git diff --check` passed. All DB/SQLcl paths used offline fakes. Adapter spies verify the actual rollout/migration implementation is exercised.
- [x] **Step 5 — Checkpoint.** Reviewed inline; the optional commit was intentionally omitted because this task has no commit authorization.

Controller review note: standalone rollout SQL scripts require the saved alias to equal `expectedUser` (`docs/migration-rules.md`). Delivery uses that guarded route only after a fresh identity read confirms the alias is the selected DEV database, and repeats the check immediately before execution. A `migrate` rollout step was rejected for this scenario because migration attempt-event files are added during apply and the rollout input snapshot then refuses the generated change; the migration adapters exercise `migrate.apply_batch` directly.

## Task 8: Optional exact-inventory cleanup

**Files:** Create `scripts/dev_test_cleanup.py`, `tests/test_dev_test_cleanup.py`; extend report/evidence tests.

**Interfaces:** `plan_cleanup(run: RunEvidence, values: Mapping[str, str], *, runner: Callable = run_sqlcl) -> dict` loads the frozen inventory/contract and returns exact eligible/refused artifacts without writing; `cleanup_run(run: RunEvidence, values: Mapping[str, str], *, runner: Callable = run_sqlcl) -> dict` performs requested supported cleanup and appends its separate result. Reuse `assert_saved_target` and Task 5 execution/readback helpers; never accept a replacement target argument.

- [x] **Step 1 — Write cleanup tests.** Coverage includes repointed aliases, modified definitions/object IDs, unexpected dependents, incomplete/corrupt inventory, ambiguous pre-creation intent, prefix-only ownership, truncated key capture, unsupported null cleanup, verified absence/idempotence, unrelated/second-run objects, interrupted cleanup, and exact project selector keys.
- [ ] **Step 2 — Run RED.** No Task 8-specific RED log was retained; do not infer one from the passing suite.
- [x] **Step 3 — Implement exact-list cleanup.** Built-ins use frozen reviewed drop contracts, pre-creation absence and observed creation evidence; cleanup rechecks target and ownership, refuses dependents and avoids CASCADE. Project cleanup uses its frozen explicit contract and exact recorded keys. Intent/outcome is durable and absence is read back.
- [x] **Step 4 — Run GREEN.** Cleanup/evidence/report tests and the final full suite passed; the ordinary `--run` path is covered as retaining fixtures without calling cleanup.
- [x] **Step 5 — Checkpoint.** Optional commit omitted; no feature commit was authorized.

## Task 9: Public CLI and shell parity

**Files:** Create `scripts/dev_test.py`, `scripts/dev_test.sh`, `tests/test_dev_test_cli.py`; modify `scripts/team.sh`, `scripts/team.ps1`, `tests/test_team_cli.py`.

**Interfaces:** `build_parser() -> argparse.ArgumentParser`; `main(argv: Sequence[str] | None = None) -> int`; `execute_suite(suite: Suite, target: Target, context: RenderContext, run: RunEvidence) -> dict`. The orchestrator owns built-in/project origin and dependency outcome propagation. A suite-wide identity failure stops all writes; per-scenario blocked/failed dependencies become NOT_RUN while independent eligible scenarios continue.

- [x] **Step 1 — Write CLI/parity tests.** Tests cover all four actions/formats, mutual exclusion, usage, explicit DEV, schema/scenario selection, saved reports without `.env`/SQLcl, cleanup override refusals, wrapper argv/path parity, project `--plan` without execution, retained fixtures, and interrupt identity classification.
- [ ] **Step 2 — Run RED.** No Task 9-specific RED log was retained; do not infer one from the passing suite.
- [x] **Step 3 — Implement parser/action dispatch.** Bash and PowerShell route to one Python CLI, preserve `--schema`, load `.env` only for database actions, and retain exit/interrupt semantics.
- [x] **Step 4 — Run GREEN.** Focused and final full suites passed; help/report work offline; shell syntax and Python compilation passed.
- [x] **Step 5 — Checkpoint.** Optional commit omitted; no feature commit was authorized.

## Task 10: Upgrade-safe project suites and complete documentation

**Files:** Create `docs/dev-test.md`, `docs/dev-test-examples/example.json`, `docs/dev-test-examples/sql/create-and-seed.sql`; modify `scripts/upgrade_template.py`, `template-manifest.json`, `README.md`, `docs/GETTING_STARTED.md`, `docs/known-limitations.md`, `tests/test_template_manifest.py`, `tests/test_upgrade_template.py`; create `tests/test_dev_test_examples.py`.

**Interfaces:** Extend existing `protected_project_path(path: str) -> bool` to protect the complete `dev-tests/` subtree, including suite SQL, during updates/deletions. Do not add a glob to the manifest's exact-path `projectOwned` list; use the established protected-subtree mechanism. Add concrete protected samples to upgrader self-checks. Own new runtime files through existing `scripts/**`, tests through `tests/**`, and add `docs/dev-test.md`/`docs/dev-test-examples/**` to templateOwned. Spec/plan remain templateOnly under `docs/superpowers/**`.

- [x] **Step 1 — Write ownership/example tests.** Tests protect project suite JSON/SQL from manifest ownership and stale lock deletion, verify core runtime retention, validate the shipped example offline, and assert manifest ownership boundaries.
- [x] **Step 2 — Run RED.** Task 10 review RED is recorded in `.superpowers/sdd/2026-10-16-dev-test-runner/task-10-review-red.log`.
- [x] **Step 3 — Implement docs/protection/example.** The guide covers dependencies, privilege/space limits, retention, cleanup, interruption, trust boundaries, and upgrade ownership; the sample uses tagged synthetic rows and no auto-cleanup.
- [x] **Step 4 — Run GREEN.** Example, manifest, upgrader, focused DEV, and final full suites passed; JSON/whitespace checks passed. Live example execution remains Task 12 and is not claimed.
- [x] **Step 5 — Checkpoint.** Optional commit omitted; no feature commit was authorized.

## Task 11: Port the feature contract to 26.1 and complete offline qualification

**Files:** Port the explicit new/modified implementation files from Tasks 1–10 to `/home/ash/projects/APEX_PROJECT_TEMPLATE_26_1`; include the shared spec and plan if committing that repository's feature branch. Preserve all release-specific files/sections not required by this feature.

**Interfaces:** Both repositories expose the same command, suite/report schema, scenario IDs, retention behavior and exit contract. Report the actual release/commit/dirty state. Do not change 26.1 upstream/schemaVersion or adopt 26.2 APEX command behavior while porting common database testing code.

- [x] **Step 1 — Establish the current port boundary.** 26.1 `main` and its local `origin/main` tracking ref were unchanged; the feature branch preserved its independent base and manifest identity. A parity regression covers the six IDs, CLI, result schema, and release metadata.
- [x] **Step 2 — Run RED in 26.1.** The parity and wrapper regressions failed before the port because the command/runtime entry points were absent; no database connection was made for RED.
- [x] **Step 3 — Port reviewed changes.** Shared runner files were copied explicitly; only narrow wrapper/profile/manifest seams were adapted. 26.1 preserves its schemaVersion 1/upstream identity and uses a dedicated legacy-rollout receipt adapter rather than upgrading unrelated rollout formats.
- [x] **Step 4 — Run full offline checks once final source is stable in each repository.** TEAM: 2,139 tests, 35 skipped, 619.389 seconds. 26.1: 1,775 tests, 32 skipped, 434.935 seconds. Both exited 0; compileall, Ruff, Bash syntax, `.env.example` validation and `git diff --check` passed. Full logs and exact feature hashes are in each ignored `scratch/dev-test-implementation-2026-10-16/` directory.
- [x] **Step 5 — Review parity and checkpoint.** The shared source/docs/examples are byte-identical except the reviewed 26.1 migration-delivery adapter and its regression tests. Release manifest identities remain unchanged. Optional commit omitted; no feature commit was authorized.

## Task 12: Real DEV acceptance, final review and delivery evidence

**Files:** Create ignored `scratch/dev-test-implementation-2026-10-16/RESULT.md` and `result.json` plus supporting logs; update only the feature guide/known limitations if newly verified behavior requires a correction. Keep scenario run evidence under the normal `scratch/dev-test/<run-id>/` layout. Do not write NATREC migrations, application changes or production runbooks for this feature qualification.

**Interfaces:** Execute only the public CLI from Task 9 using a named saved owner connection resolved by existing project configuration. Target/schema names in the examples below are placeholders; replace them only after observing the authorized actual DEV identity. Plan approval is not blanket authorization to execute SQL against an unspecified database.

- [ ] **Step 1 — Identify an actual existing DEV owner and record scope.** Inspect configuration without printing credentials; capture branch/input state and actual database/service/container/owner identity with controlled reads. Select only a normal owner already able to run the intended fixtures. Obtain any still-missing authorization for the exact target and fixture writes before running them; do not ask again if that scope is already explicitly authorized. No new users, grants, tablespace quota or production objects are required by the runner.
- [ ] **Step 2 — Exercise read-only planning.** Run `scripts/team.sh dev-test --env dev --schema <OWNER> --plan`. Verify declared prerequisites, target facts, six selected scenarios, retained expected states and exit 0 only for eligible complete coverage. Review the generated plan; verify there are no fixture objects or scenario write events from this action.
- [ ] **Step 3 — Execute the core suite and independently inspect it.** Run `scripts/team.sh dev-test --env dev --schema <OWNER> --run`; do not claim all six passed if some are blocked. Use a fresh connection to verify table definitions, settings, selected/unselected UPDATE rows, successful receipt/event hashes, expected failed receipt absence/retry refusal and deliberately invalid fixture view. Inspect the normal public `--report <run-id>` output without database access. Retain all fixtures by default.
- [ ] **Step 4 — Prove repeated-run isolation and project authoring.** Run the core suite again and verify distinct names plus unchanged first-run fixtures. Copy the shipped example into a project-owned suite directory and explicitly select it; verify exactly two retained tagged synthetic rows. Exercise a selector scenario with one/zero/two matches on controlled fixture records, proving zero/two cases do not mutate data. Never choose arbitrary MIN(ID) business records.
- [ ] **Step 5 — Exercise failure limits without expanding privileges.** Observe a known missing-prerequisite scenario; reproduce a permission/allocation refusal only if feasible within existing authorized fixture capabilities. Exercise a controlled interrupted fixture operation; verify UNKNOWN/exit 2, durable intent and no replay. Preserve unexpected outcomes for investigation; do not weaken an assertion or substitute an administrator to manufacture a pass.
- [x] **Step 6 — Qualify optional cleanup only when requested for a selected run.** No completed live run exists and no cleanup run was selected; cleanup remains unperformed and the default retention policy is preserved. Offline cleanup tests passed.
- [x] **Step 7 — Review both implementations inline and record delivery.** Offline parity, full suites, static checks, working tree limits and the unavailable live target are recorded in the per-repo results. No commit, push, PR or merge was made.

Live qualification applies only to the recorded candidate inputs and actual DEV target. The accepted DEV-clone scope does not require provisioning another database or a platform/version matrix. A single target's results must not be relabeled as proof for a different target/release; unavailable additional coverage remains explicit.

## Spec coverage and task dependencies

| Spec sections | Implementation tasks | Required evidence |
| --- | --- | --- |
| 1–2: outcome, retention, scope and exclusions | 1, 5–10 | Default selection contains only core fixtures; no extra schema, auto-repair or external adapter |
| 3: command/action/selection behavior | 1, 9 | Argument/format/exit and Bash/PowerShell parity tests |
| 4: owner, identity, prerequisite and storage limits | 3, 12 | Missing visibility vs missing prerequisite; no DBA-space requirement; real owner observations |
| 5: six built-ins | 6–7, 12 | Production-path tests plus real fresh-session readback |
| 6: project contract, lookup and typed bindings | 1, 4–5, 10, 12 | Strict JSON/example, bound Unicode, exactly-one selector and declared effects |
| 7: frozen inputs, sequential execution and transaction limits | 2, 4–5, 9 | Durable intent, per-session identities, no implicit persistence claims |
| 8: retention, cleanup, interruption and no replay | 2, 5, 8–9, 12 | Retention success, ownership refusal, unknown write window and optional cleanup outcome |
| 9: file architecture and ownership | 2, 6–10 | Runtime retained after downstream test pruning; project suites protected on upgrade |
| 10: reports/diagnostics/exit/visibility limits | 2–5, 8–9 | Schema validation, expected/actual facts, 2 > 1, redacted summaries and private raw logs |
| 11: release compatibility and docs | 9–11 | Shared public contract, preserved release metadata, complete usage/limitations |
| 12–14: acceptance, before/after and evidence | 11–12 | Current implementation evidence kept separate from historical source observations |

Execution order is 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12. The sequence is deliberate: shared state/binding contracts stabilize before scenario adapters and public wrappers. The latest user instruction authorizes AGY CLI implementers with controller review; no task depends on an unsolicited cross-chat callback.

## Completion record to fill during implementation

- [x] Each Task 1–12 checkbox reflects observed work; missing historical RED logs and incomplete live acceptance remain unchecked, and optional commit/cleanup gates are explicit.
- [x] Both repository candidates, exact feature-file hashes, base SHAs and dirty-state counts are recorded in ignored result files; dirty source is not presented as a committed candidate.
- [x] Both full offline suites and static checks are recorded with skip counts, duration and logs.
- [x] The six live scenario outcomes and project-example outcome are recorded individually as NOT_RUN because the configured DEV connection failed before identity capture; the physical target/owner is explicitly unknown.
- [x] Cleanup qualification remains unperformed and retained data is not treated as an error.
- [x] The ORA-12541 connectivity blocker and unknown target state are recorded; no automatic repair or business-behavior pass is claimed.
- [x] No integration was performed during implementation; commits and local fast-forwards were made only after the user's explicit request.

**Task 12 current status (2026-10-17):** The local 26.2 DEV profile resolves to one development-classified owner and the configured login matches that owner. Two public read-only `--plan` attempts returned ORA-12541 before capturing database identity: `scratch/dev-test/20261017T023222Z-872088e60e974cb2bd0057902350192c/report.json` and `scratch/dev-test/20261017T024707Z-bc6897db08904a6a8db80c83a1c4135e/report.json`. Both have all six scenarios NOT_RUN, no write-intent event and no fixture SQL. In 26.1, the same read-only command refused at configuration because that checkout has no local `.env`; SQLcl was not invoked. The physical DEV identity remains unknown in both checkouts. Resume live acceptance only after the configured endpoint/listener is reachable and the 26.2 read-only plan verifies the actual target; configure 26.1 locally before attempting its plan.

## Plan self-review

Before handing this plan over, verify that every spec requirement maps to the table above, every consumed interface is defined by an earlier task or existing module, and all newly named test files have a creating task. Existing focused test commands must match real files. Product test execution, commits and database operations are implementation work, not evidence produced while authoring this plan.
