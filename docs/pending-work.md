# Pending work

Single list of everything still open for this template, in the order it should
happen. Designs live where noted; this file says what is left, who does it, and
when it is done. Update it in the same commit that closes an item.

Last updated: 2026-09-25. Offline implementation of the release,
acknowledgement and hardening items is merged to `main` (PR #6), including
fixes for 17 review findings. The original offline gate passed at
`4669d29f7dccd178d3f31bc29400827640c69b72`; after the live tamper fix, the full
suite passed with 730 tests. On approved throwaway target `local-26ai`, Stages
0–3 and the source-side Stage 4 schema release checks have been exercised.
Stage 2 passed with a genuine pre-member-storage history, including backfill,
missing-member reporting, and tamper rejection before and after storage. Stage
3's two-repository migration and undo passed. Stage 4 cut and verified schema
release `0.1.0`, recut it with the same digest, refused version reuse after a
ledger change, and refused an untracked rogue object. The first Stage 5 cleanup
passed at 15:04 EEST, then the approved LT_* fixture was re-established after
the host restart to continue Stages 2–4; final cleanup remains open. I did not
issue a reboot command. The previous boot's journal ends at 17:10 EEST and the
next boot starts at 17:10:58; the logs do not identify the cause. The
snapshotted container recovered and all snapshot hashes still match. The boot
journal observation is saved at
`scratch/live-test-2026-09-25/restart-continuation/restart-check/host-boot-observation.txt`.
Isolated release replay, app release/deploy and METADATA restore rehearsal also
remain open as recorded below.

## Where things stand

| Done | What |
|---|---|
| PR #1 | SQLcl identity guard runs before any payload; signing key only exists in the signing step; no persisted checkout credentials; stricter production read-only allowlist |
| PR #2 | Signing moved to a separate trusted job; GitHub Actions pinned by commit SHA |
| PR #3 | One Git repository per developer, no shared remote: docs, agent contract, three-repository e2e harness, `.env.example` |
| PR #4 | Migration files stored in METADATA (`TEAM_MIGRATION_BUNDLE`/`MEMBER`), `adopt-migration-members` backfill |
| PR #5 | Original `TEAM_RELEASE` schema-cut groundwork |
| PR #6 | Format 3 replay and evidence, paused app release cuts, unified database `build-release`, Git release path removed, database-backed publish acknowledgements, production privilege audit and hardening, plus 17 review fixes |

Design for the release work: `docs/superpowers/specs/2026-09-24-dev-database-release-source-design.md`
(owner decisions recorded there). Live acceptance remains incomplete until an
isolated schema replay target and a throwaway APEX test app are available.

## 1. Owner actions (no code)

### 1.1 Run the live database test plan — OPEN / SOURCE CHECKS COMPLETE; ISOLATED REPLAY OPEN
- **What:** `docs/live-test-plan.md` on approved `local-26ai` with throwaway schemas.
- **Current evidence (2026-09-25):** the baseline offline gate passed on
  `4669d29`; the tamper fix branch then passed Ruff, shell syntax, all 730 unit
  tests, `ci-doctor` (`valid: true`), `git diff --check`, and a tracked-file
  CRLF scan. SQLcl is `26.2.2.233.1901`; Java is
  `21.0.12.1`. Read-only inspection of `local-26ai` reported database `FREE`,
  PDB `FREEPDB1`, APEX `26.1.4` (`APEX` registry status `VALID`), and `USERS`.
  Its `oradata-26ai` volume was snapshotted at
  `.sync-state/live-test-2026-09-25/local-26ai-oradata-pre-live-test-2026-09-25`
  (33 files, 7.0 GB). That snapshot was restored after the first failed
  migration attempt; the restore diff was empty, all snapshot hashes passed,
  and the hashes passed again after the host restart. The container is healthy.
  Stage 0 was then re-established: the four
  throwaway LT_* users have expected grants and quotas, fresh saved SQLcl
  aliases connect as the expected identities, and Alice/Bob `doctor` both pass.
  The APEX capture connection remains read-only; no APEX app was deployed.
- **Results:**

  | Stage / step | Expected | Actual | Status | Evidence |
  |---|---|---|---|---|
  | Offline baseline | All offline gates pass | The full offline gate passed after the live fixes and this documentation update: Ruff, shell syntax, 730 unit tests, `ci-doctor` (`valid: true`), `git diff --check`, and tracked CRLF scan | PASS | `scratch/live-test-2026-09-25/restart-continuation/pull-request-gates/pr10/report.json` |
  | 0 — snapshot | Consistent rollback copy before writes | Snapshot restored after Stage 2; file diff empty, all 33 hashes pass, container healthy | PASS | `.sync-state/live-test-2026-09-25/local-26ai-oradata-pre-live-test-2026-09-25.sha256`; `scratch/live-test-2026-09-25/stage2/restore-copy-attempt2.json`; `scratch/live-test-2026-09-25/stage2/restore-postcheck.json` |
  | 0 — APEX installation | APEX available on target | `DBA_REGISTRY` reports APEX `26.1.4`, status `VALID` | PASS | `scratch/live-test-2026-09-25/stage0/apex-installed-check.json` |
  | 0 — LT users | Least-privilege throwaway users and quotas | Four users are OPEN; expected system/role grants and 100 MB quotas verified | PASS | `scratch/live-test-2026-09-25/stage0/verify-lt-users-after-creation.json` |
  | 0 — saved SQLcl connections | Four `-savepwd` aliases connect as their matching LT_* users | Fresh saved aliases verified for LT_DATA, LT_CODE, LT_META and LT_VERIFY | PASS | `scratch/live-test-2026-09-25/stage0/rotate-and-save-lt-connections-corrected.json` |
  | 0 — profiles and `.env` | Credential-free profiles and valid doctor in both clones | Alice and Bob profiles passed after restart using refreshed SQLcl aliases; latest read-only rechecks pass. Root `.env` still names retired alias `lt-meta`, so its first post-restart read failed before payload | PASS after profile refresh | `scratch/live-test-2026-09-25/restart-continuation/restart-check/alice-doctor-recheck.json`; `.../bob-doctor-recheck.json`; `.../stage3/post-bob-undo-frontier/stale-root-profile-probe/root-env-stale-alias.json` |
  | 1 — wrong connection | Refuse before payload; no LT_DATA metadata objects | After restore, exit 3 with ORA-20901 before payload; no TEAM_* tables appeared in LT_DATA | PASS | `scratch/live-test-2026-09-25/stage1/wrong-connection-after-restore.json`; `scratch/live-test-2026-09-25/stage1/wrong-connection-no-tables-after-restore.json` |
  | 1 — right connection | Adopt sequence-zero frontier and create expected METADATA objects | After restore, LT_META frontier digest `4363cf314846dc29a8e3c2ce7b84a9a008b8be337340a16bd1eefdd59010e5f6`; all required metadata tables were observed | PASS | `scratch/live-test-2026-09-25/stage1/right-connection-after-restore.json`; `scratch/live-test-2026-09-25/stage1/right-connection-objects-after-restore-corrected.json` |
  | 2 — legacy Alice apply | Create an applied `ACCOUNTS` history row without stored members | Applied with the pre-member-storage runner; `ACCOUNTS` verified and one legacy up event recorded | PASS | `scratch/live-test-2026-09-25/restart-continuation/stage2/legacy/legacy-apply-command.json`; `.../legacy/history-export.json` |
  | 2 — original missing-member scenario (pre-fix plan) | A fresh Alice apply with no Bob files should report `missing` / `incomplete` | Actual output was `already_stored` / `complete` because current applies store members before SQL; the stop rule halted the initial pass before tamper testing | FAIL, plan corrected in [PR #10](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/pull/10) to create a true legacy history | `scratch/live-test-2026-09-25/stage2/rerun/bob-adopt-members-dry-run.json`; `.../stop-on-first-unexpected.json` |
  | 2 — Bob missing-member report | No local Alice files produce `missing` and `incomplete` | Bob reported Alice under `missing`, `status=incomplete` | PASS | `scratch/live-test-2026-09-25/restart-continuation/stage2/legacy/bob-legacy-missing-command.json` |
  | 2 — tamper before storage | Conflicting local bytes are refused without writes | Exit 3 with checksum mismatch; no bundle stored | PASS | `scratch/live-test-2026-09-25/restart-continuation/stage2/legacy/bob-legacy-tamper-command.json` |
  | 2 — Alice member backfill | Preview then store all four members for the legacy row | Preview selected `would_store`; write stored four exact members and their hashes | PASS | `scratch/live-test-2026-09-25/restart-continuation/stage2/backfill-write/alice-backfill-write.json`; `.../backfill-verification-before-tamper/backfill-state-before-tamper.json` |
  | 2 — Bob after backfill | No local Alice files report `already_stored` and `complete` | Bob reported `already_stored` and `complete` | PASS | `scratch/live-test-2026-09-25/restart-continuation/stage2/bob-already-stored/bob-legacy-already-stored.json` |
  | 2 — tamper after storage | A conflicting local bundle is refused even when shared history already has the checksum | Exit 3: `local migration files differ from recorded history (checksum mismatch)`; before/after shared-state digests are identical. PR #10 contains the fix and regression test | PASS | `scratch/live-test-2026-09-25/restart-continuation/stage2/bob-tamper-after-storage/bob-legacy-tamper-after-storage.json`; `.../tamper-refusal-state-comparison.json`; [PR #10](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/pull/10) |
  | 3 — Bob foreign migration and Alice observation | Bob applies balances; Alice accepts it as foreign; Bob undo emits down event | Bob apply and Alice dry run passed; Bob undo passed, and the frontier returned to Alice-only digest `d4e1a384…` | PASS | `scratch/live-test-2026-09-25/restart-continuation/stage3/bob-balances-apply/bob-balances-apply.json`; `.../alice-foreign-dry-run/alice-foreign-migration-dry-run.json`; `.../bob-balances-undo/bob-balances-undo.json`; `.../post-bob-undo-frontier/post-bob-undo-frontier-check.json` |
  | 3 — shared-app E2E runtime | E2E is `UNKNOWN` without the documented disposable fixture and protected ORDS/browser runner | The plan's `docker-demo` fixture contract and runner were not available on approved `local-26ai`; no app/browser payload was attempted | UNKNOWN | `scratch/live-test-2026-09-25/restart-continuation/stage4/replay-target-qualification.json`; `docs/local-three-developer-e2e.md` |
  | 4 — source identity/runtime preflight | Accept the pinned SQLcl, Oracle and APEX versions without payload | Read-only preflight using `require_flow_runner=False` passed identity and runtime checks; DB `23.26.2.0.0`, APEX `26.1.4`, SQLcl `26.2.2.233.1901`; no release payload | PASS | `scratch/live-test-2026-09-25/stage4/schema-v0.1.0/combined-live-preflight.json` |
  | 4 — schema run-release-test flow runner (pre-fix) | Schema releases do not require an app flow runner | Initial attempt returned `TEAM_FLOW_RUNNER is required for declared flow checks` before SQLcl or payload. The fix is covered by an offline regression test; no live rerun was attempted because the only test profile is not isolated | FAIL, fixed in [PR #11](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/pull/11); live rerun UNKNOWN | `scratch/live-test-2026-09-25/stage4/schema-v0.1.0/release-test-empty-history-preflight.json`; `scripts/teamlib/online_workflows.py:582`; `scripts/tests/test_online_workflows.py:740` |
  | 4 — Oracle 26ai runtime marker (pre-fix) | Runtime query identifies Oracle DB and APEX versions | Initial read-only preflight reported `runtime version output must contain exactly database and apex markers`; the product filter was corrected, and the subsequent read-only runtime preflight passed | FAIL, fixed in [PR #12](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/pull/12); runtime retest PASS | `scratch/live-test-2026-09-25/stage4/schema-v0.1.0/runner-optional-live-preflight.json`; `.../combined-live-preflight.json`; `scripts/sql/runtime_versions.sql:8`; `scripts/tests/test_runtime.py:168` |
  | 4 — schema release cut and verify | Format 3 release `0.1.0` contains Alice up, Bob up/down and stored down members | Cut and verification passed; digest `b1f8fc78…`; source cut 3 and frontier `d4e1a384…` | PASS | `scratch/live-test-2026-09-25/restart-continuation/stage4/cut-0.1.0/schema-release-cut-0.1.0.json`; `.../verify-0.1.0/verify-schema-release-0.1.0.json`; `.../schema-release-verification.json` |
  | 4 — deterministic recut | Unchanged ledger and version produce the same archive digest | Recut digest equals the first cut: `b1f8fc78…` | PASS | `scratch/live-test-2026-09-25/restart-continuation/stage4/recut-0.1.0/schema-release-recut-0.1.0.json`; `.../digest-comparison.json` |
  | 4 — version binding after ledger change | Reusing `0.1.0` after a reversible migration is refused | Probe migration up/down was recorded; attempt refused with `release schema/v0.1.0 is already bound to a different archive`; release row stayed unchanged | PASS | `scratch/live-test-2026-09-25/restart-continuation/stage4/version-reuse-attempt/schema-release-version-reuse-refusal.json`; `.../state-verification/report.json` |
  | 4 — drift refusal and restoration | Rogue DDL refuses a new cut; test fixture is removed and accepted frontier restored | Builder refused with `status=drift`, listing `RELEASE_DRIFT_PROBE`; guarded cleanup succeeded and inventory returned to `d4e1a384…` | PASS | `scratch/live-test-2026-09-25/restart-continuation/stage4/drift-refusal/rogue-object-drift-refusal.json`; `.../drift-probe-drop-preflight/report.json`; `.../post-drift-cleanup-frontier/post-drift-cleanup-frontier.json` |
  | 4 — empty/earlier release replay and evidence | Run release test and sign evidence on isolated test histories | No qualified isolated target exists: `.env.test` resolves to the same LT_* schemas/database as the source; `targets/test.json` has placeholder identity/workspace values and sample app ID 102. No replay payload was attempted | UNKNOWN | `scratch/live-test-2026-09-25/restart-continuation/stage4/replay-target-qualification.json` |
  | 4 — app release | Throwaway APEX app, contract, stable capture and page-lock report qualify | Workspace `LT_RELEASE_TEST` exists, but no throwaway source/test app and target contract were supplied; build/deploy not attempted | UNKNOWN | `scratch/live-test-2026-09-25/restart-continuation/stage4/replay-target-qualification.json`; `scratch/live-test-2026-09-25/setup/apex-workspace-post-reboot-preflight.json` |
  | 5 — initial cleanup | Drop only LT_* users and their dedicated SQLcl aliases, then verify | Passed at 15:04 EEST: four users removed, four aliases deleted, zero LT users remained. The LT_* fixture was re-established after the reboot to continue live checks; repeat final cleanup after remaining work | PASS, repeat pending | `scratch/live-test-2026-09-25/stage5/drop-throwaway-users-corrected.json`; `.../verify-dropped-users.json`; `.../connections-delete-and-verify.json`; post-restart doctor evidence above |

  The first migration verifier failed because it queried `USER_TABLES` as
  `LT_VERIFY`, which cannot see `LT_DATA.ACCOUNTS`; the scratch verifier was
  corrected to query `DBA_TABLES` for `LT_DATA`. The live-plan example also
  omitted required inventory arguments and was refused before payload. Current
  applies store members before SQL, so the plan now creates a true legacy
  history with the pre-member-storage runner. The stored-member tamper root
  cause was that `adopt_members` checked `stored_index` before comparing the
  local migration checksum. The code now compares first at
  `scripts/teamlib/migrate.py:528`; its regression test is in
  `scripts/tests/test_migration_members.py:251` and PR #10. The initial
  missing-member expectation was a plan mismatch: fresh applies already store
  members (`scripts/teamlib/migrate.py:385-388`), so PR #10 updates the plan to
  use a genuine legacy history before testing backfill. The Stage 4 runner
  root cause was unconditional app-flow validation for a schema archive; PR
  #11 passes `require_flow_runner` based on selected app aliases at
  `scripts/teamlib/online_workflows.py:582`. The Oracle marker failure was
  caused by the product-name filter excluding Oracle AI Database; PR #12 fixes
  it in `scripts/sql/runtime_versions.sql:8`. The root `.env` still references
  retired alias `lt-meta`; Alice and Bob's refreshed profiles passed (latest
  recheck: `scratch/live-test-2026-09-25/restart-continuation/restart-check/alice-doctor-recheck.json`
  and `.../bob-doctor-recheck.json`). The initial Stage 5 cleanup passed, but
  the LT_* fixture was re-established after restart for continuation and remains
  available. The `ACCOUNTS` table and migration ledger remain; the balances and
  temporary release-probe tables are absent. Probe up/down events remain in
  METADATA.
- **Done when:** source stages 1–4 are reported with evidence, and the
  isolated empty/earlier schema replay plus required app qualification are
  either completed or remain explicitly UNKNOWN until their targets exist;
  Stage 5 cleanup follows only after the planned checks.

### 1.2 Delete stale branches — COMPLETE
The GitHub branch API confirmed `codex/pending-work-execution` is absent
(2026-09-25). Earlier branches (`claude/determined-hawking-v3qviv`,
`codex/p1-remediation-flow-simplification`,
`claude/hopeful-ride-eu60f9`) are also gone.

### 1.3 Check GitHub protection settings — REVIEWED / ACTION OPEN
Current read-only API evidence (2026-09-25): zero repository rulesets and no
branch protection on `main`; the `integration` environment exists but has no
protection rules or required reviewers. No repository self-hosted runners are
available. The tag-triggered `.github/workflows/release.yml` was removed from
`main` by PR #6. The owner must choose and apply the reviewer and branch rules
in GitHub settings. No settings were changed. Click paths:

- Environment reviewers: **Repository `Settings` → `Environments` → `integration`
  → `Required reviewers` → add the reviewer or team → `Save protection rules`**.
  Enable `Prevent self-review` if the person who starts an integration run must
  not approve it.
- Main ruleset: **Repository `Settings` → `Code and automation` → `Rulesets` →
  `Rulesets` → `New ruleset` → `New branch ruleset`**. Target `main`, set
  enforcement to `Active`, and select `Require a pull request before merging`,
  `Block force pushes`, and `Restrict deletions`. Under required status checks,
  add `offline` and select GitHub Actions as its expected source. `database-checks`
  is the workflow name; GitHub exposes the job name `offline` as the required
  status check.
  [GitHub's environment guide](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)
  and [ruleset guide](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/creating-rulesets-for-a-repository)
  document the current options.

### 1.4 Optional
- Codex reviews stopped on usage limits (PRs #3–#6); add credits if wanted.

## 2. Release from the development database

### 2.1 Phase 3b — COMPLETE OFFLINE; SCHEMA CUT LIVE PASS; TARGET REPLAY OPEN
Spec §2 (reverted migrations), §5 (format 3), delivery table row 3b.

- **Planning (`plan-release`)**
  - Target history must equal the collapsed state of the archive's first *k*
    events for some *k* (the target is behind the cut, never beside it);
    otherwise refuse.
  - Replay events after *k*: `up` of a migration not APPLIED on the target runs
    forward; `down` of a migration APPLIED on the target runs its down pair.
    An up/down pair entirely after *k* is a net no-op only when the target has
    no history row for that migration; existing target rows replay down/redo to
    consume the source sequence.
  - Strict mode accepts a REVERTED target entry only when the archive carries
    that migration and its `down` event.
- **Apply (`apply-release`, `run-release-test`)**
  - Execute retained events in order through the migration runner; each
    destructive operation receives only its exact confirmation entry.
  - Store the source event sequence and replay base separately from local
    `applied_sequence`, so skipped no-op pairs do not break replanning or later
    releases.
- **Evidence chain**
  - `evidence.py`, `qualification.py`, `sign-test-evidence` and `gen-runbook`
    bind the manifest `source` block (`history_cut`, `history_digest`,
    `frontier_digest`) for format 3; format 2 keeps its `source_commit` binding.
  - The production runbook lists every down transition explicitly.
- **Done when:** a format 3 archive with an up/up/down ledger applies to a
  fresh test history and to one that already holds an earlier release, the
  signed evidence verifies, and `gen-runbook` emits the down step.
- **Offline status:** implemented and covered by synthetic target tests for fresh
  and earlier histories, signed format 3 evidence, and an explicit down runbook
  step. Tests also replan after an omitted no-op pair and a later release, and
  replay down/redo for a migration already present on the target. Live target
  behavior remains UNKNOWN until 1.1.

### 2.2 Phase 4 — COMPLETE OFFLINE; LIVE ACCEPTANCE OPEN
Spec §3.
- App mutex (same app-scoped pause as publish), page locks KNOWN and empty, two
  equal captures, `required_migrations` = the APPLIED set at the schema cut.
- App checks and master contracts read from the building repository; their
  digests recorded in the manifest and `TEAM_RELEASE` (decision 2).
- Manifest format 3 for app releases (`source` adds app generation and tree
  digest).
- **Offline status:** two equal paused captures, KNOWN-empty lock checks,
  applied-migration prerequisites, and app/check/master digests are covered.
  Format 3 app qualification, signed evidence, and local runbook generation are
  also covered by synthetic tests. No live APEX capture was performed.

### 2.3 Phase 5 — COMPLETE OFFLINE
Spec §6, decision 4.
- Remove the Git-commit builder, `release-record.json`, tag identity and
  `.github/workflows/release.yml`; keep `verify-release` reading format 2 for
  already-signed handoffs.
- Fold `build-schema-release` into `build-release`.
- Write the local release runbook (build → `run-release-test` → sign →
  `gen-runbook`, all on the operator's machine) and update
  `docs/promotion.md`, `docs/ci.md` and the README release section; drop the
  interim "one designated repository" rule.
- **Offline status:** complete. The public Git builder, release-record and tag
  identity were removed; format 2 remains verifiable; the tag workflow is
  deleted. Synthetic format 2 archive creation now lives only in test fixtures.

## 3. Publish acknowledgement redesign — COMPLETE OFFLINE

- **Problem:** the pause notice prints the registered checkout UUIDs
  (`publish.py`, `format_publish_notice`) and `publish-app --ack` only checks
  that those same UUIDs were supplied, so a publisher can acknowledge on
  everyone's behalf. With separate repositories the database roster is the only
  coordination channel left.
- **Proposed:** `team.sh ack-publish <preparation-id>` run from each teammate's
  own checkout writes an acknowledgement (preparation digest, checkout UUID,
  host, user, time) to the control store; `publish-app` reads acknowledgements
  from the store and drops `--ack`.
- **Needs:** a control-store table added by `setup-state` without touching
  existing metadata; `publish.py`, `team.py`, `docs/import-pause.md`,
  `AGENTS.md`, tests.
- **Offline status:** acknowledgements are written by each registered checkout
  to shared control metadata and `publish-app` verifies that roster. Checkout
  UUID remains an environment-provided self-attestation, not cryptographic proof.

## 4. Hardening

| Item | Where | Done when |
|---|---|---|
| Production read-only must be enforced by a read-only database account | `doctor`/`qualify-target` query `SESSION_PRIVS`/`SESSION_ROLES` on the production profile; `docs/promotion.md` | Offline synthetic tests refuse write-capable profiles. Live account audit remains UNKNOWN until 1.1. |
| UNKNOWN publish results are classified by text matching | `scripts/teamlib/publish.py` | Typed exceptions decide UNKNOWN vs FAILED; covered by the full suite. |
| SHA-pinned actions need updates | `.github/dependabot.yml` (`github-actions`) | PRs #7–#9 reviewed and merged on 2026-09-25; pins match their release tags and their `offline` checks passed. Manual integration remains unrun because the repository has zero registered self-hosted runners. |
| Release builds need a byte-stable toolchain | exact SQLcl build for schema and app captures | Schema cut verified on SQLcl `26.2.2.233.1901`; app capture remains UNKNOWN because no throwaway APEX app was supplied. |
| METADATA is now the release system of record | `docs/metadata-backup-restore.md` | Backup/restore guidance added; live restore exercise remains open under 1.1. |
| Local test environments | `docs/toolchain.md`: run tests in a venv with `pip install -e '.[promotion,dev]'` (Debian's system `cryptography` crashes on import) | Explicit setuptools build and package discovery restrict the editable install to `scripts/team.py` and `scripts/teamlib/`; the documented install succeeded, all 730 tests passed, and Ruff passed in the venv. |

Dependabot review (the `offline` checks passed; manual integration remains unrun):

- [#9 setup-python 5.6.0 → 7.0.0](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/pull/9): **merged** by squash on 2026-09-25. Low risk. Its pin `5fda3b9` matches v7.0.0. The action moves from Node 20 to Node 24 (Actions Runner 2.327.1 or newer) and removes the `pip-install` input, which this workflow does not use. It runs in `database-checks` on GitHub-hosted `ubuntu-latest`; the PR's offline check passed.
- [#8 checkout 4.4.0 → 7.0.1](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/pull/8): **merged by squash** on 2026-09-25 (`bc5a964`). Its pin `3d3c42e` matches v7.0.1. Moderate integration risk: Node 24 requires Actions Runner 2.327.1 or newer; both workflow call sites set `persist-credentials: false`; the safer fork-checkout behavior does not affect this repository's `pull_request` and `workflow_dispatch` triggers. No self-hosted runner is registered, so manual integration has not run.
- [#7 upload-artifact 4.6.2 → 7.0.1](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/pull/7): **merged by squash** on 2026-09-25 (`fd000b4`). Its pin `043fb46` matches v7.0.1. Moderate integration risk: Node 24 requires Actions Runner 2.327.1 or newer; direct, unzipped uploads are opt-in (`archive: false`), so the current named ZIP upload retains its name and format. Artifact immutability was already in effect in v4. No self-hosted runner is registered, so manual integration has not run.

PRs #7–#9 are merged, and PR branches #7/#8 were deleted. The repository has
zero registered self-hosted runners; manually dispatch the integration workflow
when one is available.

## 5. Known limitations (tracked, not scheduled)

- `local-team-e2e.py run` stays fail-closed (`UNKNOWN`) until a protected
  ORDS/browser runner exists.
- Undo is global last-in-first-out: a developer may have to wait for a
  colleague to revert a later migration whose down files only that colleague
  holds. Once stored, METADATA can supply those files to format-3 replay.
- Releases built from different developer repositories may qualify against
  different app checks (decision 2); digests make this visible, not impossible.
- No transition path yet for a test or production target whose history came
  from Git-built (format 2) releases: format-3 planning proves the target is an
  exact prefix of the development ledger, and without replay markers it falls
  back to local sequence numbers, which will not match. Such a target is refused
  (fail-closed). If one exists, it needs a reviewed one-time baseline adoption
  before its first format-3 release.

## Remaining order

1. Provision an isolated, disposable schema replay target whose schemas and
   database identity differ from the live source. Run `run-release-test` and
   evidence verification against both empty and earlier-release histories.
2. Supply a throwaway APEX source/test app and matching target contract before
   app capture, page-lock qualification, app release or deploy testing.
3. Give separate explicit approval for the METADATA backup/restore rehearsal
   on an isolated copy. It has not run.
4. After the planned live checks, decide when to run Stage 5 cleanup. The
   LT_* users, SQLcl connections, `LT_RELEASE_TEST` workspace, snapshot and
   evidence remain in place.
5. When a self-hosted runner becomes available, dispatch the integration
   workflow to qualify the merged #7/#8 action updates.
6. Owner: choose and enforce the integration environment reviewer policy and
   `main` ruleset in 1.3.
7. Review and approve live-fix PRs #10, #11 and #12 before merging; they remain
   separate by root cause. Dependabot PRs #7–#9 were already approved and merged.

The remaining live gates require an isolated target, a throwaway APEX app, a
separate restore approval, or owner changes in GitHub settings. They are not
represented as passing offline tests.
