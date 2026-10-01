# Troubleshooting

Find the message you saw, then read what it means and what to do. Messages are
quoted as the scripts print them; `<...>` marks a value that differs for you.
For the full list of publish refusals see [publish-rules.md](publish-rules.md);
for migration rules see [migration-rules.md](migration-rules.md).

## Setup and connection

| You see | What it means | What to do |
| --- | --- | --- |
| `configuration file not found: ... (copy .env.example to .env)` | There is no `.env` yet. | `cp .env.example .env`, then edit it. |
| `project environment error: PROJECT_NAME is required in .env` (or another setting) | A required setting is missing or empty. | Copy the missing line from `.env.example`. |
| `unsupported setting in .env: <KEY>` | `.env` contains a setting this template does not know, often a leftover from an older version. | Delete that line. Compare with `.env.example`. |
| `project environment error: DEVELOPER_NAME must be uppercase letters, digits, or underscores (at most 30), such as ASHARIF` | The name is lowercase or has other characters. | Use capitals, for example `ALICE`. |
| `DB_ENVIRONMENT must be development, test, staging, or production` | `DB_ENVIRONMENT` holds anything else, including a different case (`Development`). Both loaders compare it exactly. | Use one of the four values in lowercase. |
| `<KEY> must be an uppercase Oracle identifier`, `<KEY> contains unsupported characters`, or `--schema must be an uppercase Oracle identifier` | A schema or prefix holds a character outside `A-Z`, `0-9`, `_`, `$`, `#`; a saved connection name may also use lowercase letters, `.` and `-` but nothing else. Accented letters and other non-ASCII characters are refused in both shells. | Use an ASCII name; for a saved connection, rename it with `connmgr`. |
| `team error: unknown command '<word>'; use --help to list commands` | The first word after `team.sh` or `team.ps1` is not a command. An option such as `--schema DEMO` must come after the command: `team.sh doctor --schema DEMO`. | Put the command first, or run `team.sh --help`. |
| `Unknown connection <name>` (followed by `team error: N of M doctor check(s) failed`) | SQLcl printed this: the saved connection named in `.env` does not exist, often a typo. | Run `sql /nolog`, then `connmgr list`, and correct the matching `*_SQLCL_CONNECTION` value, or save the connection (step 5 of the getting started guide). |
| `project environment error: .env is UTF-16; save it as UTF-8` | `.env` was written by Windows PowerShell 5.1 (`>` or `Out-File`) or an old Notepad "Unicode" save. Bash cannot read it and PowerShell would decode it differently, so both loaders refuse it. | Re-save it as UTF-8 (a byte-order mark is fine), or create it with `Copy-Item .env.example .env`. |
| `<KEY> has an inline comment` | A value in `.env` is followed by `# ...` on the same line. | Move the comment to its own line. |
| `<KEY> must list the same number of entries` | The schema, connection and user settings of one profile have different numbers of comma-separated values. | Give all three the same number of entries, in the same order. |
| `ORA-12541: TNS:no listener` or `Connection refused` | The database is not running or not reachable. | Start it (with the local setup, from the `uc-local-apex-dev` folder you created in step 3 of the getting started guide: `./local-26ai.sh start`) and retry. |
| `ORA-28000: the account is locked` / "Account Is Locked" | The schema's password expired or was locked. | With the local setup, from the `uc-local-apex-dev` folder: `./local-26ai.sh unexpire-accounts`. Otherwise ask your DBA. |
| `Expected session user <X> but found <Y>` | The saved connection logs in as a different user than `*_EXPECTED_USER` says. | Fix the saved connection or the `*_EXPECTED_USER` value. This check prevents working in the wrong schema. |
| `Target schema does not exist or is not visible: <X>` | The schema name in `.env` is wrong, or that user cannot see it. | Correct the `*_SCHEMA` value. |
| `<profile> connection '<name>' resembles production but DB_ENVIRONMENT=development` | The saved connection's name looks like production: `prod`, `prd`, `production` or `live` as a word, or names like `PRODDB` and `ERPPROD`. Pre-production names such as `PREPROD` and `NONPROD` do not count. The database, unique and service names are checked the same way once connected. | Rename the saved connection, or, if it really is production, set `DB_ENVIRONMENT=production` (which makes the scripts read-only). |
| `sql: command not found` | SQLcl is not installed or not on your `PATH`. | Install SQLcl 26.1 or newer and open a new terminal. |
| `local: -n: invalid option` (or other odd Bash errors) on macOS | macOS's built-in Bash (3.2) is too old. | `brew install bash`, then run the scripts with it. |
| `Bash is required for '<script>.sh'` from `scripts/team.ps1` on Windows | The migration, comparison and deployment helpers run in Bash. `team.ps1` looks for Git Bash next to `git.exe` and in Git for Windows' usual install folders, and skips the WSL `bash.exe`, which cannot run a Windows path. None was found. | Install Git for Windows, or set `TEAM_BASH` to the full path of its `bash.exe` (for example `$env:TEAM_BASH = 'C:\Program Files\Git\bin\bash.exe'`), or run `scripts/team.sh` from Git Bash. |

