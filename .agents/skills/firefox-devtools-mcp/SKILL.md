---
name: firefox-devtools-mcp
description: Use when inspecting or interacting with a web application in Firefox through the selected project Firefox DevTools MCP.
---

# Firefox DevTools MCP

Read the [Browser MCP selection guide](../../../docs/browser-mcp.md) and check the checkout's selection with scripts/team.sh mcp status before browser work.

Use Firefox only when the saved choice is firefox or both. The MCP attaches to an existing Firefox session; it does not start the browser. Use a dedicated Firefox profile because the MCP can access that profile's open tabs, cookies, and signed-in sessions. Start Firefox with remote debugging and Marionette enabled, for example:

    firefox --marionette --remote-debugging-port

Restart Firefox normally after MCP work; Marionette changes browser fingerprint signals and should not stay enabled during everyday browsing.

Call list_pages first and use its returned page identifier for page-scoped tools. For exploratory work, create a new tab and close only that tab when finished. If the selected server is unavailable, report that instead of switching browsers or starting a second MCP connection.

Treat clicks, form submissions, and other page actions as real application operations. Follow shared APEX rules before actions that write app data or Builder state. Keep credentials, session URLs, and session identifiers out of files and reports.
