# APEX project template for teams

This template supports APEX 26.1+ applications authored as APEXlang. It uses
plain Git source, explicit deployment JSON, and per-developer SQL migration
files. No custom team metadata schema or database lock tables are required.

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
share that connection. Staging and production profiles are optional and should
be configured as connection/user pairs. Store credentials in SQLcl's secure
connection store, never in `.env` or Git.

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

## Builder-first workflow

Make and save the change in shared APEX Builder, then export that app's source:

```bash
scripts/team.sh export 100
git status --short --untracked-files=all -- apps/DEMO/100/
git diff -- apps/DEMO/100/
```

Review the APEXlang changes before committing. This route captures current
Builder state; it does not import the app back into Builder. The exporter
refuses to overwrite a dirty local source directory.

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

Create timestamped SQL files in your own developer folder and keep them
immutable after applying them to shared DEV:

```text
migrations/alice/20260926_101500_add_status.sql
migrations/bob/20260926_111000_create_audit_table.sql
```

Check all developer folders before applying a selected migration:

```bash
scripts/team.sh check-conflicts
scripts/team.sh migrate migrations/alice/20260926_101500_add_status.sql
```

Migration bodies run with SQLcl substitution disabled, so `&` is treated as
ordinary SQL text. Migration files may contain SQL statements ending in
semicolons and Oracle forms that use a standalone slash, including PL/SQL
blocks, `CREATE TYPE`, `CREATE LIBRARY`, `CREATE JAVA`, and
`CREATE MLE MODULE`. SQLcl client
commands such as `SET DEFINE`, `PROMPT`, and `WHENEVER` are rejected before
connecting.

The conflict checker looks for duplicate table, view, sequence, and
`ALTER TABLE ... ADD` column declarations. It is a guard for common collisions,
not a substitute for reviewing the SQL. A successful migration changes shared
DEV schema state. Other developers can refresh their local DBMS_METADATA
mirrors with `scripts/team.sh backup-db`.

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
check. A human DBA executes it using an approved saved connection. Database
migrations remain a DEV workflow; do not use the deployment command to apply
schema changes.

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
| `scripts/team.sh check-conflicts` | Check DDL declarations across migration folders. |
| `scripts/team.sh migrate <file> [...]` | Check conflicts, then apply selected migration files to DEV. |
| `scripts/team.sh backup-db` | Refresh local table and code metadata mirrors. |
| `scripts/team.sh deploy <id> --env <staging\|prod> [--manual]` | Confirm a promotion or print a DBA runbook. |
| `scripts/team.sh upgrade-template [--dry-run]` | Update template-owned files; never overwrites project files. |

`scripts/team.ps1` exposes the same commands for PowerShell. Migration and
deployment helpers use Bash, such as Git Bash on Windows.

## Agent guidance

Agents follow the same app ID, descriptor, drift, migration, and deployment
rules as developers. Review source before editing; do not run database-writing
commands unless the user asked; coordinate with the team before importing into
shared DEV. Never claim an unavailable live check passed. See [AGENTS.md](AGENTS.md)
for the full repository contract and [migrations/README.md](migrations/README.md)
for migration naming and immutability rules.
