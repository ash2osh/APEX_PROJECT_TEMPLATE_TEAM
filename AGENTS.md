# Team APEX agent contract

This branch is the APEX 26.2 / APEXlang team template; APEX operations require
SQLcl 26.3.0.0 or newer. APEX 26.1 projects use `codex/apex-26.1`. The template
repository follows its normal branch and pull-request process. A project
created from it gives each developer a separate Git repository; the team
shares its development Oracle database and APEX workspace, not Git history.
Never exchange downstream commits or assume a colleague's repository changes
are present locally. A Git branch does not isolate shared database or Builder
state.

At the start of every agent task, follow the mandatory weekly Oracle skills
refresh below and report its status to the developer in the current chat.

## Conversation style

At the start of every conversation, before your first reply:

1. Read `AGENT_STYLE` (default `standard`; one of `standard`, `concise`, `teaching`,
   `asharif-style`) and `AGENT_STYLE_LEARNING` (default `on`) from the root `.env`.
2. Read `.agents/styles/<AGENT_STYLE>.md` and follow it for the whole conversation.
3. Read `.agents/styles/personal.md` if it exists and apply it over the style.
4. Open your first reply with one line: `Style: <name>`, plus ` + personal layer (N entries)`
   when `personal.md` has entries. If the value is invalid or a file is missing,
   fall back to `standard` and say so in that line.

Precedence, highest first: this file, `.agents/rules/agent-safety.md` and the project rules; then
`personal.md`; then the style. A style or a personal entry never relaxes a gate (commits and
pushes, database writes, migrations, imports, claiming a check that did not run).

Learning, unless `AGENT_STYLE_LEARNING=off`: when the developer states a preference or corrects your
format, or makes the same choice three times, add one dated line to `personal.md` under the matching
heading and say `Recorded in personal.md: <entry>` in the same reply. Never record silently. Replace
an entry that the new one contradicts. Keep the file under about 40 lines. Never record credentials,
hosts, customer, employee or business data, project or schema names, or anything that loosens a
safety rule. Never commit or push `personal.md` on your own initiative. See
[.agents/styles/README.md](.agents/styles/README.md).

## Optional workflow packs

Read `.agent-skills.json` at task startup (missing means `none`). It records
`selection` (`none`, `superpowers`, `mattpocock`, `both`) and `primary`.
Only apply an enabled repository workflow pack. With both selected, use the
primary pack's process by default; use the other for an explicit user request
or task override, without duplicating whole workflows. Direct user instructions
and project safety rules take precedence over either pack.

Set up packs only when requested, using `scripts/team.sh skills setup <selection>`
(or `team.ps1`). Use `skills status` for offline installation evidence. Selection
controls repository-managed copies, not global skills or enabled app plugins;
report those separately when they affect a request to use neither.

The 11 template-authored domain/setup skills and mandatory Oracle refresh remain
independent of optional workflow packs. After installing Matt's pack, invoke
`/setup-matt-pocock-skills` when requested and put its project guidance in
`AGENTS.project.md`, preserving existing instructions, instead of template-owned
`AGENTS.md`/`CLAUDE.md`. See [workflow skill setup](docs/workflow-skills.md).

## Configuration and application source

- Copy `.env.example` to root `.env`, set `DEVELOPER_NAME` to the
  developer's uppercase name, and use SQLcl saved connection names. After
  local validation, review and keep this credential-free configuration in
  each developer's own downstream Git repository. The
  default points the table, code, and APEX profiles at the same DEV
  connection. Keep these profile values aligned unless the project needs
  separate connections. Staging and production connection/user pairs are
  optional. Each profile's schema, connection, and expected-user keys accept
  position-aligned comma lists; one value keeps current behavior. Use
  `--schema <NAME>` to narrow any command. `doctor` and `backup-db` default to
  all schemas. Never put credentials in `.env` or tracked files. An optional,
  independent `ORDS_SCHEMA`, `ORDS_SQLCL_CONNECTION` and `ORDS_EXPECTED_USER`
  profile (all three or none) enables the read-only ORDS export; its session
  user must equal the REST schema.
