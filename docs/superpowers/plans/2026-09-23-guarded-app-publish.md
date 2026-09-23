# Guarded Per-App Publish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a developer or agent publish exact committed APEXlang source into selected shared development apps without overwriting unaccounted teammate Builder work.

**Architecture:** Add a two-stage `prepare-publish`/`publish-app` surface over the existing `capture_app` and `import_app` safety core. A fresh per-app locked-page report and human acknowledgements supplement, but never replace, the baseline/receipt, double-capture, physical mutex and verified re-export guards. All selected apps preflight before any import; writes are sequential and failures retain app-scoped recovery evidence.

**Tech Stack:** Python 3.10+, unittest, SQLcl read adapter, APEX 26.1+ Page Locks metadata, local `.sync-state/` records.

**Spec:** `docs/superpowers/specs/2026-09-23-simple-multi-app-team-workflow-design.md`

## Global Constraints

- Requires completion of `docs/superpowers/plans/2026-09-23-multi-app-bindings.md`.
- APEX **26.1 or newer** and APEXlang only; import is whole-application.
- A pause covers exactly the selected aliases; acknowledgements are human statements, not machine proof.
- Locked-page status is informational when known; `UNKNOWN` is never displayed as zero locks and cannot be silently overridden.
- The tool never unlocks pages, auto-imports on Git pull, or issues an all-clear after partial/unknown import.
- Exact committed source, target identity, verified baseline/receipt, double capture, per-app mutex and verified re-export remain mandatory.
- Production writes remain refused; `.sync-state/` evidence is durable; no automatic Git commit/push.

## Review Focus

1. A page locked by the importer is still reported with its owner, not filtered out (Task 1 test).
2. A missing/inaccessible lock view produces `UNKNOWN` and a refusal, not `none` (Task 1 test).
3. A stale prepared commit, binding or app generation refuses before the first import (Task 2 test).
4. A change to the second selected app after preparation stops **both** imports before any write (Task 3 test).
5. If the second import becomes unknown after the first verifies, both selected apps stay paused and no all-clear is drafted (Task 3 test).

## File map and sequence

- Create `scripts/teamlib/page_locks.py`: read-only automatic and reviewed-manual lock report parsing.
- Create `scripts/teamlib/apex_validate.py`: offline SQLcl `apex validate` for the exact selected Git APEXlang tree.
- Create `scripts/teamlib/publish.py`: prepared record, acknowledgement validation, all-app preflight, ordered import and result journal.
- Modify `scripts/team.py`: `prepare-publish` and `publish-app` public commands; retire public direct `import-app` dispatch so it cannot bypass preparation. Keep `teamlib.apex.import_app` as the guarded low-level operation.
- Modify `scripts/teamlib/announce.py`: app-scoped pause notice and verified-only all-clear draft.
- Add `scripts/tests/test_page_locks.py`, `scripts/tests/test_apex_validate.py`, `scripts/tests/test_publish.py`; extend existing import, command and production tests.
- Update `docs/import-pause.md`, `docs/app-recovery.md`, `.agents/workflows/team-flow.md` with the real command behavior.

### Task 1: Read and display page-lock ownership

**Files:** Create `scripts/teamlib/page_locks.py`, `scripts/tests/test_page_locks.py`; modify `docs/import-pause.md`.

**Interfaces:** `PageLock(page_id: int, page_name: str | None, locked_by: str, locked_on: str | None, comment: str | None)` and `LockReport(alias: str, app_id: int, status: Literal["KNOWN", "UNKNOWN"], pages: tuple[PageLock, ...], source: str)`. `read_page_locks(target: Target, runner=run_sqlcl) -> LockReport` is read-only; `load_manual_page_locks(path, target) -> LockReport` validates an operator-reviewed JSON fallback.

