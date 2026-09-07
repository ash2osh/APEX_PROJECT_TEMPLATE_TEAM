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

The bundled live SQLcl adapter emits begin/count/end framing and reconstructs
complete UTF-8 `DBMS_METADATA` definitions from bounded chunks before hashing
them. Constraint and object-grant rows are included as structured dictionary
evidence. SQLcl-wrapped, empty, malformed or unsupported output is unknown and
refuses adoption; a project must still qualify its exact grants and metadata
transforms before treating the result as a canonical replay baseline.
