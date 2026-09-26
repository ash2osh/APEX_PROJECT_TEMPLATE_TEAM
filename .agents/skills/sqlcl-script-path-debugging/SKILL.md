---
name: sqlcl-script-path-debugging
description: Use when nested SQLcl scripts load an unexpected file, generated driver scripts fail or run the wrong content, or relative SPOOL and include paths behave differently from the shell working directory.
---

# Debug SQLcl script and include paths

Nested SQLcl scripts can involve several paths at once: the process working
directory, the active script's directory, and the destination of a `SPOOL`
command. Record the actual execution context before changing a relative path.

## Inspect the running script stack

On SQLcl releases that support them, put these read-only diagnostics near the
relative include or generated-file step:

```sql
PWD
SHOW SCRIPTS
SHOW LEVEL
```

`PWD` reports the current script directory during script execution;
`SHOW SCRIPTS` reports the nested call tree; and `SHOW LEVEL` identifies the
active script and nesting depth. Check the installed SQLcl help if a command
is unavailable in the target release.

## Make nested paths explicit

- Oracle documents `@@` as the nested-script form that searches relative to
  the calling script. Prefer it for includes intended to travel with a
  script.
- Use plain `@` only when the path is deliberately rooted at the SQLcl
  execution context and that context has been verified. Do not assume an
  `@relative.sql` include and a `SPOOL relative.sql` output use the same base.
- Avoid a thin redirect script that invokes a shared script when the shared
  script contains more relative includes whose intended directory is unclear.
  Keep a driver beside its own includes or pass a deliberate, explicit base
  path through the whole call chain.
- When one script generates a driver and immediately runs it, verify that the
  generated file exists at the path SQLcl will load, and inspect its contents
  before executing it.

One source repository observed a plain `@` include resolving against the
active script path after a thin redirect, while the preceding `SPOOL` output
landed under the process working directory. Treat that as a reason to inspect
the actual stack and paths in the target SQLcl version, not as a universal
claim about all invocations.

## Guard schema-sensitive scripts

If a script's purpose depends on its connected schema, fail before its first
DDL or DML when the connection identity does not match the expected target.
Use `WHENEVER SQLERROR EXIT SQL.SQLCODE` and compare
`SYS_CONTEXT('USERENV','CURRENT_SCHEMA')` with the explicitly supplied
expected schema. Never infer the schema from a folder name or saved-connection
label.

For scripts that export generated files, start from a clean output directory,
verify the loaded driver's object list against the target schema, and check
the complete SQLcl output for client and database errors before accepting the
mirror.

When a script runs through SQLcl MCP with `-R 0`, also use
`sqlcl-mcp-r0` to review its database, filesystem, and OS effects.

## References

- [SQLcl script commands](https://docs.oracle.com/en/database/oracle/sql-developer-command-line/26.1/sqcug/working-sqlcl.html)
- [SQLcl `SHOW LEVEL` and `SHOW SCRIPTS`](https://docs.oracle.com/en/database/oracle/sql-developer-command-line/26.1/sqcug/show-level-and-show-scripts.html)
