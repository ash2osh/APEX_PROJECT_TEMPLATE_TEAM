# Chrome DevTools MCP

This project can use the `chrome-devtools-mcp` server through a persistent local daemon. The daemon attaches to the user's running Chrome session; the small CLI client sends allowlisted tool calls over a private Unix socket on Linux and macOS, or over a token-protected TCP loopback port on Windows.

## Prerequisites

- `chrome-devtools-mcp` must already be installed and available on `PATH`, or be selected with `CHROME_MCP_EXECUTABLE`.
- Chrome must be running with remote debugging available and user consent granted when Chrome asks. The first request can take longer while consent is handled.
- Python 3 is required for the daemon and client.
- Linux, macOS, WSL and native Windows are supported. On Windows the daemon finds npm's `chrome-devtools-mcp.cmd` wrapper on `PATH` and runs it through `cmd.exe`; run the commands below with `python` where `python3` is not installed.

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

Use the `pageId` returned by `list_pages` for page-scoped tools. The client accepts a tool name and an optional JSON object. The examples quote JSON for Bash, Git Bash and PowerShell 7; Windows PowerShell 5.1 drops the inner double quotes, so there write `'{\"pageId\":1}'`.

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

On Linux and macOS the daemon uses `$XDG_RUNTIME_DIR/chrome-mcp/chrome-mcp.sock` by default. If `XDG_RUNTIME_DIR` is unset, it uses `/tmp/chrome-mcp-<uid>/chrome-mcp.sock`. The containing directory is private (`0700`) and the socket is private (`0600`).

On Windows, which has no Unix sockets for Python, the daemon listens on TCP `127.0.0.1:9223`. Every local account can reach a loopback port, so the daemon writes a random token to `%LOCALAPPDATA%\chrome-mcp\tcp-<port>.token` before it listens and refuses any request without it; the client reads the token on every call and the daemon deletes it on exit. The daemon listens on a loopback address only, binds the port exclusively before it starts its MCP child (a second daemon is refused without opening a second Chrome connection), and ends the MCP child's whole process tree when it stops. Any platform can use TCP by setting `CHROME_MCP_SOCKET` to `127.0.0.1:<port>` or `tcp://127.0.0.1:<port>`; off Windows the token then lives in the socket directory above.

Set an override in the shell only when intentionally connecting to a known daemon:

| Variable | Purpose | Default |
|---|---|---|
| `CHROME_MCP_SOCKET` | Socket path, or TCP loopback `host:port`, used by the daemon and client | Per-user runtime path above; `127.0.0.1:9223` on Windows |
| `CHROME_MCP_TIMEOUT` | Seconds the daemon waits for a Chrome MCP response | `60` |
| `CHROME_MCP_EXECUTABLE` | MCP executable started by the daemon | `chrome-devtools-mcp` |
| `CHROME_MCP_TOKEN_DIR` | Directory for the TCP token file (the tests use a temporary one) | Per-user directory above |

The socket is local to the user and protects access by filesystem permissions; the TCP port protects it by the per-user token file. Do not place secrets in tool arguments, shell history, repository files, or logs.

## Supported tool calls

The client forwards only the allowlisted Chrome DevTools MCP tools. Common inspection calls include `list_pages`, `take_snapshot`, `evaluate_script`, `take_screenshot`, `list_console_messages`, and `list_network_requests`. Page interaction calls such as `click`, `fill`, `navigate_page`, and `close_page` are also available. Unknown tool names are rejected locally.

Pass page-scoped arguments as JSON, for example `'{"pageId":1}'`. Read the current page list before selecting an ID; page IDs can change as tabs open or close.

## Troubleshooting and cleanup

| Symptom | Check |
|---|---|
| Client says daemon is unavailable | Confirm the daemon is running and that both processes use the same `CHROME_MCP_SOCKET`, if overridden. |
| Client reports no daemon token, or the daemon answers `missing or invalid daemon token` | The process on that TCP port was not started by this account, or by this project's daemon. Stop it or choose another port with `CHROME_MCP_SOCKET`. |
| Daemon says it is already listening, or the port is taken | Another daemon (perhaps another project's) or program owns the port. Use that daemon, or set `CHROME_MCP_SOCKET` to a free loopback port for both daemon and client. |
| `chrome-devtools-mcp` cannot start | Confirm the executable is installed, on `PATH`, and compatible with the running Chrome. |
| First request times out or stalls | Check whether Chrome is waiting for remote-debugging consent and whether another MCP process already owns the connection. |
| Page-scoped tool rejects its arguments | Refresh `list_pages` and pass the current `pageId`. |
| A tool call times out | Check the Chrome connection and consent state; a timeout does not start another daemon. |

Stop the daemon with Ctrl-C (or Ctrl-Break on Windows) in its terminal, or send it `SIGTERM` on Linux and macOS. It stops its MCP child, on Windows with everything the child started, and removes only the socket or token file it created. It also stops by itself when its MCP child exits. Do not remove a socket or token manually while a daemon is running.
