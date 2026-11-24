# Application context

Store durable notes under `app_context/<numeric-app-id>/`, with one directory
per numeric APEX application. Suggested files include `purpose.md`,
`data-dependencies.md`, `subscriptions.md`, `known-issues.md`, and `recovery.md`.
Keep connection names, passwords, local defaults, logs, and sync state out of
this directory.

Application source lives under `apps/<schema>/<numeric-app-id>/` (or
`apps/<numeric-app-id>/` when no schema directory is used). Schema and code
changes live in release bundles under `releases/<slug>/` with `release.json`,
`01-ddl/`, `02-code/`, `03-ords/`, and `04-verify/`. See
`docs/release-workflow.md`.

Notes in `app_context/` are informational and do not enforce release
prerequisites or gate a publish/deploy. `release check` analyzes the selected
bundle against the live target environment; `compare-schema` compares selected
live object state across environments.