## Export and backup

| You see | What it means | What to do |
| --- | --- | --- |
| `refusing to export over dirty mirror: apps/<SCHEMA>/<id>` | You have uncommitted changes in that app's folder. Export would overwrite them. | Commit or stash them, then export again. |
| `refusing to back up over dirty mirror: database/<SCHEMA>` | Same, for the database copy. | Commit or discard the changes there, then run `backup-db` again. |
| `refusing to replace mirror with ignored local files that would be deleted` | A Git-ignored file sits inside the mirror folder. Replacing the mirror would delete it without Git noticing. | Move the listed files out of `apps/<SCHEMA>/<id>/` or `database/<SCHEMA>/`, then retry. |
| `MIRROR_LOCK_STALE_SECONDS must be a whole number of seconds, at least 60` | The lock timeout override is too short or not a number. | Unset it, or set it to 60 or more. |
| `application <id> was not found in the workspace visible to this connection` | The app ID is wrong, or the connection cannot see that workspace. | Check `APEX_APP_ID` and the connection. |
| `... is parsed by <X>, which is not listed in APEX_PARSING_SCHEMA` | The app belongs to a schema you have not configured. | Add that schema (with its connection and user) to the `APEX_*` settings. |
| export refuses because the app's last update matches "the current database second" | The app changed in the same second, so the revision is ambiguous. | Wait one second and run it again. |
| `database backup is incomplete for <SCHEMA>` or `...manifest ... is missing the <TYPE> row` | SQLcl did not write every object it listed, so nothing was installed. | Run `backup-db` again. If it repeats, check disk space and SQLcl output. |

## Publish

| You see | What it means | What to do |
| --- | --- | --- |
| `[DRIFT DETECTED] Live APEX App <id> was modified in Builder on ...` | Someone changed the live app after your last export. | `scripts/team.sh export <id>` (`scripts/team.ps1 export <id>` in PowerShell; the message names the one you ran), review and merge, publish again. Use `--force` only after reviewing the difference. |
| `[DRIFT UNKNOWN] Database export baseline is unavailable` | No export has recorded this app's state yet. | Run `scripts/team.sh export <id>` first. |
| `deployment descriptor not found: ...` | `deployments/dev.json` is missing. | Copy `apps/templates/deployments/dev.json` and fill it in. |
| `application <id> is stored under apps/<A> but its descriptor parses as <B>` | The folder name and the descriptor disagree about the schema (several schemas only). | Move the folder, or fix `parsingSchema` in the descriptor. |
| `application <id> is parsed by <A>, not the descriptor's <B>` | The live app belongs to a different schema than your descriptor says. | Fix the descriptor, or publish from the right app folder. |
| `ORA-20016: Live application changed after the Builder drift check` | Someone saved or imported the app in the moments between the drift check and the import. Nothing was imported. | `scripts/team.sh export <id>`, review and merge, publish again. |
| `the application path contains characters SQLcl cannot pass` | The repository path contains `'`, `"` or `&`. | Move the checkout to a path without them. |
| `SQLcl did not report a successful APEX import` | SQLcl exited without importing, for example because the workspace name in the descriptor is wrong. | Check `workspace.name` in the descriptor. |
| `APEXlang source bytes do not match the post-import re-export` | APEX normalized the source on import. | Run `export`, commit the canonical source, and publish again. |

