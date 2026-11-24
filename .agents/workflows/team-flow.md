---
name: team-flow
description: Coordinate APEX application edits, guarded publishing, lean release bundles and promotion in a shared team workspace
---

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
3. **Database & release bundles:** Create or select a bundle under
   `releases/<slug>/` with `scripts/team.sh release list` and
   `scripts/team.sh release new <slug>`. Export DEV stored units via
   `scripts/team.sh release collect <bundle> --from dev --schema <SCHEMA> --object <TYPE:NAME> --mode <new|replace|patch>`
   and add ordered DDL/ORDS/verify scripts under `01-ddl/`, `03-ords/`,
   `04-verify/`. Run `scripts/team.sh release check <bundle> --env dev` before
   running `scripts/team.sh release run <bundle> --env dev --confirm-target <db_unique_name>`.
   See [docs/release-workflow.md](../../docs/release-workflow.md).
4. **Promotion:** Configure explicit workspace/app/schema descriptors and
   include app IDs in `release.json` `apps` (for automated Step 5 deployment) or
   use `scripts/team.sh deploy <numeric-app-id> --env staging|prod`. Direct
   `deploy` asks for interactive confirmation unless `--yes` / `--confirm-target`
   is authorized; `--manual` prints a DBA runbook without connecting. Compare
   selected live schema objects with `scripts/team.sh compare-schema --env staging --pattern 'HR_*'`
   (add `--schema <NAME>` when several schemas are configured).

When the optional ORDS profile is configured, `scripts/team.sh backup-ords`
exports a schema's ORDS (REST) definition read-only to
`database/<SCHEMA>/ords/schema.sql`, and `backup-db` refreshes it with the table
and code mirrors. It never changes ORDS; see `docs/ords-export.md`. Review and
commit the generated file like any mirror.

Use `scripts/team.sh doctor` for the read-only connection and database
identity check. Never invent live database or team coordination evidence.
