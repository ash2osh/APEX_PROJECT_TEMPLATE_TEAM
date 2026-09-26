# Full Review Findings Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve the 14 confirmed findings in the review of commit `abc98c7`, verify the applicable database assumptions read-only against `docker-demo`, and leave `main` committed and pushed.

**Architecture:** Fix each defect in its owning shell, PowerShell, SQL, Python, documentation, or CI component, with regression coverage in the established `unittest` suite. Keep database verification read-only and use offline/fake SQLcl for workflow tests; no shared database imports, migrations, exports, or other writes are part of this work.

**Tech Stack:** Bash, PowerShell 5.1-compatible scripts, SQLcl/Oracle SQL, Python `unittest`, GitHub Actions.

**Spec:** `/home/ash/.codex/artifacts/reviews/APEX_PROJECT_TEMPLATE_TEAM-abc98c7.md` and repository `AGENTS.md`.

## Global Constraints

- This canonical template supports APEX 26.1+ and APEXlang only.
- App publishing must pause only selected apps, reconcile Builder state, and refuse production writes.
- Migrations and release actions must keep their existing target guards; no live write is allowed in verification.
- Do not claim unavailable live checks passed; mark them `UNKNOWN`.
- Preserve checkout-authored acknowledgement, durable recovery, and evidence truth requirements.
- Do not exchange commits between downstream developer repositories or infer absent remote state.
- The requested integration target is `main`; the user explicitly authorized committing and pushing it.

## Review Focus

- Reparse, SQLcl client, and APEX command failures must not report success or commit failed migration DML.
- Symlinks, junctions, wildcard characters, spaces, BOMs, `$`, and `#` in valid paths/configurations must not escape a repository or break valid workflows.
- Each scanner must distinguish supported Oracle syntax and comments from identifiers or executable payloads.
- A verified publish must advance only its own correct drift baseline, while uncertainty retains the refusal.
- CI must execute the behavioral suite on supported platforms.

---

### Task 1: SQLcl failure semantics and migration text handling

**Files:**
- Modify: `scripts/migrate.sh`, `scripts/migrate.sql`, `scripts/publish_app.sh`, `scripts/publish_app.ps1`, `scripts/publish_app.sql`, `scripts/deploy.sh`, and SQL driver files using `SQL.SQLCODE`
- Test: `tests/test_migrate_cli.py`, `tests/test_publish_cli.py`, `tests/test_team_cli.py`, and `tests/test_sql_driver_contracts.py`

**Interfaces:** Migration runner continues to accept repository-relative `migrations/<developer>/<file>.sql` inputs. Publish wrappers may report success only after SQLcl exits successfully, emits no client error, and its driver reaches a verification sentinel.

- [ ] Write regression tests for stable nonzero Oracle failures, migration rollback/success commit directives, literal ampersands in migration content, and SP2/client-error publishes.
- [ ] Run the focused tests and confirm each new regression fails against the current implementation.
- [ ] Resolve the migration file path before executing it with SQLcl substitution disabled; use explicit failure rollback and success commit.
- [ ] Change all SQLcl drivers to stable success/failure exit statuses and make both publish wrappers reject SQLcl client diagnostics or a missing post-import sentinel.
- [ ] Run the focused and full unittest suites; confirm each regression passes.
- [ ] Commit as `fix: fail closed on SQLcl and migration errors`.

### Task 2: Configuration parsing and PowerShell filesystem boundaries

**Files:**
- Modify: `scripts/load_env.sh`, `scripts/invoke_sqlcl.ps1`, `scripts/replace_mirror.ps1`
- Test: `tests/test_team_cli.py`, `tests/test_export_cli.py`, and mirror replacement tests

**Interfaces:** Keep the existing environment variable names and SQLcl invocation API. Mirror destinations must be verified as physically contained in the repository before moves/deletes.

