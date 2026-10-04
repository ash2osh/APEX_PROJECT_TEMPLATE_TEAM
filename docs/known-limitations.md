# Known limitations and accepted behaviour

Reviews and end-to-end test runs have reported each item below and the maintainers
decided to keep it. **Do not report these again as defects, and do not "fix" them**,
unless the behaviour no longer matches this page or a change makes the risk worse.
Then report that difference. Each entry says why it is kept and what to do instead.

## Workflow decisions

| Behaviour | Why it is kept | What to do |
| --- | --- | --- |
| `publish` imports the files in the app folder whether or not they are committed. | The shared DEV database is the source of truth; Git records it. | Commit your edit and the stamped `application.apx` after publishing, before your next `export` of that app (export refuses to write over uncommitted files). |
| Two developers publishing the same app at the same instant can both import; there is no lock. | `AGENTS.md` forbids team mutexes, checkout rosters and metadata tables. | Tell the team which app you are publishing. The drift guard catches every import that is not simultaneous. |
| The first publish of an app made with `apex generate` stops at the post-import byte check and records no baseline. | Loosening the exact-bytes check for that case would weaken the main safety guard. | Follow "A new app from `apex generate`" in [publish-rules.md](publish-rules.md): commit, export, commit, publish again. |
| The drift guard cannot see unsaved edits in an open Builder page. | APEX records only saved changes. | Ask teammates before publishing a shared app. |

## Checks and their reach

| Behaviour | Why it is kept | What to do |
| --- | --- | --- |
| `check-conflicts` and `migrate` cannot see migrations that are pending in another developer's repository. | Repositories are independent by design; there is no shared ledger. | Coordinate schema work; preflight checks the live schema as it is. |
| `check-conflicts --local` does not enforce the schema-folder layout rule (flat folder in a multi-schema project). | `--local` loads no `.env`, so it does not know how many schemas are configured. It still refuses a `--schema` that differs from the folder and a batch that mixes schemas. | Run `check-conflicts --env dev` for the full check. |
| SQLcl prints `APEX_IMPORT_VERIFIED:<id>` even after an import it skipped (an unknown workspace in the descriptor, an unreadable `.apx`). | The marker only proves the app is visible after the import session. The SQL cannot tell a skipped import from a real one, and publish also requires SQLcl's own `Import successful.` line, so such a publish is still refused and nothing is claimed. | Nothing; read the refusal publish prints. |
| SQL that the analyzer cannot model (for example `ALTER SYSTEM`, `DBMS_*` calls, data changes) passes preflight when its folder declares checks. | Only the reviewed `checks.json` can vouch for it; preflight lists it as `REVIEW:`. | Review that SQL and its checks together before you migrate. |

## ORDS export

| Behaviour | Why it is kept | What to do |
| --- | --- | --- |
| `backup-ords` refuses a schema that owns an ORDS OAuth client. | SQLcl's `REST export schema` always includes OAuth clients (`p_include_oauth => TRUE`), and the template never exports OAuth clients, secrets or tokens. Removing them from the generated text would be a silent partial export. | Nothing; such a schema cannot be exported until SQLcl can leave OAuth clients out. |
| The ORDS export needs a login as the REST schema owner; a deployment account with `CURRENT_SCHEMA` is refused. | ORDS authorizes the actual session user, which `ALTER SESSION SET CURRENT_SCHEMA` does not change. | Save a connection that logs in as the REST schema owner. Do not enable REST for a deployment account. |
| ORDS is exported twice per schema. | The two exports must be identical; that is how a change during the export is detected without a lock. | Nothing. |
| A mismatch between the dictionary counts and the export's call counts fails the export, even when the export looks fine. | An export that silently holds less than the dictionary lists is worse than no export. | Run it again; if it repeats on a live system, report the entity and counts (the rules are `CALL_RULES` in `scripts/ords_export.py`). |
| The ORDS export was verified live only on ORDS 26.2.3 with SQLcl 26.2.2.0; other ORDS releases, non-REST-enabled and empty schemas, AutoREST objects and Windows were not. | The tests use a scripted fake SQLcl for everything else. | Run `scripts/team.sh doctor` and `backup-ords` against your DEV before relying on it; see [ords-export.md](ords-export.md#limitations-and-what-has-not-been-verified). |
| `backup-ords` lets the dictionary list more roles and privileges than the export holds. | The dictionary includes the roles and privileges that ship with ORDS, which ORDS does not export. | Nothing; an export with more of them than the dictionary lists is still refused. |

## Interrupts and signals

| Behaviour | Why it is kept | What to do |
| --- | --- | --- |
| `migrate` interrupted while a SQL step runs exits 2 ("may be partially applied"), not 130. | The result is unknown, not merely interrupted; 2 tells scripts not to treat it as a clean stop. | Reconcile as [migration-rules.md](migration-rules.md) says before retrying. |
| PowerShell 7 on Linux and macOS ignores cleanup on SIGTERM: `kill` ends `team.ps1`, while the Bash helper or SQLcl it started carries on, and its scratch folder stays. | PowerShell offers no script-level SIGTERM handler. Windows has no SIGTERM, and Linux users have `team.sh`, which hands its process to the helper. | Stop a PowerShell run with Ctrl-C, or signal its process group (`kill -TERM -- -<pgid>`); delete a left-over `scratch/` folder by hand. |
| A Ctrl-C that lands inside `Start-Process` itself, before it returns SQLcl to PowerShell, leaves that SQLcl unmanaged. | PowerShell cannot act on a process it has not been handed yet; the window is a few milliseconds, and SQLcl receives the same Ctrl-C. | Nothing. |
| `kill -9`, closing the terminal window, or a power cut stops any command without cleanup. | No program can clean up after being killed outright. | Treat a publish as an unverified import and a migration as possibly partial; see the publish and migration rules. |

## Internal scripts

| Behaviour | Why it is kept | What to do |
| --- | --- | --- |
| Helper scripts run directly (`scripts/publish_app.ps1`, `scripts/load_env.ps1`, `scripts/replace_mirror.ps1`, ...) can report a refusal differently from the wrappers. A PowerShell helper's refusal is an exception, so `pwsh` exits 1 and prints its error view. | The supported entry points are `scripts/team.sh` and `scripts/team.ps1`; they print the plain message and give the exit statuses listed in README. | Run commands through the wrappers. |
| `safe_rmtree` (Python scratch cleanup) does nothing for a path that is a symbolic link. | It only removes temporary directories the scripts themselves just created, never links, and `shutil.rmtree` never follows links inside a directory. | Nothing. |
