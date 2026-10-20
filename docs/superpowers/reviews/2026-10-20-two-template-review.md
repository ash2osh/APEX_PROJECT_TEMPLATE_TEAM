# Two-template review and fixes — 2026-10-20

## Scope and boundaries

- Reviewed the APEX 26.2 TEAM and separate APEX 26.1 template repositories from remote `main`.
  - TEAM base: `b4a996e0229db16bc92665cd077ef6ab9a9e765d`.
  - 26.1 base: `f18bb51ddf51efc9c5bd15cb71c303130d396b14`.
  - Changes are isolated in `codex/full-template-review-262` and `codex/full-template-review-261` worktrees. Original checkout working files were not edited.
- Review covered implementation, regression tests, CI/workflows, command/runbook documentation, and release-line parity.
  - Migration validation, checks, frozen payloads, attempts/receipts, revisions, rehearsal, rollout/resume and schema comparison.
  - APEX export, full/partial publish, deployment, identity guards, locks, lifecycle/automations and schema/ORDS backup paths.
  - CI/pre-push gates, configuration/readiness/query commands, DEV test planning, installer/upgrades, optional skill/Graphify and Chrome tooling.
  - This is a source and offline behavioral review, not proof that every possible defect has been found.
- Known limitations were read before classifying defects.
  - The intentional application-lock limitations, release-specific features, and different damaged-attempt evidence policies remain unchanged.
  - No real database, APEX Builder, browser, migration, import, export or deployment operation was performed.
  - Native Windows/macOS execution and live Oracle/APEX/ORDS qualification were not performed. Local tests use Linux and Python 3.14, including available PowerShell and fake SQLcl fixtures.
- Oracle skills were current at task startup.
  - Native repository and 10 deduplicated loaded installations were checked; oldest recorded installation: `2026-10-18T11:41:02.192622103Z`. No sync was needed or claimed.

## Confirmed defects and corrections

- **P1 — The pre-push gate leaked Git's hook environment into fixture repositories, in both lines.**
  - Standalone gates passed, but real pushes exported repository-local Git variables into tests. Fixture Git commands then changed the invoking repository's configuration, index and review branch instead of their temporary repositories; the push refused.
  - Correction: clear the names reported by `git rev-parse --local-env-vars` before selecting files or starting checks. This also protects already installed hooks without replacing them.
  - Regression: `test_hook_does_not_pass_repository_environment_to_fixture_tests` runs the real hook and check script with explicit hook-style Git variables; it failed before the fix and passes after it.
  - Integration recovery restored the task-owned branches/indexes from their saved commits and the changed shared Git settings. Original checkout source files were untouched. A separate PowerShell cancellation crash in the failed 26.1 hook matched an existing known-flaky entry; no retry policy or flaky list was changed.
- **P1 — 26.1 advanced the DEV baseline before lifecycle verification and deleted failed-import diagnostics.**
  - A stateful fake import followed by a failed post-read advanced `apex-team-export.json` and left no recovery directory despite returning failure.
  - Correction: verify source and lifecycle first, record the baseline last, and retain transcripts/staging after attempted imports until verification succeeds, in both Bash and PowerShell. Recovery summaries set `sourceVerified` only after the completed source comparison.
  - Regression: `tests/test_publish_lifecycle_gate.py`, including lifecycle attention, automation restoration failure and source mismatch.
- **P2 — CI could convert an unrelated child crash into a successful flaky-test retry, in both lines.**
  - Reproduced with a real test subprocess exiting 7 while another reported a listed flaky failure; the original runner returned 0 after retry. Sequential crash-after-summary was also reproduced.
  - Correction: preserve fatal child results and permit retries only for completed ordinary unittest failures.
  - Regression: `tests/test_ci_run_tests.py`, including real process crashes and truncated failure output.
- **P2 — PowerShell discarded SQLcl output when waiting was interrupted, in both lines.**
  - The transcript was assembled only after a successful wait, while `finally` deleted stdout/stderr temporary files. Reproduced with a real fake-SQLcl process followed by a wait exception; the process output existed but the requested transcript did not.
  - Correction: stop an interrupted child, capture its stdout/stderr in `finally`, then remove the intermediate files. Preserve the interrupt/failure outcome; a transcript capture failure during interruption is a warning, never success.
  - Regression: `tests/test_sqlcl_transcript.py`, wait-error and normal completion in each line. The existing 26.1 POSIX/PowerShell import-interrupt test also verifies retained transcript and unverified summary; its native Windows twin has the same assertion but remains unexecuted here.
