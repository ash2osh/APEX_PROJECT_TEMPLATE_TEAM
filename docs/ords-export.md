# ORDS metadata export

`scripts/team.sh backup-ords` writes a read-only copy of a schema's ORDS (REST)
definition to `database/<SCHEMA>/ords/schema.sql`, so that REST modules,
privileges and roles are reviewed and committed like the rest of the database
copy. It exports; it never changes anything. This page is the reference for what
it exports, what it refuses, how it checks its own result, and what has not
been verified.

## Turn it on

ORDS is an optional, independent profile in `.env`. All three keys or none:

```dotenv
ORDS_SCHEMA=REST_API
ORDS_SQLCL_CONNECTION=dev-rest
ORDS_EXPECTED_USER=REST_API
```

- Leave all three out and ORDS is disabled: nothing else in the template
  changes, and existing projects need no edit.
- One or two of the three, an empty value, an invalid name, lists of different
  lengths, or an expected user that differs from its schema is refused when
  `.env` is loaded.
- Like the other profiles, each key accepts a position-aligned comma list for
  several schemas (`ORDS_SCHEMA=REST_ONE,REST_TWO`). A schema may appear once.
- The ORDS schema need not appear in the tables, code or APEX profiles. An
  ORDS-only schema is selectable with `--schema`.
- **The saved connection must log in as the REST schema owner.** Unlike the
  other profiles, `ORDS_EXPECTED_USER` must equal `ORDS_SCHEMA`, and the
  database session user must equal both. ORDS authorizes the actual login
  user, so `ALTER SESSION SET CURRENT_SCHEMA` (which does not change the
  session user) is not a substitute, and a deployment account is refused.
  Never enable REST for a deployment account to get around this.

## Commands

| Command | What it does |
| --- | --- |
| `scripts/team.sh backup-ords` | Exports every configured ORDS schema. Only `database/<SCHEMA>/ords/` is replaced; the table and code mirrors are not touched. |
| `scripts/team.sh backup-ords --schema <NAME>` | One ORDS schema. Refused when the ORDS profile does not list it. |
| `scripts/team.sh backup-db` | Without an ORDS profile, unchanged. With one, refreshes the table, code and ORDS mirrors together; nothing is installed until every selected export verifies. |
| `scripts/team.sh doctor` | Also checks each ORDS target: session user equals the schema, ORDS is installed and exposes the export API, and SQLcl is new enough. |

`scripts/team.ps1` takes the same commands and options. The ORDS export is
refused for a production-looking connection unless `DB_ENVIRONMENT=production`,
and under `production` it only reads, like every other command here.

## Requirements

