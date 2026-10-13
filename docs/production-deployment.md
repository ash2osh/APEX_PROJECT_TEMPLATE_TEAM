# Production deployment runbook

Use this runbook to prepare a release packet, execute one reviewed rollout
manifest, and collect post-window evidence. The manifest coordinates ordered
steps; each command still uses its existing connection, checks, and target
guards. A green source/readiness report does not prove that an APEX application
works at runtime.

## Before the window

1. **Freeze and identify the release.** Name the release commit, migration
   folders, application IDs, workspace, parsing schemas, descriptors, ORDS
   files, and target environment. Confirm the saved connection and expected
   user through the configured profile. For multi-schema projects, map each
   source and target owner explicitly.
2. **Compare the environments.** Capture the full selected catalog and review
   every blocker, difference, and `not compared` section:

   ```bash
   scripts/team.sh compare-env --from staging --to prod --format markdown
   ```

   Use `--emit-dba-script <path>` only when DBA-level differences are in scope.
   The output is an additive review artifact. Inspect every statement and hash
   it before adding it as a `sql-script` step; the comparison never applies it.
3. **Build and review migration candidates.** With a reviewed
   `baseline.json`, export the source-side catalog and build against the named
   target. Reference-data generation is optional; choose one build command.
   For a structure, grants, and source build:

   ```bash
   scripts/team.sh baseline export-source --from staging
   scripts/team.sh baseline export-grants --from staging
   scripts/team.sh baseline build --from staging --to prod
   ```

   To include the configured reference-data allowlist instead, capture the
   source rows before building with `--data`:

   ```bash
   scripts/team.sh baseline export-data --from staging
   scripts/team.sh baseline build --from staging --to prod --data
   ```

   `baseline build` writes migration files only. Review each generated SQL
   statement, `checks.json`, target mapping, excluded reference-data columns,
   and README before selecting a folder. It is additive: it does not apply a
   migration or update/delete existing reference rows. `GENERATED ALWAYS`
   identity values are assigned by Oracle; this generator does not emit a
   portable `OVERRIDING SYSTEM VALUE` insert.
4. **Check declared preconditions and rehearse eligible DML.** Run read-only
   checks against the selected target and rehearse only on a target explicitly
   approved for those DML statements:

   ```bash
   scripts/team.sh verify migrations/APP/2026-10-11_release-r001 \
     --env staging --phase pre
   scripts/team.sh migrate migrations/APP/2026-10-11_release-r001 \
     --env staging --rehearse --report scratch/release-rehearsal.json
   ```

   Rehearsal executes eligible DML in a transaction and then rolls it back. It
   skips DDL, implicit-commit statements, PL/SQL, and SQL it cannot prove
   transactional. Its scalar checks do not prove that sequence or identity
   counters, autonomous trigger work, external effects, or DDL effects were
   undone. Review those effects and cleanup separately. Rehearsal on staging
   does not establish the production starting state; verify production
   preconditions separately.
5. **Agree on recovery before scheduling the window.** Obtain the DBA's
   independently reviewed and approved restore-point/backup and recovery plan
   for the exact production target. Name the recovery decision owner, executor,
   evidence location, and time budget. Store the approved plan reference and
   its exact instructions in the release packet. This template supplies no
   universal restore-point command or automatic rollback procedure.
6. **Assemble and dry-run the manifest.** Use
   [`rollout-manifest.example.json`](rollout-manifest.example.json) as the
   schema reference. Include ordered `verify`, `migrate`, reviewed `sql-script`
   or `ords-import`, `app-deploy`, and `pause` steps only as needed. Put a
   browser-smoke-check pause after imports when an app is included. Paths must
   point to reviewed, committed inputs in the checkout. `release/rollout.json`
   in the examples below is the operator's reviewed manifest path; this
   template does not ship a production rollout manifest.

   ```bash
   scripts/team.sh rollout release/rollout.json --env prod \
     --dry-run --report scratch/production-rollout-review
   ```

   Review the manifest and input hashes, frozen target binding, workspace and
   schema display, report pair, and exact confirmation text. A dry run validates
   and hashes local inputs; it does not execute the release steps.

