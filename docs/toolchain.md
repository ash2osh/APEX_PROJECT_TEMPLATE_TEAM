# Qualified team toolchain

The team workflow is intentionally conservative about the tools that can touch
an application. A Python core launches SQLcl with an argv list, a regular
empty stdin file, UTF-8 output, and a generated driver that records identity
before and after the payload. Shell and PowerShell files are compatibility
launchers only.

Before a live run, record the outputs of `sql -V`, `java -version`, and
the APEX release shown by the verified identity/doctor query. The explicit
Docker qualification is available when the environment owner supplies
`TEAM_LIVE_ENV`:

```bash
TEAM_LIVE_ENV=scratch/live-docker.env \\
  PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests/live -v
```

One SQLcl process is bounded by `TEAM_SQLCL_TIMEOUT` seconds, defaulting to 120.
APEX export and import use a longer built-in budget because a timeout there
marks the shared application uncertain and pauses the team. Raise the variable
for a slow link; do not lower it below the time a metadata write needs.

That suite expects the five credential-free profile names to resolve through
the local SQLcl connection store, exercises the full inventory, Oracle app and
migration mutexes, and performs only a same-source master import plus a
temporary table migration. A disposable CI run must still use explicitly
provisioned application IDs; `docker-demo` is not a default target for
destructive tests.

APEX exports are accepted only when the run has two matching identity
observations, a positive operation completion record, `application.apx`,
`.apex/apexlang.json`, and a complete owned tree. `.apx` and APEXlang metadata
are normalized to LF; binaries are hashed byte-for-byte. Production drivers are
SELECT-only and all writes are rejected before SQLcl starts.

`INSTANCE_ID` is the verified `INSTANCE_NAME@SERVER_HOST` pair. The host suffix
is intentional: cloned Oracle Free containers can otherwise all report the
same `INSTANCE_NAME` (`FREE`) and incorrectly share a physical lock key.

The offline portability gate is:

```bash
for script in scripts/*.sh; do bash -n "$script"; done
PYTHONPATH=scripts python3 -m unittest discover -s scripts/tests -v
```