## Migrations

| You see | What it means | What to do |
| --- | --- | --- |
| `checks.json must contain exactly schemaVersion, preconditions, and postconditions` | The file has a missing or extra key. | Match the layout in [GETTING_STARTED.md](GETTING_STARTED.md#10-make-your-first-database-change). |
| `check query calls unsupported or user-defined function <NAME>` | A check calls a function outside the allowed read-only set, or it is a function you wrote. Ordinary SQL keywords before a parenthesis (`AND (...)`, `FROM (subquery)`) are fine. | Rewrite the check with `CASE` and the functions listed for checks in [migration-rules.md](migration-rules.md). |
| `checks must contain exactly one SQL query and no SQLcl terminator` | A check ends with `;` or the SQL*Plus habit of a trailing `/`, or holds a second statement. A check is one `SELECT` that Oracle parses itself; `/` is fine as the division operator. | Remove the `;` or `/` after the query, and keep one query per check. |
| `check query has a non-ASCII character outside a quoted value` | An accented or other non-ASCII letter stands in a check outside a string literal, a quoted name or a comment. Oracle allows it in an unquoted name, which the validator cannot read safely. | Put the text in a string literal, or quote the name, for example `"CAFÉ"`. |
| `... expected value must be integer 1` | A check's `expected` is not `1`. | Every check is a query returning `1`; set `"expected": 1`. |
| `INCOMPLETE [LIVE_PREREQUISITE_UNKNOWN]` from `--local` | Offline analysis cannot know what exists in the database. | Expected. Use `--env dev` for a real answer. |
| `CONFLICT [COLUMN_ALREADY_EXISTS]` (or `LIVE_NAMESPACE_OCCUPIED`) | The object your change creates is already there. | The change may already be applied. Inspect the database; do not re-apply. |
| `CONFLICT [CHECK_FAILED]: check must return exactly one row and one numeric column equal to 1` | A precondition is false, for example the column already exists. | Read the check's `id`, then fix the data or the migration. |
| `SQL-only migration rejects SQLcl/client command or unsupported statement '<WORD>'` | The file holds a SQLcl command such as `HOST`, `SPOOL`, `DEFINE`, `START`, `PROMPT`, `WHENEVER` or `SET` (other than `SET CONSTRAINTS` and `SET TRANSACTION`), or a statement the runner does not run. Migration files hold SQL only. | Remove the command; the apply session sets what it needs. Put anything else in a reviewed migration of its own. |
| `SQL-only migration rejects a line holding only '/' (or '.') inside a SQL statement` | SQLcl ends a SQL statement at such a line, then runs the next line as a command, so the file would not run as written. | Remove the line. End a plain statement with `;`; use `/` only after a PL/SQL block, type, library, Java source or MLE module. |
| `SQL-only migration rejects a line starting with '@'` | SQLcl would splice the named file into the statement, bytes that were never reviewed or hashed. | Put the SQL in the migration file itself. |
| `SQL-only migration rejects a quoted identifier that spans lines` | SQLcl does not keep a quoted identifier together across lines. | Keep the identifier on one line. |
| `SQL-only migration rejects '<word>' ...: SQLcl reads a statement's first word up to the next space` | A statement's first word has something glued to it (`DECLARE,`, `SELECT(`, `BEGIN/**/`). SQLcl does not recognize it as that statement and runs the lines after it one by one. | Put a space after the first word. |
| `SQL-only migration rejects the comment opener '/*/'` | SQLcl's parser fails on `/*/` and then runs the comment's own lines as commands. | Write `/* /` or `/**/`. |
| `SQL-only migration rejects a quoted value outside any statement` | A string or quoted name stands between two statements, usually a stray quote or a missing `;`. SQLcl reads it as a command and may skip the next statement. | Remove it, or finish the previous statement with `;`. |
| `SQL-only migration rejects a line holding only '/' inside a comment` | SQLcl ends the statement or block at that line even inside a comment. | Remove the line or reword the comment. |
| `<migration> already has a verified dev receipt` | It was already applied and verified. | Nothing to do. For a follow-up change, create the next revision folder. |
| `<migration> may be partially applied; stop and reconcile` (or `<migration> was interrupted and may be partially applied`, or `migration interrupted after a write was attempted`) | The apply failed part-way, or you pressed Ctrl-C while it ran. The attempt is recorded, so another `migrate` of the folder refuses until you reconcile. | Inspect the database, then fix forward with a new revision. Never edit the failed folder. An interrupt before any write (`no writes were attempted`) changed nothing; run the command again. |
| `ORA-20986: Migration left objects with compilation errors or that it cannot see: ...` (with `may be partially applied`) | A package, procedure, trigger or view in the migration compiled with errors, or, marked `(not found or not visible)`, the session cannot see it (usually a unit created in another schema). The DDL ran, but no receipt was written. | Fix the code in the next revision folder and apply it. For a unit in another schema, move that change to that schema's migration folder. |
| `ORA-20987: Migration apply session differs from the preflight target` | The saved connection reached a different database than the preflight did. Nothing was run. | Check the saved connection and retry. |
| `migration family <name> exists in both migrations/ and migrations/<SCHEMA>/, which both target schema <SCHEMA>` | The same migration name is used in the flat layout and in the folder of the schema flat migrations target. | Keep the family in one layout; rename or move the newer folder before any write attempt. |
| `several schemas are configured, so migrations must live under migrations/<SCHEMA>/` | A flat migration folder in a multi-schema project. | Move it to `migrations/<SCHEMA>/`. |
| `selected migrations belong to different schemas; run one schema at a time` | One command listed folders for two schemas. | Run separate commands. |
| `--schema <X> does not match the migration folder's schema <Y>` | The option and the folder disagree. | Drop `--schema`, or use the right folder. |
| `LIVE_PREFLIGHT_UNAVAILABLE` | The live check could not read the catalog. | Read the message after it. Run `doctor` to check the connection. |

## Several schemas

| You see | What it means | What to do |
| --- | --- | --- |
| `<command> needs one schema because several are configured (<list>); pass --schema <NAME>` | The command works on one schema at a time. | Add `--schema <NAME>`. |
| `schema <X> is not configured; configured schemas: ...` | `--schema` names a schema that is not in `.env`. | Use one of the listed names. Schema names are uppercase. |
| `schema <X> is not listed in STAGING_SCHEMA` (or `PROD_SCHEMA`) | The schema is not set up for that target. | Add it, with the same name, to the `STAGING_*` or `PROD_*` settings. |

## Knowledge graph

| You see | What it means | What to do |
| --- | --- | --- |
| `installed extractor is missing` | Graphify was installed or upgraded after setup last ran. | `python3 scripts/setup_graphify_apx.py`. Rerun it after every Graphify upgrade. |
| Pages show as unconnected stubs | Graphify cached results from before the database copy existed. | `scripts/team.sh backup-db`, then `python3 scripts/setup_graphify_apx.py` and `graphify update .`. |

## Still stuck?

Run `scripts/team.sh doctor` and read every line: it prints which connection and
schema it used. Keep the exact message, because the scripts are written so the
text says what to do next. If an AI assistant is helping, paste the full output.
