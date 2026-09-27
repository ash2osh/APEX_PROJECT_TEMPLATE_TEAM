#!/usr/bin/env python3
"""One-user Chrome DevTools MCP daemon with a persistent consent session.

One `chrome-devtools-mcp --autoConnect` child is kept alive so Chrome asks for remote-debugging
consent once; local clients (same uid, private UNIX socket) send `{"name","arguments"}` lines
and receive the JSON-RPC response. Requests are correlated by id through a reader thread, so a
call the MCP never answers (consent prompt pending, another instance holding the connection)
times out and reports an error instead of wedging every later client.
"""

import json
import fcntl
import os
from pathlib import Path
import queue
import select
import signal
import socket
import stat
import subprocess
import sys
import threading
import time

ALLOWED_TOOLS = frozenset({
    "click", "close_page", "drag", "emulate", "evaluate_script", "fill", "fill_form",
    "get_console_message", "get_network_request", "handle_dialog", "hover",
    "list_console_messages", "list_network_requests", "list_pages", "navigate_page",
    "new_page", "performance_analyze_insight", "performance_start_trace",
    "performance_stop_trace", "press_key", "resize_page", "select_page",
    "take_screenshot", "take_snapshot", "type_text", "upload_file", "wait_for",
})
DEFAULT_REQUEST_TIMEOUT = float(os.environ.get("CHROME_MCP_TIMEOUT", "60"))
CLIENT_IO_TIMEOUT = 10.0
MAX_REQUEST_BYTES = 1024 * 1024


def default_socket_path() -> Path:
    override = os.environ.get("CHROME_MCP_SOCKET")
    if override:
        return Path(override)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "chrome-mcp/chrome-mcp.sock"
    return Path(f"/tmp/chrome-mcp-{os.getuid()}/chrome-mcp.sock")


def prepare_socket_path(path: Path) -> Path:
    path = path.expanduser().absolute()
    _ensure_private_socket_directory(path.parent)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if info.st_uid != os.getuid() or not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError(f"Refusing to replace unsafe socket path: {path}")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.25)
        try:
            probe.connect(str(path))
        except ConnectionRefusedError:
            pass
        except OSError as exc:
            raise RuntimeError(f"Refusing to replace ambiguous socket path {path}: {exc}") from exc
        else:
            raise RuntimeError(f"Chrome MCP daemon is already listening on {path}")
        finally:
            probe.close()
        path.unlink()
    return path


def _ensure_private_socket_directory(parent: Path) -> None:
    """Create only the dedicated socket directory; never chmod an existing path."""
    parent = parent.absolute()
    for ancestor in reversed(parent.parents):
        try:
            info = ancestor.lstat()
        except FileNotFoundError as exc:
            raise RuntimeError(f"Socket directory ancestor does not exist: {ancestor}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError(f"Socket path has an unsafe directory ancestor: {ancestor}")

    created = False
    try:
        parent.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    except FileNotFoundError as exc:
        raise RuntimeError(f"Socket parent must have an existing parent directory: {parent}") from exc

    if created:
        os.chmod(parent, 0o700)
    try:
        info = parent.lstat()
    except OSError as exc:
        raise RuntimeError(f"Cannot inspect socket directory {parent}: {exc}") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or (info.st_mode & 0o777) != 0o700
    ):
        raise RuntimeError(
            f"Socket parent must be an owned, non-symlink directory with mode 0700: {parent}"
        )


