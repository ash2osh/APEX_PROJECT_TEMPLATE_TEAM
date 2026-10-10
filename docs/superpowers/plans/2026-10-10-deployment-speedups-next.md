# Deployment speedups: status and remaining work (handoff)

Branch `feature/deployment-speedups` (template repository). Plan: [2026-10-10-deployment-speedups.md](2026-10-10-deployment-speedups.md). Per-phase results with decisions, test counts and issues: [results/](results/). Origin of every item: the DEV2 to DEV26 rollout of the ABSHRY project (project repository `natrec_team`, `docs/project/abshry/dev26-deployment-log.md` and `dev26-issues.md`).

## Done (committed on this branch; each phase passed the full suite outside the Codex sandbox)

| Phase | Result |
|---|---|
| 1 | Named verification failures, visible and configurable timeouts, batched check sessions, extra safe check functions |
| 2 | `team.sh verify` (read-only evaluation of pre/postconditions) |
| 3 | `team.sh rollout` (manifest runner with hashed inputs, one confirmation, receipts, resume, reports) |
| 4 | `team.sh migrate --rehearse` (rolled-back rehearsal, in-session postconditions, rollback proof) |
| 5 | `team.sh revise` and `revise --check`, honest `apply-not-started` attempt state |
| 6 | Preflight reliability (synonym and grant evidence for views, stable inventory comparison with retries) |
| 7 | `team.sh compare-env` (19 sections, by name, DBA script emission) |
| 8a | `team.sh baseline export-source|export-grants|build` (exact source, idempotent structure delta, grouped grants) |
| 8b | `baseline export-data`, `build --data` (reference data, foreign keys mapped by label, identity advance), `filter-ords --exclude-module`, multi-pass compile-all |

Full suite at the last run: 1,429 tests, OK (32 skipped). 24-33 socket tests cannot run inside the Codex sandbox; run the full suite outside it.

## Remaining

1. ~~Phase 8b~~ done (see the table).
2. **Phase 9**: `team.sh query --env <env> --connection <profile> "<select>"`: literal-aware read-only gate (project `scripts/abshry/ro_sql.py`), output caps, documented in `AGENTS.md` as the required way for agents to read shared databases.
3. **Phase 10**: documentation: `docs/known-limitations.md` rows (SQLcl trailing `-` and blank-line trimming, final-line newline, 19c `IS NULL` bug, no `OVERRIDING SYSTEM VALUE`, 5-minute step guidance, byte comparison across APEX patch levels), `docs/TROUBLESHOOTING.md`, new `docs/production-deployment.md` (pre-window, window, post-window, rollback, timing), agent rules in `AGENTS.md`, qualification notes, a version-aware option for the post-import byte comparison (proposal in `results/RESULT_phase3.md`).
4. **Live verification** (offline tests used fakes only): run `verify`, `migrate --rehearse`, `compare-env`, `baseline export-source` and `rollout` against a disposable schema or DEV26 before relying on them; real SQLcl output formats (`SHOW AUTOCOMMIT`, per-statement feedback) were never observed. Windows PowerShell wrappers were not executed (CI on Windows needed).
5. Review and merge the pull request for this branch (template process); then `scripts/team.sh upgrade-template` in the downstream project.

## How to continue

- Work in a worktree of this branch. Phases were implemented by `codex exec -m gpt-6-luna -c model_reasoning_effort='"max"' --sandbox workspace-write` with a prompt per phase (rules: stdlib only, no database or network, fakes in `tests/fake_sqlcl.py`, update README/docs/help/wrappers/manifest and the documentation, manifest and Windows tests), reviewed and committed by the coordinator.
- Keep `python3 -m unittest discover -s tests` green; remove `/snap/bin` from `PATH` if PowerShell checks hang.

## Further template ideas learned at the end of the DEV26 rollout (not yet in the plan; ranked)

1. **Lint step files for the SQLcl traps before apply** (`check-conflicts` / `migrate` preflight): warn or refuse when a step that creates PL/SQL (package, body, procedure, trigger, type) contains whitespace-only lines (SQLcl trims their blanks, the stored text then differs from the checks; DEV26 I-09 broke two wave-1 folders this way), a line ending in `-` (continuation), or a literal longer than the SQL string limits; suggest the CLOB assembly (`baseline build` already generates it). Also lint check queries for the 19c/26ai traps (`IS NULL` / `NVL` on `USER_SOURCE.TEXT` in large queries).
2. **Durable attempt records.** The attempt evidence that locks a folder after a failed apply lives in the git-ignored `scratch/migration-attempt-*`; removing or recreating a worktree silently unlocks the folder. Store a small `attempt.<env>.json` marker next to the folder (or in a tracked ledger directory) so the lock survives worktrees, and let `revise --check` read it.
3. **Automations lifecycle** (`team.sh automations list|enable|disable|run-once|check`, plus a `rollout` step type `automations` that restores the pre-import states): importing an APEX app disables its automations; `APEX_AUTOMATION.EXECUTE` fails with ORA-01403 unless an APEX session exists (create one with `APEX_SESSION.CREATE_SESSION`). Reference: project `scripts/abshry/dev26/enable_automations.sql`, `run_automation_once.sql`, `check_automation_runs.sql`.
4. **Target APEX version check before an import**: report the target APEX patch level against the source's recorded version in `deploy`/`rollout` preflight (DEV26 ran 26.1.0 while the source came from 26.1.5), and make the post-import byte comparison version-aware (trailing blank lines, comments and message order differ across patch levels; a proposal is in `results/RESULT_phase3.md`).
5. **Template-version drift warning in `doctor`**: a downstream copy of the migration scripts was missing the upstream `LENGTH2` fix and a verification failed on a single emoji; `doctor` should compare the scripts' version stamp with `.template-lock.json` / upstream and say when `upgrade-template` is overdue.
6. **`query` command allow-list** (phase 9): the read-only gate blocks package-qualified calls by design; let a project declare a short allow-list of pure functions (for example a container-name `IS_PROD` check) instead of making agents re-implement their logic in SQL.
7. **Rehearsal of derived-column triggers**: a trigger that recomputes a column on every UPDATE (address service area) turns an innocent reset into a no-op; `migrate --rehearse` reports rows affected, but a "rows whose value changed after the trigger" count per statement would have shown it.
8. **Test-authoring guideline** (docs): acceptance scripts must create their own fixtures inside a savepoint instead of selecting `MIN(...)` from live data, resolve ids by tag/label, derive expected counts from the target, and set `DEFINE OFF`; most DEV26 test failures were DEV2-world assumptions.
9. **Operational notes for agents** (AGENTS.md template): never `pgrep -f`/`pkill -f` with a pattern contained in the command line (kills the shell); Codex sandboxes cannot create sockets, so the Chrome-daemon tests and the full suite must run outside; give a Codex run network access with `-c sandbox_workspace_write.network_access=true` only together with a read-only database gate.
