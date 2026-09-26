# Live database test plan

This runbook records the live acceptance procedure and its current evidence.
On 2026-09-25, the schema-release path was exercised against approved local
Oracle Docker targets: schema release cut and verification, format-3 replay
from empty and earlier-release histories, evidence signing, and offline
runbook verification passed. No APEX application was captured or deployed, no
app checks ran, and the METADATA backup/restore rehearsal remains open. Keep
those unknown gates separate from the schema results and use **throwaway
schemas only** for further live work.

| Stage | Proves | Introduced by |
|---|---|---|
| 1 | Identity guard refuses a wrong connection before any SQL runs | PR #1 |
| 2 | Migration files are stored in METADATA and backfilled | PR #4 |
| 3 | Two separate repositories share one database safely | PR #3 |
| 4 | A schema release is cut, replayed and qualified from the development database; an app is captured while paused | PR #5 and phases 3b–4 |

Budget one to two hours. Stop at the first unexpected result in stages 1, 2
or 4 and keep its evidence: those block the next release phase. Stage 3 is
informational.

Format-3 schema replay, application release capture, signed evidence and
runbook generation are implemented and have offline tests. The schema path has
the local Docker evidence noted above; live APEX application acceptance and
METADATA backup/restore remain open and must use disposable source and test
targets only.

## 0. One-time setup

1. **Snapshot the container** (`docker commit`, or copy its data volume). This
   is the rollback for everything below.
2. **Create throwaway users** as SYS in the pluggable database:
   - `LT_DATA` (TABLES profile) and `LT_CODE` (CODE profile), each with
     `CREATE SESSION`, `CREATE TABLE`, `CREATE PROCEDURE` and a tablespace
     quota.
   - `LT_META` (METADATA profile) with `CREATE SESSION`, `CREATE TABLE` and a
     quota.
   - `LT_VERIFY` (VERIFY profile) with `CREATE SESSION` and
     `SELECT_CATALOG_ROLE`.
3. **Save one SQLcl connection per profile.** Credentials stay in the SQLcl
   store, never in `.env`:

   ```text
   sql /nolog
   conn -save lt-tables -savepwd LT_DATA/<password>@localhost:1521/freepdb1
   conn -save lt-code   -savepwd LT_CODE/<password>@localhost:1521/freepdb1
   conn -save lt-meta   -savepwd LT_META/<password>@localhost:1521/freepdb1
   conn -save lt-verify -savepwd LT_VERIFY/<password>@localhost:1521/freepdb1
   ```

   Add `lt-apex` pointing at the parsing schema of an existing development
   application. It is a read-only source for app capture and must never be used
   as the `run-release-test` deployment target. Any live app qualification needs
   a separately provisioned throwaway test workspace/application and isolated
   target contract.
4. **Read each connection's real identity** and copy the values into the
   matching `*_EXPECTED_*` lines of `.env`:

   ```text
   sql -S -name lt-tables @scripts/sql/identity.sql
   ```

5. **Make two developer repositories**, `alice/` and `bob/`, each created from
   this template with no shared remote, both using the same `.env` values.
6. **Check:** `scripts/team.sh doctor` prints `"status": "valid"` in both.

## 1. Identity guard (PR #1)

1. **Wrong connection.** In `alice/.env` set
   `METADATA_SQLCL_CONNECTION=lt-tables`, then run
   `scripts/team.py --env .env adopt-frontier`.
   - Expect: `identity guard refused the session before the payload ran`.
   - Expect no team metadata in the wrong schema:
     `SELECT table_name FROM all_tables WHERE owner = 'LT_DATA' AND table_name LIKE 'TEAM%'`
     returns no rows.
2. **Right connection.** Restore `METADATA_SQLCL_CONNECTION=lt-meta` and run
   `adopt-frontier`.
   - Expect success, and `LT_META` holds `TEAM_MIGRATION_*`,
     `TEAM_MIGRATION_BUNDLE`, `TEAM_MIGRATION_MEMBER` and `TEAM_RELEASE`.

## 2. Migration files stored in the database (PR #4)

