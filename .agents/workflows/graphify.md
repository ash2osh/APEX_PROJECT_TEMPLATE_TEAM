---
name: graphify
description: Turn any folder of files into a navigable knowledge graph
---

# Workflow: graphify

Use the graphify skill exposed by the current agent client when available. If
the client does not expose that skill, use the repository's
`scripts/setup_graphify_apx.py` and `graphify` CLI commands directly; do not
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
must reflect Builder state.

## Fresh setup and full rebuild

After installing the optional Graphify CLI, configure a supported semantic
backend and run:

```bash
python3 scripts/setup_graphify_apx.py
graphify extract . --force
```

The setup copies the tracked `scripts/graphify_apexlang_extractor.py` into the
isolated Graphify package, registers `.apx`, verifies the installed bytes, and
runs a smoke extraction. The full extraction indexes Markdown context as well
as deterministic APEXlang and SQL structure. Do not add `--code-only`: that
would omit `app_context`.

## Incremental updates

- After changing `.apx` or database `.sql` source, run `graphify update .`.
  This refreshes AST relationships without an LLM call and preserves unchanged
  semantic context nodes.
- After changing `app_context/**/*.md`, run `graphify extract .`. Semantic
  hashes make this incremental; only changed semantic sources should be
  redispatched.
- After changing `.graphifyignore` or intentionally deleting substantial
  source, run `graphify extract . --force` and repeat the source-root checks.

## Upgrade gate

A Graphify upgrade replaces `site-packages` and silently removes the APEXlang
patches; nothing detects the reverted state at query time. Before any
`graphify update` or `graphify extract`, run the pre-flight — it changes
nothing and exits non-zero when the integration is not installed:

```bash
python3 scripts/setup_graphify_apx.py --verify
```

If it fails, run `python3 scripts/setup_graphify_apx.py` to reinstall. Graphify
has no supported APEXlang extension point, so setup validates the current
package anchors and fails closed if an upgrade is incompatible. Never repair
the installed copy by hand; update the tracked extractor/setup and their tests
so the fix persists for every template user.

## Database mirror dependency

An `.apx` reference to a table or package is linked to the real node from
`database/<SCHEMA>/tables|views|packages/` when that file exists, and stays a
name-only stub when it does not (`APEX_*` dictionary views, `DUAL`). `.sql` files
under `database/` get the same treatment for foreign keys. Graphify caches
extraction by file content, so a result cached before the mirror existed keeps
its stubs. After the first `scripts/team.sh backup-db`, or after tables or
packages are added or removed, run `python3 scripts/setup_graphify_apx.py`
(it clears cached `.apx` and `.sql` extractions) and then `graphify update .`.

Two limits are inherent to Graphify rather than lost data: the graph is
undirected, so a mutual foreign-key pair (`A -> B` and `B -> A`) shows as one
edge, and several procedure calls into one package share one package node
because package files carry no per-procedure nodes.

## Verification and queries

Verify that every nonempty graph `source_file` begins with `apps/`,
`database/`, or `app_context/`; `.apx` sources emit nonzero edges; and context
nodes exist. Then start with `graphify query`, using `graphify path` and
`graphify explain` for focused follow-up. Representative acceptance questions
must cover page-to-table dependencies, navigation-to-page targets, process
calls/writes, and a known application gotcha from `app_context`.
