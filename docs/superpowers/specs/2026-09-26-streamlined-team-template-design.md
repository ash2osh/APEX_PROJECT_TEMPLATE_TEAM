# Streamlined APEX Team Template — Architectural Design

**Date:** 2026-09-26  
**Status:** Approved by Owner  
**Supersedes:** `2026-09-24-dev-database-release-source-design.md` and the v1 `METADATA_SCHEMA` distributed consensus architecture.  
**Baseline Reference:** `/home/ash/projects/APEX_PROJECT_TEMPLATE/` and [The Missing Apartment Number: Why Your APEX Deployments Need an Explicit Workspace](https://ash2osh.hashnode.dev/the-missing-apartment-number-why-your-apex-deployments-need-an-explicit-workspace).

---

## 1. Rationale & Core Invariants

The previous template version introduced an auxiliary `METADATA_SCHEMA` with 14 custom control tables, database-level UUID acknowledgement voting, and SQL migration storage inside Oracle CLOBs. This added severe accidental complexity and prevented teams from using a single database connection.

This design resets the team template to a clean, robust, and proven foundation:

1. **Zero Metadata Schema:** The shared Oracle development database and APEX workspace are the sole runtime source of truth. No custom control tables, mutex rows, or auxiliary administrative schemas exist.
2. **Single Database Connection & Clean Profile:** `.env` requires only one standard SQLcl saved connection for the developer's target database, matching the simplicity of `APEX_PROJECT_TEMPLATE`.
3. **Explicit Workspace Deployments:** APEX workspace names, target application IDs, and parsing schemas are defined declaratively in version-controlled JSON descriptors (`deployments/dev.json`, `staging.json`, `prod.json`) per application, eliminating runtime workspace ambiguity.
4. **Developer-Isolated Migrations in Git:** Migrations are tracked as native `.sql` files in Git, organized per developer (`migrations/<developer>/`). Pre-deployment conflict checking verifies that separate developers haven't created conflicting objects before applying to the shared database.
5. **Human-Driven Team Communication:** Team publishing coordination relies on standard team chat notices ("Publishing App 100 to DEV") rather than rigid database-level lock polling and voting tables.
6. **Builder Drift Protection:** When publishing local `.apx` changes, the tool verifies that the live APEX application has not been edited in Builder since the last export, preventing accidental overwrites.
7. **Safe Staging/Production Promotions:** Releases are cut from Git commits/tags. Staging and production deployments are supported with explicit interactive human confirmation (`[y/N]`) or an agent-generated manual DBA runbook.
8. **Structured AI Script Staging:** Includes `ai_generate/YYYY-MM-DD/` for deployable AI-generated SQL and PL/SQL scripts.

---

## 2. Target Directory Layout

```plaintext
APEX_PROJECT_TEMPLATE_TEAM/
├── .env.example                     # Streamlined environment configuration
├── .gitignore                       # Git ignore rules (.env, scratch/, etc.)
├── .gitattributes                   # LF normalization (*.apx text eol=lf)
├── AGENTS.md                        # Team conventions & agent guidelines
├── README.md                        # Team quickstart & developer workflow
├── ai_generate/
│   └── YYYY-MM-DD/                  # Deployable AI-generated SQL & PL/SQL scripts
├── apps/
│   └── <app-id>/                    # Split APEX source (pages/, app_definition.apx, etc.)
│       └── deployments/
│           ├── dev.json             # Workspace name, app id, parsing schema for DEV
│           ├── staging.json         # Workspace name, app id, parsing schema for STAGE
│           └── prod.json            # Workspace name, app id, parsing schema for PROD
├── database/
│   └── <schema>/                    # Synchronized DBMS_METADATA mirror (tables, views, pkgs)
├── migrations/
│   ├── <developer_name>/            # Per-developer migration files (e.g. alice/, bob/)
│   │   └── YYYYMMDD_HHMMSS_<desc>.sql
│   └── applied/                     # Optional local ledger of applied migrations
├── scripts/
│   ├── team.sh / team.ps1           # Central developer CLI wrapper
│   ├── check_conflicts.py           # Pre-migration conflict and collision detector
│   ├── check_builder_drift.py       # Pre-publish Builder drift detection guard
│   ├── export_apps.sh / .ps1        # SQLcl APEXLANG export automation
│   ├── publish_app.sh / .ps1        # Safe APEX import using deployments/*.json
│   ├── deploy.sh / .ps1             # Promotion runner (dev -> staging -> prod)
│   ├── backup_db.sh / .ps1          # DBMS_METADATA schema snapshot
│   ├── load_env.sh / .ps1           # Strict literal .env parser
│   └── check_db_target.sh / .ps1    # Target connection and environment guard
└── scratch/                         # Local gitignored throwaway space
```

---

## 3. Configuration Contract (`.env.example`)

Mirrors the clean foundation of `APEX_PROJECT_TEMPLATE/.env.example`, allowing either a single shared connection (`docker-demo`) or separate table/code/APEX profiles, with optional staging and production deployment targets:

```bash
# Copy this file to .env and adjust the values for this developer checkout.
# Never store passwords, wallets, tokens, or credential-bearing URLs here.

PROJECT_NAME=my-team-apex
DB_ENVIRONMENT=development

# One or more APEX application IDs (numeric)
APEX_APP_ID=100,200

# 1. Tables Metadata Mirror
TABLES_SCHEMA=DEMO
TABLES_PREFIXES=*
TABLES_SQLCL_CONNECTION=docker-demo
TABLES_EXPECTED_USER=DEMO

# 2. Code Metadata Mirror
CODE_SCHEMA=DEMO
CODE_PREFIXES=*
CODE_SQLCL_CONNECTION=docker-demo
CODE_EXPECTED_USER=DEMO

# 3. APEX Application Export (Development)
APEX_PARSING_SCHEMA=DEMO
APEX_SQLCL_CONNECTION=docker-demo
APEX_EXPECTED_USER=DEMO

# 4. Target Deployment Connections
# Production connection is required for prod deployment commands
PROD_SQLCL_CONNECTION=prod-db
PROD_EXPECTED_USER=PROD_DEPLOYER

# Staging connection is OPTIONAL (only needed for projects with a staging tier)
# STAGING_SQLCL_CONNECTION=stage-db
# STAGING_EXPECTED_USER=STAGE_DEPLOYER
```

No `METADATA_SCHEMA`, no `VERIFY_SCHEMA`, and no `TEAM_CHECKOUT_UUID`.

---

## 4. Declarative Deployment Descriptors

Following the architecture in *The Missing Apartment Number*, each application under `apps/<parsing-schema>/<app-id>/` (or `apps/<app-id>/`) contains a `deployments/` folder specifying explicit target workspace names, application IDs, and parsing schemas.

- `dev.json` (Required): Local / shared development workspace context.
- `prod.json` (Required): Production workspace context.
- `staging.json` (Optional): Staging / QA workspace context, for teams with a staging tier.

### `apps/100/deployments/dev.json`
```json
{
  "workspace": {
    "name": "DEMO"
  },
  "app": {
    "id": 100,
    "databaseSession": {
      "parsingSchema": "DEMO"
    }
  }
}
```

### `apps/100/deployments/staging.json` (Optional)
```json
{
  "workspace": {
    "name": "STAGE_WORKSPACE"
  },
  "app": {
    "id": 100,
    "databaseSession": {
      "parsingSchema": "STAGE_APP"
    }
  }
}
```

### `apps/100/deployments/prod.json`
```json
{
  "workspace": {
    "name": "PROD_WORKSPACE"
  },
  "app": {
    "id": 100,
    "databaseSession": {
      "parsingSchema": "PROD_APP"
    }
  }
}
```

### `apps/100/deployments/prod.json`
```json
{
  "workspace": {
    "name": "PROD_WORKSPACE"
  },
  "app": {
    "id": 100,
    "databaseSession": {
      "parsingSchema": "PROD_APP"
    }
  }
}
```

---

## 5. Developer Workflows

### Route 1: Builder-First Workflow
1. Developer edits pages in the shared APEX Builder.
2. Developer runs `scripts/team.sh export <app-id>`.
3. SQLcl exports APEXlang split files to `apps/<app-id>/`.
4. Developer reviews diff with `git diff apps/<app-id>/` and commits to their local Git branch.
5. Colleagues obtain the latest Builder state by running `scripts/team.sh export <app-id>`.

### Route 2: File-First / Agent APEXlang Workflow
1. Developer or AI agent edits `.apx` files under `apps/<app-id>/` and commits locally.
2. Developer sends a quick notice on team chat: *"Publishing App 100 to DEV"*.
3. Developer runs `scripts/team.sh publish <app-id> --env dev`.
4. **Drift Guard:** The tool queries APEX Builder metadata for application `<app-id>`. If the live application was modified in Builder after the local export timestamp, publish halts with a warning to review and export before overwriting.
5. The tool imports the application into SQLcl using `apps/<app-id>/deployments/dev.json`.

### Route 3: Database Migrations & Conflict Checking
1. Each developer authors forward migration scripts in their own subfolder:
   `migrations/<developer>/YYYYMMDD_HHMMSS_<name>.sql`.
2. Before applying to DEV, the developer runs:
   `scripts/team.sh check-conflicts`
   - Scans all unapplied migrations across developer folders.
   - Detects duplicate object names (`CREATE TABLE X` in multiple files).
   - Detects conflicting column alterations or duplicate sequence names.
3. The developer applies their migration:
   `scripts/team.sh migrate migrations/<developer>/YYYYMMDD_HHMMSS_<name>.sql`
4. Other teammates run `scripts/team.sh backup-db` to update their `database/<schema>/` mirror.

### Route 4: Staging & Production Promotion
1. Releases are tagged in Git (e.g. `v1.2.0`).
2. Deployment command:
   `scripts/team.sh deploy <app-id> --env staging` (or `--env prod`)
3. Prompts the human operator:
   ```text
   Deploying App 100 to PROD_WORKSPACE (Parsing Schema: PROD_APP)...
   Are you sure you want to proceed? [y/N]:
   ```
4. Or with `--manual` flag:
   Generates a clean, copy-pasteable SQLcl script with exact `@deployments/prod.json` commands for the DBA.

---

## 6. Migration and Replacement Plan

1. Archive tag `archive/v1-metadata-experiment` preserves all prior work.
2. Clean `main` branch by clearing out legacy `scripts/teamlib/`, `ci/`, and `targets/` metadata artifacts.
3. Copy clean core scripts and structure from `/home/ash/projects/APEX_PROJECT_TEMPLATE/`.
4. Add the new team-specific enhancements:
   - `deployments/*.json` per application
   - `check_conflicts.py` for multi-developer migrations
   - `check_builder_drift.py` for pre-publish safety
   - Updated unified `scripts/team.sh`
5. Verify end-to-end functionality against local Oracle 26ai (`docker-demo`).
