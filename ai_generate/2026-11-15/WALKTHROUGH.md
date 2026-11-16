# Walkthrough 2026-11-15

## Yes-accepts human stops and automations (26.2 ports)

Ported the 26.1 `--yes` work to the APEX 26.2 template in two pull requests:

- PR #101 (merge `1453958`, commit `f9232a4`): `-y` / `--yes` for every human stop, typed acknowledgments included. This is the 26.2 port of 26.1 PR 69.
- PR #102 (merge `d9e26a2`, commit `5a83af0`): `-y` / `--yes` for `automations enable|disable|run-once`, recorded as `acknowledgedBy`.

### Changes Ported

1. **PR #101: `--yes` replaces the typed stops** (`scripts/rollout.py`, `scripts/rollout_capsule.py`, `scripts/feature_cli.py`, `scripts/migration_resolution.py`, `scripts/release_state.py`, `scripts/release_recovery.py`, `scripts/browser_mcp.py`, `scripts/terminal_confirm.py`, `scripts/team.sh`, `scripts/team.ps1`)
   - `mcp setup -y` / `--yes` accepts the default choice `none` without the menu. An interactive menu on non-interactive input is a clean refusal, not an `EOFError`; this is the Windows fix.
   - The rollout `pause` step: `--yes` accepts the pause at once and the report says no person confirmed the manual step.
   - `rollout --prepare --recover --yes` replaces the typed `RECONCILE <attempt>` acknowledgment. It prints the same evidence a person would read and writes nothing to the target, so it needs no database name. Other `--yes` runs on production still need `--confirm-target`.
   - `feature reconcile-run -y` replaces the typed `RECONCILE-RUN <run>` acknowledgment.
   - `reconcile-migration -y` replaces the typed owner acknowledgment.
   - Reconciliation and migration-resolution records carry an optional `acknowledgedBy` of `--yes` or `terminal`. Records written before this change have none and are read as `terminal`; any other value is refused.
   - `terminal_confirm`: a typed acknowledgment is read only from a terminal. Redirected or piped input is refused with the acknowledgment message.
   - Docs: `AGENTS.md`, `.agents/rules/agent-safety.md`, `README.md`, `docs/migration-attempt-resolution.md`, `docs/feature-releases.md`, `docs/migration-rules.md`, `docs/live-feature-tests.md`, `docs/browser-mcp.md`, `docs/known-limitations.md`.

2. **PR #102: `--yes` for automations** (`scripts/automation_operations.py`, `scripts/team.sh`, `scripts/team.ps1`, `tests/test_automation_operations.py`, `tests/test_documentation_contract.py`)
   - `automations enable|disable|run-once --yes` replaces the typed exact action confirmation. The target and action lines are still printed, and the native-lock, status and identity rechecks still run.
   - `--yes` is refused for any other action. `--confirm-target` is refused with `--env dev`.
   - On `--env prod`, `--yes` alone is refused. `--confirm-target <db_unique_name>` must equal the observed `db_unique_name`.
   - Every automation report carries `acknowledgedBy: --yes` or `acknowledgedBy: terminal`, including refusals.
   - Typed confirmation still needs a terminal; redirected input is refused.
   - Docs: `AGENTS.md`, `.agents/rules/agent-safety.md`, `README.md`, `docs/known-limitations.md`.

### Verification Results

- The two merge commits and their feature commits record no test results, and this walkthrough does not claim any for them.
- The full suite and the pre-push checks run on this branch after both PRs, on 2026-11-16. Their results are in `ai_generate/2026-11-16/WALKTHROUGH.md`.

### Not verified

- No live database, SQLcl session or APEX target was used for either PR. The `--yes` routes are covered by offline tests with fixtures and mocks only.
- The live reconciliation tests remain skipped in the automated suite, as before.

### Decisions for Review

1. `rollout --prepare --recover --yes` on production does not require `--confirm-target`, because preparation writes nothing to the target. Confirm this is the intended boundary.
2. `acknowledgedBy` records the route but does not prevent it. A process with shell access can pass `--yes`; `docs/known-limitations.md` says so and says to keep `-y` out of unreviewed scripts.
