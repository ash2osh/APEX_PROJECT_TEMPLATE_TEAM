# Release bundle state per environment: design

Date: 2026-11-25. Status: approved by the owner 2026-11-25, including suggestions 11–13 and one state file per environment. Applies to the 26.1 template's Lean release runner (`scripts/release.py`).

## Intent

A release bundle (`releases/<slug>/`) shows today only whether it is `draft` or `deployed` to production. A successful staging or DEV run leaves its evidence in the ignored `scratch/release-reports/`, so nothing tracked says what reached staging, and the evidence is lost when `scratch/` is cleaned or the work moves to another machine. The owner wants each bundle to record, by itself and in Git, every object and each environment it reached, and the bundle status to say when it is staged.

A second need follows from day-to-day work: after a small change to a staged bundle, a staging rerun should apply only what changed, not repeat the whole 24-minute run.

The template is used by one developer per repository; the repository is not shared between developers. The state files therefore describe that developer's runs only, which is the intended scope.

## Goals

- New runner-written files `releases/<slug>/state/<env>.json`, one per environment (`dev`, `staging`, `prod`), record per bundle file and per app the fingerprint last applied there, with time and commit, plus that environment's last successful run.
- `release.json` status becomes `draft` → `staged` → `deployed`.
- A change after staging returns the bundle to `draft`; only the changed entries lose their staging match.
- DEV and staging runs apply only bundle files whose fingerprint differs from the one recorded for that environment. `--full` applies everything.
- Production refuses unless the bundle is `staged` and every fingerprint matches staging, with an explicit `--no-staging-proof` override that is recorded.

## Non-goals

- No change to what a step does, to phase order (`01-ddl` → `02-code` → `03-ords` → `04-verify` → apps), or to the identity, backup, invalid-object and prod-pinning checks.
- No database-side ledger or metadata table (the shared database stays the runtime truth; AGENTS.md forbids team metadata tables).
- No sharing of state between repositories or developers.
- No partial production apply: production always runs the whole bundle once.

## Anti-goals (a failure even if it works)

- A step skipped on staging or production that the target does not actually hold in that version.
- Production running a bundle that differs from what staging received, without the override being visible in the report and in `release.json`.
- A failed or interrupted run recording anything as applied in `state/<env>.json`. (Amended 2026-11-25: a failed DEV/staging run may write the separate, opt-in `state/<env>.partial.json`; see rule 2.)
- The runner committing or pushing on its own.

## Approaches considered

1. **Per-environment state files in the bundle (chosen).** Tracked JSON under `state/` next to `release.json`, one file per environment, written only by the runner after a fully successful run. Visible in Git, travels with the bundle, needs no database objects.
2. **Status only in `release.json`** (add `staged` and `stagedAt`, no per-object state). Smaller, but cannot say which object changed after staging, and cannot drive a delta rerun.
3. **Records in the database** (a table or comments on the target). Rejected: the template forbids team metadata tables, and the record would vanish when staging is restored from production.

Owner decisions on behaviour:

- After a change, the bundle returns to `draft` (rejected: refuse `collect` on a staged bundle; keep `staged` and only record the difference).
- A staging rerun applies only what changed, with `--full` available (rejected: always everything; delta with no full option).
- Production requires `staged` with matching fingerprints, override `--no-staging-proof` (rejected: warn only; no check).
- prod-v1 in natrec gets its first record from a `--full` staging rerun after the upgrade (rejected: a backfill command from an old report).
- The state is per repository, which matches one developer per repository.
- One file per environment (`state/<env>.json`) rather than one combined file (rejected: a single `release-state.json`). Per-environment Git history is direct (`git log releases/<slug>/state/staging.json`); readers combine the files.

## Design

### Files

`release.json` gains three fields when staged and keeps the existing production fields:

```json
{ "slug": "prod-v1", "status": "staged",
  "stagedAt": "2026-11-26T09:40:00Z", "stagedTarget": "edn1pod", "stagedCommit": "a46b8c97…" }
```

Accepted statuses: `draft`, `staged`, `deployed`. `deployed` keeps `deployedAt`, `deployedTarget`, `deployedCommit` and adds `deployedWithoutStagingProof: true` only when the override was used.

