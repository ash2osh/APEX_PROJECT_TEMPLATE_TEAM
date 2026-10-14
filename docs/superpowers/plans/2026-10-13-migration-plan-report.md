# Migration Plan Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. Recommended execution here is inline and sequential because the branches share one checkout.

**Goal:** Give both release branches a deterministic migration inventory and an explicit live preflight report without applying SQL or claiming cross-repository coverage.

**Architecture:** Add a focused report module that consumes the existing loader and evidence inspector. A small public preflight adapter reuses the existing local/live analyzers; both shell routers call the same Python CLI. Branch-specific evidence prerequisites are delivered before parity is claimed.

**Tech Stack:** Python 3.10 standard library, unittest, Bash, PowerShell, existing read-only SQLcl adapters.

**Spec:** ../specs/2026-10-13-migration-planning-and-bundling-design.md, sections A, B and D.

## Global Constraints

- Python 3.10 or newer; standard library only for the daily workflow.
- New live migration-report and baseline workflows require SQLcl 26.3.0.0 or newer; offline reporting needs no SQLcl.
- One migration folder changes one schema; cross-schema work uses separate folders and an explicitly ordered release.
- Default reporting reads local files only, creates no files, loads no .env, starts no SQLcl and performs no network access.
- Live report mode performs the existing guarded read-only preflight; it never applies SQL, rehearses DML, repairs state or writes migration receipts.
- Keep Bash and PowerShell public arguments, output schema and exit codes equivalent; preserve existing Windows fixes.
- No shared migration ledger, database mutex, team table or cross-repository pending-work discovery.
- Preserve APEX 26.1 application/source/deployment behavior on codex/apex-26.1 and APEX 26.2 behavior on main.

All remaining spec constraints apply. Run fake-SQLcl fixtures with tests/_no_real_sqlcl.py protection. No live database operation is required by this plan.

## Review Focus

1. A historical receipt whose payload changed is diagnosed without calling it current live state: Task 2.
2. Malformed unrelated scratch evidence cannot turn an unknown folder into an editable draft: Tasks 1 and 2.
3. Discovered alphabetical order is never shown as executable dependency order; same folder names in different schemas remain distinct: Task 2.
4. Later-folder preconditions can depend on earlier writes and must remain deferred in a read-only live report: Task 3.
5. A schema filter, Windows path or stdout formatter must not bypass configuration/identity guards or disclose SQL/configuration contents: Tasks 2 through 4.

---

### Task 1: Port durable attempt evidence to the 26.1 migration path

**Branch:** APEX 26.1 only; main already has this subsystem. Use origin/main at the reviewed baseline as a source of selected database-only hunks, not a whole-branch merge.

**Files:** create scripts/migration_attempts.py and tests/test_migration_attempts.py; modify scripts/migration_manifest.py, scripts/migration_revision.py, scripts/migrate.py and, where payload-start markers are emitted, scripts/migrate.sql; extend tests/test_migration_manifest.py, tests/test_migration_revision.py and tests/test_migrate_cli.py.

**Interfaces:** preserve load_migration(repo_root, relative_folder), Migration.payload_digest, inspect_migration_lock(repo_root, relative_folder) and its unlocked/locked/unknown result. Add the current main migration_attempts reader/writer API and pending/apply-not-started/verified event transitions with legacy scratch inspection retained. Do not add schema-2 metric acceptance without its runner support.

- [ ] Inspect the exact main/26.1 diff for the named files. Record which existing SQL marker writer is used; retain existing app and rollout paths.
- [ ] Write failing regressions named test_pending_event_survives_missing_scratch, test_legacy_scratch_attempt_remains_locked, test_no_write_evidence_must_match_digest, and test_unknown_attempt_never_becomes_unlocked. Assert pending/verified events lock even after scratch removal; a removed no-write transcript becomes unknown; explicit pre-payload identity/connect/start refusal may be unlocked only with valid retained evidence.
- [ ] Run `PYTHONPATH=tests:. python3 -m unittest -v test_migration_attempts test_migration_revision test_migration_manifest test_migrate_cli`; the new durable-state cases must fail against the old 26.1 reader/writer.
- [ ] Port reader/writer, allowed sidecar validation, writer markers and lock inspection together. Pending is installed before payload start; verified is appended only after verified receipt installation. Preserve existing payload/check digest formats and no-replay behavior. Keep ambiguous interrupted writes locked and do not rewrite events or receipts.
- [ ] Rerun those modules plus test_migration_receipts and test_migration_rehearsal. Confirm legacy fixtures remain supported and tests cannot fall through to real SQLcl.
- [ ] Review the 26.1 diff for accidental APEX 26.2 imports or metric acceptance without evaluation. Proposed future commit: `fix: preserve durable migration attempt evidence on APEX 26.1`.

