---
name: apexlang-export-debugging
description: Use when Oracle APEX APEXLANG (.apx) or READABLE_YAML exports fail with ORA-01403 in WWV_META_META_DATA, especially when SQL export succeeds or missing plugin or template references are suspected.
---

# Debug APEXlang export failures

Use component isolation and metadata checks to investigate export failures.
Read [the supplied debugging guide](references/APEXLANG_EXPORT_DEBUGGING_GUIDE.md)
for backtrace capture, page isolation, candidate queries, and repair examples.
The guide records a particular failure pattern; apply the qualifications below
when using it.

## Scope and prerequisites

- Establish the numeric application ID, workspace, parsing schema, saved SQLcl
  connection, and installed APEX/SQLcl versions from project configuration and
  evidence. Confirm metadata visibility before interpreting empty query results.
- Use `sqlcl-mcp-r0` when operating SQLcl MCP. For browser work, use the project's
  `chrome-devtools-mcp` skill and client.
- Follow `AGENTS.md`: diagnosis does not authorize exports or repairs. Run exports
  only when requested, using a fresh scratch directory for diagnostic artifacts.
  Do not refresh tracked source, advance export baselines, import, or publish
  merely to investigate a stack trace. Shared Builder changes and imports require
  team coordination and authorization for that operation.

## Diagnose from evidence

1. Capture the original command, full error/backtrace, versions, and target
   identity. Compare SQL and APEXLANG/READABLE_YAML results only if available or
   export testing is authorized. SQL success can coexist with an unresolved
   metadata reference; a `WWV_META_META_DATA` frame alone does not prove one.
2. Isolate pages with `APEX_EXPORT.GET_APPLICATION` and `PAGE:<page-id>` selectors
   as illustrated in the guide. Include Page 0 when present, batch large apps,
   and record each failure. Do not assume Page 0 fails first or is the only cause.
   If all pages pass, investigate shared components and application metadata.
3. Inspect supported dictionary-view columns for the installed release before
   adapting the guide's SQL. Search dynamic action actions, items, processes,
   regions, and templates for unresolved references. Treat display-name queries
   as candidate checks; confirm identifiers and the relevant theme/plugin type.
   Do not infer missing metadata from an inaccessible view or absent context.
4. For raw plugin codes, match the literal `PLUGIN_` prefix using
   `LIKE 'PLUGIN\_%' ESCAPE '\'`. Prefer a correlated `NOT EXISTS` with the same
   application ID over nullable `NOT IN` comparisons. Confirm the component ID,
   plugin name/type, and related actions before concluding that it is orphaned.

## Prepare a minimal repair

Restore a required dependency, select an existing valid template, or remove an
obsolete reference after reviewing its behavior. Preserve valid sibling actions;
remove an entire dynamic action event only when all its actions are intentionally
being removed. Never update Oracle-owned APEX internal tables directly.

Page SQL import can remove and recreate the whole page. Inspect the complete
export and diff, retain a recovery copy, and follow the project's applicable
import/publish rules before executing a requested repair. Use
`apex-workflow-lifecycle` when live workflows or tasks may be affected.

## Verify and report

Repeat the metadata check, then test the isolated page and full application export
when authorized. Inspect generated files and logs: the guide's exception handlers
print errors without re-raising, so a zero client exit code alone is insufficient.
Verify affected runtime behavior and review source differences before committing.
Report confirmed causes, changed components, checks performed, and unavailable
checks separately; do not claim a live repair from a prepared patch.
