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
