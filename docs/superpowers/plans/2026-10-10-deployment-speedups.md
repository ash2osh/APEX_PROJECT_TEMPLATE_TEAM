# Implementation plan: faster, safer deployments (template improvements)

Branch: `feature/deployment-speedups`, created from `origin/main` (`e211f4f`, APEX 26.2 template). Target: a production deployment window that is short because
everything that can be done earlier is done earlier, the window itself is one scripted command, and a failure tells you exactly what failed.

Origin: the DEV2 -> DEV26 rollout of the ABSHRY project (project repository `natrec_team`, worktree `/home/ash/projects/natrec_wt/D26`, log
`docs/project/abshry/dev26-deployment-log.md`, steps 1-25). Every item below fixes something that cost real hours there. The project's own tooling in
`/home/ash/projects/natrec_wt/D26/scripts/abshry/` is the reference implementation for the generic features (it works, it is not generic): read it, do not copy it blindly.

## Rules for every phase (apply to the implementer)

1. Python 3 standard library only (the template has no runtime dependencies). LF line endings. No credentials. Pronoun-neutral wording. Tag nothing with names; follow the template's existing style.
2. Work only in this worktree. NO database access, no network, no real SQLcl: all tests use `tests/fake_sqlcl.py` and the other fakes. Do not run `git add/commit` (the coordinator commits after review).
3. Do not change behaviour that existing tests pin without updating those tests deliberately and saying why. The full suite must stay green after every phase: `python3 -m unittest discover -s tests`.
4. Every new script is registered in `template-manifest.json` (template-owned list) and, where the repository has a Windows counterpart pattern (`scripts/*.ps1`, `tests/test_windows_support.py`), gets a Windows wrapper and test. Read `tests/test_template_manifest.py`, `tests/test_documentation_contract.py` and `tests/test_windows_support.py` first and satisfy them.
5. Every user-visible command or behaviour change updates `scripts/team.sh --help`, `README.md` (command list), `docs/migration-rules.md` and, if it is a deliberate limit, `docs/known-limitations.md`. Documentation is part of the phase, not a final clean-up.
6. Safety properties of the migration runner stay intact: frozen payload, identity guard, read-only checks (`ALTER SESSION DISABLE COMMIT IN PROCEDURE`, `SET TRANSACTION READ ONLY`), receipts only after a verified apply, the folder lock after a write attempt, the staging/prod confirmation. New features may add safety, never remove it. Do not edit `SAFE_FUNCTIONS` without a written argument per function in the docs.
7. Each phase ends with `.agent/RESULT_phaseN.md` (not committed): files changed, decisions, test counts, what could not be verified offline.

## Evidence from the field (what the phases must fix)

| # | Observed on DEV26 | Where in the template |
|---|---|---|
| E1 | `committed but fresh verification failed; no receipt` names no failing check; the ids are in a run manifest only if the SQLcl output existed | `scripts/migrate.py` `apply_batch` (record["verificationErrors"] is stored, never printed) |
| E2 | A 1,856-statement file was cut off after 5 minutes with no ORA error; an oversized verification session ended with no output at all | `scripts/sqlcl_session.py` `timeout_seconds=300`; `scripts/migration_checks.py` `_driver_for_checks` (one driver, hex chunks, 9.8 MB for 193 checks) |
| E3 | Exact-text verification needed 2.3 MB of check SQL; no hash function is allowed | `scripts/migration_manifest.py` `SAFE_FUNCTIONS` |
| E4 | Two generator bugs (a name turned into a date; `OVERRIDING SYSTEM VALUE`) were caught only by a hand-made rolled-back dry run | no rehearsal mode |
| E5 | Five structure revisions and three code revisions were needed; every failed attempt locks its folder, also one that failed before any statement ran | `scripts/migrate.py` run manifest `writeAttempted`; `scripts/abshry`-style revision helpers |
| E6 | `check-conflicts` reports `MISSING_PREREQUISITE` for a CREATE VIEW over tables reached through a public synonym, and flaky `LIVE_PREFLIGHT_UNAVAILABLE: owner inventory changed since selector discovery` | `scripts/migration_checks.py` `preflight`, `scripts/schema_catalog*.py` |
| E7 | The gap between two databases was found by hand: constraints/indexes/grants/ACEs/privileges differed in ways the first report (truncated lists, whole-line comparison) got wrong | `scripts/compare_schema.py` (objects only, selected by name) |
| E8 | No way to run a frozen, ordered rollout unattended; every step needed a person and ~15 s per connection | none |
| E9 | The baseline (structure delta, exact stored source, grants, reference data, ORDS filter) was custom per project | none |
| E10 | SQLcl/Oracle traps cost hours: trailing `-` continues a line, whitespace-only lines lose their blanks, the last source line gains a newline, 19c fails with ORA-12801/ORA-01008 on `IS NULL` against `USER_SOURCE.TEXT` in a large query, `NVL` there is 11x slower on 26ai, CLOB `EXECUTE IMMEDIATE` is the way to create a unit with exact blanks | docs |