- [ ] **Step 1: Write failing unit tests** with fake SQLcl results: two locks for HR with distinct owners, zero rows as `KNOWN`/empty, missing view or malformed rows as `UNKNOWN`, and a lock held by the current user retained. A manual report must bind `target.state_key`, alias, app ID, reviewed-by name, UTC capture time, and explicit page list; reject stale (>5 minutes), unknown fields and mismatched targets. Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -m unittest scripts.tests.test_page_locks -v`; expect import/function failures.
- [ ] **Step 2: Discover the live metadata contract through a qualified non-production APEX read profile**, if available. Inspect `APEX_DICTIONARY` for `APEX_APPLICATION_LOCKED_PAGES` and required owner/page columns, then record the observed column names and APEX version in a test evidence note under `scratch/` (ignored). Do not run direct unqualified SQLcl or print saved-connection secrets. If unavailable, implement the adapter to return `UNKNOWN` and leave live evidence explicitly unavailable; never guess columns from a blog.
- [ ] **Step 3: Implement the adapter** with only whitelisted dictionary-discovered column names and numeric `target.app_id`. The required canonical fields are `page_id` and `locked_by`; `page_name`, `locked_on`, and `comment` are optional and render as `-` when the view lacks them. Reject duplicate page IDs, negative IDs, control characters, cross-app rows and failed identity/completion records. Render one block per selected alias, including `No locked pages reported` only for `KNOWN`/empty.
- [ ] **Step 4: Implement the manual fallback** accepted only when an operator supplies a recent JSON report that explicitly lists `version: 1`, `target_state_key`, `alias`, `app_id`, `captured_at_utc`, `reviewed_by`, and `pages`; it is labeled `MANUAL BUILDER REVIEW`, never masquerades as an automated query. The operator is responsible for transcribing the APEX Builder Page Locks report. Document this limitation and the location of the Page Locks UI.
- [ ] **Step 5: Run** the targeted test, `ruff check scripts/`, `git diff --check`; commit only Task 1 files with `git commit -m "feat: report page locks before app publish"`.

### Task 2: Prepare an exact, app-scoped pause

**Files:** Create `scripts/teamlib/apex_validate.py`, `scripts/tests/test_apex_validate.py`, `scripts/teamlib/publish.py`, `scripts/tests/test_publish.py`; modify `scripts/team.py`, `scripts/teamlib/announce.py`, `scripts/tests/test_announce.py`, `docs/import-pause.md`.

**Interfaces:** `validate_apexlang_tree(tree: Mapping[str, bytes], *, sqlcl_bin: str = "sql") -> ValidationReport` materializes only the selected tree and runs SQLcl `apex validate` with `/nolog`; it requires the explicit success marker and retains diagnostics without credentials. `prepare_publish(repo: Path, targets: tuple[Target, ...], source_commit: str, lock_reports: Mapping[str, LockReport], store: ControlStore | SqlControlStore, *, runner=run_sqlcl, validator=validate_apexlang_tree) -> Preparation`; `Preparation` stores `version`, sorted unique aliases, source commit, each `target.state_key`, observed tree digest/generation, lock report, roster and `prepared_at_utc` under `.sync-state/publish/<id>/prepare.json`. CLI: `scripts/team.sh prepare-publish hr payroll --ref <40-hex-commit> [--manual-lock-report hr:<json-path>] [--manual-lock-report payroll:<json-path>]` prints selected app notices and locks; it performs no Builder write.

- [ ] **Step 1: Write failing tests** proving omitted/duplicate aliases, dirty selected source, non-development targets, missing verified baseline **and** missing reviewed recovery receipt, stale source ref, `UNKNOWN` lock report, invalid APEXlang and missing selected app all refuse. An explicit reviewed `--replace-from alias:capture-id` may account for an app without a baseline; it must bind the observed tree and the selected commit. Assert preparing HR does not include Payroll in notice or record. In `test_apex_validate.py`, fake SQLcl output must distinguish `Validation successful` from compiler errors, warnings without success and an exit-zero failure. Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -m unittest scripts.tests.test_apex_validate scripts.tests.test_publish -v`; expect failure.
- [ ] **Step 2: Implement offline APEXlang validation and preparation**. Materialize each exact Git tree in a fresh ignored temporary directory, run `apex validate -input <directory>` with SQLcl `/nolog`, require `Validation successful` and no compile-error markers, retain the validation log, and delete only the owned temporary directory. Never use a connected database to validate a locally edited tree. Then capture selected Builder apps read-only, compute `tree_digest`, read baseline or exact reviewed replacement capture/receipt and registry, and write a new non-secret JSON record atomically under `.sync-state/publish/<uuid>/`. Include the validation result and lock report exactly as displayed. An existing path or symlink is a refusal. Derive a canonical SHA-256 record digest from the JSON; do not trust a caller-supplied digest.
- [ ] **Step 3: Wire CLI preparation and notices** through `team.py`; every alias must be configured and unique. Print a per-app roster, observed-versus-baseline finding, selected commit, and lock-owner table. Do not mark the app paused in metadata merely because a notice was printed; the human posts it. Keep `announce-import` only if it has a distinct recovery use, and document that it is not publish authorization.
- [ ] **Step 4: Run** `scripts.tests.test_apex_validate`, `scripts.tests.test_publish`, announcement and public help tests, then `ruff check scripts/` and `git diff --check`. Commit only Task 2 files with `git commit -m "feat: prepare selected app publish with durable evidence"`.

### Task 3: Require acknowledgements and preflight all apps before import

