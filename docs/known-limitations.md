# Known limitations and accepted behaviour

Reviews and end-to-end test runs have reported each item below and the maintainers
decided to keep it. **Do not report these again as defects, and do not "fix" them**,
unless the behaviour no longer matches this page or a change makes the risk worse.
Then report that difference. Each entry says why it is kept and what to do instead.

## Workflow decisions

| Behaviour | Why it is kept | What to do |
| --- | --- | --- |
| Persistent SQLcl sessions reuse database sessions; the reset cannot clear rows in a session-duration global temporary table, application contexts, `SET ROLE` or session events. | A reconnect costs about as much as a SQLcl start against a remote database; generated scripts use none of these. See [sqlcl-sessions.md](sqlcl-sessions.md). | Run such a project script with `SQLCL_PERSISTENT_SESSIONS=0`. |
| On Windows a deployment starts one SQLcl per script. | The persistent session pool uses POSIX named pipes. | Nothing; behaviour is as before the pool. |
| `publish` imports the files in the app folder whether or not they are committed. | The shared DEV database is the source of truth; Git records it. | Commit your edit and the stamped `application.apx` after publishing, before your next `export` of that app (export refuses to write over uncommitted files). |
| Public native application locking allows same-account reentry; raw SQLcl imports bypass another owner's lock. | Measured on APEX 26.2/SQLcl 26.3. The wrappers refuse preexisting locks and verify the run comment/timestamp, but these checks are not a database process mutex. | Use a separate Builder username per developer, coordinate imports, and avoid concurrent publishes from one account. |
| DEV publish refuses an app that does not exist yet. Generated source can still differ from canonical exports. | The public native lock requires an existing app; byte verification remains exact. | Coordinate initial app creation separately, then export canonical source before using DEV publish. See "A new app from `apex generate`" in [publish-rules.md](publish-rules.md). |
| SQLcl can inspect Builder page locks but has no qualified public page-lock setter. | The public view is read-only; page locks are acquired in Builder. Full imports remove page locks, while the qualified selected-page route preserves them. | Follow [partial-publish.md](partial-publish.md). The optional no-notice route requires pre-edit locks, explicit live-matching protection settings and the agreed separate-account workflow; other imports retain coordination. |
| The drift guard cannot see unsaved edits in an open Builder page. | APEX records only saved changes. | Ask teammates before publishing a shared app. |
| A process with shell access can pass `-y` or `--confirm-target` to a staging or production run, or `-y` to `automations enable|disable|run-once`. | A `[y/N]` prompt or action confirmation cannot be answered from a pipe, and `release run` replaces stdin prompts altogether with `--confirm-target <db_unique_name>`. Explicit flags keep target confirmation and action acknowledgment recorded (`confirmedBy` / `acknowledgedBy: --yes`) without making unattended automation impossible; agents supply `--confirm-target` only under the conditions in `AGENTS.md`. | Review the run before executing; keep `-y` out of scripts nobody reviews. |

## Checks and their reach

