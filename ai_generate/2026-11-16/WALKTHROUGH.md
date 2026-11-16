# Walkthrough 2026-11-16

## Review findings fixes (branch `fix/review-2026-11-16-262`)

Fixed five review findings on the 26.2 template, branched from `origin/main` at `d9e26a2`. Offline only: no database, SQLcl, migration, publish, deploy, export or Graphify setup/extract command ran.

### Changes

1. **Graphify fix hint** (commit `901d761`): `scripts/verify_local.py`, `tests/test_verify_local.py`
   - The unverified-extractor hint now points to `python3 scripts/graphify_project.py setup`, not `scripts/setup_graphify_apx.py`.
   - The missing-script message also names that command.
   - New test pins the missing-script message.

2. **uc-apx wording** (commit `da6f919`): `.agents/skills/building-apex-template-components/SKILL.md`, `reference/anatomy.md`, `reference/testing-and-import.md`
   - Kept the verification facts: APEX 26.1, compatibility 24.2, SQLcl `apex validate`, real imports and browser runs.
   - Stated that uc-apx is retired and not part of this template. Removed the uc-apx validation commands and the "run both validators" advice.
   - The `.claude/skills` mirror has no uc-apx wording and is unchanged.

3. **Retired `TEAM_ACK_FROM_STDIN`** (commit `f6d092e`): `AGENTS.md`, `.agents/rules/agent-safety.md`, `scripts/terminal_confirm.py`, `tests/test_terminal_confirm.py`, `tests/test_migration_resolution.py`
   - The variable is described as retired and ignored. Piped or redirected acknowledgments stay refused; `--yes` is the recorded route.
   - `may_read_acknowledgment` lost its unused `environ` parameter and the `Mapping` import. All callers already passed no argument.

4. **Privacy scrub, text only** (commits `bf6528e`, `178850a`, `35d956a`): `docs/apex-26.2-qualification.md`, `docs/superpowers/**` (except `template-parity.json`, which is not edited), `tests/test_feature_ddl_adversarial.py`, `tests/test_feature_ddl_forms.py`
   - Downstream project names replaced with neutral wording. Personal home-directory paths replaced with `<26.1 checkout>`, `<26.2 checkout>`, or `<downstream checkout>`.
   - Relative links into a downstream checkout became plain text, because those files are not in this repository.
   - A grantee name derived from a downstream project in two DDL fixture modules became `SERVICE_USER`; expected classifications are unchanged.
   - After the scrub, a search for those names and paths hits only the deny-list in `tests/test_agent_styles.py`, which is kept on purpose.