## Phase 1 - Verification you can trust and read  (E1, E2, E3)

**Goal:** a failed or cut-off apply/verification says what happened; exact-text verification becomes small and fast.

- **1.1 Name the failures.** In `apply_batch`, the "committed but fresh verification failed" error lists the failed check ids (first 20, then a count), each with expected/observed value or error code, and states when no check output existed at all ("verification produced no output: timeout, size limit or SQLcl crash; evidence ..."). `_parse_check_output` returns the failing ids; the run manifest stores them. Add `--verbose` to print all.
- **1.2 Visible timeouts.** `run_sqlcl` raises a dedicated `SqlclTimeout` carrying the phase (apply, preconditions, postconditions, inventory) and the elapsed time; the runner reports "cut off after N s; the folder may be partially applied" for apply and "checks did not finish within N s" for verification. Timeouts become configurable: environment keys `MIGRATION_APPLY_TIMEOUT_SECONDS` and `MIGRATION_CHECK_TIMEOUT_SECONDS` (documented in `.env.example` and `docs/migration-rules.md`), default unchanged (300 s) for existing behaviour but raised to a sensible check default only if you can justify it with the safety argument in the docs.
- **1.3 Batched check execution.** `run_checks` splits a long check list into several SQLcl sessions each below a configurable byte budget (default 2 MB of generated driver), runs them one after the other under the same read-only guarantees, and merges the reports (order and ids preserved, identity checked per session). A single check larger than the budget is an error with its id.
- **1.4 Hash and helper functions in checks.** Extend `SAFE_FUNCTIONS` with deterministic, read-only functions only: `chr`, `ora_hash`, `standard_hash`, `decode`, `nullif`, `instr`, `replace`, `trim`, `ltrim`, `rtrim`, `lengthb`, `lpad`, `rpad`, `listagg` (check `validate_check_query` for aggregate handling). Each addition gets a test (accepted) and a docs line; keep `FORBIDDEN_WORDS` behaviour. Add a documented pattern "fingerprint check" to `docs/migration-rules.md`: compare `COUNT(*)` and `SUM(ORA_HASH(...))` over `USER_SOURCE` lines instead of repeating the text, with the caveat that `ORA_HASH` of the same text is stable across 19c and 26ai (state the evidence you can give offline; list what must be verified live).
- **Tests:** fake SQLcl scenarios for: verification with failing checks (message lists ids), no output (message explains), timeout (SqlclTimeout path), batched sessions merged, a check over the budget rejected, each new function accepted/rejected correctly.
- **Acceptance:** `migrate` never prints a bare "verification failed" again; a 200-check folder with 24 KB checks produces several small sessions.

## Phase 2 - `verify`: read-only evaluation without applying  (E1, E8)

- New `team.sh verify <folder> [...] --env <env> [--phase pre|post|both] [--only-failed] [--format text|json] [--jobs N]`: loads migrations, runs their preconditions and/or postconditions read-only (same driver as the runner, batched per Phase 1.3), prints a table (phase, id, expected, observed, status) and a summary, exit 0 only if everything requested is true, 1 if any false, 2 on error. `--jobs N` runs sessions in parallel (default 1).
- Reuse `run_checks`; do not duplicate guards. Honour `--schema`, multi-schema folders, staging/prod resolution through `db_targets.py`.
- Reference: `D26/scripts/abshry/evaluate_checks_readonly.py` (batches 25 statements per connection, marker rows).
- Windows wrapper, docs, tests (fake SQLcl): all true, some false, error in one check, JSON output schema.

## Phase 3 - Rollout command for the window  (E8)

- New `team.sh rollout <manifest.json> --env <env> [--from-step N] [--dry-run] [--report <file>] [--yes-for-all]`. The manifest (JSON, schema documented and validated) lists ordered steps of types `migrate` (folders), `verify` (folders, phase), `sql-script` (a reviewed script run with the same identity guard as the DBA scripts, for DBA prerequisites), `ords-import` (a file, optional module exclusions), `app-deploy` (numeric app id), `pause` (message). Execution per step: preflight, confirmation once at the start (staging/prod) listing every step with its SHA-256, apply, verify, timing; stop at the first failure; write a report (JSON and Markdown) with start/end times, durations and evidence paths; `--from-step` resumes only after verifying that the previous steps' receipts exist and match.
- Safety: the manifest and every referenced file are hashed at start; any change during the run aborts. No new bypass of the per-folder confirmation: one explicit confirmation for the whole manifest is acceptable only if the manifest and its hashes were shown.
- Reference: `D26/docs/project/abshry/dev26-deployment-log.md` "Order of execution".
- Tests: step ordering, stop-on-failure, resume, hash change aborts, dry-run prints plan, report content, Windows wrapper.

