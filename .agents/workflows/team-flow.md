# Team workflow

This template uses one downstream Git repository per developer and a shared
development database/APEX workspace. Developer repositories do not exchange
commits. Git branches do not isolate shared Builder or database state.

1. **Builder-first:** Coordinate the shared app edit, save in Builder, run
   `scripts/team.sh export <numeric-app-id>`, review the APEXlang diff, then
   commit. Export captures Builder state; it does not import.
2. **File-first:** Edit and commit the app's APEXlang source. Tell teammates
   which numeric app ID will be published and check for in-progress Builder
   work. Run `scripts/team.sh publish <numeric-app-id> --env dev`; the drift
   guard refuses to overwrite Builder edits newer than the local export. After
   import, publish re-exports the app, verifies exact APEXlang file bytes, and
   advances the DEV baseline only when the live revision stayed stable.
   Publish first stamps `[DEVELOPER_NAME-YYYY-MM-DDrNNN]` onto the app
   version; the guard compares the live version to catch a teammate's
   import. Commit the stamped `application.apx` afterwards. When publish
   refuses, follow `docs/publish-rules.md`.
3. **Schema work:** Add immutable SQL files under
   `migrations/<developer>/`, run `scripts/team.sh check-conflicts`, then
   apply selected files with `scripts/team.sh migrate <file>`. This changes
   the shared DEV schema.
4. **Promotion:** Configure explicit workspace/app/schema descriptors and
   use `scripts/team.sh deploy <numeric-app-id> --env staging|prod`. The
   direct route asks for interactive confirmation. `--manual` prints a DBA
   runbook without connecting.

Use `scripts/team.sh doctor` for the read-only connection and database
identity check. Never invent live database or team coordination evidence.
