# Lean 6-step release bundle workflow

Schema, stored-code, ORDS, verification, and APEX app releases use focused release folders under `releases/<slug>/`. There is no migration ledger, no attempt-resolution state machine, no capsule archive, and **zero interactive `[y/N]` stdin prompts** in `release` commands — every step is deterministic and agent-runnable, guarded by `--confirm-target <db_unique_name>`, `baseSha256` optimistic locking for patches, target object backups, and before/after invalid-object delta checks.

## Bundle layout

```text
releases/<slug>/
├── release.json
├── 01-ddl/       # Ordered DDL / data SQL scripts (optional)
├── 02-code/      # Packages, views, triggers, procedures, functions, types
├── 03-ords/      # ORDS module scripts (optional)
└── 04-verify/    # Read-only verification / smoke SQL scripts (optional)
```

- `slug`: lowercase identifier matching `^[a-z0-9][a-z0-9._-]*$`.
- **`release.json` (`schemaVersion: 1`):**
  - `slug`: bundle folder name.
  - `status`: `"draft"` (mutable) or `"deployed"` (locked after a successful `--env prod` run).
  - `createdAt`: ISO-8601 UTC timestamp.
  - `deployedAt`, `deployedTarget`, `deployedCommit`: written automatically when `release run --env prod` succeeds.
  - `schemas`: optional/inferred ordered list of touched schema names.
  - `ddl`: ordered list of `{ "schema": "<SCHEMA>", "file": "01-ddl/001-add-column.sql" }` entries.
  - `code`: ordered list of `{ "schema", "file", "objectType", "objectName", "mode", "baseSha256"? }` entries:
    - `mode: "new"`: object must not already exist on target (unless `--allow-existing` is passed).
    - `mode: "replace"`: full overwrite (creates or replaces on target).
    - `mode: "patch"`: requires `baseSha256` (64-char hex SHA-256 of normalized target `ALL_SOURCE` text). `release check` and `release run` **refuse** if live target source changed since capture and write a unified diff to `scratch/release-diffs/`.
  - `ords`: optional ordered list of `{ "schema", "file" }` entries under `03-ords/`.
  - `verify`: optional ordered list of `{ "schema", "file" }` entries under `04-verify/` (read-only smoke SQL).
  - `apps`: optional list of numeric APEX app IDs deployed in Step 5 via `scripts/deploy.sh`.
  - `forbiddenPatterns`: optional list of uppercase glob/prefix patterns (such as `"HR_WF_*"`) refused in bundle filenames or SQL.

## The 6 steps

### Step 1 — Create or select a release folder

List open `draft` and locked `deployed` bundles offline (no `.env` or SQLcl needed):

```bash
scripts/team.sh release list
scripts/team.sh release new 2026-11-24-order-fixes
```

### Step 2 — Collect code and author guarded DDL

Export live stored code (or views/triggers via `DBMS_METADATA.GET_DDL`) from DEV into `02-code/` and register it in `release.json` in 1 SQLcl session:

```bash
# New object
scripts/team.sh release collect 2026-11-24-order-fixes \
  --from dev --schema APP_CODE --object "PACKAGE:ORDER_UTIL_PKG" --mode new

# Patch against staging/prod (queries target read-only to capture baseSha256)
scripts/team.sh release collect 2026-11-24-order-fixes \
  --from dev --target-env prod --schema APP_CODE \
  --object "PACKAGE BODY:ORDER_UTIL_PKG" --mode patch
```

`release collect` automatically strips `ALTER SESSION SET PLSQL_CODE_TYPE = INTERPRETED;` lines so Oracle Autonomous Database (ADB) non-ADMIN logins never fail with `ORA-01031`. Both `release check` and `release run` refuse any SQL file that still contains that statement.

Add hand-authored DDL or reference updates under `01-ddl/` and list them under `"ddl"` in `release.json`. Prefer idempotent/guarded PL/SQL blocks where appropriate so re-runs or target drift fail cleanly with clear errors:

