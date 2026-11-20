# Live verification of the baseline generator

`scripts/live_verify.py` checks the baseline generator's output and its catalog assumptions against a
real Oracle database. It runs the DDL and checks that the generator emits through SQLcl, then reads back
what Oracle recorded. The script has 32 checks in seven scenarios, followed by one cleanup check. It creates objects only in a
disposable schema, and it is run by hand. It is not part of CI, `doctor` or `verify-local`; those stay
offline.

## When to run it

- After changing `scripts/baseline_bundles.py`, the catalog SQL it uses, or the DDL the generator emits.
- After moving the baseline to a new Oracle release.

## Scenarios

Run `python3 scripts/live_verify.py --list` to print the names. Each scenario is one of these:

| Scenario | What it proves |
| --- | --- |
| `check_replacement` | A reviewed CHECK constraint is replaced by a stricter one, and a re-run is a no-op. A stale review (the live condition or status changed after review) is refused with ORA-20994 and leaves the constraint alone. A primary key with the same name is refused with ORA-20993. The precondition query counts the rows that would violate the new condition. A DISABLED constraint stays DISABLED. Quoted identifiers compare equal after spaces outside quotes are normalised. The run also confirms that a plain ADD after a DROP fails, which is the hazard the precondition guards against. |
| `sequence` | Sequence sync keeps the target's own increment, moves the next value past `MAX(id)`, and a second run changes nothing. |
| `type_owner` | An owner-qualified column type in `ADD COLUMN` executes, and the column postcondition returns 1 for the owner and 0 when the owner differs. |
| `fk_order` | Tables built from catalog-shaped references are ordered parent first, and creating them in that order with the foreign key succeeds. |
| `grants_and_catalog` | `ALL_TAB_PRIVS.GRANTABLE` reads `NO` for a plain grant. `GRANT ALL` is recorded as individual privileges, never as `ALL`. A column grant does not appear as `UPDATE(TOTAL)`. |
| `hidden_columns` | The catalog marks an INVISIBLE column with `HIDDEN_COLUMN = 'YES'` and `USER_GENERATED = 'YES'`. A virtual column's expression is available in `DATA_DEFAULT`. A system-generated hidden column from a function-based index has `USER_GENERATED = 'NO'`. |
| `invisible` | `ADD COLUMN ... INVISIBLE` executes and its postcondition returns 1. A visible column's postcondition does not need `HIDDEN_COLUMN`. `INVISIBLE` followed by `GENERATED ... AS IDENTITY` executes in that clause order. The generator classifies the live columns correctly. |

## Prerequisites

- A disposable Oracle database. The supported floor is Oracle 23ai; use a newer release where you can.
  A local Docker container is fine.
- A SQLcl saved connection that logs in as the throwaway schema owner. The script runs `sql` from `PATH`
  unless you pass `--sqlcl`.
- A schema that may be written to. The scenarios create and drop tables, sequences, types, indexes and
  grants to `PUBLIC`, all with names that start with `LVF_`.

## Command

Bash:

```bash
python3 scripts/live_verify.py --connection <saved connection> --schema <SCHEMA> --confirm-target <db_unique_name>
python3 scripts/live_verify.py --connection <saved connection> --schema <SCHEMA> --confirm-target <db_unique_name> \
  --scenario invisible
python3 scripts/live_verify.py --list
```

PowerShell:

```powershell
python scripts\live_verify.py --connection <saved connection> --schema <SCHEMA> --confirm-target <db_unique_name>
```

Options:

- `--scenario NAME` runs one scenario. It can be repeated, and the `scenario_` prefix is optional.
- `--sqlcl PATH` names the SQLcl executable when `sql` is not on `PATH`.
- `--list` prints the scenario names and exits. It needs no database.

`--confirm-target` takes the database unique name that the database reports. Get it from the database
itself; do not guess it.

## Safety rules

- Before anything is created or dropped, one read-only query reads the connected user and the database
  unique name.
- The run goes ahead only when the user equals `--schema` and `--confirm-target` equals the reported
  database unique name (both compared case-insensitively). Otherwise it stops with exit 2 and changes
  nothing.
- The script creates and drops only objects whose names start with `LVF_`.
- Cleanup runs before the first scenario as well as after each run. Any `LVF_` objects already in the
  schema are dropped at the start of the run.
- The developer types the target name. An agent must not supply `--confirm-target` unless the developer
  typed that exact name for the run.
- Never run it against shared DEV, staging or production. Use a disposable database.

## Exit codes

- `0`: every check passed.
- `1`: a check failed, a scenario raised an exception, or cleanup left `LVF_` objects behind.
- `2`: a usage error (for example a missing required option), SQLcl was not found, or the safety guard
  refused the run. Nothing was created or dropped.

## When a check fails

The failing line is printed as `FAIL <name>` followed by the detail in brackets, and the summary lists it
again at the end. Then:

1. Read the printed detail. It holds the SQL output or the traceback.
2. Rerun only that scenario with `--scenario <name>`.
3. Check whether the Oracle release or the generator changed since the scenario last passed.

Cleanup runs even after a failure and prints `cleanup: objects left = N`. A count of 0 is expected. If the
count is not 0, the run exits 1 and the summary reports `cleanup leaves no LVF_ objects`. Drop the
remaining `LVF_` objects in the throwaway schema before the next run.

## Known limits

- The checks cover the generator and the catalog on one Oracle release at a time.
- The script has not yet been run against a live database on Windows. On Windows it starts SQLcl with an empty input
  pipe (SQLcl aborts with `java.io.IOException: Incorrect function` and still exits 0 when its input is the NUL device)
  and inside a job object, so a timeout ends the java.exe that sql.exe starts.
- An `@script` path that contains spaces worked with SQLcl 26.3 on Windows in a check without a database connection.

See also [Live feature-release checks](live-feature-tests.md) for the feature-release path, and
[Local qualification](local-qualification.md) for the read-only observations against a local target.
