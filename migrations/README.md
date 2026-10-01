# Migrations

Use one dated folder per migration, with one numbered SQL file per step. For a
multi-schema code profile, place it under `migrations/<SCHEMA>/`:

```text
migrations/DEMO/2026-09-27_create-customers-r001/
├── 001-create-table.sql
├── 002-create-index.sql
├── checks.json
└── status.dev.json  # created only after a verified DEV apply
```

The full folder name is `migrations/<SCHEMA>/YYYY-MM-DD_<migration-name>-rNNN/`;
it contains no developer name. When `CODE_SCHEMA` contains one schema, the
existing flat `migrations/YYYY-MM-DD_<migration-name>-rNNN/` layout remains
valid and the schema-folder layout is also accepted. With two or more code
schemas, the schema folder is required and a flat folder is refused. One
migration changes one schema. Revision numbers start at `r001` independently
for each schema and migration-name family. The creation date sorts lexically
newest first when the folder view is sorted descending, but it cannot set the
view's sort direction. Files execute by ascending sequence. Multiple selected
folders execute in the explicit command-line order. A cross-schema change such
as a grant or a synonym is two coordinated migrations, one per schema.

Run `scripts/team.sh check-conflicts <folder> --env dev` for read-only live
preflight or add `--local` for selected-batch analysis without a connection.
Apply with `scripts/team.sh migrate <folder> --env dev|staging|prod`. Checks see
the selected local files and live target state; they cannot discover pending
migrations in another independent repository, and concurrent changes can race
preflight. Each migration requires read-only `checks.json` conditions. Keep
SQL and checks immutable after any apply attempt; put a correction in the next
revision. Environment status JSON is written only after a successful apply and
fresh verification. See [the migration guide](../docs/migration-rules.md) for
the full contract, target configuration, comparison, and recovery limits.

Migration SQL runs with SQLcl substitution disabled. SQLcl client commands
such as `SET`, `PROMPT`, `HOST`, `WHENEVER`, `SPOOL`, and `CONNECT` are rejected
before SQLcl connects. A SQL statement ends at its `;`. SQLcl would also end it
at a line holding only `/` or `.`, or splice a file into it at a line starting
with `@`, so those lines are rejected inside a statement, and a line holding
only `/` is rejected inside a comment. Blank lines inside a statement are fine.
