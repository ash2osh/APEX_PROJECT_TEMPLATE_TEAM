# Team APEX agent contract

This repository is the APEX 26.1+ / APEXlang team template. The template
repository follows its normal branch and pull-request process. A project
created from it gives each developer a separate Git repository; the team
shares its development Oracle database and APEX workspace, not Git history.
Never exchange downstream commits or assume a colleague's repository changes
are present locally. A Git branch does not isolate shared database or Builder
state.

## Configuration and application source

- Copy `.env.example` to ignored `.env`, set `DEVELOPER_NAME` to the
  developer's uppercase name, and use SQLcl saved connection names. The
  default points the table, code, and APEX profiles at the same DEV
  connection. Keep these profile values aligned unless the project needs
  separate connections. Staging and production connection/user pairs are
  optional. Each profile's schema, connection, and expected-user keys accept
  position-aligned comma lists; one value keeps current behavior. Use
  `--schema <NAME>` to narrow any command. `doctor` and `backup-db` default to
  all schemas. Never put credentials in `.env` or tracked files. An optional,
  independent `ORDS_SCHEMA`, `ORDS_SQLCL_CONNECTION` and `ORDS_EXPECTED_USER`
  profile (all three or none) enables the read-only ORDS export; its session
  user must equal the REST schema.
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
- **File-first:** Edit the exact APEXlang source; committing before publish
  is optional, since publish imports the files on disk. Before importing
  to shared DEV, tell the team which numeric app ID is being published and
  check for in-progress Builder edits. Run
  `scripts/team.sh publish <app-id> --env dev`. The drift guard compares the
  live app's update time with the database-time revision captured before the
  latest export. The exporter also checks that Builder did not change during
  that export. Oracle records this value at one-second precision, so the
  exporter and guard fail closed when it matches the current database second.
  This timestamp cannot see unsaved Builder edits; communicate with teammates
  before publishing. APEX leaves it null after an APEXlang import, so each
  DEV publish stamps `[DEVELOPER_NAME-YYYY-MM-DDrNNN]` onto the application
  version in `application.apx` before import, and the guard also refuses when
  the live version differs from the baseline (a teammate's import). Commit the
  stamped `application.apx` after publish; `deploy` ships the tag unchanged.
  The guard refuses to overwrite newer Builder work. Export and reconcile
  before retrying. After import, publish re-exports the app and requires
  exact APEXlang file and byte equality before it advances the DEV baseline.
  A failed or ambiguous verification leaves the old baseline in place, so the
  next publish still refuses newer Builder state. Use `--force` only when the
  user explicitly directs an override after review. When publish refuses,
  follow [docs/publish-rules.md](docs/publish-rules.md), which lists every
  refusal, its meaning, and the fix.
- **Migrations:** Put each migration in
  `migrations/YYYY-MM-DD_<name>-rNNN/` with consecutive named SQL steps such as
  `001-create-table.sql`, plus `checks.json`. No developer name belongs in the
  path. Keep source immutable after a write attempt; a follow-up uses the next
  revision folder. Run `scripts/team.sh check-conflicts <folder> --env dev`
  before applying with `scripts/team.sh migrate <folder> --env dev|staging|prod`.
  Checks cover only selected local migrations and observed live state; they
  cannot see another independent repository's pending files. Staging and
  production migrations require `STAGING_SCHEMA` or `PROD_SCHEMA` in addition
  to the existing connection and expected-user settings. Receipts are written
  only after a verified apply. Multi-schema projects use
  `migrations/<SCHEMA>/…`; one migration changes one schema. See
  [docs/migration-rules.md](docs/migration-rules.md) for naming, ordering,
  recovery, and detection limits.
- **Schema comparison:** Use `scripts/team.sh compare-schema --env staging
  --object CUSTOMERS` or `--from dev --to prod --pattern 'HR_*'` to compare
  selected live objects. This reports schema drift; local receipts cannot
  reliably attribute a shape to one migration file.
- **Promotion:** Put an explicit deployment descriptor in the application
  source. Use `scripts/team.sh deploy <app-id> --env staging` or `--env prod`;
  each direct import requires the displayed `[y/N]` confirmation. Add
  `--manual` to print a DBA runbook without connecting. A deployment writes
  to the selected APEX workspace and parsing schema.
- **Other commands:** `scripts/team.sh doctor` validates `.env` and performs a
  read-only SQLcl identity check. `scripts/team.sh backup-db` refreshes the
  local table and code mirrors, and the ORDS mirror when the ORDS profile
  exists. `scripts/team.sh backup-ords` exports ORDS metadata only: it never
  enables REST, imports, changes ORDS configuration, creates metadata, grants
  privileges or commits, and it never exports OAuth clients or secrets. Read
  [docs/ords-export.md](docs/ords-export.md) before changing it.

## Optional Tooling

`graphify` (a knowledge-graph indexer exposed via ignored `graphify-out/`) is
optional; every script works without it. Its rules live in
[`.agents/rules/graphify.md`](.agents/rules/graphify.md) and
[`.agents/workflows/graphify.md`](.agents/workflows/graphify.md), and every rule
is gated on `graphify-out/graph.json` existing. Install it with
`uv tool install graphifyy --with tree-sitter-sql`, run
`python3 scripts/setup_graphify_apx.py` (rerun after every Graphify upgrade;
`--verify` checks without changing anything), then `graphify extract . --force`.
The corpus is a domain allowlist of `apps/`, `database/`, and `app_context/`.
Graphify links cross-schema references and mirrored synonyms when their targets
are present in the database mirror. The graph reflects this repository's
files, not shared Builder state.

## Project instructions

- `AGENTS.md`, `CLAUDE.md`, `README.md`, and `.agents/rules/agent-safety.md`
  belong to the template; `scripts/team.sh upgrade-template` replaces them.
  Put project-specific agent instructions in `AGENTS.project.md`, project
  rules in `.agents/rules/project.md`, and the project overview in
  `PROJECT.md`. The upgrade never overwrites those files.
- Read `AGENTS.project.md` and `.agents/rules/project.md` after this file.
  When they conflict with this file, ask the user which applies.

## Rules for coding agents

- Before reporting a defect or changing behaviour in a review or test run,
  check [docs/known-limitations.md](docs/known-limitations.md). The behaviour
  listed there is deliberate: do not report or "fix" it again unless it no
  longer matches that page.
- Before writing PL/SQL for APEX automations, workflow activities, task
  actions, or background execution chains, read
  `.agents/skills/apex-background/SKILL.md`.
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
[migrations/README.md](migrations/README.md) and
[docs/migration-rules.md](docs/migration-rules.md) for the migration contract.
