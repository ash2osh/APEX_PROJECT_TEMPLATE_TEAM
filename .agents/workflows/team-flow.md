# Team workflow

This template uses one downstream Git repository per developer and a shared
development database/APEX workspace. Developer repositories do not exchange
commits. Git branches do not isolate shared Builder or database state.

1. **Builder-first:** Coordinate the shared app edit, save in Builder, run
   `scripts/team.sh export <numeric-app-id>`, review the APEXlang diff, then
   commit. Export captures Builder state; it does not import.
2. **File-first:** Edit the app's APEXlang source (commit before or after
   publishing; publish imports the files on disk). Tell teammates
   which numeric app ID will be published and check for in-progress Builder
   work. Run `scripts/team.sh publish <numeric-app-id> --env dev`; the drift
   guard refuses to overwrite Builder edits newer than the local export. After
   import, publish re-exports the app, verifies exact APEXlang file bytes, and
   advances the DEV baseline only when the live revision stayed stable.
   Publish first stamps `[DEVELOPER_NAME-YYYY-MM-DDrNNN]` onto the app
   version; the guard compares the live version to catch a teammate's
   import. Commit the stamped `application.apx` afterwards. When publish
   refuses, follow `docs/publish-rules.md`.
3. **Schema work:** Add `NNN-<step-name>.sql` files and `checks.json` under
   `migrations/YYYY-MM-DD_<name>-rNNN/` (or
   `migrations/<SCHEMA>/YYYY-MM-DD_<name>-rNNN/` when several schemas are
   configured; one migration changes one schema). Run
   `scripts/team.sh check-conflicts <folder> --env dev`, then
   `scripts/team.sh migrate <folder> --env dev` (or explicitly select staging
   or production). Preflight sees only selected local files and the live
   catalog; independent repositories' pending files are not visible. Follow
   [docs/migration-rules.md](../../docs/migration-rules.md) for immutable
   revisions and recovery.
4. **Promotion:** Configure explicit workspace/app/schema descriptors and
   use `scripts/team.sh deploy <numeric-app-id> --env staging|prod`. The
   direct route asks for interactive confirmation. `--manual` prints a DBA
   runbook without connecting. Compare selected live schema objects with
   `scripts/team.sh compare-schema --env staging --pattern 'HR_*'` (add
   `--schema <NAME>` when several schemas are configured); this reports drift
   and does not prove migration-file attribution.

Use `scripts/team.sh doctor` for the read-only connection and database
identity check. Never invent live database or team coordination evidence.
