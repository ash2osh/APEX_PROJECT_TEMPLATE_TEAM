---
name: chrome-devtools-mcp
description: Use when inspecting or interacting with a web application in Chrome through the selected project MCP or the explicitly authorized Chrome daemon fallback.
---

# Chrome DevTools MCP

Read the [Browser MCP selection guide](../../../docs/browser-mcp.md) and check the checkout's selection with scripts/team.sh mcp status before browser work.

## Connect

- When the saved choice is chrome or both, use the project-configured direct Chrome DevTools MCP. Call list_pages first and use its returned page identifier for page-scoped tools.
- Do not use the Chrome daemon client or another Chrome MCP connection while the direct project MCP is selected.
- When Chrome is not selected, use the persistent project daemon only if the user requested Chrome browser work and startup is authorized with user consent. Follow the [daemon guide](../../../docs/CHROME_DEVTOOLS_MCP.md).
- If the selected server is unavailable, report that. Do not silently bypass the selection or create a second connection.
- The setup command does not start Chrome or the MCP server. Browser startup and consent remain separate.

## Inspect safely

Create a tab for exploratory navigation and close only that tab when finished. Prefer snapshots to identify accessible elements before interacting. Treat clicks, form submissions, and other page actions as real application operations; follow shared APEX rules before actions that write app data or Builder state.

Use browser-side evaluation only to inspect rendered DOM and runtime values. Check console and network results after meaningful changes when available. Keep credentials, session URLs, and session identifiers out of files, shell history, and reports.
