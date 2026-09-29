---
trigger: always_on
description: Consult the domain-focused graphify knowledge graph for APEX application, database, and context questions.
---

## graphify

This project can use a domain-focused graphify knowledge graph at `graphify-out/`, if the
`graphify` CLI is installed (`uv tool install graphifyy --with tree-sitter-sql`
— note the distribution is `graphifyy` and the command is `graphify`; see
`AGENTS.md` "Optional Tooling"). It is not guaranteed to be present; if `graphify-out/`
does not exist, none of the rules below apply — fall back to normal
file reads and grep.

Setup on new machines / devices:
- Run `python3 scripts/setup_graphify_apx.py` to install the repository-owned APEXlang extractor into Graphify and verify it with a smoke extraction.
- Configure a supported semantic backend, then run `graphify extract . --force`. The initial full extraction is required so `app_context/**/*.md` is indexed; do not use `--code-only`.

Rules:
- For codebase or architecture questions, when `graphify-out/graph.json` exists, first run `graphify query "<question>"` (CLI) or `query_graph` (MCP). Use `graphify path "<A>" "<B>"` / `shortest_path` for relationships and `graphify explain "<concept>"` / `get_node` for focused concepts. These return a scoped subgraph, usually much smaller than `GRAPH_REPORT.md` or raw grep output.
- If graphify-out/wiki/index.md exists, navigate it instead of reading raw files
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context
- `.graphifyignore` is a domain allowlist. Graph sources must come only from `apps/`, `database/`, or `app_context/`; agent skills, project automation, `migrations/`, `ai_generate/`, deployment descriptors, `.apex` tooling metadata, workspace components, and static payloads stay excluded.
- `graphify-out/` is local, gitignored state and reflects only this repository's files, not shared DEV Builder or database state. Export first (`scripts/team.sh export <app-id>`) if the graph must describe current Builder state, and never claim it does otherwise.
- After modifying APEXlang or database source, run `graphify update .` to refresh deterministic AST relationships without semantic API cost. For database mirror changes, first follow the next rule to clear cached extractions.
- Application-to-database links depend on the `database/` mirror. Unqualified names resolve in the app's own schema first; qualified names resolve in the named schema. When an object is absent locally, the extractor can follow a mirrored private synonym one hop by reading its `FOR "SCHEMA"."OBJECT"` clause. A target over a database link or absent from the mirror stays a stub. Expression-style three-part package references without parentheses are not detected.
- Graphify caches its index by repository root. After the first `scripts/team.sh backup-db` or when database mirrors change, run `python3 scripts/setup_graphify_apx.py` (clears stale cached extractions) before `graphify update .`.
- After modifying `app_context/**/*.md`, run `graphify extract .` so the semantic hash and context concepts refresh.
- After changing `.graphifyignore` or deliberately deleting substantial source, run `graphify extract . --force`, then verify excluded paths are absent and all three retained domain roots remain queryable.
- Before `graphify update` or `graphify extract`, run `python3 scripts/setup_graphify_apx.py --verify`. It changes nothing and exits non-zero if a Graphify upgrade has silently reverted the APEXlang integration; reinstall with `python3 scripts/setup_graphify_apx.py` when it does.