class ChromeMcpDaemon:
    def __init__(
        self,
        executable: str | None = None,
        socket_path: Path | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        initialize_timeout: float | None = None,
    ):
        self.executable = executable or os.environ.get("CHROME_MCP_EXECUTABLE", "chrome-devtools-mcp")
        self.socket_path = socket_path or default_socket_path()
        self.request_timeout = request_timeout
        self.initialize_timeout = request_timeout if initialize_timeout is None else initialize_timeout
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()            # serialises writes to the MCP stdin and id allocation
        self.request_id = 0
        self.pending: dict[int, queue.Queue[dict]] = {}
        self.pending_lock = threading.Lock()
        self.server_sock: socket.socket | None = None
        self.socket_identity: tuple[int, int] | None = None
        self.lock_fd: int | None = None
        self.lock_path: Path | None = None
        self.stopping = threading.Event()
        self.initialize_timed_out = threading.Event()
        self.initialize_complete = threading.Event()
        self.reader_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None

    def _acquire_startup_lock(self) -> None:
        _ensure_private_socket_directory(self.socket_path.expanduser().absolute().parent)
        self.socket_path = self.socket_path.expanduser().absolute()
        lock_path = self.socket_path.with_name(self.socket_path.name + ".lock")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        created = False
        try:
            fd = os.open(lock_path, flags | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            try:
                fd = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
            except OSError as exc:
                raise RuntimeError(f"Cannot safely open daemon lock {lock_path}: {exc}") from exc
        except OSError as exc:
            raise RuntimeError(f"Cannot create daemon lock {lock_path}: {exc}") from exc

        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or (info.st_mode & 0o777) != 0o600
            ):
                raise RuntimeError(f"Refusing unsafe daemon lock file: {lock_path}")
            if created:
                os.fchmod(fd, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(f"A Chrome MCP daemon is already starting or running for {self.socket_path}") from exc
        except Exception:
            os.close(fd)
            raise
        self.lock_fd = fd
        self.lock_path = lock_path

    # ------------------------------------------------------------------ MCP child I/O
    def _log(self, message: str) -> None:
        if not self.stopping.is_set():
            try:
                print(message, flush=True)
            except (ValueError, OSError):
                pass

    def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            self._log(f"chrome-devtools-mcp: {line.rstrip()}")

    def _write(self, payload: dict, deadline: float | None = None) -> None:
        assert self.proc and self.proc.stdin
        fd = self.proc.stdin.fileno()
        if os.get_blocking(fd):
            os.set_blocking(fd, False)
        encoded = (json.dumps(payload) + "\n").encode("utf-8")
        offset = 0
        write_deadline = deadline if deadline is not None else time.monotonic() + self.request_timeout
        while offset < len(encoded):
            remaining = write_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out writing a request to chrome-devtools-mcp")
            try:
                _readable, writable, _exceptional = select.select([], [fd], [], remaining)
            except OSError as exc:
                raise RuntimeError(f"Failed to wait for chrome-devtools-mcp stdin: {exc}") from exc
            if not writable:
                raise TimeoutError("Timed out writing a request to chrome-devtools-mcp")
            try:
                written = os.write(fd, encoded[offset:])
            except BlockingIOError:
                continue
            if written <= 0:
                raise RuntimeError("chrome-devtools-mcp stdin closed while writing")
            offset += written

    def _read_response(self, request_id: int) -> dict:
        """Synchronous reader used only for the initialize handshake (before the reader thread)."""
        assert self.proc and self.proc.stdout
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("chrome-devtools-mcp closed its response stream")
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON-RPC response from chrome-devtools-mcp: {exc}") from exc
            if message.get("id") == request_id:
                return message
            if "id" in message:
                raise RuntimeError(
                    f"Unexpected JSON-RPC response id {message.get('id')!r}; expected {request_id!r}"
                )
            # Legal notification/log message; keep reading for this request's response.

    def _dispatch_responses(self) -> None:
        """Route every MCP stdout line to the waiting request; answer server->client requests."""
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._log(f"chrome-devtools-mcp: non-JSON output ignored: {line.rstrip()[:200]}")
                continue
            if "id" in message and "method" in message:
                # A request from the server (roots/list, sampling, ...): decline politely.
                with self.lock:
                    try:
                        self._write({"jsonrpc": "2.0", "id": message["id"],
                                     "error": {"code": -32601, "message": "Method not supported by daemon"}})
                    except (OSError, ValueError):
                        pass
                continue
            if "id" not in message:
                continue  # notification
            with self.pending_lock:
                waiter = self.pending.pop(message["id"], None)
            if waiter is None:
                self._log(f"chrome-devtools-mcp: late or unknown response id {message.get('id')!r} dropped")
                continue
            waiter.put(message)
        # stdout closed: fail every pending request
        with self.pending_lock:
            waiters = list(self.pending.values())
            self.pending.clear()
        for waiter in waiters:
            waiter.put({"jsonrpc": "2.0", "error": {"code": -32000, "message": "chrome-devtools-mcp closed its response stream"}})

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        if tool_name not in ALLOWED_TOOLS:
            raise ValueError(f"Tool is not allowed: {tool_name!r}")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        deadline = time.monotonic() + self.request_timeout
        waiter: queue.Queue[dict] = queue.Queue(maxsize=1)
        remaining = max(0.0, deadline - time.monotonic())
        if not self.lock.acquire(timeout=remaining):
            self.shutdown()
            raise RuntimeError(
                f"chrome-devtools-mcp did not accept '{tool_name}' within {self.request_timeout:g}s; request timed out."
            )
        request_id: int | None = None
        try:
            if not self.proc or self.proc.poll() is not None:
                raise RuntimeError("chrome-devtools-mcp process is not running")
            self.request_id += 1
            request_id = self.request_id
            with self.pending_lock:
                self.pending[request_id] = waiter
            try:
                self._write({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                             "params": {"name": tool_name, "arguments": arguments}}, deadline=deadline)
            except TimeoutError as exc:
                with self.pending_lock:
                    self.pending.pop(request_id, None)
                self.shutdown()
                raise RuntimeError(
                    f"chrome-devtools-mcp timed out writing '{tool_name}' within {self.request_timeout:g}s; daemon stopped."
                ) from exc
            except (OSError, ValueError, RuntimeError) as exc:
                with self.pending_lock:
                    self.pending.pop(request_id, None)
                raise RuntimeError(f"Failed to send request to chrome-devtools-mcp: {exc}") from exc
        finally:
            self.lock.release()

        try:
            return waiter.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            with self.pending_lock:
                if request_id is not None:
                    self.pending.pop(request_id, None)
            raise RuntimeError(
                f"chrome-devtools-mcp did not answer '{tool_name}' within {self.request_timeout:g}s total; request timed out. "
                "Check Chrome's remote-debugging consent prompt and that no other MCP instance holds the connection."
            ) from None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._install_signal_handlers()
        try:
            # The sidecar flock serializes socket replacement and child startup. Bind before
            # spawning MCP so a competing daemon cannot create a second browser connection.
            self._acquire_startup_lock()
            socket_path = prepare_socket_path(self.socket_path)
            self.socket_path = socket_path
            self.server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server_sock.bind(str(socket_path))
            os.chmod(socket_path, 0o600)
            bound = socket_path.lstat()
            self.socket_identity = (bound.st_dev, bound.st_ino)
            self.server_sock.listen(10)

            self._log("Starting persistent chrome-devtools-mcp process...")
            self.proc = subprocess.Popen(
                [self.executable, "--autoConnect"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
            )
            self.stderr_thread = threading.Thread(target=self._drain_stderr, name="mcp-stderr")
            self.stderr_thread.start()

            initialize_deadline = time.monotonic() + self.initialize_timeout
            init_timer = threading.Timer(self.initialize_timeout, self._expire_initialize)
            init_timer.daemon = True
            init_timer.start()
            try:
                self.request_id += 1
                self._write({
                    "jsonrpc": "2.0", "id": self.request_id, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                               "clientInfo": {"name": "chrome-mcp-daemon", "version": "1.0"}},
                }, deadline=initialize_deadline)
                response = self._read_response(self.request_id)
                if self.initialize_timed_out.is_set():
                    raise TimeoutError("Timed out waiting for chrome-devtools-mcp initialization")
                if response.get("error") or response.get("id") != self.request_id:
                    raise RuntimeError(f"chrome-devtools-mcp initialization failed: {response}")
                self._write({"jsonrpc": "2.0", "method": "notifications/initialized"},
                            deadline=initialize_deadline)
                self.initialize_complete.set()
            except TimeoutError as exc:
                self.initialize_timed_out.set()
                self.shutdown()
                raise RuntimeError(
                    f"chrome-devtools-mcp initialization timed out after {self.initialize_timeout:g}s"
                ) from exc
            except RuntimeError as exc:
                if self.initialize_timed_out.is_set():
                    raise RuntimeError(
                        f"chrome-devtools-mcp initialization timed out after {self.initialize_timeout:g}s"
                    ) from exc
                if self.stopping.is_set():
                    return
                raise
            except OSError as exc:
                if self.initialize_timed_out.is_set():
                    raise RuntimeError(
                        f"chrome-devtools-mcp initialization timed out after {self.initialize_timeout:g}s"
                    ) from exc
                if self.stopping.is_set():
                    return
                raise RuntimeError(f"chrome-devtools-mcp initialization failed: {exc}") from exc
            finally:
                init_timer.cancel()

            self.reader_thread = threading.Thread(target=self._dispatch_responses, name="mcp-stdout")
            self.reader_thread.start()
            self._log(f"Daemon listening on {socket_path}")
            while not self.stopping.is_set():
                try:
                    connection, _ = self.server_sock.accept()
                except OSError:
                    break  # socket closed by shutdown()
                threading.Thread(target=self.handle_client, args=(connection,), daemon=True).start()
        finally:
            self.close()

    def _expire_initialize(self) -> None:
        if not self.initialize_complete.is_set():
            self.initialize_timed_out.set()
            self._log(f"chrome-devtools-mcp initialization timed out after {self.initialize_timeout:g}s")
            self.shutdown()

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return

        def _stop(signum, _frame):
            self._log(f"Received signal {signum}; shutting down")
            self.shutdown()

        for signum in (signal.SIGTERM, signal.SIGINT, getattr(signal, "SIGHUP", None)):
            if signum is not None:
                signal.signal(signum, _stop)

    def shutdown(self) -> None:
        """Stop accepting requests and terminate MCP to unblock startup or pipe I/O."""
        self.stopping.set()
        server = self.server_sock
        if server is not None:
            # shutdown() wakes a thread blocked in accept(); close() alone does not on Linux
            for action in (lambda: server.shutdown(socket.SHUT_RDWR), server.close):
                try:
                    action()
                except OSError:
                    pass
        process = self.proc
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def handle_client(self, connection: socket.socket) -> None:
        with connection:
            try:
                connection.settimeout(CLIENT_IO_TIMEOUT)
                data = b""
                while b"\n" not in data:
                    chunk = connection.recv(4096)
                    if not chunk:
                        return
                    data += chunk
                    if len(data) > MAX_REQUEST_BYTES:
                        raise ValueError("request exceeds 1 MiB")
                request = json.loads(data.split(b"\n", 1)[0])
                response = self.call_tool(request.get("name"), request.get("arguments", {}))
                connection.settimeout(CLIENT_IO_TIMEOUT)
                connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
            except Exception as exc:
                response = {"jsonrpc": "2.0", "error": {"code": -32600, "message": str(exc)}}
                try:
                    connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
                except OSError:
                    pass

    def close(self) -> None:
        self.stopping.set()
        process = self.proc
        if self.server_sock:
            try:
                self.server_sock.close()
            except OSError:
                pass
            self.server_sock = None
        if process:
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for thread in (self.reader_thread, self.stderr_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=2)
        if process:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream:
                    stream.close()
        self.proc = None
        try:
            info = self.socket_path.lstat()
            identity = (info.st_dev, info.st_ino)
            if (
                self.socket_identity is not None
                and identity == self.socket_identity
                and info.st_uid == os.getuid()
                and stat.S_ISSOCK(info.st_mode)
            ):
                self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self.socket_identity = None
        if self.lock_fd is not None:
            try:
                os.close(self.lock_fd)
            except OSError:
                pass
            self.lock_fd = None


if __name__ == "__main__":
    try:
        ChromeMcpDaemon().start()
    except RuntimeError as exc:
        print(f"chrome-mcp-daemon: {exc}", file=sys.stderr)
        sys.exit(1)
