# Team APEX agent contract

This repository is the APEX 26.1+ / APEXlang team template. The template
repository follows its normal branch and pull-request process. A project
created from it gives each developer a separate Git repository; the team
shares its development Oracle database and APEX workspace, not Git history.
Never exchange downstream commits or assume a colleague's repository changes
are present locally. A Git branch does not isolate shared database or Builder
state.

## Configuration and application source

- Copy `.env.example` to ignored `.env` and use SQLcl saved connection names.
  The default points the table, code, and APEX profiles at the same DEV
  connection. Keep these profile values aligned unless the project needs
  separate connections. Staging and production connection/user pairs are
  optional. Never put credentials in `.env` or tracked files.
- Use numeric APEX application IDs in commands and descriptors. Store each
  app's APEXlang source and `deployments/{dev,staging,prod}.json` under
  `apps/<parsing-schema>/<app-id>/`. `apps/templates/deployments/` contains
  descriptor examples. Each descriptor names the target workspace, numeric
  app ID, and parsing schema explicitly.
- Keep APEXlang and SQL files in LF line endings. Review generated source and
  database mirror changes before committing.
- The shared database is the runtime source of truth. Do not add custom team
  metadata tables, mutexes, checkout rosters, or database-backed migration
  ledgers.

## Team workflow

- **Builder-first:** Coordinate with teammates before editing a shared app in
  Builder. Run `scripts/team.sh export <app-id>`, review the APEXlang diff, and
  commit it. Export records the observed Builder state; do not import as part
  of this route.
- **File-first:** Edit and commit the exact APEXlang source. Before importing
  to shared DEV, tell the team which numeric app ID is being published and
  check for in-progress Builder edits. Run
  `scripts/team.sh publish <app-id> --env dev`. The drift guard compares the
  live app's update time with the database-time revision captured before the
  latest export. The exporter also checks that Builder did not change during
  that export. Oracle records this value at one-second precision, so the
  exporter and guard fail closed when it matches the current database second.
  This timestamp cannot see unsaved Builder edits; communicate with teammates
  before publishing. The guard refuses to overwrite newer Builder work. Export
  and reconcile before retrying. Use `--force` only when the user explicitly
  directs an override after review.
- **Migrations:** Add immutable files under `migrations/<developer>/`, run
  `scripts/team.sh check-conflicts`, then apply selected files with
  `scripts/team.sh migrate migrations/<developer>/<file>.sql`. A migration
  changes the shared DEV schema for everyone. The checker reports duplicate
  table, view, sequence, and added-column declarations across developer
  folders; it does not replace SQL review.
- **Promotion:** Put an explicit deployment descriptor in the application
  source. Use `scripts/team.sh deploy <app-id> --env staging` or `--env prod`;
  each direct import requires the displayed `[y/N]` confirmation. Add
  `--manual` to print a DBA runbook without connecting. A deployment writes
  to the selected APEX workspace and parsing schema.
- **Other commands:** `scripts/team.sh doctor` validates `.env` and performs a
  read-only SQLcl identity check. `scripts/team.sh backup-db` refreshes the
  local table and code mirrors.

## Rules for coding agents

- Read the relevant `.apx`, APEXlang, SQL, and deployment descriptor files
  before editing them. Preserve the app's numeric ID and explicit workspace
  mapping. Do not invent a live workspace, schema, connection, or migration
  result.
- Treat export, migration, publish, and deployment commands according to
  their database effects. Do not run a migration, import, or deployment unless
  the user requested that operation. Export only when requested because it
  refreshes tracked source from shared Builder state.
- Coordinate shared DEV app imports with the human team. There are no database
  pause tables or acknowledgement commands in this workflow. Never claim that
  team communication or a live database check happened unless it did.
- Do not commit or push silently. Follow explicit instructions for commits;
  do not push unless asked.
- If SQLcl, the database, or a requested environment is unavailable, report
  the check as unknown or unavailable rather than passed.

See [README.md](README.md) for setup and command examples, and
[migrations/README.md](migrations/README.md) for the migration file contract.
