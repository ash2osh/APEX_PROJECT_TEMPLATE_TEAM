---
name: graphify
description: Build and query the optional domain graph without changing source state.
---

`python3 scripts/setup_graphify_apx.py` installs the tracked extractor only when the
optional Graphify package is present and verifies the installed bytes with a
smoke extraction. Run `graphify extract . --force` after changing
`.graphifyignore`; otherwise use `graphify update .` for application/database
source and `graphify extract .` for `app_context/<alias>/` changes. Never use
`--code-only` for a full rebuild because durable context is part of the corpus.

Query with `graphify query`, `graphify path` and `graphify explain` when the
graph exists. Verify nonempty source files begin with `apps/`, `database/` or
`app_context/` and that `.apx` nodes have architectural edges. Graph creation
is read-only with respect to baselines and recovery state.
