#!/usr/bin/env python3
"""Fake chrome-devtools-mcp: controlled read/response modes for lifecycle tests.

  hang        - answers initialize but not tools/call
  echo        - answers initialize and every tools/call
  init-hang   - never answers initialize
  stop-reading - answers initialize, then stops consuming stdin
"""
import json
import os
import sys
import time

mode = os.environ.get("FAKE_MCP_MODE", "hang")
print("fake mcp: starting", file=sys.stderr, flush=True)
start_file = os.environ.get("FAKE_MCP_START_FILE")
if start_file:
    with open(start_file, "a", encoding="utf-8") as marker:
        marker.write(f"{os.getpid()}\n")
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        if mode != "init-hang":
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {}}}), flush=True)
    elif message.get("method") == "notifications/initialized":
        ready_file = os.environ.get("FAKE_MCP_READY_FILE")
        if ready_file:
            with open(ready_file, "w", encoding="utf-8") as marker:
                marker.write(str(os.getpid()))
        if mode == "stop-reading":
            while True:
                time.sleep(1)
    elif message.get("method") == "tools/call":
        if mode == "echo":
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"echo": message["params"]}}), flush=True)
        # hang: say nothing, keep reading
