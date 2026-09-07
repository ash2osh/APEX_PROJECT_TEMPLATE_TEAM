# Conflict resolution assistant

`explain-conflict` reads only an immutable recovery bundle. It describes
property-level APEXlang differences when parsing is confident and falls back to
the raw four-tree comparison otherwise. It labels the capture as the shared
application, never as the operator's own change, and asks the developer to
choose. It never writes tracked source or synthesizes a merged value.

Any candidate produced under `scratch/` still passes through
`resolve-export`, which independently verifies the original HEAD, paths,
preimages, tombstones, and result bytes.
