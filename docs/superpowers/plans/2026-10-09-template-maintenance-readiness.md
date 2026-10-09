# Template Maintenance and Local Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` for inline execution, or `subagent-driven-development` only if the developer explicitly chooses delegation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the reviewed fixes, a safe 26.1 maintenance path and one offline readiness command.

**Architecture:** Preserve the existing correction set, backport by release capability, and route both team wrappers to a shared Python readiness report. Reuse current source/descriptor/upgrade validators and SQLcl's native skills records.

**Tech Stack:** Python 3.10+, unittest, Bash, PowerShell, native SQLcl skills registry.

**Spec:** [Local improvements design](../specs/2026-10-09-local-template-improvements-design.md), Release A.

## Global Constraints

- APEX 26.2 operations require SQLcl 26.3.0.0 or newer; 26.1 stays on `codex/apex-26.1`.
- Weekly skills refresh uses SQLcl's native registry only; no extra date file or scheduled automation.
- Keep LF APEXlang/SQL, numeric app IDs and explicit workspace/parsing-schema mappings.
- Planning does not authorize implementation or database qualification writes.
- No silent commits/pushes, team messages, imports, exports or migrations.

## Review Focus

- Legacy lock files missing release/ref fields must require an explicit apply choice (Task 2).
- Conflicting installed-skill records, future dates and exactly seven-day-old dates must refuse freshness (Task 3).
- Local paths containing spaces, UTF-8 BOMs and literal shell substitution must preserve parser semantics (Task 3).
- Old completed recovery records must not be reported as live locks; unresolved records must remain visible (Task 3).
- An empty template and a customized downstream checkout must produce appropriate, read-only readiness results (Task 3).

---

### Task 1: Prepare the reviewed maintenance correction set

**Files:** Review the existing modifications in `.agents/workflows/team-flow.md`,
`scripts/{compare_schema.py,ords_export.py,schema_catalog.py,schema_catalog.sql,sqlcl_session.py,upgrade_apexlang.py}` and
`tests/{test_compare_schema.py,test_ords_export.py,test_upgrade_apexlang.py,test_schema_catalog_subobjects.py,test_sqlcl_session.py}`.
Modify `docs/known-limitations.md` only where its description no longer matches
the corrected behavior; retain the accepted limitations.

**Interfaces:** No new API. Preserve existing entry points, return statuses and
the four-field catalog `ObjectKey` including `subobject_name`.

- [x] Read the correction diff and ignored review adjudication; identify all twelve paths before editing or staging.
- [x] Run `python3 -m unittest discover -s tests -v`; expect success, documenting platform skips rather than asserting Windows parity.
- [x] Run the repository Ruff and Bash syntax checks plus `git diff --check`; expect no errors.
- [x] Review changes against `docs/known-limitations.md`; document only actual behavior changes and prior native qualification scope.
- [x] Prepare a narrowly scoped maintenance commit/PR when commit/push authorization is present; do not stage unrelated planning files or advance a baseline.

### Task 2: Make the 26.1 maintenance upgrade safe and explicit

**Files:** Branch-specific modifications to `scripts/upgrade_template.py`,
`scripts/team.sh`, `scripts/team.ps1`, `template-manifest.json`, `AGENTS.md`,
`CLAUDE.md`, `README.md`, `docs/GETTING_STARTED.md`,
`tests/test_upgrade_template.py`, `tests/test_template_manifest.py`,
`tests/test_documentation_contract.py`; backport applicable Task 1 files/tests.
Use the existing checkout on a named branch; do not switch with the current
uncommitted correction set present. Preserve the existing 26.1 version floors.

**Interfaces:** Keep `fetch_template(source: str, ref: str | None,
destination: Path) -> str` and `main(argv: list[str] | None = None) -> int`.
Lock metadata remains schema 1 with `templateRef` and `apexRelease` fields;
the 26.1 manifest remains compatible with 26.1 and must not advertise 26.2 APIs.

