# APEX project template for teams

This branch targets APEX 26.2 applications authored as APEXlang and requires
SQLcl 26.3.0.0 or newer for APEX operations. APEX 26.1 projects use the separate
[APEX_PROJECT_TEMPLATE_26_1](https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_26_1) repository. It uses
plain Git source, explicit deployment JSON, and lean 6-step release bundles
(`releases/<slug>/`). No custom team metadata schema or database lock tables are required.

**Supported databases:** Oracle Database 23ai and newer (this includes 26ai). The tools are tested only there. An older database such as 19c is not refused: `doctor` and the commands that write to a target print a warning and continue on a best-effort basis, and some features that need 23ai catalog columns (for example virtual-column expressions) are not available on it.

Projects created from this template use one Git repository per developer.
Those repositories do not share commits or a remote; developers coordinate
through the shared development database and APEX Builder workspace. Branches
separate file changes only and do not isolate a shared APEX application.

## Start here

| I want to… | Read |
| --- | --- |
| set everything up for the first time | [Getting started](docs/GETTING_STARTED.md) |
| choose a browser MCP for this checkout | [Browser MCP selection](docs/browser-mcp.md) |
| do a specific task, step by step | [Examples](docs/EXAMPLES.md) |
| understand an error message | [Troubleshooting](docs/TROUBLESHOOTING.md) |
| look up a command | [Command reference](#command-reference) |
| prepare and run a database/code/app release bundle | [Release workflow](docs/release-workflow.md) |
| compare full environment catalogs | [Environment comparison](docs/compare-env.md) |
| know exactly when publish refuses | [Publish rules](docs/publish-rules.md) |
| know which limits are deliberate | [Known limitations](docs/known-limitations.md) |
| check the PowerShell scripts under Windows PowerShell 5.1 | [Windows verification](docs/windows-verification.md) |
| see what an AI assistant can do here | [Skills and agent support](#skills-and-agent-support) |

After cloning this template into your own project, complete the
[post-clone cleanup](docs/GETTING_STARTED.md#post-clone-cleanup) via file-level
provenance review against `template-manifest.json` and `.template-lock.json`. Downstream
projects may prune inherited template-maintenance tests and CI workflows; the
original template keeps them for its own validation. Always retain
project-specific regression tests, linked project plans, durable docs, custom rules,
operational scripts, `.env`, manifest and lock.

## Daily choices

- Export an app you changed in Builder so that Git records it: [Getting started, section 9A](docs/GETTING_STARTED.md#a-builder-first).
- Publish a file edit to shared DEV with `scripts/team.sh publish <id> --env dev`: [publish rules](docs/publish-rules.md) list every refusal, and [partial publish](docs/partial-publish.md) covers existing pages only.
- Ship stored code, guarded DDL, ORDS scripts, smoke tests, and optional app deploys in one bundle under `releases/<slug>/`: [Release workflow](docs/release-workflow.md).
- Promote an app standalone to staging or production with `deploy`: [release to staging and production](docs/EXAMPLES.md#7-release-to-staging-and-production).

## What you get

| Feature | What it does |
| --- | --- |
| APEX apps as text in Git | `export` copies a live app into APEXlang files you can review, diff and commit. |
| Two ways to work | **Builder-first** (change it in APEX, export it) or **file-first** (edit the files, publish them). |
| Safe publish | Refuses newer Builder work, restores only automations that were active before import, then verifies normalized source. |
| Lean 6-step release bundles | `releases/<slug>/` holds `release.json`, `01-ddl/`, `02-code/`, `03-ords/`, and `04-verify/` checks; preflights live state with `release check` and deploys zero-prompt via `release run --confirm-target`. |
| DEV test runner | Read-only planning and explicit schema-owner fixture runs (`datatype-roundtrip`, `compiler-settings`, or a project suite) with durable reports and retained results; see [DEV test runner](docs/dev-test.md). |
| Schema comparison | Read-only comparison of selected tables, views and code between DEV, staging and production (`compare-schema` / `compare-env`). |
| Several schemas per workspace | Comma-separated lists in `.env` and `--schema`; apps and database copies are kept per schema, and release bundles declare schema per entry. |
| Staging and production | Per-app descriptors, an explicit `[y/N]` confirmation (or `--confirm-target`), and `--manual` to print a runbook for a DBA. |
| Read-only database copy | `backup-db` mirrors tables with their triggers and sequences, and views, packages, procedures, functions, other triggers and synonyms (structure only, never data). `backup-ords` adds an optional, read-only export of a schema's ORDS (REST) definition. |
| Safety built in | Every connection is identity-checked, production-looking names are caught, production is never written without an explicit confirmation (a `y` typed at the terminal, or `--confirm-target` naming the database's `db_unique_name`), file installs are all-or-nothing, and your uncommitted work is never overwritten. |
| Template upgrades | `upgrade-template` updates the template's own files and never touches your apps, release bundles, database copy or `.env`. |
| Knowledge graph | Optional Graphify index that links each app's pages, regions and processes to the tables and packages they use. |
| AI-assistant support | Repository rules, 12 skills, optional workflow packs, SQLcl MCP, per-checkout Chrome/Firefox MCP selection, and an optional Chrome daemon. |
| Bash and PowerShell | `scripts/team.sh` and `scripts/team.ps1` expose the same commands. |

## Skills and agent support

AI coding assistants read [AGENTS.md](AGENTS.md) (Claude Code loads it through
`CLAUDE.md`) for the rules and load a skill on their own when a task matches
its description. The same 12 skills for APEX and project setup are listed in `.agents/skills/` (for agents
that follow the `AGENTS.md` convention) and `.claude/skills/` (for Claude Code).
Five Claude Code entries (`building-apex-template-components`, `chrome-devtools-mcp`, `firefox-devtools-mcp`,
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
| Chrome DevTools MCP | Inspecting or driving your running web app through the selected Chrome MCP or authorized daemon fallback. |
| Firefox DevTools MCP | Inspecting or driving your running web app through the selected Firefox MCP. |
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

## Browser MCP setup

Choose Chrome, Firefox, both, or none for this checkout with scripts/team.sh mcp setup. Run scripts/team.sh mcp status to see the saved choice. The files are local and ignored; Codex requires a trusted checkout, and the client must be reloaded after a change. Setup starts no browser process. See the Browser MCP selection guide.

## Quickstart

New here? Follow [Getting started](docs/GETTING_STARTED.md): it walks through
everything below with the output you should see. You need SQLcl 26.3.0.0 or newer,
Git, Python 3.10 or newer, Bash 4.4 or newer, and an Oracle database with
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
share that connection. Staging and production profiles are
optional connection/user pairs; `release` and schema comparison also require
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
needs Git for Windows (the release, comparison and deployment helpers run in
its Bash), Python 3.10 or newer, and SQLcl on `PATH`. Five things trip people
up there:

- **Script policy.** Windows PowerShell 5.1 on a client Windows starts with the
  `Restricted` policy and refuses to run any `.ps1`. Allow your own account to
  run local scripts with `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
  A copy downloaded as a ZIP carries a "from the internet" mark that
  `RemoteSigned` still blocks; clone with Git, or run
  `Get-ChildItem -Recurse -Filter *.ps1 | Unblock-File` once.
- **Python.** The Bash helpers behind `release`,
  `compare-schema` and `deploy` call `python3`, which a python.org or winget
  install does not provide (the Microsoft Store `python3.exe` is a stub when
  Python came from elsewhere). `team.ps1` finds a working Python (`python3`,
  `python`, then `py -3`) and gives Git Bash a `python3` for that run, so there
  is nothing to set up. If you run `scripts/team.sh` straight from Git Bash and
  see `python3: command not found`, create one once and open a new Git Bash
  window: `mkdir -p ~/bin && printf '#!/usr/bin/env bash\nexec py -3 "$@"\n' > ~/bin/python3 && chmod +x ~/bin/python3`.
- **File encoding.** Release SQL files and `release.json` should be UTF-8 without a
  byte-order mark and with LF line endings. A
  deployment descriptor must be UTF-8 without a byte-order mark (a BOM is
  refused; CRLF is accepted). `Set-Content -Encoding UTF8` and `Out-File` in
  Windows PowerShell 5.1 write a BOM, UTF-16 or CRLF. Create the files in an
  editor set to UTF-8 and LF, or with
  `[System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($false)))`.
- **Checkout path.** Prefer a folder name without `[` and `]`. PowerShell reads
  them as wildcards in many cmdlets and Git Bash does not convert such a path
  for native programs; the scripts work around both, but your own commands in
  that folder may not (`Set-Location "C:\work [1]"` fails; use `-LiteralPath`).
- **Arguments under Windows PowerShell 5.1.** Windows PowerShell 5.1 cannot pass an empty argument, a double quote
  (`"`), or a value that contains a space and ends in a backslash to Git Bash or Python intact, so `team.ps1` refuses
  such an argument there and names the fix: run the command with PowerShell 7.3 or newer (`pwsh`) and a program that is not
  a `.cmd` or `.bat` file, or `scripts/team.sh` in Git Bash. PowerShell 7.3 or newer also refuses these values for a `.cmd`
  or `.bat` program, such as a `TEAM_BASH` batch file or a pyenv-win Python shim, because it keeps the legacy rules there.
  That refusal applies only when `$PSNativeCommandArgumentPassing` is `Windows` (its default on Windows) or `Legacy`; in `Standard` mode it does not refuse them.
  A single quote in an argument for Git Bash is refused on Windows under every engine, as before.

`team.ps1` accepts a bundle path or slug such as `releases\my-bundle` or `my-bundle`.

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

For supported commands, the SQLcl login may differ from the target schema;
see [Running as a login that acts as another schema](docs/login-acts-as-schema.md)
for the privilege and command limits.

| Command | Multi-schema behavior |
| --- | --- |
| `doctor` | Checks every distinct `(connection, expected user, schema)` across the three profiles, and every ORDS target when that optional profile exists, with one read-only SQLcl identity check each. It reports each schema and fails if any check fails. `--schema` narrows it. |
| `export <id>` | Reads the app's parsing schema from `APEX_APPLICATIONS` using the first `APEX_SQLCL_CONNECTION` entry, requires that schema in `APEX_PARSING_SCHEMA`, then reconnects with that schema's own connection and writes `apps/<SCHEMA>/<id>/`. `team.sh export` takes one ID. `scripts/export_apps.sh` with no argument resolves each configured app ID separately and installs all exports in one all-or-nothing mirror replacement. |
| `backup-db` | Runs the tables scope for each tables schema and the code scope for each code schema, plus the ORDS scope for each ORDS schema when the optional ORDS profile exists. Each has a staging directory, a completeness check, and a dirty-mirror check. Nothing is installed until every schema verifies; one mirror replacement installs them all. A scope this run did not refresh (for example the code scope of a tables-only run) is carried over unchanged from the committed mirror, not deleted. `--schema` narrows the run. |
| `backup-ords` | Exports each ORDS schema (`ORDS_SCHEMA`, optional) to `database/<SCHEMA>/ords/schema.sql` and replaces only that folder. `--schema` narrows the run; an ORDS-only schema is selectable. |
| `publish <id>` | Imports to shared DEV only (`scripts/team.sh publish <id> [--env dev]` or `--file ...`). Requires the app folder, descriptor, and live app parsing schema to agree and be listed, then uses that schema's connection. Full mode keeps the app-wide drift guard; selected-page mode verifies a fresh merged snapshot. Promotion to staging or production uses `deploy`. |
| `deploy <id> --env staging\|prod` | Confirms promotion to staging or production or prints a DBA runbook (`--manual`). The descriptor's `parsingSchema` selects the same-named staging or production entry. Confirmation shows the schema and connection; imports restore previously active automations. |
| `release` (`collect`, `check`, `run`) | Each entry in `release.json` (`ddl`, `code`, `ords`, `verify`) names its target schema; `release check` and `release run` open 1 SQLcl session per schema. |
| `compare-schema` | Requires `--schema` when more than one schema is configured and compares that schema across the selected environments. |
| `compare-env` | Requires `--schema` when more than one schema is configured and compares that schema's full catalog across the selected environments. |

One bundle can touch multiple schemas because every step entry declares its
schema explicitly; `release run` applies each schema's steps in sequence using
its configured target profile. Private synonyms are mirrored under
`database/<SCHEMA>/synonyms/` with the code objects.

Limits:

- Position-aligned lists rely on order; the loader cannot detect values that
  were swapped consistently across a profile.
- Schemas sharing one SQLcl connection share its privileges. Identity checks
  confirm the session user, not that it can reach only one schema.
- `publish --force` skips the drift check, but still requires schema agreement
  between the app folder, descriptor, and live app.

## Builder-first workflow

Make and save the change in shared APEX Builder, then run `scripts/team.sh export <id>`
and review `git diff` before you commit. Export records the current Builder state and
never imports the app back. It refuses to overwrite uncommitted local files, so commit
or stash your own edits first.

Steps: [Getting started, section 9A](docs/GETTING_STARTED.md#a-builder-first). Export
and publish rules: [publish rules](docs/publish-rules.md#how-the-check-works).

## File-first APEXlang workflow

Edit the files and review them. Publish imports what is on disk, so committing first is
optional. Tell your team which app you are publishing and check for unsaved Builder
edits, then run `scripts/team.sh publish <id> --env dev`. Publish stamps a version tag
into `application.apx`, refuses if the live app changed since your last export, and
verifies the imported source. Commit the stamped file after a successful publish;
`deploy` handles staging and production.

Steps: [Getting started, section 9B](docs/GETTING_STARTED.md#b-files-first). Every
refusal and its fix: [publish rules](docs/publish-rules.md). Existing pages only:
[partial publish](docs/partial-publish.md).

## Release bundles

Ship schema DDL, stored code (`02-code/`), ORDS scripts (`03-ords/`), verification
queries (`04-verify/`), and APEX application deploys in one lean 6-step release
bundle under `releases/<slug>/`:

```text
releases/2026-11-24-order-fixes/
├── release.json
├── 01-ddl/
├── 02-code/
├── 03-ords/
└── 04-verify/
```

1. **Create or select:** `scripts/team.sh release list` (offline) or
   `scripts/team.sh release new 2026-11-24-order-fixes`.
2. **Collect stored code & author DDL:** `scripts/team.sh release collect <bundle> --from dev --schema APP_CODE --object "PACKAGE:ORDER_PKG" --mode patch --target-env prod`.
   `collect` exports live source in 1 SQLcl session, strips
   `ALTER SESSION SET PLSQL_CODE_TYPE = INTERPRETED;` (for ADB compatibility),
   and records `baseSha256` from `--target-env` when `--mode patch` is used. Add
   any ordered DDL SQL under `01-ddl/`, ORDS scripts under `03-ords/`, smoke
   checks under `04-verify/`, and numeric app IDs under `"apps"` in
   `release.json`.
3. **Preflight read-only:** `scripts/team.sh release check <bundle> --env <dev|staging|prod>`
   validates paths, `forbiddenPatterns`, `new` non-existence, and `patch`
   `baseSha256` digests (writing a unified diff to `scratch/release-diffs/` on
   mismatch).
4. **Execute zero-prompt:** `scripts/team.sh release run <bundle> --env <dev|staging|prod> --confirm-target <db_unique_name>`
   requires `--confirm-target` matching live `DB_UNIQUE_NAME` with **zero stdin
   prompts**, backs up existing touched objects into `scratch/backups/`,
   applies `01-ddl/` -> `02-code/` -> `03-ords/` in 1 SQLcl session per schema,
   checks before/after invalid objects (failing only on *new* invalids), runs
   `04-verify/`, deploys listed `apps` via `scripts/deploy.sh --yes --confirm-target`,
   writes a markdown report to `scratch/release-reports/`, and locks `release.json`
   (`status: "deployed"`) plus Git tag `release/<slug>` when `--env prod` succeeds.

See [docs/release-workflow.md](docs/release-workflow.md) for the complete
`release.json` contract, `01-ddl/` guarded DDL examples, and recovery notes.

Compare selected live object names or prefixes with `compare-schema`:

```bash
scripts/team.sh compare-schema --from dev --to staging \
  --object CUSTOMERS --object ORDERS
scripts/team.sh compare-schema --env prod --pattern 'HR_*' --pattern 'GL_*'
```

This reports current catalog drift for the selection.

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

## Staging and production deployments

For standalone app promotion without a release bundle, configure the target
connection/user pair in `.env` and provide a per-app descriptor. The tool prints
the selected app, workspace, and schema before asking for confirmation:

```bash
scripts/team.sh deploy 100 --env staging
scripts/team.sh deploy 100 --env prod
```

Direct standalone `deploy` imports require `Deploying to <ENV>. Proceed?
[y/N]`, answered at a terminal; piped answers are refused. `-y` / `--yes` accepts it on staging. On production
it also needs `--confirm-target <db_unique_name>` with the observed name (`release run` passes both non-interactively in Step 5); see
[publish rules](docs/publish-rules.md#staging-and-production-confirmation). To prepare a DBA handoff without opening a database connection, use:

```bash
scripts/team.sh deploy 100 --env prod --manual
```

The runbook includes the explicit deployment descriptor and SQLcl identity
check. A human DBA executes it using an approved saved connection, then runs
the re-export check the runbook ends with.

## Upgrading from the template

Projects created from this template do not share its Git history, so updates
are copied file by file. `template-manifest.json` decides which repository
files the upgrade may touch: template scripts, tests, CI, and agent guidance
are upgraded; project files such as `AGENTS.project.md`,
`.agents/rules/project.md`, `PROJECT.md`, and `TODO.md` are created once and never
overwritten; application source, database mirrors, release bundles,
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
repository does not require a downstream lock. Tests and `.github/` files that
the post-clone cleanup removed are reported as `removed-by-cleanup`, which is
informational and does not make the status `attention`.

The lock also records the selected `templateRef` and `apexRelease`. Later runs
reuse the recorded ref when `--ref` is omitted. Keep a 26.1 project on the 26.1
repository with `--source https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_26_1.git --ref main --apex-release 26.1`.
Moving to the 26.2 line requires an explicit
`--source https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_TEAM.git --ref main --apex-release 26.2`.
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
`database/<SCHEMA>/synonyms/`; review and commit that new folder.

A project upgraded to a template version that mirrors table triggers and sequences
needs no `.env` change. Its next `scripts/team.sh backup-db` takes each trigger on a
table that the tables profile mirrors into the tables scope (counted in
`manifest-tables.txt` instead of `manifest-code.txt`; the file stays in
`database/<SCHEMA>/triggers/`), and adds `database/<SCHEMA>/sequences/`. Review and
commit that diff. A trigger is taken when its table or its own name starts with a
`TABLES_PREFIXES` entry; add a prefix there for a project trigger on a table outside
those prefixes.

A project upgraded to a template version with ORDS support needs no `.env`
change either: ORDS stays disabled until all three `ORDS_*` keys are added. The
upgrade delivers the ORDS scripts, tests and [docs/ords-export.md](docs/ords-export.md)
as template files and never touches `database/`, so an existing
`database/<SCHEMA>/ords/` export, your `.env`, apps and release bundles are kept.

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
26.1, use `--source https://github.com/ash2osh/APEX_PROJECT_TEMPLATE_26_1.git --ref main --apex-release 26.1`
instead.
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

`doctor` detects the full database version and `COMPATIBLE` separately for each
checked DEV target, including a configured ORDS target. It does not write these
observations into `.env` or change database settings. Missing, malformed or
unavailable detection makes the check fail; it is not a compatibility pass.
The observations belong to the displayed target identity and this run: do not
reuse DEV results as proof for staging or production. Before choosing a feature
for several targets, establish their supported versions and compatibility
settings through authorized read-only checks; if unavailable, report them as
unknown. Version and compatibility do not prove feature privileges or availability.

| Command | Purpose |
| --- | --- |
| `scripts/team.sh app-lock <id> --env dev` | Acquire a native application lock as the configured workspace developer. |
| `scripts/team.sh app-unlock <id> --env dev` | Release only the configured developer's application lock after reconciliation. |
| `scripts/team.sh upgrade-apexlang <id> --env dev --mode builder\|files` | Explicitly convert clean app source to canonical 26.2; see [source upgrade](docs/apexlang-upgrade.md). |
| `scripts/team.sh doctor` | Validate `.env`, check the DEV SQLcl identity and report the detected database version and `COMPATIBLE`. |
| `scripts/team.sh targets [--format text\|json]` | Show, offline and read-only, the schema, SQLcl connection and expected login that each operation uses; a split owner appears as a `REFUSED` row where the command would refuse. Never connects and never starts SQLcl. |
| `scripts/team.sh dev-test --env dev --schema NAME --plan\|--run` | Plan or run the two built-in DEV checks (`datatype-roundtrip`, `compiler-settings`), or an explicitly selected project suite; fixtures are retained by default. See [DEV test runner](docs/dev-test.md). |
| `scripts/team.sh dev-test --report <run-id> [--format text\|json\|markdown]` | Render saved evidence without connecting to a database. |
| `scripts/team.sh dev-test --cleanup <run-id>` | Explicitly clean supported recorded artifacts; project suites without a cleanup contract remain manual. |
| `scripts/team.sh query --env <dev\|staging\|prod> --connection <tables\|code\|apex\|ords> [--schema NAME] [--file PATH \| SQL]` | Run one conservative, bounded SELECT in a read-only SQLcl transaction; see [read-only ad-hoc queries](#read-only-ad-hoc-queries). |
| `scripts/team.sh export <id>` | Export one numeric app from shared DEV Builder. |
| `scripts/team.sh publish <id> --env dev [--strict-bytes] [--no-restore-automations]` | Drift-check and import one app to DEV; by default restores automations that were active before import. |
| `scripts/team.sh publish <id> --file pages/<file>.apx [--no-team-notice] [--no-restore-automations]` | Publish selected existing DEV pages, verify and synchronize canonical source. |
| `scripts/team.sh release list [--format text\|json]` | List open `draft` and locked `deployed` release bundles offline. |
| `scripts/team.sh release new <slug>` | Create a new `draft` release bundle under `releases/<slug>/`. |
| `scripts/team.sh release collect <bundle> --from <env> --schema <SCHEMA> --object <TYPE:NAME> --mode <new\|replace\|patch> [--target-env <env>] [--file <relpath>]` | Export live stored code/view/trigger source into `02-code/`, strip `PLSQL_CODE_TYPE = INTERPRETED`, capture `baseSha256` on `patch`, and register in `release.json`. |
| `scripts/team.sh release check <bundle> --env <dev\|staging\|prod> [--format text\|json] [--allow-existing]` | Read-only preflight: validates `release.json`, checks live target identity, verifies `new` non-existence and `patch` `baseSha256`, prints the execution plan. |
| `scripts/team.sh release run <bundle> --env <dev\|staging\|prod> --confirm-target <db_unique_name> [--allow-existing] [--skip-backup] [--report <path>]` | Zero-prompt 6-step execution: backups (`scratch/backups/`), apply `01-ddl/` -> `02-code/` -> `03-ords/`, invalid-object delta check, `04-verify/`, optional APEX `apps` deploy, markdown report (`scratch/release-reports/`), and prod lock + tag. |
| `scripts/team.sh compare-schema --env <env> --object <name>` | Compare selected live schema objects read-only. |
| `scripts/team.sh compare-env --from <env> --to <env> [--section <name>] [--format text\|json\|markdown] [--emit-dba-script <file>]` | Compare complete, paged environment catalogs by object name; see [compare-env](docs/compare-env.md). |
| `scripts/team.sh backup-db` | Refresh local table and code metadata mirrors (and ORDS, when configured). |
| `scripts/team.sh backup-ords` | Export ORDS (REST) metadata read-only to `database/<SCHEMA>/ords/schema.sql`. |
| `scripts/team.sh deploy <id> --env <staging\|prod> [--manual] [--strict-bytes] [--no-restore-automations] [--yes\|-y] [--confirm-target <db_unique_name>]` | Confirm a standalone promotion or print a DBA runbook with the automation restore step. `--yes` / `-y` accepts the prompt on staging; production needs `--confirm-target` with it. `--manual` takes neither flag. |
| `scripts/team.sh staging-neutralize --capture\|--check\|--apply\|--restore FILE [--confirm-target <db_unique_name>] [--yes\|-y]` | Make a staging copy restored from production inert before testing: disable the project principals' scheduler jobs, the APEX jobs that send or call out, every active automation, and outbound network grants, through `STAGING_DBA_SQLCL_CONNECTION`. `--apply` and `--restore` need `--confirm-target`; `--apply` writes the undo file under `scratch/staging-neutralize/`. |
| `scripts/team.sh upgrade-template [--dry-run]` | Update template-owned files; never overwrites project files. |
| `scripts/team.sh verify-local [--format text\|json] [--live]` | Validate local environment, lock, apps and skills read-only. Warns (exit code unchanged) when a configured staging or production profile has no matching app descriptor, or when a `production.json` descriptor would be ignored. |
| `scripts/team.sh skills setup none\|superpowers\|mattpocock\|both [--primary superpowers\|mattpocock]` | Set up optional, pinned workflow packs locally. |
| `scripts/team.sh skills status [--format text\|json]` | Inspect repository-managed packs offline. |
| `scripts/team.sh mcp setup [--choice chrome\|firefox\|both\|none] [--yes\|-y]` | Select checkout-local Chrome and Firefox MCP servers; setup writes ignored client configuration only. |
| `scripts/team.sh mcp status` | Show the saved browser MCP choice for this checkout. |
| `scripts/team.sh template-status [--format text\|json]` | Compare template-owned files with the local lock; no network access by default. |
| `scripts/team.sh template-status --upstream --ref <ref>` | Explicitly compare local files with a branch, tag or commit from the manifest's upstream repository. |
| `scripts/team.sh automations list <id> --env <env> --schema <schema>` | Read one application's automation status in its descriptor workspace. |
| `scripts/team.sh automations check <id> --env <env> --schema <schema> --automation <static-id> --expected-status ACTIVE` | Check an exact selected automation against APEX 26.2's `ACTIVE`, `DISABLED`, or `ERROR` schedule states. |
| `scripts/team.sh automations enable|disable|run-once <id> --env <env> --schema <schema> --automation <static-id> [--yes|-y] [--confirm-target <db_unique_name>]` | Explicitly change one automation's schedule or run it once after target/action confirmation; no publish path invokes these operations. `--yes` / `-y` supplies the confirmation; production also needs `--confirm-target`. |

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

Use `query` for a one-off inspection without adding a script or release SQL file:

```bash
scripts/team.sh query --env dev --connection tables --schema DEMO "SELECT table_name FROM user_tables ORDER BY table_name"
scripts/team.sh query --env dev --connection code --file scratch/query.sql
```

`--connection` names a configured profile, never an arbitrary SQLcl alias or
connection string. In DEV, `tables`, `code`, `apex` and `ords` use their own
aligned schema, SQLcl connection and expected-user settings. In staging or
production, profiles use that environment's aligned schema/connection/expected-user
triple. ORDS is DEV-only. Missing or incomplete settings,
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

Exit status, the same in Bash and PowerShell:

| Status | Meaning |
| --- | --- |
| 0 | Done; for `compare-schema` and `compare-env`, nothing found; for `release check`, preflight passed. |
| 1 | `compare-schema` or complete `compare-env` found differences, a `[y/N]` prompt was declined, `upgrade-template` left `.template-new` files to merge, or `.env` is invalid (`project environment error: ...`). |
| 2 | Refused or failed; the message says why and what changed. `release check` / `release run` return 1 or 2 when validation, preflight, execution, or verification fails; `compare-env` uses 2 for incomplete capture. |
| 130 / 143 | Stopped by Ctrl-C / by SIGTERM (`kill`). |

SIGTERM is for Bash: `team.sh` hands its process to the command it runs, so a
`kill` reaches it. PowerShell 7 on Linux and macOS cannot run any cleanup on
SIGTERM, so `kill` ends `team.ps1` while the Bash helper or SQLcl it started
carries on; its scratch folder stays behind. Stop a PowerShell run with Ctrl-C,
or signal its whole process group (`kill -TERM -- -<pgid>`). Windows has no SIGTERM.

`scripts/team.ps1` exposes the same commands for PowerShell. Release and
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

Use `python3 scripts/graphify_project.py extract` as the only Graphify update
path. Routine `.apx` and `database/*.sql` updates use the offline project-local
CLI and do not require an LLM API key. Do not use a Graphify skill or MCP update
operation. Semantic indexing of changed `app_context/**/*.md` files is optional
and may use a configured backend; it is separate from routine code updates.

- `python3 scripts/graphify_project.py verify` checks the isolated installation,
  pinned versions, and cached corpus coverage without modifying files or launching
  a subprocess. Missing/stale corpus receipts report coverage as unavailable.
- `python3 scripts/graphify_project.py verify --refresh-corpus` explicitly runs the
  local detector with saved exclusions and writes the ignored
  `graphify-out/.template-corpus.json` coverage receipt. Extraction also refreshes
  it. Source, ignore, build-setting, Git index/exclusion or graph changes invalidate it.
  Domain-root and ignore-metadata symlinks are unsupported and refuse coverage;
  excluded source links are inventoried without reading their targets, and selected
  links must resolve inside the project. Refresh explicitly after changing external
  Git environment/configuration. This proves file presence, not extracted content
  freshness or semantic completeness.
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

Agents follow the same app ID, descriptor, drift, release, and deployment
rules as developers. Review source before editing; do not run database-writing
commands unless the user asked; coordinate with the team before importing into
shared DEV. Never claim an unavailable live check passed. See [AGENTS.md](AGENTS.md)
for the full repository contract, and [docs/release-workflow.md](docs/release-workflow.md)
for the 6-step release bundle contract. For browser runtime checks, see
[Chrome DevTools MCP](docs/CHROME_DEVTOOLS_MCP.md) and use the project browser
[skill](.agents/skills/chrome-devtools-mcp/SKILL.md). For confirmed inherited-
template issues, keep a sanitized local record in [TODO.md](TODO.md) and follow
the [upstream feedback guide](docs/template-feedback.md).
