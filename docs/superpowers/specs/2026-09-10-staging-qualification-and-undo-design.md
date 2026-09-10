# Staging qualification and migration undo/redo — Design

**Date:** 2026-09-10
**Status:** Draft, pending review
**Supersedes:** the disposable-Docker replay job described in
`docs/superpowers/plans/2026-09-09-review-remediation.md` and
`docs/superpowers/plans/2026-09-10-guard-and-workflow-remediation.md`. Those
plans' other tasks are unaffected; only the CI-replay/provisioner portions are
replaced by this document.
**Spec:** [Team design](2026-09-06-team-template-design.md) — the safety
contract this design must not weaken.

---

## 1. Why this changes

The template's CI proves migrations are safe by provisioning a throwaway
Oracle Free + ORDS pair inside Docker for every push
(`ci/provisioners/docker_pdb.sh`), then running the full migration history
against it from scratch. This has never actually completed successfully: the
job assumes real Oracle SQLcl is on the runner's `PATH`, which is true on
neither GitHub-hosted Actions runners (confirmed: this repository has zero
self-hosted runners registered) nor most developers' machines (Docker itself
is not guaranteed either). The guard that was meant to fail cleanly in that
case (`command -v sql`) instead matches an unrelated binary that happens to be
named `sql` on the stock runner image, so the job fails deep into a 15-30
minute run with a nonsensical SQLcl argument-parsing error instead of the
intended clear refusal.

This is a template-level default, so it is guidance every team that forks this
repository inherits. Most teams will not have a spare Oracle instance to spin
up in Docker inside CI. The design must change, not just the bug.

### What this document does

1. Replaces disposable-Docker qualification with an **optional** check that
   deploys to a real, persistent staging/UAT target — the same target
   `integration.yml` already deploys to.
2. Because that target is shared and persistent (not thrown away after the
   run), a bad deploy needs a real recovery path. Part 3 adds **authored
   down-migrations** with explicit `undo`/`redo` commands, written by
   whichever coding agent authors the forward migration.

### Non-goals

- This does not restore a "proves a fresh install works from zero" guarantee.
  That property dies with the disposable container. The provisioner
  abstraction it depended on (`ci-replay`, `_invoke_provisioner`, and
  `docker_pdb.sh` itself, per §2.1) is deleted outright, not kept-but-unwired
  — it never ran successfully in this repository's history and would bit-rot
  silently if left in place. A team that wants that guarantee back is
  building a new mechanism, not resuming a disabled one; the deleted code
  remains recoverable from git history as a starting point if useful.
- No automatic/generated DDL reversal. `docs/promotion.md` already states
  "DDL rollback is not assumed"; this design does not contradict that. Undo
  only ever runs SQL a human or agent actually wrote and committed.
