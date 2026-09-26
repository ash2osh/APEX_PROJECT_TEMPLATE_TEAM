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
- Use `scripts/team.sh check-conflicts` before applying a migration. Applied
  SQL changes shared DEV state and migration files remain immutable.
- `scripts/team.sh deploy` requires confirmation for staging and production.
  `--manual` prints a runbook and makes no connection. Do not describe the
  manual output as an executed deployment.
- Do not claim a live check passed unless it actually ran. Report unavailable
  checks as unknown.
- Do not commit or push without an explicit instruction. Never silently push.
