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
- For a full rebuild that includes `app_context/**/*.md`, run `python3 scripts/graphify_project.py extract -- --force`; do not add `--code-only`, which omits context. That rebuild may perform optional semantic indexing of context; it is not a prerequisite for routine code graph updates.

Graph updates:
- Use only the repository's offline, project-isolated CLI wrapper to update the graph: `python3 scripts/graphify_project.py extract`. Do not use the Graphify skill or MCP to update the graph, call a provider API directly, or run a bare `graphify update` command.
- Routine `.apx` and `database/*.sql` updates use local extraction and do not require an LLM API key. Do not ask developers for an LLM API key to refresh those code relationships.
- Only changed `app_context/**/*.md` files may use a configured semantic backend. Treat that as optional context indexing, separate from the code graph update. If the CLI reports that semantic indexing is unavailable, report that limitation accurately and continue with normal source reads; do not present an API key as a prerequisite for routine updates.

Rules:
- At the start of every new conversation, when `graphify-out/graph.json` exists, start a background refresh and do not wait for it: `python3 scripts/check_graphify_apx.py && python3 scripts/graphify_project.py extract` (run it with the client's background mechanism and keep its output out of the reply). This is the required local CLI update path; it checks package readiness before running and validates the result afterward. Do not replace it with a Graphify skill, provider API, or MCP update. Do not gate refresh on `verify`, which can reject an old graph that needs updating. `extract` without `--force` is incremental: unchanged files are skipped by hash, and only a changed `app_context/**/*.md` file can use the configured semantic backend. Never add `--force` here. Skip it when a Graphify run is already active or the project-isolated environment is not set up. If the pre-flight fails, say so in one line and use normal file reads; do not repair the installation by hand. Treat query answers as possibly stale until the refresh finishes, and report only a failure, never a success. If a project tracks `graphify-out/` in Git, the refresh dirties the tree: never commit it on your own initiative, and commit or stash it before running `scripts/team.sh upgrade-template`.
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
- For explicit extraction outside the startup refresh, run `python3 scripts/graphify_project.py verify`. It changes nothing and verifies environment readiness, pinned dependencies, canonical extractor patching, and corpus coverage.
- Corpus coverage uses an ignored `graphify-out/.template-corpus.json` receipt from the exact local detector. Source inventory/content, nested ignore rules, saved build settings, Git index/exclusions or graph changes invalidate it. If coverage is unavailable, run `python3 scripts/graphify_project.py verify --refresh-corpus` to invoke the local detector and write only that ignored receipt, or run `extract`, which refreshes it after extraction. Default `verify` and `verify-local` stay read-only and never launch the detector; missing receipts never guess which excluded files belong in the graph.
- Also run `python3 scripts/check_graphify_apx.py` before extraction and `python3 scripts/check_graphify_apx.py --graph graphify-out/graph.json` afterward. Both must pass before reporting APEXlang file coverage: Graphify can exit zero after a per-file parser failure. The checker uses the active CLI detector with saved exclusions and Git-ignore settings from the default `graphify-out/` directory, fails on unavailable/incomplete scans, and changes no repository files. Custom output roots or new CLI corpus overrides require separate coverage verification. It checks parsing and file presence, not semantic completeness, content freshness, or live runtime state.
