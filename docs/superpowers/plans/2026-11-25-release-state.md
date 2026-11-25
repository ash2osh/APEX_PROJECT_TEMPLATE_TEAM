# Release state per environment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (owner chose inline execution, no subagents). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each release bundle records per environment what it applied (`releases/<slug>/state/<env>.json`), status goes `draft` → `staged` → `deployed`, DEV/staging reruns apply only changed steps, and production requires a matching staging record.

**Architecture:** Pure state logic (fingerprints, load/write, step selection, effective status, staging comparison, status table) lives in a new module `scripts/release_state.py`. `scripts/release.py` calls it from `_preflight_bundle` (selection, drift, prod gate), `cmd_run` (filtered apply, app skip, live hash capture, state and status write), `cmd_collect` (back to draft), `cmd_list`, `cmd_check` and a new `cmd_status`.

**Tech Stack:** Python 3 standard library, SQLcl drivers already in `release.py`, `unittest` with `tests/fake_sqlcl`.

**Spec:** `docs/superpowers/specs/2026-11-25-release-state-design.md`

## Global Constraints

- State files: `releases/<slug>/state/dev.json`, `state/staging.json`, `state/prod.json`; written only by the runner, only for the env it ran on, only after a fully successful run; atomic (temp + rename), `sort_keys=True`, LF.
- Fingerprint: sha256 of file text with CRLF/CR → LF; keyed by bundle-relative path (`02-code/…`). App fingerprint: sha256 over sorted relative paths + bytes of `apps/*/<app_id>/`.
- Statuses accepted in `release.json`: `draft`, `staged`, `deployed`. Staged fields: `stagedAt`, `stagedTarget`, `stagedCommit`. Override marker: `deployedWithoutStagingProof: true`.
- `04-verify/` always runs and is never recorded in `steps`.
- Production always runs every step and imports every app; it keeps the strict `baseSha256` patch guard.
- New flags: `run --full`, `run --no-staging-proof`, `check --full`; new subcommand `status <bundle> [--format text|json] [--live --env <env>]`.
- The runner never commits; it prints the files to commit.
- Tag new code blocks with `ASHARIF 2026-11-25` short comments, as the repo does.

## Review Focus

1. Delta run where every step is unchanged (0 to apply): apply phase must be skipped cleanly, verify still runs, state rewritten with `applied: 0`. Test in Task 3.
2. A failed staging run after an earlier success: state file and `release.json` must be byte-identical afterwards. Test in Task 3.
3. A step removed from `release.json` but present in state: dropped on next write, and not counted as "not on staging". Test in Task 1.
4. A corrupt or wrong-slug `state/<env>.json`: refuse with a clear error rather than silently re-running everything. Test in Task 1.
5. Production with `staged` status but one file edited after staging (stale): refused naming the file, unless `--no-staging-proof`. Test in Task 4.

---

### Task 1: `scripts/release_state.py` pure state module

**Files:**
- Create: `scripts/release_state.py`
- Test: `tests/test_release_state.py`

**Interfaces (Produces):**
- `STATE_SCHEMA_VERSION = 1`
- `text_sha256(text: str) -> str`
- `app_source_sha256(repo_root: Path, app_id: int, app_dir: Path | None = None) -> str` (missing dir → sha256 of empty input)
- `state_path(bundle_dir: Path, env: str) -> Path`
- `load_state(bundle_dir: Path, slug: str, env: str) -> dict | None` (raises `StateError` on bad JSON, wrong `slug`/`env`/`schemaVersion`, or a symlink)
- `write_state(bundle_dir: Path, env: str, state: Mapping) -> None`
- `select_steps(current: Mapping[str, str], env_state: dict | None, *, full: bool) -> tuple[list[str], list[str]]` → (to_apply, skipped), preserving `current` order
- `staging_mismatches(current: Mapping[str, str], app_shas: Mapping[int, str], staging_state: dict | None) -> list[str]` → labels like `02-code/x.sql` or `app 130`
- `merge_state(previous: dict | None, *, slug, env, current, applied, live_hashes: Mapping[str, str], app_shas_deployed: Mapping[int, str], run: Mapping, at: str, commit: str) -> dict` (keeps previous entries for still-current skipped files, drops files not in `current`)
- `effective_status(manifest: Mapping, current, app_shas, staging_state) -> tuple[str, list[str]]` → (`draft|staged|stale|deployed`, mismatches)
- `class StateError(Exception)`

