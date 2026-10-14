# Migration planning and baseline bundling design

Date: 2026-10-13
Status: proposed implementation contract, based on the approved in-chat direction; implementation awaits review of the accompanying plans.

## Outcome

Reduce unnecessary migration folders and repeated operator decisions while preserving migration ordering, checks, independent schema ownership and recovery evidence. Deliver the grouping guidance and migration report on both supported branches, and provide the same optional baseline bundling workflow after the database-only prerequisites have been ported to APEX 26.1.

The user explicitly requested an implementation plan for both branches. This document records the decisions needed to make that plan concrete. Creating these planning documents does not authorize database writes, execution of the implementation, or new commits and pushes.

## Verified branch baseline

| Track | Remote baseline fetched during planning | Observed capabilities |
| --- | --- | --- |
| APEX 26.2 | origin/main, 6770f5523bf687788b639f20f8d1ab9ad21799ed | Baseline generator, complete environment capture, durable attempt events, Linux and Windows offline CI |
| APEX 26.1 | origin/codex/apex-26.1, 0bb72a356aa8b6cb6242e4e20b4653801037679a | Migration loader, checks, receipts, scratch attempt inspection, rollout and selected-object comparison; no baseline.py or compare_env_catalog.sql; Ubuntu offline CI |

Planning checkout: codex/template-review-fixes at the observed origin/main SHA. Existing local .env, DEMO exports/mirrors, remediation plan/spec and uv.lock are unrelated work and must be preserved. No branch switch or product implementation was performed to create this design.

Sources read: scripts/team.sh, scripts/team.ps1, scripts/baseline.py, scripts/migration_manifest.py, scripts/migration_revision.py, scripts/migration_checks.py, scripts/compare_schema.py, scripts/schema_catalog.py, scripts/rollout.py, branch versions of those files, and docs/known-limitations.md.

## Global constraints

- Python 3.10 or newer; standard library only for the daily workflow.
- New live migration-report and baseline workflows require SQLcl 26.3.0.0 or newer; offline reporting needs no SQLcl.
- Preserve APEX 26.1 application/source/deployment behavior on codex/apex-26.1 and APEX 26.2 behavior on main.
- One migration folder changes one schema; cross-schema work uses separate folders and an explicitly ordered release.
- Preserve existing folder naming, consecutive SQL numbering, checks.json schema 1, migration digests and verified-receipt formats. Existing schema-2 rehearsal metrics remain supported where already present; these features do not require schema 2.
- Once any database write may have begun, SQL/check bytes and migration identity remain immutable. Unknown evidence refuses mutation; successful receipts are never overwritten.
- Default reporting reads local files only, creates no files, loads no .env, starts no SQLcl and performs no network access.
- Live report mode performs the existing guarded read-only preflight; it never applies SQL, rehearses DML, repairs state or writes migration receipts.
- Baseline generation reads live metadata and writes new local migration folders; it never applies generated SQL.
- Keep Bash and PowerShell public arguments, output schema and exit codes equivalent; preserve existing Windows fixes.
- No shared migration ledger, database mutex, team table or cross-repository pending-work discovery.
- Preserve project-owned files and existing attempted/applied migration folders; no automatic squashing, deleting, renaming or archiving.
- All new source/SQL/JSON/shell files use LF. Reports expose paths, counts, hashes and sanitized diagnostics, never SQL bodies, credentials or full logs.

## A. Migration granularity guidance

Use one folder for a cohesive change in one schema when its SQL can be ordered and verified together. Tables, indexes, views and stored units belonging to that change can be consecutive steps within the folder. A folder is a review and execution boundary, not an Oracle DDL transaction or rollback guarantee.

Keep separate folders for different schemas, independent release dates, long-running backfills, and changes requiring an intermediate verification/recovery boundary. Consolidation of unattempted drafts remains a human-reviewed workflow: confirm team coordination and local attempt evidence, then review dependencies and the final checks. Do not provide an automatic folder-merging tool in this iteration.

