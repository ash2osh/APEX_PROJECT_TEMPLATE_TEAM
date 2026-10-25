---
trigger: always_on
description: Honor the selected project browser MCP and avoid duplicate browser connections.
---

<!-- Project-specific agent rules. The template upgrade never overwrites this file. -->
# Project browser access

- Before browser work, run `scripts/team.sh mcp status`, read the [browser MCP guide](../../docs/browser-mcp.md), and follow the skill for the saved choice: [Chrome](../skills/chrome-devtools-mcp/SKILL.md) or [Firefox](../skills/firefox-devtools-mcp/SKILL.md).
- Use only direct project MCP servers enabled by the saved checkout choice. If both are enabled, use the browser requested for the task and one connection for that browser.
- When Chrome is selected, use the direct project Chrome MCP; do not also use the daemon or another Chrome MCP connection for that session.
- If Chrome is not selected and the task requires Chrome, use the daemon only with explicit startup authorization and user consent; follow the [daemon guide](../../docs/CHROME_DEVTOOLS_MCP.md). If a selected server is unavailable, report it instead of bypassing the saved choice.
- Treat browser actions as real app interactions. Follow `AGENTS.md` and shared APEX workflow rules before actions that write app data or Builder state.
