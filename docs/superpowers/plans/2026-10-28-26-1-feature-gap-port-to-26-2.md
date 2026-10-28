# APEX 26.1 feature gaps to consider for the 26.2 template

**Status:** planning proposal only. This change adds no runtime implementation,
database operation, import, or deployment.

## Comparison baseline

- 26.2 `main`: `86fb735` (`origin/main` at review time).
- 26.1 `main`: `e3b34db` (`origin/main` at review time).
- The repositories have a shared history through `5178f57` (2026-10-05), but
  have since advanced independently. Port reviewed changes selectively; do not
  merge the 26.1 branch wholesale into the 26.2 template.

The 26.1 implementation sources are in `scripts/feature_cli.py`,
`feature_manifest.py`, `feature_preflight.py`, `feature_release.py`,
`feature_sources.py`, `release_capsule.py`, `release_ownership.py`, and
`release_state.py`. Its entry points and contract are documented in
`docs/feature-releases.md`.

## Gap 1: feature-oriented release capsules

The 26.1 template supports mutable feature source under
`features/<feature>/<SCHEMA>/`, live DEV source capture, offline source diffs,
and freezing committed inputs into an environment-neutral release capsule.
The capsule workflow prepares environment-specific target plans, executes the
same payload in staging and production, requires verified staging evidence for
production, and retains attempt, receipt, recovery, and cleanup state.

The 26.2 template has no feature CLI, capsule runtime, or corresponding
`rollout --prepare`, `--capsule`, `--stage-report`, and `--recover` options.
Keep the existing dated migration and schema-version-1 rollout paths working;
the new feature workflow must be an explicit version-2 path.

### Planned implementation work

1. **Re-audit the source implementation.** Compare the current 26.1 runtime and
   its tests with 26.2 migration, rollout, SQLcl transport, target-profile,
   schema-catalog, wrapper, and template-upgrade contracts. Reconcile the
   historical F1–F12 review notes in
   `docs/superpowers/reviews/2026-10-27-template-review-findings.md` against
   the current 26.1 code before porting; those notes describe an earlier
   implementation snapshot.
2. **Port feature source and freezing.** Add feature initialization, guarded
   DEV capture, whole-package source files, offline review diffs, schema-2
   manifest validation, committed-input checks, and a frozen capsule that
   binds the selected source and runtime engine.
3. **Add guarded preparation.** Capture target-specific identity and before
   state read-only; validate supported dependencies, compiler settings,
   application descriptors, migration profiles, and logical SQL/ORDS targets.
   Keep target-plan hashes environment-specific while the payload hash remains
   shared across staging and production.
4. **Integrate execution and recovery.** Add feature checkpoints, run-bound
   target ownership, durable attempts and atomic verification receipts,
   interruption reconciliation, replay refusal for completed writes, staged
   production proof, timing/coverage reports, and guarded capsule cleanup.
5. **Wire and document both wrappers.** Add the feature subcommands and rollout
   options to `scripts/team.sh` and `scripts/team.ps1`; document the workflow,
   manifest, recovery limits, and compatibility boundary. Preserve existing
   schema-1 inputs and command behavior.
6. **Qualify before release.** Add focused regressions for every new contract,
   run the full local and pre-push suites, and require hosted Linux, Windows,
   and macOS checks. Keep live staging rehearsal and any database writes outside
   this implementation until separately authorized.

### Acceptance criteria

- A schema-version-1 rollout and existing migration behavior remain unchanged.
- A version-2 write cannot run without a validated capsule and prepared target
  plan; unsafe or incomplete source/target evidence fails closed.
- Staging and production use the same frozen payload but independently
  prepared target plans; production requires verified staging evidence.
- Unknown writes cannot be replayed or cleaned up as though they succeeded.
- Tests cover feature CLI and source capture, capsule integrity, target
  preflight, staging/promotion, receipts/recovery, wrapper parity, and upgrade
  ownership.
- Local tests and CI are reported separately from any live Oracle/APEX
  qualification; neither implies a production rollout.

## Gap 2: restricted-console-safe test output

The 26.1 test runner routes captured serial and parallel output through
`_write_output`, which escapes characters unsupported by the active console
encoding. The 26.2 runner prints captured output directly. Under a restricted
Windows console encoding, output containing an unencodable character can raise
`UnicodeEncodeError` and interrupt the test runner.

### Planned implementation work

- Port the small output helper to `scripts/ci_run_tests.py` and use it for
  serial output, timeout output, and each parallel module's captured output.
- Add restricted-encoding regression tests for serial and parallel paths,
  including replacement characters from decoded subprocess output.
- Preserve the original Unicode string returned to callers; escape only what
  must be written to the console.

### Acceptance criteria

- Unsupported console characters are emitted as escaped text without changing
  the runner's exit code or summary parsing.
- The regression tests pass with an explicitly restricted encoding, and the
  pre-push check still rejects genuine test failures and incomplete runs.

## Explicit exclusions

- Do not restore the optional `uc-apx` installer/synchronization workflow in
  this port. The 26.2 template explicitly retires its environment settings and
  tests that retirement; reconsider it only as a separate product decision.
- Do not copy 26.1 release-qualification claims or APEX-version-specific
  fixtures into 26.2 without qualifying them on the 26.2 line.
- Do not replace the 26.2 migration, baseline-bundle, or rollout contracts.
- This plan authorizes no SQLcl connection, migration, import, staging write,
  or production action.