## Phase 4 - Rehearsal mode  (E4)

- `team.sh migrate ... --rehearse`: for each folder, run the data-changing files inside one transaction on the real target and `ROLLBACK` at the end, report rows affected per statement where SQLcl prints them, and prove the rollback by re-running the folder's own preconditions afterwards (they must still hold). Folders or files containing DDL / statements that commit implicitly are classified with the existing analyzer (`analyze_batch`) and are NOT executed; the report names them as "not rehearsable" with the reason. Rehearsal never writes a receipt or a write-attempt marker, never takes the confirmation shortcut for prod (it still requires it).
- Guard: refuse if the session would autocommit (`SET AUTOCOMMIT OFF`, `WHENEVER SQLERROR EXIT FAILURE ROLLBACK`, verify with the identity guard).
- Tests with fake SQLcl: rehearsable DML folder, DDL file refused, error mid-file reported and rolled back, receipts untouched.
- Reference: the hand-made `dryrun_ref.sql` approach in the D26 log (steps 21, 23).

## Phase 5 - Revision handling and honest attempt state  (E5)

- `team.sh revise <folder>`: copies the folder to the next revision number (`-rNNN` + 1), keeps file names and numbering, rewrites nothing else, adds a first line to the new README "Supersedes <old>: <reason from --reason>", leaves the old folder untouched, refuses if a newer revision already exists. Print the migration order hint.
- Attempt state: after a failed apply, classify from the SQLcl output whether any payload file started (the identity-guard marker is present and `MIGRATION_APPLY_COMPLETED` absent, but no payload statement ran) and record `apply-not-started` instead of `apply-failed-or-unknown`; a folder in `apply-not-started` is not locked (it did not touch the database). Everything else stays locked. This must be conservative: when in doubt, locked. Document the exact rule and its limits.
- Tests: revise creates revision 2 and 3 correctly, refuses double revise, README header, lock rules for each recorded state.

## Phase 6 - Preflight reliability  (E6)

- **6.1** `check-conflicts` / the runner's preflight: a CREATE VIEW (and other dependency checks) whose referenced table is not in the owner's namespace but is reachable through a PUBLIC synonym or a synonym in the owner schema, to an existing object the owner can select (grant evidence), is satisfied; the catalog capture must include synonyms and grants for this (extend `schema_catalog` capture if needed, keeping it read-only). Keep reporting a real missing object.
- **6.2** "owner inventory changed since selector discovery": compare the inventory between the two captures ignoring volatile attributes (status, last DDL time caused by automatic recompilation), fail only when object names or types changed; retry the capture up to a configurable number of times (default 3) before reporting `LIVE_PREFLIGHT_UNAVAILABLE`.
- Tests: view over a synonym passes, over a missing table fails; inventory with only status changes passes; with a new object retries then fails; use recorded catalog fixtures (`tests/fixtures`).

## Phase 7 - `compare-env`: find the whole gap by name  (E7)

- New `team.sh compare-env --from <env> --to <env> [--section ...] [--format text|json|markdown] [--emit-dba-script <file>]`. Sections, all compared BY NAME and never by whole line (system-generated names differ between databases), complete and untruncated (page the results, fail loudly if a cap is hit): tables and columns (type, length, semantics, nullability), constraints (type, columns, referenced table, condition, status, validated), indexes (name and column list, uniqueness), triggers, sequences, synonyms (public and schema), views, stored code units (name, type, status, line count, SUM(ORA_HASH) of the lines), invalid objects, identity columns, object grants (grantee, privilege, grantable) for selected prefixes, system privileges and roles of the configured schemas, network ACEs, ORDS modules/templates/handlers, Java and MLE objects, installed options (JVM, Spatial), versions of database and APEX.
- DBA-level sections need an optional privileged read-only connection per environment: new optional `.env` keys `<ENV>_DBA_SQLCL_CONNECTION` (documented, never required, never used for writes). Without it those sections are reported as "not compared (no DBA connection)".
- Output: a readiness report grouped as blockers / differences / identical, and `--emit-dba-script` writes a reviewed SQL script (grants, ACEs, enable ORDS) that the `rollout` command can run as a `sql-script` step. Differences are classified (missing on target, different, only on target) and the report states what is deliberately not compared.
- Extend `scripts/compare_schema.py` structure and its tests rather than starting a parallel tool; keep the existing `compare-schema` behaviour.
- Reference: D26 comparison queries in the log steps 4, 5, 14, 17, 18, 19 and the corrected claims of the first gap report.
- Tests with recorded catalog fixtures: each difference class detected, name-based matching of system-generated constraint names, truncation guard.

