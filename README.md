# APEX project template for teams

This template supports APEX 26.1+ applications authored as APEXlang. It uses
plain Git source, explicit deployment JSON, and dated, ordered SQL migration
folders. No custom team metadata schema or database lock tables are required.

Projects created from this template use one Git repository per developer.
Those repositories do not share commits or a remote; developers coordinate
through the shared development database and APEX Builder workspace. Branches
separate file changes only and do not isolate a shared APEX application.

## Quickstart

Copy the example configuration, set `DEVELOPER_NAME` to your uppercase name
(for example `ASHARIF`), adjust SQLcl saved connection names and expected
users, then check the DEV connection:

```bash
cp .env.example .env
scripts/team.sh doctor
```

The example uses `docker-demo` / `DEMO`. Table, code, and APEX settings can
share that connection. Staging and production app-deployment profiles are
optional connection/user pairs; migrations and schema comparison also require
an explicit target schema. Store credentials in SQLcl's secure connection
store, never in `.env` or Git.

On Windows, use the PowerShell wrapper:

```powershell
Copy-Item .env.example .env
pwsh -File scripts/team.ps1 doctor
```

## Application files and deployment descriptors

Use the numeric APEX application ID as the stable source directory key. The
standard layout is:

```text
apps/DEMO/100/
├── application.apx
├── .apex/
└── deployments/
    ├── dev.json
    ├── staging.json
    └── prod.json
```

`apps/templates/deployments/` contains example JSON descriptors. Copy the
needed descriptors into each app and set its workspace name, numeric app ID,
and target parsing schema. Keep `dev.json` aligned with the configured DEV
profile. A staging descriptor is needed only when the project uses staging.

## Several schemas in one workspace

The schema, SQLcl connection, and expected-user values in each profile are
comma-separated lists aligned by position. For example, this DEV setup uses
two schemas in each profile:

```dotenv
TABLES_SCHEMA=EPROMHQ,TMS
TABLES_SQLCL_CONNECTION=42_epromhq,42_tms
TABLES_EXPECTED_USER=EPROMHQ,TMS

CODE_SCHEMA=EPROMHQ,TMS
CODE_SQLCL_CONNECTION=42_epromhq,42_tms
CODE_EXPECTED_USER=EPROMHQ,TMS

APEX_PARSING_SCHEMA=EPROMHQ,TMS
APEX_SQLCL_CONNECTION=42_epromhq,42_tms
APEX_EXPECTED_USER=EPROMHQ,TMS
APEX_APP_ID=117,301
```

All three values in each profile must have the same number of entries, and a
schema may appear only once in a profile. A single value keeps the existing
single-schema behavior. Prefix filters apply to every schema in their profile.
When staging or production profiles are configured, a DEV schema maps to the
same name in those schema lists. One exception keeps today's behavior: a
project with a single DEV schema may name its staging or production schema
differently, and that mapping applies only to the project's one configured DEV
schema. An unlisted schema is never mapped to a target.

Pass `--schema <NAME>` to narrow a command to one schema. `doctor` and
`backup-db` check or mirror all configured schemas by default.

| Command | Multi-schema behavior |
| --- | --- |
| `doctor` | Checks every distinct `(connection, expected user, schema)` across the three profiles with one read-only SQLcl identity check each. It reports each schema and fails if any check fails. `--schema` narrows it. |
| `export <id>` | Reads the app's parsing schema from `APEX_APPLICATIONS` using the first `APEX_SQLCL_CONNECTION` entry, requires that schema in `APEX_PARSING_SCHEMA`, then reconnects with that schema's own connection and writes `apps/<SCHEMA>/<id>/`. `team.sh export` takes one ID. `scripts/export_apps.sh` with no argument resolves each configured app ID separately and installs all exports in one all-or-nothing mirror replacement. |
| `backup-db` | Runs the tables scope for each tables schema and the code scope for each code schema. Each has a staging directory, manifest count check, and dirty-mirror check. Nothing is installed until every schema verifies; one mirror replacement installs them all. `--schema` narrows the run. |
| `publish <id>` | Requires the app folder, descriptor, and live app parsing schema to agree and be listed, then uses that schema's connection. The drift guard, version stamp, and byte-equality verification remain unchanged. |
| `deploy <id> --env staging\|prod` | The descriptor's `parsingSchema` selects the same-named staging or production entry. Confirmation shows the schema and connection. |
| `migrate`, `check-conflicts` | Uses `migrations/<SCHEMA>/YYYY-MM-DD_<name>-rNNN/`; the folder selects the target entry. Scans, revision ordering, and receipts are scoped to one schema, and one invocation cannot mix schemas. |
| `compare-schema` | Requires `--schema` when more than one schema is configured and compares that schema across the selected environments. |