- [ ] Add failing fixtures for `$`/`#` Oracle identifiers, UTF-8 BOM profiles, a checkout containing brackets, and a destination ancestor that is a symlink to an external directory.
- [ ] Run the focused tests and confirm they expose the parser, launch, or containment failures.
- [ ] Make the Bash parser accept the same identifiers and BOMs as PowerShell, launch SQLcl from literal paths, and reject reparse-point/symlink destination ancestors before replacement.
- [ ] Run the focused and full unittest suites plus PowerShell parser checks; confirm the external fixture remains untouched.
- [ ] Commit as `fix: harden cross-platform path and config handling`.

### Task 3: Backup spool paths for valid Oracle schema names

**Files:**
- Modify: `scripts/backup_db.sql`, `scripts/backup_db.sh`, `scripts/backup_db.ps1`
- Test: backup workflow tests in `tests/`

**Interfaces:** Database mirrors remain under `database/<configured-schema>/`; only staging/spool paths may use an encoded filesystem-safe schema component.

- [ ] Add failing Bash and PowerShell workflow fixtures using a schema containing `$` and assert staged files are installed under the original schema spelling.
- [ ] Run the focused tests and observe the raw schema path failing in the fixture.
- [ ] Map the configured schema to a collision-free spool directory for every object file and manifest, then relocate verified scope output before replacing mirrors.
- [ ] Run both focused fixtures and the full unittest suite; confirm object counts and final mirror paths match.
- [ ] Commit as `fix: spool backups for dollar schemas`.

### Task 4: Scanner correctness, active documentation, and CI coverage

**Files:**
- Modify: `scripts/check_conflicts.py`, `scripts/graphify_apexlang_extractor.py`, `app_context/README.md`, `.github/workflows/database-checks.yml`
- Test: `tests/test_check_conflicts.py`, a new extractor test module, and `tests/test_documentation_contract.py`

**Interfaces:** Preserve the extractor's current node/edge JSON contract and the migration conflict check's current CLI/output contract. Documentation must describe only guards present in current code.

- [x] Add failing tests for `CREATE SEQUENCE IF NOT EXISTS`, comment backticks before real APEXlang fences, stale alias/release claims, and CI behavioral test invocation.
- [x] Run the focused tests and confirm each finding is detected.
- [x] Parse optional sequence syntax, ignore comment fence delimiters while retaining real payload behavior, align active docs with current numeric app/migration paths, and add unittest discovery to CI.
- [x] Run focused tests and full unittest suite (61 tests); confirm extractor parser output contracts remain unchanged.
- [x] Commit as `fix: align scanners docs and CI with supported workflows` (`952a9d8`).

### Task 5: Refresh the development Builder baseline after verified publish

**Files:**
- Modify: `scripts/publish_app.sh`, `scripts/publish_app.ps1`, `scripts/deploy.sh`, `scripts/verify_publish_state.py`, `README.md`, `AGENTS.md`, `.agents/workflows/team-flow.md`
- Test: `tests/test_publish_cli.py`, `tests/test_team_cli.py`, `tests/test_verify_publish_state.py`, `tests/test_documentation_contract.py`

**Interfaces:** A successful development publish records a baseline for the exact app/source revision it imported. Failed imports or uncertain live revisions must not advance the baseline.

- [x] Add failing fake-SQLcl tests where import advances the live app revision and a second publish must pass, plus failure/race cases that must preserve refusal.
- [x] Run the focused tests and confirm the current publish leaves a stale baseline or reports success after an unverified post-import source.
- [x] Re-export after the import sentinel, compare exact APEXlang file sets and bytes, and record the post-import DEV revision only after the export stayed stable and unambiguous.
- [x] Run focused and full unittest suites; confirm Bash and PowerShell first publishes advance the baseline and failed/uncertain cases do not.
- [x] Commit as `fix: advance Builder baseline after verified publish`.

## Final verification and delivery

- [ ] Run the repository's complete supported checks, inspect the final diff, and obtain a fresh whole-change review.
- [ ] Fix any critical or important review issue and rerun its regression plus the full suite.
- [ ] Commit any final review fixes, verify `main` is clean, and push the requested branch.
- [ ] Report the `docker-demo` read-only checks, tests, commit SHA, push result, and any unavailable checks as `UNKNOWN`.
