# Chrome DevTools MCP

This project can use the `chrome-devtools-mcp` server through a persistent local daemon. The daemon attaches to the user's running Chrome session; the small CLI client sends allowlisted tool calls over a private Unix socket.

## Prerequisites

- `chrome-devtools-mcp` must already be installed and available on `PATH`, or be selected with `CHROME_MCP_EXECUTABLE`.
- Chrome must be running with remote debugging available and user consent granted when Chrome asks. The first request can take longer while consent is handled.
- Python 3 is required for the daemon and client.

This guide does not install or update Chrome, Node.js, or `chrome-devtools-mcp`. If the MCP executable is missing, report the missing prerequisite and use the machine's approved setup process.

## Start and use the daemon

Start one daemon in a dedicated terminal while Chrome is available:

```bash
python3 tools/chrome_mcp_daemon.py
```

The process owns one `chrome-devtools-mcp --autoConnect` child and serves tool requests until stopped. In a second terminal, list the connected pages:

```bash
python3 tools/chrome_devtools_client.py list_pages
```

Use the `pageId` returned by `list_pages` for page-scoped tools. The client accepts a tool name and an optional JSON object:

```bash
python3 tools/chrome_devtools_client.py take_snapshot '{"pageId":1}'
python3 tools/chrome_devtools_client.py evaluate_script '{"pageId":1,"function":"() => document.title"}'
python3 tools/chrome_devtools_client.py take_screenshot '{"pageId":1}'
python3 tools/chrome_devtools_client.py list_console_messages '{"pageId":1}'
python3 tools/chrome_devtools_client.py list_network_requests '{"pageId":1}'
```

The client does not start a daemon. If it reports that the socket is unavailable, start the daemon explicitly or report that it is unavailable; do not launch a second MCP server or call a separate browser connection to work around it. One daemon should own the consented Chrome connection at a time.

Use `new_page` to create a tab for work that should not alter another tab's navigation, then use that tab's returned `pageId`. Close only tabs you created with `close_page`. Treat clicks, form submissions, and other page actions as real application interactions; follow the project database and shared APEX rules before actions that write application data or Builder state.

## Socket and environment

By default, the daemon uses `$XDG_RUNTIME_DIR/chrome-mcp/chrome-mcp.sock`. If `XDG_RUNTIME_DIR` is unset, it uses `/tmp/chrome-mcp-<uid>/chrome-mcp.sock`. The containing directory is private (`0700`) and the socket is private (`0600`).

Set an override in the shell only when intentionally connecting to a known daemon:

| Variable | Purpose | Default |
|---|---|---|
| `CHROME_MCP_SOCKET` | Socket path used by the daemon and client | Per-user runtime path above |
| `CHROME_MCP_TIMEOUT` | Seconds the daemon waits for a Chrome MCP response | `60` |
| `CHROME_MCP_EXECUTABLE` | MCP executable started by the daemon | `chrome-devtools-mcp` |

The socket is local to the user and protects access by filesystem permissions. Do not place secrets in tool arguments, shell history, repository files, or logs.

## Supported tool calls

The client forwards only the allowlisted Chrome DevTools MCP tools. Common inspection calls include `list_pages`, `take_snapshot`, `evaluate_script`, `take_screenshot`, `list_console_messages`, and `list_network_requests`. Page interaction calls such as `click`, `fill`, `navigate_page`, and `close_page` are also available. Unknown tool names are rejected locally.

Pass page-scoped arguments as JSON, for example `'{"pageId":1}'`. Read the current page list before selecting an ID; page IDs can change as tabs open or close.

## Troubleshooting and cleanup

| Symptom | Check |
|---|---|
| Client says daemon is unavailable | Confirm the daemon is running and that both processes use the same `CHROME_MCP_SOCKET`, if overridden. |
| `chrome-devtools-mcp` cannot start | Confirm the executable is installed, on `PATH`, and compatible with the running Chrome. |
| First request times out or stalls | Check whether Chrome is waiting for remote-debugging consent and whether another MCP process already owns the connection. |
| Page-scoped tool rejects its arguments | Refresh `list_pages` and pass the current `pageId`. |
| A tool call times out | Check the Chrome connection and consent state; a timeout does not start another daemon. |

Stop the daemon with Ctrl-C in its terminal or send it `SIGTERM`. It stops its MCP child and removes only the socket it created. Do not remove a socket manually while a daemon is running.
