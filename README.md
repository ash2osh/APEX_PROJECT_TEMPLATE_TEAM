# APEX project template for teams

This template supports APEX 26.1+ applications authored as APEXlang. It uses
plain Git source, explicit deployment JSON, and per-developer SQL migration
files. No custom team metadata schema or database lock tables are required.

Projects created from this template use one Git repository per developer.
Those repositories do not share commits or a remote; developers coordinate
through the shared development database and APEX Builder workspace. Branches
separate file changes only and do not isolate a shared APEX application.

## Quickstart

Copy the example configuration, adjust SQLcl saved connection names and
expected users, then check the DEV connection:

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

`scripts/team.ps1` exposes the same commands for PowerShell. Migration and
deployment helpers use Bash, such as Git Bash on Windows.

## Agent guidance

Agents follow the same app ID, descriptor, drift, migration, and deployment
rules as developers. Review source before editing; do not run database-writing
commands unless the user asked; coordinate with the team before importing into
shared DEV. Never claim an unavailable live check passed. See [AGENTS.md](AGENTS.md)
for the full repository contract and [migrations/README.md](migrations/README.md)
for migration naming and immutability rules.
