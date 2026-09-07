# Agent safety rules

- Inspect SQL/APEX files before executing `@`, `@@`, `START`, APEX import, or
  migration content.
- Use `team.py` and the verified SQLcl boundary. Do not put credentials in
  command lines, tracked bindings, recovery manifests, or logs.
- Treat `.sync-state` captures as durable recovery evidence; `scratch/` is
  temporary staging only. Never recursively replace an application directory.
- APEX source is keyed by alias, not by a numeric directory. Database evidence
  is generated evidence, not a source mirror.
- Production profiles are SELECT-only. A target role cannot override the
  verified production identity.
