# Persistent SQLcl sessions

A deployment command (`rollout`, `migrate`, `feature` prepare, capture, apply and `doctor --live`,
`compare-schema` and `compare-env`) generates many small SQLcl scripts: checks, catalog captures and
applies. Earlier versions started SQLcl once per script. Against a remote database each start cost about
6.5 s, mostly SQLcl's own connect-time settings replay, so start-up was most of a release's time.

Now a command keeps a few SQLcl processes running for its whole run and sends every script to one of them.

## What runs where

- **Writer**: one process per saved connection runs the apply scripts.
- **Readers**: up to `SQLCL_POOL_MAX_READERS` processes (default 2) per connection run everything else:
  preconditions, postconditions and catalog captures.
- A check after an apply never runs in the session that wrote, so it sees only committed data.
- With one login for every schema (acting-as profiles), a whole release uses one writer and one or two
  readers.

## What is reset between scripts

Each process normally reuses its database session. Before every script except its first, a
PL/SQL call does the following:

- rolls back;
- re-enables `COMMIT IN PROCEDURE`;
- sets `CURRENT_SCHEMA` back to the login;
- restores NLS and PL/SQL compiler settings to the values the session started with;
- clears package state (`DBMS_SESSION.RESET_PACKAGE`).

A second call restores the `DBMS_METADATA` transforms after the package reset has taken effect.
SQLcl's own `SET` options come back from a `STORE SET` baseline, and the column, break and compute settings
are cleared. Variables a script defined are undefined, including those defined in the scripts it runs with `@`, `@@` or
`START`, and the positional variables (`&1`, `&2`, ...) an include was given.

If any part of the reset fails, the process ends and the next script gets a new one. A script that fails
still ends its SQLcl process through `WHENEVER SQLERROR EXIT`.

A driver’s timeout includes waiting for a pooled session, startup, reset and script execution. If the
timeout expires or the request is cancelled before dispatch, the driver does not run. A timeout or
cancellation after dispatch terminates the SQLcl process tree. A one-shot fallback uses the time
remaining and requires that the pooled process has never dispatched that driver.

Each script's run folder keeps `sqlcl-output.log` and a `sqlcl-session.json` record
with the driver name, phase, role, transport, outcome, and any cold-start or
replacement reason. Pooled records include the PID and the script's sequence
in that process. Acquisition, startup, reset, and driver time are separate for
pooled runs. One-shot driver time includes its launch and connection.

The rollout report's `sqlcl` section counts driver calls, dispatches by
transport, launcher starts, connection attempts, confirmed session baselines,
resets, and fallbacks.
`mode` is `pooled`, `one-shot`, `mixed`, or `none` when no driver was dispatched.
`processesStarted` counts launcher starts and `scripts` counts dispatched
drivers. `databaseSessions` counts confirmed sessions for pooled-only runs.
It is null for one-shot and mixed runs because their connection attempts do not
independently confirm session counts. Summed driver timings can overlap when
readers run concurrently. These diagnostics are informational and outside
staging-proof validation.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `SQLCL_PERSISTENT_SESSIONS` | `1` | `0` starts one SQLcl per script, as earlier versions did |
| `SQLCL_POOL_MAX_READERS` | `2` | Read-only processes per connection, 1 to 4 |

An explicit process environment value overrides the root `.env` for these two settings. For example,
`SQLCL_PERSISTENT_SESSIONS=0 scripts/team.sh rollout ...` disables pooling for that invocation.

## Limits

- **Session state the reset cannot clear.** A reset cannot clear rows a script leaves in a session-duration
  global temporary table, application contexts it sets, roles changed with `SET ROLE`, or session events.
  No generated script does these. A project script that does should run with `SQLCL_PERSISTENT_SESSIONS=0`.
- **Owner logins without catalog access.** When the login cannot read `V$PARAMETER` (often an owner login
  on DEV without catalog access), the writer reconnects before each apply instead of reusing its session.
  That reconnect costs about a SQLcl start.
- **Windows** runs one SQLcl per script for now. The pool needs POSIX named pipes.
- **APEX imports** (`deploy`, `publish`) keep their own SQLcl processes.
