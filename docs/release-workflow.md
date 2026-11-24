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
  - `ddl`: ordered list of `{ "schema": "<SCHEMA>", "file": "01-ddl/001-add-column.sql", "objects"?: [...] }` entries. Optional `objects` lists `{"objectType": "...", "objectName": "..."}` or `"TYPE:NAME"` strings to back up; when omitted, `release` automatically extracts candidate existing object references (`ALTER TABLE`, `DROP ...`, `CREATE [OR REPLACE] ...`, `GRANT ... ON`) from the SQL file.
  - `code`: ordered list of `{ "schema", "file", "objectType", "objectName", "mode", "baseSha256"? }` entries:
    - `mode: "new"`: object must not already exist on target (unless `--allow-existing` is passed).
    - `mode: "replace"`: full overwrite (creates or replaces on target).
    - `mode: "patch"`: requires `baseSha256` (64-char hex SHA-256 of normalized target `ALL_SOURCE` text). `release check` and `release run` **refuse** if live target source changed since capture and write a unified diff to `scratch/release-diffs/`.
  - `ords`: optional ordered list of `{ "schema", "file", "objects"?: [...] }` entries under `03-ords/`.
  - `verify`: optional ordered list of `{ "schema", "file" }` entries under `04-verify/` (read-only smoke SQL).
  - `apps`: optional list of numeric APEX app IDs deployed in Step 5 via `scripts/deploy.sh` (`--env staging` and `--env prod` only).
  - `forbiddenPatterns`: optional list of uppercase glob/prefix patterns (such as `"HR_WF_*"`) refused in bundle filenames or SQL.
- Duplicate `file` references across `ddl`, `code`, `ords`, and `verify` are refused.

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

`release collect` writes schema-qualified default filenames (`02-code/<schema>_<object_name>_<type>.sql`), refuses overwriting a file already used by a different `(schema, objectType, objectName)` entry in `release.json`, and automatically strips `ALTER SESSION SET PLSQL_CODE_TYPE = INTERPRETED;` lines so Oracle Autonomous Database (ADB) non-ADMIN logins never fail with `ORA-01031`. Both `release check` and `release run` refuse any SQL file that still contains that statement.

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
- Validates `release.json` structure, duplicate file references, path safety, `forbiddenPatterns`, and `PLSQL_CODE_TYPE` bans, and refuses already-`deployed` bundles.
- Verifies target identity (`DB_UNIQUE_NAME`, `CON_NAME`, `USER`) per schema, honoring optional `.env` `MIGRATION_*` / `STAGING_MIGRATION_*` / `PROD_MIGRATION_*` override triples when set.
- Checks `ALL_OBJECTS` for `mode: "new"` entries (refuses if object already exists unless `--allow-existing`).
- Fetches live `ALL_SOURCE` normalized SHA-256 for `mode: "patch"` entries and refuses on mismatch, saving a unified diff under `scratch/release-diffs/`.
- When `apps` is non-empty, refuses `--env dev` (use `scripts/team.sh publish <app_id> --env dev` for DEV) and verifies `scripts/deploy.sh` and `apps/*/<app_id>/deployments/<env>.json` exist for `staging` / `prod`.
- Prints the ordered execution plan.

### Steps 3–6 — Execute (`release run`)

`release run` executes **with zero interactive prompts** (`--confirm-target <db_unique_name>` is always required and checked case-insensitively against live `DB_UNIQUE_NAME` before any write; for `--env prod`, it also verifies before any backup or SQL runs that `releases/<slug>/release.json` is tracked in Git and `releases/<slug>/` has a clean working tree with a 40-character `HEAD` SHA):

```bash
scripts/team.sh release run 2026-11-24-order-fixes \
  --env staging --confirm-target STAGE_DB_U1
scripts/team.sh release run 2026-11-24-order-fixes \
  --env prod --confirm-target PROD_DB_U1
```

Execution sequence:
- **Step 3 — Backup target objects:** unless `--skip-backup` is passed, uses 1 SQLcl session per schema to export `DBMS_METADATA.GET_DDL` / `ALL_SOURCE` for existing touched objects (`02-code/`, explicit `objects` on `01-ddl/` and `03-ords/`, and auto-extracted `01-ddl/` references) into `scratch/backups/<slug>-<env>-<YYYYMMDDTHHMMSSZ>/`, recording both `expectedObjects` and backed-up `objects` in `backup-manifest.json`. Fails closed before apply if any existing object returns empty DDL/source.
- **Step 4 — Apply & invalid-object delta:** records `ALL_OBJECTS WHERE status <> 'VALID'` for every touched schema before any DDL runs. Every apply session begins with an in-session PL/SQL target guard (`DB_UNIQUE_NAME`, `CON_NAME`, `USER`, `CURRENT_SCHEMA`, raising `-20901` on mismatch), and every `mode: "patch"` step runs an in-session PL/SQL live-source guard immediately before its SQL executes (raising `-20902` if the target object changed between preflight and apply). Executes **all `01-ddl/` across all schemas before any `02-code/`, and all `02-code/` before any `03-ords/`**. Within each phase (`01-ddl/` -> `02-code/` -> `03-ords/`), steps execute in exact `release.json` order and consecutive same-schema steps share 1 SQLcl session (`ALTER SESSION SET CURRENT_SCHEMA = <SCHEMA>` when login differs from schema). After all apply phases finish, `ALL_ERRORS` and post-apply invalid objects are checked per schema (so cross-schema invalidations resolved by a later `02-code/` compile do not false-fail, while genuine new invalids still fail); runs `04-verify/` smoke scripts if present.
- **Step 5 — Optional APEX app deploy:** for each numeric app ID in `"apps"` (`staging` and `prod` only), invokes `scripts/deploy.sh <app_id> --env <env> --yes --confirm-target <db_unique_name>` non-interactively.
- **Step 6 — Report & prod lock:** writes a markdown report to `scratch/release-reports/<slug>-<env>-<YYYYMMDDTHHMMSSZ>.md` (or `--report <path>`), including on failures after preflight starts (`Outcome: FAILED`, error, backup path, completed phases, and timings). When `--env prod` succeeds, creates Git tag `release/<slug>` at the verified `HEAD` release commit before updating `release.json` to `"status": "deployed"` with `deployedAt`, `deployedTarget`, and `deployedCommit`.

## Upgrading existing downstream projects

Existing downstream projects created from pre-Lean-Release template locks (with retired migration/rollout paths in `.template-lock.json`) must upgrade in **two steps**:

1. First upgrade to the template commit/tag that includes the retired-lock-path upgrader compatibility fix (`scripts/upgrade_template.py`).
2. Once the local upgrader is updated, run `scripts/team.sh upgrade-template` again to upgrade to the Lean 6-step release bundle release.
