# Getting started

This guide takes you from nothing to a working setup: a connection that
passes its health check, your first export of an APEX app, your first change,
and your first database migration. Plan on about 30 minutes, plus the time to
install anything you are missing.

Every command is shown for Bash (Linux, macOS, Git Bash on Windows). On
Windows you can run `pwsh -File scripts/team.ps1 <command>` instead of
`scripts/team.sh <command>`; the arguments are the same.

## 1. How it fits together

```text
 Your computer                          Shared by the whole team
┌────────────────────────────┐         ┌──────────────────────────────┐
│ your own Git repository    │         │ development Oracle database  │
│  apps/       APEX source   │ export  │  + APEX workspace (Builder)  │
│  database/   read-only copy│ ◄────── │                              │
│  migrations/ your SQL      │ publish │   the source of truth        │
│                            │ ──────► │                              │
└────────────────────────────┘         └──────────────────────────────┘
        the template's scripts reach the database through SQLcl
```

Four ideas are enough to start:

1. **The shared database is the truth.** Your Git repository holds a copy of
   the APEX apps (as APEXlang text files) so you can review changes, but the
   live app in the shared APEX workspace is what actually runs.
2. **One repository per developer.** Each developer has their own copy of this
   template. Repositories do not share commits, so Git does not protect the
   shared database. You coordinate with teammates in person or in chat.
3. **You always choose the direction.** `export` copies the live app *into*
   your files. `publish` copies your files *into* the live app. Nothing moves
   by itself.
4. **Passwords live in SQLcl, never in files.** This repository only stores
   the *name* of a saved SQLcl connection.

What each folder is for:

| Folder or file | What it holds | Who changes it |
| --- | --- | --- |
| `apps/<SCHEMA>/<app-id>/` | One APEX app as APEXlang (`.apx`) files | `export` writes it; you edit it and `publish` it |
| `database/<SCHEMA>/` | A read-only copy of your tables, views, code and synonyms | `backup-db` writes it; never edit by hand |
| `migrations/` | Dated SQL changes you write, one folder each | you |
| `app_context/` | Notes about each app, for AI assistants | you |
| `.env` | Which connections and schemas to use (no passwords) | you; it is not committed |
| `scripts/`, `tests/`, `docs/`, `.agents/` | The template itself | template upgrades |

## 2. What you need

Check each row. The command in the last column prints the version.

| You need | Version | Check with |
| --- | --- | --- |
| An Oracle database with **APEX 26.1 or newer** and a workspace | 26.1+ | `select version_no from apex_release;` |
| **SQLcl** | 26.1 or newer | `sql -V` |
| **Git** | any recent | `git --version` |
| **Python** | 3.10 or newer | `python3 --version` |
| **Bash** | 4.3 or newer | `bash --version` |

Notes:

- **macOS** ships Bash 3.2, which is too old. Install a current one with
  `brew install bash` and run the scripts with it.
- **Windows** needs Git for Windows (it provides Bash). PowerShell 7 or
  Windows PowerShell 5.1 can run the `team.ps1` wrapper.
