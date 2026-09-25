# Live database test plan

The offline suite and CI prove the Python contracts; this plan records live
Oracle/APEX acceptance on the approved disposable `local-26ai` target, using
**throwaway schemas only**. Current outcomes and evidence are kept in
`docs/pending-work.md` §1.1. Source migration and schema-release checks passed;
release replay and app qualification need separate test targets:

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
runbook generation are implemented and have offline tests. Live Oracle/APEX
acceptance remains open and must use disposable source and test targets only.

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

1. **Alice authors and applies a reversible migration.**

   ```text
   scripts/team.py new-migration --author alice --slug accounts --target tables
   # write CREATE TABLE ACCOUNTS(ID NUMBER), its verify query, and the down pair
   scripts/team.py --env .env migrate
   ```

   - Expect `TEAM_MIGRATION_BUNDLE` with one row and `TEAM_MIGRATION_MEMBER`
     with four rows for its checksum, and one `up` history event.
2. **Alice checks the backfill.** `adopt-migration-members --dry-run` lists
   her migration under `already_stored`.
3. **Bob, who does not have Alice's files,** runs
   `adopt-migration-members --dry-run`.
   - Expect Alice's migration under `already_stored` and `"status": "complete"`:
     a new migration stores its members in METADATA before the payload runs.
4. **Legacy backfill.** If the target has history from before migration members
   were stored and has no bundle for that history, run the dry-run as Bob.
   - Expect that migration under `missing` and `"status": "incomplete"`. If no
     such legacy history exists on the throwaway target, record this live case
     as `UNKNOWN`; the offline tests cover the missing-member path. Do not create
     synthetic history on the shared development database to force this case.
5. **Tampering.** Bob creates files with Alice's migration ID but different
   content and runs `adopt-migration-members --dry-run`.
   - Expect a refusal naming a checksum mismatch. Verify the stored bundle and
     member bytes remain unchanged.

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

The schema release cut, format 3 replay and paused app capture have offline test coverage. Use the approved disposable `local-26ai` target as the development source from Stage 0. `run-release-test` requires a separate isolated non-production `role: test` profile, schemas and matching target contract; it must not reuse the source profile or target app 102. Live app qualification additionally requires a throwaway APEX workspace/application and target contract. `targets/test.json` is sample data for app 102 and is not an approved live target contract.

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
5. **Replay on test histories.** On a separately provisioned isolated test profile, run `plan-release`, `run-release-test` and evidence verification for both an empty history and an earlier release. The test profile and contract must be distinct from the Stage 0 source profiles; do not reuse `targets/test.json` while it references app 102. Expect every up/down transition in order and no payload for an up/down pair that nets to no change.
6. **Build an app release.** Register the current checkout, ensure the selected source app has a known empty page-lock report, then run `build-release --kind app --alias hr`. Expect two equal capture digests, the APPLIED migration set at the schema cut, and app-check/master-contract digests in the manifest. If exercising `run-release-test` for that app, use a separately provisioned throwaway APEX test app and target contract; do not deploy to `lt-apex`.

Stop and preserve command JSON plus the named `.team-sqlcl-*.log` on any unexpected result. Record a stage as `UNKNOWN` if the approved target, runner, browser checks, or required profile are unavailable; offline tests are not live proof.

## 5. Clean up

After confirming the expected final frontier, drop the four `LT_*` users and
delete the exact `lt-tables`, `lt-code`, `lt-meta`, and `lt-verify` saved
connections. Preserve `lt-apex` as a read-only capture source. Verify container
health and the absence of the throwaway users/aliases. Restore the snapshot only
if evidence shows the target needs rollback.

## What to report

For every step, keep the command's JSON output. For any failure, keep the
`.team-sqlcl-*.log` file it names; secrets are already redacted in it.
