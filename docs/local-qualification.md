# Local template qualification

Routine CI runs offline tests. These commands require a separately authorized,
explicit local Oracle target; they are never run automatically by CI, `doctor`
or `verify-local`. Use SQLcl 26.3.0.0 or newer with APEX 26.2. Obtain the actual
host, service, workspace and saved connection from the selected installation.
Keep reports and SQLcl diagnostics under ignored `scratch/` or `.sync-state/`.
Reports contain identities, counts and statuses, without credentials, task
payloads, checksum salts or full SQLcl output.

## Read-only observations

```bash
python3 tools/qualify_template_readonly.py \
  --connection <saved-alias> --expected-user <DB_USER> --schema <SCHEMA> \
  --workspace <WORKSPACE> --server-host <local-host> --service <service> \
  --app-id <existing-numeric-id> --output scratch/qualification-<unique-run>
```

The output directory must be new, physically inside this checkout's `scratch/`,
without symlinked parents. The runner verifies the client version and exact
target before reading the owner catalog and application lifecycle. Each adapter
repeats identity/context guards in a read-only transaction and exits with
rollback. It does not export application source, import, migrate, enable an
automation or create a fixture. Exit 0 means all observations passed; exit 2
means a refusal or unavailable check. Inspect `qualification.json` for individual
durations, coverage and limitations; empty arrays do not establish positive
runtime qualification.

The catalog check uses the production gzip/base64 decoder against the real owner
inventory. Large synthetic Unicode transport is separate evidence, not proof
that this owner has a large inventory. See [qualification results](apex-26.2-qualification.md).

## Disposable lifecycle writes

After authorization of the exact unused application ID and workspace/schema:

```bash
python3 tools/probe_apex_26_2.py \
  --connection <saved-alias> --expected-user <DB_USER> --schema <SCHEMA> \
  --workspace <WORKSPACE> --server-host <local-host> --service <service> \
  --app-id <unused-numeric-id> --allow-writes --lifecycle \
  --source-26-2 <reviewed-26.2-fixture-directory> --developer <existing-Builder-user> \
  --report-dir scratch/lifecycle-<unique-run>
```

Use a reviewed, minimal fixture without initialization or supporting-object
installation code. The probe adds a disabled automation with a no-op action,
a waiting workflow and an action task. It uses an existing workspace developer;
it does not create a developer. It checks the unused ID again before import and
records the run-created inventory before any write attempt. Every later write
checks the numeric ID, opaque run alias, workspace, schema and database identity.
It creates an APEX session only for this disposable app to start its workflow
and task, then deletes that session.

The scenario observes native full-import behavior and exercises the actual
production export, full publish and selected-page publish wrappers in a private
checkout. A controlled post-import observation failure in that checkout tests
baseline, native lock and source/lifecycle recovery evidence. Temporary Git
commits belong only to this ignored fixture checkout. The original repository
and existing applications are not published by the probe.

Fixtures remain after success, interruption or an ambiguous import. Review the
report and reconcile retained locks before explicitly removing the owned app:

```bash
python3 tools/probe_apex_26_2.py \
  --connection <saved-alias> --expected-user <DB_USER> --schema <SCHEMA> \
  --workspace <WORKSPACE> --server-host <local-host> --service <service> \
  --app-id <same-id> --allow-writes \
  --cleanup-report scratch/lifecycle-<unique-run>/report.json \
  --report-dir scratch/lifecycle-cleanup-<unique-run>
```

Cleanup verifies the exact saved inventory and live alias, and refuses a lock
owned by another operation. Public app removal must leave no application,
workflow or task rows for that ID before `cleanupRequired` becomes false.
Keep the original report and cleanup report together. The existing two-developer
conversion/lock scenario remains available through `--source-26-1` and
`--developers`; those arguments do not qualify a live APEX 26.1 installation.

Linux Bash or PowerShell tests cannot establish native Windows behavior. APEX
26.1 performance changes require separate qualification on its own client/database
pair. Staging and production runtime behavior must be qualified separately.

## Deployment-speedup qualification on 2026-10-11

The root task separately qualified the deployment tooling against local
disposable Docker schema `DEMO2` on Oracle Database 23.26.3.0.0, APEX 26.2.0,
and SQLcl 26.3.0.0. The complete outcome is summarized in ignored local evidence
at `scratch/deployment-review-live-20261011/qualification-summary.json`; the
referenced reports and SQLcl output remain in that ignored directory.

The current command interfaces are:

