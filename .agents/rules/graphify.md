---
trigger: always_on
description: Consult the domain-focused graphify knowledge graph for APEX application, database, and context questions.
---

## graphify

This project can use a domain-focused graphify knowledge graph at `graphify-out/`, if the
project-isolated Graphify environment is configured (`.venv-graphify/`; see
`AGENTS.md` "Optional Tooling"). It is not guaranteed to be present; if `graphify-out/`
does not exist, none of the rules below apply — fall back to normal
file reads and grep.

Setup on new machines / devices:
- Run `python3 scripts/graphify_project.py setup` to create the isolated `.venv-graphify/` environment, install pinned dependencies, patch the project APEXlang extractor, and clear stale caches.
- Configure a supported semantic backend, then run `python3 scripts/graphify_project.py extract -- --force`. The initial full extraction is required so `app_context/**/*.md` is indexed; do not use `--code-only`.

Rules:
- For codebase or architecture questions, when `graphify-out/graph.json` exists, first run `graphify query "<question>"` (CLI) or `query_graph` (MCP). Use `graphify path "<A>" "<B>"` / `shortest_path` for relationships and `graphify explain "<concept>"` / `get_node` for focused concepts. These return a scoped subgraph, usually much smaller than `GRAPH_REPORT.md` or raw grep output.
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context
- `.graphifyignore` is a domain allowlist. Graph sources must come only from `apps/`, `database/`, or `app_context/`; agent skills, project automation, `migrations/`, `ai_generate/`, deployment descriptors, `.apex` tooling metadata, workspace components, and static payloads stay excluded.
- `graphify-out/` is local, gitignored state and reflects only this repository's files, not shared DEV Builder or database state. Export first (`scripts/team.sh export <app-id>`) if the graph must describe current Builder state, and never claim it does otherwise. Durable documentation belongs in project-owned files (`PROJECT.md`, `docs/`, `app_context/`), not ignored graph artifacts.
- After modifying APEXlang or database source, run `python3 scripts/graphify_project.py extract` to refresh AST relationships.
- Application-to-database links depend on the `database/` mirror. Unqualified names resolve in the app's own schema first; qualified names resolve in the named schema. When an object is absent locally, the extractor can follow a mirrored private synonym one hop by reading its `FOR "SCHEMA"."OBJECT"` clause. A target over a database link or absent from the mirror stays a stub. Expression-style three-part package references without parentheses are not detected.
- Graphify caches its index by repository root. After the first `scripts/team.sh backup-db` or when database mirrors change, run `python3 scripts/graphify_project.py setup` (clears stale cached extractions) before extracting.
- After modifying `app_context/**/*.md`, run `python3 scripts/graphify_project.py extract` so the semantic hash and context concepts refresh.
- After changing `.graphifyignore` or deliberately deleting substantial source, run `python3 scripts/graphify_project.py extract -- --force`, then verify excluded paths are absent and all three retained domain roots remain queryable.
- Before extraction, run `python3 scripts/graphify_project.py verify`. It changes nothing and verifies environment readiness, pinned dependencies, canonical extractor patching, and corpus coverage.
- Also run `python3 scripts/check_graphify_apx.py` before extraction and `python3 scripts/check_graphify_apx.py --graph graphify-out/graph.json` afterward. Both must pass before reporting APEXlang file coverage: Graphify can exit zero after a per-file parser failure. The checker uses the active CLI detector with saved exclusions and Git-ignore settings from the default `graphify-out/` directory, fails on unavailable/incomplete scans, and changes no repository files. Custom output roots or new CLI corpus overrides require separate coverage verification. It checks parsing and file presence, not semantic completeness, content freshness, or live runtime state.
