# Authentic APEXlang conversion fixtures

`source_26_1` comes from Oracle skills 26.1 `example-0`, commit
`6c57a52588c2a68e261ff0542b8fde80fd2b9c42`. The qualification input changed
only fixture identification and its explicit Docker descriptor. Its original
format marker remains `26.1.0+3102`.

`canonical_26_2` is the measured SQLcl 26.3 export from Docker APEX 26.2,
format `26.2.0+3479`, repeated twice with identical source bytes. The conversion
produced 23 files and preserved the five PNG assets. These sanitized copies
replace generated checksum salts with 64 zeroes. No source format marker was
rewritten. `hashes.json` records the sanitized fixture bytes, not database state.

These examples qualify this small application. They do not establish behavior
for translated applications or executable supporting-object scripts.

Oracle upstream: https://github.com/oracle/skills
