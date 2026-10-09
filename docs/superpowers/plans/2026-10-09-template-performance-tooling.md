# Template Performance and Optional Tooling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` for inline execution, or `subagent-driven-development` only if the developer explicitly chooses delegation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve large catalog capture, isolate optional Graphify and preserve downstream ownership through upgrades.

**Architecture:** Keep the existing catalog model and drift guarantees while optimizing lookup/transport. Make optional tooling reproducible in a project environment, and consolidate qualification around explicit read-only versus disposable-write modes.

**Tech Stack:** Python 3.10+, unittest, standard-library base64/gzip, Oracle PL/SQL, SQLcl; optional pinned Graphify environment.

**Spec:** [Local improvements design](../specs/2026-10-09-local-template-improvements-design.md), Release C.

## Global Constraints

- APEX 26.2 operations require SQLcl 26.3.0.0 or newer; 26.1 stays on `codex/apex-26.1`.
- No custom database metadata tables, mutexes, rosters or migration ledgers.
- Keep LF APEXlang/SQL, numeric app IDs and explicit workspace/parsing-schema mappings.
- Planning does not authorize implementation or database qualification writes.
- No silent commits/pushes, team messages, imports, exports or migrations.
- UC-APX remains retired; working copies remain deferred.

## Review Focus

- Emoji/multibyte text and corrupt compressed frames must never produce a partial successful inventory (Task 1).
- Batching must preserve identity-sequence ownership and partition drift evidence (Task 1).
- Two downstream projects must not patch the same Graphify installation (Task 2).
- Customized tests, linked plans and durable notes must survive cleanup/upgrades (Task 2).
- Probe failure must retain ambiguous-write evidence and never remove preexisting fixtures (Task 3).

---

### Task 1: Batch catalog lookups and qualify compressed transport

**Files:** Modify `scripts/schema_catalog.sql`, `scripts/schema_catalog.py`,
`tests/test_schema_catalog.py`, `tests/test_schema_catalog_subobjects.py`,
`tests/test_compare_schema.py`, `tests/test_migration_checks.py`;
create `tests/test_catalog_transport.py` and
`tests/fixtures/schema_catalog/transport-cases.json`;
update `docs/TROUBLESHOOTING.md` and qualification documentation.

**Interfaces:** Preserve `capture_inventory`, `capture_snapshot`,
`parse_inventory`, `parse_snapshot` and `ObjectKey`.
Add `decode_catalog_payload(lines: Sequence[str]) -> dict` in
`schema_catalog.py`; decoded cap `MAX_CATALOG_BYTES = 128 * 1024 * 1024`.
Inside an existing begin/end frame, the exact first line
`CATALOG_ENCODING:gzip-base64-v1` selects compressed decoding. No encoding line
means legacy JSON. Keep phase/schema and verified-sentinel requirements.

- [x] Add regressions for both encodings with 10,000 partitioned inventory rows, more than 1 MiB text, supplementary Unicode/combining characters, identity sequences, and unchanged drift signatures; assert byte/text and parsed-model equality across encodings.
- [x] Add refusal tests for unsupported encoding, invalid base64, gzip corruption/truncation, concatenated/trailing compressed data, invalid UTF-8, decoded oversize, missing end/success sentinel and duplicate real object keys. Decode in bounded chunks rather than decompressing an unlimited buffer first.
- [x] Run `python3 -m unittest discover -s tests -p 'test_catalog_transport.py' -v`; expect failure before the decoder exists.
- [x] Replace correlated identity lookups with one grouped owner/name lookup joined into the inventory; retain owner coverage, subobject keys, deterministic ordering and snapshot signatures.
- [x] Implement the framed decoder and Oracle compression adapter. Qualify CLOB-to-UTF-8 conversion, compression framing and base64 output on the explicitly selected local database before enabling compressed output. An unsupported adapter refuses instead of silently downgrading; plain legacy parsing remains supported.
- [x] Run catalog, transport, comparison and migration-check regressions; expect identical semantic catalogs and complete refusals on corruption.
- [x] Run authorized read-only large/Unicode Oracle probes; measure lookup and transfer phases separately against identical synthetic data, using exact output equality as the acceptance gate. Report timings without promising a VPN speedup from Docker evidence.
- [ ] Record versions, limits and verification; consider a separate 26.1 backport only after that branch's Oracle/SQLcl pair is independently qualified. Prepare the performance change when authorized.

### Task 2: Isolate Graphify and document safe downstream ownership