The report counts one-SQL-file folders as a neutral metric. It does not declare them defective or impose a maximum folder count. Existing multi-folder migrate and rollout commands reduce operator runs without changing folder count.

## B. Public migration report

Public commands on both branches:

    scripts/team.sh migration-plan [<folder> ...] [--schema NAME] [--format text|json|markdown]
    scripts/team.sh migration-plan <folder> [...] --live --env dev|staging|prod [--schema NAME] [--format text|json|markdown]

PowerShell accepts identical arguments. Without folders, discover and report all local migration folders in deterministic display order; this is inventory, not inferred dependency order. Explicit folders retain caller order and are validated using existing batch rules. Offline selection may describe multiple explicit schemas by validating each schema group independently; duplicate paths and nonascending family revisions refuse. Live mode requires an explicit selection for one resolved migration owner, plus --env; an environment without --live, --live without --env, and live discovery without selected folders refuse before connecting.

Offline --schema filters explicit schema directories. Flat folders have unresolved schema unless the caller asserts --schema; mark that assertion as unverified and never read .env to infer it. Explicit directory mismatches refuse. Display schema groups, not a synthetic shared execution order across owners.

JSON report schemaVersion 1 has these stable root keys:

- schemaVersion: integer 1.
- mode: offline or live.
- selection: discovered or explicit.
- orderStatus: sorted-display-only or caller-supplied.
- environment: null offline, selected environment live.
- target: null offline, sanitized observed identity from successful live capture.
- summary: folderCount, sqlFileCount, preconditionCount, postconditionCount, singleSqlFileFolderCount, immutableFolderCount, unknownEvidenceFolderCount; all nonnegative integers.
- migrations: records with path, schema, schemaSource (directory, caller-asserted or unresolved), family, revision, payloadDigest, checksSha256, ordered file names/hashes and check counts; localEvidence contains status (unlocked, locked or unknown) and sanitized reasons.
- preflight: null offline; existing preflight result plus deferredPreconditionFolders live. Failure retains its diagnostics and never synthesizes observed identity.
- diagnostics: structured code, path and sanitized message entries.
- limitations: local-only evidence, no other-repository pending-work coverage, no inferred dependency order, and the live precondition boundary when applicable.

An unlocked local folder means no locking evidence was found in this checkout. It does not prove nobody attempted it elsewhere. A local receipt is historical evidence, not current database shape. Do not call an offline folder ready, applied on the current target, safe to rewrite, or safe to squash.

Offline exit 0: valid complete inventory, including known locked folders and an empty migration directory. Exit 2: unsafe/invalid paths, malformed migration contents or unknown/unreadable evidence. A missing migration directory is an error, not an empty inventory. Live exit 0: selected preflight has no conflicts/errors and no selected locked evidence; exit 1: known conflict or selected immutable folder; exit 2: invalid input, incomplete observation, unknown evidence or target refusal. Preserve reports for exit 1/2 when structurally possible.

Live mode evaluates only the first folder's preconditions before any write, as the current batch preflight does. Report later preconditions as deferred; do not evaluate them early, drop them, or claim they passed. Existing opaque-SQL manual-review notices remain visible even when preflight exits 0.

## C. Optional generated baseline bundling

Public interface on both branches after prerequisites:

    scripts/team.sh baseline build --from dev --to staging --bundle customer-status
    scripts/team.sh baseline build --from dev --to staging --bundle customer-status --data

The bundle name is an ASCII lowercase migration slug matching [a-z][a-z0-9]*(?:-[a-z0-9]+)*, maximum 64 characters. Validate it before any SQLcl start. Generate family baseline-bundle-<name>, one folder for each nonempty target-schema bundle. Do not combine schemas or accept existing migration folders as inputs.

