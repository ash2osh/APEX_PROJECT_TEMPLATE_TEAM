# Agent safety rules

- Read APEXlang and SQL source before changing it or applying it. Keep numeric
  app IDs and explicit deployment workspace/schema mappings consistent.
- Do not place database credentials in `.env`, tracked files, command output,
  or logs. Use saved SQLcl connection names.
- Treat export as a read from shared Builder that refreshes tracked local
  source. Do not export unless requested, and review the result before
  committing it.
- Do not run migrations, APEX imports, or deployments unless the user asked
  for that database write. Coordinate with teammates before importing into a
  shared DEV app; Git branches do not protect shared database state.
- Store migration SQL in numbered files under
  `migrations/YYYY-MM-DD_<name>-rNNN/`; do not add a developer-name path.
  Require `checks.json`, preflight selected folders, and apply only with an
  explicit `--env`. Independent repositories' pending files cannot be seen by
  the conflict checker. Treat a write attempt without a verified receipt as
  requiring live reconciliation before retry. See
  `docs/migration-rules.md`.
- Use `scripts/team.sh compare-schema` with exact names or object prefixes for
  read-only live drift reports. Receipts and resulting DDL cannot reliably
  identify one unique migration file.
- `scripts/team.sh deploy` requires confirmation for staging and production.
  `--manual` prints a runbook and makes no connection. Do not describe the
  manual output as an executed deployment.
- Do not claim a live check passed unless it actually ran. Report unavailable
  checks as unknown.
- Do not commit or push without an explicit instruction. Never silently push.
