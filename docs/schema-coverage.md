# Schema inventory coverage

Version 1 fingerprints tables, constraints, indexes, views, package specs and
bodies, routines, triggers, sequences, types, private synonyms, and object
grants. Inventory manifests record logical ownership, schema-set identity,
coverage version, and normalization version. Volatile counters and timestamps
are diagnostics, not structural identity.

Unknown object classes, invalid/unframed output, reserved `TEAM_*` controller
objects in application schemas, and mismatched topology fail closed. A live
drift report has three separate parts: structural differences, unexplained
frontier differences, and local/applied history differences. A migration ID by
itself never excuses a changed object.

The bundled live SQLcl adapter uses a deterministic DBMS_METADATA prefix plus
the full definition length for its first qualification pass. This is useful
for identity/drift triage but is not an adoption baseline for very large DDL
definitions. A project must replace that bounded query with its qualified
full-definition/chunked adapter before accepting canonical schema evidence.