- **P2 — Manual deployment commands omitted required captured identity arguments, in both lines.**
  - TEAM printed 8 of 16 SQL script arguments; 26.1 printed 7 of 15. The remaining arguments were unconditional substitution variables in `publish_app.sql`.
  - Correction: capture lifecycle identity, validate the snapshot, encode all eight identity fields, and pass them to the printed import command. Preserve recovery evidence until verification succeeds; include 26.1 lifecycle restoration/post-read steps. Both manual runbooks record successful source comparison before lifecycle verification so recovery summaries distinguish the two gates.
  - Regression: `tests/test_publication_identity.py`, exercising generated commands against fake SQLcl and refusing malformed snapshots before import.
- **P2 — Full publish did not establish the parsing schema before comparing captured identity, in both lines.**
  - Lifecycle capture sets `CURRENT_SCHEMA` to the parsing schema; the fresh writer session previously retained the deployment account's schema, causing a supported separate-account deployment to fail its identity check.
  - Correction: establish the validated target schema before the writer identity guard, preserving the independent authenticated-user check.
  - Evidence is the generated-session contract and offline regression, not a live successful deployment.
- **P2 — Daily migration diffs collided across schemas, in both lines.**
  - Two valid schema folders with the same migration basename shared one output path: automatic recording skipped the second and manual recording overwrote the first.
  - Correction: schema-folder records use `ai_generate/<date>/<SCHEMA>/<folder>.diff`; flat migrations retain their original path. Existing unscoped evidence remains untouched. Recording errors remain nonfatal after a verified migration.
  - Regression: `tests/test_migration_diff.py`, automatic/manual collisions, old evidence preservation, flat compatibility and path-resolution failure.
- **P2 — Package diff headers could select the wrong mirror, in both lines.**
  - Leading comments could select a different package; multiline headers failed to match; quoted mixed-case identifiers were uppercased incorrectly.
  - Correction: match the first actual package header across comments/newlines and preserve quoted identifier case and backup filename encoding.
  - Regression: `tests/test_migration_diff.py`; prior Unicode support is retained.
- **P2 — DEV planning JSON failed across DBMS_OUTPUT chunk boundaries, in both lines.**
  - A 12,911-character catalog response split at 8,000 characters failed JSON parsing because the parser inserted a newline inside a string.
  - Correction: concatenate transport chunks without added characters and split only physical line endings, preserving valid Unicode separators inside JSON values.
  - Regression: `tests/test_dev_test_planning_output.py`, large catalogs, CRLF, Arabic, escaped newlines, U+2028 and U+0085.
- **P2 — TEAM readiness reported success with missing template-locked files.**
  - Reproduced a downstream lock naming a missing `scripts/team.sh`: report exit 0, drift check `pass`, message `1 missing`.
  - Correction: missing locked files require attention (exit 1); safe customizations remain passing.
  - Regression: `tests/test_verify_local.py`.
- **P2 — TEAM Graphify coverage ignored configured exclusions.**
  - The static eligible-file scanner did not read `.graphifyignore`/Git exclusion settings, so intentionally excluded files could make extraction verification fail.
  - Correction: the exact isolated detector selects the corpus during extraction or explicit `verify --refresh-corpus`; an ignored receipt records source/settings/graph fingerprints for subsequent subprocess-free inspection. New/deleted paths, detector noise markers, changed source/ignore/build settings, Git index/exclusions and graph bytes invalidate that receipt. Missing or unverified receipts report coverage unavailable.
  - Excluded symlink targets are not read; selected links must remain inside the project. Domain-root and ignore-metadata links refuse coverage. Global CLI options remain effective when supplied before the subcommand.
  - Regression: `tests/test_graphify_corpus_receipt.py`, including the custom exclusion, missing selected source, incomplete/during-change scan, noise-marker removal, linked-worktree metadata, source-link and CLI option cases.
  - The pinned `graphifyy==0.9.75` wheel was downloaded without dependencies and its detector source inspected. Exact detector/extraction execution in an installed pinned environment remains unverified; focused tests use controlled detector replies. No global/shared installation was modified.