5. **Daily change record**: `ai_generate/2026-11-15/WALKTHROUGH.md` (PRs #101 and #102) and this file.

### Verification Results

Commands and results, all run on this branch after the five commits above:

- `python3 -m unittest discover -s tests -p test_verify_local.py -v`: Ran 31 tests, OK.
- `python3 -m unittest discover -s tests -p test_template_component_skill.py`: Ran 7 tests, OK.
- `python3 -m unittest discover -s tests -p test_terminal_confirm.py`: OK.
- `python3 -m unittest discover -s tests -p test_migration_resolution.py`: Ran 35 tests, OK.
- `python3 scripts/pre_push_check.py --all`: exit 0, "all checks passed; CI also runs the full suite on Linux, Windows and macOS".
- `CI_TEST_JOBS=4 python3 scripts/ci_run_tests.py`: "Ran 3081 tests; skipped=60; failures=0; errors=0; incomplete_modules=0", exit 0.

### Not verified

- ShellCheck did not run: `shellcheck` is not on PATH here. CI runs it on Linux.
- The PowerShell parse step ran inside `pre_push_check --all` (pwsh is on PATH here) and passed with the rest of the checks. Windows PowerShell and the Windows CI job were not run here.
- No live database, SQLcl session, migration, publish or deploy was used.
- The 26.1 repository gets the identical `terminal_confirm.py` change from another agent. This branch did not compare the two files.
- `docs/superpowers/template-parity.json` is not refreshed; it waits until both repositories merge.
- The suite count (3081 run, 60 skipped) is this run's result. It was not compared against a baseline run here; the 2026-11-14 record shows 3051 run and 70 skipped on a different branch.

### Open

- Merge decision and PR creation are left to the owner. Nothing was pushed.
- The 26.1 side of the `TEAM_ACK_FROM_STDIN` change must land identically.

### Decisions for Review

1. The missing-script message in `verify_local` now also gives the setup command. It was not in the review list; it was changed for consistency.
2. The uc-apx history sentence stays ("an earlier verification also ran uc-apx") so the historical record is not rewritten.
3. Kept the `DEV2`/`DEV26` labels and the `D26` worktree folder name in docs/superpowers. They are environment labels, not downstream names, and the scrub pattern does not include them.
4. Commit trailers use the Opus 5.5 line from the task. The environment attribution names a different model. Choose one for future commits.

## Follow-up on this branch

- `scripts/workflow_skills.py`: `CORE_SKILLS` now lists `firefox-devtools-mcp`, so a workflow pack cannot install a skill with that name over the template skill. `tests/test_workflow_skills.py` `CoreSkillListTests` pins the list to the template skill folders, ignoring skills an installed pack recorded in `.agent-skills.lock.json`. The same change is on the 26.1 branch.

## Live reconciliation scenarios run again

- `tests/test_live_feature_release.py`: `test_abrupt_termination_mid_apply_is_recovered_after_reconciliation` and `test_unknown_run_without_a_checkpoint_is_reconciled_then_frees_the_target` were permanently skipped because they typed an acknowledgment. They now supply it with `--yes` (`rollout --prepare --recover --yes`, `feature reconcile-run --yes`), assert `acknowledgedBy: --yes`, and first assert that a piped acknowledgment, even the exact text, is refused. `docs/live-feature-tests.md` describes this.
- Live results are in the pull request description.

## Shared core files from APEX_TEMPLATE_CORE

- The 202 files under `scripts/`, `tools/` and `tests/` that are identical in both templates now come from the new public repository `ash2osh/APEX_TEMPLATE_CORE` through `scripts/sync_core.py`. `.core-stamp.json` (template-only, not shipped downstream) lists each file with its SHA-256 and the core commit.
- `scripts/pre_push_check.py` runs `scripts/sync_core.py check .` when the stamp exists; the `offline` CI job runs `check --core` against a clone of the core. `docs/ci-checks.md` describes the fix-in-core workflow.
- Phase 2 (merging the 104 differing files behind an `apexRelease` switch) is next; see the core README.

## Template parity refresh against the 26.1 template

Refreshed `docs/superpowers/template-parity.json` against `APEX_PROJECT_TEMPLATE_26_1` at `fce9a7b`, the 26.1 `main` HEAD.

- Before: `template_parity.py` exit 1 with 142 differing shared files: 10 unreviewed, 28 changed, 2 stale, 30 feature and 74 intended.
- Removed the two stale entries. `docs/feature-releases.md` and `docs/live-feature-tests.md` are byte-identical in both lines now, so the shared core tracks them.
- Reclassified all 38 unreviewed or changed files from the actual diff, with one reason each that names the difference. Result: 103 intended, 30 feature, 9 missed, 0 unreviewed, 0 changed, 0 stale. Exit 0.
- The nine `missed` files are work items. Each reason in the JSON gives the port direction. Nothing was fixed in this change:
  - `scripts/publish_app.sh`: 26.1 refuses `--app-source-dir` with `--env dev`; 26.2 does not.
  - `scripts/rollout.py`: 26.2 has four safety checks 26.1 lacks (SQLcl transport preflight for rollout payloads, receipt evidence path checks, target identity checks for schema-1 manifests, frozen app-deploy child configuration).
  - `scripts/team.sh` and `scripts/team.ps1`: 26.2 lost the `compare-env --help` path that works without `.env`.
  - `scripts/verify_local.py`: 26.2 reports missing template-locked files as attention; 26.1 reports them as pass.
  - `tests/test_baseline_generated_sql.py`: 26.2 needs `PYTHONPATH` to import its guard module.
  - `tests/test_feature_rollout.py`, `tests/test_rollout.py`, `tests/test_verify_local.py`: regression tests present on one side only.
- Offline only. Commands run: `template_parity.py` (read and `--mark`), `python3 -B -m unittest tests.test_baseline_generated_sql` with and without `PYTHONPATH`, and `scripts/team.sh compare-env --help` with no `.env` (exit 1). No database, SQLcl or network command ran.

## backup-db keeps its exit status

- `scripts/backup_db.sh`: the EXIT trap now keeps the run's status; under errexit a failed scratch cleanup used to turn a refusal (2) or a finished backup (0) into 1. A cleanup failure is reported as a warning. `run_ords_scope` exits 2 directly on each refusal and is called without `||`, so errexit stays on inside it (26.2 had it off). The file is now identical in both templates.
- The Windows known-flaky entry (refusal printed, exit 0) is not explained by this; the leading candidate is `exec` in `scripts/team.sh` under Git Bash, which needs a Windows probe. The entry stays until then.

## Parity ports into 26.2

- `scripts/publish_app.sh`: `--app-source-dir` with `--env dev` is refused before any SQLcl call, as in 26.1 (the option was added for staging and production only).
- `scripts/team.sh`, `scripts/team.ps1`: `compare-env --help` answers before `.env` is loaded, as in 26.1.
- Tests ported from 26.1: the capsule rerun refusal on an occupied receipt (`test_feature_rollout`), a template lock without `templateRef` (`test_verify_local`), and ten `test_rollout` cases (pause-step rerun, multiline `UPDATE ... SET`, auxiliary DML classifier, shared-catalog identity, schema-2 budget).

## Parity refresh 2 against the 26.1 main

- Compared against 26.1 `origin/main` at `babee3f`. The 26.1 checkout's HEAD is `1663bd8`, three commits behind it, so the tool ran against a read-only clone pinned to `babee3f`.
- Before: 142 entries (103 intended, 30 feature, 9 missed as stored), 13 changed, 19 stale, 0 unreviewed.
- Removed the 19 stale entries. Their files are byte-identical in both lines now (core sync batch 1: 26.1 PR 75 and 26.2 PR 109), including the earlier `test_baseline_generated_sql.py` missed item.
- Reclassified the 13 changed files from their committed diffs. After: 123 entries (85 intended, 34 feature, 4 missed), 0 unreviewed, 0 changed, 0 stale; `template_parity.py` exit 0.
- Decision recorded: 26.1 does not port the 26.2 app-deploy target identity and child-configuration bindings. That is intended in the `scripts/rollout.py` entry, which stays `missed` for a separate check (below).
- Missed, not fixed here:
  - `scripts/rollout.py`: 26.2 observes the target identity after each migrate or SQL step, checks each migration receipt against it on execute and resume, and refuses legacy schema-1 receipts without observed identity. 26.1 does not.
  - `scripts/team.sh`: 26.1 passes the migration-plan root through `cygpath -m --` under a test. 26.2 omits the `--`.
  - `tests/test_rollout.py`: five PR 74 tests (transport preflight, evidence paths, missing-evidence receipt) exist in 26.1 only, although 26.2 `rollout.py` has the same checks.
  - `tests/test_backup_ords_cli.py`: 26.2 has an unguarded-ORDS-failure regression test that 26.1 lacks, although `scripts/backup_db.sh` is identical in both.
- Checked offline only: no database, SQLcl, network or publish command.

## Decisions 1 and 3: symlink test guard and dry-run wording

- Branch `chore/decisions-262` from `origin/main` (`c22604e`). Commits: `db20969` (symlink guard), `112d22a` (dry-run wording), `8ec7117` (parity re-marks), then this section and the parked item in `TODO.md`.
- `tests/test_path_utils.py`: the parent-alias test now calls `skipTest` when `symlink_to` raises `OSError`, the guard `test_validate_app_source.py` uses. The assertion is unchanged and no test was added. With `Path.symlink_to` patched to raise `OSError`, the test reports skipped, not errored.
- Dry-run wording, checked in the code first:
  - `scripts/rollout.py` `_prepare_plan` and `_target_binding` resolve each step's target from `PROD_*` or `STAGING_*` (`resolve_target(os.environ, ...)`). That happens before the dry-run return in `_run_legacy_rollout`. `_observe_target_identity` and `_plan_targets` run only after that return.
  - `scripts/rollout_capsule.py` reaches the same resolution through `rollout._prepare_plan`. Its dry-run return comes before `feature_release.prepare_target`. The request named a `_prepare_plan` in that file; the function is not defined there.
  - Reworded: `docs/migration-rules.md` (dry-run paragraph), `README.md` (the production example paragraph and the rollout rules paragraph) and `docs/feature-releases.md` (dry-run paragraph).
  - Wording differs from the request: the docs say "process environment", not "environment or .env". `scripts/rollout.sh` execs `rollout.py` on `--dry-run` before it sources `.env`, and `rollout.py` reads `.env` only for an `app-deploy` step's child configuration.
  - Offline probe in a scratch copy: with `PROD_*` only in `.env`, a schema-version 2 dry run failed with `schema APP is not listed in PROD_SCHEMA for prod` (exit 2). With the same values exported, it printed the plan (exit 0).
  - "No database session" rests on the code reading and on `test_v2_dry_run_without_capsule_stays_offline_and_schema1_execution_is_unchanged`, which asserts that `run_sqlcl` and `_observe_target_identity` are not called. A stub named `sql` on the PATH recorded no calls, but the bridge's binary name was not confirmed, so that stub proves little.
- Parity (`8ec7117`): `tests/test_path_utils.py`, `docs/migration-rules.md` and `README.md` were re-marked `intended` with their reasons kept (blob ids changed). `docs/feature-releases.md` was added as `intended`: 26.1's dry run plans from local source only (its `_prepare_plan` resolves no target), so its local-only wording stays correct there.
- Parity left alone: `scripts/rollout.py`, `tests/test_rollout.py`, `tests/test_backup_ords_cli.py` and `scripts/dev_test_core_migrations.py` were already `changed` before this work; `scripts/team.sh` stays `missed`.
- Open: the dry-run `.env` wording waits for the core `rollout.sh` change. Parked in `TODO.md` under "Parked 2026-11-16".
- Verification, offline, on `chore/decisions-262`:
  - `python3 scripts/pre_push_check.py --all`: exit 0, all checks passed.
  - `python3 -m unittest discover -s tests -p test_path_utils.py -v`: 3 tests, OK.
  - `python3 -m unittest discover -s tests -p test_documentation_contract.py`: 26 tests, OK.
  - `python3 -m unittest discover -s tests -p test_validate_app_source.py`: 7 tests, OK.
  - `CI_TEST_JOBS=4 python3 scripts/ci_run_tests.py`: exit 0; 3106 tests, skipped=60, failures=0, errors=0, incomplete_modules=0.
  - `python3 tools/template_parity.py ../APEX_PROJECT_TEMPLATE_26_1`: 0 unreviewed, 1 missed (`scripts/team.sh`), 4 changed (the four pre-existing records above), exit 1.
- Not verified: Windows symlink privilege (simulated only). The core `rollout.sh` change. No database, SQLcl, publish or export command ran. The 26.1 checkout was read only through its committed HEAD (the parity tool and `git show`).
