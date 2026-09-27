---
trigger: always_on
description: Use the project's persistent Chrome DevTools MCP daemon for browser access.
---

<!-- Project-specific agent rules. The template upgrade never overwrites this file. -->
# Project browser access

- Before browser work, read [the Chrome DevTools MCP skill](../skills/chrome-devtools-mcp/SKILL.md) and [its daemon guide](../../docs/CHROME_DEVTOOLS_MCP.md).
- Route browser calls through `python3 tools/chrome_devtools_client.py <tool> [json_args]`. Never call another browser MCP connection or attach directly to Chrome as a workaround.
- The client does not start a daemon. Use one daemon for the user's consented Chrome connection; if it is unavailable, start it explicitly only when the current task authorizes startup and consent is available. Otherwise report that it is unavailable and ask the user to start it.
- Treat browser actions as real app interactions. Follow `AGENTS.md` and shared APEX workflow rules before actions that write app data or Builder state.
