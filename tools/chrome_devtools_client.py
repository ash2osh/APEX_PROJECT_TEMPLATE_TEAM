#!/usr/bin/env python3
"""Client for the explicitly started, persistent Chrome DevTools MCP daemon."""

import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any, Dict, Optional

try:
    from tools.chrome_mcp_daemon import ALLOWED_TOOLS, DEFAULT_REQUEST_TIMEOUT, default_socket_path
except ModuleNotFoundError:  # direct execution from tools/
    from chrome_mcp_daemon import ALLOWED_TOOLS, DEFAULT_REQUEST_TIMEOUT, default_socket_path


def _can_connect(socket_path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.5)
            connection.connect(str(socket_path))
        return True
    except OSError:
        return False


def ensure_daemon_running(socket_path: Optional[Path] = None, auto_spawn: bool = False) -> Path:
    path = socket_path or default_socket_path()
    if _can_connect(path):
        return path
    if not auto_spawn:
        raise RuntimeError(f"Chrome MCP daemon is not running at {path}; start tools/chrome_mcp_daemon.py explicitly")
    command = [sys.executable, str(Path(__file__).with_name("chrome_mcp_daemon.py"))]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if process.poll() is not None:
            raise RuntimeError(f"Chrome MCP daemon failed to start (exit {process.returncode})")
        if _can_connect(path):
            return path
        time.sleep(0.1)
    process.terminate()
    raise RuntimeError(f"Chrome MCP daemon did not become ready at {path}")


class ChromeDevToolsClient:
    def __init__(
        self,
        socket_path: Optional[Path] = None,
        auto_spawn: bool = False,
        response_timeout: float = DEFAULT_REQUEST_TIMEOUT + 10.0,
    ):
        self.socket_path = ensure_daemon_running(socket_path, auto_spawn=auto_spawn)
        # slightly longer than the daemon's own request timeout so its error reaches us first
        self.response_timeout = response_timeout

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        if name not in ALLOWED_TOOLS:
            raise ValueError(f"Chrome MCP tool is not allowed: {name!r}")
        ensure_daemon_running(self.socket_path)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.response_timeout)
            connection.connect(str(self.socket_path))
            connection.sendall((json.dumps({"name": name, "arguments": arguments or {}}) + "\n").encode("utf-8"))
            data = b""
            try:
                while b"\n" not in data:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout as exc:
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
