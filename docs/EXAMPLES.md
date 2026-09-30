# Examples

Copy-paste recipes for everyday tasks. Each one says what you want, what to
run, what you should see, and what can go wrong. They use the example app
`100` in schema `DEMO`; replace those with your own. New here? Do
[GETTING_STARTED.md](GETTING_STARTED.md) first.

1. [Bring a change made in Builder into Git](#1-bring-a-change-made-in-builder-into-git)
2. [Edit app files and publish them](#2-edit-app-files-and-publish-them)
3. [Add a column to a table safely](#3-add-a-column-to-a-table-safely)
4. [Test a SQL change before touching the database](#4-test-a-sql-change-before-touching-the-database)
5. [Keep a read-only copy of the database in Git](#5-keep-a-read-only-copy-of-the-database-in-git)
6. [Work with more than one schema](#6-work-with-more-than-one-schema)
7. [Release to staging and production](#7-release-to-staging-and-production)
8. [Find out how two environments differ](#8-find-out-how-two-environments-differ)
9. [Update the template](#9-update-the-template)
10. [Use an AI assistant on this project](#10-use-an-ai-assistant-on-this-project)
11. [Ask questions about how the app uses the database](#11-ask-questions-about-how-the-app-uses-the-database)

## 1. Bring a change made in Builder into Git

**When:** someone (you or a teammate) changed app 100 in APEX Builder and you
want the change reviewed and committed.

```bash
scripts/team.sh export 100
git status --short apps/DEMO/100/
git diff apps/DEMO/100/
git add apps/ && git commit -m "Capture Builder changes to app 100"
```

**You should see:** `Exporting Workspace DEMO - application 100:...`, then a
diff of the `.apx` files that changed.

**If it goes wrong:**
- `refusing to export over dirty mirror: apps/DEMO/100`: you have uncommitted
  edits to that app. Commit or stash them first; export never overwrites them.
- The export fails with a message about the current database second: wait one
  second and run it again.

## 2. Edit app files and publish them

**When:** you would rather change the app in a text editor than in Builder.

1. Edit a file, for example `apps/DEMO/100/pages/p00001-dashboard.apx`.
2. Tell your team you are publishing app 100 and check nobody is editing it in
   Builder. Git cannot see Builder.
3. Run:

```bash
scripts/team.sh publish 100 --env dev
```

**You should see** the stamped version and, at the end:

```text
Published APEX App 100 to DEV (DEMO / DEMO).
Commit the stamped version in apps/DEMO/100/application.apx: Release 1.0 [ALICE-2026-09-30r001]
```

Commit `application.apx`: it carries the stamp that lets teammates tell whose
import is live.

**If it goes wrong:**
- `[DRIFT DETECTED] Live APEX App 100 was modified in Builder on ...`: someone
  changed the live app after your last export. Run `scripts/team.sh export 100`,
  review the diff, merge it with your edits, and publish again. Do not use
  `--force` unless you have reviewed the difference.
- `deployment descriptor not found`: create `apps/DEMO/100/deployments/dev.json`
  (see step 8 of the getting started guide).

## 3. Add a column to a table safely

**When:** a table needs a new column and you want it checked, applied and
recorded.

Create `migrations/2026-09-30_add-notes-to-hr-users-r001/` with two files.

`001-add-notes-column.sql`:

```sql
ALTER TABLE HR_USERS ADD (NOTES VARCHAR2(200));
```

`checks.json`:

```json
{
  "schemaVersion": 1,
  "preconditions": [
    {
      "id": "notes-column-absent",
      "sql": "SELECT CASE WHEN COUNT(*) = 0 THEN 1 ELSE 0 END FROM user_tab_columns WHERE table_name = 'HR_USERS' AND column_name = 'NOTES'",
      "expected": 1
    }
  ],
  "postconditions": [
    {
      "id": "notes-column-present",
      "sql": "SELECT CASE WHEN COUNT(*) = 1 THEN 1 ELSE 0 END FROM user_tab_columns WHERE table_name = 'HR_USERS' AND column_name = 'NOTES'",
      "expected": 1
    }
  ]
}
```

```bash
scripts/team.sh check-conflicts migrations/2026-09-30_add-notes-to-hr-users-r001 --env dev
scripts/team.sh migrate migrations/2026-09-30_add-notes-to-hr-users-r001 --env dev
git add migrations/ && git commit -m "Add HR_USERS.NOTES"
```

**You should see:** `Result: exit 0; 0 conflict(s), 0 incomplete/error
condition(s)` from the check, then `Applied and verified ... on dev; receipt
status.dev.json.` from `migrate`.

**Rules to remember:**
- A check is a query returning exactly one row with the number `1`; `expected`
  is always `1`.
- After you have tried to apply a migration, never edit it. Fix problems in a
  new folder with the next revision (`...-r002`).
- If the check says `COLUMN_ALREADY_EXISTS`, the change is already in the
  database (or a teammate added it). Do not apply it again.

## 4. Test a SQL change before touching the database

**When:** you want a quick answer, or you have no database connection.

```bash
scripts/team.sh check-conflicts migrations/2026-09-30_add-notes-to-hr-users-r001 --local
```

**You should see:** `Local selected-batch analysis only; no live database state
was checked.` and `INCOMPLETE [LIVE_PREREQUISITE_UNKNOWN]`. That is expected:
offline analysis can read your files but cannot know whether a table exists.
Use `--env dev` for the real answer.

## 5. Keep a read-only copy of the database in Git

**When:** you want to review database changes in pull requests, or let tools such
as the knowledge graph see your tables and code.

```bash
scripts/team.sh backup-db
git status --short database/
git add database/ && git commit -m "Refresh database mirror"
```

**You should see** a stream of SQLcl output, then object counts per type
(`PACKAGE=5`, `TRIGGER=12`, ...). The copy lands in `database/DEMO/tables/`,
`views/`, `packages/`, `procedures/`, `functions/`, `triggers/` and
`synonyms/`. It holds structure only, never data. Never edit it by hand.

**If it goes wrong:** `refusing to back up over dirty mirror: database/DEMO`
means you have uncommitted changes there. Commit or discard them first.

## 6. Work with more than one schema

**When:** several schemas share one APEX workspace and each has its own tables
and code.

In `.env`, give each setting a comma-separated list. The three settings of a
profile must have the same number of entries, in the same order:

```dotenv
TABLES_SCHEMA=HR,SALES
TABLES_SQLCL_CONNECTION=dev-hr,dev-sales
TABLES_EXPECTED_USER=HR,SALES

CODE_SCHEMA=HR,SALES
CODE_SQLCL_CONNECTION=dev-hr,dev-sales
CODE_EXPECTED_USER=HR,SALES

APEX_PARSING_SCHEMA=HR,SALES
APEX_SQLCL_CONNECTION=dev-hr,dev-sales
APEX_EXPECTED_USER=HR,SALES
APEX_APP_ID=100,200
```

Then work as usual. `doctor` and `backup-db` cover every schema, `export` and
`publish` find each app's schema for you, and `--schema` picks one:

```bash
scripts/team.sh doctor
scripts/team.sh backup-db --schema SALES     # only one schema
scripts/team.sh check-conflicts migrations/SALES/2026-09-30_add-flag-r001 --env dev
```

**Migrations go in a folder named for their schema** when more than one schema
is configured: `migrations/HR/...` and `migrations/SALES/...`. One migration
changes one schema. For a change that touches two (a grant, say) write two
migrations.

**If it goes wrong:**
- `needs one schema because several are configured`: add `--schema <NAME>`.
- `must live under migrations/<SCHEMA>/`: move your migration into its
  schema's folder.
- `schema X is not listed in STAGING_SCHEMA`: a schema you deploy to staging
  must appear, with the same name, in the staging settings.

## 7. Release to staging and production

**When:** the app works in DEV and you want to promote it.

Add the target connection and schema to `.env` (once):

```dotenv
STAGING_SQLCL_CONNECTION=stage-db
STAGING_EXPECTED_USER=STAGE_DEPLOYER
STAGING_SCHEMA=APP_STAGE
```

Copy `apps/templates/deployments/staging.json` to
`apps/DEMO/100/deployments/staging.json` and fill in the workspace and schema.
Then:

```bash
scripts/team.sh deploy 100 --env staging
```

**You should see** the application, workspace, target schema and connection,
then `Deploying to STAGING. Proceed? [y/N]:`. Nothing happens until you answer
`y`.

**Prefer a DBA to run it?** Produce a runbook without connecting:

```bash
scripts/team.sh deploy 100 --env prod --manual
```

`--manual` never opens a database connection. A DBA runs the printed SQLcl steps
with an approved saved connection, then runs the re-export check the runbook ends with.

## 8. Find out how two environments differ

**When:** staging misbehaves and you suspect it differs from DEV.

```bash
scripts/team.sh compare-schema --from dev --to staging --object HR_USERS --object HR_ROLES
scripts/team.sh compare-schema --env prod --pattern 'HR_*'
```

**You should see** a list such as `MISSING_ON_TARGET: TABLE HR_ROLES` with the
source DDL, or no differences. Add `--format json` for a machine-readable
report.

**If it goes wrong:**
- `at least one --object or --pattern selector is required`: name what to compare.
- `SELF_COMPARISON`: both sides resolve to the same database and schema, so
  there is nothing to compare. Check your staging settings.

## 9. Update the template

**When:** the template gained fixes or features and you want them.

```bash
git status                                   # commit your work first
scripts/team.sh upgrade-template --dry-run   # show the plan
scripts/team.sh upgrade-template
git status
```

The upgrade only touches template-owned files (scripts, tests, CI, agent
guidance, `README.md`). Your apps, database copy, migrations, `.env` and your
`PROJECT.md` notes are never touched. If you customized a file the template also
changed, you get `<file>.template-new` beside it and exit status 1: merge the
two, delete the `.template-new` file, and commit.

## 10. Use an AI assistant on this project

The repository is set up for AI coding assistants: `AGENTS.md` (also loaded by
`CLAUDE.md`) gives them the rules, and `.agents/skills/` gives them specialist
skills that they load on their own when a task matches. See the
[skills list](../README.md#skills-and-agent-support).

Good first requests:

- `/init`: sets up your `.env` by asking a few questions. It never asks for or
  writes passwords.
- "Export app 100 and summarize what changed since the last commit."
- "Write a migration that adds a NOTES column to HR_USERS, with checks, and run
  `check-conflicts --local` on it."
- "Why does `doctor` fail?" (it reads [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
  and the scripts' output).

The safety rules apply to the assistant too: it should not run a migration,
publish or deployment unless you asked, and it should tell you when a live check
could not be run.

## 11. Ask questions about how the app uses the database

**When:** you want to know which pages read a table, or what calls a package.

Graphify builds a knowledge graph that links each app's pages, regions and
processes to the tables and packages they use.

```bash
uv tool install graphifyy --with tree-sitter-sql
python3 scripts/setup_graphify_apx.py
scripts/team.sh backup-db                  # so the database objects exist locally
graphify extract . --force
graphify query "which pages read the HR_USERS table?"
```

The first build may ask you to configure a semantic backend (it summarizes the
notes in `app_context/`); follow Graphify's own setup for that. After later
changes run `graphify update .`, which runs locally at no cost. After `backup-db` or when tables
or packages change, run `python3 scripts/setup_graphify_apx.py` first, so pages
link to the real table nodes instead of stale cached ones. The graph reflects
your repository's files, not the live database; export and back up first when
you need it current. More in
[`.agents/workflows/graphify.md`](../.agents/workflows/graphify.md).
