# Lean 6-step release bundle workflow

Schema, stored-code, ORDS, verification, and APEX app releases use focused release folders under `releases/<slug>/`. There is no migration ledger, no attempt-resolution state machine, no capsule archive, and **zero interactive `[y/N]` stdin prompts** in `release` commands — every step is deterministic and agent-runnable, guarded by `--confirm-target <db_unique_name>`, `baseSha256` optimistic locking for patches, target object backups, and before/after invalid-object delta checks.

## Bundle layout

```text
releases/<slug>/
├── release.json
├── 01-ddl/       # Ordered DDL / data SQL scripts (optional)
├── 02-code/      # Packages, views, triggers, procedures, functions, types
├── 03-ords/      # ORDS module scripts (optional)
├── 04-verify/    # Read-only verification / smoke SQL scripts (optional)
└── state/        # Written by `release run`: dev.json, staging.json, prod.json (commit them);
                  # a failed DEV/staging run adds <env>.partial.json for --resume
```

- `slug`: lowercase identifier matching `^[a-z0-9][a-z0-9._-]*$`.
- **`release.json` (`schemaVersion: 1`):**
  - `slug`: bundle folder name.
  - `status`: `"draft"` (mutable), `"staged"` (set by a successful `--env staging` run whose state matches every file and app) or `"deployed"` (locked after a successful `--env prod` run). `release collect` on a staged bundle sets it back to `"draft"`; a hand edit cannot change the JSON, so `list`, `check`, `run` and `status` compute the effective status from fingerprints and show `staged (stale: N changed)`.
  - `stagedAt`, `stagedTarget`, `stagedCommit`: written by a successful staging run.
  - `createdAt`: ISO-8601 UTC timestamp.
  - `deployedAt`, `deployedTarget`, `deployedCommit`: written automatically when `release run --env prod` succeeds; `deployedWithoutStagingProof: true` is added when `--no-staging-proof` was used.
  - `schemas`: optional/inferred ordered list of touched schema names.
  - `ddl`: ordered list of `{ "schema": "<SCHEMA>", "file": "01-ddl/001-add-column.sql", "objects"?: [...] }` entries. Optional `objects` lists `{"objectType": "...", "objectName": "..."}` or `"TYPE:NAME"` strings to back up; when omitted, `release` automatically extracts candidate existing object references (`ALTER TABLE`, `DROP ...`, `CREATE [OR REPLACE] ...`, `GRANT ... ON`) from the SQL file.
  - `code`: ordered list of `{ "schema", "file", "objectType", "objectName", "mode", "baseSha256"? }` entries:
    - `mode: "new"`: object must not already exist on target (unless `--allow-existing` is passed).
    - `mode: "replace"`: full overwrite (creates or replaces on target).
    - `mode: "patch"`: requires `baseSha256` (64-char hex SHA-256 of normalized target `ALL_SOURCE` text). `release check` and `release run` **refuse** if live target source changed since capture and write a unified diff to `scratch/release-diffs/`.
  - `ords`: optional ordered list of `{ "schema", "file", "objects"?: [...] }` entries under `03-ords/`.
  - `verify`: optional ordered list of `{ "schema", "file" }` entries under `04-verify/` (read-only smoke SQL).
  - `apps`: optional list of numeric APEX app IDs deployed in Step 5 via `scripts/deploy.sh` (`--env staging` and `--env prod`; a `--env dev` run applies the SQL and skips the apps, which reach DEV through `scripts/team.sh publish <app_id> --env dev`).
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

