# Walkthrough 2026-11-19

Branch `codex/review-fixes-2026-11-19` in the 26.2 template, on top of `main`. Commits in this day's range (oldest first): `8128be7` shared-core sync, `2add258` backup scopes, `56c1eea` interrupt-test hardening, `4a4ebab` partial publish hashes the live export once, `2f815d6`, `b8a630d`, `a1dd180`, `b4a451c`, `d346ca7`. The commit that adds this file follows them.

## 1. Partial publish lint fix (2f815d6)

- **Why:** `pre_push_check.py` failed on Ruff B023 in `tests/test_partial_publish.py`. The counting closure in the committed test (from `4a4ebab`) read loop variables.
- **What changed:** a small factory `counting_hashes(counted, real)` builds each counter. The assertions are unchanged.
- **Verified:** `ruff check` clean; `test_partial_publish` 24 tests OK.

## 2. Offline `targets` command (b8a630d)

- **What:** `scripts/team.sh targets` and `scripts/team.ps1 targets` print, offline, the schema, SQLcl connection and expected login each operation resolves to, from the shared `scripts/show_targets.py`. Documented in the README command reference, GETTING_STARTED step 6 and the cheat sheet, and `docs/migration-rules.md`.
- **Defect found and fixed during review:** without a `.env`, `targets` exited 1 ("configuration file not found"), while `template-status` works without one. Both wrappers now run the report with no `.env`: every row reads `not configured`, SQLcl never starts, and extra arguments or `--schema` exit 2, as `template-status` does. With no `.env`, the wrappers pass an empty environment, so an inherited `CODE_SCHEMA` is not reported as configuration.
- **Parity:** with a `.env`, the bash and PowerShell outputs (text and JSON) were byte-identical on PowerShell 7.7 (Linux).
- **Tests added:** a no-`.env` test in both wrappers, covering status, `SQLcl not started`, the inherited-variable case and argument status.
- **Known, not fixed (core):** `show_targets.py` sets the argparse program name to `scripts/team.sh targets`, so PowerShell usage errors also say `team.sh`.

## 3. Getting started: verify-local after the exports (a1dd180)

- **Why:** `verify-local` reports each configured app with no folder as `[FAIL]` until it is exported. Step 7 ran it before the first export, so a correct setup looked broken. Step 8 exported only app 100 while `APEX_APP_ID` listed 100 and 200.
- **What changed:** step 7 runs `targets`, then `doctor`, and says that `verify-local` comes at the end of step 8. Step 8 exports every listed app, adds a DEV descriptor for each, runs `verify-local`, and then commits the exports and descriptors together. The commit used to come before the descriptors.
- **TROUBLESHOOTING:** a new "Local readiness" section covers the `verify-local` lines a new developer can hit, so the step can point to a fix.
- **Tests:** no existing test pinned the old order. A contract test now pins the new one.
- **Verified:** a scratch copy with a `.env` (outside the repo) showed the facts used here: `targets` resolves the example; `verify-local` fails the two configured apps and reports `template_lock` as FAIL because a template repository has no lock.
- **Not verified:** a real downstream project with `.template-lock.json` and exported apps. The step says "expect PASS for every configured application", not "all PASS".

## 4. README: daily choices and moved workflow rules (b4a451c)

- **What:** a "Daily choices" section with five one-sentence bullets, each linking to the document that owns the procedure. Builder-first and file-first sections shortened to summaries that link to GETTING_STARTED 9A/9B and publish-rules.
- **Moved before shortening (no statement lost):**
  - GETTING_STARTED 9A: the `git status --short --untracked-files=all` command, that export does not import the app back, the uncommitted-file refusal, and the `scratch/` sequence.
  - publish-rules.md: the export re-check of `last_updated_on`; the time-zone and one-second behaviour; the tag counter and never-reused rule; the `#APP_VERSION#` note; that APEX does not stamp `last_updated_on` on import; the normalization sort keys and exclusions; the cosmetic summary line; the first-differing-line report; the `sourceProvenance` condition; the `Import successful.` requirement; the automation before/after table; and the one-line failure rule after import.
  - Checked: a normalized sentence search found 47 sentences from the removed text, 4 verbatim in an owner document and 43 not. Each of the 43 was checked by hand against its owner; all are covered, reworded or moved.
- **Line counts:** README 1077 before, 1013 after. The command reference is unchanged.
- **Links:** all 43 distinct README link targets resolve, including the five Daily choices links and the `#a-builder-first`, `#b-files-first` and `#how-the-check-works` anchors.
- **Test change:** the contract test that asserted the moved phrases now reads the owning documents. The phrases it checks are the owners' wording. The `readme_section` helper still covers "Command reference".

