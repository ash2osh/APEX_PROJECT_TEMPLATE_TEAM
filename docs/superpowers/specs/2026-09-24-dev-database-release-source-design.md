# Releases built from the development database — design

**Date:** 2026-09-24

**Status:** All four owner decisions are recorded. Release phases are implemented and offline-tested in the current worktree; live Oracle/APEX acceptance is still open.
**Scope:** This repository. Supersedes the former Git-commit release source; Git-backed archive fixtures are test-only, while production releases are cut from the shared database.

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
| Version identity / record | tag + `release-record.json` | `TEAM_RELEASE` binds release key, archive digest, and source metadata |

## Design

### 1. Migration members stored at apply time

New METADATA tables `TEAM_MIGRATION_BUNDLE (checksum PK, migration_id, member_count, stored_at, stored_by)`
and `TEAM_MIGRATION_MEMBER (checksum, member_name, byte_length, sha256, content_base64 CLOB)`,
primary key `(checksum, member_name)`, foreign key to the bundle. The bundle row and all its
members are written in one transaction. `apply_plan` stores every member (forward, verify, and
down pair when reversible) under the migration mutex before the attempt starts and before the
payload runs, then reads them back and recomputes the checksum. `record_event` refuses
(`MIGRATION_MEMBERS_MISSING`) unless the bundle row for that checksum and ID exists.
Content-addressed and immutable, so re-uploading is idempotent and two developers applying
the same bundle cannot disagree. Members are stored base64-encoded so bytes survive exactly.

Backfill: `team.sh adopt-migration-members --source migrations/` uploads members for
already-applied IDs from whichever repository still has the files, accepted only
when they recompute to the recorded checksum. A release refuses while any
APPLIED migration in its cut lacks members.

Large members use the existing chunked transfer pattern (base64 lines, as the
schema inventory does) so no single statement exceeds SQLcl limits.

### 2. Schema release = a cut of the ledger

`build-release --kind schema --version X --out DIR`:

1. Read history under the migration mutex; refuse if any attempt is RUNNING/FAILED/UNKNOWN.
2. Cut = the current highest `applied_sequence`; everything APPLIED at head ships.
3. Archive the **ledger event sequence** through the cut (every `up` and `down` event, in
   `applied_sequence` order), with members read back and re-verified by sha256 and bundle
   checksum. A migration whose latest event is REVERTED ships with its down members too.
4. The drift gate still requires the observed frontier to equal the frontier recorded at the cut.

Reverted migrations are **not** dropped from the archive. A migration may already have
shipped in an earlier release and be APPLIED on test/production, then be reverted in
development; if the next archive omitted it, strict planning would refuse the target's
history (it rejects both foreign APPLIED and foreign REVERTED entries) and the archive would
have no down transition to reach the cut. So:

- `apply-release` replays the archive's events past the target's own last event: a `down`
  event for a migration APPLIED on the target runs its authored down pair. An up/down pair
  entirely after the target cut is skipped as a net no-op only when that migration has no
  row in target history; existing target rows replay down/redo transitions to consume the
  source sequence even if the final status is unchanged.
- Strict planning accepts a REVERTED entry only when the archive carries that migration and
  its down event; anything else in target history that the archive does not carry is still
  refused.
- Undo in development already requires an authored down pair, so a reverted migration
  always has down members to ship. Irreversible migrations cannot be reverted and so never
  reach this path.
- A down transition on test is destructive under the existing destructive-confirmation
  convention; the production runbook lists it explicitly.

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

### 4. Release assets and version ledger

- App checks and master contracts come from the builder's repository (decision 2); their
  digests are recorded with the release.
- `TEAM_RELEASE (release_key PK, kind, alias, version, archive_digest, source_json, built_by, built_at)`,
  where `release_key` is `schema/vX.Y.Z` or `app/<alias>/vX.Y.Z` and `source_json` carries the
  manifest's `source` block (ledger cut and digests; app generation/tree and asset digests for
  app releases). Replaces `release-record.json`; a version can only ever mean one archive,
  whichever repository built it.

### 5. Manifest format 3

`source_commit` is replaced by
`"source": {"kind": "dev-database", "instance_id", "history_cut", "history_digest", "frontier_digest"}`
(app releases add their generation and tree digest in phase 4), and schema archives carry the
`events` ledger (`sequence`, `id`, `operation`, `checksum`). `verify-release` requires the events to
be contiguous from 1, valid per migration (up, then down only for reversible bundles), each carried
by a packaged bundle with the same checksum, no packaged bundle unused, and `history_cut` /
`history_digest` to match them. `verify-release`, `apply-release`,
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
2. **Release assets — decided: read from the builder's repository.** App checks
   (`ci/app-checks/<alias>*`) and master contracts (`targets/masters.json`) are taken from the
   repository that runs `build-release`, and their digests are recorded in the manifest and in
   `TEAM_RELEASE`. Accepted trade-off: two developers can release with different checks; the
   recorded digests make that visible after the fact, but do not prevent it.
3. **Build/test host — decided: locally.** The operator builds the archive and runs
   `run-release-test`, signing and `gen-runbook` locally; the tag-triggered `release.yml`
   job is retired.
4. **Legacy path — decided: remove.** The Git-commit builder, `release-record.json`, tag
   identity and format-2 building are deleted. `verify-release` keeps reading format 2 only
   so already-signed handoffs stay checkable.

Evidence, signing, and runbook generation bind the format-3 `source` block
(`history_cut`, `history_digest`, app generation/tree and asset digests); format 2
remains verifiable for existing signed handoffs.

## Delivery phases

Open items and their order are tracked in `docs/pending-work.md`.

| Phase | Change | Depends on |
|---|---|---|
| 0 | Security fixes (identity guard, signing key, credentials) — PR #1 | — |
| 1 | Implemented: Docs/README/AGENTS for one-repo-per-developer; e2e with independent repos (no shared bare remote); `.env.example` connection names; drop dead `team.py::_store` | — |
| 2 | `TEAM_MIGRATION_MEMBER` + upload in `apply_plan` + backfill command (`adopt-migration-members`) — implemented | 1 |
| 3a | Implemented: `TEAM_RELEASE` ledger and unified `build-release --kind schema` cut with drift gate; `verify-release` checks format 3 | 2 |
| 3b | Implemented: ordered up/down replay, strict target-prefix planning, format-3 evidence/signing, and explicit down runbook steps; offline tests pass, live acceptance open | 3a |
| 4 | Implemented: paused app capture, applied prerequisites, app-check/master digests, app format-3 qualification and local handoff; offline tests pass, live APEX capture open | 3b |
| 5 | Implemented: public Git builder, tag identity, release-record and tag-triggered workflow retired; local release runbook; format-2 verify retained | 4 |
| — | Implemented: checkout-issued publish acknowledgements; checkout identity remains a non-cryptographic self-attestation | — |

## Risks

- **The dev database becomes the system of record** for releases: METADATA needs the same backup discipline as production data.
- **Work-in-progress leakage** through the cut rule (decision 1).
- **Export determinism:** app trees must be byte-stable; schema and app release capture now require an exact SQLcl build. The live release path still needs acceptance on the approved target.
- **Check drift between repositories** (decision 2): releases built from different repositories may qualify against different checks.
- **Metadata upgrade:** existing installations get `TEAM_MIGRATION_BUNDLE`, `TEAM_MIGRATION_MEMBER` and `TEAM_RELEASE` from the idempotent migration bootstrap (`migrate --bootstrap`, `adopt-frontier`, `adopt-migration-members`, `build-release`) without touching recorded history.