Add hand-authored DDL or reference updates under `01-ddl/` and list them under `"ddl"` in `release.json`. The runner commits after each `01-ddl` file finishes, so a later failure does not roll back that file's DML, and a failed run can be resumed (see [Failed runs and `--resume`](#failed-runs-and---resume)). Keep one change per file so a failure leaves whole files done or not done. Prefer idempotent/guarded PL/SQL blocks where appropriate so re-runs or target drift fail cleanly with clear errors:

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
- Validates `release.json` structure, Git tag ref-format (`release/<slug>`), duplicate file references (including case-folded collisions), symlink/reparse-point bans, `forbiddenPatterns`, nested SQLcl script directives (`@`, `@@`, `START <file>` outside comments/strings), foreign explicit `CREATE <OWNER>.<OBJECT>` prefixes, and `PLSQL_CODE_TYPE` bans, and refuses already-`deployed` bundles.
- Refuses unterminated PL/SQL before any database session starts: a `02-code/` package, package body, procedure, function, trigger, type or type body must close with a line containing only `/` (after it, only plain SQL ending in `;`, such as a `GRANT` or the `ALTER TRIGGER ... DISABLE;` that `collect` keeps), and any file whose last statement is a `BEGIN`/`DECLARE`/`CREATE` PL/SQL block ending in `END;` without a following `/` is refused. All steps run in one SQLcl script, so a missing `/` would otherwise absorb the next step and store the object with compilation errors before the step markers are checked. Files written by `release collect` already end with `/`.
- Refuses a `01-ddl/` file whose last SQL statement is not ended by `;` (or by a `/` line). The runner adds `COMMIT;` after every `01-ddl` file, which would otherwise join that statement. Trailing `PROMPT`, `REM` and SQL*Plus `SET` lines are ignored.
- Refuses an app ID that has two `apps/*/<app_id>/` source folders (one left behind after the app moved schema), naming both. Remove the stale folder and rerun; a single folder is the only supported layout.
- Verifies target identity (`DB_UNIQUE_NAME`, `CON_NAME`, `USER`) per schema, honoring optional `.env` `MIGRATION_*` / `STAGING_MIGRATION_*` / `PROD_MIGRATION_*` override triples when set.
- Checks `ALL_OBJECTS` for `mode: "new"` entries (refuses if object already exists unless `--allow-existing`).
- Fetches live `ALL_SOURCE` normalized SHA-256 for `mode: "patch"` entries and refuses on mismatch, saving a unified diff under `scratch/release-diffs/`.
- When `apps` is non-empty, a `--env dev` run skips them (printed and listed in the report as `Apps skipped on DEV`; use `scripts/team.sh publish <app_id> --env dev` for DEV); for `staging` / `prod` it verifies `scripts/deploy.sh`, `scripts/publish_app.sh`, and `apps/*/<app_id>/deployments/<env>.json` exist for `staging` / `prod`, and runs `scripts/publish_app.sh <app_id> --env <env> --describe` offline to validate the strict deployment descriptor and `--for-import` source rules before any SQL runs.
- Reports `ordsBackingObjects` (whether declared PL/SQL/table `objects` exist for backup) separately from `ordsMetadataCoverage` (`"unbacked"` whenever `03-ords/` steps exist, because Step 3 does not export ORDS module/handler metadata), and keeps `ordsBackupCoverage` in sync (`"unbacked"` when `03-ords/` steps exist).
- Prints the ordered execution plan.

### Steps 3–6 — Execute (`release run`)

`release run` executes **with zero interactive prompts** (`--confirm-target <db_unique_name>` is always required and checked case-insensitively against live `DB_UNIQUE_NAME` before any write, under an exclusive per-bundle checkout lock `scratch/.release-<slug>.lock`; for `--env prod`, it also verifies before any backup or SQL runs that `release/<slug>` does not already exist, that every file under `releases/<slug>/` and each referenced `apps/*/<app_id>/` directory is tracked cleanly in Git at `HEAD` with no modified, untracked, or Git-ignored paths, and extracts and executes strictly the bundle SQL and app source trees pinned at that `HEAD` commit via `--app-source-dir`):

```bash
scripts/team.sh release run 2026-11-24-order-fixes \
  --env staging --confirm-target STAGE_DB_U1
scripts/team.sh release run 2026-11-24-order-fixes \
  --env prod --confirm-target PROD_DB_U1
```

