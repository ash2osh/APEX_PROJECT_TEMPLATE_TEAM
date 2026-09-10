# Candidate application checks

Keep one version-1 declaration at `ci/app-checks/<alias>.json` for every
configured application. Declarations are source-controlled, deterministic, and
must not contain credentials. Missing declarations, members, fixtures, flow
adapters, or structured results fail closed.

## SELECT checks

Each SELECT check names a relative `.verify.sql` member and may list expected
objects. It runs through the observation-only `VERIFY` profile and must return
framed assertion rows:

```sql
SELECT 'TEAM_ASSERT|' || assertion_name || '|' || status
  FROM (SELECT 'employee_table_exists' assertion_name,
               CASE WHEN COUNT(*) = 1 THEN 'PASS' ELSE 'FAIL' END status
          FROM all_tables WHERE table_name = 'EMPLOYEE');
```

At least one `TEAM_ASSERT|<name>|PASS` row is required. Every assertion must be
`PASS`; malformed, duplicate, FAIL, noisy, or absent framing is a failure.

## Flow checks

Flow declarations describe safe navigation, fill, and click steps plus an
expected URL or visible-text assertion. Protected online jobs pass the
qualified executable named by `TEAM_FLOW_RUNNER`:

```text
TEAM_FLOW_RUNNER --alias <alias> --check-json <path>
```

The adapter must print one JSON object with `status` `PASS`, `FAIL`, or
`UNKNOWN`. The qualification report records app/page/check IDs, expected
objects, declaration/check digests, coverage, and the unknown count. Persistent
qualification is observational and does not prove a fresh installation.