- [ ] Step 1: tests `test_text_sha256_normalises_line_endings`, `test_select_steps_full_delta_and_none`, `test_merge_state_keeps_skipped_drops_removed`, `test_load_state_rejects_corrupt_and_wrong_slug`, `test_write_state_atomic_sorted_lf`, `test_staging_mismatches_and_effective_status`, `test_app_source_sha256_changes_with_content`.
- [ ] Step 2: run `python3 -m pytest tests/test_release_state.py -q` → fails (module missing).
- [ ] Step 3: implement the module.
- [ ] Step 4: rerun → pass. Commit.

### Task 2: manifest status, collect back to draft, list labels

**Files:** Modify `scripts/release.py` (`_load_and_validate_manifest`, `cmd_collect`, `cmd_list`); Test `tests/test_release.py`.

- [ ] Step 1: tests `test_manifest_accepts_staged_status`, `test_collect_on_staged_returns_to_draft_and_drops_staged_fields`, `test_list_shows_staged_stale_and_not_on_staging_counts` (expects `staged`, `staged (stale: 1 changed)`, `draft: 1 of 2 steps not on staging`).
- [ ] Step 2: run → fail. Step 3: implement. Step 4: pass. Commit.

### Task 3: delta selection, drift, state write and staged status in check/run

**Files:** Modify `scripts/release.py` (`PreflightResult` + fields `to_apply: list[str]`, `skipped: list[str]`, `drifted: list[str]`, `current_shas: dict[str,str]`, `app_shas: dict[int,str]`, `apps_to_deploy: list[int]`, `env_state: dict | None`; `_preflight_bundle(..., full: bool = False, no_staging_proof: bool = False)`; `cmd_check` text/json shows apply/skip/drift; `cmd_run(..., full: bool = False, no_staging_proof: bool = False)`; parser flags).

Behaviour: preflight loads `state/<env>.json` (not for prod), selects steps, fetches source for skipped code steps that have `liveSha256`, moves drifted ones to apply, filters `steps` to applied + verify and `fetched_by_schema` to applied items, accepts a patch step whose live hash equals the recorded `liveSha256` (non-prod). After verify and apps, `cmd_run` reads live source of applied code objects (one read-only fetch per schema), merges state with evidence (`identities` with invalid counts, `timings`, `appsDeployed`, `report`, `mode`, `applied`, `skipped`, `drifted`), writes it, and on staging sets `release.json` `staged` + staged fields; prints files to commit.

- [ ] Step 1: tests `test_staging_rerun_applies_only_changed_and_marks_staged`, `test_full_rerun_applies_everything`, `test_noop_delta_skips_apply_runs_verify`, `test_failed_run_writes_no_state_and_keeps_manifest`, `test_drifted_skipped_object_is_reapplied`, `test_patch_rerun_accepts_recorded_live_hash`, `test_unchanged_app_is_not_redeployed`, `test_dev_run_records_state_without_status_change`, `test_check_lists_apply_skip_and_drift`.
- [ ] Step 2: fail. Step 3: implement. Step 4: pass (whole `tests/test_release.py`). Commit.

### Task 4: production gate and prod state

**Files:** Modify `scripts/release.py` (`_preflight_bundle` prod gate, `cmd_run` prod lock writes `state/prod.json` and `deployedWithoutStagingProof`); update existing prod tests to stage first or pass `--no-staging-proof`.

- [ ] Step 1: tests `test_prod_refuses_without_staged_record`, `test_prod_refuses_stale_staged_bundle_naming_file`, `test_prod_no_staging_proof_records_override`, `test_prod_after_staging_runs_all_and_writes_prod_state`.
- [ ] Step 2: fail. Step 3: implement. Step 4: pass. Commit.

### Task 5: `release status`, docs, manifest

**Files:** Modify `scripts/release.py` (`cmd_status`, parser), `scripts/team.sh` help text if it lists release subcommands, `docs/release-workflow.md`, `README.md` command summary, `AGENTS.md` release paragraph.

- [ ] Step 1: tests `test_status_table_text_and_json`, `test_status_live_reports_drift` (read-only fetch only, no apply driver).
- [ ] Step 2: fail. Step 3: implement + docs. Step 4: pass. Commit.

### Task 6: verification and delivery

- [ ] `python3 -m pytest tests/test_release_state.py tests/test_release.py -q` → all pass.
- [ ] `python3 scripts/pre_push_check.py` → pass.
- [ ] Live test on the local Docker database (dev env profile there): run, delta rerun, `--full`, `status --live`.
- [ ] Push branch, open PR, CI green, merge (owner authorised), upgrade natrec.