## Phase 8 - Baseline tooling as a template feature  (E9)

- New `team.sh baseline <subcommand>` driven by a project file `baseline.json` (schemas, prefixes, reference-data allow-list with excluded columns, ORDS modules to exclude, grant grantees to skip/keep options, test-user policy default "none"):
  - `baseline export-source --from <env>`: exact stored text and compiler settings of the configured units (`USER_SOURCE` lines exactly, `ALL_PLSQL_OBJECT_SETTINGS`), as JSON files under `database/<SCHEMA>/stored-source/` or a scratch directory; read-only. (Reference: `export_stored_source.py`.)
  - `baseline export-grants --from <env>` (reference: `export_object_grants.py`), `baseline export-data --from <env>` (allow-listed tables, JSON rows, no sensitive columns; reference: `export_reference_data.py`).
  - `baseline build --to <env>`: compares with Phase 7, then generates migration folders: idempotent structure delta (guarded creates; existing-by-name constraints replaced when the definition differs; indexes skipped when the same column list exists; NOT NULL after ORA-01442 handled; sequences and identities moved past seeded ids), object grants (grouped statements, each step well below the 300 s limit), exact-source code units with per-unit session settings and the PL/SQL CLOB form for units with whitespace-only lines, reference data with strict literal formatting (dates only for full ISO values, identity-always columns left to the database, empty-string lines rejected), the multi-pass compile-all, and name-based fast checks (no `IS NULL`/`NVL` on source text; final-line tolerance). Next free revision numbers; never touches attempted folders.
  - `baseline filter-ords --exclude-module NAME` (reference: `filter_ords_export.py`).
- Everything the project tooling learned goes in as tests: the SQLcl hyphen continuation (literal newlines after a trailing `-`), blank-line trimming, final-line newline, 19c `IS NULL` failure, `OVERRIDING SYSTEM VALUE`, date detection, q-quote delimiter collision, 32-character chunking.
- Keep this phase split into reviewable sub-steps 8.1 export, 8.2 structure/grants build, 8.3 code build, 8.4 data build, 8.5 ORDS filter, each with its own tests and docs.

## Phase 9 - Read-only ad-hoc queries  (E7, tooling)

- `team.sh query --env <env> --connection <profile> "<select>"` or `--file`: the literal-aware read-only gate from `D26/scripts/abshry/ro_sql.py` (SELECT/WITH only; comments and `;` handled outside string literals; DML/DDL/PL-SQL/packages/db links blocked; read-only transaction; `set define off`; output cap; embedded newlines in literals kept as explicit `CHR(10)` because of the SQLcl hyphen continuation). It is the single place where ad-hoc read-only access to a shared database is allowed for agents; document it in `AGENTS.md` rules for coding agents as the required way, with the SQLcl MCP/CLI direct use discouraged for shared targets.
- Tests: every blocked form, literal edge cases (`--`, `;`, quotes, newlines, `&`).

## Phase 10 - Documentation, runbook, qualification

- `docs/known-limitations.md`: new rows for E10 with "why kept / what to do". `docs/TROUBLESHOOTING.md`: the symptoms above with causes and fixes.
- New `docs/production-deployment.md`: pre-window (compare-env, baseline build, rehearse, verify, DBA script, restore point), window (one `rollout` command), post-window (exhaustive verify, browser checks), rollback (restore point), timing expectations and a checklist template. Link from README and `docs/migration-rules.md`.
- `AGENTS.md` / agent rules: use `verify`, `rehearse` and `query` instead of ad-hoc SQL; never reduce a check to make it pass.
- Update `docs/local-qualification.md` and the 26.2 qualification note with the new commands; register all new files in `template-manifest.json`; run the full suite and the template qualification tests.
- Final: a summary of command changes for the release notes.

## Order and review cycle

Phases 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8 (8.1-8.5) -> 9 -> 10. One implementer run per phase (a sub-step for phase 8). After each: the coordinator reads the result, the diff and the tests, runs the full suite, rejects anything that weakens a safety property or edits a template-owned validator without the written argument, then commits (one commit per phase). Nothing is pushed and no PR is opened until the owner agrees; the branch ends as a series of reviewable commits (or several PRs if the owner prefers them split per phase).