```bash
scripts/team.sh query --env dev --connection tables --schema DEMO2 "SELECT table_name FROM user_tables"
scripts/team.sh verify migrations/DEMO2/2026-10-11_release-r001 --env dev --phase pre
scripts/team.sh migrate migrations/DEMO2/2026-10-11_release-r001 --env dev --rehearse \
  --report scratch/rehearsal.json
scripts/team.sh compare-env --from dev --to staging --format markdown
scripts/team.sh baseline export-source --from dev
scripts/team.sh rollout release/rollout.json --env dev --dry-run
scripts/team.sh rollout release/rollout.json --env dev --report scratch/rollout
```

Replace example paths and profiles with configured local values. `query`, `verify`,
`compare-env`, and `baseline export-source` are read-only. `migrate
--rehearse` executes eligible DML on the selected real database and attempts a
transaction rollback; it is not an offline test. `rollout --dry-run` validates
and hashes local inputs without executing the manifest. The offline suite uses
fakes and recorded fixtures for command flow and refusal boundaries; it cannot
establish Oracle behavior by itself.

Baseline export commands take their schema scope from `PROJECT_SCHEMA` when it
is configured; otherwise they use the schema list in `baseline.json`. They do
not accept `--schema`; that option is supported by `baseline build`.

The native run observed these scoped results:

- The configured-profile query preserved the reviewed Unicode/CRLF/q-quoted
  literal cases and token-like text. A query that attempted an autonomous
  commit was refused with `ORA-00034`; the fixture table remained empty. The
  row-limit and output-byte-limit cases refused with empty stdout.
- The repaired owner-scoped `verify`, `compare-env`, and baseline source export
  completed. The recorded comparison covered tables, columns, and stored code
  in the same selected schema and found no differences. It was not a distinct
  source-to-target deployment comparison.
- Native source verification now rejects a same-line-count mutation that replaces
  an expected blank source line with a declaration: the original check returned
  `1`, the mutated check returned `0`, and the source was restored. External
  synonym checks accepted a directly granted TABLE and refused a SELECT-granted
  SEQUENCE. The privileged materialized-view case used the observed
  `TABLE`/namespace `1` plus `MATERIALIZED VIEW`/namespace `19` pair; other
  type/namespace combinations in that run were refused. A later offline
  regression covers same-name `INDEX`/namespace `4` rows beside TABLE and
  materialized-view targets; this mixed-index case has not had separate native
  Oracle qualification.
- Native ORDS 26.3 capture read a separate `NO AUTHENTICATION`,
  `NOT_PUBLISHED` fixture. It detected first-line and late-body source changes,
  and completed long supplementary-character, empty-CLOB, and 20,000-newline
  cases. No endpoint was called and no handler body was executed.
- DML rehearsal passed both folders' postconditions and rollback checks. The
  second folder began at scalar value `0`, observed its apply-boundary value
  `1`, and returned to `0` after rollback; an independent fresh connection
  observed zero rows.
- The accepted rollout executed its INSERT and UPDATE folders, then a verify
  step. Schema-2 receipts bound the configured target and independently
  observed database/service/container/schema/user tuple. Resume at step 2
  validated the retained step-1 receipt and fresh target identity, then ran the
  verify step without replaying the migration payload. A separate post-run
  verification returned the expected check value `1`, and an independent
  connection observed the committed fixture row.
- Final independent cleanup inventory found zero owned DEMO2 fixture objects
  and recycle-bin entries, zero objects/grants/recycle-bin entries in the
  temporary ORDS schema, and zero temporary ORDS schemas or owned DEMO2
  synonyms.

Evidence paths are `project-task11-native-final/rollout-after-task11.json`,
`project-task11-native-final/rollout-resume-after-task11.json`,
`task11-verify.log`, `task11-rollout-independent-row-check/sqlcl-output.log`,
`source-blank-after-task12/sqlcl-output.log`, `ords-original-pagination-fixed.json`,
`ords-longemoji-boundary.json`, `ords-empty-boundary.json`,
`ords-newline-boundary-fixed.json`,
`materialized-view-namespace-after-restart/sqlcl-output.log`,
`fixture-cleanup/sqlcl-output.log`, and
`final-fixture-inventory/sqlcl-output.log`, all under
`scratch/deployment-review-live-20261011/`. The final inventory used an
independent SYS query. The observed identity tuple is scoped target evidence;
it does not prove globally unique physical database or clone identity.

This qualification does not establish native Windows behavior, an APEX 26.1
runtime/backport, a shared-app import, a promotion to a distinct target, or a
tolerant APEX patch-pair profile. It performed no APEX application publish or
import. Treat these as unknown until a separate exact-target qualification is
authorized and recorded.
