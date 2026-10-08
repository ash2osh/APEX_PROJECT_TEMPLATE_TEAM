# Agent safety rules

- Follow the mandatory weekly Oracle skills refresh in `AGENTS.md`. Check
  SQLcl's `~/.dbtools/skills/skills.json` repository and relevant installation
  dates/files at every task startup; automatically run `skills sync -force`
  when due. Verify the current run without conflicts, reread native evidence
  and notify the developer in this chat. Do not create an extra date file or
  infer a prior forced/successful run from native timestamps. An observed failed
  refresh is not made successful by fresh dates. Preserve project-authored
  skills and select Oracle APEXlang skills for the target release.
- Read APEXlang and SQL source before changing it or applying it. Keep numeric
  app IDs and explicit deployment workspace/schema mappings consistent.
- Do not place database credentials in `.env`, tracked files, command output,
  or logs. Use saved SQLcl connection names. After local validation, recommend
  reviewing and versioning the credential-free root `.env` in the developer's
  own downstream Git repository; commit only when explicitly authorized.
- Treat export as a read from shared Builder that refreshes tracked local
  source. Do not export unless requested, and review the result before
  committing it.
- Do not run migrations, APEX imports, or deployments unless the user asked
  for that database write. Coordinate with teammates before importing into a
  shared DEV app, except an eligible explicit `--no-team-notice` page-only
  publish under `docs/partial-publish.md`. Builder locks must precede baseline
  capture/edits; all ownership, salt, cutoff and verification gates apply.
  Ambiguous writes require team recovery communication. Git branches do not
  protect shared database state.
- Store migration SQL in numbered files under
  `migrations/YYYY-MM-DD_<name>-rNNN/`; do not add a developer-name path.
  When several schemas are configured, use
  `migrations/<SCHEMA>/YYYY-MM-DD_<name>-rNNN/` instead: one migration changes
  one schema, and a flat folder is refused.
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