When `CODE_SCHEMA` has one schema, the existing flat
`migrations/YYYY-MM-DD_<name>-rNNN/` layout remains valid and the
`migrations/<SCHEMA>/` layout is also accepted. With two or more code schemas,
the schema folder is required and a flat folder is refused. One migration
changes one schema. A cross-schema change such as a grant or synonym is two
coordinated migrations, one per schema. Private synonyms are mirrored under
`database/<SCHEMA>/synonyms/` with the code objects.

Limits:

- Position-aligned lists rely on order; the loader cannot detect values that
  were swapped consistently across a profile.
- Schemas sharing one SQLcl connection share its privileges. Identity checks
  confirm the session user, not that it can reach only one schema.
- `--local` conflict checks load no `.env`, so they cannot enforce the
  schema-folder layout rule.
- `publish --force` skips the drift check, but still requires schema agreement
  between the app folder, descriptor, and live app.
- The backup manifest count guard accepts a non-empty subset of type rows.
  This is pre-existing, and synonyms inherit the same limit.

## Builder-first workflow

Make and save the change in shared APEX Builder, then export that app's source:

```bash
scripts/team.sh export 100
git status --short --untracked-files=all -- apps/DEMO/100/
git diff -- apps/DEMO/100/
```

Review the APEXlang changes before committing. This route captures current
Builder state; it does not import the app back into Builder. The exporter
refuses to overwrite a dirty local source directory. It first exports into
`scratch/`, checks the SQLcl result and Builder revision, then replaces the
local source mirror while preserving its authored deployment descriptors.

## File-first APEXlang workflow

Edit the APEXlang files, review them, and commit the source. Before importing
into shared DEV, tell teammates which app ID is being published and check for
unsaved or in-progress Builder edits. Then run:

```bash
scripts/team.sh publish 100 --env dev
```

The exporter records the app's `last_updated_on` value from Oracle before it
reads APEXlang, then checks the value again when the export completes. If the
app changed during export, it refuses to install that source. The DEV drift
guard compares the current Builder value with this database-time baseline, so
the workstation's time zone does not affect the check. If Builder is newer,
export and reconcile before importing. `--force` bypasses only the drift guard
and should be used only after the discrepancy is reviewed. Oracle stores this
revision at one-second precision; if the revision matches the database's
current second, export and publish fail closed and ask you to retry after one
second. This persisted timestamp cannot reveal unsaved Builder edits, so
coordinate with teammates before publishing. Staging and production promotion
uses the separate `deploy` command.

APEX does not stamp `last_updated_on` while importing, so an app installed or
published from APEXlang has no Builder timestamp until someone saves it in
Builder. To make each import identifiable, a DEV publish first stamps a
publish tag onto the end of the application version in `application.apx`:

```text
version: "V2 Powered By xxx [ASHARIF-2026-09-26r001]"
```

The tag is `DEVELOPER_NAME` from `.env`, the publish date, and a counter that
counts up with each publish of that date, whoever published, and restarts at
`r001` on a later date. The date never moves back, so a tag is never reused.
The export marker records the live version with the Builder timestamp, and the
drift guard refuses to publish when the live version differs from your
baseline, which is how it detects a teammate's import. Commit the stamped
`application.apx` after a successful publish. If the import fails, publish
restores the unstamped file. The tag appears wherever the theme renders
`#APP_VERSION#`, such as the Universal Theme footer, and `deploy` ships it to
staging and production unchanged.

After a successful DEV import, `publish` exports the app again and compares the
APEXlang file names and bytes with the local source, excluding the local
deployment descriptors and export marker. It advances the drift baseline only
when that source matches and the live Builder revision stayed stable during the
verification export. If SQLcl reports an error, the source differs, or the
revision is ambiguous, publish fails and leaves the old baseline in place.
SQLcl can exit successfully without importing, for example when a descriptor
names an unknown workspace, so publish also requires its `Import successful.`
line.
Publish preflight also requires the selected application tree to be physically
inside the checkout and rejects symbolic links and reparse points.