## 5. TODO inventory status (d346ca7)

Each status was checked in code and test before it was written.

- **Landed:** P1 concurrent migration replay (`scripts/migrate.py` takes `release_ownership.target_writer_lease` after the confirmation prompt and reruns the receipt and attempt recheck under it; `tests/test_migrate_cli.py` `test_migration_completed_while_confirmation_waits_blocks_the_confirmed_replay`). P1 generated baseline overwrite (`scripts/baseline_bundles.py` `_code_before_step` and the per-unit before-state checks; `tests/test_baseline_generated_sql.py`).
- **Partly landed:** P2 baseline column fidelity. Oracle-generated hidden columns are skipped; virtual columns and user-visible hidden columns are refused. Still open: capturing virtual expressions and column defaults.
- **Landed for the refusal and the target listing only:** P2 generated migrations and profiles disagree. `scripts/new_migration.py` refuses a code owner that `migrate` would reject; `targets` marks a split owner `REFUSED`. Still open: owner-relative DDL and target-owner-bound checks. The row stays open for them.
- **Landed for the replacement only:** P2 backup replaces unrefreshed scopes (`scripts/backup_db.sh` `carry_over_unrefreshed_scopes`, commit `2add258`; the same rule in `scripts/backup_db.ps1`, which arrived with the core sync `8128be7`). Still open: moving reviewed ORDS inputs out of the generated mirror.
- **Open items added:** virtual-column expression capture needs an edit in each template's `scripts/compare_env_catalog.sql` (template-owned, not in the shared core); Oracle's catalog representation of `INVISIBLE` columns is not verified against a live database; the PowerShell backup check ran on PowerShell 7.7 on Linux only.

## 6. Review follow-ups on `fix/review-followups-2026-11-19`

- **A. Cleanup removals are not drift (`scripts/template_drift.py`, `scripts/verify_local.py`):** a missing locked file under `tests/` or `.github/` is `removed-by-cleanup`, not `missing`, so `template-status` stays `current` and `verify-local` passes. Anywhere else a missing file is still `missing` and `attention`. The post-clone cleanup in GETTING_STARTED now also removes `.core-stamp.json` and says so.
- **B. Malformed manifest no longer crashes `verify-local` (`scripts/verify_local.py`):** `scripts/local_config.py` reads the manifest at import time, so a bad manifest raised `TemplateReleaseError` before `main` ran. The import is guarded. `main` then prints one failed `template_release` check and exits 2, and skips the other checks rather than inventing their results. The release is read from the checkout that holds the script, as before.
- **C. Linked worktrees count as the original template (`scripts/verify_local.py`):** `_git_config_path` reads a `.git` file's `gitdir:` line and its `commondir`, still with no Git process. Any read or parse problem returns False.
- **D. A verified `app-unlock` closes earlier `held` records (`scripts/application_lock.py`):** `close_held_records` looks only at sibling `run.*` directories and closes a `held` record only when target, app, workspace, parsing schema and developer (case-insensitive) match the unlock. Other records stay as they are. A closing failure is reported on stderr and does not fail the unlock.
- **E. `team.ps1 targets` names itself:** both `-c` calls pass `prog='scripts/team.ps1 targets'`. The Python source uses single quotes inside the PowerShell string. Double quotes would be dropped from the native argument in Windows PowerShell.
- **F. Docs:** GETTING_STARTED (cleanup adds `.core-stamp.json`, `removed-by-cleanup`), ci-checks (shared-core stamp wording), TROUBLESHOOTING (two local-readiness rows), publish-rules (`app-unlock` closes matching `held` records), README (`removed-by-cleanup` note in the template-status paragraph). `TODO.md` was not changed because no open row matches A-E.
- **Tests:** `test_template_drift` 3 -> 6, `test_verify_local` 32 -> 42 (cleanup states, malformed manifest run as subprocesses, linked worktree and real `git worktree`), `test_application_lock` 15 -> 20 (unlock closes records end to end through `main`, with the non-matching, own-directory, unverified and closing-failure cases), `test_team_cli` 80 -> 81 (`targets` usage under pwsh, skipped without PowerShell 7). Each new A, B, C and D test fails when the old module is restored from HEAD.
- **Not verified:** no database, SQLcl or network call ran. Windows PowerShell 5.1 was not tested; the `targets` quoting was checked on PowerShell 7.7 on Linux only. `known-limitations.md` has no entry for these areas.

## Verification

