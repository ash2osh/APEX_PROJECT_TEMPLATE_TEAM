# Streamlined APEX Team Template Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform `APEX_PROJECT_TEMPLATE_TEAM` on `main` from the overengineered v1 metadata-schema experiment into a clean, lightweight, production-ready team template based on `APEX_PROJECT_TEMPLATE` and explicit workspace deployment descriptors.

**Architecture:** Single database connection profile in `.env.example`, zero custom metadata tables, native Git-based per-developer migration files with conflict detection, declarative deployment JSONs (`deployments/dev.json`, `prod.json`, `staging.json`) per application, pre-publish Builder drift detection, and interactive confirmation for staging/prod deployment.

**Tech Stack:** Bash, Python 3, SQLcl 26.2, Oracle AI Database 26ai Free, APEX 26.1+ / APEXlang.

**Spec:** `docs/superpowers/specs/2026-09-26-streamlined-team-template-design.md`

## Global Constraints

- Zero auxiliary metadata schemas or custom control tables (`TEAM_APP_MUTEX`, etc.).
- Work with numeric Application IDs (`100`, `200`) across all CLI commands and deployment files.
- `.env.example` must follow the clean structure of `APEX_PROJECT_TEMPLATE/.env.example` with optional staging/production connection variables.
- All `.apx` files must enforce Unix line endings (`LF`).
- Pre-publish guard must halt if the live APEX application was modified in Builder after the local export.
- Staging and production deployments must require interactive human confirmation (`[y/N]`) unless `--manual` is passed to output a DBA runbook.

## Review Focus

1. **Clean Removal:** All traces of `teamlib`, 14 metadata tables, UUID rosters, and `TEAM_MIGRATION_MEMBER` CLOB logic must be removed.
2. **Backward Compatibility with SQLcl:** Deployment JSONs (`dev.json`, `prod.json`, `staging.json`) must strictly match SQLcl deployment specification format (`workspace.name`, `app.id`, `app.databaseSession.parsingSchema`).
3. **Drift Guard Failure Behavior:** If `apex_applications.last_updated_on` is newer than the local export timestamp, `publish` must refuse to overwrite and instruct the developer to export.
4. **Migration Conflict Detection:** `check_conflicts.py` must detect duplicate table names, duplicate column additions, or duplicate sequences across developer folders (`migrations/<dev1>/` vs `migrations/<dev2>/`).
5. **No Broken Links:** `AGENTS.md` and `README.md` must not reference any deleted v1 metadata docs.

---

### Task 1: Archive Verification & Clean Purge of Legacy Bloat

**Files:**
- Delete: `scripts/teamlib/` (entire directory)
- Delete: `scripts/team.py`
- Delete: `scripts/tests/` (entire directory)
- Delete: `ci/` (entire directory)
- Delete: `targets/` (entire directory)
- Delete: `operations/` (entire directory)
- Delete legacy docs in `docs/` referencing v1 metadata: `app-recovery.md`, `ci.md`, `conflict-resolution.md`, `import-pause.md`, `live-test-plan.md`, `local-three-developer-e2e.md`, `metadata-backup-restore.md`, `migrations.md`, `pending-work.md`, `promotion.md`, `schema-coverage.md`, `toolchain.md`, `working-on-apex-together.html`.

- [ ] **Step 1: Verify archive tag exists**
  - Verify that `git rev-parse archive/v1-metadata-experiment` returns commit `5ba7dc4`.

- [ ] **Step 2: Remove legacy Python code, tests, CI, and targets**
  - Run:
    ```bash
    rm -rf scripts/teamlib scripts/team.py scripts/tests ci targets operations
    git rm -r --cached scripts/teamlib scripts/team.py scripts/tests ci targets operations 2>/dev/null || true
    ```

- [ ] **Step 3: Remove obsolete v1 documentation**
  - Delete legacy doc files from `docs/` while preserving `docs/superpowers/`.
  - Commit purge:
    ```bash
    git add -A
    git commit -m "chore: purge legacy v1 metadata schema and distributed consensus bloat"
    ```

---

### Task 2: Import Core Architecture & Scripts from `APEX_PROJECT_TEMPLATE`

