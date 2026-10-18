# APEX project template for teams

This branch targets APEX 26.2 applications authored as APEXlang and requires
SQLcl 26.3.0.0 or newer for APEX operations. APEX 26.1 projects use the pinned
`codex/apex-26.1` maintenance branch. It uses
plain Git source, explicit deployment JSON, and dated, ordered SQL migration
folders. No custom team metadata schema or database lock tables are required.

Projects created from this template use one Git repository per developer.
Those repositories do not share commits or a remote; developers coordinate
through the shared development database and APEX Builder workspace. Branches
separate file changes only and do not isolate a shared APEX application.

## Start here

| I want to… | Read |
| --- | --- |
| set everything up for the first time | [Getting started](docs/GETTING_STARTED.md) |
| do a specific task, step by step | [Examples](docs/EXAMPLES.md) |
| understand an error message | [Troubleshooting](docs/TROUBLESHOOTING.md) |
| look up a command | [Command reference](#command-reference) |
| know exactly when publish or migrate refuses | [publish-rules](docs/publish-rules.md), [migration-rules](docs/migration-rules.md) |
| know which limits are deliberate | [Known limitations](docs/known-limitations.md) |
| see what an AI assistant can do here | [Skills and agent support](#skills-and-agent-support) |

After cloning this template into your own project, complete the
[post-clone cleanup](docs/GETTING_STARTED.md#post-clone-cleanup) via file-level
provenance review against `template-manifest.json` and `.template-lock.json`. Downstream
projects may prune inherited template-maintenance tests and CI workflows; the
original template keeps them for its own validation. Always retain
project-specific regression tests, linked project plans, durable docs, custom rules,
operational scripts, `.env`, manifest and lock.

## What you get

| Feature | What it does |
| --- | --- |
| APEX apps as text in Git | `export` copies a live app into APEXlang files you can review, diff and commit. |
| Two ways to work | **Builder-first** (change it in APEX, export it) or **file-first** (edit the files, publish them). |
| Safe publish | Refuses newer Builder work, restores only automations that were active before import, then verifies normalized source. |
| SQL migrations | Dated folders of numbered SQL plus `checks.json`; a live check for conflicts before applying; a verified receipt after. |
| DEV test runner | Read-only planning and explicit schema-owner fixture runs with durable reports and retained results; see [DEV test runner](docs/dev-test.md). |
| Schema comparison | Read-only comparison of selected tables, views and code between DEV, staging and production. |
| Environment baselines | Read-only source, grant and allow-listed reference-row exports plus reviewed structure, grant, source and optional reference-data migrations from `baseline.json`. |
| Several schemas per workspace | Comma-separated lists in `.env` and `--schema`; apps, database copies and migrations are kept per schema. |
| Staging and production | Per-app descriptors, an explicit `[y/N]` confirmation, and `--manual` to print a runbook for a DBA. |
| Read-only database copy | `backup-db` mirrors tables, views, packages, procedures, functions, triggers and synonyms (structure only, never data). `backup-ords` adds an optional, read-only export of a schema's ORDS (REST) definition. |
| Safety built in | Every connection is identity-checked, production-looking names are caught, production is read-only, file installs are all-or-nothing, and your uncommitted work is never overwritten. |
| Template upgrades | `upgrade-template` updates the template's own files and never touches your apps, migrations, database copy or `.env`. |
| Knowledge graph | Optional Graphify index that links each app's pages, regions and processes to the tables and packages they use. |
| AI-assistant support | Repository rules, 11 skills plus optional workflow packs, SQLcl MCP guidance and a Chrome DevTools daemon for assistants such as Claude Code and Codex. |
| Bash and PowerShell | `scripts/team.sh` and `scripts/team.ps1` expose the same commands. |

## Skills and agent support

AI coding assistants read [AGENTS.md](AGENTS.md) (Claude Code loads it through
`CLAUDE.md`) for the rules and load a skill on their own when a task matches
its description. The same 11 skills for APEX and project setup are listed in `.agents/skills/` (for agents
that follow the `AGENTS.md` convention) and `.claude/skills/` (for Claude Code).
Four Claude Code entries (`building-apex-template-components`, `chrome-devtools-mcp`,
`safeguarding-apexlang-text-messages` and `sqlcl-mcp-r0`) are short pointers to the full
`.agents/skills/` copy.
You do not have to call them by name.

At the start of every task, agents read SQLcl's own
`~/.dbtools/skills/skills.json`. They check the Oracle repository's `lastSync`
and every required `installedAt` at this agent's actual Oracle skill paths,
including the installed files. A fresh repository download alone is insufficient.
Missing, invalid, future or seven-day-old evidence triggers an automatic refresh
of all Oracle roots without connecting to a database:

```text
sql /nolog
SQL> skills sync -force
SQL> exit
```

SQLcl MCP `skills_sync` with `force=true` and all roots selected is equivalent.
The agent verifies the current run completed without conflicts, rereads the
native registry and reports the verified date/count in this chat. When current,
it reports the oldest relevant installation date without claiming a new sync.
Native timestamps do not record an earlier run's force option or overall success.
Failed/conflicted refreshes block Oracle work even if SQLcl updated some dates;
read-only investigation can continue with the failure reported.

The full contract travels in `AGENTS.md`, also loaded by `CLAUDE.md`. There is
no project timestamp file or scheduled automation. Projects sharing this user's
installed skills reuse SQLcl's native dates; inactive projects check at next task
startup. Agents preserve project-authored skills and use release-matched APEXlang.

Agents also load a conversation style at the start of each conversation: set `AGENT_STYLE` in
`.env` (`standard`, `concise`, `teaching` or `asharif-style`). Your own preferences accumulate in
`.agents/styles/personal.md`; see [.agents/styles/README.md](.agents/styles/README.md).

**APEX and database skills**

| Skill | Use it when |
| --- | --- |
| `initialize-project` | Setting up a new copy: `/init` asks a few questions, checks your tools and writes `.env`. It never handles passwords. |
| `apex-background` | Writing or debugging PL/SQL in automations, workflow activities, task actions or background chains. |
| `apex-session-context` | SQL or PL/SQL outside a page request reads task or workflow views or calls workflow APIs (SQLcl, schedulers). |
| `apex-workflow-lifecycle` | Importing or moving an app that has live workflow or task instances, or diagnosing a missing schedule. |
| `apexlang-export-debugging` | An APEXlang export fails with `ORA-01403` in `WWV_META_META_DATA`. |
| `optimizing-apex-task-inboxes` | Task inboxes are slow from repeated task API or view calls. |
| `safeguarding-apexlang-text-messages` | Localizing or repairing `textMessages` without breaking substitutions or tokens. |
| `sqlcl-mcp-r0` | An assistant operates SQLcl through MCP at restriction level 0. |
| `sqlcl-script-path-debugging` | Nested SQLcl scripts load the wrong file, or relative `SPOOL` and include paths misbehave. |
| `chrome-devtools-mcp` | Inspecting or driving your running web app in Chrome through the project's persistent DevTools daemon. |
| `building-apex-template-components` | Creating, translating or debugging an APEX template component (report type) for cards or lists: files, placeholders, region mapping, mobile and Arabic layout, optional Alpine.js, import and browser testing. |

**Optional workflow packs:** choose Superpowers, Matt Pocock's skills, both, or
neither during `/init` or later. New projects default to neither. Installations
are local and ignored; pinned commits and content hashes are recorded.

```bash
scripts/team.sh skills setup both --primary superpowers
scripts/team.sh skills status
scripts/team.sh skills setup none
```

With both installed, use one primary workflow per task. This setting does not
disable user-level skills or app plugins. See [workflow skill setup](docs/workflow-skills.md)
for all choices, Matt's follow-up setup, upgrades and recovery.

Rules and workflows that apply to everyone, human or assistant, live in
`.agents/rules/` (`agent-safety.md`, `graphify.md`) and `.agents/workflows/`
(`team-flow.md`, `graphify.md`). Put your project's own
instructions in `AGENTS.project.md`, `.agents/rules/project.md` and
`PROJECT.md`; template upgrades never overwrite those.

## Quickstart

New here? Follow [Getting started](docs/GETTING_STARTED.md): it walks through
everything below with the output you should see. You need SQLcl 26.3.0.0 or newer,
Git, Python 3.10 or newer, Bash 4.3 or newer, and an Oracle database with
APEX 26.2. To practice locally, the
[United Codes `uc-local-apex-dev`](https://github.com/United-Codes/uc-local-apex-dev)
containers give you one.

Copy the example configuration, set `DEVELOPER_NAME` to your uppercase name
(for example `ASHARIF`), adjust SQLcl saved connection names and expected
users, then check the DEV connection:

```bash
cp .env.example .env
scripts/team.sh doctor
```

The example uses `docker-demo` / `DEMO`. Table, code, and APEX settings can
share that connection. Staging and production app-deployment profiles are
optional connection/user pairs; migrations and schema comparison also require
an explicit target schema. Store credentials in SQLcl's secure connection
store, never in `.env` or Git. After validating your configuration, review
and keep the credential-free root `.env` in your own downstream Git repository.
It contains literal settings and saved connection names only; nested `.env`
files and `.env.*` variants remain ignored. The template ships `.env.example`
and does not create or commit a configured `.env` for you.

On Windows, use the PowerShell wrapper:

```powershell
Copy-Item .env.example .env
pwsh -File scripts/team.ps1 doctor
```

`powershell.exe` (Windows PowerShell 5.1) works the same way. Windows also
needs Git for Windows (the migration, comparison and deployment helpers run in
its Bash), Python 3.10 or newer, and SQLcl on `PATH`. Four things trip people
up there:

- **Script policy.** Windows PowerShell 5.1 on a client Windows starts with the
  `Restricted` policy and refuses to run any `.ps1`. Allow your own account to
  run local scripts with `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
  A copy downloaded as a ZIP carries a "from the internet" mark that
  `RemoteSigned` still blocks; clone with Git, or run
  `Get-ChildItem -Recurse -Filter *.ps1 | Unblock-File` once.
- **Python.** The Bash helpers behind `check-conflicts`, `migration-plan`, `migrate`,
  `compare-schema` and `deploy` call `python3`, which a python.org or winget
  install does not provide (the Microsoft Store `python3.exe` is a stub when
  Python came from elsewhere). `team.ps1` finds a working Python (`python3`,
  `python`, then `py -3`) and gives Git Bash a `python3` for that run, so there
  is nothing to set up. If you run `scripts/team.sh` straight from Git Bash and
  see `python3: command not found`, create one once and open a new Git Bash
  window: `mkdir -p ~/bin && printf '#!/usr/bin/env bash\nexec py -3 "$@"\n' > ~/bin/python3 && chmod +x ~/bin/python3`.
- **File encoding.** Migration SQL and `checks.json` must be UTF-8 without a
  byte-order mark and with LF line endings, and are refused otherwise. A
  deployment descriptor must be UTF-8 without a byte-order mark (a BOM is
  refused; CRLF is accepted). `Set-Content -Encoding UTF8` and `Out-File` in
  Windows PowerShell 5.1 write a BOM, UTF-16 or CRLF. Create the files in an
  editor set to UTF-8 and LF, or with
  `[System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))`.
- **Checkout path.** Prefer a folder name without `[` and `]`. PowerShell reads
  them as wildcards in many cmdlets and Git Bash does not convert such a path
  for native programs; the scripts work around both, but your own commands in
  that folder may not (`Set-Location "C:\work [1]"` fails; use `-LiteralPath`).

`team.ps1` accepts a migration folder as `migrations\2026-10-02_name-r001`,
`.\migrations\2026-10-02_name-r001\` or an absolute path inside the checkout.

## ORDS metadata (optional)

`backup-ords` keeps a schema's REST modules, templates, handlers, parameters,
roles and privileges in Git as `database/<SCHEMA>/ords/schema.sql`. It is off
until all three `ORDS_*` keys are set, so existing projects change nothing:

```dotenv
ORDS_SCHEMA=REST_API
ORDS_SQLCL_CONNECTION=dev-rest
ORDS_EXPECTED_USER=REST_API
```

```bash
scripts/team.sh backup-ords                      # every ORDS schema
scripts/team.sh backup-ords --schema REST_API    # one
scripts/team.sh backup-db                        # tables, code and ORDS together
```

The saved connection must log in **as the REST schema owner**: the session user
must equal `ORDS_SCHEMA` and `ORDS_EXPECTED_USER` (and `.env` loading refuses
two that differ). `ALTER SESSION SET
CURRENT_SCHEMA` does not count, because ORDS authorizes the actual login user.
It needs SQLcl 26.1 or newer and an ORDS release with the schema export API
(Oracle documents it from ORDS 25.1).

It only exports. It never enables REST, imports the generated SQL, changes ORDS
configuration, creates metadata, grants privileges or commits. OAuth clients,
client secrets, tokens and credentials are never exported: a schema that owns an
OAuth client is refused, because SQLcl's schema export always includes them.
Application data is not exported either.

An export is installed only after it passes checks that do not rely on SQLcl's
exit status: identity, completion, a before-and-after inventory from the ORDS
dictionary views, the call counts in the script, and a second identical export.
A verified empty schema is a valid result; an unavailable inventory is a failure.
The previous mirror is kept on any failure or interruption, and `backup-ords`
leaves the table and code mirrors alone. Details, every message and what has not
been verified: [docs/ords-export.md](docs/ords-export.md).

## Application files and deployment descriptors

Use the numeric APEX application ID as the stable source directory key. The
standard layout is:

```text
apps/DEMO/100/
├── application.apx
├── .apex/
└── deployments/
    ├── dev.json
    ├── staging.json
    └── prod.json
```

`apps/templates/deployments/` contains example JSON descriptors. Copy the
needed descriptors into each app and set its workspace name, numeric app ID,
and target parsing schema. Keep `dev.json` aligned with the configured DEV
profile. A staging descriptor is needed only when the project uses staging.

## Several schemas in one workspace

Publishes and deployments verify automation/workflow/task state as well as
canonical source. A lifecycle refusal preserves recovery evidence; source
verification alone does not mean runtime readiness. Read the
[runtime verification and recovery rules](docs/publish-rules.md#runtime-verification-after-import).
Explicit local test modes are documented in
[local qualification](docs/local-qualification.md).

The schema, SQLcl connection, and expected-user values in each profile are
comma-separated lists aligned by position. For example, this DEV setup uses
two schemas in each profile:

```dotenv
TABLES_SCHEMA=APP_ONE,APP_TWO
TABLES_SQLCL_CONNECTION=dev_app_one,dev_app_two
TABLES_EXPECTED_USER=APP_ONE,APP_TWO

CODE_SCHEMA=APP_ONE,APP_TWO
CODE_SQLCL_CONNECTION=dev_app_one,dev_app_two
CODE_EXPECTED_USER=APP_ONE,APP_TWO

APEX_PARSING_SCHEMA=APP_ONE,APP_TWO
APEX_SQLCL_CONNECTION=dev_app_one,dev_app_two
APEX_EXPECTED_USER=APP_ONE,APP_TWO
APEX_APP_ID=117,301
```

All three values in each profile must have the same number of entries, and a
schema may appear only once in a profile. A single value keeps the existing
single-schema behavior. Prefix filters apply to every schema in their profile.
When staging or production profiles are configured, a DEV schema maps to the
same name in those schema lists. One exception keeps today's behavior: a
project with a single DEV schema may name its staging or production schema
differently, and that mapping applies only to the project's one configured DEV
schema. An unlisted schema is never mapped to a target.

Pass `--schema <NAME>` to narrow a command to one schema. `doctor` and
`backup-db` check or mirror all configured schemas by default.

| Command | Multi-schema behavior |
| --- | --- |
| `doctor` | Checks every distinct `(connection, expected user, schema)` across the three profiles, and every ORDS target when that optional profile exists, with one read-only SQLcl identity check each. It reports each schema and fails if any check fails. `--schema` narrows it. |
| `export <id>` | Reads the app's parsing schema from `APEX_APPLICATIONS` using the first `APEX_SQLCL_CONNECTION` entry, requires that schema in `APEX_PARSING_SCHEMA`, then reconnects with that schema's own connection and writes `apps/<SCHEMA>/<id>/`. `team.sh export` takes one ID. `scripts/export_apps.sh` with no argument resolves each configured app ID separately and installs all exports in one all-or-nothing mirror replacement. |
| `backup-db` | Runs the tables scope for each tables schema and the code scope for each code schema, plus the ORDS scope for each ORDS schema when the optional ORDS profile exists. Each has a staging directory, a completeness check, and a dirty-mirror check. Nothing is installed until every schema verifies; one mirror replacement installs them all. `--schema` narrows the run. |
| `backup-ords` | Exports each ORDS schema (`ORDS_SCHEMA`, optional) to `database/<SCHEMA>/ords/schema.sql` and replaces only that folder. `--schema` narrows the run; an ORDS-only schema is selectable. |
| `publish <id>` | Requires the app folder, descriptor, and live app parsing schema to agree and be listed, then uses that schema's connection. Full mode keeps the app-wide drift guard; selected-page mode verifies a fresh merged snapshot. |
| `deploy <id> --env staging\|prod` | The descriptor's `parsingSchema` selects the same-named staging or production entry. Confirmation shows the schema and connection; imports restore previously active automations. |
| `migrate`, `check-conflicts` | Uses `migrations/<SCHEMA>/YYYY-MM-DD_<name>-rNNN/`; the folder selects the target entry. Scans, revision ordering, and receipts are scoped to one schema, and one invocation cannot mix schemas. |
| `compare-schema` | Requires `--schema` when more than one schema is configured and compares that schema across the selected environments. |
| `compare-env` | Requires `--schema` when more than one schema is configured and compares that schema's full catalog across the selected environments. |

When `CODE_SCHEMA` has one schema, the existing flat
`migrations/YYYY-MM-DD_<name>-rNNN/` layout remains valid and the
`migrations/<SCHEMA>/` layout is also accepted. With two or more code schemas,
the schema folder is required and a flat folder is refused. One migration
changes one schema. A cross-schema change such as a grant or synonym is two
coordinated migrations, one per schema. Private synonyms are mirrored under
`database/<SCHEMA>/synonyms/` with the code objects.

Limits:

- Position-aligned lists rely on order; the loader cannot detect values that
  were swapped consistently across a profile.
- Schemas sharing one SQLcl connection share its privileges. Identity checks
  confirm the session user, not that it can reach only one schema.
- `--local` conflict checks load no `.env`, so they cannot enforce the
  schema-folder layout rule. They still refuse a `--schema` that differs from
  the folder's schema and a batch that mixes schemas.
- `publish --force` skips the drift check, but still requires schema agreement
  between the app folder, descriptor, and live app.

## Builder-first workflow

Make and save the change in shared APEX Builder, then export that app's source:

```bash
scripts/team.sh export 100
git status --short --untracked-files=all -- apps/DEMO/100/
git diff -- apps/DEMO/100/
```

Review the APEXlang changes before committing. This route captures current
Builder state; it does not import the app back into Builder. The exporter
refuses to overwrite a dirty local source directory. It first exports into
`scratch/`, checks the SQLcl result and Builder revision, then replaces the
local source mirror while preserving its authored deployment descriptors.

## File-first APEXlang workflow

Edit the APEXlang files and review them. Committing first is optional:
publish imports the files on disk, and the shared database is the source of
truth. Before importing into shared DEV, tell teammates which app ID is being published and check for
unsaved or in-progress Builder edits. Then run:

```bash
scripts/team.sh publish 100 --env dev
```

The exporter records the app's `last_updated_on` value from Oracle before it
reads APEXlang, then checks the value again when the export completes. If the
app changed during export, it refuses to install that source. The DEV drift
guard compares the current Builder value with this database-time baseline, so
the workstation's time zone does not affect the check. If Builder is newer,
export and reconcile before importing. `--force` bypasses only the drift guard
and should be used only after the discrepancy is reviewed. Oracle stores this
revision at one-second precision; if the revision matches the database's
current second, export and publish fail closed and ask you to retry after one
second. This persisted timestamp cannot reveal unsaved Builder edits, so
coordinate with teammates before publishing. Staging and production promotion
uses the separate `deploy` command.

APEX does not stamp `last_updated_on` while importing, so an app installed or
published from APEXlang has no Builder timestamp until someone saves it in
Builder. To make each import identifiable, a DEV publish first stamps a
publish tag onto the end of the application version in `application.apx`:

```text
version: "V2 Powered By xxx [ASHARIF-2026-09-26r001]"
```

The tag is `DEVELOPER_NAME` from `.env`, the publish date, and a counter that
counts up with each publish of that date, whoever published, and restarts at
`r001` on a later date. The date never moves back, so a tag is never reused.
The export marker records the live version with the Builder timestamp, and the
drift guard refuses to publish when the live version differs from your
baseline, which is how it detects a teammate's import. Commit the stamped
`application.apx` after a successful publish. If the import fails, publish
restores the unstamped file. The tag appears wherever the theme renders
`#APP_VERSION#`, such as the Universal Theme footer, and `deploy` ships it to
staging and production unchanged.

After a successful import, `publish` restores exactly the automations recorded
as ACTIVE before import, then prints their before/after status table. Full
publish and deployment compare normalized APEXlang source, excluding local deployment
descriptors and the export marker. Normalization removes `//` comment lines,
sorts `textMessage` blocks by key and language, report `column` blocks by name,
and `displayColumn` blocks by column, and strips trailing whitespace and blank
lines. Real differences are reported for every file with its first differing
line. Cosmetic-only differences pass with one summary line. Pass `--strict-bytes`
to require raw byte equality; `--no-restore-automations` keeps the previous
post-import automation behavior.

DEV advances the drift baseline only when the imported source matches the
normalized comparison, a second re-export has the same normalized form, and the
live Builder revision stays stable across both exports. If SQLcl reports an
error, the source differs, the second export is unstable, or the revision is
ambiguous, publish fails and leaves the old baseline in place.
SQLcl can exit successfully without importing, for example when a descriptor
names an unknown workspace, so publish also requires its `Import successful.`
line.
Publish preflight also requires the selected application tree to be physically
inside the checkout and rejects symbolic links and reparse points.

The export marker includes `sourceProvenance` only when the guarded export
observed the same exact APEX release before and after reading source. It binds
that release and the database UTC observation time to the canonical source file
set used by strict comparison. `sourceFormat.apexRelease` still identifies the
26.2 format family. Local edits make provenance stale; legacy or manually
staged source remains unknown. Publish reports an observed source/target APEX
version pair when available, but the pair never relaxes byte verification.
Stored database-code exports record their independently observed database
release in `source-manifest.json`; they do not infer an APEX release.

When publish refuses, see [docs/publish-rules.md](docs/publish-rules.md) for
every rule, what each message means, and what to do.

## Schema migrations

Create one dated folder per migration. Put each SQL step in its own numbered
file and list the folder when checking or applying it:

```text
migrations/2026-09-27_create-customers-r001/
├── 001-create-table.sql
├── 002-create-index.sql
├── 003-create-view.sql
└── checks.json
```

The folder date is a creation date. Names sort newest first only when the file
browser is sorted descending; they do not control its sort setting. SQL steps
run in ascending sequence, while multiple folders run in the explicit
command-line order. `create-customers-r001` and
`2026-09-28_create-customers-r002` are separate sequential migrations; the
second contains the follow-up change. Keep a migration and its `checks.json`
immutable once an apply attempt may have started. A recovery belongs in the
next revision.

Create that follow-up from the prior folder with `revise`. It copies the SQL
files and checks into the next `-rNNN` folder, keeps their names and numbering,
adds a `Supersedes` line to the new `README.md`, and leaves the source untouched.
Use `--reason` to explain the correction; if omitted, the header says
`follow-up revision`. The command refuses when a later revision already exists
and prints the explicit migration order hint. It does not copy environment
receipts into the new revision.

Use `revise --check` to inspect local attempt and receipt evidence without
connecting. It exits 0 for unlocked, 1 for locked, and 2 when evidence is
unknown or unreadable. See the [migration rules](docs/migration-rules.md) for
the exact `apply-not-started` classification and its limits.

Use `scripts/team.sh migration-plan --format text` for a local inventory, or
pass folders in intended order. Add `--live --env dev|staging|prod` for the
guarded read-only preflight. The [migration planning guide](docs/migration-planning.md)
explains the evidence boundary, deferred preconditions, report formats and
immutable-folder rule.

Run a selected live preflight and apply with an explicit environment:

```bash
scripts/team.sh check-conflicts \
  migrations/2026-09-27_create-customers-r001 --env dev
scripts/team.sh migrate \
  migrations/2026-09-27_create-customers-r001 --env dev
```

Before applying generated data changes, rehearse ordered folders against the
configured target and write a JSON report:

```bash
scripts/team.sh migrate \
  migrations/2026-09-27_seed-customers-r001 \
  migrations/2026-09-28_assign-customers-r001 \
  --env dev --rehearse --report scratch/customer-seed-rehearsal.json
```

Rehearsal runs analyzer-approved DML files in one SQLcl transaction, evaluates
each folder's postconditions in that same transaction, then rolls back and
rechecks each folder's preconditions. SQLcl row counts are reported per
statement when printed. DDL, implicit-commit statements, and files the analyzer
cannot prove transaction-safe are listed as `not rehearsable` and are skipped.
Staging and production still require confirmation. Rehearsal creates no
migration receipt or write-attempt marker. See the [migration rules](docs/migration-rules.md)
for classification, rollback proof, report fields, and exit codes.

Fresh postcondition failures name up to 20 failed checks; add `--verbose` to
print all of them. Apply and verification session limits are configurable with
`MIGRATION_APPLY_TIMEOUT_SECONDS`, `MIGRATION_CHECK_TIMEOUT_SECONDS`, and
`MIGRATION_CHECK_BATCH_BYTES` in `.env`. Live catalog capture retries are
configurable with `MIGRATION_PREFLIGHT_INVENTORY_RETRIES` (default `3`); see
the [migration rules](docs/migration-rules.md) for defaults, batching, timeout
recovery and preflight behavior.

The offline `--local` checker analyzes selected files only. Live preflight
compares the selected batch with the target catalog, but cannot see pending
files in another developer's independent repository. Two developers can pass
before either writes; preflight runs again before apply, though a concurrent
change can still race it. See [docs/migration-rules.md](docs/migration-rules.md)
for coverage and recovery limits.

`checks.json` requires read-only preconditions and postconditions. Its original
schema version 1 stays unchanged; optional schema version 2 can add
rehearsal-only scalar metrics for before, after, and rollback values. See
[`docs/migration-rules.md`](docs/migration-rules.md#folder-and-file-names) for
the exact fields and limits.
`status.<env>.json` appears only after the committed result has been verified
through a fresh connection. It is local evidence, not a shared deployment
ledger. Migration SQL can use standalone slash terminators for PL/SQL blocks,
`CREATE TYPE`, `CREATE LIBRARY`, `CREATE JAVA`, and `CREATE MLE MODULE`.
SQLcl client commands such as `SET DEFINE`, `PROMPT`, and `WHENEVER` are
rejected before connecting.

For staging or production, configure the existing connection and expected
user plus `STAGING_SCHEMA` or `PROD_SCHEMA`. The migration schema can differ
between environments, so these settings are explicit and never inferred from
DEV. Direct staging and production applies require interactive confirmation.

Compare selected live object names or prefixes with `compare-schema`:

```bash
scripts/team.sh compare-schema --from dev --to staging \
  --object CUSTOMERS --object ORDERS
scripts/team.sh compare-schema --env prod --pattern 'HR_*' --pattern 'GL_*'
```

This reports current catalog drift for the selection. Status receipts can help
with follow-up review but cannot reliably prove which migration file caused a
shape: separate migrations may produce the same DDL, and a manual change can
mimic a migration.

Use `compare-env` for a complete, paged readiness comparison by object name:

```bash
scripts/team.sh compare-env --from dev --to staging
scripts/team.sh compare-env --from dev --to staging \
  --section tables --section columns --section constraints --section triggers
scripts/team.sh compare-env --from dev --to staging --format markdown
scripts/team.sh compare-env --from dev --to staging \
  --emit-dba-script scratch/dev-to-staging-dba.sql
```

The report groups blockers, differences, identical records and deliberate
exclusions. Differences say `missing on target`, `different`, or `only on target`.
DBA-level sections use optional `DEV_DBA_SQLCL_CONNECTION`,
`STAGING_DBA_SQLCL_CONNECTION`, and `PROD_DBA_SQLCL_CONNECTION` aliases only
for read-only catalog capture. Missing DBA access is reported as
`not compared (no DBA connection)` and exits 2. The emitted SQL is an additive
review artifact for object and direct system/role grants, network ACEs and
ORDS schema enablement; it does not run automatically. See [compare-env sections, connections and
limits](docs/compare-env.md).

### Generate an environment baseline

Copy `docs/baseline.example.json` to the project root as `baseline.json` and
set the actual schema names, object prefixes, exclusions, grant policy,
sequence mappings and reference-data table/column allow-list. Export stored
source, grants or reference rows, then compare environments and generate new
migration folders:

```bash
scripts/team.sh baseline export-source --from dev
scripts/team.sh baseline export-grants --from dev
scripts/team.sh baseline export-data --from dev
scripts/team.sh baseline build --from dev --to staging
scripts/team.sh baseline build --from dev --to staging --data
scripts/team.sh baseline build --from dev --to staging --bundle customer-status
scripts/team.sh baseline build --from dev --to staging --bundle customer-status --data
scripts/team.sh baseline filter-ords --exclude-module legacy.api --input scratch/ords/schema.sql --output scratch/ords/schema-filtered.sql
```

Source and grant exports go to `scratch/baseline/<environment>/<schema>/`;
reference rows go to `scratch/baseline/<environment>/data/<schema>/`. Reference
tables declare excluded columns, natural keys, matching labels, identity
handling and per-table row limits. Excluded passwords, tokens and other
environment-specific fields never enter the export. `build --data` writes
natural-key-guarded inserts, resolves allow-listed foreign keys by target
labels, and reports existing rows whose values differ. Existing rows are never
updated. Use `migrate <folder> --env <env> --rehearse` to roll back a rehearsal
of the generated DML; identity advancement is DDL and must be reviewed
separately. `filter-ords` validates the filtered file and can refuse a named
module that is not present; use the output file in an `ords-import` rollout
step. These commands never apply migrations or import ORDS metadata. See
[baseline generation](docs/baseline.md) for configuration, coverage and limits.

Without `--bundle`, the builder keeps the separate structure, grants and code
families; `--data` adds its own family. `--bundle <name>` combines the generated
structure, grants and code steps in that order into
`baseline-bundle-<name>`: a typical three-family output becomes 3-to-1. With
`--data`, the data migration remains separate and is ordered after the bundle,
so a typical four-family output becomes 4-to-2. The bundle name is an ASCII
lowercase migration slug of at most 64 characters. Empty categories create no
folder, and each selected target schema gets its own migration folder. Bundle
README files record ordered source filenames and SQL SHA-256 values. The bundle
is built from the same complete source and target captures; it does not combine
existing migrations or reveal pending work in other independent repositories.
Oracle DDL can commit implicitly, so a failed bundle needs target inspection and
reviewed recovery. Existing attempted or applied folders remain immutable.

## Staging and production deployments

For direct promotion, configure the target connection/user pair in `.env` and
provide a per-app descriptor. The tool prints the selected app, workspace,
and schema before asking for confirmation:

```bash
scripts/team.sh deploy 100 --env staging
scripts/team.sh deploy 100 --env prod
```

Direct staging and production imports require `Deploying to <ENV>. Proceed?
[y/N]`. To prepare a DBA handoff without opening a database connection, use:

```bash
scripts/team.sh deploy 100 --env prod --manual
```

The runbook includes the explicit deployment descriptor and SQLcl identity
check. A human DBA executes it using an approved saved connection. App
promotion uses deployment descriptors; schema migrations use the separate
`migrate --env` command and explicit target-schema settings.

For a one-window release manifest, pre-window comparison and rehearsal,
post-window checks, recovery ownership, and the release packet checklist, use
the [production deployment runbook](docs/production-deployment.md). A reviewed
manifest can run in one command:

```bash
scripts/team.sh rollout release/rollout.json --env prod --dry-run
scripts/team.sh rollout release/rollout.json --env prod
```

The dry run prints the local manifest and input hashes. The production run
shows its target and requires the manifest confirmation.

## Upgrading from the template

Projects created from this template do not share its Git history, so updates
are copied file by file. `template-manifest.json` decides which repository
files the upgrade may touch: template scripts, tests, CI, and agent guidance
are upgraded; project files such as `AGENTS.project.md`,
`.agents/rules/project.md`, `PROJECT.md`, and `TODO.md` are created once and never
overwritten; application source, database mirrors, migrations,
`app_context/<id>/`, and `.env` are never touched, even when `.env` is tracked
in Git. Put project-specific
instructions in those placeholder files, not in `AGENTS.md` or `README.md`.
The engine also writes its fixed `.template-lock.json` metadata file, which
records the installed template commit and hashes.

Commit your work, then run:

```bash
scripts/team.sh upgrade-template --dry-run
scripts/team.sh upgrade-template
git status
```

A file you customized is kept when the template did not change it. When both
changed, the upgrade keeps your file, writes the new template version beside
it as `<file>.template-new`, and exits with status 1: merge the two, delete the
`.template-new` file, and commit. The next upgrade refuses to run while any
`.template-new` file remains. The upgrade records the installed template commit
in `.template-lock.json`; commit it with the upgraded files. Use `--ref <tag>`
to install a specific template version and `--source <url>` for a fork. If the
upgrade reports that `.env` needs attention, compare it with `.env.example`.
A differing `.env.example` on the first upgrade also leaves an
`.env.example.template-new` review copy; merge it into the example before
reviewing any corresponding changes to your own `.env`.

Run `scripts/team.sh template-status` to compare template-owned files with the
local lock. It never fetches by default. For a read-only upstream comparison,
pass both `--upstream` and an explicit `--ref`; the command reports the fetched
commit and file statuses without installing or changing files. A missing lock
in a downstream project is reported as unknown, while the original template
repository does not require a downstream lock.

The lock also records the selected `templateRef` and `apexRelease`. Later runs
reuse the recorded ref when `--ref` is omitted. Keep a 26.1 project pinned with
`--ref codex/apex-26.1 --apex-release 26.1`. Moving to the 26.2 line requires
an explicit `--ref main --apex-release 26.2` after that release is merged.
Unknown or different release lines are refused before files change. This is
an offline choice: `doctor` and APEX operations still check the live release.
A legacy upgrader refuses schema-2 manifests; bootstrap the newer reviewed
`scripts/upgrade_template.py` from the desired template ref before crossing
the release boundary. Download it for review before executing it; do not pipe
remote code into a shell. `--dry-run` leaves both files and lock untouched.
Files removed after they were recorded in `.template-lock.json` remain deleted
(`KEEP-DELETED`). Keep that lock when cleaning a clone. New upstream files can
still be created; review tests and GitHub automation after future upgrades.
If the upgrade is interrupted (Ctrl-C or `kill`), it puts back every file it had
replaced and says `the project was left as it was`; run it again.

A project upgraded to a template version with multi-schema support needs no
`.env` change: one value per key is still the single-schema setup. Its next
`scripts/team.sh backup-db` also mirrors each schema's private synonyms under
`database/<SCHEMA>/synonyms/`; review and commit that new folder. The upgrade
never moves existing migration folders, and the flat
`migrations/YYYY-MM-DD_<name>-rNNN/` layout stays valid while `CODE_SCHEMA` has
one schema.

A project upgraded to a template version with ORDS support needs no `.env`
change either: ORDS stays disabled until all three `ORDS_*` keys are added. The
upgrade delivers the ORDS scripts, tests and [docs/ords-export.md](docs/ords-export.md)
as template files and never touches `database/`, so an existing
`database/<SCHEMA>/ords/` export, your `.env`, apps and migrations are kept.

Projects created before `template-manifest.json` existed do not have the
upgrade script yet. Run it once from a fresh template clone. When the target
repository has no `.template-lock.json`, the source resolves from its own
`template-manifest.json`; `--source` overrides that value, and a lock's upstream
is used when no explicit source is supplied:

```bash
git clone https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM.git /tmp/apex-template
python3 /tmp/apex-template/scripts/upgrade_template.py --project-root . --source https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM.git --ref main --apex-release 26.2
```

The example selects the 26.2 template explicitly. For a project remaining on
26.1, use `--ref codex/apex-26.1 --apex-release 26.1` instead.
That first run has no lock, so every file that differs from the template is
reported as a conflict instead of being overwritten. Keep the project clean
before running it; conflicts must be reviewed and merged manually.

The engine stages updates and restores them if a filesystem operation fails.
If it reports an incomplete rollback, keep the named recovery directory and
restore those backups before retrying the upgrade.

`APEX_WORKSPACE_USERNAME` identifies the actual Builder developer/admin account
used to own native application locks. It is independent of `DEVELOPER_NAME`
(the version author) and `APEX_EXPECTED_USER` (the database login). Email and
case-preserving usernames are supported. Set the account that exists in each
selected DEV workspace; there is no fallback to the author or schema. The
loader keeps this optional for schema/ORDS commands; the lock workflow validates
it before a lock or DEV publish. `doctor` also checks the account in the live
workspaces of the configured app IDs. If none is visible in a selected schema,
that workspace check is unavailable and doctor refuses rather than claiming it
passed. The 26.2 commands `app-lock <id> --env dev [--comment <text>]` and
`app-unlock <id> --env dev` use public application APIs. DEV publish acquires
and verifies a run-owned lock automatically; an existing lock refuses it even
with `--force`. The app must already exist. Unknown imports retain the lock,
old baseline and local recovery evidence for reconciliation. These application
locks accompany page locks in Builder and the agreed team workflow.

Use `publish <id> --file pages/<file>.apx` for an existing-page DEV update.
Repeat `--file` for more pages. A schema-2 baseline and unchanged unselected
local source are mandatory; a fresh snapshot preserves unrelated saved live
work. Successful verification synchronizes the canonical whole-app source
locally. Review that diff. Under the agreed separate-account guarded workflow,
`--no-team-notice` additionally requires exact pre-edit Builder page locks and
explicit live-matching salt/cutoff settings. See
[partial publishing](docs/partial-publish.md) for eligibility, refusals and recovery.
Full changes and promotion retain team coordination; working copies remain deferred.

## Command reference

| Command | Purpose |
| --- | --- |
| `scripts/team.sh app-lock <id> --env dev` | Acquire a native application lock as the configured workspace developer. |
| `scripts/team.sh app-unlock <id> --env dev` | Release only the configured developer's application lock after reconciliation. |
| `scripts/team.sh upgrade-apexlang <id> --env dev --mode builder\|files` | Explicitly convert clean app source to canonical 26.2; see [source upgrade](docs/apexlang-upgrade.md). |
| `scripts/team.sh doctor` | Validate `.env` and check the DEV SQLcl identity. |
| `scripts/team.sh dev-test --env dev --schema NAME --plan\|--run` | Plan or run the six built-in DEV checks, or an explicitly selected project suite; fixtures are retained by default. See [DEV test runner](docs/dev-test.md). |
| `scripts/team.sh dev-test --report <run-id> [--format text\|json\|markdown]` | Render saved evidence without connecting to a database. |
| `scripts/team.sh dev-test --cleanup <run-id>` | Explicitly clean supported recorded artifacts; project suites without a cleanup contract remain manual. |
| `scripts/team.sh query --env <dev\|staging\|prod> --connection <tables\|code\|apex\|ords\|migration> [--schema NAME] [--file PATH \| SQL]` | Run one conservative, bounded SELECT in a read-only SQLcl transaction; see [read-only ad-hoc queries](#read-only-ad-hoc-queries). |
| `scripts/team.sh export <id>` | Export one numeric app from shared DEV Builder. |
| `scripts/team.sh publish <id> --env dev [--strict-bytes] [--no-restore-automations]` | Drift-check and import one app to DEV; by default restores automations that were active before import. |
| `scripts/team.sh publish <id> --file pages/<file>.apx [--no-team-notice] [--no-restore-automations]` | Publish selected existing DEV pages, verify and synchronize canonical source. |
| `scripts/team.sh check-conflicts <folder> [...] --env <env>` | Preflight selected migrations against a live schema. |
| `scripts/team.sh check-conflicts <folder> [...] --local` | Analyze selected migrations without a connection. |
| `scripts/team.sh migration-plan [<folder> ...] [--live --env <env>] [--schema NAME] [--format text\|json\|markdown]` | Inventory local migrations or report an explicitly selected guarded live preflight; see [migration planning](docs/migration-planning.md). |
| `scripts/team.sh migrate <folder> [...] --env <env> [--verbose] [--rehearse] [--report <file>]` | Verify and apply selected migration folders, or rehearse eligible DML in one transaction and roll it back; `--report` writes rehearsal JSON. |
| `scripts/team.sh revise <folder> [--reason TEXT]` | Copy a migration to its next revision; the old folder stays unchanged. |
| `scripts/team.sh revise --check <folder>` | Report whether local receipt or attempt evidence locks a migration folder; exit 0/1/2 for unlocked/locked/unknown. |
| `scripts/team.sh verify <folder> [...] --env <env> [--phase pre\|post\|both] [--only-failed] [--format text\|json] [--jobs N]` | Evaluate selected migration checks read-only; exit 1 for false checks and 2 for errors. |
| `scripts/team.sh rollout <manifest.json> --env <env> [--from-step N] [--dry-run] [--report <file>]` | Run a frozen, ordered migration, SQL, ORDS and app deployment plan; staging and production show one manifest-wide hash confirmation. |
| `scripts/team.sh compare-schema --env <env> --object <name>` | Compare selected live schema objects read-only. |
| `scripts/team.sh compare-env --from <env> --to <env> [--section <name>] [--format text\|json\|markdown] [--emit-dba-script <file>]` | Compare complete, paged environment catalogs by object name; see [compare-env](docs/compare-env.md). |
| `scripts/team.sh baseline export-source --from <env>` | Export configured exact stored source, compiler settings and view text to scratch. |
| `scripts/team.sh baseline export-grants --from <env>` | Export configured object grants and the schema's direct system privileges. |
| `scripts/team.sh baseline export-data --from <env>` | Export allow-listed reference rows, excluding configured sensitive columns, to scratch. |
| `scripts/team.sh baseline build --to <env> [--from <env>] [--data] [--bundle <name>]` | Generate validated structure, grant, exact-source and optional reference-data migration folders; `--bundle` combines the three generated DDL/code categories, while data stays separate; see [baseline generation](docs/baseline.md). |
| `scripts/team.sh baseline filter-ords --exclude-module NAME [...] --input <file> --output <file>` | Remove complete named modules from an ORDS export for a rollout import step. |
| `scripts/team.sh backup-db` | Refresh local table and code metadata mirrors (and ORDS, when configured). |
| `scripts/team.sh backup-ords` | Export ORDS (REST) metadata read-only to `database/<SCHEMA>/ords/schema.sql`. |
| `scripts/team.sh deploy <id> --env <staging\|prod> [--manual] [--strict-bytes] [--no-restore-automations]` | Confirm a promotion or print a DBA runbook with the automation restore step. |
| `scripts/team.sh upgrade-template [--dry-run]` | Update template-owned files; never overwrites project files. |
| `scripts/team.sh verify-local [--format text\|json] [--live]` | Validate local environment, lock, apps and skills read-only. |
| `scripts/team.sh skills setup none\|superpowers\|mattpocock\|both [--primary superpowers\|mattpocock]` | Set up optional, pinned workflow packs locally. |
| `scripts/team.sh skills status [--format text\|json]` | Inspect repository-managed packs offline. |
| `scripts/team.sh template-status [--format text\|json]` | Compare template-owned files with the local lock; no network access by default. |
| `scripts/team.sh template-status --upstream --ref <ref>` | Explicitly compare local files with a branch, tag or commit from the manifest's upstream repository. |
| `scripts/team.sh automations list <id> --env <env> --schema <schema>` | Read one application's automation status in its descriptor workspace. |
| `scripts/team.sh automations check <id> --env <env> --schema <schema> --automation <static-id> --expected-status ACTIVE` | Check an exact selected automation against APEX 26.2's `ACTIVE`, `DISABLED`, or `ERROR` schedule states. |
| `scripts/team.sh automations enable|disable|run-once <id> --env <env> --schema <schema> --automation <static-id>` | Explicitly change one automation's schedule or run it once after typed target/action confirmation; no publish path invokes these operations. |

Each write accepts one exact static ID, rechecks target/app/status and native
application lock before calling the public APEX API, then reads the same app
state back. `run-once` runs synchronously and is verified only by a completed
successful APEX execution log plus an independent lifecycle read. A timeout or
missing log is `unknown`; reconcile the automation and execution log before any
repeat. Session creation/deletion may run the application's configured session
initialization and cleanup code, which the command displays before confirmation.
The command does not advance a failed publish baseline or release a recovery lock.

For an `unknown` action result, do not replay `run-once`. First inspect the exact
automation and its APEX execution log, then reconcile its state independently:

```bash
scripts/team.sh automations list "$APP_ID" --env dev --schema "$SCHEMA"
scripts/team.sh automations check "$APP_ID" --env dev --schema "$SCHEMA" \
  --automation "$AUTOMATION_STATIC_ID" --expected-status DISABLED
```

Set `APP_ID`, `SCHEMA`, and `AUTOMATION_STATIC_ID` from the verified deployment
descriptor and the selected app's exact static ID; choose the expected status
from the observed recovery decision. If the execution log or lifecycle read is
inconclusive, leave the result unknown and investigate manually. This check does
not change a publish baseline or release a recovery lock.

## Read-only ad-hoc queries

Use `query` for a one-off inspection without adding a script or migration:

```bash
scripts/team.sh query --env dev --connection tables --schema DEMO "SELECT table_name FROM user_tables ORDER BY table_name"
scripts/team.sh query --env dev --connection code --file scratch/query.sql
```

`--connection` names a configured profile, never an arbitrary SQLcl alias or
connection string. In DEV, `tables`, `code`, `apex` and `ords` use their own
aligned schema, SQLcl connection and expected-user settings. `migration` uses
the configured migration-owner triple when present, otherwise the existing DEV
code profile. In staging or production, non-migration profiles use that
environment's aligned schema/connection/expected-user triple; `migration` uses
its environment-specific migration triple when configured and otherwise that
same environment profile. ORDS is DEV-only. Missing or incomplete settings,
an unlisted schema, or a multi-schema profile without `--schema` refuses before
SQLcl starts.

The input must be one statement beginning with `SELECT` or a SELECT-producing
`WITH`. The gate handles quoted strings/identifiers and comments, permits only
a conservative set of built-ins, and refuses DML, DDL, PL/SQL, SQLcl commands,
remote links, sequence reads, package/member calls and unknown functions.
SQLcl substitution is disabled; the session changes to the configured schema,
disables commits in stored procedures, checks login/schema and observed
database/service identity before the query, starts a read-only transaction,
and exits with rollback. A production-marked live identity refuses a DEV or
staging target even when the selected saved connection is named for that
environment. Results are CSV without headings. Defaults are a
32 KiB SQL input limit, 60-second timeout, 500-row result limit and 1 MiB
captured-output limit. Crossing a row or byte cap, timing out, a SQLcl error,
or incomplete identity/framing refuses the result rather than displaying a
partial success. The result is withheld on errors.

The read-only transaction and syntax gate reduce risk; they do not provide
database privilege isolation or prove that every object selected is side-effect
free. The lexical gate cannot distinguish a bare zero-argument stored function
from a column reference. Disabling commits in stored procedures blocks their
database commits, including autonomous-transaction commits, but cannot prevent
nontransactional external effects. A view, virtual column or other database
object can hide behavior outside the query text. Use a suitably read-only
account and review the selected objects.

`rollout` validates and hashes every referenced source file before it starts,
stops on the first failed step and writes JSON and Markdown timing/evidence
reports. A production or staging run has one explicit confirmation listing the
manifest and every step/input SHA-256. Resume with `--from-step N` only after
the earlier successful step receipts under `scratch/` match this exact
manifest, environment and input set. `--dry-run` validates local inputs and
prints the hashes before execution. The confirmation includes app workspace
and schema or standalone SQL connection/user/schema details. The command
prints JSON/Markdown report paths, including on step failure; report outputs
cannot overwrite inputs, receipts or recovery evidence. See the [rollout rules and manifest
schema](docs/migration-rules.md#rollout) and [example manifest](docs/rollout-manifest.example.json).

Exit status, the same in Bash and PowerShell:

| Status | Meaning |
| --- | --- |
| 0 | Done; for `check-conflicts` and `compare-schema`, nothing found; for `compare-env`, every selected section was identical and complete; for `verify`, every requested check returned its expected value; for `revise --check`, the folder is unlocked. |
| 1 | `check-conflicts` found conflicts, `compare-schema` or complete `compare-env` found differences, or `verify` found a false check; `revise --check` found a lock; a `[y/N]` prompt was declined; `upgrade-template` left `.template-new` files to merge; or `.env` is invalid (`project environment error: ...`). |
| 2 | Refused or failed; this includes incomplete `compare-env` capture or selected DBA sections without a DBA connection. The message says why and what changed. Also `migrate` interrupted while a SQL step runs ("may be partially applied"). |
| 130 / 143 | Stopped by Ctrl-C / by SIGTERM (`kill`). |

SIGTERM is for Bash: `team.sh` hands its process to the command it runs, so a
`kill` reaches it. PowerShell 7 on Linux and macOS cannot run any cleanup on
SIGTERM, so `kill` ends `team.ps1` while the Bash helper or SQLcl it started
carries on (a migration may finish and write its receipt); its scratch folder
stays behind. Stop a PowerShell run with Ctrl-C, or signal its whole process
group (`kill -TERM -- -<pgid>`). Windows has no SIGTERM.

`scripts/team.ps1` exposes the same commands for PowerShell. Migration and
deployment helpers use Bash, such as Git Bash on Windows; `team.ps1` finds Git
Bash itself, and `TEAM_BASH` names a different `bash.exe` when it cannot.

## Optional knowledge graph

Graphify is optional; every command in this template works without it, and
agents fall back on grep. It indexes a domain-only corpus (`apps/`, `database/`,
and `app_context/`) through a repository-owned APEXlang extractor, so `.apx`
files are read as APEX architecture — containment, navigation, authorization,
database reads and writes, and PL/SQL calls — rather than as generic SQL. The
graph reflects this repository's files; export first when it must describe
current Builder state.

Install the optional, project-isolated Graphify environment into `.venv-graphify/`
using the pinned dependencies in `tools/graphify/requirements.txt`:

Optional Graphify requires Python 3.12+; the core commands still support Python
3.10+. After a Graphify upgrade, rerun project setup and read-only verification.

```bash
python3 scripts/graphify_project.py setup
python3 scripts/graphify_project.py extract
```

- `python3 scripts/graphify_project.py verify` checks the isolated installation,
  pinned versions, and corpus coverage without modifying any files.
- `python3 scripts/graphify_project.py setup` creates or updates the isolated
  `.venv-graphify/` using copy mode, installs pinned dependencies, patches the
  canonical APEXlang extractor, and clears stale extraction caches.
- `python3 scripts/graphify_project.py extract` runs extraction within the isolated
  environment.
- `python3 scripts/check_graphify_apx.py` before every build, then
  `python3 scripts/check_graphify_apx.py --graph graphify-out/graph.json`
  afterward. Both must pass: Graphify can exit zero after skipping parser
  failures. This checks APEXlang parsing and file presence, not live runtime
  validity or semantic completeness; it uses the default output directory's
  saved corpus exclusions and Git-ignore setting.

`graphify-out/` is local and gitignored; durable documentation belongs in
project-owned files (`PROJECT.md`, `docs/`, `app_context/`). See
[`.agents/workflows/graphify.md`](.agents/workflows/graphify.md).

## Agent guidance

Agents follow the same app ID, descriptor, drift, migration, and deployment
rules as developers. Review source before editing; do not run database-writing
commands unless the user asked; coordinate with the team before importing into
shared DEV. Never claim an unavailable live check passed. See [AGENTS.md](AGENTS.md)
for the full repository contract, [migrations/README.md](migrations/README.md),
and [docs/migration-rules.md](docs/migration-rules.md) for migration naming,
verification, and comparison limits. For browser runtime checks, see
[Chrome DevTools MCP](docs/CHROME_DEVTOOLS_MCP.md) and use the project browser
[skill](.agents/skills/chrome-devtools-mcp/SKILL.md). For confirmed inherited-
template issues, keep a sanitized local record in [TODO.md](TODO.md) and follow
the [upstream feedback guide](docs/template-feedback.md).


Optional migration profiles let a project mirror code from `CODE` while applying
migrations to `CUSTDATA` or `API`. Configure `MIGRATION_SCHEMA`,
`MIGRATION_SQLCL_CONNECTION` and `MIGRATION_EXPECTED_USER` together; use the
`STAGING_` and `PROD_` prefixes for those environments. Each triple accepts
position-aligned lists. `--schema API` selects that migration owner even when
it is absent from `CODE_SCHEMA`. A single DEV migration owner can map to a
renamed single staging/production owner; unlisted owners never map. If a whole
triple is absent, migration commands retain the original environment target.
Partial, empty or misaligned triples refuse before connecting. `check-conflicts`
uses the same migration identity, and `doctor` includes explicit DEV migration
identities once per connection/user/schema. Backups and schema comparison keep
their existing profiles. Flat migration folders require one configured migration
owner; otherwise use `migrations/<SCHEMA>/…` and keep each batch in one schema.
