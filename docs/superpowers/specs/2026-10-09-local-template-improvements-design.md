# Local template improvements design

**Status:** Proposed for developer review; implementation is not authorized by this document.

## Purpose and evidence

Improve the reusable template using the completed inline review, the independent
AGY review and the read-only investigation of `natrec_team`. Deliver three
independently releasable changesets instead of one broad refactor.

The existing twelve-path correction set is uncommitted on
`codex/inline-template-review`. Its recorded final run passed 1,070 tests with
21 platform skips. That is prior verification, not a claim that a future patch
or the 26.1 branch has passed. Review evidence remains in ignored scratch:

- `scratch/agy-review-20261009/adjudication.md`
- `scratch/natrec-template-review-20261009/report.md`

Natrec supplies examples and historical performance evidence. Do not alter its
repository, connect to its shared database or import its downstream commits.

## Release A: maintenance and local readiness

1. Integrate the already reviewed timeout, decoding, identity, partition,
   comparison, Unicode and workflow-frontmatter corrections through the normal
   template branch/PR process, once implementation/integration is authorized.
2. Backport applicable corrections and release-aware upgrade handling to
   `codex/apex-26.1`. Preserve the existing 26.1 release requirements; do not
   transplant 26.2 lock, partial-publish or descriptor APIs. Pin downstream
   upgrades to their recorded branch and release. Legacy locks missing those
   fields must obtain an explicit branch/release choice before apply; an offline
   dry-run may report that choice is required. Upgrading the script alone must
   not imply an APEX source conversion or a database upgrade.
3. Add `verify-local [--format text|json] [--skills-root PATH ...] [--live]`
   to both supported team entry points. Default execution reads local files
   only: no SQLcl launch, network calls, package installation, file repair,
   source export, import, migration, database lock or baseline update.

`verify-local` validates literal credential-free configuration, aligned profile
lists, template release/ref records, numeric app directories, explicit DEV
descriptors and unresolved recovery/upgrade conflicts. Staging/production
descriptors remain optional until that environment is selected. An empty
template checkout with no real applications is valid; `apps/templates/` is not
an application. A downstream project must have its upgrade lock; the original
template repository need not have a downstream lock.

Its skills check reads SQLcl's registry and synced catalog, matches expanded
installation paths and validates all Oracle roots in the supplied loaded skill
directories. Codex defaults are `~/.agents/skills` and `~/.codex/skills`; another
agent must supply its actual roots. Distinguish missing/invalid, stale and
current records, deduplicate shared paths, and report the oldest installation
date/count. Never write a timestamp file or infer freshness from file mtimes.
The command reports a due sync; the mandatory agent startup instruction remains
responsible for automatically running and verifying `skills sync -force`.

Output uses result entries with `check`, `status`, `path` and `message`; status
is `pass`, `attention`, `unavailable` or `fail`. Exit 0 means all required checks
pass, 1 means attention/unavailable without a hard failure, and 2 means an
invalid configuration/contract or failed verification. Missing optional tooling
is informational and does not itself change the exit code. Local recovery
files establish recorded unresolved work, not a current database lock.
`--live` explicitly adds the existing read-only `doctor` check; it never adds
exports or writes. Diagnostics must not print arbitrary configuration values,
credentials, checksum salts or raw SQLcl logs.

## Release B: database operation profiles and lifecycle observations

Add optional, complete position-aligned migration triples:

| Environment | Keys |
| --- | --- |
| DEV | `MIGRATION_SCHEMA`, `MIGRATION_SQLCL_CONNECTION`, `MIGRATION_EXPECTED_USER` |
| Staging | `STAGING_MIGRATION_SCHEMA`, `STAGING_MIGRATION_SQLCL_CONNECTION`, `STAGING_MIGRATION_EXPECTED_USER` |
| Production | `PROD_MIGRATION_SCHEMA`, `PROD_MIGRATION_SQLCL_CONNECTION`, `PROD_MIGRATION_EXPECTED_USER` |

All three keys are required together per environment. When absent, retain the
existing environment's migration target behavior. An explicitly configured
profile never silently falls back to code/DEV credentials. Profiles affect
`migrate` and live `check-conflicts`, not backup, comparison, ORDS or APEX
targets. Keep `--schema`, folder/schema matching, actual session-user checks,
production safeguards and one-schema-per-batch enforcement. Offline readiness
validates profiles; `doctor` also validates configured DEV migration identities.

