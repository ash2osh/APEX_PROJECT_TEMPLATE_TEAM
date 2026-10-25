# Running as a login that acts as another schema

## When to use it

Use this mode when a SQLcl connection must authenticate as a deployment login
but the command targets another schema. `*_EXPECTED_USER` remains the actual
login, and `*_SCHEMA` remains the schema being targeted. The scripts verify
both identities and use `ALTER SESSION SET CURRENT_SCHEMA` to resolve
unqualified names in that schema. This needs neither the schema owner's
credentials nor a proxy grant; it does not make the login the schema owner or
give it extra privileges.

When the login and target schema are the same, behavior is unchanged: there is
no acts-as-schema banner and `doctor` adds no privilege report. A different
login must already have the privileges required for the requested statements.
Live catalog preflight requires either `SELECT ANY DICTIONARY` or
`SELECT_CATALOG_ROLE` to validate metadata visibility.

## Configuration

Use the existing connection, expected-user, and schema settings. The example
below authenticates as `APP_LOGIN` and targets `APP_STAGE`:

```dotenv
STAGING_SQLCL_CONNECTION=stage-db
STAGING_EXPECTED_USER=APP_LOGIN
STAGING_SCHEMA=APP_STAGE
```

For position-aligned lists, each connection, expected login, and schema keeps
the same position. For example, use `APP_LOGIN` or `STAGE_DEPLOYER` as the
expected user only when that is the login saved in the corresponding SQLcl
connection. An owner login such as `APP_DEV` can keep `*_EXPECTED_USER` equal
to `*_SCHEMA` with no change to its existing behavior. No additional `.env`
setting is needed.

## What each command does

These commands retain their identity checks. The supported commands proceed
when the login has the needed privileges and the live checks pass.

| Command | Behavior when login differs from schema |
| --- | --- |
| `migrate` | Runs after verifying the login, selecting the target schema, and passing live migration preflight. |
| `check-conflicts` | Runs a read-only live preflight. It refuses if neither `SELECT ANY DICTIONARY` nor `SELECT_CATALOG_ROLE` validates metadata visibility, with `enabled metadata-reader privileges were not validated`. |
| `export` | Runs the APEX application export using the selected parsing-schema profile and its identity checks. |
| `publish` | Runs the APEX import after its identity and drift checks. The banner identifies the login and schema; staging and production still require the `[y/N]` confirmation. |
| `compare-env` | Runs a read-only environment comparison when catalog visibility is validated for the selected profiles. |
| `backup-db` | Runs the table and code mirror scopes when their access checks pass. If the optional ORDS profile is configured, that scope still requires its login to equal the REST schema owner. |
| `dev-test` | Refuses because fixture execution requires `SESSION_USER` to equal the selected schema (`session_user APP_LOGIN does not match selected schema APP_STAGE; first-release execution requires session_user=selected_schema`). Use a connection that logs in as the selected schema owner; see [dev-test.md](dev-test.md#limits-and-safe-use). |
| `backup-ords` | Refuses because ORDS export authenticates the actual session user; `CURRENT_SCHEMA` does not substitute for the REST schema owner. Use a connection that logs in as that owner, with `ORDS_EXPECTED_USER` equal to `ORDS_SCHEMA`; see [ords-export.md](ords-export.md). |

## Privileges by statement

When the login issues a statement directly against another schema, Oracle
checks the login's privileges. These are the cross-schema privileges used by
the corresponding operations:

| Statement | Required privilege |
| --- | --- |
| `CREATE OR REPLACE PACKAGE` | `CREATE ANY PROCEDURE` |
| `ALTER … COMPILE` | `ALTER ANY PROCEDURE` |
| `CREATE SYNONYM` | `CREATE ANY SYNONYM` |
| `CREATE TABLE` | `CREATE ANY TABLE` |
| `GRANT` on owner objects | `GRANT ANY OBJECT PRIVILEGE` |
| APEX import | `APEX_ADMINISTRATOR_ROLE` (expected for importing into a schema other than the login's own; the template has not verified this against every APEX release, so confirm it with a first publish to a disposable application) |

## What it does not prove

A statement run directly in the session uses the login's privileges. Stored
code compiled in the target schema still resolves privileges as that schema's
owner under definer rights, so compilation still catches a missing owner grant
for code it needs. A trial in this mode does not prove that the owner itself
has the grants needed for statements run directly in the session. Rehearse in
an environment with the owner login when owner-grant fidelity for those
statements matters.

## Writing checks

Check the active target schema with
`SYS_CONTEXT('USERENV','CURRENT_SCHEMA')`. Do not use `SESSION_USER` to check
the target: it remains the authenticated login, such as `APP_LOGIN`, so a
project-written `SESSION_USER` check can return `0` in this mode. The generated
`*-owner-session` check already uses `CURRENT_SCHEMA`, and `new-migration`
continues to generate that check.

## Reading the doctor output

When the login differs from the target, command summaries and publish prompts
show this banner verbatim:

```text
Acting as schema APP_STAGE through ALTER SESSION SET CURRENT_SCHEMA; login is APP_LOGIN.
```

For each target where login and schema differ, `doctor` appends `YES` or `NO`
to these privilege and role labels and emits a mode line naming that target:

```text
SESSION_PRIVILEGE:SELECT ANY DICTIONARY=
SESSION_PRIVILEGE:CREATE ANY PROCEDURE=
SESSION_PRIVILEGE:ALTER ANY PROCEDURE=
SESSION_PRIVILEGE:CREATE ANY TABLE=
SESSION_PRIVILEGE:ALTER ANY TABLE=
SESSION_PRIVILEGE:CREATE ANY INDEX=
SESSION_PRIVILEGE:CREATE ANY TRIGGER=
SESSION_PRIVILEGE:CREATE ANY SEQUENCE=
SESSION_PRIVILEGE:CREATE ANY VIEW=
SESSION_PRIVILEGE:CREATE ANY SYNONYM=
SESSION_PRIVILEGE:GRANT ANY OBJECT PRIVILEGE=
SESSION_ROLE:APEX_ADMINISTRATOR_ROLE=
SESSION_MODE:ACTS_AS_SCHEMA APP_STAGE LOGIN APP_LOGIN
```

The mode line is specific to the checked schema and login; read it alongside
that profile's identity result when the configuration uses aligned lists.
`doctor` does not report `SELECT_CATALOG_ROLE` separately, so a `NO` for
`SELECT ANY DICTIONARY` alone does not mean the metadata-reader check will fail:
`SELECT_CATALOG_ROLE` is also accepted. The live preflight is authoritative
and refuses with `enabled metadata-reader privileges were not validated` when
neither path is validated. When login equals schema, `doctor` leaves its
existing output unchanged and emits no extra report.