- No change to the production promotion path (`gen-runbook`,
  `apply-release`'s production refusal). Undo/redo apply only through the
  same non-production `migrate`/`deploy-app` commands forward migrations
  already use.

---

## 2. Part A — Optional staging qualification

### 2.1 What is removed

| Removed | Why |
|---|---|
| `ci/provisioners/docker_pdb.sh` | Docker+SQLcl disposable provisioning; unworkable per §1. |
| `scripts/ci_replay_runner.py` | Only ever invoked against the disposable instance. |
| `replay` job in `.github/workflows/database-checks.yml` | Depends on the above. |
| `qualification` job in `.github/workflows/integration.yml` | Polls for the `replay` job's evidence artifact, which no longer exists. |
| `ci-replay` CLI command and `_invoke_provisioner`/provisioner-path validation in `scripts/teamlib/ci.py` | Provisioner abstraction has no remaining caller. |
| `provisioner`/`runner` fields and their `ci-doctor` checks in `ci/runner-contract.json` / `ci.py` | Same. |
| Replay-specific cases in `scripts/tests/test_ci_contract.py` (`ReplayWorkflowIsolationTests`, provisioner-path tests) | Test the removed contract. |

`ci-doctor`'s checks that are *not* specific to the disposable provisioner
(the Python-version-pin check across workflow files, secret-field scanning)
stay — they still apply to the workflows that remain.

### 2.2 What replaces it

`database-checks.yml` keeps only its `offline` job (unit tests + `ci-doctor`).
That job is fast (~15s), needs no external infrastructure, and is already
fixed and green.

`integration.yml` is trimmed to a single job that does exactly what its
`deploy` job already does — drift-check, `migrate`, `deploy-app` against
`targets/integration.json` (already modeled as `"environment": "staging"`, no
new target file needed) — with two changes:

- **Trigger:** `workflow_dispatch` instead of `on: push: branches: [main]`.
  This is the mechanism for "optional": a push to `main` never blocks on
  staging reachability, and a team that has staging configured runs it
  on demand (a human, or a follow-up agent turn, triggers it after a push
  they want qualified).
- **No qualification gate to wait for.** The job runs the deploy directly;
  there is no longer a separate "wait for disposable-replay evidence"
  polling step, because there is no longer a disposable run producing that
  evidence.

A team without `TEAM_ENV_FILE`/`TEAM_APP_ALIASES` configured simply never
triggers this workflow — it does not appear as a failing or skipped
required check, because it is not a required check and is not
push-triggered.

### 2.3 What a team gets by default vs. what they configure

Shipped working with zero configuration: `template-checks` (lint/shell/JSON)
and `database-checks` (unit tests + contract doctor). Requires the team to
configure `TEAM_ENV_FILE`, `TEAM_APP_ALIASES`, and the environment secrets
`integration.yml` already documents: `integration` (optional, manual staging
qualification).

---

## 3. Part B — Migration undo/redo

### 3.1 Bundle format: the optional down member

A migration bundle may now include a third file, `<id>.down.sql`, alongside
the existing `<id>.sql` (forward) and `<id>.verify.sql` (verification)
members.

- **Presence is optional.** Unlike `.verify.sql` (always required to exist,
  content may be empty), `.down.sql` may not exist at all. A migration with
  no down member cannot be undone — `undo-migration` refuses by name rather
  than attempting anything.
- **Header:** `migration-version` and `destructive` directives, same syntax
  as the forward member. `target` is inherited from the sibling `.sql`
  member (a down-script always targets the same schema role its forward
  script did). `depends-on` is not permitted in a down member — undo
  ordering is structural (§3.3), not authored.
- **Content safety:** scanned by the same `_assert_controls` pass as the
  forward member (no nested `@`/`START`/`SCRIPT` includes, no client
  commands, no substitution/error-policy changes). Unlike `.verify.sql`, it
  is *not* restricted to `SELECT` — its job is to run real DDL/DML.
- **Checksum:** joins the bundle checksum alongside the other members, so a
  down-script is as immutable and as tied to its declared dependents'
  checksums as the forward script already is.
- **Authoring:** `new-migration` does not scaffold a `.down.sql` stub —
  not every migration warrants one, and creating the file is a plain text
  write with the same header-comment convention as the forward member. The
  coding agent authoring the forward migration creates `.down.sql` directly
  when a reverse path exists and is worth committing.

### 3.2 History ledger: three states, not two

`TEAM_MIGRATION_HISTORY` currently has `PRIMARY KEY (id)` — one row per
migration, forever. Undo/redo means a migration's lifecycle can be
`(never applied) → applied → reverted → [redone → reverted → ...]`, so the
ledger needs to hold more than one row per id.

**Schema change** (`SqlMigrationStore.bootstrap`, both the fresh-install
`CREATE TABLE` and a new `ALTER TABLE` path for existing installations):

- Add `operation VARCHAR2(4) NOT NULL CHECK (operation IN ('up','down'))`.
- Change the primary key from `(id)` to `(id, applied_sequence)`.
- `bootstrap()` must detect the old shape (column absent) and migrate it in
  place — existing rows all get `operation = 'up'` — before proceeding. This
  is the control schema's own upgrade path; it is exercised the same way any
  other upgrade is (see §4).

The local JSON-backed `MigrationStore` (used where there is no SQL
controller) gets the equivalent change: history becomes a list of entries
per id instead of a single dict value, or an `operation` field per entry —
implementation detail, same externally-observed behavior as below.

**`read_history`'s per-id collapse** (`migration_store.py`) already
overwrites earlier rows with later ones in a loop ordered by
`applied_sequence` — this is unchanged. What changes is what `status` it
reports for the winning row:

| Latest row's `operation` | Reported `status` |
|---|---|
| `up` | `APPLIED` |
| `down` | `REVERTED` (new status literal) |

**`plan_migrations`** (`migration_plan.py`) gains `REVERTED` to its
recognized-status set, but treats it as neither `APPLIED` nor ordinary
pending:

- Not `APPLIED` — a dependent migration cannot treat a reverted dependency
  as satisfied (consistent with §3.3: if anything depended on it, LIFO
  ordering means that dependent must already have been undone first).
- **Not auto-pending either.** This is the correction from the original
  approach ("redo is just running migrate again"): if a reverted migration
  fell back into the ordinary pending set, the next routine `migrate` run
  would silently reapply it — even when the team undid it *because it was
  wrong* and intends to replace it with a different migration, not
  reapply it verbatim. A `REVERTED` migration is excluded from
  `Plan.pending`. It only comes back through the explicit `redo-migration`
  command in §3.4.

### 3.3 Undo: a global LIFO stack

The observation chain (`TEAM_MIGRATION_OBSERVATION`) is a single sequence
across *all* migrations regardless of `target` (tables/code) — each entry's
`before_digest` must equal the previous entry's `after_digest`. Undo has to
respect that same total order: **you can only undo the single most recently
applied migration** (highest `applied_sequence` whose current status is
`APPLIED`), full stop, regardless of `target`. This is the same reasoning
forward `depends-on` ordering already uses, applied in reverse.

`undo-migration <id>` refuses if `<id>` is not currently the latest applied
entry, naming whichever migration actually is. There is no "force" flag —
the fix for wanting to undo something buried under later migrations is to
undo the later ones first, same as the existing model expects dependents to
be ordered correctly rather than overridden.

### 3.4 New apply paths

Both new functions live in `migrate.py`, next to `apply_plan`, and reuse its
mutex/attempt/observation-chain machinery (acquire → attempt-start →
execute → verify → observe → record → release) rather than duplicating it.

- **`apply_undo(source, profiles, migration_id)`** — refuses unless
  `migration_id` is the current LIFO top (§3.3) and has a `.down.sql`
  member. Executes the down SQL where the forward path executes the up SQL.
  Destructive down-scripts require the same explicit confirmation forward
  destructive migrations already require. Records a new history row with
  `operation='down'`.
- **`apply_redo(source, profiles, migration_id)`** — refuses unless
  `migration_id`'s current status is `REVERTED`. Executes the *forward*
  `.sql` member again (not a new script) through the same path
  `apply_plan` uses per-migration. Records a new history row with
  `operation='up'`.

### 3.5 CLI

`scripts/team.py` gains two online commands (env-required, same parser
family as `migrate`/`deploy-app`):

```text
scripts/team.py --env <file> undo-migration <id>
scripts/team.py --env <file> redo-migration <id>
```

---

## 4. Rollout for existing installations

A team that already ran `bootstrap()` under the old two-column-key schema
needs the `ALTER TABLE ... ADD operation ...` / primary-key-swap path in
§3.2 to run once against their real metadata schema before `undo-migration`
or `redo-migration` are usable there. `bootstrap()` already runs
idempotently on every `migrate --bootstrap`, so this is not a separate
migration step a team has to remember — the existing bootstrap call picks it
up automatically, the same way `create_if_missing` already makes fresh
installs and existing ones converge.

---

## 5. Testing

Existing conventions apply throughout (TDD; `scripts/tests/` mirrors
`scripts/teamlib/` module-for-module):

- `test_migration_bundle.py`: down-member parsing, optional presence,
  checksum inclusion, `_assert_controls` reuse, rejection of `depends-on`
  in a down header.
- `test_migration_plan.py`: `REVERTED` excluded from both `APPLIED` and
  `pending`; a dependent's `depends-on` check still fails against a
  reverted dependency.
- `test_migrate.py`: `apply_undo`/`apply_redo` happy paths; LIFO refusal
  naming the true top-of-stack; destructive-confirmation gate on undo;
  refusal to undo/redo without a `.down.sql` member or without `REVERTED`
  status respectively.
- `test_ci_contract.py`: remove the provisioner/replay-specific cases
  listed in §2.1; keep the Python-version-pin and secret-scanning cases.
- Workflow-level: extend `test_release_workflow.WorkflowContractTests` (or
  a sibling) to assert `database-checks.yml` no longer references
  `docker_pdb.sh`, and `integration.yml` triggers on `workflow_dispatch`
  rather than `push`.

## 6. Docs to update

- `README.md`: drop the Docker/SQLcl-runner CI description; note staging
  qualification is optional and manually triggered.
- `docs/migrations.md`: new "Undo and redo" section describing §3.1-3.5 at
  user level.
- `docs/promotion.md`: the existing "DDL rollback is not assumed" sentence
  gains one clause — rollback is not *automatic*, but an authored, reviewed
  down-migration is the supported path when a bundle provides one.
