# Simple Operator Guides and Command Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the implemented team workflow learnable from a short README and consistent agent instructions, and remove obsolete public mechanisms rather than merely hide them.

**Architecture:** Documentation is an executable contract: examples use only commands introduced and tested by the three preceding plans. Keep the root README to start-here, three daily paths, guarded publish and independent release examples; link to advanced runbooks. Audit every public command against a named unique use and delete obsolete dispatch/tests/docs after replacement behavior is proven.

**Tech Stack:** Markdown, AGENTS.md, Python unittest documentation checks, shell launcher help.

**Spec:** `docs/superpowers/specs/2026-09-23-simple-multi-app-team-workflow-design.md`

## Global Constraints

- Run this after `2026-09-23-multi-app-bindings.md`, `2026-09-23-guarded-app-publish.md`, and `2026-09-23-independent-app-releases.md` have passing offline evidence.
- APEX **26.1 or newer** and APEXlang only; Builder, manual files and coding agents are first-class authoring routes.
- The normal Builder loop is export/review/commit; file-first publish is explicit and app-scoped.
- A pause affects only selected apps; locked-page reports are informational; unsaved Builder edits remain unobservable.
- Shared schema migrations release once per environment; app releases select one app and check prerequisites.
- No automatic Git commit/push, silent import/unlock, or production write.
- Keep advanced safety/recovery internals accessible through clear runbooks; remove only proven duplicate public mechanisms.

## Review Focus

1. A newcomer must not mistake a page-lock-free report for proof of no unsaved Builder work (Task 1 test).
2. A README example must not name a command, flag or target binding absent from the implemented CLI (Task 1 test).
3. An agent asked to edit APEXlang must not import until the same prepare/acknowledge/publish gate is satisfied (Task 2 test).
4. A retired direct import or all-app release command must not remain reachable through shell/PowerShell launchers (Task 3 test).
5. Cleanup must not remove `.sync-state/` recovery or another person's untracked file (Task 3 test).

## File map and sequence

- `README.md`: short start-here and copyable examples.
- `AGENTS.md`, `.agents/workflows/team-flow.md`: matching human/agent contract.
- `.env.example`: final local binding explanations.
- `docs/{import-pause,app-recovery,migrations,promotion,ci,toolchain}.md`, `docs/working-on-apex-together.html`: advanced details and plain-language guide.
- `scripts/team.py`, `scripts/teamlib/config.py`, `scripts/teamlib/release.py`: remove retired public paths after replacement tests.
- `scripts/tests/{test_docs,test_launchers,test_production_boundary,test_release,test_publish}.py`: command/docs regressions.

### Task 1: Replace the root README with a genuinely short start-here

**Files:** Modify `README.md`, `.env.example`, `scripts/tests/test_docs.py`.

**Interfaces:** Documentation-only; examples call the public CLI of the completed preceding plans.

- [ ] **Step 1: Write failing docs tests** asserting README contains one same-schema and one split-schema `.env` example, `doctor`, Builder `export-app`, file-first `prepare-publish`/`publish-app`, an HR-only pause example, schema-then-HR release, and the unsaved-Builder caveat. Replace the existing blanket `no import step` assertion with `no import in the normal Builder-first loop`; file-first publish is allowed only through the guarded route. Assert every fenced `scripts/team.sh <command>` maps to `COMMAND_HELP` or the parser and that root README links resolve. Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=scripts python3 -m unittest scripts.tests.test_docs -v`; expect missing content failures.
- [ ] **Step 2: Rewrite README around this order:** (1) what is shared; (2) copy `.env.example` to ignored `.env`, saved connection names and `doctor`; (3) Builder-first three commands; (4) file-first edit/commit/prepare/acknowledge/publish; (5) shared migration once, independent HR release; (6) `ask an agent` example using exactly the same CLI and no bypass; (7) links for recovery/CI/production. Include a literal example where HR pauses and Payroll continues, and one where HR+Payroll both pause. Keep advanced evidence formats in linked runbooks, not the opening screen. The central example must look like:

  ```bash
  cp .env.example .env
  scripts/team.sh doctor
  # Builder-first: capture the shared app after saving in Builder.
  scripts/team.sh export-app hr
  git status --short --untracked-files=all -- apps/hr/
  git add -- apps/hr/
  git diff --cached -- apps/hr/
  git commit -m "Capture reviewed HR Builder changes"
  # On a later file-first change, edit apps/hr/, review it, then commit it.
  # Publish only that reviewed commit into the shared Builder app:
  scripts/team.sh prepare-publish hr --ref HEAD
  # Post the printed HR pause, obtain Omar's acknowledgement, then use its ID:
  scripts/team.sh publish-app --prepared PREPARATION_ID --confirm-pause --ack hr:OMAR_CHECKOUT_UUID
  ```
- [ ] **Step 3: Use copyable examples, not fictitious automated acknowledgements.** The publish example prints `prepare-publish` output, has Omar explicitly supply his checkout acknowledgement, then runs `publish-app --prepared <printed-id> --confirm-pause --ack hr:<Omar's registered checkout UUID>`. Mark the angle-bracket values as output to copy, not shell literals. State that a lock report is informational and that a failed before-check stops for export/reconciliation.
- [ ] **Step 4: Run** docs tests, `python3 scripts/team.py --help`, Markdown link check via `test_docs`, and `git diff --check`; commit only Task 1 files with `git commit -m "docs: make the team README a simple start-here guide"`.