## Verification and parity

- Clean baseline gates passed before production edits.
  - TEAM: 117 modules, 2,253 tests, 38 skipped; full CI command exit 0.
  - 26.1: 96 modules, 1,897 tests, 32 skipped; full CI command exit 0.
  - `python3 scripts/pre_push_check.py --all` passed in each worktree.
  - Full suite command: `CI_TEST_JOBS=4 python3 scripts/ci_run_tests.py`.
- Each behavior correction has a failing reproduction followed by a passing focused check.
  - Each line: 18 migration-diff tests, 21 CI runner tests, 5 publication identity/runbook tests, 2 chunked planning-output tests.
  - Each line: 2 real fake-SQLcl subprocess transcript tests passed. The 26.1 real signal/unknown-import retention test passed after the launcher correction.
  - TEAM: 71 combined Graphify receipt/project/readiness tests passed after the last Graphify correction.
  - 26.1: lifecycle-gate regression passed with six engine/failure-mode subcases (Bash and installed PowerShell).
  - The first integration pre-push pass caught a missing `zip(strict=...)` test argument and the old 26.1 unconditional-cleanup assertion; both were corrected and their focused checks rerun.
  - The first full integration run caught the old 26.1 interrupt-cleanup assertion. Updating it to require retained diagnostics exposed the shared launcher's missing partial transcript; that root cause was fixed in both lines before restarting both final gates.
  - Example configuration, application JSON and LF checks passed in each line.
- Final gates passed after the last code correction, performed inline.

  | Release line | Pre-push gate | Full CI command | Modules | Tests including skips | Skipped |
  | --- | --- | --- | ---: | ---: | ---: |
  | 26.1 | exit 0 | exit 0 | 100 | 1,919 | 32 |
  | TEAM 26.2 | exit 0 | exit 0 | 121 | 2,291 | 38 |

  - Every module in both final runs exited 0. Synthetic `mod_a.ClassA.test_one` retry warnings are emitted by the CI runner's own passing regression tests, not an actual failing repository test.
  - Final commands were `python3 scripts/pre_push_check.py --all`, followed by `CI_TEST_JOBS=4 python3 scripts/ci_run_tests.py` in each worktree. `git diff --check` also passed both.
  - Logs are retained under each worktree's ignored `scratch/full-review-2026-10-20/`: `<line>-baseline.log`, `<line>-prepush-final.log`, `<line>-full-final.log`, and the first integration attempt `<line>-full-first-attempt.log`, where `<line>` is `261` or `262`.
  - Focused planning, readiness/Graphify and 26.1 lifecycle/interrupt evidence is retained beside those logs. TEAM also has `working-parity.json`, a comparison of working-file Git blob hashes rather than HEAD-only results.
  - Remote `main` was reread during final verification and still matched each recorded base. The first pushes exercised and exposed the hook-environment defect above; neither reached the remote.
  - After fixing hook isolation, both pre-push/full-suite sequences passed again with the counts above. Integration logs and final cleanup evidence are retained outside the disposable review worktrees in the Codex task artifacts; hosted outcomes remain recorded on the integration PRs.
- Committed baseline parity was checked with the existing TEAM tool.
  - 392 shared files, 114 differing: 32 `feature`, 82 `intended`, 0 unreviewed/changed/missed/stale.
  - The tool compares committed HEAD only; pending working-file corrections are not represented by that baseline result.
  - After committing both source changes, parity was refreshed with `tools/template_parity.py --mark` and verified against both committed HEADs: 395 shared files, 114 differing, 32 `feature`, 82 `intended`, 0 unreviewed/changed/missed/stale. No JSON was edited by hand.
  - The shared fixes were ported to both affected lines; Graphify isolation/coverage and other established release-specific behavior remain deliberately different.

## Remaining limits

- **TEAM issue #49 remains unresolved.**
  - The Windows-only ORDS exit-status flake has no confirmed reproduction/root cause in this review. No speculative `backup_db.sh` change was made and no flaky-list entry was added or weakened.
- At review completion the fixes were local and uncommitted; the developer subsequently authorized committing, merging through protected `main`, and cleaning up the task-owned worktrees.
  - Integration requires the pre-push/full-suite gates and all three hosted checks before merge. Hosted outcomes belong to the integration PRs; the local review results above are not a claim that those hosted checks ran.