- Optional: `uv` and Graphify for the [knowledge graph](../README.md#optional-knowledge-graph),
  and an AI coding assistant such as Claude Code or Codex.

## 3. No APEX database yet? Run one on your computer

If your team already has a shared development database, skip to step 4.

To practice locally, the open-source
[United Codes `uc-local-apex-dev`](https://github.com/United-Codes/uc-local-apex-dev)
project runs Oracle Database 26ai Free, APEX and ORDS in containers. It needs
Docker or Podman (with the `compose` command), SQLcl on your `PATH`, `unzip`,
and `curl` or `wget`, plus roughly 4 GB of memory, 3 CPUs and 35 GB of disk.

```bash
git clone https://github.com/United-Codes/uc-local-apex-dev.git
cd uc-local-apex-dev
./install.sh                 # takes 20 to 40 minutes
./local-26ai.sh create-user  # creates a schema and an APEX workspace; follow the prompts
```

It opens APEX at `http://localhost:8181/ords/apex` and registers a SQLcl saved
connection for each user it creates. Run `./local-26ai.sh --help` for the other
helpers, such as `upgrade-apex` (if you need 26.1 or newer) and
`unexpire-accounts` (if a login says the account is locked). Their
[documentation](https://www.united-codes.com/products/uc-local-apex-dev/docs/)
is the authority for that setup; this template only needs a schema, a
workspace, and a saved connection.

## 4. Create your project

Get your own copy of this template. Pick one:

**A. On GitHub:** open the template repository and click **Use this template**,
then clone your new repository. (A repository owner turns this button on once
under *Settings → General → Template repository*.)

**B. From the command line:**

```bash
git clone https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM.git my-apex-project
cd my-apex-project
git remote rename origin template     # keep the template as a separate remote
git remote add origin <your-new-repository-url>
git push -u origin main
```

Either way, the files `AGENTS.project.md`, `PROJECT.md` and
`.agents/rules/project.md` are yours. Put your project's own notes there,
because template upgrades replace `README.md` and `AGENTS.md` but never touch
those three.

## 5. Save your SQLcl connection

SQLcl keeps the password in its own secure store. Save one connection for your
development schema:

```bash
sql /nolog
```

```text
SQL> connect -save my-dev -savepwd DEMO@localhost:1521/freepdb1
Password? ********
SQL> exit
```

Replace `my-dev` with any name, `DEMO` with your schema, and the address with
your database's host, port and service. If you used `./local-26ai.sh
create-user` in step 3, the connection already exists. List your saved
connections with:

```bash
sql /nolog
SQL> connmgr list
```

## 6. Configure `.env`

```bash
cp .env.example .env
```

On Windows PowerShell use `Copy-Item .env.example .env`. Open `.env` and change
only what differs for you. Comment lines start with `#`; values are literal, so
do not add spaces or trailing comments.

| Setting | Meaning | Example |
| --- | --- | --- |
| `DEVELOPER_NAME` | Your name in capitals. It is stamped into the app version every time you publish, so teammates can tell whose import it was. | `ALICE` |
| `DB_ENVIRONMENT` | `development` for a shared dev database. | `development` |
| `APEX_APP_ID` | The numeric IDs of your APEX apps, separated by commas. | `100,200` |
| `*_SCHEMA` | The schema that owns your tables, your code, and your APEX apps. | `DEMO` |
| `*_SQLCL_CONNECTION` | The saved connection from step 5. | `my-dev` |
| `*_EXPECTED_USER` | The database user that connection should log in as. A safety check: the scripts stop if it is not this user. | `DEMO` |

The example uses the same schema, connection and user for tables, code and APEX,
which is right for most projects. If you work with an AI assistant, you can ask
it to run `/init` and it will ask these questions for you.

## 7. Check the connection

```bash
scripts/team.sh doctor
```

You should see:

```text
SQLcl target: session_user=DEMO, current_schema=DEMO, db_name=FREEPDB1, service=freepdb1
...
SQLcl connection and database identity checks passed.
APEX_DOCTOR_VERIFIED:DEMO
Doctor checks passed for the configured DEV connection.
```

`doctor` only reads. If it fails, [TROUBLESHOOTING.md](TROUBLESHOOTING.md) lists
each message and what to do.

## 8. Export your first app

```bash
scripts/team.sh export 100
```

Use one of your own app IDs. You should see lines like:

```text
Exporting Workspace DEMO - application 100:Employee Self Service
File apps/DEMO/employee-self-service/application.apx created
```

The folder is renamed to the app ID when the export finishes:

```text
apps/DEMO/100/
├── application.apx        the app itself
├── page-groups.apx
├── pages/                 one .apx file per page, e.g. p00001-dashboard.apx
├── shared-components/     authorizations, lists, LOVs, and so on
├── workspace-components/
├── deployments/           environment settings (next step; starts with default.json)
└── apex-team-export.json  records when the export was taken (used by publish)
```

Commit the result:

```bash
git add apps/
git commit -m "Export app 100"
```

### Tell the template where the app deploys

Publishing needs a small descriptor that names the workspace, the app ID, and
the parsing schema. Copy the example and edit the three values:

```bash
cp apps/templates/deployments/dev.json apps/DEMO/100/deployments/dev.json
```

```json
{
  "workspace": { "name": "DEMO" },
  "app": {
    "id": 100,
    "databaseSession": { "parsingSchema": "DEMO" }
  }
}
```

The same folder can hold `staging.json` and `prod.json` later.

## 9. Make a change

There are two ways to change an app. Use whichever suits the task.

### A. Builder first

1. Tell your team you are editing app 100 in Builder.
2. Make and save the change in APEX Builder.
3. Bring it into Git:

```bash
scripts/team.sh export 100
git diff apps/DEMO/100/
git add apps/ && git commit -m "Change the dashboard title"
```

### B. Files first

1. Edit a file. For example, in `apps/DEMO/100/pages/p00001-dashboard.apx`
   change `title: Employee Self Service` to `title: Employee Self Service Portal`.
2. **Tell your team which app you are publishing**, and check that nobody has
   unsaved work in Builder on it. Git cannot see Builder.
3. Publish:

```bash
scripts/team.sh publish 100 --env dev
```

Publish refuses, with an explanation, if the live app changed since your last
export. That is the safety net: run `export`, review, and try again. On success
it stamps your name and the date onto the app version, imports, re-exports the
app to prove it matches your files, and ends with:

```text
Published APEX App 100 to DEV (DEMO / DEMO).
Commit the stamped version in apps/DEMO/100/application.apx: Release 1.0 [ALICE-2026-09-30r001]
```

Commit that change too:

```bash
git add apps/ && git commit -m "Publish app 100"
```

## 10. Make your first database change

Database changes are dated folders of numbered SQL files, plus a `checks.json`
that proves the change is safe and that it worked.

```text
migrations/2026-09-30_add-notes-to-hr-users-r001/
├── 001-add-notes-column.sql
└── checks.json
```

`001-add-notes-column.sql`:

```sql
ALTER TABLE HR_USERS ADD (NOTES VARCHAR2(200));
```

`checks.json` (each check is a query that must return exactly one row containing
the number `1`; `expected` is always `1`):

```json
{
  "schemaVersion": 1,
  "preconditions": [
    {
      "id": "notes-column-absent",
      "sql": "SELECT CASE WHEN COUNT(*) = 0 THEN 1 ELSE 0 END FROM user_tab_columns WHERE table_name = 'HR_USERS' AND column_name = 'NOTES'",
      "expected": 1
    }
  ],
  "postconditions": [
    {
      "id": "notes-column-present",
      "sql": "SELECT CASE WHEN COUNT(*) = 1 THEN 1 ELSE 0 END FROM user_tab_columns WHERE table_name = 'HR_USERS' AND column_name = 'NOTES'",
      "expected": 1
    }
  ]
}
```

*Preconditions* must be true before the change runs; *postconditions* must be
true afterwards. Check first, then apply:

```bash
scripts/team.sh check-conflicts migrations/2026-09-30_add-notes-to-hr-users-r001 --env dev
scripts/team.sh migrate        migrations/2026-09-30_add-notes-to-hr-users-r001 --env dev
```

`check-conflicts` reads only. `migrate` runs the SQL, then confirms the result
through a fresh connection and writes `status.dev.json` next to your files as
proof. Once you have tried to apply a migration, never edit it: add the next
revision folder instead (`...-r002`). Commit the folder.

## 11. Everyday cheat sheet

| I want to… | Run |
| --- | --- |
| check my setup | `scripts/team.sh doctor` |
| copy a live app into Git | `scripts/team.sh export <app-id>` |
| push my edited files to the live app | `scripts/team.sh publish <app-id> --env dev` |
| refresh the read-only database copy | `scripts/team.sh backup-db` |
| test a SQL change without a database | `scripts/team.sh check-conflicts <folder> --local` |
| test a SQL change against the database | `scripts/team.sh check-conflicts <folder> --env dev` |
| apply a SQL change | `scripts/team.sh migrate <folder> --env dev` |
| see how two environments differ | `scripts/team.sh compare-schema --from dev --to staging --object <NAME>` |
| release to staging or production | `scripts/team.sh deploy <app-id> --env staging` |
| get the latest template scripts | `scripts/team.sh upgrade-template --dry-run` |

## Next steps

- [EXAMPLES.md](EXAMPLES.md): step-by-step recipes for common tasks, including
  several schemas, releases, and working with an AI assistant.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md): what each error means and how to fix it.
- [../README.md](../README.md): the full feature list, the skills list, and the
  command reference.
- [publish-rules.md](publish-rules.md) and [migration-rules.md](migration-rules.md):
  every rule in detail.