**Done:** the report can consume equivalent durable states on both branches, while historical 26.1 scratch evidence remains meaningful. No claimed live qualification follows from these tests.

### Task 2: Implement the local report model and renderers

**Files:** create scripts/migration_plan.py and tests/test_migration_plan.py. Reuse existing migration_manifest and migration_revision APIs; avoid adding a second migration parser.

**Interfaces:**

    build_migration_plan(repo_root: Path, folders: Sequence[str] = (), *, schema: str | None = None) -> dict
    render_migration_plan(report: Mapping, output_format: str) -> str

The returned dictionary uses the exact spec section B root/record keys. Keep diagnostics structured and source bodies absent. Add `exitCode` only as an internal value removed by the JSON renderer; CLI returns it separately. Renderer accepts only text, json and markdown.

- [ ] Write test_discovery_is_inventory_without_execution_order: two schemas containing the same dated folder name produce two path-distinct records, selection discovered and orderStatus sorted-display-only. Assert explicit selection retains caller order, with each schema group validated by load_batch.
- [ ] Write test_report_counts_are_informational with two valid folders totaling three SQL files, two preconditions and three postconditions. Assert summary folderCount 2, sqlFileCount 3, preconditionCount 2, postconditionCount 3 and singleSqlFileFolderCount 1. A known locked folder is counted, not called ready, and does not fail an otherwise complete offline inventory.
- [ ] Write test_offline_report_never_opens_external_or_write_paths: patch read_project_env, SQLcl launch, subprocess network access and file write helpers to raise. Assert reporting works without .env/SQLcl and leaves a recursively hashed fixture tree unchanged.
- [ ] Write test_bad_evidence_and_paths_are_diagnosed: changed receipt hashes, malformed receipts, missing transcript, missing migration directory, revision gap, symlink/reparse-point input, escaping paths and duplicate explicit paths return exit 2 with useful sanitized diagnostics. Empty existing migration directory returns 0 with zero counts. Preserve other readable records when possible.
- [ ] Write test_schema_filter_marks_flat_owner_unverified: explicit directory owners filter correctly; a supplied owner for a flat folder has schemaSource caller-asserted; no owner has schema null and schemaSource unresolved. An explicit schema mismatch refuses.
- [ ] Run `PYTHONPATH=tests:. python3 -m unittest -v test_migration_plan`; new model tests must fail before implementation.
- [ ] Implement discovery/loading, grouping, counts, hashes and localEvidence with existing helpers. Validate receipt content against loaded hashes using validate_receipt with its recorded target solely for local self-consistency; never describe that target as a live observation. Invalid receipt evidence overrides a shallow locked classification to unknown with exit 2. Escape Markdown cells and sanitize control characters in diagnostics without printing raw SQL, configuration values or log bodies.
- [ ] Run the new module plus test_migration_manifest, test_migration_schema_folders and test_migration_revision. Golden-format tests assert parsed JSON contract and meaningful text/Markdown fields rather than whole-output snapshots that mirror layout.
- [ ] Port the version-neutral report commit to the second branch after its prerequisites, rerun its focused tests there, and keep commit maps in the umbrella plan's results section. Proposed future commit: `feat: add offline migration inventory and counts`.

**Done:** both branches have equivalent offline reports and no inferred apply readiness.

### Task 3: Add the explicit read-only live preflight adapter

**Files:** modify scripts/migration_checks.py and scripts/migration_plan.py; create scripts/migration_runtime.py; extend tests/test_migration_plan.py and tests/test_migration_checks.py; create tests/test_migration_runtime.py.

**Interfaces:**

    preflight_selected(repo_root: Path, folders: Sequence[str], *, environment: str | None = None, schema: str | None = None, values: Mapping[str, str] | None = None) -> PreflightReport
    require_migration_sqlcl_version(*, runner: Callable = subprocess.run) -> None
    build_live_migration_plan(repo_root: Path, folders: Sequence[str], *, environment: str, schema: str | None, values: Mapping[str, str]) -> dict

