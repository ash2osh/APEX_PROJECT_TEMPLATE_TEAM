# Pending work

Single list of everything still open for this template, in the order it should
happen. Designs live where noted; this file says what is left, who does it, and
when it is done. Update it in the same commit that closes an item.

Last updated: 2026-09-24, after PR #5 (`038660d`).

## Where things stand

| Done | What |
|---|---|
| PR #1 | SQLcl identity guard runs before any payload; signing key only exists in the signing step; no persisted checkout credentials; stricter production read-only allowlist |
| PR #2 | Signing moved to a separate trusted job; GitHub Actions pinned by commit SHA |
| PR #3 | One Git repository per developer, no shared remote: docs, agent contract, three-repository e2e harness, `.env.example` |
| PR #4 | Migration files stored in METADATA (`TEAM_MIGRATION_BUNDLE`/`MEMBER`), `adopt-migration-members` backfill |
| PR #5 | `build-schema-release` cuts format 3 schema releases from the development ledger; `TEAM_RELEASE` version ledger |

Design for the release work: `docs/superpowers/specs/2026-09-24-dev-database-release-source-design.md`
(owner decisions recorded there). Nothing from PR #1 to #5 has run against a
live Oracle database yet.

## 1. Owner actions (no code)

### 1.1 Run the live database test plan — blocks 3b
- **What:** `docs/live-test-plan.md` on `docker-demo` with throwaway schemas.
- **Done when:** stages 1, 2 and 4 pass, or their failures are reported with the
  command JSON and the named `.team-sqlcl-*.log`.

### 1.2 Delete stale branches
Branch deletion is blocked from agent sessions; do it in GitHub → Branches.
- `claude/hopeful-ride-eu60f9` and `claude/determined-hawking-v3qviv`: fully
  contained in `main`, safe to delete.
- `codex/p1-remediation-flow-simplification`: **unrelated history** (no common
  ancestor with `main`, 107 commits, last 2026-09-10). Confirm nothing in it is
  still needed, or tag it (`archive/p1-remediation`) before deleting.

### 1.3 Check GitHub protection settings
- `test` and `integration` environments require reviewers.
- Until phase 5 retires tag-triggered releases, `schema/*` and `app/*/v*` tags
  are protected (Settings → Rules).

### 1.4 Optional
- Codex reviews stopped on usage limits (PRs #3–#5); add credits if wanted.

## 2. Release from the development database

### 2.1 Phase 3b — apply database-built schema releases
Spec §2 (reverted migrations), §5 (format 3), delivery table row 3b.

- **Planning (`plan-release`)**
  - Target history must equal the collapsed state of the archive's first *k*
    events for some *k* (the target is behind the cut, never beside it);
    otherwise refuse.
  - Replay events after *k*: `up` of a migration not APPLIED on the target runs
    forward; `down` of a migration APPLIED on the target runs its down pair; a
    migration applied and reverted entirely after *k* is a net no-op.
  - Strict mode accepts a REVERTED target entry only when the archive carries
    that migration and its `down` event.
- **Apply (`apply-release`, `run-release-test`)**
  - Execute the replay in event order through `apply_plan`/`apply_undo`
    machinery; down transitions go through the destructive-confirmation
    convention.
- **Evidence chain**
  - `evidence.py`, `qualification.py`, `sign-test-evidence` and `gen-runbook`
    bind `source_commit` today; bind the manifest `source` block
    (`history_cut`, `history_digest`, `frontier_digest`) for format 3.
  - The production runbook lists every down transition explicitly.
- **Done when:** a format 3 archive with an up/up/down ledger applies to a
  fresh test history and to one that already holds an earlier release, the
  signed evidence verifies, and `gen-runbook` emits the down step.

### 2.2 Phase 4 — application releases from a paused capture
Spec §3.
- App mutex (same app-scoped pause as publish), page locks KNOWN and empty, two
  equal captures, `required_migrations` = the APPLIED set at the schema cut.
- App checks and master contracts read from the building repository; their
  digests recorded in the manifest and `TEAM_RELEASE` (decision 2).
- Manifest format 3 for app releases (`source` adds app generation and tree
  digest).

### 2.3 Phase 5 — retire the Git release path
Spec §6, decision 4.
- Remove the Git-commit builder, `release-record.json`, tag identity and
  `.github/workflows/release.yml`; keep `verify-release` reading format 2 for
  already-signed handoffs.
- Fold `build-schema-release` into `build-release`.
- Write the local release runbook (build → `run-release-test` → sign →
  `gen-runbook`, all on the operator's machine) and update
  `docs/promotion.md`, `docs/ci.md` and the README release section; drop the
  interim "one designated repository" rule.

## 3. Publish acknowledgement redesign — high priority, independent

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

## 4. Hardening

| Item | Where | Done when |
|---|---|---|
| Production read-only must be enforced by a read-only database account | `doctor`/`qualify-target` query `SESSION_PRIVS`/`SESSION_ROLES` on the production profile; `docs/promotion.md` | a production profile with any write privilege is refused |
| UNKNOWN publish results are classified by text matching | `scripts/teamlib/publish.py` (`"unknown" in str(exc)`) | typed exceptions decide UNKNOWN vs FAILED |
| SHA-pinned actions need updates | add `.github/dependabot.yml` (github-actions) | Dependabot opens pin-update PRs |
| Release builds need a byte-stable toolchain | pin an exact SQLcl version for `build-schema-release` and app captures | builds refuse other SQLcl versions |
| METADATA is now the release system of record | backup/restore guidance for the METADATA schema | documented and exercised once |
| Local test environments | `docs/toolchain.md`: run tests in a venv with `pip install -e '.[promotion,dev]'` (Debian's system `cryptography` crashes on import) | note added |

## 5. Known limitations (tracked, not scheduled)

- `local-team-e2e.py run` stays fail-closed (`UNKNOWN`) until a protected
  ORDS/browser runner exists.
- Undo is global last-in-first-out: a developer may have to wait for a
  colleague to revert a later migration whose down files only that colleague
  holds (now also recoverable from METADATA once 3b can apply them).
- Releases built from different repositories may qualify against different app
  checks (decision 2); the recorded digests make this visible, not impossible.

## Suggested order

1. Owner: 1.1 live test plan, 1.2 branches, 1.3 settings.
2. 3 (publish acknowledgements) and 2.1 (phase 3b) — independent, can overlap.
3. 2.2 (phase 4), then 2.3 (phase 5).
4. Hardening items as capacity allows; the read-only production check first.