Execution sequence:
- **Step 3 — Backup target objects:** unless `--skip-backup` is passed, exports `DBMS_METADATA.GET_DDL` / executable `ALL_SOURCE` DDL (`format`: `"ddl"` or `"source-ddl"`) for existing touched objects (`02-code/`, explicit `objects` on `01-ddl/` and `03-ords/`, and auto-extracted `01-ddl/` references, failing closed if any DDL names an unconfigured schema) into a unique directory `scratch/backups/<slug>-<env>-<YYYYMMDDTHHMMSSZ>-<pid>-<seq>/`, verifying the backup session's live `DB_UNIQUE_NAME`, `CON_NAME`, `USER`, and `CURRENT_SCHEMA` match preflight before writing any backup files, and recording `expectedObjects`, `objects`, `ordsBackingObjects`, `ordsMetadataCoverage`, `ordsBackupCoverage`, and `unbackedOrdsSteps` in `backup-manifest.json`. Fails closed before apply if any existing object returns empty DDL/source, or if any `03-ords/` step exists without `--allow-unbacked-ords` (including when `--skip-backup` is passed).
- **Step 4 — Apply & invalid-object delta:** records `ALL_OBJECTS WHERE status <> 'VALID'` for every touched schema before any DDL runs. Every apply, post-apply, and `04-verify/` session begins with an in-session PL/SQL target guard (`DB_UNIQUE_NAME`, `CON_NAME`, `USER`, `CURRENT_SCHEMA`, raising `-20901` on mismatch), each SQL step keeps `WHENEVER SQLERROR EXIT FAILURE ROLLBACK`, every `mode: "new"` step (unless `--allow-existing`) runs an in-session pre-check raising `-20904` if the object appeared before apply, and every `mode: "patch"` step runs an in-session PL/SQL live-source guard immediately before its SQL executes (raising `-20902` if the target object changed between preflight and apply). Executes **all `01-ddl/` across all schemas before any `02-code/`, and all `02-code/` before any `03-ords/`**. Within each phase (`01-ddl/` -> `02-code/` -> `03-ords/`), steps execute in exact `release.json` order (1 session for single-schema bundles; consecutive same-schema batches for multi-schema) and require explicit `RELEASE_STEP_END` / `RELEASE_BATCH_APPLIED` markers. After all apply phases finish, `ALL_ERRORS` (`ERROR` fails, non-`ERROR` warnings are reported) and `ALL_OBJECTS` (asserting every `02-code/` object exists and is `VALID`, with zero new invalids) are checked per schema; runs `04-verify/` smoke scripts if present.
- **Step 5 — Optional APEX app deploy:** for each numeric app ID in `"apps"` (`staging` and `prod` only), invokes `scripts/deploy.sh <app_id> --env <env> --yes --confirm-target <db_unique_name>` non-interactively (passing `--app-source-dir` to the commit-pinned materialized app tree on `--env prod`).
- **Step 6 — Report & prod lock:** writes a markdown report to `scratch/release-reports/<slug>-<env>-<YYYYMMDDTHHMMSSZ>-<pid>-<seq>.md` (or `--report <path>`), including `ordsBackingObjects`, `ordsMetadataCoverage`, `ordsBackupCoverage`, `unbackedOrdsSteps` (if any), compiler warnings, and failure details when an error occurs after preflight starts (`Outcome: FAILED`, error, backup path, completed phases, and timings). When `--env prod` succeeds, re-verifies that on-disk `release.json` still matches the preflight baseline and that tag `release/<slug>` does not exist, creates Git tag `release/<slug>` at the verified `HEAD` release commit, and updates `release.json` to `"status": "deployed"` with `deployedAt`, `deployedTarget`, and `deployedCommit`. The locked `release.json` is written to disk but not committed, and the tag points at the release commit (which still holds the draft manifest): commit `releases/<slug>/release.json` afterwards and push the tag with your branch (`git push origin release/<slug>`). The run prints this reminder.

## Per-environment state (`state/<env>.json`)

Each successful `release run` writes `releases/<slug>/state/<env>.json` for the environment it ran on (`dev.json`, `staging.json`, `prod.json`). A failed run never changes it; it may write a separate failed-run record (below). The runner never commits; it prints the files to commit, and production refuses uncommitted bundle files, so a staging record cannot be skipped by accident. The repository belongs to one developer, so these files describe that developer's runs. Release state lives in each developer's own Git repository, so a teammate's staging run does not satisfy your production gate: `--env prod` checks the `staging.json` in your repository only.

