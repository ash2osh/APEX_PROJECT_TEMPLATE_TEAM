# DEV test runner

Use `dev-test` to inspect an existing development schema and run a small,
explicit database qualification suite against it. The runner uses the schema
owner's existing DEV connection. It requires `--env dev`, rejects identities
that look production, does not create a schema, ask for DBA access, apply
application migrations, or import an APEX app. A DEV database may still contain
production-derived records, so use synthetic run-tagged fixtures and inspect the
observed target before every run.

Planning is read-only. Running a suite performs its declared DDL and DML. The
default built-in suite checks six template behaviors. A project suite under
`dev-tests/` lets a team exercise its own behavior on explicitly named,
run-tagged fixtures. The runner keeps each run's input, intent, output and
verification evidence under `scratch/dev-test/<run-id>/`, outside the
application's migration folders.

## Start with a plan

Configure the DEV migration profile (or the existing DEV fallback profile)
with a saved SQLcl connection that logs in as the schema owner. Never put
credentials in `.env`. For one configured schema, `--schema` may be omitted;
for multiple schemas, select the intended owner explicitly.

```bash
scripts/team.sh dev-test --env dev --schema APP --plan
scripts/team.sh dev-test --env dev --schema APP --run
scripts/team.sh dev-test --report <run-id> --format markdown
```

`APP` is an example, not a detected target. The plan records the actual
database, service, container, owner, SQLcl/APEX versions, prerequisites,
planned fixture names and coverage. Review it before choosing `--run`. A run
rechecks its target and prerequisites and freezes its own source; it does not
trust an earlier plan as permission to write.

The default run selects the complete six-scenario built-in suite. To run a
project suite, provide it explicitly:

```bash
scripts/team.sh dev-test --env dev --schema APP \
  --suite dev-tests/suites/orders.json --scenario cancel-order --plan
scripts/team.sh dev-test --env dev --schema APP \
  --suite dev-tests/suites/orders.json --scenario cancel-order --run
```

Project suites run only their declared scenarios; they are not combined with
the built-in suite. Repeating `--scenario` selects more scenarios, but each
selected scenario's declared dependencies must also be selected. The plan's
coverage describes only the selected scenarios.

Both `scripts/team.sh` and `scripts/team.ps1` accept the same arguments. Help,
saved-report display and report formatting work without a database connection.
Every plan/run saves `report.json` and `report.md`. A report can be rendered
again later without reading `.env` or starting SQLcl.

## Use the sample project suite

Copy the example files into the project-owned suite directory:

```bash
mkdir -p dev-tests/suites/sql
cp docs/dev-test-examples/example.json dev-tests/suites/example.json
cp docs/dev-test-examples/sql/create-and-seed.sql dev-tests/suites/sql/create-and-seed.sql
```

Then plan and run it with `--suite dev-tests/suites/example.json`. It creates a
new generated table, inserts two synthetic rows tagged with the run ID, commits
them and checks that exactly two tagged rows are present. The example has
`cleanup: null`: those rows and the generated table stay in the schema for
inspection and manual management. Template upgrades protect the complete
`dev-tests/` tree, including SQL files, so they will not overwrite or delete
project scenarios.

## Dependencies, privileges and space

Declare required objects and members under `requirements`. The plan checks
their visible owner/name/type/status and reports `satisfied`, `missing` or
`unknown`; limited metadata visibility is not treated as proof that an object
or grant is absent. A missing dependency blocks the affected scenario and its
dependents. Independent eligible scenarios may still run.

The `datatype-roundtrip` built-in uses SQL `BOOLEAN`, which requires Oracle
Database 23ai or newer. On an older target, planning blocks that scenario
before fixture creation while leaving other eligible scenarios available.

The runner never creates a missing cross-schema dependency, grants access,
recompiles application objects or transfers code from another schema. If a
fixture depends on a package or table in another owner, have that object's
owner follow the normal project deployment path and have the DBA grant only
the access the application needs. Then rerun the plan. Do not use a copied
fixture implementation to claim that the original dependency compiled.

The schema owner does not need DBA-wide space views for a plan to succeed.
Visible quota/free-space details are advisory, and planning cannot guarantee
that the tablespace still has room when the run starts. If Oracle refuses a
fixture for quota or allocation, keep the report, reduce the fixture if that
preserves meaningful coverage, or ask the DBA about the existing schema's
space. The runner does not create another schema or retry an ambiguous write
automatically.

## Retention, cleanup and interruption

All runs retain fixtures by default, including expected invalid objects and
synthetic rows. A retained fixture is an expected result, not a failed cleanup.
There is no background cleanup policy. The optional command
`scripts/team.sh dev-test --cleanup <run-id>` removes only supported artifacts
recorded for that run after rechecking target identity and ownership. It
accepts no environment, schema or target override. A project scenario with
`cleanup: null` is unsupported for automatic row cleanup; manage its test rows
manually using the recorded run ID.

Exit codes are 0 for verified success, 1 for a verified assertion failure, 2
for a refusal, blocked/incomplete coverage or an ambiguous possible write, and
130 when interrupted before any write could have started. After a possible
write, interruption is `UNKNOWN` and exits 2. Inspect the saved report and
evidence before starting a new run; the runner never resumes or replays a
write attempt.

## Limits and safe use

Project SQL is trusted input, not sandboxed code. The manifest and report
describe the declared effects but cannot prove the absence of indirect
triggers, autonomous work, shared database triggers or external effects. DDL
and explicit `COMMIT` can persist independently of a surrounding rollback.
Use a development database that is appropriate for fixture writes, review the
suite and its affected-object declarations, and coordinate when its owner is
shared with other developers. A DEV clone can contain production-derived data;
the identity guard classifies the target, but cannot certify the provenance of
its rows. Keep test data synthetic and tagged by run ID. Keep fixture size
small; separate generated names do not isolate database resources.

Raw SQLcl session logs remain in the local run folder and may contain sensitive
diagnostics. Reports summarize evidence, but review logs before sharing any
run folder outside the project team. A successful plan proves only the
recorded prerequisites and target observations at planning time; it does not
prove application business behavior or guarantee a later write will succeed.