**Files:** Modify `scripts/teamlib/publish.py`, `scripts/team.py`, `scripts/teamlib/announce.py`, `scripts/tests/test_publish.py`, `scripts/tests/test_import_app.py`, `scripts/tests/test_production_boundary.py`, `docs/app-recovery.md`, `docs/import-pause.md`.

**Interfaces:** `publish_prepared(repo: Path, preparation_id: str, acknowledgements: Mapping[str, tuple[str, ...]], confirm_pause: bool, *, config: Config, store: ControlStore | SqlControlStore, runner=run_sqlcl, lock_reader=read_page_locks) -> PublishReport`; CLI: `scripts/team.sh publish-app --prepared <id> --confirm-pause --ack hr:<omar-checkout-uuid> --ack payroll:<layla-checkout-uuid>`. Every registered checkout for each selected alias must be acknowledged by its exact recorded checkout UUID; the operator may also name an unregistered active editor. No tool fabricates acknowledgements. `PublishReport` maps every selected alias to `VERIFIED`, `UNCHANGED`, or `UNKNOWN` and retains recovery paths.

- [ ] **Step 1: Write failing tests** for absent pause confirmation, one missing registered acknowledgement, acknowledgement for an unselected alias, edited preparation JSON, changed target binding, changed selected commit, changed first/second app capture, and `UNKNOWN` lock report. Assert the fake runner receives **zero write calls** in each refusal. Run the targeted test; expect failure.
- [ ] **Step 2: Implement the all-app preflight**: reload and validate the canonical preparation record, configured targets, exact commit and selected trees; require acknowledgements for every registered checkout in each selected app; recapture every app and compare against preparation/baseline-or-receipt; refresh every selected lock report. Any difference or unavailable report refuses before `import_app` is invoked. Use no global team pause or cross-app mutex.
- [ ] **Step 3: Write a second failing test** where HR import verifies, Payroll import times out, and Payroll's state is `UNKNOWN`. Assert no all-clear, a durable per-app result journal, and explicit status for HR and Payroll. Add a success test where both verify and only then a verified-only all-clear draft is available.
- [ ] **Step 4: Implement ordered imports** by calling the existing `import_app` once per selected app with its exact commit and reviewed recovery receipt. Let `import_app` keep its own physical mutex, double capture and post-import equality; do not duplicate or weaken those checks. Catch a later failure, persist the precise partial report, retain selected-app pause, and never auto-rollback or auto-unlock. Replace public `import-app` dispatch with `publish-app`; recovery `--replace-from` must pass through the same preparation and acknowledgement gate.
- [ ] **Step 5: Run** `scripts.tests.test_publish`, `scripts.tests.test_import_app`, `scripts.tests.test_production_boundary`, full unittest discovery, `ruff check scripts/`, `bash -n scripts/*.sh`, and `git diff --check`. Commit only Task 3 files with `git commit -m "feat: guard sequential selected-app publish"`.

### Task 4: Exercise the two-developer refusal path

**Files:** Modify `scripts/teamlib/local_team_e2e.py`, `scripts/tests/test_local_team_e2e.py`, `docs/local-three-developer-e2e.md`.

**Interfaces:** Existing disposable fixture lifecycle; no production or shared user app IDs are allowed.

- [ ] **Step 1: Add a failing fixture test**: Alice changes HR APEXlang, Bob exports a saved HR Builder page, Carol keeps working in a separate selected-out app. HR preparation sees Bob's lock owner; a changed HR capture refuses before import; after Bob's source is reconciled and his acknowledgement is recorded, publish verifies HR while Carol's app generation is unchanged.
- [ ] **Step 2: Extend the fixture only inside its owned non-production target**. Keep HR as app `9099`; reserve Payroll fixture app `9100` only after read-only preflight proves that ID absent, the workspace/schema assignment qualified, and seed app `103` untouched. Give each created fixture its own run-ID ownership marker; cleanup may delete only a fixture whose marker and physical identity still match. Preflight must recheck run ID, both app IDs, workspace/schema ownership and saved connections before any test write. Where an APEX Page Locks query or browser adapter is unavailable, record `UNKNOWN` and do not claim that coverage passed.
- [ ] **Step 3: Run** the local E2E unit test and any available protected disposable live run. Separate source/fake-runner PASS from live SQLcl, Builder-page-lock and browser evidence. Commit only Task 4 files with `git commit -m "test: exercise guarded two-developer publish"`.

**Handoff:** Report which selected apps paused, which verified, which remained unchanged/unknown, and whether page locks came from a qualified APEX query or a labeled manual Builder report. Unsaved Builder edits and arbitrary DML remain outside tool visibility.