One state file per environment, `releases/<slug>/state/<env>.json` (`dev.json`, `staging.json`, `prod.json`; owner decision). A file exists only after the first successful run on that environment. Object type, name and schema are not repeated: they come from `release.json`, and the current fingerprint of each step is computed from the file on disk, so no fourth file is needed.

`releases/prod-v1/state/staging.json`:

```json
{ "schemaVersion": 1, "slug": "prod-v1", "env": "staging",
  "lastRun": { "at": "2026-11-26T09:40:00Z", "commit": "a46b8c97", "mode": "delta", "applied": 1, "skipped": 130,
               "report": "scratch/release-reports/prod-v1-staging-….md" },
  "steps": {
    "01-ddl/001_RUNERP_CUSTDATA_stage1_sequences.sql":
      { "sha256": "77ab…", "at": "2026-11-26T08:05:00Z", "commit": "a46b8c97" },
    "02-code/045_RUNERP_CODE_absh_visit_pkg_package_body.sql":
      { "sha256": "b41d…", "liveSha256": "c903…", "at": "2026-11-26T09:40:00Z", "commit": "a46b8c97" } },
  "apps": { "130": { "sourceSha256": "e01f…", "at": "2026-11-26T08:20:00Z" } } }
```

`dev.json` has the same shape and never has `apps` (DEV refuses app deploys). `prod.json` is written once, by the run that locks the bundle.

### Rules

1. **Fingerprint.** sha256 of the file text with line endings normalised to LF. Keyed by the path under the bundle. A renamed file is a new step and is applied again; a moved position in `release.json` is not a change. App fingerprint: sha256 over the sorted relative paths and contents of `apps/*/<app_id>/`.
2. **Writing.** Only the runner writes `state/<env>.json`, only for the environment it ran on, and only at the end of a fully successful run (all phases including verify and apps). Written atomically (temp file and rename), keys sorted, LF. A failed run never writes `state/<env>.json`. Amended 2026-11-25 (the original "writes nothing" rule left a rerun stuck on `ORA-00955` after a halfway failure): a failed DEV or staging run writes `state/<env>.partial.json` with the `01-ddl` files that finished (each committed before its end marker), read only by `release run --resume`, which skips them while their fingerprint is unchanged, and removed after a successful run. Without `--resume` the next run applies the same changes again. Entries for files no longer in `release.json` are dropped on the next write of that environment.
3. **Delta (DEV, staging).** A step runs when its current fingerprint differs from the one recorded for that environment, or has none. `--full` runs every step and rewrites the environment's records. `04-verify/` always runs in full. On staging an app is imported only when its fingerprint changed; production always imports (it runs everything); DEV still refuses apps. Backup covers only the objects of the steps that run. Phase and schema batching keep their order with the skipped steps removed.
4. **Patch steps on a rerun.** A `patch` step re-applied to an environment that already holds an earlier bundle version would fail its `baseSha256` guard. Record, at apply time, the live source hash after the step as `liveSha256` (the post-apply phase already reads the source; rule 12 uses the same value). On a later delta run the guard accepts live == `baseSha256` or live == that recorded post-apply hash. Production, which runs once, keeps the strict `baseSha256` guard.
5. **`new` steps.** Skipped when recorded with the same fingerprint, so a rerun no longer needs `--allow-existing`. A changed `new` step still refuses an existing object unless `--allow-existing`.
6. **Status changes.**
   - `release new` → `draft`.
   - Successful staging run and every step and app fingerprint now equal to staging → `staged` with `stagedAt/Target/Commit`.
   - `release collect` on a `staged` bundle → `draft`, staged fields removed.
   - A hand edit cannot change the JSON. `release list`, `check` and `run` compute the effective status from fingerprints and show `staged (stale: N changed)`; the runner treats a stale bundle as `draft`.
   - Successful production run → `deployed`, as today, with tag `release/<slug>`.