| Behaviour | Why it is kept | What to do |
| --- | --- | --- |
| A statement run directly in the session uses the login's privileges, so a missing owner grant is not caught for that statement in acts-as-schema mode. | The mode supports logins that cannot use the schema owner's credentials. | Rehearse in an environment with the owner login when owner-grant fidelity for directly-run statements matters; see [login-acts-as-schema.md](login-acts-as-schema.md). |
| `dev-test` and `backup-ords` refuse a login that is not the selected schema owner. | Fixture execution and ORDS export require the actual session user to be the owner; `CURRENT_SCHEMA` does not change `SESSION_USER`. | Use an owner login for those commands; see [dev-test.md](dev-test.md#limits-and-safe-use), [ords-export.md](ords-export.md), and [login-acts-as-schema.md](login-acts-as-schema.md). |
| `release check` and `release run` cannot see pending bundles or uncollected edits in another developer's repository. | Repositories are independent by design; there is no shared ledger. | Coordinate schema and code work; preflight checks the live target as it is. |
| SQLcl prints `APEX_IMPORT_VERIFIED:<id>` even after an import it skipped (an unknown workspace in the descriptor, an unreadable `.apx`). | The marker only proves the app is visible after the import session. The SQL cannot tell a skipped import from a real one, and publish also requires SQLcl's own `Import successful.` line, so such a publish is still refused and nothing is claimed. | Nothing; read the refusal publish prints. |
| `compare-env` source hashes can collide, and catalog conditions at the `SEARCH_CONDITION_VC` length boundary are treated as uncertain. | Oracle `ORA_HASH` is a compact comparison aid, and the catalog view exposes a bounded condition field. The command ignores source line 1 so generated headers do not create false drift. | Review source and DDL directly before release; a condition that may be truncated blocks readiness and must be inspected with an exact DDL/source capture. |
| `compare-env` reports object grants only for configured prefixes and needs separate DBA aliases for privileged catalog sections. | Prefixes keep grant reports scoped to selected application objects. Ordinary schema connections cannot prove direct system privileges, roles, network ACEs, ORDS metadata and installed options. | Configure `<ENV>_DBA_SQLCL_CONNECTION` when those sections are required and set `TABLES_PREFIXES` / `CODE_PREFIXES` to the object families to compare. Treat `not compared (no DBA connection)` as incomplete readiness. |
| `compare-env` cannot compare reference data or application rows, including ERP and camp data. | This command compares structure and access metadata, not environment-specific content. | Compare foreign-key targets by table and column names, and compare reference values by label, not id, with an explicit data check. |
| DEV scenario SQL is not sandboxed and separate fixture names do not isolate database effects. | A reviewed scenario can commit or run DDL, and triggers, autonomous transactions or shared database activity can have indirect effects outside its declared objects. | Use a suitable DEV clone, review the scenario effects and keep fixtures small. The runner does not replace release bundles or APEX publish workflows; see [dev-test.md](dev-test.md#limits-and-safe-use). |
| The DEV test plan cannot guarantee future tablespace allocation from quota or free-space observations. | Space views may be unavailable to the schema owner, and available space can change between planning and execution. | DBA-wide visibility is advisory, not required. If fixture creation hits a quota/allocation refusal, keep the evidence, reduce the test shape if valid, or ask the DBA about the existing schema's space; do not automatically create another schema or replay an ambiguous write. |

## ORDS export

| Behaviour | Why it is kept | What to do |
| --- | --- | --- |
| `backup-ords` refuses a schema that owns an ORDS OAuth client. | SQLcl's `REST export schema` always includes OAuth clients (`p_include_oauth => TRUE`), and the template never exports OAuth clients, secrets or tokens. Removing them from the generated text would be a silent partial export. | Nothing; such a schema cannot be exported until SQLcl can leave OAuth clients out. |
| The ORDS export needs a login as the REST schema owner; a deployment account with `CURRENT_SCHEMA` is refused. | ORDS authorizes the actual session user, which `ALTER SESSION SET CURRENT_SCHEMA` does not change. | Save a connection that logs in as the REST schema owner. Do not enable REST for a deployment account. |
| ORDS is exported twice per schema. | The two exports must be identical; that is how a change during the export is detected without a lock. | Nothing. |
| A mismatch between the dictionary counts and the export's call counts fails the export, even when the export looks fine. | An export that silently holds less than the dictionary lists is worse than no export. | Run it again; if it repeats on a live system, report the entity and counts (the rules are `CALL_RULES` in `scripts/ords_export.py`). |
| The ORDS export was verified live only on ORDS 26.2.3 with SQLcl 26.2.2.0; other ORDS releases, non-REST-enabled and empty schemas, AutoREST objects and Windows were not. | The tests use a scripted fake SQLcl for everything else. | Run `scripts/team.sh doctor` and `backup-ords` against your DEV before relying on it; see [ords-export.md](ords-export.md#limitations-and-what-has-not-been-verified). |
| `backup-ords` lets the dictionary list more roles and privileges than the export holds. | The dictionary includes the roles and privileges that ship with ORDS, which ORDS does not export. | Nothing; an export with more of them than the dictionary lists is still refused. |

## SQLcl and release execution

| Behaviour | Why it is kept | What to do |
| --- | --- | --- |
| SQLcl trims whitespace-only lines in a plain script and treats a trailing hyphen as a continuation of the next line. | The SQLcl script reader normalizes these forms before Oracle sees the statement. | Avoid trailing hyphens in plain SQL scripts where a continuation is not intended. |
| Full app imports may rewrite outer APEXlang comments, trailing horizontal whitespace/EOF blank lines, or the ordering of adjacent supported component blocks across APEX patch levels. Literal payload content and file sets remain exact; ambiguous string boundaries receive no formatting exemption, and `--strict-bytes` remains byte-exact. | Full publish normalizes only the named outer formatting differences. | Review each reported path. For a real difference, preserve recovery evidence and reconcile a fresh target export before retrying. |

## Interrupts and signals

| Behaviour | Why it is kept | What to do |
| --- | --- | --- |
| PowerShell 7 on Linux and macOS ignores cleanup on SIGTERM: `kill` ends `team.ps1`, while the Bash helper or SQLcl it started carries on, and its scratch folder stays. | PowerShell offers no script-level SIGTERM handler. Windows has no SIGTERM, and Linux users have `team.sh`, which hands its process to the helper. | Stop a PowerShell run with Ctrl-C, or signal its process group (`kill -TERM -- -<pgid>`); delete a left-over `scratch/` folder by hand. |
| A Ctrl-C that lands inside `Start-Process` itself, before it returns SQLcl to PowerShell, leaves that SQLcl unmanaged. | PowerShell cannot act on a process it has not been handed yet; the window is a few milliseconds, and SQLcl receives the same Ctrl-C. | Nothing. |
| `kill -9`, closing the terminal window, or a power cut stops any command without cleanup. | No program can clean up after being killed outright. | Treat an interrupted publish or release run as potentially partial; inspect live objects or restore from `scratch/backups/`. |

## Internal scripts

| Behaviour | Why it is kept | What to do |
| --- | --- | --- |
| Helper scripts run directly (`scripts/publish_app.ps1`, `scripts/load_env.ps1`, `scripts/replace_mirror.ps1`, ...) can report a refusal differently from the wrappers. A PowerShell helper's refusal is an exception, so `pwsh` exits 1 and prints its error view. | The supported entry points are `scripts/team.sh` and `scripts/team.ps1`; they print the plain message and give the exit statuses listed in README. | Run commands through the wrappers. |
| A Python module under `scripts/` run as a file (`python3 scripts/release.py --help`, `python3 scripts/verify_local.py ...`) can fail with `ImportError: attempted relative import with no known parent package`. | The modules are one package: they import each other, and the wrappers start them as `python3 -m scripts.<name>` from the repository root. | Use `scripts/team.sh` or `scripts/team.ps1`. To call a module yourself, run `python3 -m scripts.<name>` from the repository root. |
| `safe_rmtree` (Python scratch cleanup) does nothing for a path that is a symbolic link. | It only removes temporary directories the scripts themselves just created, never links, and `shutil.rmtree` never follows links inside a directory. | Nothing. |
