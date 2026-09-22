# Local three-developer E2E acceptance

This runbook exercises Alice, Bob, and Carol in independent local Git clones
against one disposable shared APEX fixture. It is deliberately bounded to the
`docker-demo` database, the `docker-sys` administrative connection, app `9099`
(`TEAM-E2E-9099`), schema `TEAM_E2E_META`, and the tracked alias `team-e2e`.
Production targets are refused.

## Prerequisites

- SQLcl and Git are installed and available on `PATH`.
- `docker-demo` and `docker-sys` resolve to the same development database.
- The disposable APEX workspace contains seed app `103` and the caller has
  permission to export/import the fixture app.
- ORDS is running if runtime rendering is to be proven. Without a protected
  ORDS/browser runner, runtime status remains `UNKNOWN` and the report must not
  be called a complete browser acceptance.

## Lifecycle

Run preflight first. It performs read-only identity, workspace, application,
schema, saved-connection, and seed checks and prints the run ID:

```bash
python3 scripts/local-team-e2e.py preflight \
  --run-root scratch/local-team-e2e/manual
```

The full `run` phase is safe to retain for inspection with
`--keep-on-success`:

```bash
python3 scripts/local-team-e2e.py run \
  --run-root scratch/local-team-e2e/manual \
  --keep-on-success
```

The command is fail-closed: if the protected ORDS/browser smoke contract is
not supplied, it records `UNKNOWN` after preflight and retains the run. It
never turns a missing runtime/browser layer into a synthetic `PASS`.

Inspect a live or retained run at any time:

```bash
python3 scripts/local-team-e2e.py status \
  --run-root scratch/local-team-e2e/manual
```

On PASS, the default lifecycle removes only the exact owned app `9099`, the
run-owned `TEAM_E2E_META` controller schema, the generated saved connection,
and run-owned clone/remote directories. On FAIL or UNKNOWN, the fixture is
retained; the run retains failure evidence for inspection. Inspect `status` and
the report before deciding whether cleanup is safe. Cleanup requires the exact
run ID printed by preflight:

```bash
python3 scripts/local-team-e2e.py cleanup \
  --run-root scratch/local-team-e2e/manual \
  --confirm-run-id <run-id printed by preflight>
```

Cleanup rechecks physical database identity, workspace, app alias/ID, the
fixture ownership marker, and unrelated seed app `103`/schema `DEMO`. An
unknown deletion result is retained as `UNKNOWN`; it is not retried by
overwriting the shared Builder workspace.

## Evidence and limits

The report binds the source commit, run ID, live identity, fixture ownership,
checkout UUIDs, clone/live tree digests, migration frontier/history, mutex and
roster evidence, command hashes, runtime status, and cleanup status. Credentials,
raw environment dumps, and paths outside the run root are rejected.

The runtime smoke proves HTTP success and a final visible marker only. It does
not prove Builder editing, page-lock UX, login roles, or a browser matrix.
Uncaptured transient Builder edits and arbitrary out-of-band DML remain outside
the supported inventory and are reported as coverage limits.