| Requirement | Detail |
| --- | --- |
| SQLcl | **26.1 or newer** (also the template's baseline). Checked from `sql -V` before any export session opens; an older release is refused with `SQLcl <release> is not supported for the ORDS export: SQLcl 26.1 or newer is required`. The export uses SQLcl's `REST export schema` command. |
| ORDS in the database | Installed, with `ORDS_METADATA.ORDS_EXPORT.EXPORT_SCHEMA` available to the schema owner. Oracle documents the schema export API (`ORDS_EXPORT_ADMIN`, and `ORDS_EXPORT` compatible with it) from **ORDS 25.1**. The template checks that the function exists rather than a version number, and SQLcl's own `Failed to execute the ORDS export function` message is reported as an unsupported ORDS release. |
| Python | 3.10 or newer, as for the other helpers. Needed only when an ORDS profile is configured. |

What was inspected: the REST extension bundled with SQLcl 26.2.2.0, whose
`REST export schema` calls `ORDS_METADATA.ORDS_EXPORT.EXPORT_SCHEMA` with
`p_include_enable_schema`, `p_include_privs` and `p_include_oauth` all `TRUE`.
Earlier SQLcl 26.1 releases were not available to inspect.

## What is exported

One script, `database/<SCHEMA>/ords/schema.sql`: the output of SQLcl's
`REST export schema` for the connected schema. As ORDS generates it:

- the schema's REST enablement (`ORDS.ENABLE_SCHEMA`);
- modules, templates, handlers (including handler source) and parameters;
- roles and privileges, with each privilege's role, module and URL pattern
  mappings;
- AutoREST-enabled objects, when the ORDS release emits them.

Whatever else `ORDS_EXPORT.EXPORT_SCHEMA` emits by default (for example JWT
profile definitions) is kept as exported, but is not independently verified.

The file is the generator's output with two changes: LF line endings, and the
export timestamp removed from the `-- Schema: <NAME>  Date: ...` header line, so
that an export of unchanged metadata does not show up as a Git change.

A schema that is not REST enabled and defines no ORDS metadata is a **verified
empty** result. SQLcl cannot export it, so none is requested; `schema.sql`
holds two comment lines saying so. A REST-enabled schema with no modules,
privileges or roles exports normally and is reported as verified empty too.

## What is never exported

- **OAuth clients**, and with them client secrets, tokens and credentials.
  SQLcl's `REST export schema` always includes OAuth client definitions, which
  this template cannot switch off, so a schema that owns an OAuth client is
  **refused** (`owns N ORDS OAuth client(s)`). Nothing is exported and nothing
  is changed. A scan of the finished export refuses any OAuth client or secret
  call as well. Words such as `token` inside handler source are not treated as
  secrets.
- Application data: table rows, sequences' values, anything in your tables.
- ORDS server configuration: pools, wallets, `settings.xml`, ORDS-level
  security, other schemas' metadata.
- Anything outside the configured schema; there is no cross-schema export.
- ORDS is never modified: no REST enablement, no import of the generated SQL, no
  configuration change, no created metadata, no grants, no `COMMIT`. The SQL
  only reads dictionary views and runs SQLcl's export.

## How the result is trusted

SQLcl exits 0 and leaves a non-empty file after many failures, so neither counts
as proof. Per schema, `scripts/ords_export.py` requires all of:

1. SQL-side: the session user equals `ORDS_SCHEMA` and `ORDS_EXPECTED_USER`,
   the schema is visible, ORDS is installed and exports the schema.
2. The transcript shows the same identity, the access-check marker and the
   completion marker, each exactly once, and no SQLcl or Oracle error.
3. An **inventory** read from the ORDS dictionary views (`USER_ORDS_*`) before
   the export and again after it: schema rows, REST enablement, modules,
   templates, handlers, parameters, roles, privileges, the three privilege
   mapping kinds, AutoREST objects and OAuth clients. An unreadable view stops
   the run; a missing inventory is never an empty schema. The two inventories
   must be equal.
4. The export starts with the generator header, names the right schema, and ends
   with `COMMIT;` and `END;`, with the `EXCEPTION ... ROLLBACK; RAISE;` handler
   ORDS 26.x writes in between (a truncated file fails here).
5. The number of `DEFINE_MODULE`, `DEFINE_TEMPLATE`, `DEFINE_HANDLER`,
   `DEFINE_PARAMETER` and `ENABLE_OBJECT` calls equals the dictionary count of
   the same entity. `CREATE_ROLE` and `DEFINE_PRIVILEGE` may be fewer than the
   dictionary lists (the views also list the roles and privileges that ship
   with ORDS, which ORDS does not export) but never more. Calls are counted in
   the *code* of the script: comments and the contents of string literals (a
   handler's stored source, `q'[...]'` quoting included) are ignored, so stored
   text can neither inflate a count nor stand in for a missing definition. The
   OAuth scan, the script-ending check and the scan of the SQLcl transcript read
   the same way, and an export echoed to the screen is removed from the
   transcript up to its real `COMMIT;` `END;`, not one inside handler source.
6. A **second export**, taken after the first, is identical to it. Together with
   step 3 this catches metadata that changed while the export ran.

Only then is `database/<SCHEMA>/ords/` replaced, through the same mechanism as
the other mirrors: a dirty mirror is refused before SQLcl starts, the old
mirror is moved aside and restored on any failure or interruption, and with
several mirrors in one run all of them are installed or none.

## What changes on disk

| Run | Replaced | Untouched |
| --- | --- | --- |
| `backup-ords` | `database/<SCHEMA>/ords/` for each exported schema | every other file in `database/` |
| `backup-db`, schema has tables or code scopes and ORDS is selected | `database/<SCHEMA>/` as a whole, including `ords/` | other schemas |
| `backup-db`, schema has tables or code scopes, ORDS not selected (no profile, or `--schema` outside it) | `database/<SCHEMA>/` as a whole, **except** its existing `ords/`, which is carried over unchanged | other schemas |
| `backup-db`, schema only in the ORDS profile | `database/<SCHEMA>/ords/` | that schema's other mirrors |

A dirty `database/<SCHEMA>/ords/` (uncommitted or untracked changes) blocks both
commands before SQLcl starts. A dirty table or code mirror does not block
`backup-ords`.

## Messages

| You see | What it means | What to do |
| --- | --- | --- |
| `backup-ords error: ORDS is not configured` | No `ORDS_*` keys in `.env`. | Add the three keys (see above). |
| `backup-ords error: the ORDS profile does not list schema <X>` | `--schema` names a schema outside the ORDS profile. | Use a listed schema, or add it to the profile. |
| `ORDS_SCHEMA, ORDS_SQLCL_CONNECTION and ORDS_EXPECTED_USER must be configured together` | One or two of the three keys. | Set all three, or remove all three. |
| `ORDS_EXPECTED_USER must equal ORDS_SCHEMA entry for entry` | The expected user is not the REST schema. The session user must equal both, so such a profile can never succeed. | Use the REST schema owner as the expected user, with a saved connection that logs in as it. |
| `SQLcl <release> is not supported for the ORDS export: SQLcl 26.1 or newer is required` | SQLcl is too old. | Install SQLcl 26.1 or newer. |
| `ORA-20061: ORDS export must authenticate as the REST schema owner` / `the session user was <X>, not the REST schema owner` | The saved connection logs in as another user. | Save a connection that logs in as the REST schema owner. |
| `ORA-20062: ORDS is not installed in this database, or its metadata is not visible` | No ORDS, or the owner cannot read `USER_ORDS_*`. | Check the database and the schema owner's access. |
| `ORA-20063` / `unsupported ORDS release` | ORDS lacks the export function SQLcl calls. | Upgrade ORDS in the database (documented from 25.1). |
| `owns <N> ORDS OAuth client(s)` / `contains OAuth client or secret material` | The schema has OAuth clients, which the export would include. | Not supported: this template exports no OAuth client. Nothing was exported. |
| `the ORDS metadata of <X> changed while it was exported` | The inventories or the two exports differ. | Run it again when nobody is editing REST services. |
| `the export is incomplete or inconsistent: the dictionary lists <N> <entity> but the export has <M> <CALL> call(s)` | The export does not hold what the dictionary lists. | Run it again. If it repeats, the ORDS release may emit this entity differently; see below. |
| `the export is truncated`, `export file is missing or empty`, `does not start with the generator header` | SQLcl produced no usable script. | Read the SQLcl output above the message. |
| `is not REST enabled yet the dictionary lists ORDS metadata` | The inventory contradicts itself. | Nothing was installed; check the schema with `USER_ORDS_SCHEMAS`. |
| `refusing to back up over dirty mirror: database/<SCHEMA>/ords` | Uncommitted changes in the ORDS mirror. | Commit, stash or discard them. |

## Limitations and what has not been verified

- **Verified against a live database once**, on 2026-10-04: SQLcl 26.2.2.0
  against a development database running ORDS 26.2.3 (a REST schema with 3
  modules, 58 templates, 61 handlers and 1 parameter). Checked there: `doctor`,
  `backup-ords`, a second export that changed nothing, a login as the wrong
  user (refused, mirror untouched) and `backup-db` refreshing tables, code and
  ORDS together. That run corrected two assumptions taken from Oracle's
  published examples: the script ends with an `EXCEPTION ... ROLLBACK; RAISE;`
  handler before `END;`, and the dictionary lists ORDS's built-in roles and
  privileges, which are not exported.
- **Not verified live:** a schema that is not REST enabled, a REST-enabled
  schema with no metadata (both only covered by the scripted fake SQLcl),
  AutoREST objects, the OAuth refusal, other ORDS releases (an older ORDS may
  word its output differently), and Windows. The call-count rules are one table
  in `scripts/ords_export.py` (`CALL_RULES`); a mismatch always fails closed
  with the entity and both counts and nothing is installed.
- Privilege-to-role, module and pattern mappings are compared between the two
  inventories and the two exports, not counted call by call.
- Two exports double the export time and read the ORDS metadata twice. They
  detect a change during the export; they are not a lock.
- A schema with an OAuth client cannot be exported at all, until SQLcl offers a
  way to leave OAuth clients out.
- The export covers one connected schema. A DBA exporting several schemas with
  `ORDS_EXPORT_ADMIN` and `-schema-name` is a different workflow and is not
  supported here.
