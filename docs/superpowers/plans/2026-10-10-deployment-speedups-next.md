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

Full suite at the last run: 1,413 tests, OK (32 skipped). 24-33 socket tests cannot run inside the Codex sandbox; run the full suite outside it.

## Remaining

1. **Phase 8b**: `baseline export-data` / reference-data build with strict literal formatting and label-based foreign keys (DEV26 issue I-41: copied foreign keys to reference ids must be mapped by label), `baseline filter-ords --exclude-module`, multi-pass compile-all. Reference implementation: project `scripts/abshry/build_env_baseline.py` (`build_reference_data_baseline`), `export_reference_data.py`, `filter_ords_export.py`.
2. **Phase 9**: `team.sh query --env <env> --connection <profile> "<select>"`: literal-aware read-only gate (project `scripts/abshry/ro_sql.py`), output caps, documented in `AGENTS.md` as the required way for agents to read shared databases.
3. **Phase 10**: documentation: `docs/known-limitations.md` rows (SQLcl trailing `-` and blank-line trimming, final-line newline, 19c `IS NULL` bug, no `OVERRIDING SYSTEM VALUE`, 5-minute step guidance, byte comparison across APEX patch levels), `docs/TROUBLESHOOTING.md`, new `docs/production-deployment.md` (pre-window, window, post-window, rollback, timing), agent rules in `AGENTS.md`, qualification notes, a version-aware option for the post-import byte comparison (proposal in `results/RESULT_phase3.md`).
4. **Live verification** (offline tests used fakes only): run `verify`, `migrate --rehearse`, `compare-env`, `baseline export-source` and `rollout` against a disposable schema or DEV26 before relying on them; real SQLcl output formats (`SHOW AUTOCOMMIT`, per-statement feedback) were never observed. Windows PowerShell wrappers were not executed (CI on Windows needed).
5. Review and merge the pull request for this branch (template process); then `scripts/team.sh upgrade-template` in the downstream project.

## How to continue

- Work in a worktree of this branch. Phases were implemented by `codex exec -m gpt-6-luna -c model_reasoning_effort='"max"' --sandbox workspace-write` with a prompt per phase (rules: stdlib only, no database or network, fakes in `tests/fake_sqlcl.py`, update README/docs/help/wrappers/manifest and the documentation, manifest and Windows tests), reviewed and committed by the coordinator.
- Keep `python3 -m unittest discover -s tests` green; remove `/snap/bin` from `PATH` if PowerShell checks hang.
