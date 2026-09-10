# Candidate application checks

Add one `<alias>.json` declaration for every tracked application under
`apps/<alias>/`. Each version-1 declaration must include at least one restricted
observation-only `select` check and one declarative `flow` check. Put referenced
`.verify.sql` members and flow JSON beside the declaration.

Qualification is fail-closed: missing declarations, fixtures, SQL members,
adapters, or structured results are failures, never successful skips. The
report records explicit app/page/check coverage and an `unknown` count. A
persistent target with no tracked applications reports zero-app coverage only
when the configured application binding set is also empty.

## SELECT checks

Each `select` check names a `.verify.sql` member and may list expected objects.
The member is executed through the read-only `VERIFY` profile and must project
framed assertion rows:

```sql
SELECT 'TEAM_ASSERT|' || assertion_name || '|' || status
  FROM (SELECT 'employee_table_exists' assertion_name,
               CASE WHEN COUNT(*) = 1 THEN 'PASS' ELSE 'FAIL' END status
          FROM all_tables WHERE table_name = 'EMPLOYEE');
```

Every returned assertion must be `PASS`. A FAIL row, a query error, or no
framed row is a qualification failure.

## Flow checks

Flow declarations describe safe `navigate`, `fill`, and `click` steps with an
expected URL or visible-text assertion. A qualified adapter is configured with
`TEAM_FLOW_RUNNER` and invoked as:

```text
TEAM_FLOW_RUNNER --alias <alias> --check-json <path>
```

It must print one JSON object whose `status` is `PASS`, `FAIL`, or `UNKNOWN`.
Credentials must be referenced by test-secret name, never embedded in the
declaration.
