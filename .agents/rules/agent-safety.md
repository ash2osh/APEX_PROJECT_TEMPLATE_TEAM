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
- For feature capsules, keep complete mutable package sources under
  `features/<slug>/<SCHEMA>/`, capture only from DEV, and review the generated
  source diff. Replace the scaffold check and commit every selected input and
  the schema-version 2 manifest before freezing. Use the same frozen capsule
  for staging and production; production requires a verified staging report.
  Treat an interrupted/unknown checkpoint as unresolved until reconciled, and
  describe only the selected feature-checkpoint and migrate-step budget, not
  whole-deployment duration. See `docs/feature-releases.md`. An agent never types
  the `RECONCILE-RUN` acknowledgment; the developer does. The same holds for the
  `RECONCILE-MIGRATION` acknowledgment of `scripts/team.sh reconcile-migration`, which
  records a migration attempt's outcome: an agent never types it.
- `-y` / `--yes` answers a staging or production `[y/N]` prompt only when the developer asked in the
  conversation for that run to be unattended. Production also needs `--confirm-target` with the name the
  developer typed for the run. An agent never sets `TEAM_ACK_FROM_STDIN`, which only the live tests use.
- Use `scripts/team.sh compare-schema` with exact names or object prefixes for
  read-only live drift reports. Receipts and resulting DDL cannot reliably
  identify one unique migration file.
- `scripts/team.sh deploy` requires confirmation for staging and production.
  `--manual` prints a runbook and makes no connection. Do not describe the
  manual output as an executed deployment.
- Do not claim a live check passed unless it actually ran. Report unavailable
  checks as unknown.
- Use `scripts/team.sh verify` for declared read-only checks and
  `scripts/team.sh query` with a named configured profile for ad-hoc shared-
  target reads. `migrate --rehearse` executes eligible DML on the real selected
  target; run it only when that target and the DML are authorized. Its scalar
  rollback checks do not prove sequence/identity counters, autonomous work,
  external effects, or DDL were undone. Keep reviewed assertions intact; never
  weaken a check to obtain a green result. The documented disposable-fixture
  qualification path remains available only for its explicitly approved
  local target and cleanup.
- For confirmed inherited-template issues, check known limitations, record a
  sanitized entry in project-owned `TODO.md`, and follow
  [`docs/template-feedback.md`](../../docs/template-feedback.md). Resolve the
  repository from the template lock and manifest, never the downstream
  `origin`; search existing upstream issues/PRs before filing. A focused PR
  needs current-task authorization for upstream-only commit/push. Queue
  ambiguous or unsafe reports locally and continue safe work. Never disclose
  credentials, project source/data, or full logs.
- Do not commit or push without an explicit instruction. Never silently push.