```json
{ "schemaVersion": 1, "slug": "2026-11-24-order-fixes", "env": "staging",
  "lastRun": { "at": "…", "commit": "a46b8c97…", "mode": "delta", "applied": 1, "skipped": 130, "drifted": [],
               "identities": { "APP_CODE": { "dbUniqueName": "STAGE_DB_U1", "conName": "STAGEPDB", "user": "APP_LOGIN",
                                             "invalidBefore": 30, "invalidAfter": 30, "newInvalid": 0 } },
               "timings": { "preflight": 46.6, "backup": 212.0, "apply": 942.5, "verify": 26.9, "apps": 195.8 },
               "appsDeployed": [130], "report": "scratch/release-reports/….md" },
  "steps": { "02-code/orders_pkg_package_body.sql": { "sha256": "b41d…", "liveSha256": "c903…", "at": "…", "commit": "a46b8c97…" } },
  "apps": { "130": { "sourceSha256": "e01f…", "at": "…" } } }
```

- **Fingerprints:** `sha256` of each bundle file with line endings normalised to LF, keyed by its path (a renamed file is a new step). Apps: sha256 over the sorted paths and bytes of `apps/*/<app_id>/`. `04-verify/` is never recorded and always runs.
- **Delta (DEV, staging):** a step runs only when its fingerprint differs from the one recorded for that environment. `--full` runs every step (use it after restoring staging from production). On staging an app is imported only when its source changed. Production always runs every step and imports every app.
- **Drift:** after each run the live source of every applied code object is recorded as `liveSha256`. When a later run would skip that object but its live source changed (someone edited it on that target), it is applied again and listed under `drifted`. A `patch` step re-applied on staging accepts live source equal to `baseSha256` or to its recorded `liveSha256`; production keeps the strict `baseSha256` guard.
- **Database names:** each record names the `DB_UNIQUE_NAME` it was written on, per schema. A DEV or staging run against a different live database is refused, for example `releases/<slug>/state/dev.json was recorded on database FREE (schema APP_CODE); this target is FREEX.` Rerun with `--full` (it replaces the record) or move the state file aside. `--full` bypasses this check and production never applies it. A record written without a name keeps working. `release status` shows the database of each environment's record.
- **Production gate:** `release run --env prod` refuses unless the bundle is `staged` and every file and app fingerprint equals `state/staging.json`, naming what differs. `--no-staging-proof` overrides this (for projects without staging); the report and `release.json` record it.
- **Failed runs and `--resume`:** see the next section.
- **`release status <bundle>`:** offline table, one row per step and app, columns DEV / STAGING / PROD (`ok` current version applied, `old` an older version, `-` never). `--live --env <env>` adds a read-only drift check; `--format json` for agents.

## Adopting a bundle that ran before release state

`release adopt <bundle> --env dev|staging` gives a bundle that ran before `state/<env>.json` existed a record, so the next run applies only what is really missing. It reads the database once per schema (read-only, same identity guard as `check`) and never applies anything. `release.json` is not changed; the next normal run sets `staged`.

