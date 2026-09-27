---
name: chrome-devtools-mcp
description: Use when inspecting or interacting with a web application in the user's running Chrome through the persistent Chrome DevTools MCP daemon.
---

# Chrome DevTools MCP

Use the project daemon for browser runtime evidence. See the [daemon guide](../../../docs/CHROME_DEVTOOLS_MCP.md) for prerequisites, socket configuration, supported tools, and troubleshooting.

## Connect

1. Check whether the daemon is already serving the shared user socket. Do not start a second daemon.
2. If none is running, use `python3 tools/chrome_mcp_daemon.py` only when this task authorizes startup and Chrome consent is available. Otherwise, report that the daemon is unavailable and ask the user to start it.
3. Send every browser call through `python3 tools/chrome_devtools_client.py <tool> [json_args]`. The client does not start the daemon; do not call a separate browser MCP connection or connect directly to Chrome's debugging endpoint.
4. Call `list_pages` first and use the returned `pageId` for page-scoped tools.

```bash
python3 tools/chrome_devtools_client.py list_pages
python3 tools/chrome_devtools_client.py take_snapshot '{"pageId":1}'
python3 tools/chrome_devtools_client.py evaluate_script '{"pageId":1,"function":"() => document.title"}'
python3 tools/chrome_devtools_client.py list_console_messages '{"pageId":1}'
python3 tools/chrome_devtools_client.py list_network_requests '{"pageId":1}'
```

## Inspect safely

- For exploratory navigation, create your own tab with `new_page` and close only that tab with `close_page` when finished. Avoid changing the user's existing tab state.
- Use snapshots to identify accessible elements before clicking or filling them. Treat submission, navigation, and other interactive actions as real application operations; follow project authorization and shared APEX rules before any action that writes application data or Builder state.
- Use `evaluate_script` to inspect rendered DOM and runtime values. Any temporary browser-side prototype is evidence only; put lasting changes in the appropriate source files and verify them again in the browser.
- Check console and network results after meaningful changes. Use screenshots when visual evidence helps. Keep credentials, session URLs, and session identifiers out of files, shell history, and reports.
- A timeout or missing socket is a daemon/connection issue. Do not bypass it with a second MCP instance; report the failure and follow the daemon guide.