## During the window

Execute the reviewed manifest once from the release checkout:

```bash
scripts/team.sh rollout release/rollout.json --env prod \
  --report scratch/production-rollout-2026-10-11
```

Staging and production display one manifest-level confirmation before steps
start. The runner checks the selected target binding and each source hash at
step boundaries, records per-step evidence, and writes local receipts only for
successful steps. Keep the operator and recovery owner available through the
whole run. A failed or interrupted write can have an unknown outcome; inspect
the retained report, SQLcl evidence, and live target before a retry. Resume only
from validated earlier receipts and a fresh target identity observation:

```bash
scripts/team.sh rollout release/rollout.json --env prod \
  --from-step 4 --report scratch/production-rollout-resume-2026-10-11
```

Resume refuses a legacy receipt without independently observed target identity.
Preserve the original receipts, reports, SQLcl evidence, and immutable migration
folders. Inspect the live target independently and obtain a reviewed
continuation plan before any further writes. Do not edit or fabricate receipt
identity, delete evidence to bypass the refusal, or replay completed writes to
manufacture a fresh receipt. A receipt's observed
database/service/container/schema/user tuple is useful target evidence; it is
not proof of globally unique physical instance or clone identity.

### Window budget

The default SQLcl apply timeout is 300 seconds per session. Verification has a
separate configurable timeout and byte budget; a single oversized check is
refused before SQLcl starts. The 300-second setting is not a total rollout
deadline. A manifest can open multiple SQLcl sessions for migration,
verification, DBA scripts, ORDS, or app deployment. Estimate each step from a
rehearsal or prior run on a comparable target, include connection setup and
post-check time, and leave the recovery owner enough time to decide. Use the
rollout report's measured step durations after a run; do not substitute a
generic speed estimate for target evidence.

## After the window

- Require a successful rollout report and matching step receipts for every
  required write. Retain the manifest digest, input hashes, confirmation
  evidence, reports, and evidence paths in the release packet.
- Run every declared postcondition and any wider scoped checks required by the
  release:

  ```bash
  scripts/team.sh verify migrations/APP/2026-10-11_release-r001 \
    --env prod --phase post
  scripts/team.sh compare-env --from staging --to prod --format markdown
  ```

- Perform the approved browser smoke checks against the target application.
  Observe expected pages, authentication, task/workflow behavior, and relevant
  user paths. Record the tester, target, app ID/version, time, and result.
- Inspect scoped automation, workflow, and task observations. An import or a
  passing source comparison does not authorize enabling an automation, resuming
  a workflow, or repairing a task. Follow the existing runtime recovery gates.
- Record unresolved differences, skipped sections, unavailable checks, and
  follow-up owners. Do not label unavailable evidence as passed.

## Rollback and recovery

The rollout manifest is ordered execution and evidence, not a reverse-migration
generator. If the release needs rollback, the named DBA and release owner use
the exact independently approved recovery plan for the selected database,
schema, application, and point in time. Preserve the original receipts and
reports, record the recovery decision and actions, then re-run identity,
schema, declared-check, and browser verification. Reconcile any ambiguous write
before resuming or applying a correction. Do not infer recovery from a client
exit code or from DML rehearsal's scalar rollback checks.

## Release packet checklist

```text
Release / commit / window:
Target environment / saved alias / expected user:
Observed database / service / schema mapping:
Application ID / workspace / source version / deployment descriptor:
Manifest path / SHA-256:
Migration, ORDS, DBA-script, and descriptor hashes:
Pre-window compare-env / export / verify / rehearsal evidence:
Operator confirmation / approver / timestamp:
Rollout report / receipts / per-step evidence paths:
Post-window verify / compare-env / browser-check evidence:
Approved DBA recovery-plan reference / approval:
Recovery decision owner / executor / contact:
Release owner / evidence custodian:
Open differences / unavailable checks / follow-up owners:
```

See [migration and rollout rules](migration-rules.md#rollout) for manifest
properties, receipt rules, and exit codes.
