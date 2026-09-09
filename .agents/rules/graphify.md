---
trigger: always_on
description: Consult the optional alias-keyed Graphify corpus for APEX, database and context questions.
---

Use Graphify only when `graphify-out/` exists and the CLI is installed. Run
`python3 scripts/setup_graphify_apx.py` after every Graphify upgrade, then use
`graphify extract . --force` for a changed ignore contract and `graphify update .`
after ordinary `.apx` or `database/` edits. The corpus is an allowlist of
`apps/`, `database/` and `app_context/`; it excludes agent files, scripts,
deployments, `.apex` tooling metadata, static payloads, logs and sync state.
Fall back to `rg` and exact file reads when the optional graph is absent.
