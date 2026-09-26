# Per-developer migrations

Create one folder per developer under `migrations/`, then add immutable SQL
files such as `migrations/alice/20260926_101500_add_status.sql`. Use a
timestamp-prefixed, descriptive filename and do not edit a migration after it
has been applied to the shared development database.

Before applying a file, run `scripts/team.sh check-conflicts`. The checker
compares table, view, sequence, and added-column declarations across developer
folders. Apply reviewed files with `scripts/team.sh migrate <file>`.
The SQLcl driver verifies the expected session user and switches the session's
current schema to the configured `CODE_SCHEMA` before executing the file.