- `python3 scripts/pre_push_check.py` (changed files): passed on each run during this work. The final run before the `--all` check was clean.
- `python3 scripts/pre_push_check.py --all`: exit 0, "all checks passed".
- Touched modules, each run once after the final edits: `test_backup_db_cli` 9 OK; `test_team_cli` 79 OK (6 skipped, all Windows-only Ctrl-C or Git Bash cases); `test_migration_checks` 58 OK; `test_partial_publish` 24 OK; `test_documentation_contract` 28 OK; `test_show_targets` 10 OK; `test_template_manifest` 18 OK; `test_new_migration` 11 OK; `test_migrate_cli` 59 OK.
- Full suite: `CI_TEST_JOBS=2 python3 scripts/ci_run_tests.py --jobs 2`, log `/tmp/phaseB2_full.log`: 3225 tests, 71 skipped, 0 failures, 0 errors, EXIT=0.

## Not verified

- No database, SQLcl session, publish, export, migrate or deploy command ran. `doctor` was not run. The `targets` report never connects by design, and it was checked on a scratch copy only.
- Windows PowerShell 5.1 was not tested. All PowerShell checks ran on PowerShell 7.7 on Linux.
- The Oracle skills registry was read: `lastSync` and the installations are at 2026-11-18, within seven days, so no `skills sync -force` was due. No sync was run.
- `docs/superpowers/template-parity.json` was not changed, as the brief required. The parity ledger is for the lead to update after both templates are finished.
- No shared-core file was edited in this repository. `scripts/show_targets.py` is shared core and keeps its `team.sh` usage text.

## Open

- P2 rows above: column defaults and virtual expressions; owner-relative generated DDL; ORDS input placement.
- The three open items listed in the TODO.
- Daily diffs for non-migration files: the repository rule asks for `git diff` output in this folder. It was not generated, because the brief asked for this file alone and no migration or feature source changed.

## 7. Review pass 2 on `fix/review-pass-2-2026-11-19`

- **1. Credentials in `--source` (`scripts/upgrade_template.py`):** `_reject_credentials` refuses a password anywhere and a username on http(s). `ssh://user@host`, scp-style `user@host:path` and local paths stay allowed. It runs in `_normalize_source`, before any git command for an explicit `--source`, and in `_validated_lock` for a lock upstream (the lock message adds "remove it from .template-lock.json"). `run_git` redacts `scheme://user[:password]@` in the arguments and stderr it prints. Why: the URL, with its secret, was written into `.template-lock.json` and echoed in errors.
- **2. Original-template detection (`scripts/verify_local.py`):** the config parser uses `interpolation=None, strict=False`, so a repeated `fetch` line no longer breaks detection. Origin and manifest upstream compare as `_normalized_remote` (lowercase host, path without a leading slash or trailing `.git`). Https, http, git, ssh, ssh-with-port and scp-style forms of one repository match. Unparsable values give None, which never matches. Local absolute paths keep matching their own origin, as before.
- **3. Legacy raw-hash locks (`scripts/template_drift.py`):** `_local_file_state` reports `matches-lock` when the lock value is either the raw or the normalized digest, as the upgrade planner already does.
- **4. Empty or commit-less lock (`scripts/upgrade_template.py`):** `read_lock_with_hash` refuses a lock whose `commit` is not a 40- or 64-hex string or whose `files` is empty. Internal first-install state and `_lock_bytes` are unchanged. Deviation: `tests/test_template_drift.py` fixture lock gained `"commit": "a" * 40`, because a fixture without a commit is now an invalid lock.
- **5. `features/` and `releases/` are project data (`scripts/upgrade_template.py`):** added the three 26.1 samples to `PROTECTED_SAMPLES` and the 26.1 `protected_project_path` rules. A manifest that assigns `features/**` or `releases/**` to the template is refused by `load_manifest`. No shipped manifest pattern collides.
- **6. `application_lock.py` follow-ups:** the `unlock` branch takes `started = time.time()` just before the unlock SQL, and `close_held_records(target, intent, run_dir, started)` closes only records whose `recovery.json` mtime is at or before it. It scans `publish.*` as well as `run.*`, with the same matching rules. `docs/publish-rules.md` now says so. Deviation: the existing direct test call gained the `started` argument.
- **7. `team.ps1` leaks `PROJECT_SCHEMA`:** the caller's value is saved at the top and restored by `Restore-CallerProjectSchema`, called from `Fail`, `Exit-Team`, the catch-block refusal exit and the outer `finally`. It is idempotent. Deviation: no offline command takes `--schema` and sets the variable, because `targets` and `migration-plan` are excluded from that path. The tests therefore use `targets --help` (the removal branch) and `doctor --schema lowercase_name` (refused before dispatch, nothing connects).
- **8. Docs:** GETTING_STARTED names `.github/workflows/template-checks.yml` and `.github/dependabot.yml` explicitly, and says to list a project's own workflows before removing anything, never by glob. README "Safety built in" now reads: production is never written without an explicit confirmation (a `y` typed at the terminal, or `--confirm-target` naming the observed `db_unique_name`). Deviation: the suggested wording ("typed confirmation of its database name") would be false for the plain `y/N` path that `migrate.py` and `deploy.sh` accept.
- **Tests:** `test_upgrade_template` 75 -> 90 (all OK after; items 1, 4, 5 failed on the HEAD engine: 16 failures, 10 errors). `test_template_drift` 6 -> 11 (items 3 and 4: 3 failures on HEAD). `test_verify_local` 42 -> 48 (item 2: 7 failures and 1 error on HEAD). `test_application_lock` 20 -> 23 (item 6: 2 failures and 1 error on HEAD). `test_team_cli` 81 -> 84 (item 7: 2 failures on the old script, 3 OK after; pwsh 7.7 on Linux). The HEAD check ran the new test files against a `git archive HEAD` copy in the scratchpad.
- **Not verified:** no database, SQLcl, network, publish, migrate or deploy command ran. Windows PowerShell 5.1 was not tested; the `team.ps1` fix was checked on PowerShell 7.7 on Linux only. `scripts/team.sh` and the shared-core files were not edited, and `sync_core.py check` reports OK.
- **Known regression, open:** the full offline suite (`scripts/ci_run_tests.py --jobs 4`) reports `Combined: Ran 3294 tests; skipped=71; failures=3; errors=1`, all in `test_template_update_notice` (its fixture lock has `"files": {}`). Item 4 makes that lock unreadable. `scripts/check_template_update.py` and `tests/test_template_update_notice.py` are shared core, so they were not edited here. The same module passes on HEAD. Resolve upstream in APEX_TEMPLATE_CORE (a fixture with one file, or a notice path that accepts an empty lock), then sync.