7. **Production check.** Before any write, refuse unless effective status is `staged` and every step and app fingerprint equals the staging record, naming the steps that differ. `--no-staging-proof` skips this; the report and `release.json` record it.
8. **Commits.** The runner prints which files to commit (`release.json`, `state/<env>.json`). The existing production rule that every bundle file is committed at `HEAD` means a staging record cannot be skipped by accident.
9. **Restored staging.** After staging is restored from production, its records no longer describe it. `--full` reapplies everything and rewrites them.
10. **Output.** `release list` shows `prod-v1 (draft: 1 of 131 steps not on staging)`. `release check --env <env>` lists the steps it would apply and skip. The run report lists mode, applied and skipped steps.
11. **Evidence kept in Git (owner-accepted suggestion).** Each file's `lastRun` also stores what the report proves, so it survives cleaning `scratch/` or a move to another machine: target identity per schema (`DB_UNIQUE_NAME`, `CON_NAME`, `USER`), invalid counts before/after/new per schema, phase timings, apps deployed, and the report path:

    ```json
    "lastRun": { "at": "…", "commit": "a46b8c97", "mode": "full", "applied": 131, "skipped": 0,
      "identities": { "RUNERP_CODE": { "dbUniqueName": "edn1pod", "conName": "G5A5C1FE1C978D4_STAGE", "user": "ASHERIF",
                                       "invalidBefore": 30, "invalidAfter": 30, "newInvalid": 0 } },
      "timings": { "preflight": 46.6, "backup": 212.0, "apply": 942.5, "verify": 26.9, "apps": 195.8 },
      "appsDeployed": [130], "report": "scratch/release-reports/prod-v1-staging-….md" }
    ```
12. **Drift warning (owner-accepted suggestion).** For each code step applied, record the live source hash read after the apply (`liveSha256`, the same value rule 4 needs). `release check --env <env>` compares the current live source of recorded objects with it and reports `changed on <env> since our apply` per object. It is a warning in `check`; in `run` a drifted object that the run would skip is applied again instead (it no longer holds our version). Only code objects are compared; DDL steps have no single source to compare.
13. **`release status <slug>` (owner-accepted suggestion).** Offline, read-only; reads `release.json`, the bundle files and every `state/<env>.json`. One row per step and app, one column per environment: `ok` recorded and matching, `old` recorded with an older fingerprint, `-` never applied (builder's call: ASCII, because Windows consoles in cp1252 cannot print ✓). Header shows status, effective status and last run per environment. `--format json` for agents. `--live --env <env>` adds the drift column from rule 12 (read-only query).

    ```
    prod-v1   status: staged   last runs: dev 2026-11-24, staging 2026-11-26, prod –
    STEP                                                       DEV  STAGING  PROD
    01-ddl/001_RUNERP_CUSTDATA_stage1_sequences.sql             ok     ok     -
    02-code/045_RUNERP_CODE_absh_visit_pkg_package_body.sql     old    ok     -
    app 130                                                     -      ok     -
    ```

### Day-3 example

1. `release collect prod-v1 … ABSH_VISIT_PKG --mode replace --file 045_…` changes that file's sha256 to `b41d…`; `release.json` goes to `draft`.
2. `release run prod-v1 --env staging --confirm-target edn1pod` applies that one file, skips 130, runs both checks, skips App 130, records `staging.sha256 = b41d…`, sets `staged`.
3. Commit both files and push. `release run prod-v1 --env prod` runs the whole bundle and locks it.

## Left to the builder

- Make sure bundle validation accepts the `state/` folder (JSON only, never run as SQL, symlink ban still applies) and that the production clean-tree check covers it.
- Function layout inside `scripts/release.py`, JSON write order (stable, sorted keys for small diffs), and where the post-apply source hash is captured.
- Test cases in `tests/test_release.py`, test-first: evidence fields written from the run, drift detection and re-apply of a drifted skipped object, `release status` text and JSON output, delta selection, `--full`, failed run writes nothing, collect returns to draft, stale detection from a hand edit, prod refusal and override, patch rerun guard, app fingerprint skip, file rename, removed step.
- Docs: `docs/release-workflow.md`, README command summary, `AGENTS.md` release paragraph.
- Live check on the local Docker database: DEV run, delta rerun, `--full`.