Expose a narrow public adapter over existing _local_report/_live_report. Extend the live helper to accept explicit values; existing callers retain os.environ behavior. The new runtime guard runs SQLcl version detection without a database connection and accepts only 26.3.0.0 or newer, using a parser that can be verified against the branch's existing version-output fixtures.

- [ ] Write test_live_requires_explicit_one_owner_selection and test_runtime_floor_refuses_before_capture. Assert discovery, absent environment, mixed owner batches, incomplete profiles and old/unreadable SQLcl versions fail before SQLcl database capture. No .env is read for help or offline mode.
- [ ] Write test_live_retains_guarded_preflight_diagnostics: fake successful identity, production-looking DEV identity, incomplete capture and known object conflicts map to exit 0/2/2/1 respectively. Selected locked evidence returns 1 even when preflight has no conflicts; selected unknown evidence returns 2. Do not call a locked migration ready.
- [ ] Write test_later_preconditions_are_deferred: the second folder queries an object the first creates; assert its query is never evaluated early and appears in deferredPreconditionFolders. Preserve manual-review notices and the current preflight limitation.
- [ ] Run `PYTHONPATH=tests:. python3 -m unittest -v test_migration_plan test_migration_checks test_migration_runtime`; verify failure of the new adapter cases.
- [ ] Implement the interfaces, preserving identity/current-schema guards, catalog completeness, conservative checks and error evidence retention. Resolve target using the validated file mapping with established precedence; do not mutate process os.environ to inject configuration. Add coverage.observedIdentity and coverage.deferredPreconditionFolders to the public adapter using the actual captured snapshot and existing deferred_precondition_folders helper. Only a successful complete capture can populate report target. No apply, rehearsal, receipt or lock-release call is reachable from this adapter.
- [ ] Rerun focused tests and existing check-conflicts CLI tests in test_check_conflicts. Preserve legacy callers and exit codes. Proposed future commit: `feat: add guarded live migration planning`.

**Done:** live planning reports current observations without changing the database or moving later verification boundaries.

### Task 4: Expose both wrappers and ship guidance

**Files:** modify scripts/team.sh, scripts/team.ps1, tests/test_team_cli.py, tests/test_documentation_contract.py, tests/test_template_manifest.py, README.md, migrations/README.md, docs/migration-rules.md, docs/EXAMPLES.md, docs/known-limitations.md and template-manifest.json; create docs/migration-planning.md.

**Interfaces:**

    main(argv: Sequence[str] | None = None, *, repo_root: Path | None = None) -> int

The public command and all argument combinations follow the spec. The Python CLI reads PROJECT_ENV_FILE or root .env with read_project_env only after validated --live input; offline --schema must be forwarded to the report instead of being lost in the routers' global schema stripping. Use existing native-Windows Python and path resolution paths in each wrapper.

- [ ] Write real-wrapper tests named test_migration_plan_help_and_offline_need_no_env, test_both_wrappers_forward_schema_and_folder_order, and test_live_refuses_ambiguous_flags_before_connection. Use fake SQLcl and paths containing spaces/brackets; compare parsed JSON and exit codes across Bash/PowerShell. Record unavailable PowerShell honestly; Windows CI must exercise it.
- [ ] Run `PYTHONPATH=tests:. python3 -m unittest -v test_team_cli test_migration_plan`; wrapper cases must fail before adding the command.
- [ ] Add help and routing to both wrappers. Implement main with a fixed JSON error envelope where possible; syntax errors remain exit 2. Use stdout for reports and stderr for sanitized startup errors. Do not add new env configuration keys.
- [ ] Document the folder decision rule, offline vs live evidence, explicit ordering, deferred preconditions, one-file-folder metric and existing multi-folder migrate/rollout examples. Explain that no automatic draft squasher is provided and applied/attempted folders remain immutable. Add docs/migration-planning.md to templateOwned in both branch manifests; retain each branch's manifest format and qualification templateOnly entries.
- [ ] Run focused wrapper/report/documentation/ownership tests, Bash syntax and PowerShell parser checks. Leave full-branch verification and integration to the umbrella plan. Proposed future commit: `docs: ship migration planning workflow on both APEX branches`.

**Done:** the feature is accessible and documented on both branches, with common behavior and branch-appropriate surrounding commands.