- Use numeric APEX application IDs in commands and descriptors. Store each
  app's APEXlang source and `deployments/{dev,staging,prod}.json` under
  `apps/<parsing-schema>/<app-id>/`. `apps/templates/deployments/` contains
  descriptor examples. Each descriptor names the target workspace, numeric
  app ID, and parsing schema explicitly.
- Keep APEXlang and SQL files in LF line endings. Review generated source and
  database mirror changes before committing.
- The shared database is the runtime source of truth. Do not add custom team
  metadata tables, mutexes, checkout rosters, or database-backed migration
  ledgers.

## Team workflow

- **Builder-first:** Coordinate with teammates before editing a shared app in
  Builder. Run `scripts/team.sh export <app-id>`, review the APEXlang diff, and
  commit it. Export records the observed Builder state; do not import as part
  of this route.
- **File-first:** Edit the exact APEXlang source; committing before publish
  is optional, since publish imports the files on disk. Before importing
  to shared DEV, tell the team which numeric app ID is being published and
  check for in-progress Builder edits. Run
  `scripts/team.sh publish <app-id> --env dev`. The drift guard compares the
  live app's update time with the database-time revision captured before the
  latest export. The exporter also checks that Builder did not change during
  that export. Oracle records this value at one-second precision, so the
  exporter and guard fail closed when it matches the current database second.
  This timestamp cannot see unsaved Builder edits; communicate with teammates
  before publishing. APEX leaves it null after an APEXlang import, so each
  DEV publish stamps `[DEVELOPER_NAME-YYYY-MM-DDrNNN]` onto the application
  version in `application.apx` before import, and the guard also refuses when
  the live version differs from the baseline (a teammate's import). Commit the
  stamped `application.apx` after publish; `deploy` ships the tag unchanged.
  The guard refuses to overwrite newer Builder work. Export and reconcile
  before retrying. After import, publish re-exports the app and requires
  normalized APEXlang equality and a matching normalized second re-export
  before it advances the DEV baseline. `--strict-bytes` requests byte-exact
  source comparison. A failed or ambiguous verification leaves the old baseline
  in place, so the next publish still refuses newer Builder state. Use `--force` only when the
  user explicitly directs an override after review. When publish refuses,
  follow [docs/publish-rules.md](docs/publish-rules.md), which lists every
  refusal, its meaning, and the fix.
- **Native DEV locks:** Set `APEX_WORKSPACE_USERNAME` to your existing Builder
  developer/admin login, independently of `DEVELOPER_NAME` and the database
  login. DEV publish acquires a committed public application lock before its
  final drift check and verifies ownership through import and re-export. Any
  preexisting lock, including your own, refuses publish; `--force` never breaks
  a lock. The app must already exist in the descriptor's workspace/schema;
  working copies are deferred. Use `scripts/team.sh app-lock <id> --env dev`
  for an explicit lock and `app-unlock <id> --env dev` for an owner-filtered
  release. SQLcl imports bypass native locks, and same-account native API
  reentry is not a process mutex. Continue human coordination. An ambiguous
  import retains the old baseline, native lock and ignored recovery evidence;
  inspect/reconcile before unlocking or retrying. Page locks are created in
  Builder; no public SQLcl page-lock setter is qualified.
- **Selected DEV pages:** Use repeatable `publish <id> --file pages/<file>.apx`
  for existing non-global pages with a schema-2 baseline. Refuse unselected local
  edits and selected-page drift; preserve unrelated saved live work through a
  fresh whole-app snapshot, compiler validation and exact post-import verification.
  Follow [docs/partial-publish.md](docs/partial-publish.md). Ordinary partial
  imports retain coordination. `--no-team-notice` asserts separate workspace
  accounts and an agreed guarded workflow; it additionally requires unchanged
  pre-edit Builder locks owned by `APEX_WORKSPACE_USERNAME` on every selected
  page, and an explicitly supplied live-matching checksum salt and URL cutoff.
  Never claim those checks acquire page locks or prove human account exclusivity.
  Initial creation, full/global changes and promotion still require coordination;
  ambiguous partial writes or retained locks require recovery communication.
  Partial mode refuses `--force` and non-DEV targets; it never falls back to full
  import. Review all synchronized source, including unrelated live changes.
- **Runtime verification:** Full/partial publish and promotion require scoped
  automation/workflow/task observations before and after import, independently
  of source verification. After import, it re-enables exactly the
  automations that were ACTIVE before import with `APEX_AUTOMATION.ENABLE`,
  then prints and verifies their read-back state. `--no-restore-automations`
  opts out while keeping lifecycle verification active. Lifecycle attention or
  an unavailable post-read retains the old DEV baseline, run-owned lock and
  recovery evidence. Inspect
  `sourceVerified`, `lifecycleStatus` and the lifecycle report before recovery.
  Never resume a workflow or repair a task automatically;
  `--force` does not bypass this gate. Follow
  [docs/publish-rules.md](docs/publish-rules.md#runtime-verification-after-import).
- **Migrations:** Put each migration in
  an independently configured migration owner when needed: the optional
  `MIGRATION_SCHEMA`, `MIGRATION_SQLCL_CONNECTION`, `MIGRATION_EXPECTED_USER`
  triple and its `STAGING_` / `PROD_` equivalents must be configured together.
  Each accepts aligned lists. An absent triple uses the original environment
  profile; an incomplete or empty triple refuses. Metadata mirrors retain
  their own profiles. Then put each migration in
  `migrations/YYYY-MM-DD_<name>-rNNN/` with consecutive named SQL steps such as
  `001-create-table.sql`, plus `checks.json`. No developer name belongs in the
  path. Keep source immutable after a write attempt; a follow-up uses the next
  revision folder. Run `scripts/team.sh check-conflicts <folder> --env dev`
  before applying with `scripts/team.sh migrate <folder> --env dev|staging|prod`.
  Keep `checks.json` schema 1 unchanged unless a reviewed rehearsal needs
  schema-2 `rehearsalMetrics`; those metrics run only under `--rehearse`, never
  as apply postconditions. Follow [docs/migration-rules.md](docs/migration-rules.md)
  for their bounds and rollback limits.
  Checks cover only selected local migrations and observed live state; they
  cannot see another independent repository's pending files. Staging and
  production migrations require `STAGING_SCHEMA` or `PROD_SCHEMA` in addition
  to the existing connection and expected-user settings. Receipts are written
  only after a verified apply. Multi-schema projects use
  `migrations/<SCHEMA>/…`; one migration changes one schema. See
  [docs/migration-rules.md](docs/migration-rules.md) for naming, ordering,
  recovery, and detection limits.
- **Schema comparison:** Use `scripts/team.sh compare-schema --env staging
  --object CUSTOMERS` or `--from dev --to prod --pattern 'HR_*'` to compare
  selected live objects. This reports schema drift; local receipts cannot
  reliably attribute a shape to one migration file.
- **Daily change record:** A package migration holds the whole source, so Git
  cannot show what changed. For each day with code work, keep
  `ai_generate/YYYY-MM-DD/` with the readable diff of each migration
  (`python3 scripts/migration_diff.py migrations/<SCHEMA>/<folder>`, local files
  only, run before `backup-db` refreshes the mirror) and one `WALKTHROUGH.md` for
  that day. Commit the folder with the work. See
  [docs/daily-change-record.md](docs/daily-change-record.md).
- **Promotion:** Put an explicit deployment descriptor in the application
  source. Use `scripts/team.sh deploy <app-id> --env staging` or `--env prod`;
  each direct import requires the displayed `[y/N]` confirmation. Add
  `--manual` to print a DBA runbook without connecting. A deployment writes
  to the selected APEX workspace and parsing schema. For an ordered release
  manifest and recovery packet, follow
  [the production deployment runbook](docs/production-deployment.md).
- **Checks and shared-target reads:** Use `scripts/team.sh verify` to evaluate
  declared migration checks read-only. For ad-hoc shared-target reads, use
  `scripts/team.sh query` with a named configured profile and identity guards;
  do not improvise a raw SQLcl read. `migrate --rehearse` runs eligible DML on
  the selected real target and rolls back its transaction, so use it only when
  that exact target and DML have been authorized. Its scalar checks do not
  prove that sequence/identity advancement, autonomous work, external effects,
  or DDL were undone. Do not lower or remove a reviewed assertion just to make a
  command pass. The explicitly approved disposable-fixture route in
  [local qualification](docs/local-qualification.md) remains scoped to its
  named local target and cleanup.
- **Other commands:** `scripts/team.sh doctor` validates `.env` and performs a
  read-only SQLcl identity check. `scripts/team.sh query --env <env>
  --connection <profile> [--schema NAME] [--file PATH | SQL]` runs the bounded,
  conservative read-only query flow described in [README.md](README.md#read-only-ad-hoc-queries).
  `scripts/team.sh verify-local` validates local
  configuration, lock, app descriptors and native Oracle skills offline.
  `scripts/team.sh template-status` reports local template-owned file hashes
  against `.template-lock.json` without network access; only an explicit
  `--upstream --ref <ref>` performs a read-only upstream fetch/comparison.
  `scripts/team.sh automations list|check <app-id> --env <env> --schema <schema>`
  reads app-scoped automation states through the lifecycle adapter; `check`
  requires an explicit status and exact static-ID selection. Explicit
  `enable|disable|run-once` actions require one static ID and exact typed
  confirmation, recheck native application locks and target/status, and read
  the result back. `run-once` is synchronous and requires a completed success
  log; an ambiguous action is `unknown` and must not be replayed automatically.
  These commands never repair workflow/task state or advance a failed publish
  baseline. Session creation/deletion may run configured app session
  initialization/cleanup code; the command states this before confirmation.
  Migration, rehearsal and rollout script delivery also runs the SQLcl
  transport preflight; an error refuses before the payload starts, while a
  warning keeps the exact source bytes and requires review of the emitted SQL.
  `scripts/team.sh backup-db` refreshes the local table and code mirrors, and the
  ORDS mirror when the ORDS profile exists. `scripts/team.sh backup-ords`
  exports ORDS metadata only: it never enables REST, imports, changes ORDS
  configuration, creates metadata, grants privileges or commits, and it never
  exports OAuth clients or secrets. Read [docs/ords-export.md](docs/ords-export.md)
  before changing it.

## Optional Tooling

`graphify` (a knowledge-graph indexer exposed via ignored `graphify-out/`) is
optional; every script works without it. Its rules live in
[`.agents/rules/graphify.md`](.agents/rules/graphify.md) and
[`.agents/workflows/graphify.md`](.agents/workflows/graphify.md), and every rule
is gated on `graphify-out/graph.json` existing. Explicit setup uses
`python3 scripts/graphify_project.py setup` with pinned dependencies in the
project's ignored `.venv-graphify/` (Python 3.12+). Run
`python3 scripts/graphify_project.py verify` for read-only package/cache/coverage
checks, and `python3 scripts/graphify_project.py extract` to rebuild the canonical
`graphify-out/`. Never patch a global/shared installation or another project.
The corpus is a domain allowlist of `apps/`, `database/`, and `app_context/`.
Graphify links cross-schema references and mirrored synonyms when their targets
are present in the database mirror. The graph reflects this repository's
files, not shared Builder state. When the graph exists, each new conversation
starts a background incremental `extract` (see the graphify rules for its
guards); it is skipped when the Graphify environment is not set up.

## Mandatory weekly Oracle skills refresh

- At the start of **every task**, including planning, reviews and non-database
  work, read SQLcl's user-local `~/.dbtools/skills/skills.json`. Resolve `~`
  against the current user's home on each platform. SQLcl owns this file;
  never copy it into a project, edit its dates, or create an extra timestamp file.
- Check the Oracle repository's `repositories[].lastSync` and the
  `installations[].installedAt` records for all Oracle skill roots in the actual
  skill directories loaded by this agent. Match expanded `targetPath` values,
  not only agent labels, and deduplicate identical records for a shared path.
  Conflicting duplicates are invalid. Discover expected roots from the synced
  Oracle catalog (currently `apex`, `db`, `fusion`, `graal` and `oci`); require
  each installation record and its installed `SKILL.md`. Do not use file mtimes
  or a fresh repository timestamp as proof of fresh installed skills.
- If the registry/catalog or required records/files are missing, unreadable or
  malformed, or any relevant UTC timestamp is invalid, in the future or seven
  days old or older, automatically refresh all Oracle roots. Start SQLcl without
  a database connection (`sql /nolog`), then issue `skills sync -force`.
  SQLcl MCP `skills_sync(force=true)` with all roots selected is equivalent.
  This instruction authorizes a due refresh; do not ask again.
- Check the current run completed successfully with no conflicts, then reread
  SQLcl's native registry and confirm the relevant repository/installation dates
  and installed files are current. Never manually advance or repair its records.
  Notify the developer in the **current chat**: "I automatically ran Oracle
  skills sync with -force and verified SQLcl's native installation dates."
  Include the verified date and available installation count.
- If already current, report "Oracle skills are current; recorded installation
  date: <date>." Use the oldest relevant installation date. Do not claim a new
  sync ran. The native registry does not persist a force flag or a complete-run
  conflict/success verdict; its dates establish freshness, not proof that an
  earlier run used `-force` or completed entirely without conflicts.
- A failed/conflicted run or unverifiable registry blocks Oracle/APEX
  implementation and live operations for this task; report it as failed or
  unavailable. Read-only investigation/planning may continue. SQLcl may advance
  some native dates during an unsuccessful run: those dates never override an
  observed failure in the current task. Do not synthesize a success receipt.
- Preserve project-authored `.agents/skills/` and `.claude/skills/`, compare Git
  status before/after refresh and report unexpected changes. This authorization
  covers Oracle-maintained installed skills only. Do not put credentials or
  full SQLcl logs in tracked files or status notifications.
- Use APEXlang skills matching the target release; do not mix 26.1 and 26.2.
  No standalone automation is required. Inactive projects check at next task
  startup; projects sharing this user's installations reuse native freshness.
  Notification does not authorize messages to teammates or external channels.

## Template update check

At the start of every new conversation, run once in the background:
`python3 scripts/check_template_update.py`. This read-only check compares the
lock's `commit` with its recorded `templateRef` (branch or tag), using a portable
10-second timeout on each Git call. It does not fetch objects into the project.

- Stay silent when the lock/ref is missing or invalid, a commit is pinned,
  the ref is ambiguous, the network/Git is unavailable, or the commits match.
- If the command prints an update notice, include it once in the first reply,
  or the next progress update if the background result arrives later. Do not
  delay the reply while waiting for it.
- Never run the upgrade or commit on your own initiative; the developer decides.

## Project instructions

- After cloning into a new downstream project, follow the
  [post-clone cleanup](docs/GETTING_STARTED.md#post-clone-cleanup) instructions:
  establish `.template-lock.json`, then remove inherited template-maintenance
  tests, GitHub workflows/Dependabot and historical planning documents. This
  cleanup applies to downstream projects, not the original template repository,
  which retains its tests and CI. Preserve project-specific regression tests
  when cleaning an existing project. Keep `.env`, operational scripts, agent
  guidance, the manifest and upgrade lock.
- `AGENTS.md`, `CLAUDE.md`, `README.md`, and `.agents/rules/agent-safety.md`
  belong to the template; `scripts/team.sh upgrade-template` replaces them.
  Put project-specific agent instructions in `AGENTS.project.md`, project
  rules in `.agents/rules/project.md`, and the project overview in
  `PROJECT.md`. The upgrade never overwrites those files.
- Read `AGENTS.project.md` and `.agents/rules/project.md` after this file.
  When they conflict with this file, ask the user which applies.

## Rules for coding agents

- **Upstream template feedback:** For a confirmed inherited-template issue,
  check [known limitations](docs/known-limitations.md), record sanitized
  evidence and local status in project-owned [TODO.md](TODO.md), and follow
  [the feedback guide](docs/template-feedback.md). Resolve the repository from
  `.template-lock.json`, then `template-manifest.json`; never use the downstream
  `origin`. Search existing upstream issues/PRs first and link a match, or open
  a concise sanitized issue in the verified upstream by default. A focused PR
  requires current-task authorization for its upstream-only commit and push.
  If provenance, access or safe disclosure is unclear, keep the entry queued
  with the reason and continue the safe local work. Never disclose credentials,
  project source/data or full logs; never auto-merge, deploy, or upgrade from a
  report.

- Before reporting a defect or changing behaviour in a review or test run,
  check [docs/known-limitations.md](docs/known-limitations.md). The behaviour
  listed there is deliberate: do not report or "fix" it again unless it no
  longer matches that page.
- Before writing PL/SQL for APEX automations, workflow activities, task
  actions, or background execution chains, read
  `.agents/skills/apex-background/SKILL.md`.
- Read the relevant `.apx`, APEXlang, SQL, and deployment descriptor files
  before editing them. Preserve the app's numeric ID and explicit workspace
  mapping. Do not invent a live workspace, schema, connection, or migration
  result.
- Treat export, migration, publish, and deployment commands according to
  their database effects. Do not run a migration, import, or deployment unless
  the user requested that operation. Export only when requested because it
  refreshes tracked source from shared Builder state.
- Coordinate shared DEV app imports with the human team, except the explicitly
  qualified routine page-only policy above. There are no database
  pause tables or acknowledgement commands in this workflow. Never claim that
  team communication or a live database check happened unless it did.
- Do not commit or push silently. Follow explicit instructions for commits;
  do not push unless asked.
- If SQLcl, the database, or a requested environment is unavailable, report
  the check as unknown or unavailable rather than passed.
- Before writing version-dependent SQL or PL/SQL, obtain database version and
  compatibility information for the intended target through the configured,
  identity-guarded read-only flow. `scripts/team.sh doctor` (or `team.ps1 doctor`)
  reports the full database version and `COMPATIBLE` for each checked DEV target;
  existing migration and schema-comparison observations also record a database
  version, but do not establish its compatibility setting. Do not infer staging
  or production capabilities from DEV. For code intended for several targets,
  use features supported by all of them, within their compatibility settings.
  Use modern SQL and PL/SQL when supported and appropriate; preserve behavior,
  established interfaces and data conventions. Do not rewrite working code only
  to modernize it or assume different constructs have identical semantics.
  Check Oracle documentation or the `db` skill for feature-specific requirements;
  a version observation alone does not prove every feature is enabled or usable.
  If the target is unavailable or its compatibility is unknown, report that and
  ask once for the required compatibility level before choosing version-dependent
  features; continue unrelated work. Never guess or maintain a database-version
  setting in `.env`, and never change database compatibility to fit the code.

See [README.md](README.md) for setup and command examples, and
[migrations/README.md](migrations/README.md) and
[docs/migration-rules.md](docs/migration-rules.md) for the migration contract.
