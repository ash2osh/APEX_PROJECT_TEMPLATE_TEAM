# Pending work

Single list of everything still open for this template, in the order it should
happen. Designs live where noted; this file says what is left, who does it, and
when it is done. Update it in the same commit that closes an item.

Last updated: 2026-09-25. Offline implementation of the release,
acknowledgement and hardening items is merged to `main` (PR #6), including
fixes for 17 review findings. The offline gate passed on `main` at
`4669d29f7dccd178d3f31bc29400827640c69b72`. On the approved throwaway
`local-26ai` target, Stage 0 and the Stage 1 rerun passed. The Stage 2
`ACCOUNTS` migration passed durable database verification, then Stage 2 stopped
at Bob's missing-member report: the live run reported `already_stored` because
the new migration flow stored all four members before applying it. The earlier
failed verifier attempt and the pre-test snapshot restore remain documented
below. No Stage 3 or 4 actions followed the stop.

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

### 1.1 Run the live database test plan — OPEN / STOPPED AT STAGE 2
- **What:** `docs/live-test-plan.md` on approved `local-26ai` with throwaway schemas.
- **Current evidence (2026-09-25):** the offline gate passed: Ruff, shell syntax,
  729 unit tests (1 skipped), `ci-doctor` (`valid: true`), `git diff --check`,
  and a tracked-file CRLF scan. SQLcl is `26.2.2.233.1901`; Java is
  `21.0.12.1`. Read-only inspection of `local-26ai` reported database `FREE`,
  PDB `FREEPDB1`, APEX `26.1.4` (`APEX` registry status `VALID`), and `USERS`.
  Its `oradata-26ai` volume was snapshotted at
  `.sync-state/live-test-2026-09-25/local-26ai-oradata-pre-live-test-2026-09-25`
  (33 files, 7.0 GB). That snapshot was restored after the first failed
  migration attempt; the restore diff was empty, all snapshot hashes passed,
  and the container is healthy. Stage 0 was then re-established: the four
  throwaway LT_* users have expected grants and quotas, fresh saved SQLcl
  aliases connect as the expected identities, and Alice/Bob `doctor` both pass.
  The APEX capture connection remains read-only; no APEX app was deployed.
- **Results:**

  | Stage / step | Expected | Actual | Status | Evidence |
  |---|---|---|---|---|
  | Offline baseline | All offline gates pass | Ruff, shell syntax, 729 tests (1 skipped), `ci-doctor`, `git diff --check`, and CRLF scan passed on the recorded baseline | PASS | `scratch/live-test-2026-09-25/initial-evidence.json` |
  | 0 — snapshot | Consistent rollback copy before writes | Snapshot restored after Stage 2; file diff empty, all 33 hashes pass, container healthy | PASS | `.sync-state/live-test-2026-09-25/local-26ai-oradata-pre-live-test-2026-09-25.sha256`; `scratch/live-test-2026-09-25/stage2/restore-copy-attempt2.json`; `scratch/live-test-2026-09-25/stage2/restore-postcheck.json` |
  | 0 — APEX installation | APEX available on target | `DBA_REGISTRY` reports APEX `26.1.4`, status `VALID` | PASS | `scratch/live-test-2026-09-25/stage0/apex-installed-check.json` |
  | 0 — LT users | Least-privilege throwaway users and quotas | Four users are OPEN; expected system/role grants and 100 MB quotas verified | PASS | `scratch/live-test-2026-09-25/stage0/verify-lt-users-after-creation.json` |
  | 0 — saved SQLcl connections | Four `-savepwd` aliases connect as their matching LT_* users | Fresh saved aliases verified for LT_DATA, LT_CODE, LT_META and LT_VERIFY | PASS | `scratch/live-test-2026-09-25/stage0/rotate-and-save-lt-connections-corrected.json` |
  | 0 — profiles and `.env` | Credential-free profiles and valid doctor in both clones | Alice and Bob doctor both valid after the snapshot restore | PASS | `scratch/live-test-2026-09-25/stage0/alice-doctor-after-restore.json`; `scratch/live-test-2026-09-25/stage0/bob-doctor-after-restore.json` |
  | 1 — wrong connection | Refuse before payload; no LT_DATA metadata objects | After restore, exit 3 with ORA-20901 before payload; no TEAM_* tables appeared in LT_DATA | PASS | `scratch/live-test-2026-09-25/stage1/wrong-connection-after-restore.json`; `scratch/live-test-2026-09-25/stage1/wrong-connection-no-tables-after-restore.json` |
  | 1 — right connection | Adopt sequence-zero frontier and create expected METADATA objects | After restore, LT_META frontier digest `4363cf314846dc29a8e3c2ce7b84a9a008b8be337340a16bd1eefdd59010e5f6`; all required metadata tables were observed | PASS | `scratch/live-test-2026-09-25/stage1/right-connection-after-restore.json`; `scratch/live-test-2026-09-25/stage1/right-connection-objects-after-restore-corrected.json` |
  | 2 — drift preflight | Reviewed inventory matches current schemas and accepted frontier | Fresh expected and actual inventories matched the accepted frontier with zero drift; dry run selected only Alice's `accounts` migration | PASS | `scratch/live-test-2026-09-25/stage2/rerun/inventory-preflight.json`; `scratch/live-test-2026-09-25/stage2/rerun/alice-migrate-dry-run.json` |
  | 2 — Alice reversible migration | Store four members, create ACCOUNTS, verify, record one up event and release mutex | Oracle shows attempt APPLIED, one up event, four members, verifier PASS, ACCOUNTS present and mutex released. The initial CLI stdout/exit capture is UNKNOWN; postchecks prove the database result | PASS (database state) | `scratch/live-test-2026-09-25/stage2/rerun/alice-accounts-apply-observed.json`; `scratch/live-test-2026-09-25/stage2/rerun/post-apply-verification.json`; `scratch/live-test-2026-09-25/stage2/rerun/team-sqlcl-logs.json` |
  | 2 — Alice backfill preview | Alice's applied migration is already stored | `already_stored` contained Alice's migration; no writes requested | PASS | `scratch/live-test-2026-09-25/stage2/rerun/alice-adopt-members-dry-run.json` |
  | 2 — Bob missing-member report | Missing local files produce `missing` and `incomplete` | **Unexpected:** Bob returned `already_stored` and `complete`, because the fresh migration flow stored the bundle in shared METADATA before applying it. Stop rule halted Stage 2 here | FAIL (plan scenario mismatch) | `scratch/live-test-2026-09-25/stage2/rerun/bob-adopt-members-dry-run.json`; `scratch/live-test-2026-09-25/stage2/rerun/stop-on-first-unexpected.json` |
  | 2 — tamper refusal | Conflicting local bytes are refused without writes | Not run after the first unexpected result; source inspection shows an existing bundle is short-circuited before local bytes are compared, so this case remains unproven | UNKNOWN | `scratch/live-test-2026-09-25/stage2/rerun/stop-on-first-unexpected.json`; `scripts/teamlib/migrate.py:528-540` |
  | 3 — shared repositories | Foreign migrations and undo work; browser run may be UNKNOWN | Not run because Stage 2 stopped. The E2E browser runner is also unavailable | UNKNOWN | `scratch/live-test-2026-09-25/stage2/rerun/stop-on-first-unexpected.json` |
  | 4 — schema release | Cut, deterministic digest, drift refusal, replay and evidence pass | Not run because Stage 2 stopped | UNKNOWN | `scratch/live-test-2026-09-25/stage2/rerun/stop-on-first-unexpected.json` |
  | 4 — app release | Throwaway app and target contract qualify | Not supplied; app release/deploy not attempted | UNKNOWN | `scratch/live-test-2026-09-25/initial-evidence.json` |

  The first migration attempt failed because its verifier queried
  `USER_TABLES` as `LT_VERIFY`, which cannot see `LT_DATA.ACCOUNTS`. The scratch
  verifier was corrected to use `DBA_TABLES` with `OWNER='LT_DATA'`, and the
  migration then passed database verification. The live-plan apply example
  also omits the required `--expected-inventory` and `--actual-inventory`
  arguments; the initial invocation was refused before payload execution.
  Earlier evidence remains under `scratch/live-test-2026-09-25/stage2/`.

  Stage 2 stopped at Bob's missing-member expectation. New applies call
  `store_members` before executing the payload (`scripts/teamlib/migrate.py:385-388`),
  while `adopt_members` reports a matching stored bundle as `already_stored`
  before looking for a local source file (`scripts/teamlib/migrate.py:528-534`).
  Therefore the `missing` expectation in `docs/live-test-plan.md:89-91` cannot
  occur after the newly applied Alice migration. The tamper step was not run;
  the same early branch means local conflicting bytes may not be checked when a
  matching bundle is already stored. Treat that as UNKNOWN pending a reviewed
  regression test and corrected test scenario. The command outputs and copied
  SQLcl logs for this rerun are retained under
  `scratch/live-test-2026-09-25/stage2/rerun/`. The original apply command's
  stdout/exit code was not captured; its durable Oracle state was verified
  independently. Stage 5 cleanup has not run, and the live database currently
  contains the throwaway LT_* users and Alice's `ACCOUNTS` table.
- **Done when:** stages 1, 2 and 4 pass, or their failures are reported with the
  command JSON and the named `.team-sqlcl-*.log`.

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
| Local test environments | `docs/toolchain.md`: run tests in a venv with `pip install -e '.[promotion,dev]'` (Debian's system `cryptography` crashes on import) | Explicit setuptools build and package discovery restrict the editable install to `scripts/team.py` and `scripts/teamlib/`; the documented install succeeded, all 729 tests passed with one expected skip, and Ruff passed in the venv. |

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

1. Resolve the Stage 2 backfill expectation and review the unproven tamper path;
   then continue Stage 2–4 with fresh evidence. Stage 5 cleanup follows only
   after the planned checks. The METADATA backup/restore rehearsal still needs
   its separate explicit OK.
2. When a self-hosted runner becomes available, dispatch the integration
   workflow to qualify the merged #7/#8 action updates.
3. Owner: choose and enforce the integration environment reviewer policy and
   main ruleset in 1.3.

The remaining gates require an approved database target, an owner decision for
external refs/reviewers, or external service activity; they are not represented
as passing offline tests.