### Task 2: Align agent instructions and runbooks with the same route

**Files:** Modify `AGENTS.md`, `.agents/workflows/team-flow.md`, `docs/import-pause.md`, `docs/app-recovery.md`, `docs/migrations.md`, `docs/promotion.md`, `docs/ci.md`, `docs/toolchain.md`, `docs/working-on-apex-together.html`, `scripts/tests/test_docs.py`.

**Interfaces:** Documentation-only; the agent contract is the same public command contract as README.

- [ ] **Step 1: Write failing docs tests** asserting `AGENTS.md` contains `APEX 26.1+`, `APEXlang`, Builder/manual/agent routes, app-scoped pause, teammate acknowledgement, locked-by report, before/after check, selected-app-only release, production refusal and `.sync-state/` durability. Assert current operator docs do not say all apps pause for one import or all apps deploy for one app release. Run `scripts.tests.test_docs`; expect failure.
- [ ] **Step 2: Update AGENTS.md** to state: Builder edits -> `export-app` -> review/commit; manual or agent APEXlang edits -> exact commit -> `prepare-publish` -> named acknowledgements -> `publish-app`; schema migration -> qualified drift/metadata path; release -> schema once then selected app. Agents may run requested daily commands but cannot invent teammate acknowledgement, silently commit/push/import, clear locks, or claim unavailable live checks passed.
- [ ] **Step 3: Update each runbook at its owning boundary.** `docs/import-pause.md` explains selected-app pause, lock report and partial multi-app state; `docs/app-recovery.md` explains retained evidence and no blind retry; `docs/migrations.md` explains shared TABLES/CODE stream; `docs/promotion.md` and `docs/ci.md` explain kind-bound archives/evidence and independent app release; `docs/toolchain.md` explains APEX 26.1+ and profile qualification; the HTML guide mirrors the two-teammate example. Keep historical `docs/superpowers/` files unchanged.
- [ ] **Step 4: Run** docs tests, full unittest discovery, `ruff check scripts/`, `git diff --check`. Commit only Task 2 files with `git commit -m "docs: align agents and runbooks with simple team flow"`.

### Task 3: Remove proven duplicate public routes and verify no safety regression

**Files:** Modify `scripts/team.py`, `scripts/teamlib/config.py`, `scripts/teamlib/release.py`, `scripts/tests/test_launchers.py`, `scripts/tests/test_docs.py`, `scripts/tests/test_production_boundary.py`, `scripts/tests/test_release.py`, `scripts/tests/test_publish.py`.

**Interfaces:** Retain `teamlib.apex.import_app` and recovery internals; public import is only `publish-app`. Keep release verification and owner handoff commands, but no all-app/all-migration `build-release` mode. Remove public `announce-import` once `prepare-publish` and verified all-clear drafting cover its only behaviors.

- [ ] **Step 1: Inventory `python3 scripts/team.py --help` into a review table** with each command classified `daily`, `protected/recovery`, or `duplicate`. Verify through `rg` that `announce-import` has no distinct caller after the publish plan, direct `import-app` has no public dispatch, and old all-in-one `build-release` has no caller after the release plan. Preserve `plan-release`, `apply-release`, `recover-*`, replay and migration lifecycle commands until their distinct safety or owner use has been independently replaced; do not delete them for a smaller help count.
- [ ] **Step 2: Write failing route tests**: invoking `import-app` or `announce-import` through `team.py`, `team.sh` and `team.ps1` must return unknown-command/help and perform no SQLcl write; `build-release` without `--kind` must refuse. The replacement `prepare-publish`, `publish-app`, schema release and app release remain reachable. Run `scripts.tests.test_launchers` and `scripts.tests.test_publish`; expect failures.
- [ ] **Step 3: Remove the duplicate dispatcher/parser/help branches** and their obsolete tests/docs, but retain shared announcement formatting used by the new publish flow. Remove dead all-in-one release packaging code only after the kind-bound builder tests pass. Run `rg -n 'announce-import|import-app|all configured applications' README.md AGENTS.md docs scripts/team.py` and inspect each remaining hit: historical documents and explanatory references are fine; current instructions must not offer a bypass.
- [ ] **Step 4: Run** full unittest discovery, `ruff check scripts/`, `bash -n scripts/*.sh`, PowerShell launcher tests where PowerShell is installed, `python3 scripts/team.py --help`, `git diff --check`, and a status check showing `.sync-state/` and the unrelated untracked local E2E plan untouched. Commit only Task 3 files with `git commit -m "refactor: remove duplicate import and release routes"`.

**Handoff:** Show a one-page beginner route and a separate advanced-recovery route, the before/after command inventory, exact tests run, any unavailable protected/PowerShell/browser evidence, and confirmation that no production write or shared Builder import was made during documentation cleanup.
