# Pending work

Single list of everything still open for this template, in the order it should
happen. Designs live where noted; this file says what is left, who does it, and
when it is done. Update it in the same commit that closes an item.

Last updated: 2026-09-25. Offline implementation of the release,
acknowledgement and hardening items is merged to `main` (PR #6), including
fixes for 17 review findings. Live work on `fix/live-migration-members` has
completed source Stages 0–3, schema release cuts in Stage 4, and Stage 5 cleanup
on approved throwaway `local-26ai`. The stored-member tamper bug was fixed with
a regression test; the offline gate passed at 730 tests. Isolated release
replay, app release, browser E2E, METADATA backup/restore, and production-profile
qualification remain UNKNOWN or require a separately supplied target/approval.
The pre-test snapshot and all live evidence are documented below.

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
(owner decisions recorded there). PRs #1 to #6 have not completed live
acceptance against an Oracle/APEX database.

## 1. Owner actions (no code)

### 1.1 Run the live database test plan — SOURCE ACCEPTANCE COMPLETE / QUALIFICATION OPEN
- **What:** `docs/live-test-plan.md` against the approved disposable `local-26ai` target. Live database work is complete for source stages 0–3, schema release cuts in Stage 4, and Stage 5 cleanup. Release replay on a separate test target, app release, browser E2E, backup/restore, and production privilege qualification remain open or UNKNOWN.
- **Current evidence (2026-09-25):** SQLcl is `26.2.2.233.1901`; Java is `21.0.12.1`; Oracle Free is `FREEPDB1`; APEX is `26.1.4` / `VALID`. The pre-test volume snapshot is `.sync-state/live-test-2026-09-25/local-26ai-oradata-pre-live-test-2026-09-25` (33 files, 7.0 GB). It was restored after the initial verifier failure, and the target was requalified before continuing. The final `local-26ai` container is healthy; ORDS is up. Stage 5 dropped `LT_DATA`, `LT_CODE`, `LT_META`, `LT_VERIFY` and deleted only `lt-tables`, `lt-code`, `lt-meta`, `lt-verify`. `lt-apex` remains read-only. The throwaway `TEAM_RELEASE` rows and migration ledger were removed with `LT_META`; release archives and test evidence remain under `scratch/`. The pre-test snapshot was not restored after the successful run because the final database frontier was checked clean before authorized cleanup.
- **Offline gate:** Ruff, shell syntax, 730 unit tests, `ci-doctor` (`valid: true`), `git diff --check`, and tracked-file CRLF scan passed after the migration fix. The root `.env` was temporarily hidden on the same filesystem for the unit test run because one test expects no implicit default profile; it was restored automatically. Final gate evidence is `scratch/live-test-2026-09-25/final/offline-gate.json` and `.log`.
- **Results:**

  | Stage / step | Expected | Actual | Status | Evidence |
  |---|---|---|---|---|
  | Offline gate | All required local checks pass | Ruff, shell syntax, 730 unit tests, `ci-doctor`, diff check and tracked CRLF scan passed; `.env` restored | PASS | `scratch/live-test-2026-09-25/final/offline-gate.json` |
  | 0 — snapshot and target | Recoverable copy and approved disposable target | Snapshot recorded; initial failed verifier restore had an empty diff and passed all hashes. Final target is healthy after cleanup. | PASS | `.sync-state/live-test-2026-09-25/local-26ai-oradata-pre-live-test-2026-09-25.sha256`; `scratch/live-test-2026-09-25/stage2/restore-postcheck.json`; `scratch/live-test-2026-09-25/stage5/verify-dropped-users.json` |
  | 0 — APEX and toolchain | APEX 26.1+, pinned SQLcl and Java | APEX 26.1.4 `VALID`; SQLcl `26.2.2.233.1901`; Java `21.0.12.1` | PASS | `scratch/live-test-2026-09-25/stage0/apex-installed-check.json`; `scratch/live-test-2026-09-25/initial-evidence.json` |
  | 0 — profiles and users | Four least-privilege throwaway profiles and valid Alice/Bob clones | LT users, grants, quotas, saved aliases and both `doctor` runs passed; the four users and their four SQLcl profiles were removed in Stage 5 | PASS | `scratch/live-test-2026-09-25/stage0/verify-lt-users-after-creation.json`; `scratch/live-test-2026-09-25/stage0/alice-doctor-after-restore.json`; `scratch/live-test-2026-09-25/stage0/bob-doctor-after-restore.json`; `scratch/live-test-2026-09-25/stage5/connections-delete-and-verify.json` |
  | 1 — identity guard | Reject wrong profile before payload; adopt the right frontier | Wrong profile refused with ORA-20901 before payload; right profile created expected metadata and sequence-zero frontier | PASS | `scratch/live-test-2026-09-25/stage1/wrong-connection-after-restore.json`; `scratch/live-test-2026-09-25/stage1/right-connection-after-restore.json` |
  | 2 — Alice migration and stored members | Apply reversible `ACCOUNTS`; store four members and verify | Durable state shows APPLIED, one up event, four bundle members, verifier PASS, table present, mutex clear. Initial CLI stdout/exit capture is UNKNOWN; postchecks establish DB outcome. | PASS (database state) | `scratch/live-test-2026-09-25/stage2/rerun/alice-accounts-apply-observed.json`; `scratch/live-test-2026-09-25/stage2/rerun/post-apply-verification.json` |
  | 2 — Bob without local files | Matching stored migration reports `already_stored` / `complete` | Observed as expected because new applies persist members before payload execution | PASS | `scratch/live-test-2026-09-25/stage2/rerun/bob-no-local-adopt-members-after-fix.json` |
  | 2 — tamper refusal | Refuse differing local bytes without changing stored members | Appended-comment fixture refused with `local migration files differ from recorded history (checksum mismatch): 20260925T083338__alice__accounts`; metadata digest and all member hashes were unchanged | PASS | `scratch/live-test-2026-09-25/stage2/rerun/bob-tamper-adopt-members-after-fix.json`; `scratch/live-test-2026-09-25/stage2/rerun/stage2-tamper-check-comparison.json` |
  | 2 — legacy missing-member backfill | Report pre-member history with no stored bundle as missing/incomplete | No such legacy row existed on `local-26ai`; offline tests cover the missing path | UNKNOWN (live scenario absent) | `scratch/live-test-2026-09-25/stage2/rerun/` |
  | 3 — separate repositories | Bob migration applies, is foreign to Alice, and can be undone | Bob applied `BALANCES`; Alice saw it as foreign; Bob's undo succeeded. Final source frontier returned to Alice APPLIED / Bob REVERTED. | PASS | `scratch/live-test-2026-09-25/stage3/bob-migrate-balances-apply.json`; `scratch/live-test-2026-09-25/stage3/alice-migrate-dry-run-after-bob-balances.json`; `scratch/live-test-2026-09-25/stage3/bob-undo-balances-apply.json`; `scratch/live-test-2026-09-25/stage3/bob-balances-post-undo-verification.json` |
  | 3 — three-developer browser harness | Run only against its qualified disposable fixture and protected browser/ORDS runner | Not run: harness is bound to `docker-demo` / `docker-sys`, apps 9099/9100 and `TEAM_E2E_META`, not the approved `local-26ai` target; no protected browser runner contract was available | UNKNOWN / NOT RUN | `scratch/live-test-2026-09-25/stage3/e2e-harness-status.json` |
  | 4 — source schema release cuts | Cut and verify deterministic ledger releases; bind versions; reject drift | `0.1.0` and `0.2.0` built and verified. Same-ledger recut had identical digest; reused version after ledger change was refused; sentinel drift was refused and then removed. Final sequence 1–5 frontier matched inventory, Alice is APPLIED, Bob REVERTED, no unresolved attempts or mutex. | PASS | `scratch/live-test-2026-09-25/stage4/build-schema-v0.1.0.json`; `scratch/live-test-2026-09-25/stage4/recut-determinism.json`; `scratch/live-test-2026-09-25/stage4/version-binding-refusal-check.json`; `scratch/live-test-2026-09-25/stage4/drift-test/drift-refusal-check.json`; `scratch/live-test-2026-09-25/stage4/final-source-frontier-verification.json` |
  | 4 — release replay on test histories | Replay empty and earlier-release histories and verify signed evidence | Not run: no isolated `.env.test` / role=`test` contract and schemas were supplied. `targets/test.json` is sample data referencing prohibited app 102, so it was not used. | UNKNOWN / NOT RUN | `scratch/live-test-2026-09-25/initial-evidence.json` |
  | 4 — app release and APEX test deploy | Capture a selected app twice and qualify against a throwaway APEX test app | No throwaway workspace/app and target contract supplied. `lt-apex` stayed read-only; app capture/deploy was not attempted. | UNKNOWN / NOT RUN | `scratch/live-test-2026-09-25/initial-evidence.json` |
  | 4 — signing and runbook | Sign verified test evidence and generate owner runbook | Not run without release-test evidence and a qualified test target | UNKNOWN / NOT RUN | `scratch/live-test-2026-09-25/initial-evidence.json` |
  | 5 — cleanup | Remove test schemas and their saved connections; verify target | SYS preflight passed on `FREEPDB1`; all four LT users dropped; exactly four `lt-*` profiles deleted; no unrelated aliases changed; `local-26ai` and ORDS are up | PASS | `scratch/live-test-2026-09-25/stage5/admin-cleanup-preflight.json`; `scratch/live-test-2026-09-25/stage5/drop-throwaway-users-corrected.json`; `scratch/live-test-2026-09-25/stage5/connections-delete-and-verify.json`; `scratch/live-test-2026-09-25/stage5/container-health.json` |
  | Evidence secret scan | Scratch output contains no credential strings | Twelve prior credential-bearing artifacts were sanitized; final scanner found no quoted `IDENTIFIED BY`, password assignments, or unredacted `secret_input` fields | PASS | `scratch/live-test-2026-09-25/stage0/evidence-redaction.json`; `scratch/live-test-2026-09-25/final/evidence-secret-scan.json` |

  The first migration verifier failed because it queried `USER_TABLES` as `LT_VERIFY`, which cannot see `LT_DATA.ACCOUNTS`. The scratch verifier was changed to query `DBA_TABLES` with `OWNER='LT_DATA'`; the migration then passed durable database verification. The initial plan invocation also lacked required `--expected-inventory` and `--actual-inventory` arguments and was refused before payload execution.

  The Stage 2 plan mismatch exposed a real backfill bug: when a stored bundle existed, `adopt_members` returned before comparing any matching local files. `scripts/teamlib/migrate.py` now verifies local file checksums before the `already_stored` shortcut; `scripts/tests/test_migration_members.py::test_local_files_that_disagree_with_stored_bundle_are_refused` proves the prior behavior fails and stored bytes remain unchanged. The live tamper fixture confirmed the refusal and no-write behavior. The fix is on `fix/live-migration-members` ([PR #10](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/pull/10)); review and approval are required before merge.

  The cleanup's first guarded script refused before DDL because its local guard expected `DB_NAME=FREE`; the read-only diagnostic showed the actual identity is `DB_NAME=FREEPDB1`, `SERVICE_NAME=freepdb1`. The guard was corrected and the cleanup then passed. Both the initial refusal and corrected output are retained. No production profile or app 102 was used; no APEX deploy, backup/restore, or production write was performed.
- **Done when:** source checks through Stage 5 remain PASS and the remaining isolated release test/app targets, protected browser runner, backup/restore approval, and production-like read-only profile are qualified or explicitly closed as unavailable.

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
in GitHub settings. No settings were changed.

### 1.4 Optional
- Codex reviews stopped on usage limits (PRs #3–#6); add credits if wanted.

## 2. Release from the development database

### 2.1 Phase 3b — COMPLETE OFFLINE; LIVE ACCEPTANCE OPEN
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
| Release builds need a byte-stable toolchain | exact SQLcl build for schema and app captures | Builders refuse other builds; live capture acceptance remains UNKNOWN. |
| METADATA is now the release system of record | `docs/metadata-backup-restore.md` | Backup/restore guidance added; live restore exercise remains open under 1.1. |
| Local test environments | `docs/toolchain.md`: run tests in a venv with `pip install -e '.[promotion,dev]'` (Debian's system `cryptography` crashes on import) | Explicit setuptools build and package discovery restrict the editable install to `scripts/team.py` and `scripts/teamlib/`; the documented install succeeded, all 730 unit tests passed on `fix/live-migration-members`, and Ruff passed in the venv. |

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

1. Provide a separate non-production `role: test` SQLcl profile, isolated test schemas, and matching target contract for schema `run-release-test` on empty and earlier-release histories. Do not use the sample `targets/test.json` contract (app 102) or the removed Stage 0 LT_* profiles.
2. Provide a throwaway APEX workspace/application and target contract for live app release/deploy qualification. `lt-apex` remains a read-only source. The `local-three-developer-e2e.py` fixture also needs its documented disposable target and a protected ORDS/browser runner before it can move beyond `UNKNOWN`.
3. With separate explicit OK, rehearse METADATA backup/restore on an isolated copy and record evidence in `docs/metadata-backup-restore.md`.
4. Provide a production-like, truly read-only SQLcl profile to qualify the privilege audit. No production profile was used in this run.
5. When a self-hosted runner becomes available, dispatch the integration workflow to qualify the merged #7/#8 action updates; Node 24 requires Actions Runner 2.327.1 or newer.
6. Owner: choose and enforce the integration environment reviewer policy and `main` ruleset in 1.3.
7. Review and approve [PR #10](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/pull/10) before merge. Do not merge it automatically.

The user-approved Dependabot PRs #7–#9 are already merged; no further merge action is pending for those PRs. Optional Codex reviews remain unrun because review usage limits were reached.

The remaining gates require an approved isolated database/APEX target, explicit recovery approval, a production-like read-only account, external runner availability, or the owner's GitHub settings/PR decision. They are not represented as passing offline tests.
