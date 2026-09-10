# Candidate application checks

Add one `<alias>.json` declaration for every tracked application under
`apps/<alias>/`. Each declaration must include at least one restricted
observation-only `select` check and one declarative `flow` check. Put referenced
`.verify.sql` members and flow JSON beside the declaration.

The disposable replay runner treats missing declarations, missing fixtures and
missing SQL/browser adapters as qualification failures. It never converts an
unavailable check into a pass. The template has no shipped application yet, so
the reference replay reports an explicit zero-app coverage result until an
application and its qualified adapters are added.

## What a `select` check must project

The replay runner executes a `select` check's `.verify.sql` member through the
read-only VERIFY profile and reads framed assertion rows:

```sql
SELECT 'TEAM_ASSERT|' || assertion_name || '|' || status
  FROM (SELECT 'employee_table_exists' assertion_name,
               CASE WHEN COUNT(*) = 1 THEN 'PASS' ELSE 'FAIL' END status
          FROM all_tables WHERE table_name = 'EMPLOYEE');
```

The check passes only when every returned row says `PASS`. A member that
returns no framed rows, cannot be read, or whose query fails is a **FAIL**, not
an UNKNOWN.
