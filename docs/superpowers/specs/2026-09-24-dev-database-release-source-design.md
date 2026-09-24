# Releases built from the development database — design

**Date:** 2026-09-24

**Status:** Decisions 1, 3, 4 accepted by the owner; decision 2 open. No behavior in this document is implemented merely by writing it.
**Scope:** This repository. Supersedes the Git-commit release source in `teamlib/release.py::build_release`.

## Why

Each developer keeps a **separate Git repository**; there is no shared remote.
What the team shares is the development database: one APEX workspace with the
same applications, and one TABLES/CODE schema stream recorded in the METADATA
ledger. Day-to-day work already coordinates through that database (app mutex,
checkout roster, page locks, migration mutex and history are all keyed by the
physical database identity, not by a repository).

Releases do not. `build_release` reads a Git commit, and test/production plan
migrations in `strict` mode (`release.py:922`), which refuses history entries the
archive does not contain. With one repository per developer, no repository holds
the whole migration stream, so the second developer to release is refused
("strict history contains foreign migration"). Tags such as `schema/v1.2.0` can
also be created with different content in different repositories.

The fix: **the development database is the single release source.** Git remains
where each developer authors and reviews; it is no longer where releases come from.

## Invariants (unchanged unless stated)

- Production writes stay refused; the signed test-evidence → offline runbook chain is unchanged and still binds the archive digest.
- Test/production keep `strict` migration planning. Because every archive is now cut from one ledger, strict becomes satisfiable again.
- Target identity is verified inside the SQLcl session before any payload (PR #1 identity guard).
- Nothing is released that the database cannot reproduce byte-for-byte and verify by checksum.

## What the database lacks today

| Release input | Today (Git) | In the dev database today |
|---|---|---|
| Migration SQL/verify/down members | `migrations/*.sql` in the commit | **Missing** — `TEAM_MIGRATION_HISTORY` keeps only `checksum`, `dependencies_json`, `payload_manifest_json` (observation), `source_commit` |
| Application source | `apps/<alias>/` in the commit | Present — the live APEX app, exported with SQLcl (`capture_app`) |
| App migration prerequisites | `app_context/<alias>/release.json` | Derivable — the history frontier at capture time |
| App checks, master contracts | `ci/app-checks/<alias>*`, `targets/masters.json` | **Missing** |
| Version identity / record | tag + `release-record.json` | **Missing** |

## Design

### 1. Migration members stored at apply time

New METADATA table `TEAM_MIGRATION_MEMBER (checksum, member_name, sha256, byte_length, content CLOB)`,
primary key `(checksum, member_name)`. `apply_plan` uploads every member of the
bundle (forward, verify, and down pair when reversible) inside the attempt,
before the payload runs; `record_applied` refuses unless the stored members
recompute to the history `checksum`. Content-addressed, so re-uploading is idempotent
and two developers applying the same bundle cannot disagree.

Backfill: `team.sh adopt-migration-members --source migrations/` uploads members for
already-applied IDs from whichever repository still has the files, accepted only
when they recompute to the recorded checksum. A release refuses while any
APPLIED migration in its cut lacks members.

Large members use the existing chunked transfer pattern (base64 lines, as the
schema inventory does) so no single statement exceeds SQLcl limits.

### 2. Schema release = a cut of the ledger

`build-release --kind schema --version X` (no `--ref`):

1. Read history under the migration mutex; refuse if any attempt is RUNNING/FAILED/UNKNOWN.
2. Cut = current highest `applied_sequence` (optionally `--through <sequence>`).
3. Archive every migration whose latest event in the cut is APPLIED, in applied order,
   with members read back and re-verified by sha256 and bundle checksum.
4. REVERTED migrations are excluded; the drift gate still requires the observed frontier
   to equal the frontier recorded at the cut.

Consequence to accept: **whatever is applied in shared dev at the cut ships.** Work in
progress must be undone (or not yet applied) before cutting. This matches how the shared
dev schema already behaves for everyone using it.

### 3. App release = a paused capture of the live app

`build-release --kind app --alias hr --version X`:

1. Acquire the existing app mutex (same app-scoped pause as publish; sibling apps unaffected).
2. Page-lock report must be KNOWN and empty; capture twice and require equal trees
   (the existing before/after capture rule).
3. `required_migrations` = the APPLIED set at the current schema cut, recorded with checksums
   (replaces `app_context/<alias>/release.json`).
4. Release the mutex; the manifest records the app generation and tree digest.

### 4. Release assets and version ledger in METADATA

- `TEAM_RELEASE_ASSET (kind, name, sha256, content CLOB, published_by, published_at)` holds
  `ci/app-checks/<alias>` bundles and `targets/masters.json`; `team.sh publish-release-assets`
  uploads them from a repository, so every developer releases with the same checks.
- `TEAM_RELEASE (kind, alias, version, archive_digest, history_cut, history_digest,
  app_generation, app_tree_digest, built_by, built_at)` with a unique key on
  `(kind, NVL(alias,'-'), version)`. Replaces `release-record.json`; a version can only
  ever mean one archive, whichever repository built it.

### 5. Manifest format 3

`source_commit` is replaced by
`"source": {"kind": "dev-database", "instance_id", "history_cut", "history_digest",
"app_generation", "app_tree_digest"}`. `verify-release`, `apply-release`,
`run-release-test`, `sign-test-evidence` and `gen-runbook` keep working on archive
bytes; format 2 archives remain verifiable for already-signed handoffs.

### 6. Where the build runs

The build needs development database access, so it cannot run on a hosted GitHub runner.

- **Decided:** an operator runs `build-release` locally and the archive digest is recorded in
  `TEAM_RELEASE`. `run-release-test`, `sign-test-evidence` and `gen-runbook` also run locally,
  with the test profile and signing key on the operator's machine.
- Tag-triggered `release.yml` is retired; tags stop being release identity.

## Owner decisions (2026-09-24)

1. **Cut rule — decided: everything APPLIED at head ships.** No per-migration release mark.
   Work in progress must be undone in shared dev before a cut.
2. **Release assets — open.** Store app checks (`ci/app-checks/<alias>*`) and master
   contracts (`targets/masters.json`) in METADATA, or read them from the builder's repository
   and record only their digests.
3. **Build/test host — decided: locally.** The operator builds the archive and runs
   `run-release-test`, signing and `gen-runbook` locally; the tag-triggered `release.yml`
   job is retired.
4. **Legacy path — decided: remove.** The Git-commit builder, `release-record.json`, tag
   identity and format-2 building are deleted. `verify-release` keeps reading format 2 only
   so already-signed handoffs stay checkable.

Follow-on: signing currently binds `source_commit` (`docs/ci.md`); format 3 binds the
`source` block (`history_cut`, `history_digest`, app generation/tree digest) instead.

## Delivery phases

| Phase | Change | Depends on |
|---|---|---|
| 0 | Security fixes (identity guard, signing key, credentials) — PR #1 | — |
| 1 | Docs/README/AGENTS for one-repo-per-developer; e2e with independent repos (no shared bare remote); `.env.example` connection names; drop dead `team.py::_store` | — |
| 2 | `TEAM_MIGRATION_MEMBER` + upload in `apply_plan` + backfill command | 1 |
| 3 | `TEAM_RELEASE` ledger + schema release from the ledger (format 3) | 2 |
| 4 | App release from paused capture + auto prerequisites + `TEAM_RELEASE_ASSET` | 3 |
| 5 | Remove the Git-commit builder and `release.yml`; local release runbook; promotion/CI docs | 4 |
| — | Publish acknowledgement redesign (checkout-issued acks) — independent, high priority | — |

## Risks

- **The dev database becomes the system of record** for releases: METADATA needs the same backup discipline as production data.
- **Work-in-progress leakage** through the cut rule (decision 1).
- **Export determinism:** app trees must be byte-stable across SQLcl versions; the toolchain pin (`sqlcl 26.2.1+`) should become an exact version for release builds.
- **Metadata upgrade:** existing installations need `setup-state` to add three tables without touching recorded history.
