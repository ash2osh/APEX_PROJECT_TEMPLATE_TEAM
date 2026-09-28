# Application context

Store durable notes under `app_context/<numeric-app-id>/`, with one directory
per numeric APEX application. Suggested files include `purpose.md`,
`data-dependencies.md`, `subscriptions.md`, `known-issues.md`, and `recovery.md`.
Keep connection names, passwords, local defaults, logs, and sync state out of
this directory.

Application source lives under `apps/<schema>/<numeric-app-id>/` (or
`apps/<numeric-app-id>/` when no schema directory is used). Schema changes use
dated migration folders such as
`migrations/2026-09-27_create-customers-r001/`, with consecutively numbered SQL
steps, `checks.json`, and verified-only environment receipts. See
`docs/migration-rules.md` for the contract and independent-repository limits.

release.json is not read by current scripts. Notes in
application context are informational and do not enforce app migration
prerequisites or gate a publish/deploy. The conflict checker analyzes selected
local migrations against observed live catalog state; it cannot see pending
files in another independent repository or enforce an application dependency
graph. `compare-schema` compares selected live object state but does not
reliably attribute a definition to a migration file.