Observe application lifecycle before and after full and selected-page DEV
publish, then integrate the same check with direct staging/production imports.
First qualify read-only public view access/columns and the required APEX
session context against the installed release. A complete snapshot records
target identity, release, capture time, automation static IDs/status and
workflow/task instance IDs/states; no task payloads, secrets or business data.

The comparison reports automation enablement changes, lost previously live
instances and newly observed error/suspension states for review. Naturally
completed/progressed instances are informational, not automatic failures.
Counts alone cannot establish lost instances or attribute changes to an import.
Use a bounded second post-import observation for a newly suspended workflow,
then report any remaining uncertainty. Do not enable, resume, cancel, terminate
or delete runtime objects. Projects may intentionally change definitions;
this release reports those changes and leaves recovery decisions explicit.

A pre-import unavailable/malformed snapshot refuses before writing. An
unavailable post-import observation or a lifecycle change needing review returns
2, preserves recovery evidence and the old DEV baseline/native lock, and states
whether source bytes were separately verified. The success message, baseline
advance and lock release occur only after both gates pass. For direct promotion,
retain the existing human confirmation and report uncertainty without claiming
a DEV lock exists. Lifecycle observations do not prove import causality.

## Release C: catalog performance, optional tooling and qualification

Replace repeated identity-sequence lookups with one owner-scoped grouped lookup
joined to the inventory. Keep the complete inventory, partition/subpartition
identity, drift signatures and unsupported-root checks.

Add an explicitly framed `gzip-base64-v1` transport for large catalog payloads,
with strict base64, gzip, UTF-8 and JSON validation, a 128 MiB decoded limit and
the existing verification sentinel. The parser still accepts the existing
plain-JSON transport. Failure never retries a weaker extraction or reports a
partial catalog as complete. Qualify the SQL compression conversion against
the actual database/SQLcl pair before changing the default transport.

Use a dedicated ignored `.venv-graphify/` environment with an exact version pin
for the qualified Graphify package and dependencies. Setup/extraction must not
patch another project's shared install. Existing explicitly selected installs
may be verified read-only; repair requires selecting the isolated environment.
Graphify remains optional, with clear absent, incompatible, stale and complete
coverage results. Generated `graphify-out/` remains ignored; durable notes
belong in project-owned documentation. New Graphify support files must have
explicit template ownership.

Strengthen cleanup/upgrade guidance and fixtures so project-specific regression
files and linked project plans survive. Keep `AGENTS.project.md`, project rules,
`PROJECT.md`, `.env`, app source, migrations and recovery evidence. Classify
historical template-maintenance files explicitly; never delete a file simply
because it lives under `tests/` or `docs/superpowers/`. Preserve existing conflict
copies and no-overwrite behavior. The original template retains tests and CI.

Consolidate reusable local-Docker qualification. A new read-only runner covers
identity, Unicode/large catalog framing and lifecycle visibility; extend the
existing explicitly write-enabled APEX probe for publish/lifecycle effects.
Write scenarios require an explicitly identified disposable app, verified target
identity and unused fixture IDs. Cleanup only fixtures created by the run;
retain evidence after ambiguous writes. No qualification may run against a
shared project database implicitly.

## Constraints and exclusions

- APEX 26.2 operations require SQLcl 26.3.0.0 or newer; 26.1 stays on `codex/apex-26.1`.
- Keep LF APEXlang/SQL, numeric app IDs and explicit workspace/parsing-schema mappings.
- No custom database metadata tables, mutexes, rosters or migration ledgers.
- Weekly skills refresh uses SQLcl's native registry only; no extra date file or scheduled automation.
- UC-APX remains retired; working copies remain deferred.
- Native application locks are not a process mutex; SQLcl imports can bypass them.
- Builder creates page locks; retain the existing qualified partial-publish coordination policy.
- No silent commits/pushes, team messages, imports, exports or migrations.
- Planning does not authorize implementation or database qualification writes.

## Delivery and review

Release A is first. Release B and C each follow their own tests/review and may
ship separately. Test Bash/PowerShell parity and supported Windows behaviors;
Linux skips are not Windows execution evidence. Run the full offline suite per
finished changeset; live qualification is a separate, explicitly authorized
gate. Record actual versions, results, unavailable checks and cleanup outcomes.