**Files:** Create `tools/graphify/requirements.txt`,
`scripts/graphify_project.py`, `tests/test_graphify_project.py`.
Modify `scripts/setup_graphify_apx.py`, `scripts/check_graphify_apx.py`,
`scripts/verify_local.py`, `scripts/upgrade_template.py` only if preservation
tests expose a missing rule, `.gitignore`, `template-manifest.json`,
`.agents/rules/graphify.md`, `.agents/workflows/graphify.md`,
`docs/GETTING_STARTED.md`, `README.md`,
`tests/test_setup_graphify.py`, `tests/test_upgrade_template.py`,
`tests/test_template_manifest.py`, `tests/test_verify_local.py`.

**Interfaces:** Add
`project_graphify_environment(repo_root: Path) -> Path` returning
`repo_root / '.venv-graphify'`, and
`inspect_graphify(repo_root: Path) -> dict` returning
`status='absent'|'incompatible'|'stale'|'current'` and diagnostic paths/reasons.
CLI `python3 scripts/graphify_project.py setup|verify|extract` uses that
environment. `verify` never installs/repairs; `setup` is an explicit local
mutation. The pinned requirements file records the exact successfully qualified
package/dependency versions chosen during this task, not an open version range.

- [x] Add two-project fixtures sharing a global Graphify install; assert each setup selects its own ignored environment and cannot patch the shared install or the other's environment. Assert verify-only behavior never changes packages/files.
- [x] Add readiness cases for absent optional Graphify, extractor mismatch, stale cache and missing domain coverage; absent is informational, configured broken/stale tooling is attention with actionable diagnostics.
- [x] Add upgrade/cleanup fixtures with project tests, linked project plans, customized rules and durable notes alongside inherited template history. Assert project files survive, conflicts retain evidence, and generated cache files are not treated as project documentation. Preserve existing no-overwrite behavior rather than broad directory deletion.
- [x] Run new Graphify and ownership tests; expect isolation failures before the local environment selector exists.
- [x] Implement explicit isolated setup/verify/extract using the canonical template extractor and current setup helpers. Qualify a local package build, freeze its tested versions and add support-file ownership/ignore entries; do not repair Natrec or any shared installation.
- [x] Update post-clone cleanup with file-level provenance review and project-owned documentation paths. Keep original-template CI/tests and project-specific downstream regressions; do not introduce an automatic sweep.
- [x] Run Graphify tests with the isolated environment and with Graphify absent, plus upgrade/manifest/readiness tests and the full offline suite. Both configurations must support core commands.
- [ ] Prepare the optional-tooling/ownership change when authorized.

### Task 3: Consolidate explicit local qualification and reporting

**Files:** Create `tools/qualify_template_readonly.py`,
`tests/test_template_qualification.py`, `docs/local-qualification.md`.
Modify `tools/probe_apex_26_2.py`, `tests/test_apex_26_2_probe.py`,
`template-manifest.json`, `docs/apex-26.2-qualification.md`,
`.github/workflows/database-checks.yml` only to run offline runner tests.
Use `docs/local-qualification.md` for both release-appropriate modes; do not
execute live database commands from routine CI.

**Interfaces:** Read-only runner CLI requires saved connection, expected user,
schema, workspace, expected host/service and `--output` under ignored scratch.
Return a version-1 report with target identity/release/client version,
per-check status/duration, overall result and limitations; no credentials or
raw logs. Consume existing `run_sqlcl`, catalog and lifecycle helpers. Extend
the existing write probe rather than creating a competing importer.

- [x] Add fake-runner tests for identity mismatch before probes, unavailable database, read-only SQL enforcement, no implicit exports, unsafe output paths, version mismatch and no secrets in summaries.
- [x] Add write-probe tests for preexisting fixture refusal, interruption, wrong-owner cleanup, successful cleanup and ambiguous outcome retention; removal must use the run-created fixture inventory.
- [x] Run qualification test modules; expect new report/mode contracts to fail before implementation.
- [x] Implement a read-only runner for identity, catalog/Unicode transport and lifecycle coverage, and explicit write scenarios in the existing disposable probe. Distinguish fixture execution from inspection of real owner inventories and distinguish empty from positively verified runtime coverage.
- [x] Run all offline tests and lint/syntax checks; normal CI must not require Docker, saved connections or live Oracle access.
- [x] When live operations are explicitly authorized, run read-only qualification on the verified local Docker target; separately run disposable import/lock/partial/lifecycle scenarios with unused IDs. Record actual cleanup outcomes and remaining unavailable checks; do not claim Windows qualification from Linux skips.
- [ ] Link verified reports from release documentation and prepare Release C integration when authorized.