When publish refuses, see [docs/publish-rules.md](docs/publish-rules.md) for
every rule, what each message means, and what to do.

## Schema migrations

Create one dated folder per migration. Put each SQL step in its own numbered
file and list the folder when checking or applying it:

```text
migrations/2026-09-27_create-customers-r001/
├── 001-create-table.sql
├── 002-create-index.sql
├── 003-create-view.sql
└── checks.json
```

The folder date is a creation date. Names sort newest first only when the file
browser is sorted descending; they do not control its sort setting. SQL steps
run in ascending sequence, while multiple folders run in the explicit
command-line order. `create-customers-r001` and
`2026-09-28_create-customers-r002` are separate sequential migrations; the
second contains the follow-up change. Keep a migration and its `checks.json`
immutable once an apply attempt may have started. A recovery belongs in the
next revision.

Run a selected live preflight and apply with an explicit environment:

```bash
scripts/team.sh check-conflicts \
  migrations/2026-09-27_create-customers-r001 --env dev
scripts/team.sh migrate \
  migrations/2026-09-27_create-customers-r001 --env dev
```

The offline `--local` checker analyzes selected files only. Live preflight
compares the selected batch with the target catalog, but cannot see pending
files in another developer's independent repository. Two developers can pass
before either writes; preflight runs again before apply, though a concurrent
change can still race it. See [docs/migration-rules.md](docs/migration-rules.md)
for coverage and recovery limits.

`checks.json` requires read-only preconditions and postconditions.
`status.<env>.json` appears only after the committed result has been verified
through a fresh connection. It is local evidence, not a shared deployment
ledger. Migration SQL can use standalone slash terminators for PL/SQL blocks,
`CREATE TYPE`, `CREATE LIBRARY`, `CREATE JAVA`, and `CREATE MLE MODULE`.
SQLcl client commands such as `SET DEFINE`, `PROMPT`, and `WHENEVER` are
rejected before connecting.

For staging or production, configure the existing connection and expected
user plus `STAGING_SCHEMA` or `PROD_SCHEMA`. The migration schema can differ
between environments, so these settings are explicit and never inferred from
DEV. Direct staging and production applies require interactive confirmation.

Compare selected live object names or prefixes with `compare-schema`:

```bash
scripts/team.sh compare-schema --from dev --to staging \
  --object CUSTOMERS --object ORDERS
scripts/team.sh compare-schema --env prod --pattern 'HR_*' --pattern 'GL_*'
```

This reports current catalog drift for the selection. Status receipts can help
with follow-up review but cannot reliably prove which migration file caused a
shape: separate migrations may produce the same DDL, and a manual change can
mimic a migration.

## Staging and production deployments

For direct promotion, configure the target connection/user pair in `.env` and
provide a per-app descriptor. The tool prints the selected app, workspace,
and schema before asking for confirmation:

```bash
scripts/team.sh deploy 100 --env staging
scripts/team.sh deploy 100 --env prod
```

Direct staging and production imports require `Deploying to <ENV>. Proceed?
[y/N]`. To prepare a DBA handoff without opening a database connection, use:

```bash
scripts/team.sh deploy 100 --env prod --manual
```

The runbook includes the explicit deployment descriptor and SQLcl identity
check. A human DBA executes it using an approved saved connection. App
promotion uses deployment descriptors; schema migrations use the separate
`migrate --env` command and explicit target-schema settings.

## Upgrading from the template

Projects created from this template do not share its Git history, so updates
are copied file by file. `template-manifest.json` decides which repository
files the upgrade may touch: template scripts, tests, CI, and agent guidance
are upgraded; project files such as `AGENTS.project.md`,
`.agents/rules/project.md`, and `PROJECT.md` are created once and never
overwritten; application source, database mirrors, migrations,
`app_context/<id>/`, and `.env` are never touched. Put project-specific
instructions in those placeholder files, not in `AGENTS.md` or `README.md`.
The engine also writes its fixed `.template-lock.json` metadata file, which
records the installed template commit and hashes.

Commit your work, then run:

```bash
scripts/team.sh upgrade-template --dry-run
scripts/team.sh upgrade-template
git status
```