```sql
DECLARE
  v_count NUMBER;
BEGIN
  SELECT COUNT(*) INTO v_count
  FROM user_tab_columns
  WHERE table_name = 'ORDERS' AND column_name = 'NOTES';
  IF v_count = 0 THEN
    EXECUTE IMMEDIATE 'ALTER TABLE orders ADD (notes VARCHAR2(200))';
  END IF;
END;
/
```

Add optional ORDS scripts to `03-ords/`, read-only smoke queries to `04-verify/`, and numeric APEX app IDs to `"apps"` in `release.json`.

### Preflight (`release check`)

Run a read-only preflight before any database write (1 SQLcl session per schema):

```bash
scripts/team.sh release check 2026-11-24-order-fixes --env dev
scripts/team.sh release check 2026-11-24-order-fixes --env prod --format json
```

`release check`:
- Validates `release.json` structure, path safety, `forbiddenPatterns`, and `PLSQL_CODE_TYPE` bans, and refuses already-`deployed` bundles.
- Verifies target identity (`DB_UNIQUE_NAME`, `CON_NAME`, `USER`) per schema, honoring optional `.env` `MIGRATION_*` / `STAGING_MIGRATION_*` / `PROD_MIGRATION_*` override triples when set.
- Checks `ALL_OBJECTS` for `mode: "new"` entries (refuses if object already exists unless `--allow-existing`).
- Fetches live `ALL_SOURCE` normalized SHA-256 for `mode: "patch"` entries and refuses on mismatch, saving a unified diff under `scratch/release-diffs/`.
- Prints the ordered execution plan.

### Steps 3–6 — Execute (`release run`)

`release run` executes **with zero interactive prompts** (`--confirm-target <db_unique_name>` is always required and checked case-insensitively against live `DB_UNIQUE_NAME` before any write):

```bash
scripts/team.sh release run 2026-11-24-order-fixes \
  --env staging --confirm-target STAGE_DB_U1
scripts/team.sh release run 2026-11-24-order-fixes \
  --env prod --confirm-target PROD_DB_U1
```

Execution sequence:
- **Step 3 — Backup target objects:** unless `--skip-backup` is passed, uses 1 SQLcl session per schema to export `DBMS_METADATA.GET_DDL` / `ALL_SOURCE` for existing touched objects into `scratch/backups/<slug>-<env>-<YYYYMMDDTHHMMSSZ>/`.
- **Step 4 — Apply & invalid-object delta:** records `ALL_OBJECTS WHERE status <> 'VALID'` for every touched schema before any DDL runs, then executes **all `01-ddl/` across all schemas before any `02-code/`, and all `02-code/` before any `03-ords/`**. Within each phase (`01-ddl/` -> `02-code/` -> `03-ords/`), steps execute in exact `release.json` order and consecutive same-schema steps share 1 SQLcl session (`ALTER SESSION SET CURRENT_SCHEMA = <SCHEMA>` when login differs from schema). After all apply phases finish, `ALL_ERRORS` and post-apply invalid objects are checked per schema (so cross-schema invalidations resolved by a later `02-code/` compile do not false-fail, while genuine new invalids still fail); runs `04-verify/` smoke scripts if present.
- **Step 5 — Optional APEX app deploy:** for each numeric app ID in `"apps"`, invokes `scripts/deploy.sh <app_id> --env <env> --yes --confirm-target <db_unique_name>` non-interactively.
- **Step 6 — Report & prod lock:** writes a markdown report to `scratch/release-reports/<slug>-<env>-<YYYYMMDDTHHMMSSZ>.md` (or `--report <path>`). When `--env prod` succeeds, updates `release.json` to `"status": "deployed"` with `deployedAt`, `deployedTarget`, and `deployedCommit`, and creates the local Git tag `release/<slug>` (if in a Git repo and the tag does not already exist).
