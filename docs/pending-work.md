# Pending work

Single list of everything still open for this template, in the order it should
happen. Designs live where noted; this file says what is left, who does it, and
when it is done. Update it in the same commit that closes an item.

Last updated: 2026-09-25. Offline implementation of the release,
acknowledgement and hardening items is merged to `main` (PR #6), including
fixes for 17 review findings. The offline gate passed on `main` at
`4669d29f7dccd178d3f31bc29400827640c69b72`. A candidate Oracle container was
queried read-only and snapshotted, but no live write acceptance has run.

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
(owner decisions recorded there). Nothing from PR #1 to #6 has run against a
live Oracle database yet.

## 1. Owner actions (no code)

### 1.1 Run the live database test plan — OPEN / UNKNOWN
- **What:** `docs/live-test-plan.md` on `docker-demo` with throwaway schemas.
- **Current evidence (2026-09-25):** the offline gate passed: Ruff, shell syntax,
  729 unit tests, `ci-doctor` (`valid: true`), `git diff --check`, and a tracked
  file CRLF scan. SQLcl is `26.2.2.233.1901`; Java is `21.0.12.1`. Read-only
  inspection of `local-26ai` reported database `FREE`, PDB `FREEPDB1`, APEX
  `26.1.4`, and `USERS`. Its persistent `oradata-26ai` volume was copied to
  `.sync-state/live-test-2026-09-25/local-26ai-oradata-pre-live-test-2026-09-25`
  (33 files, 7.0 GB); all SHA-256 checks passed and the container resumed.
  There is no Docker container named `docker-demo`, no repository `.env`, and no
  LT_* users or saved test connections. Owner approval of the test target is
  required before provisioning or running a database write.
- **Results:**

  | Stage / step | Expected | Actual | Status | Evidence |
  |---|---|---|---|---|
  | Offline baseline | All offline gates pass | All listed gates passed on `main` at `4669d29` | PASS | `scratch/live-test-2026-09-25/initial-evidence.json` |
  | 0 — snapshot | Consistent rollback copy before writes | Snapshot copied and verified; container running | PASS | `.sync-state/live-test-2026-09-25/local-26ai-oradata-pre-live-test-2026-09-25.sha256` |
  | 0 — profiles and `.env` | LT_* users, saved SQLcl profiles, valid doctor | Not run; awaiting target approval | UNKNOWN | `scratch/live-test-2026-09-25/initial-evidence.json` |
  | 1 — identity guard | Wrong profile refused before payload | Not run; awaiting target approval | UNKNOWN | `scratch/live-test-2026-09-25/initial-evidence.json` |
  | 2 — migration storage | Backfill and tamper checks behave as specified | Not run; awaiting target approval | UNKNOWN | `scratch/live-test-2026-09-25/initial-evidence.json` |
  | 3 — shared repositories | Foreign migrations and undo work; browser run may be UNKNOWN | Not run; target unavailable and no self-hosted runner is registered | UNKNOWN | `scratch/live-test-2026-09-25/initial-evidence.json` |
  | 4 — schema release | Cut, deterministic digest, drift refusal, replay and evidence pass | Not run; awaiting target approval | UNKNOWN | `scratch/live-test-2026-09-25/initial-evidence.json` |
  | 4 — app release | Throwaway app and target contract qualify | Not supplied; app release/deploy not attempted | UNKNOWN | `scratch/live-test-2026-09-25/initial-evidence.json` |

  No `.team-sqlcl-*.log` was created because no team SQLcl command was run.
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
| SHA-pinned actions need updates | `.github/dependabot.yml` (`github-actions`) | PRs #7–#9 observed and reviewed on 2026-09-25; pins match their release tags and the `offline` check passes. All remain open pending owner approval; see review notes below. |
| Release builds need a byte-stable toolchain | exact SQLcl build for schema and app captures | Builders refuse other builds; live capture acceptance remains UNKNOWN. |
| METADATA is now the release system of record | `docs/metadata-backup-restore.md` | Backup/restore guidance added; live restore exercise remains open under 1.1. |
| Local test environments | `docs/toolchain.md`: run tests in a venv with `pip install -e '.[promotion,dev]'` (Debian's system `cryptography` crashes on import) | Explicit setuptools build and package discovery restrict the editable install to `scripts/team.py` and `scripts/teamlib/`; the documented install succeeded, all 729 tests passed with one expected skip, and Ruff passed in the venv. |

Dependabot review (all three PRs remain open; only the `offline` job has passed):

- [#9 setup-python 5.6.0 → 7.0.0](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/pull/9): low risk. Its pin `5fda3b9` matches v7.0.0. The action moves from Node 20 to Node 24 (Actions Runner 2.327.1 or newer) and removes the `pip-install` input, which this workflow does not use. It runs in `database-checks` on GitHub-hosted `ubuntu-latest`; the PR's offline check passed.
- [#8 checkout 4.4.0 → 7.0.1](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/pull/8): moderate risk pending a manual integration run. Its pin `3d3c42e` matches v7.0.1. Node 24 requires Actions Runner 2.327.1 or newer; `persist-credentials` remains `true` by default, while both workflow call sites explicitly set it to `false`. The safer fork-checkout behavior does not affect this repository's `pull_request` and `workflow_dispatch` triggers. The protected integration job has no available self-hosted runner.
- [#7 upload-artifact 4.6.2 → 7.0.1](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM/pull/7): moderate risk pending a manual integration run. Its pin `043fb46` matches v7.0.1. It moves to Node 24 (Actions Runner 2.327.1 or newer); direct, unzipped uploads are opt-in (`archive: false`), so the current named ZIP upload retains its name and format. Artifact immutability was already in effect in v4. The protected integration job has no available self-hosted runner.

No Dependabot PR was merged. If #7/#8 are approved and a self-hosted runner is
registered, manually dispatch `.github/workflows/integration.yml` afterward.

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

1. Owner: approve `local-26ai` as the throwaway target or identify the correct
   `docker-demo` target; then complete 1.1. The METADATA restore rehearsal also
   needs the owner's explicit OK.
2. Owner: decide whether to merge Dependabot PRs #7–#9. If #7/#8 are merged and
   a self-hosted runner becomes available, dispatch the integration workflow.
3. Owner: choose and enforce the integration environment reviewer policy and
   main ruleset in 1.3.

The remaining gates require an approved database target, an owner decision for
external refs/reviewers, or external service activity; they are not represented
as passing offline tests.