Without --bundle, preserve existing baseline-structure, baseline-grants, baseline-code and baseline-data output behavior. With --bundle, compose generated structure, grants and code in that exact order. Preserve the original step order inside each category and the SQL content; renumber all steps consecutively from 001 to at most 999. New filenames are NNN-<category>-<original-step-slug>.sql, with category structure, grants or code. Compilation stays after the code-generation steps where the existing generator places it.

Keep every generated pre/postcondition SQL and expected value; prefix IDs with structure-, grants- or code- to avoid cross-category collisions. Refuse duplicate IDs within a component, invalid checks or more than 999 steps. Do not deduplicate supposedly redundant assertions or broaden the query allowlist.

Only the current generated structure/grants/code components are eligible. They must be built against the same complete source/target captures and have checks that can run at the combined initial/final boundaries. Reject identical observed database/container/schema/edition scope before generating files even if configured environment labels differ; missing scope evidence is an error. Assert that grants and code components have no initial preconditions; refuse newly introduced boundary requirements until reviewed rather than lifting them across an earlier write. Preserve structure preconditions at the initial boundary. Arbitrary SQL and reference-data components are ineligible.

With --data, baseline-data remains a separate folder after the bundle. Its preconditions, natural-key/foreign-key handling, DML limits, rehearsal instructions and identity-DDL handling remain intact. Typical output is three eligible category folders reduced to one, or four category/data folders reduced to two; no promise applies to revision count or schemas with no changes.

Bundle README records source/target environment and schema, included categories, ordered original-to-generated filename mapping and SQL SHA-256 values, review/check instructions, the DDL recovery caveat, and the separate data-folder ordering when applicable. Keep metadata in README, not a new unchecked sidecar.

Use the existing migration folder installer and validator. Identical inputs do not create another revision. Changed input creates the next valid revision without replacing old files, attempted folders or receipts. Unknown lock evidence refuses. Staging/validation must not expose a partially populated final folder on failure; final installation must refuse an occupied destination on both platforms.

## D. Cross-branch delivery

Use separate implementation branches based on the verified release branches, in this existing checkout and sequentially. Recheck upstream tips before execution; do not assume these recorded SHAs stay current.

APEX 26.2 can implement the report and bundle against current helpers. APEX 26.1 needs a database-only prerequisite port before feature parity is complete:

1. Durable migration-attempt events and their reader/writer integration, including backward-compatible legacy scratch evidence inspection.
2. Complete paged catalog capture and database scope checks used by baseline generation; required SQLcl framing/path/Windows transport support.
3. The existing baseline generator, supporting fixtures and docs, using the existing branch target resolver and ORDS filter helper. No APEX application parser/import/lifecycle code is part of the port.

Port narrowly from the pinned 26.2 implementation, review dependencies and preserve APEX 26.1 source examples, descriptors, qualification files and manifest format. Do not merge the entire main branch into codex/apex-26.1. Code imports must prove that the new database paths do not depend on APEX 26.2-only modules. Add Windows offline CI on the 26.1 track; a green Ubuntu run alone is insufficient platform evidence.

## Acceptance and limits

- Both branches expose equivalent report/bundle arguments, JSON contracts and generated migration outputs for shared database fixtures.
- The existing granular baseline output remains byte-identical with no --bundle.
- Golden fixtures show three eligible categories becoming one validated folder; --data still produces a separate validated data folder.
- Offline report tests fail if a real SQLcl, .env reader, network client or write path is invoked.
- Tests cover malformed/unknown attempt evidence, symlinks and Windows reparse points, occupied destinations, mid-generation failure, revision gaps, multi-schema ownership and duplicate/check-boundary cases.
- Focused tests, full branch suites and Linux/Windows CI pass at the exact reviewed tip before integration. Platform skips are reported explicitly.
- Live qualification is optional follow-up requiring named-target authorization. Planning and implementation tests use fake SQLcl; no live migration/import/rehearsal is authorized here.

The source template currently tracks no actual migration folders beyond migrations/README.md and .gitkeep. Reduction targets are therefore fixture expectations and downstream workflow goals, not measurements of an existing migration backlog.
