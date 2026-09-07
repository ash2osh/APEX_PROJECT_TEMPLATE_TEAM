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
