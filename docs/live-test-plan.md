# Live database test plan

The offline suite and CI prove the Python contracts; none of the following has
been exercised against a real Oracle database yet. This plan checks, on the
local `docker-demo` database and with **throwaway schemas only**:

| Stage | Proves | Introduced by |
|---|---|---|
| 1 | Identity guard refuses a wrong connection before any SQL runs | PR #1 |
| 2 | Migration files are stored in METADATA and backfilled | PR #4 |
| 3 | Two separate repositories share one database safely | PR #3 |
| 4 | A schema release is cut from the development database | PR #5 |

Budget one to two hours. Stop at the first unexpected result in stages 1, 2
or 4 and keep its evidence: those block the next release phase. Stage 3 is
informational.

Still out of scope: applying a database-built (format 3) release to test or
production and generating its runbook, and application releases. Those are
not implemented yet.

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
   application; stages 1, 2 and 4 never write to it.
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
   - Expect Alice's migration under `missing` and `"status": "incomplete"`.
4. **Tampering.** Bob creates files with Alice's migration ID but different
   content and runs `adopt-migration-members`.
   - Expect a refusal naming a checksum mismatch; nothing is stored.

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

## 4. Cutting a schema release (PR #5)

1. **Cut and verify.**

   ```text
   scripts/team.py --env .env build-schema-release --version 0.1.0 --out scratch/r1
   scripts/team.py verify-release scratch/r1/release.tar
   ```

   - Expect the manifest `events` to read: Alice `up`, Bob `up`, Bob `down`,
     with Bob's `.down.sql` files packaged.
   - Expect a `schema/v0.1.0` row in `TEAM_RELEASE`.
2. **Deterministic re-cut.** Cut `0.1.0` again into `scratch/r2`.
   - Expect the same archive digest as `scratch/r1`.
3. **A version means one archive.** Alice applies a third migration, then cuts
   `0.1.0` into `scratch/r3`.
   - Expect a refusal (`already bound`). Cutting `0.2.0` succeeds with
     `history_cut` 4.
4. **Drift gate.** Run `CREATE TABLE LT_DATA.ROGUE (X NUMBER)`, then cut
   `0.3.0`.
   - Expect exit code `3` and a diff naming `ROGUE`. Drop the table and the
     cut succeeds.
5. **Unresolved migration.** Apply a migration whose SQL fails, then cut.
   - Expect the cut refused because the migration mutex is held by the failed
     attempt. Clear it with `recover-migration`.
6. **Apply is not implemented yet.** `plan-release` on `scratch/r1/release.tar`
   (with the test target contract and an exported history) refuses with
   `not implemented yet`.

## 5. Clean up

Drop the `LT_*` users, delete the `lt-*` saved connections, and restore the
snapshot if anything looks wrong.

## What to report

For every step, keep the command's JSON output. For any failure, keep the
`.team-sqlcl-*.log` file it names; secrets are already redacted in it.
