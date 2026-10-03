#!/usr/bin/env python3
"""Client for the explicitly started, persistent Chrome DevTools MCP daemon."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any

try:
    from tools.chrome_mcp_daemon import (
        ALLOWED_TOOLS, DEFAULT_REQUEST_TIMEOUT, default_socket_path,
        is_tcp_address, parse_tcp_address, read_token, token_path,
    )
except ModuleNotFoundError:  # direct execution from tools/
    from chrome_mcp_daemon import (
        ALLOWED_TOOLS, DEFAULT_REQUEST_TIMEOUT, default_socket_path,
        is_tcp_address, parse_tcp_address, read_token, token_path,
    )


def _connect(socket_path: str | Path, timeout: float) -> socket.socket:
    if is_tcp_address(socket_path):
        return socket.create_connection(parse_tcp_address(socket_path), timeout=timeout)
    if not hasattr(socket, "AF_UNIX"):
        raise OSError(f"Unix sockets are unavailable here; set CHROME_MCP_SOCKET to the daemon's TCP address, not {socket_path}")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(str(socket_path))
    except OSError:
        connection.close()
        raise
    return connection


def _can_connect(socket_path: str | Path) -> bool:
    try:
        with _connect(socket_path, 0.5):
            return True
    except OSError:
        return False


def ensure_daemon_running(socket_path: str | Path | None = None, auto_spawn: bool = False) -> str | Path:
    path = socket_path or default_socket_path()
    if _can_connect(path):
        return path
    if not auto_spawn:
        raise RuntimeError(f"Chrome MCP daemon is not running at {path}; start tools/chrome_mcp_daemon.py explicitly")
    command = [sys.executable, str(Path(__file__).with_name("chrome_mcp_daemon.py"))]
    # The daemon reads its address from the environment: start it on the one polled below.
    env = {**os.environ, "CHROME_MCP_SOCKET": str(path)}
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    else:
        process = subprocess.Popen(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if process.poll() is not None:
            raise RuntimeError(f"Chrome MCP daemon failed to start (exit {process.returncode})")
        if _can_connect(path):
            return path
        time.sleep(0.1)
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
    process.terminate()
    raise RuntimeError(f"Chrome MCP daemon did not become ready at {path}")


class ChromeDevToolsClient:
    def __init__(
        self,
        socket_path: str | Path | None = None,
        auto_spawn: bool = False,
        response_timeout: float = DEFAULT_REQUEST_TIMEOUT + 10.0,
    ):
        self.socket_path = ensure_daemon_running(socket_path, auto_spawn=auto_spawn)
        # slightly longer than the daemon's own request timeout so its error reaches us first
        self.response_timeout = response_timeout

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        if name not in ALLOWED_TOOLS:
            raise ValueError(f"Chrome MCP tool is not allowed: {name!r}")
        ensure_daemon_running(self.socket_path)
        request: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        if is_tcp_address(self.socket_path):
            # Read on every call: a restarted daemon writes a new token.
            _host, port = parse_tcp_address(self.socket_path)
            token = read_token(token_path(port))
            if token is None:
                raise RuntimeError(
                    f"No Chrome MCP daemon token at {token_path(port)}; the daemon on {self.socket_path} "
                    "was not started by this account"
                )
            request["token"] = token
        with _connect(self.socket_path, self.response_timeout) as connection:
            connection.sendall((json.dumps(request) + "\n").encode("utf-8"))
            data = b""
            try:
                while b"\n" not in data:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            except TimeoutError as exc:
                raise RuntimeError(
                    f"Chrome MCP daemon did not answer '{name}' within {self.response_timeout:g}s; request timed out"
                ) from exc
        if not data:
            raise RuntimeError("Chrome MCP daemon returned no response")
        response = json.loads(data.split(b"\n", 1)[0])
        if "error" in response:
            raise RuntimeError(f"MCP error: {response['error']}")
        return response.get("result", {})

    def list_pages(self) -> Any:
        return self.call_tool("list_pages")

    def navigate_page(self, page_id: int, url: str) -> Any:
        return self.call_tool("navigate_page", {"pageId": page_id, "url": url})

    def evaluate_script(self, page_id: int, function_code: str) -> Any:
        return self.call_tool("evaluate_script", {"pageId": page_id, "function": function_code})

    def close(self) -> None:
        pass


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python3 tools/chrome_devtools_client.py <tool_name> [json_args]", file=sys.stderr)
        raise SystemExit(1)
    tool_name = sys.argv[1]
    arguments = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(json.dumps(ChromeDevToolsClient().call_tool(tool_name, arguments), indent=2))


if __name__ == "__main__":
    main()
