---
name: graphify
description: Turn any folder of files into a navigable knowledge graph
---

# Workflow: graphify

Use the graphify skill exposed by the current agent client when available. If
the client does not expose that skill, use the repository's
`scripts/graphify_project.py` helper commands directly; do not
assume a provider-specific home directory or invent an unavailable tool.

## Domain corpus

`.graphifyignore` is intentionally an allowlist. Index only:

- `apps/` — application declarations, pages, shared components, and supporting
  source, excluding static files, deployment descriptors, and workspace/`.apex`
  metadata.
- `database/` — synchronized database object source.
- `app_context/` — durable purpose, architecture, pattern, and gotcha context.

Do not re-include agent skills, project scripts, `migrations/`, general
documentation, `ai_generate/`, scratch data, generated graph output, or
embedded static/BLOB payloads. This graph describes the APEX application
domain, not maintenance of the repository template.

The graph is built from this repository only. The shared DEV database and
Builder are the runtime source of truth; export the app first when the graph
must reflect Builder state. Durable documentation belongs in project-owned
files (`PROJECT.md`, `docs/`, `app_context/`), not ignored graph artifacts.

## Fresh setup and full rebuild

Configure a supported semantic backend and run:

```bash
python3 scripts/graphify_project.py setup
python3 scripts/graphify_project.py extract -- --force
```

The setup creates the project-isolated `.venv-graphify/` environment using
copy mode, installs pinned requirements from `tools/graphify/requirements.txt`,
copies the tracked `scripts/graphify_apexlang_extractor.py` into the
isolated Graphify package, registers `.apx`, verifies the installed bytes, and
clears stale extraction caches. The full extraction indexes Markdown context as well
as deterministic APEXlang and SQL structure. Do not add `--code-only`: that
would omit `app_context`.

## Incremental updates

- After changing `.apx` or database `.sql` source, run `python3 scripts/graphify_project.py extract`.
  This refreshes AST relationships without an LLM call and preserves unchanged
  semantic context nodes.
- After changing `app_context/**/*.md`, run `python3 scripts/graphify_project.py extract`. Semantic
  hashes make this incremental; only changed semantic sources should be
  redispatched.
- After changing `.graphifyignore` or intentionally deleting substantial
  source, run `python3 scripts/graphify_project.py extract -- --force` and repeat the source-root checks.

## Upgrade gate

A Graphify dependency upgrade replaces `site-packages` and silently removes the APEXlang
patches; nothing detects the reverted state at query time. Before any
extraction, run the pre-flight — it changes
nothing and exits non-zero when the integration or environment is not ready:

```bash
python3 scripts/graphify_project.py verify
```

If it fails, run `python3 scripts/graphify_project.py setup` to reinstall. Graphify
has no supported APEXlang extension point, so setup validates the current
package anchors and fails closed if an upgrade is incompatible. Never repair
the installed copy by hand; update the tracked extractor/setup and their tests
so the fix persists for every template user.

## Database mirror dependency

An `.apx` reference to a table or package is linked to the real node from the
database mirror when that file exists. Unqualified names resolve in the app's
own schema first; qualified names resolve in the named schema. When a local
object is absent, a mirrored private synonym can resolve one hop by reading
its `FOR "SCHEMA"."OBJECT"` clause. Targets over database links and targets
not present in the mirror stay name-only stubs. `.sql` files under `database/`
get the same treatment for foreign keys. `APEX_*` dictionary views remain
name-only stubs; `DUAL` is ignored and produces no node or edge. Expression-style three-part package references without
parentheses are not detected.

Graphify caches its index by repository root, so extraction results cached
before a mirror existed can retain stubs. After the first
`scripts/team.sh backup-db`, or when database mirrors change, run
`python3 scripts/graphify_project.py setup` (it clears cached `.apx` and `.sql`
extractions) and then `python3 scripts/graphify_project.py extract`.

Two limits are inherent to Graphify rather than lost data: the graph is
undirected, so a mutual foreign-key pair (`A -> B` and `B -> A`) shows as one
edge, and several procedure calls into one package share one package node
because package files carry no per-procedure nodes.

## Verification and queries

Before every update or extraction, after the installation pre-flight, run:

```bash
python3 scripts/check_graphify_apx.py
```

After building the graph, require:

```bash
python3 scripts/check_graphify_apx.py --graph graphify-out/graph.json
```

Both checks must pass before reporting APEXlang file coverage. Graphify can
return zero after skipping files with parser errors; its exit status alone is
insufficient. The checker uses the active Graphify detector and its ignore rules,
with saved exclusions and Git-ignore settings from the default `graphify-out/`
directory. Custom output roots or new CLI corpus overrides require separate
coverage verification. It writes its scan cache only in a temporary directory,
and fails on unavailable or
incomplete scans, parser failures, unreadable graphs, or omitted APEXlang sources.
An empty eligible corpus is reported as zero files. These checks prove parsing
and file presence, not semantic completeness, matching content hashes, or runtime
validity. The parser accepts exported `$`, bind-alias and Unicode identifiers
without treating SQL or comment payloads as component declarations.

Verify that every nonempty graph `source_file` begins with `apps/`,
`database/`, or `app_context/`; `.apx` sources emit nonzero edges; and context
nodes exist. Then start with `graphify query`, using `graphify path` and
`graphify explain` for focused follow-up. Representative acceptance questions
must cover page-to-table dependencies, navigation-to-page targets, process
calls/writes, and a known application gotcha from `app_context`.