**Files:**
- Create: `.env.example`
- Create: `ai_generate/.gitkeep`
- Create: `database/.gitkeep`
- Create: `scripts/load_env.sh`
- Create: `scripts/load_env.ps1`
- Create: `scripts/check_db_target.sh`
- Create: `scripts/check_db_target.ps1`
- Create: `scripts/export_apps.sh`
- Create: `scripts/export_apps.ps1`
- Create: `scripts/export_apps.sql`
- Create: `scripts/backup_db.sh`
- Create: `scripts/backup_db.ps1`
- Create: `scripts/backup_db.sql`
- Create: `scripts/replace_mirror.sh`
- Create: `scripts/replace_mirror.ps1`
- Create: `scripts/normalize_apx.sh`
- Create: `scripts/normalize_apx.ps1`
- Create: `scripts/verify_db_access.sql`

- [ ] **Step 1: Copy core proven scripts from `/home/ash/projects/APEX_PROJECT_TEMPLATE/scripts/`**
  - Copy all core `.sh`, `.ps1`, and `.sql` utility files into `scripts/`.
  - Ensure execution permissions: `chmod +x scripts/*.sh`.

- [ ] **Step 2: Create `ai_generate/` and `database/` folders**
  - Create directories with `.gitkeep` placeholders.

- [ ] **Step 3: Create `.env.example` with optional staging/production configs**
  - Write `.env.example` following the spec format.
  - Commit core import:
    ```bash
    git add -A
    git commit -m "feat: import clean core scripts and layout from APEX_PROJECT_TEMPLATE"
    ```

---

### Task 3: Declarative Deployment Descriptors & App Structure

**Files:**
- Create: `apps/templates/deployments/dev.json`
- Create: `apps/templates/deployments/prod.json`
- Create: `apps/templates/deployments/staging.json`
- Create: `scripts/publish_app.sh`
- Create: `scripts/publish_app.ps1`

- [ ] **Step 1: Create template deployment descriptors**
  - Add template JSON descriptors in `apps/templates/deployments/`:
    - `dev.json`: `workspace.name`, `app.id`, `app.databaseSession.parsingSchema`.
    - `prod.json`: `workspace.name`, `app.id`, `app.databaseSession.parsingSchema`.
    - `staging.json`: `workspace.name`, `app.id`, `app.databaseSession.parsingSchema`.

- [ ] **Step 2: Implement `scripts/publish_app.sh`**
  - Accepts `<app_id>` and optional `--env <dev|staging|prod>` (default `dev`).
  - Resolves application directory: looks in `apps/*/<app_id>` or `apps/<app_id>`.
  - Verifies existence of `deployments/<env>.json`.
  - Runs pre-publish Builder drift check (Task 4).
  - Executes SQLcl import pointing to the deployment descriptor.
  - Commit:
    ```bash
    git add apps/ scripts/publish_app.*
    git commit -m "feat: add declarative deployment descriptors and publish_app script"
    ```

---

### Task 4: Pre-Publish Builder Drift Guard

**Files:**
- Create: `scripts/check_builder_drift.py`

- [ ] **Step 1: Implement drift detection logic in `scripts/check_builder_drift.py`**
  - Input: `<app_id>`, connection name, local app directory.
  - Reads `application.apx` or `.apex/apexlang.json` to find last export timestamp.
  - Executes a fast, read-only SQLcl / Oracle query against `apex_applications`:
    `SELECT to_char(last_updated_on, 'YYYY-MM-DD"T"HH24:MI:SS') FROM apex_applications WHERE application_id = :1;`
  - Compares live `last_updated_on` with the local export timestamp.
  - If live application is newer than local export:
    - Exits with status 1 and prints:
      `[DRIFT DETECTED] Live APEX App <id> was modified in Builder on <timestamp>.`
      `To prevent accidental overwrites, run: scripts/team.sh export <id> to review and merge changes.`
  - If live application is older or equal:
    - Exits with status 0 (`[DRIFT OK] No uncaptured Builder edits detected.`).

- [ ] **Step 2: Wire drift guard into `scripts/publish_app.sh`**
  - Call `python3 scripts/check_builder_drift.py` before executing the import.
  - Add flag `--force` to `publish_app.sh` to allow overriding if explicitly desired.
  - Commit:
    ```bash
    git add scripts/check_builder_drift.py scripts/publish_app.sh
    git commit -m "feat: add pre-publish Builder drift detection guard"
    ```

---

### Task 5: Per-Developer Migrations & Conflict Detector

**Files:**
- Create: `migrations/.gitkeep`
- Create: `scripts/check_conflicts.py`
- Create: `scripts/migrate.sh`

- [ ] **Step 1: Create `migrations/` layout**
  - Establish `migrations/<developer>/` pattern with a clear README or `.gitkeep`.