1. **Create an applied legacy migration without stored members.** Current
   migration applies store all four members before running SQL, so they cannot
   produce the missing-member case. Use a disposable developer checkout of the
   pre-member-storage runner to apply Alice's reversible `ACCOUNTS` migration
   and create the legacy history row. Verify the `up` event and table, then
   restore Alice's current checkout before running current commands.
2. **Bob, who does not have Alice's files,** runs
   `adopt-migration-members --dry-run`.
   - Expect Alice's migration under `missing` and `"status": "incomplete"`.
3. **Tampering before backfill.** Bob creates files with Alice's migration ID
   but different content and runs `adopt-migration-members`.
   - Expect a refusal naming a checksum mismatch; shared state stays unchanged.
4. **Alice backfills the legacy bundle.** Alice runs
   `adopt-migration-members` without `--dry-run`.
   - Expect all four members stored and their hashes to match Alice's source.
5. **Bob observes the completed backfill.** With no local Alice files,
   `adopt-migration-members --dry-run` lists the migration under
   `already_stored` and reports `complete`.
6. **Tampering after backfill.** Bob creates conflicting local files with
   Alice's stored migration ID and runs `adopt-migration-members`.
   - Expect a checksum-mismatch refusal even though the matching bundle is
     already stored; compare METADATA before and after to prove no write.

## 3. Two repositories, one database (PR #3)

1. **Bob authors and applies his own reversible migration** (`balances`).
   - Expect success: Alice's migration is accepted as applied even though its
     files are not in Bob's repository.
2. **Alice** runs `migrate --dry-run`.
   - Expect nothing pending; Bob's migration is reported as foreign.
3. **Bob reverts his migration:** `undo-migration <bob-migration-id>`.
   - Expect a `down` event in the history.
4. **Applications.** Run the harness in `docs/local-three-developer-e2e.md`:
   `preflight`, then `run`, then `cleanup --confirm-run-id <id>`.
   - Expect `UNKNOWN` from `run` without a protected ORDS/browser runner;
     that is the documented fail-closed result, not a failure.

## 4. Cutting and applying a database release (phases 3b–4)

The schema release cut, format 3 replay and paused app capture have offline test coverage. Use only an approved disposable `docker-demo` source/test setup from stage 0; `run-release-test` must target isolated throwaway schemas and a throwaway APEX app, never the development Builder source. No live Oracle acceptance is claimed by this repository change.

1. **Cut and verify a schema release.**

   ```text
   scripts/team.sh --env .env build-release --kind schema --version 0.1.0 --out scratch/r1
   scripts/team.sh verify-release scratch/r1/release.tar
   ```

   - Expect format 3 `source` metadata and Alice `up`, Bob `up`, Bob `down` events, with Bob's stored down members packaged.
   - Expect a `schema/v0.1.0` row in `TEAM_RELEASE`.
2. **Deterministic re-cut.** Cut the unchanged ledger as `0.1.0` into `scratch/r2`; expect the same archive digest.
3. **Version binding.** Change the ledger and attempt `0.1.0` again; expect refusal because the version is already bound. A new version must identify the new cut.
4. **Drift and unresolved attempts.** Create a rogue object and cut; expect refusal with the drift report. Restore the fixture and clear unresolved migration attempts only through the named recovery owner and reviewed evidence.
5. **Replay on test histories.** Run `plan-release`, `run-release-test` and evidence verification for both an empty history and an earlier release. Expect every up/down transition in order and no payload for an up/down pair that nets to no change.
6. **Build an app release.** Register the current checkout, ensure the selected source app has a known empty page-lock report, then run `build-release --kind app --alias hr`. Expect two equal capture digests, the APPLIED migration set at the schema cut, and app-check/master-contract digests in the manifest. If exercising `run-release-test` for that app, use a separately provisioned throwaway APEX test app and target contract; do not deploy to `lt-apex`.

Stop and preserve command JSON plus the named `.team-sqlcl-*.log` on any unexpected result. Record a stage as `UNKNOWN` if the approved target, runner, browser checks, or required profile are unavailable; offline tests are not live proof.

## 5. Clean up

Drop the `LT_*` users, delete the `lt-*` saved connections, and restore the
snapshot if anything looks wrong.

## What to report

For every step, keep the command's JSON output. For any failure, keep the
`.team-sqlcl-*.log` file it names; secrets are already redacted in it.