- **Code steps** (packages, views, triggers) are recorded when the live source equals the bundle file after both are reduced to the text Oracle keeps (no `ALTER SESSION` header, `CREATE OR REPLACE` prefix or terminating `/`; no GRANT lines).
- **DDL and ORDS steps** need `--report <path>` to a successful run report for this bundle and environment. A step is recorded only when the report's identity matches the live schema, the file at the report's commit (`--commit`, default the report's `Commit`) equals today's file, and every object the step names exists. A name parsed from DDL counts as present when it exists under any type that statement could mean (a `GRANT` names several). A name the step drops, also inside `EXECUTE IMMEDIATE`, must be absent live unless the step creates it again. Objects listed explicitly in `release.json` must exist with their own type. A step that names no object (seed `INSERT`/`MERGE`) is recorded on the report alone (`objectCheck: none`).
- **Apps** (staging only) need the report too, and the app folder must equal its state at that commit. Git-ignored local files in the folder (exports, local deployment files) do not count; untracked files do.
- Anything else is printed as `not proven: <file> (<reason>)` and applied by the next run.

```bash
scripts/team.sh release adopt prod-v1 --env staging --report scratch/release-reports/prod-v1-staging-….md --commit <sha> --dry-run
scripts/team.sh release adopt prod-v1 --env staging --report scratch/release-reports/prod-v1-staging-….md --commit <sha>
scripts/team.sh release run prod-v1 --env staging --confirm-target <db_unique_name>   # applies only the not-proven steps
```

Refused: `--env prod`, an existing `state/<env>.json` (unless `--replace`), a failed or other-bundle or other-environment report. `--dry-run` writes nothing. Commit `state/<env>.json` after a real adopt.

## Failed runs and `--resume`

A run can fail halfway: `01-ddl/001-create.sql` creates a table, `01-ddl/002-add-note.sql` fails. Running the bundle again would hit `ORA-00955` on file 1, because the table already exists.

- **What a failed DEV or staging run records.** The runner prints `RELEASE_STEP_END:<file>` after each file and commits after each `01-ddl` file, so it knows which `01-ddl` files finished and are saved. It writes them to `releases/<slug>/state/<env>.partial.json`: each file's fingerprint, the time, the commit and the Oracle error lines (no local paths). `state/<env>.json` is not touched, so it still describes successful runs only.
- **Resume restarts the failed file.** The file the run stopped in runs again from its first statement, so a statement that already ran in it runs again (a `CREATE` before the failing `ALTER` hits `ORA-00955`). Keep one change per `01-ddl` file, or make that file rerunnable. `check` and `run` name the first unfinished `01-ddl` file in the plan, with a note that it runs again in full.
- **Resume.** Fix the failing file, then `release run <slug> --env <env> --confirm-target <db_unique_name> --resume`. It skips the `01-ddl` files that finished, as long as their text is unchanged; an edited file runs again. `02-code` and `03-ords` files always run again (they replace objects, and a finished code file may still be invalid). `release check --resume` shows the same plan first.
- **Without `--resume`** every step runs again, as before; `check` and `run` print a `NOTE` naming the partial record.
- **After a successful run** the partial record is removed and its steps are written to `state/<env>.json`. Commit the removal if the partial record was committed (commit it when a teammate may finish the run).
- **Old records.** The plan shows the age of the partial record. A record older than 7 days gets a `WARNING`: confirm the target still holds the finished files before using `--resume`.
- **Refused:** `--resume` with no partial record, `--resume --full`, `--resume --env prod`, and `--resume` when the partial record names another database (see Per-environment state). Production always runs the whole bundle; a failed production run prints which files finished in its output and report.

### Run output

`release run` narrates every phase so anyone reading the console can follow it: the target identity, the plan (`apply` / `skip` / `resume` / `drift`), then for each SQLcl session a `start` line per file before it runs and a `done`, `FAILED` or `not run` line per file when the session ends (SQLcl returns its output at the end of a session, so the result lines follow the session). `04-verify` scripts and app deploys print the same before and after lines.

```text
[Step 4/6] Apply: 01-ddl, then 02-code, then 03-ords (each 01-ddl file is committed when it finishes)
    SQLcl session as TPLSTG: 3 file(s)
      [1/3] start    01-ddl/001-create.sql  [TPLSTG, DDL]
      [2/3] start    01-ddl/002-add-note.sql  [TPLSTG, DDL]
      [3/3] start    02-code/resume_live_p.sql  [TPLSTG, new PROCEDURE RESUME_LIVE_P]
      [1/3] done     01-ddl/001-create.sql
      [2/3] FAILED   01-ddl/002-add-note.sql
      [3/3] not run  02-code/resume_live_p.sql
```

## Upgrading existing downstream projects

Existing 26.2 downstream projects created from pre-Lean-Release template locks (with retired migration/rollout paths in `.template-lock.json`) must upgrade in **two steps** using the published bridge commit `70dd338` (`70dd338c46c24b6265e6feca5f2cd65a5bdac423`, PR #142):

1. First upgrade to bridge commit `70dd338` so your local `scripts/upgrade_template.py` validates retired lock paths against your local manifest:

   ```bash
   scripts/team.sh upgrade-template --ref 70dd338c46c24b6265e6feca5f2cd65a5bdac423 --apex-release 26.2 --dry-run
   scripts/team.sh upgrade-template --ref 70dd338c46c24b6265e6feca5f2cd65a5bdac423 --apex-release 26.2
   ```

   Review and commit the bridge upgrade (resolving any `.template-new` files).
2. Once the local upgrader is updated, upgrade to `main` for the Lean 6-step release bundle release:

   ```bash
   scripts/team.sh upgrade-template --ref main --apex-release 26.2 --dry-run
   scripts/team.sh upgrade-template --ref main --apex-release 26.2
   ```