A file you customized is kept when the template did not change it. When both
changed, the upgrade keeps your file, writes the new template version beside
it as `<file>.template-new`, and exits with status 1: merge the two, delete the
`.template-new` file, and commit. The next upgrade refuses to run while any
`.template-new` file remains. The upgrade records the installed template commit
in `.template-lock.json`; commit it with the upgraded files. Use `--ref <tag>`
to install a specific template version and `--source <url>` for a fork. If the
upgrade reports that `.env` needs attention, compare it with `.env.example`.

Projects created before `template-manifest.json` existed do not have the
upgrade script yet. Run it once from a fresh template clone. When the target
repository has no `.template-lock.json`, the source resolves from its own
`template-manifest.json`; `--source` overrides that value, and a lock's upstream
is used when no explicit source is supplied:

```bash
git clone https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM.git /tmp/apex-template
python3 /tmp/apex-template/scripts/upgrade_template.py --project-root . --source https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM.git
```

That first run has no lock, so every file that differs from the template is
reported as a conflict instead of being overwritten. Keep the project clean
before running it; conflicts must be reviewed and merged manually.

The engine stages updates and restores them if a filesystem operation fails.
If it reports an incomplete rollback, keep the named recovery directory and
restore those backups before retrying the upgrade.

## Command reference

| Command | Purpose |
| --- | --- |
| `scripts/team.sh doctor` | Validate `.env` and check the DEV SQLcl identity. |
| `scripts/team.sh export <id>` | Export one numeric app from shared DEV Builder. |
| `scripts/team.sh publish <id> --env dev` | Drift-check and import one app to DEV. |
| `scripts/team.sh check-conflicts <folder> [...] --env <env>` | Preflight selected migrations against a live schema. |
| `scripts/team.sh check-conflicts <folder> [...] --local` | Analyze selected migrations without a connection. |
| `scripts/team.sh migrate <folder> [...] --env <env>` | Verify and apply selected migration folders to DEV, staging, or production. |
| `scripts/team.sh compare-schema --env <env> --object <name>` | Compare selected live schema objects read-only. |
| `scripts/team.sh backup-db` | Refresh local table and code metadata mirrors. |
| `scripts/team.sh deploy <id> --env <staging\|prod> [--manual]` | Confirm a promotion or print a DBA runbook. |
| `scripts/team.sh upgrade-template [--dry-run]` | Update template-owned files; never overwrites project files. |

`scripts/team.ps1` exposes the same commands for PowerShell. Migration and
deployment helpers use Bash, such as Git Bash on Windows.

## Optional knowledge graph

Graphify is optional; every command in this template works without it, and
agents fall back on grep. It indexes a domain-only corpus (`apps/`, `database/`,
and `app_context/`) through a repository-owned APEXlang extractor, so `.apx`
files are read as APEX architecture — containment, navigation, authorization,
database reads and writes, and PL/SQL calls — rather than as generic SQL. The
graph reflects this repository's files; export first when it must describe
current Builder state.

Install Graphify and its SQL parser (the distribution is `graphifyy`, the
command is `graphify`), then configure a supported semantic backend:

```bash
uv tool install graphifyy --with tree-sitter-sql
python3 scripts/setup_graphify_apx.py
graphify extract . --force
```

- `python3 scripts/setup_graphify_apx.py --verify` checks the installation
  without changing it; rerun setup after **every Graphify upgrade**.
- `graphify update .` after APEXlang or database changes (local, no API cost).
- `graphify extract .` after changing `app_context`.
- `python3 scripts/setup_graphify_apx.py` then `graphify update .` after the first
  `backup-db` or when tables or packages change, so pages link to the real
  database nodes instead of cached stubs.
- `graphify extract . --force` after changing `.graphifyignore`.

`graphify-out/` is local and gitignored. See
[`.agents/workflows/graphify.md`](.agents/workflows/graphify.md).

## Agent guidance

Agents follow the same app ID, descriptor, drift, migration, and deployment
rules as developers. Review source before editing; do not run database-writing
commands unless the user asked; coordinate with the team before importing into
shared DEV. Never claim an unavailable live check passed. See [AGENTS.md](AGENTS.md)
for the full repository contract, [migrations/README.md](migrations/README.md),
and [docs/migration-rules.md](docs/migration-rules.md) for migration naming,
verification, and comparison limits. For browser runtime checks, see
[Chrome DevTools MCP](docs/CHROME_DEVTOOLS_MCP.md) and use the project browser
[skill](.agents/skills/chrome-devtools-mcp/SKILL.md).
