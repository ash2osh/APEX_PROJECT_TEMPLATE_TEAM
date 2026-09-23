# Alias-keyed application context

Store durable knowledge below `app_context/<alias>/` only. Suggested files are
`purpose.md`, `data-dependencies.md`, `subscriptions.md`, `known-issues.md`,
`release.json`, and `recovery.md`. Keep connection names, passwords, local defaults,
logs, and sync state out of this directory. Context is advisory; target contracts
and verified SQLcl identity remain authoritative.

## Application Release Prerequisites

Each application must declare its database migration prerequisites in
`app_context/<alias>/release.json`:

```json
{
  "version": 1,
  "requires": [
    "20260907T100000__alice__one"
  ]
}
```

For applications with no database migration prerequisites:

```json
{
  "version": 1,
  "requires": []
}
```

Authors declare canonical migration IDs in `requires`. The release builder
resolves exact immutable SHA-256 bundle checksums from the selected Git commit;
authors do not hand-copy checksums. The builder rejects missing declarations,
unknown fields, duplicate IDs, non-canonical IDs, and IDs absent from the commit.