- [x] Add temporary-Git-repository regressions: short `codex/apex-26.1` and fully qualified refs resolve to the same commit; an explicit pinned 26.1 apply never clones default main; a legacy missing pin refuses apply without explicit choices; switching to 26.2 requires explicit release selection; customized files retain conflict/no-overwrite protection.
- [x] Run `python3 -m unittest discover -s tests -p 'test_upgrade_template.py' -v`; expect the new legacy-pin test to fail before implementation. Keep tests proving existing supported behavior.
- [x] Implement explicit release/ref resolution and retained lock metadata using existing upgrader safety helpers. Do not infer a live database version or convert app source.
- [x] Carry over the native-registry weekly refresh instructions and qualifying maintenance fixes; exclude 26.2 application/page APIs. Ensure retired UC-APX settings cannot be newly reintroduced by the upgrader.
- [x] Run upgrade, manifest and documentation tests, then the full offline suite separately on each release branch; expect each branch's own release contract to pass.
- [x] Dry-run the resulting 26.1 engine against a disposable copied legacy project fixture with project tests/custom rules; expect a pinned preview and preserved project files, with no tracked writes. Do not apply to Natrec.
- [ ] Review and prepare the independent 26.1 maintenance integration when authorized; do not assume all 26.2 fixes cherry-pick unchanged.

### Task 3: Add shared native skills inspection and offline readiness

**Files:** Create `scripts/oracle_skills_status.py`, `scripts/local_config.py`,
`scripts/verify_local.py`, `tests/test_oracle_skills_status.py`,
`tests/test_local_config.py`, `tests/test_verify_local.py`.
Modify `scripts/team.sh`, `scripts/team.ps1`, `tests/test_team_cli.py`,
`tests/test_windows_support.py`, `README.md`, `docs/GETTING_STARTED.md`,
`template-manifest.json` and agent guidance where command examples are needed.

**Interfaces:**
- `read_project_env(path: Path) -> dict[str, str]` in `local_config.py`: literal parsing/validation matching supported loaders, without shell evaluation or value disclosure.
- `inspect_oracle_skills(registry: Path, skill_roots: Sequence[Path], now: datetime) -> dict` in `oracle_skills_status.py`: `status`, `oldestInstalledAt`, `installationCount`, `issues`; read-only, deterministic with injected UTC clock.
- `build_report(repo_root: Path, values: Mapping[str, str], skills: dict) -> dict` in `verify_local.py`: `schemaVersion=1`, `checks` entries with `check/status/path/message`, and `exitCode` according to the design.
- `main(argv: list[str] | None = None) -> int`: flags `--format text|json`, repeatable `--skills-root`, and `--live`; default roots match the design.

- [x] Add skills fixtures for valid dates, exact seven-day expiry, future/naive/invalid timestamps, absent catalog/files, identical and conflicting duplicate path records, and extra Oracle catalog roots; assert freshness derives from every loaded installation, never file mtime.
- [x] Add configuration parity tests against both loaders for BOMs, quoted values, aligned lists, unknown/duplicate keys and literal `$(...)`/backticks; verify no command executes and errors omit values.
- [x] Add readiness integration tests with fake SQLcl/network launchers that fail if called: valid empty template, downstream missing lock, wrong release/ref, missing DEV descriptor, optional staging/prod absent, malformed descriptor, links escaping checkout, unresolved versus released recovery evidence, and upgrade conflict copies. Assert hashes/mtime-independent contents of project/registry stay unchanged.
- [x] Run `python3 -m unittest discover -s tests -p 'test_verify_local.py' -v`; expect failure before the command exists.
- [x] Implement shared local validation using `read_descriptor`, `validate_app_source` and safe upgrade-lock helpers. Read recovery status only; never delete evidence or claim a live lock check. Native freshness inspection must not create a registry or run a sync.
- [x] Add Bash/PowerShell dispatch parity and text/JSON output. Implement `--live` by calling the existing `doctor` entry point, mapping verified/unknown/failure into report entries and excluding raw logs from the summary.
- [x] Run all three new test modules plus wrapper/documentation tests; expect deterministic result statuses and correct 0/1/2 exits. Run native Windows checks on Windows or explicitly record unavailable evidence.
- [x] Update command help, ownership and startup guidance: the agent still automatically syncs when due, then reports native dates; the offline command reports freshness without claiming a fresh sync occurred.
- [x] Run the full offline suite, lint/syntax and `git diff --check`; prepare the Release A review when authorized.
