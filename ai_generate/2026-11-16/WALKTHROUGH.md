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
