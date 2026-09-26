# Application context

Store durable notes under `app_context/<numeric-app-id>/`, with one directory
per numeric APEX application. Suggested files include `purpose.md`,
`data-dependencies.md`, `subscriptions.md`, `known-issues.md`, and `recovery.md`.
Keep connection names, passwords, local defaults, logs, and sync state out of
this directory.

Application source lives under `apps/<schema>/<numeric-app-id>/` (or
`apps/<numeric-app-id>/` when no schema directory is used). Schema changes are
authored as immutable files under `migrations/<developer>/`.

release.json is not read by current scripts. Notes in
application context are informational and do not enforce app migration
prerequisites or gate a publish/deploy. The conflict checker detects common
duplicate DDL declarations in the migration files it can see; it does not
enforce an application dependency graph.