## Live verification on local Docker Oracle (2026-11-19, pass 3)

Targets: the three disposable scratch containers `fq-scratch-dev`, `fq-scratch-stg` and `fq-scratch-prod` (Oracle AI Database 26ai Free 23.26.3, schema `FEATURE_QA`, saved connections `fqdev`, `fqstg`, `fqtarget3`). The persistent `local-26ai` container was not written to. The scratch containers were started for this run and stopped afterwards.

- **Repository live modules, run from throwaway clones of the final branch:** `tests/test_live_baseline.py` 14 tests OK; `tests/test_live_feature_release.py` 22 tests OK with the three-target proof, the dedicated long-view and the staging benchmark scenarios enabled (1 skipped: the two-schema scenario needs a second schema).
- **Generator checks written for this pass (32, all OK):** the CHECK replacement block compiles and runs on Oracle (reviewed replace, idempotent re-run, refusal with -20994 for a stale review or changed status, -20993 for a primary key of the same name, DISABLED stays DISABLED, quoted identifiers compare by their text, the precondition query counts violating rows, and the known drop-then-add hazard is confirmed so the precondition is what prevents it); the sequence block restores the target's own increment; an owner-qualified object-type column adds and checks; catalog-shaped foreign-key order creates parent first; `ALL_TAB_PRIVS.GRANTABLE` exists; GRANT ALL and column grants are not recorded as ALL or UPDATE(col); INVISIBLE columns add with `INVISIBLE` (also before an identity clause) and the postcondition checks `HIDDEN_COLUMN`.
- **What the catalog showed:** an INVISIBLE user column is `HIDDEN_COLUMN=YES`, `USER_GENERATED=YES`; a function-based index column is hidden, `USER_GENERATED=NO`, virtual; `ALL_TAB_COLS.DATA_DEFAULT_VC` returns a virtual column's expression on 23ai (not confirmed on older releases, so expression capture is not implemented). `DATA_DEFAULT` is a LONG and cannot be used in a SQL expression.
- **SQLcl session cost:** about 2.1 s for connect, one statement, exit.
- **Real project check:** `upgrade_template.py --dry-run` against a long-lived downstream project changed nothing in it (the working tree stayed clean) and found no mode-only updates.
- **Not verified:** `app-unlock` closing held lock records (26.2): the scratch databases have no APEX. Windows PowerShell 5.1.