- [ ] **Step 2: Implement `scripts/check_conflicts.py`**
  - Scans all `.sql` files across `migrations/*/*.sql`.
  - Parses AST or regex for DDL statements:
    - `CREATE TABLE [schema.]<name>`
    - `CREATE OR REPLACE VIEW [schema.]<name>`
    - `CREATE SEQUENCE [schema.]<name>`
    - `ALTER TABLE [schema.]<name> ADD <column>`
  - Groups detected objects by canonical uppercase name.
  - If two different developers declare the same table, view, or sequence:
    - Flags collision and exits with status 1.
  - If no collisions detected:
    - Prints summary of objects and exits with status 0.

- [ ] **Step 3: Implement `scripts/migrate.sh`**
  - Runs `python3 scripts/check_conflicts.py`. If conflicts exist, halts immediately.
  - Executes reviewed migration file(s) via SQLcl against the target database.
  - Commit:
    ```bash
    git add migrations/ scripts/check_conflicts.py scripts/migrate.sh
    git commit -m "feat: add per-developer migration structure and conflict checker"
    ```

---

### Task 6: Unified CLI (`scripts/team.sh`) & Staging/Prod Deployment

**Files:**
- Create: `scripts/team.sh`
- Create: `scripts/team.ps1`
- Create: `scripts/deploy.sh`

- [ ] **Step 1: Implement `scripts/deploy.sh`**
  - Accepts `<app_id>` and `--env <staging|prod>`.
  - Reads target connection from `.env` (`STAGING_SQLCL_CONNECTION` or `PROD_SQLCL_CONNECTION`).
  - Checks if `--manual` flag is passed:
    - If `--manual`, prints step-by-step SQLcl commands for DBA execution without connecting.
    - If direct execution:
      - Prints target summary: Application `<app_id>`, Workspace `<name>`, Target Schema `<schema>`.
      - Prompts: `Deploying to <ENV>. Proceed? [y/N]: `
      - Executes deployment via SQLcl on confirmation.

- [ ] **Step 2: Implement unified `scripts/team.sh` CLI**
  - Commands exposed:
    - `scripts/team.sh doctor`: validates `.env` and tests SQLcl connection.
    - `scripts/team.sh export <app-id>`: wraps `export_apps.sh`.
    - `scripts/team.sh publish <app-id> [--env dev] [--force]`: wraps `publish_app.sh`.
    - `scripts/team.sh check-conflicts`: wraps `check_conflicts.py`.
    - `scripts/team.sh migrate <file>`: wraps `migrate.sh`.
    - `scripts/team.sh backup-db`: wraps `backup_db.sh`.
    - `scripts/team.sh deploy <app-id> --env <staging|prod> [--manual]`: wraps `deploy.sh`.
  - Commit:
    ```bash
    git add scripts/team.sh scripts/team.ps1 scripts/deploy.sh
    git commit -m "feat: add unified team.sh CLI wrapper and safe deploy runner"
    ```

---

### Task 7: Update Documentation (`AGENTS.md` and `README.md`)

**Files:**
- Modify: `AGENTS.md`
- Modify: `README.md`

- [ ] **Step 1: Rewrite `AGENTS.md`**
  - Document team conventions, single connection profile, declarative deployment descriptors, human team communication, per-developer migrations, and drift safety.
  - Detail rules for AI coding agents modifying APEXlang and SQL scripts.

- [ ] **Step 2: Rewrite `README.md`**
  - Quickstart guide: `cp .env.example .env`, `scripts/team.sh doctor`.
  - Workflow guide: Builder-first, File-first, Migrations, Deployments.
  - Commit:
    ```bash
    git add AGENTS.md README.md
    git commit -m "docs: rewrite AGENTS.md and README.md for streamlined team template"
    ```

---

### Task 8: End-to-End Verification Against Local Oracle 26ai

- [ ] **Step 1: Configure local `.env`**
  - Configure `.env` pointing to verified `docker-demo` / user `DEMO` with `APEX_APP_ID=100`.

- [ ] **Step 2: Test `scripts/team.sh doctor`**
  - Run `scripts/team.sh doctor` and verify exit code 0.

- [ ] **Step 3: Test `scripts/team.sh export 100`**
  - Export app 100 and verify clean creation of APEXlang files and `deployments/dev.json`.

- [ ] **Step 4: Test `scripts/team.sh check-conflicts`**
  - Test conflict checker with sample non-conflicting and conflicting migration files.

- [ ] **Step 5: Test pre-publish drift guard and publish**
  - Verify drift guard detects unchanged state, test dry-run publish.

- [ ] **Step 6: Verify clean Git status**
  - Ensure template repository has only tracked, clean, version-controlled files.
