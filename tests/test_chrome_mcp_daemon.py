import importlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from tools import chrome_mcp_daemon
from tools.chrome_mcp_daemon import ChromeMcpDaemon, prepare_socket_path
from tools.chrome_devtools_client import ChromeDevToolsClient, ensure_daemon_running

# The Unix-socket mode needs AF_UNIX, fcntl, POSIX file modes and process groups. Each
# behaviour it covers has a TCP twin in ChromeMcpTcpTests, which runs on every platform.
UNIX_MODE = os.name != "nt" and hasattr(socket, "AF_UNIX")
UNIX_ONLY = "Unix-socket mode is POSIX-only (AF_UNIX, fcntl, file modes); its TCP twin runs here"
FAKE = str(Path(__file__).resolve().parent / "fixtures/mcp/fake_mcp_server.py")


def no_signal_handlers():
    """start() on the main thread would install the daemon's handlers in the test runner."""
    return patch.object(ChromeMcpDaemon, "_install_signal_handlers")


def free_tcp_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{probe.getsockname()[1]}"


class ChromeMcpConfigurationTests(unittest.TestCase):
    def test_generic_socket_override_is_used(self):
        with patch.dict(os.environ, {"CHROME_MCP_SOCKET": "/tmp/portable-chrome.sock"}, clear=True):
            self.assertEqual(chrome_mcp_daemon.default_socket_path(), Path("/tmp/portable-chrome.sock"))

    def test_generic_timeout_override_is_used(self):
        with patch.dict(os.environ, {"CHROME_MCP_TIMEOUT": "17.5"}, clear=True):
            reloaded = importlib.reload(chrome_mcp_daemon)
            self.assertEqual(reloaded.DEFAULT_REQUEST_TIMEOUT, 17.5)
        importlib.reload(chrome_mcp_daemon)

    def test_generic_executable_override_is_used(self):
        with patch.dict(os.environ, {"CHROME_MCP_EXECUTABLE": "/opt/bin/chrome-mcp"}, clear=True):
            daemon = ChromeMcpDaemon()
            self.assertEqual(daemon.executable, "/opt/bin/chrome-mcp")

    @unittest.skipUnless(UNIX_MODE, UNIX_ONLY)
    def test_default_socket_uses_xdg_runtime_dir(self):
        with tempfile.TemporaryDirectory() as runtime:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}, clear=True):
                expected = Path(runtime) / "chrome-mcp/chrome-mcp.sock"
                self.assertEqual(chrome_mcp_daemon.default_socket_path(), expected)

    @unittest.skipUnless(UNIX_MODE, UNIX_ONLY)
    def test_default_socket_uses_uid_fallback_without_xdg_runtime_dir(self):
        with patch.dict(os.environ, {}, clear=True):
            expected = Path(f"/tmp/chrome-mcp-{os.getuid()}/chrome-mcp.sock")
            self.assertEqual(chrome_mcp_daemon.default_socket_path(), expected)


class ChromeMcpDaemonTests(unittest.TestCase):
    @unittest.skipUnless(UNIX_MODE, UNIX_ONLY)
    def test_socket_directory_is_private(self):
        with tempfile.TemporaryDirectory() as temp:
            path = prepare_socket_path(Path(temp) / "service/chrome.sock")
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    @unittest.skipUnless(UNIX_MODE, UNIX_ONLY)
    def test_existing_public_socket_parent_is_rejected_without_chmod(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / "public"
            parent.mkdir(mode=0o755)
            os.chmod(parent, 0o755)
            with self.assertRaises(RuntimeError):
                prepare_socket_path(parent / "chrome.sock")
            self.assertEqual(parent.stat().st_mode & 0o777, 0o755)

    @unittest.skipUnless(UNIX_MODE, UNIX_ONLY)
    def test_symlink_socket_parent_is_rejected_without_chmod(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            protected = root / "protected"
            protected.mkdir(mode=0o755)
            os.chmod(protected, 0o755)
            link = root / "linked"
            link.symlink_to(protected, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                prepare_socket_path(link / "chrome.sock")
            self.assertEqual(protected.stat().st_mode & 0o777, 0o755)

    @unittest.skipUnless(UNIX_MODE, UNIX_ONLY)
    def test_existing_regular_file_is_never_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "service/chrome.sock"
            target.parent.mkdir()
            target.write_text("keep", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                prepare_socket_path(target)
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    @unittest.skipUnless(UNIX_MODE, UNIX_ONLY)
    def test_existing_symlink_is_never_followed_or_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            protected = root / "protected"
            protected.write_text("keep", encoding="utf-8")
            target = root / "service/chrome.sock"
            target.parent.mkdir()
            target.symlink_to(protected)
            with self.assertRaises(RuntimeError):
                prepare_socket_path(target)
            self.assertTrue(target.is_symlink())
            self.assertEqual(protected.read_text(encoding="utf-8"), "keep")

    def test_unknown_tool_is_rejected_without_forwarding(self):
        daemon = ChromeMcpDaemon(socket_path=Path("/tmp/unused-test.sock"))
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        client.sendall(b'{"name":"arbitrary_tool","arguments":{}}\n')
        client.shutdown(socket.SHUT_WR)
        daemon.handle_client(server)
        response = json.loads(client.recv(4096).decode("utf-8"))
        self.assertIn("not allowed", response["error"]["message"])

    def test_response_reader_skips_notifications_and_matches_id(self):
        daemon = ChromeMcpDaemon(socket_path=Path("/tmp/unused-test.sock"))
        daemon.proc = SimpleNamespace(stdout=StringIO(
            '{"jsonrpc":"2.0","method":"notifications/message","params":{}}\n'
            '{"jsonrpc":"2.0","id":7,"result":{"ok":true}}\n'
        ))
        response = daemon._read_response(7)
        self.assertEqual(response["result"], {"ok": True})

    @unittest.skipUnless(UNIX_MODE, UNIX_ONLY)
    def test_live_socket_is_not_unlinked_by_second_daemon(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "service/chrome.sock"
            target.parent.mkdir()
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(str(target))
            listener.listen(1)
            with self.assertRaises(RuntimeError):
                prepare_socket_path(target)
            self.assertTrue(target.exists())

    @unittest.skipUnless(UNIX_MODE, UNIX_ONLY)
    def test_client_gives_up_on_a_silent_daemon(self):
        import threading
        from tools.chrome_devtools_client import ChromeDevToolsClient
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "silent.sock"
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            listener.bind(str(target))
            listener.listen(2)
            accepted = []
            def accept_connections():
                for _ in range(2):
                    connection, _address = listener.accept()
                    accepted.append(connection)

            thread = threading.Thread(target=accept_connections, daemon=True)
            thread.start()
            try:
                client = ChromeDevToolsClient(socket_path=target, response_timeout=0.3)
                with self.assertRaises(RuntimeError) as context:
                    client.call_tool("list_pages")
                self.assertIn("timed out", str(context.exception))
            finally:
                for connection in accepted:
                    connection.close()
                thread.join(timeout=1)

    def test_missing_daemon_fails_loudly_when_autostart_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(RuntimeError):
                ensure_daemon_running(Path(temp) / "missing.sock", auto_spawn=False)

    def test_missing_daemon_does_not_spawn_child(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("tools.chrome_devtools_client.subprocess.Popen") as popen:
                with self.assertRaises(RuntimeError):
                    ChromeDevToolsClient(socket_path=Path(temp) / "missing.sock")
                popen.assert_not_called()


@unittest.skipUnless(UNIX_MODE, UNIX_ONLY)
class ChromeMcpDaemonLifecycleTests(unittest.TestCase):
    FAKE = FAKE

    def call(self, socket_path: Path, name: str, timeout: float = 5.0) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(socket_path))
            connection.sendall((json.dumps({"name": name, "arguments": {}}) + "\n").encode("utf-8"))
            data = b""
            while b"\n" not in data:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.split(b"\n", 1)[0])

    def wait_for_socket(self, socket_path: Path, seconds: float = 5.0) -> None:
        import time
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    probe.connect(str(socket_path))
                return
            except OSError:
                time.sleep(0.05)
        self.fail(f"daemon socket {socket_path} never became ready")

    def wait_for_file(self, marker: Path, seconds: float = 5.0) -> None:
        import time
        deadline = time.time() + seconds
        while time.time() < deadline:
            if marker.exists():
                return
            time.sleep(0.02)
        self.fail(f"marker file {marker} was not created")

    def read_marker_lines(self, marker: Path) -> list[str]:
        if not marker.exists():
            return []
        return marker.read_text(encoding="utf-8").splitlines()

    def test_unanswered_tool_call_times_out_and_releases_the_lock(self):
        import threading
        import time
        with tempfile.TemporaryDirectory() as temp:
            socket_path = Path(temp) / "service/chrome.sock"
            with patch.dict(os.environ, {"FAKE_MCP_MODE": "hang"}):
                daemon = ChromeMcpDaemon(executable=self.FAKE, socket_path=socket_path, request_timeout=0.3)
                thread = threading.Thread(target=daemon.start, daemon=True)
                thread.start()
                try:
                    self.wait_for_socket(socket_path)
                    started = time.time()
                    first = self.call(socket_path, "list_pages")
                    second = self.call(socket_path, "list_pages")
                    self.assertLess(time.time() - started, 3.0)
                    self.assertIn("timed out", first["error"]["message"])
                    self.assertIn("timed out", second["error"]["message"])
                    self.assertTrue(daemon.proc and daemon.proc.poll() is None, "MCP child must survive a timeout")
                finally:
                    daemon.shutdown()
                    thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(socket_path.exists())

    def test_answered_tool_call_is_correlated_by_id(self):
        import threading
        with tempfile.TemporaryDirectory() as temp:
            socket_path = Path(temp) / "service/chrome.sock"
            with patch.dict(os.environ, {"FAKE_MCP_MODE": "echo"}):
                daemon = ChromeMcpDaemon(executable=self.FAKE, socket_path=socket_path, request_timeout=2.0)
                thread = threading.Thread(target=daemon.start, daemon=True)
                thread.start()
                try:
                    self.wait_for_socket(socket_path)
                    response = self.call(socket_path, "list_pages")
                    self.assertEqual(response["result"]["echo"]["name"], "list_pages")
                finally:
                    daemon.shutdown()
                    thread.join(timeout=5)

    def test_bound_socket_is_private(self):
        import threading
        with tempfile.TemporaryDirectory() as temp:
            socket_path = Path(temp) / "service/chrome.sock"
            with patch.dict(os.environ, {"FAKE_MCP_MODE": "hang"}):
                daemon = ChromeMcpDaemon(executable=self.FAKE, socket_path=socket_path, request_timeout=0.3)
                thread = threading.Thread(target=daemon.start, daemon=True)
                thread.start()
                try:
                    self.wait_for_socket(socket_path)
                    self.assertEqual(socket_path.stat().st_mode & 0o777, 0o600)
                finally:
                    daemon.shutdown()
                    thread.join(timeout=5)
                self.assertFalse(thread.is_alive())

    def test_second_daemon_cannot_spawn_while_first_initializes(self):
        import threading
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            socket_path = root / "service/chrome.sock"
            starts = root / "fake-pids"
            errors = []
            with patch.dict(os.environ, {"FAKE_MCP_MODE": "init-hang", "FAKE_MCP_START_FILE": str(starts)}):
                first = ChromeMcpDaemon(executable=self.FAKE, socket_path=socket_path)
                second = ChromeMcpDaemon(executable=self.FAKE, socket_path=socket_path)

                def run(daemon):
                    try:
                        daemon.start()
                    except Exception as exc:
                        errors.append(exc)

                first_thread = threading.Thread(target=run, args=(first,), daemon=True)
                first_thread.start()
                self.wait_for_file(starts)
                second_thread = threading.Thread(target=run, args=(second,), daemon=True)
                second_thread.start()
                try:
                    import time
                    time.sleep(0.3)
                    self.assertEqual(len(self.read_marker_lines(starts)), 1)
                    self.assertTrue(any("already" in str(error).lower() for error in errors))
                finally:
                    first.close()
                    second.close()
                    first_thread.join(timeout=3)
                    second_thread.join(timeout=3)
                self.assertFalse(first_thread.is_alive())
                self.assertFalse(second_thread.is_alive())

    def test_request_deadline_covers_blocked_mcp_stdin_write(self):
        import threading
        import time
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            socket_path = root / "service/chrome.sock"
            ready = root / "fake-ready"
            with patch.dict(os.environ, {"FAKE_MCP_MODE": "stop-reading", "FAKE_MCP_READY_FILE": str(ready)}):
                daemon = ChromeMcpDaemon(executable=self.FAKE, socket_path=socket_path, request_timeout=0.25)
                daemon_thread = threading.Thread(target=daemon.start, daemon=True)
                daemon_thread.start()
                self.wait_for_file(ready)
                outcomes = []

                def call():
                    started = time.monotonic()
                    try:
                        daemon.call_tool("evaluate_script", {"function": "x" * (2 * 1024 * 1024)})
                    except Exception as exc:
                        outcomes.append((time.monotonic() - started, str(exc)))

                caller = threading.Thread(target=call, daemon=True)
                caller.start()
                try:
                    caller.join(timeout=1.5)
                    self.assertFalse(caller.is_alive(), "a blocked child-stdin write must obey the request deadline")
                    self.assertTrue(outcomes)
                    self.assertLess(outcomes[0][0], 1.0)
                    self.assertIn("timed out", outcomes[0][1].lower())
                finally:
                    if daemon.proc and daemon.proc.poll() is None:
                        daemon.proc.kill()
                    daemon.shutdown()
                    daemon.close()
                    caller.join(timeout=3)
                    daemon_thread.join(timeout=3)
                self.assertFalse(caller.is_alive())
                self.assertFalse(daemon_thread.is_alive())

    def test_initialize_response_timeout_stops_child_and_cleans_socket(self):
        import signal
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            socket_path = root / "service/chrome.sock"
            starts = root / "fake-pids"
            env = os.environ.copy()
            env["CHROME_MCP_SOCKET"] = str(socket_path)
            env["CHROME_MCP_EXECUTABLE"] = self.FAKE
            env["CHROME_MCP_TIMEOUT"] = "0.3"
            env["FAKE_MCP_MODE"] = "init-hang"
            env["FAKE_MCP_START_FILE"] = str(starts)
            process = subprocess.Popen(
                [sys.executable, "tools/chrome_mcp_daemon.py"], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
            )
            try:
                self.wait_for_file(starts)
                output, _ = process.communicate(timeout=2)
                self.assertNotEqual(process.returncode, 0, output)
                self.assertIn("timed out", output.lower())
                self.assertFalse(socket_path.exists())
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=3)

    def test_sigterm_interrupts_unanswered_initialize(self):
        import signal
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            socket_path = root / "service/chrome.sock"
            starts = root / "fake-pids"
            env = os.environ.copy()
            env["CHROME_MCP_SOCKET"] = str(socket_path)
            env["CHROME_MCP_EXECUTABLE"] = self.FAKE
            env["FAKE_MCP_MODE"] = "init-hang"
            env["FAKE_MCP_START_FILE"] = str(starts)
            process = subprocess.Popen(
                [sys.executable, "tools/chrome_mcp_daemon.py"], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
            )
            try:
                self.wait_for_file(starts)
                process.send_signal(signal.SIGTERM)
                output, _ = process.communicate(timeout=1.5)
                self.assertEqual(process.returncode, 0, output)
                self.assertFalse(socket_path.exists())
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=3)

    def test_sigterm_exits_cleanly_and_removes_the_socket(self):
        import signal
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as temp:
            socket_path = Path(temp) / "service/chrome.sock"
            env = os.environ.copy()
            env["CHROME_MCP_SOCKET"] = str(socket_path)
            env["CHROME_MCP_EXECUTABLE"] = self.FAKE
            env["FAKE_MCP_MODE"] = "hang"
            process = subprocess.Popen(
                [sys.executable, "tools/chrome_mcp_daemon.py"], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            try:
                self.wait_for_socket(socket_path)
                process.send_signal(signal.SIGTERM)
                output, _ = process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
            self.assertEqual(process.returncode, 0, output)
            self.assertNotIn("Fatal Python error", output)
            self.assertNotIn("Traceback", output)
            self.assertFalse(socket_path.exists())


class ChromeMcpTcpConfigurationTests(unittest.TestCase):
    def test_tcp_endpoints_are_told_apart_from_socket_paths(self):
        for endpoint in ("127.0.0.1:9223", "tcp://127.0.0.1:9223", "localhost:9300"):
            with self.subTest(endpoint=endpoint):
                self.assertTrue(chrome_mcp_daemon.is_tcp_address(endpoint))
        for path in ("/tmp/chrome-mcp.sock", r"C:\Users\me\chrome.sock", "relative/chrome.sock", Path("/tmp/x:1")):
            with self.subTest(path=path):
                self.assertFalse(chrome_mcp_daemon.is_tcp_address(path))
        self.assertEqual(chrome_mcp_daemon.parse_tcp_address("tcp://127.0.0.1:9300"), ("127.0.0.1", 9300))
        self.assertEqual(chrome_mcp_daemon.parse_tcp_address("localhost:9300"), ("localhost", 9300))

    def test_tcp_override_is_used(self):
        with patch.dict(os.environ, {"CHROME_MCP_SOCKET": "127.0.0.1:9311"}, clear=True):
            self.assertEqual(chrome_mcp_daemon.default_socket_path(), "127.0.0.1:9311")

    def test_default_is_tcp_loopback_without_unix_sockets(self):
        with patch.object(chrome_mcp_daemon, "unix_socket_mode_supported", return_value=False):
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(chrome_mcp_daemon.default_socket_path(), "127.0.0.1:9223")

    def test_unix_socket_path_is_refused_where_unix_mode_is_unsupported(self):
        with tempfile.TemporaryDirectory() as temp:
            starts = Path(temp) / "fake-pids"
            with patch.object(chrome_mcp_daemon, "unix_socket_mode_supported", return_value=False):
                with patch.dict(os.environ, {"FAKE_MCP_START_FILE": str(starts)}):
                    daemon = ChromeMcpDaemon(executable=FAKE, socket_path=Path(temp) / "chrome.sock")
                    with self.assertRaisesRegex(RuntimeError, "TCP"):
                        with no_signal_handlers():
                            daemon.start()
            self.assertFalse(starts.exists(), "no MCP child may start")

    def test_non_loopback_tcp_endpoint_is_refused_before_anything_starts(self):
        with tempfile.TemporaryDirectory() as temp:
            starts = Path(temp) / "fake-pids"
            with patch.dict(os.environ, {"FAKE_MCP_START_FILE": str(starts), "CHROME_MCP_TOKEN_DIR": temp}):
                daemon = ChromeMcpDaemon(executable=FAKE, socket_path="0.0.0.0:9312")
                with self.assertRaisesRegex(RuntimeError, "loopback"):
                    with no_signal_handlers():
                        daemon.start()
            self.assertFalse(starts.exists(), "no MCP child may start")
            self.assertEqual(list(Path(temp).glob("*.token")), [])

    def test_python_script_runs_with_this_interpreter(self):
        self.assertEqual(chrome_mcp_daemon._resolve_command(FAKE), [sys.executable, FAKE])

    def test_windows_npm_wrapper_runs_through_cmd(self):
        wrapper = r"C:\Users\me\AppData\Roaming\npm\chrome-devtools-mcp.CMD"
        with patch.object(chrome_mcp_daemon.sys, "platform", "win32"):
            with patch.object(chrome_mcp_daemon.shutil, "which", return_value=wrapper):
                self.assertEqual(chrome_mcp_daemon._resolve_command("chrome-devtools-mcp"), ["cmd.exe", "/c", wrapper])

    def test_plain_executable_is_resolved_on_path(self):
        with patch.object(chrome_mcp_daemon.shutil, "which", return_value="/usr/local/bin/chrome-devtools-mcp"):
            self.assertEqual(chrome_mcp_daemon._resolve_command("chrome-devtools-mcp"), ["/usr/local/bin/chrome-devtools-mcp"])

    def test_token_file_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.dict(os.environ, {"CHROME_MCP_TOKEN_DIR": temp}):
                path = chrome_mcp_daemon.token_path(9313)
                self.assertEqual(path, Path(temp) / "tcp-9313.token")
                chrome_mcp_daemon.write_token(path, "secret-value")
                self.assertEqual(chrome_mcp_daemon.read_token(path), "secret-value")
                self.assertEqual([p.name for p in Path(temp).iterdir()], ["tcp-9313.token"])
                if UNIX_MODE:
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIsNone(chrome_mcp_daemon.read_token(Path(temp) / "missing.token"))


class ChromeMcpTcpTests(unittest.TestCase):
    """The daemon over TCP loopback, the transport on Windows; runs on every platform."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.endpoint = free_tcp_endpoint()
        self.port = int(self.endpoint.rsplit(":", 1)[1])
        self.token_dir = self.root / "tokens"
        environment = patch.dict(os.environ, {"CHROME_MCP_TOKEN_DIR": str(self.token_dir)})
        environment.start()
        self.addCleanup(environment.stop)

    def start_daemon(self, mode: str, request_timeout: float = 2.0, **extra: str) -> tuple[ChromeMcpDaemon, threading.Thread]:
        with patch.dict(os.environ, {"FAKE_MCP_MODE": mode, **extra}):
            daemon = ChromeMcpDaemon(executable=FAKE, socket_path=self.endpoint, request_timeout=request_timeout,
                                     initialize_timeout=30.0)
            thread = threading.Thread(target=daemon.start, daemon=True)
            thread.start()
            self.addCleanup(self.stop_daemon, daemon, thread)
            self.wait_until_listening()
            # The port opens before the daemon starts its MCP child: keep the fake's settings in
            # the environment until the child is initialized, and time each call from then on.
            self.assertTrue(daemon.initialize_complete.wait(30), "the fake MCP child never initialized")
        return daemon, thread

    def stop_daemon(self, daemon: ChromeMcpDaemon, thread: threading.Thread) -> None:
        daemon.shutdown()
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "the daemon must stop")

    def wait_until_listening(self, seconds: float = 10.0) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if chrome_mcp_daemon.read_token(chrome_mcp_daemon.token_path(self.port)):
                try:
                    with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                        return
                except OSError:
                    pass
            time.sleep(0.05)
        self.fail(f"daemon never listened on {self.endpoint}")

    def wait_for_file(self, marker: Path, seconds: float = 10.0) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if marker.exists():
                return
            time.sleep(0.02)
        self.fail(f"marker file {marker} was not created")

    def send(self, request: dict, timeout: float = 10.0) -> dict:
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as connection:
            connection.sendall((json.dumps(request) + "\n").encode("utf-8"))
            data = b""
            while b"\n" not in data:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.split(b"\n", 1)[0])

    def token(self) -> str:
        token = chrome_mcp_daemon.read_token(chrome_mcp_daemon.token_path(self.port))
        self.assertTrue(token)
        return token

    def test_answered_tool_call_is_correlated_by_id(self):
        self.start_daemon("echo")
        response = self.send({"name": "list_pages", "arguments": {}, "token": self.token()})
        self.assertEqual(response["result"]["echo"]["name"], "list_pages")

    def test_request_without_the_token_is_refused_and_not_forwarded(self):
        self.start_daemon("echo")
        for request in ({"name": "list_pages", "arguments": {}},
                        {"name": "list_pages", "arguments": {}, "token": "not-the-token"}):
            with self.subTest(request=request):
                response = self.send(request)
                self.assertNotIn("result", response)
                self.assertIn("token", response["error"]["message"])

    def test_unknown_tool_is_rejected_without_forwarding(self):
        self.start_daemon("echo")
        response = self.send({"name": "arbitrary_tool", "arguments": {}, "token": self.token()})
        self.assertIn("not allowed", response["error"]["message"])

    def test_unanswered_tool_call_times_out_and_releases_the_lock(self):
        daemon, _thread = self.start_daemon("hang", request_timeout=0.3)
        started = time.monotonic()
        first = self.send({"name": "list_pages", "arguments": {}, "token": self.token()})
        second = self.send({"name": "list_pages", "arguments": {}, "token": self.token()})
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertIn("timed out", first["error"]["message"])
        self.assertIn("timed out", second["error"]["message"])
        self.assertTrue(daemon.proc and daemon.proc.poll() is None, "MCP child must survive a timeout")

    def test_second_daemon_on_the_same_port_is_refused_before_it_starts_a_child(self):
        starts = self.root / "fake-pids"
        self.start_daemon("hang", FAKE_MCP_START_FILE=str(starts))
        self.wait_for_file(starts)
        with patch.dict(os.environ, {"FAKE_MCP_MODE": "hang", "FAKE_MCP_START_FILE": str(starts)}):
            second = ChromeMcpDaemon(executable=FAKE, socket_path=self.endpoint)
            with self.assertRaisesRegex(RuntimeError, "already"):
                with no_signal_handlers():
                    second.start()
        time.sleep(0.3)
        self.assertEqual(len(starts.read_text(encoding="utf-8").splitlines()), 1)
        self.assertTrue(self.token(), "the first daemon's token must survive the refused second one")

    def test_shutdown_removes_the_token_and_frees_the_port(self):
        daemon, thread = self.start_daemon("echo")
        token_file = chrome_mcp_daemon.token_path(self.port)
        child = daemon.proc
        self.stop_daemon(daemon, thread)
        self.assertFalse(token_file.exists())
        self.assertIsNotNone(child.poll(), "the MCP child must be stopped")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as rebind:
            rebind.bind(("127.0.0.1", self.port))

    def test_daemon_stops_when_the_mcp_child_exits(self):
        starts = self.root / "fake-pids"
        with patch.dict(os.environ, {"FAKE_MCP_MODE": "exit", "FAKE_MCP_START_FILE": str(starts)}):
            daemon = ChromeMcpDaemon(executable=FAKE, socket_path=self.endpoint)
            thread = threading.Thread(target=daemon.start, daemon=True)
            thread.start()
            self.wait_for_file(starts)
        self.addCleanup(self.stop_daemon, daemon, thread)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "the daemon must stop once its MCP child is gone")
        self.assertFalse(chrome_mcp_daemon.token_path(self.port).exists())

    def test_request_deadline_covers_blocked_mcp_stdin_write(self):
        ready = self.root / "fake-ready"
        daemon, _thread = self.start_daemon("stop-reading", request_timeout=0.25, FAKE_MCP_READY_FILE=str(ready))
        self.wait_for_file(ready)
        outcomes = []

        def call():
            started = time.monotonic()
            try:
                daemon.call_tool("evaluate_script", {"function": "x" * (2 * 1024 * 1024)})
            except Exception as exc:
                outcomes.append((time.monotonic() - started, str(exc)))

        caller = threading.Thread(target=call, daemon=True)
        caller.start()
        caller.join(timeout=3)
        self.assertFalse(caller.is_alive(), "a blocked child-stdin write must obey the request deadline")
        self.assertTrue(outcomes)
        self.assertLess(outcomes[0][0], 2.0)
        self.assertIn("timed out", outcomes[0][1].lower())

    def test_client_sends_the_token_and_returns_the_result(self):
        self.start_daemon("echo")
        client = ChromeDevToolsClient(socket_path=self.endpoint)
        self.assertEqual(client.call_tool("list_pages")["echo"]["name"], "list_pages")

    def test_client_without_a_token_file_fails_loudly(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(8)  # the client probes it twice before the call
        client = ChromeDevToolsClient(socket_path=self.endpoint)
        with self.assertRaisesRegex(RuntimeError, "token"):
            client.call_tool("list_pages")

    def test_client_gives_up_on_a_silent_daemon(self):
        chrome_mcp_daemon.write_token(chrome_mcp_daemon.token_path(self.port), "silent")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(2)
        accepted = []

        def accept_connections():
            for _ in range(2):
                connection, _address = listener.accept()
                accepted.append(connection)

        thread = threading.Thread(target=accept_connections, daemon=True)
        thread.start()
        try:
            client = ChromeDevToolsClient(socket_path=self.endpoint, response_timeout=0.3)
            with self.assertRaises(RuntimeError) as context:
                client.call_tool("list_pages")
            self.assertIn("timed out", str(context.exception))
        finally:
            for connection in accepted:
                connection.close()
            thread.join(timeout=1)

    def test_missing_daemon_fails_loudly_and_spawns_nothing(self):
        with patch("tools.chrome_devtools_client.subprocess.Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "not running"):
                ChromeDevToolsClient(socket_path=self.endpoint)
            popen.assert_not_called()

    def run_daemon_process(self, mode: str, timeout: str = "60", **popen_arguments) -> tuple[subprocess.Popen, Path]:
        starts = self.root / "fake-pids"
        env = os.environ.copy()
        env.update({"CHROME_MCP_SOCKET": self.endpoint, "CHROME_MCP_EXECUTABLE": FAKE, "CHROME_MCP_TIMEOUT": timeout,
                    "FAKE_MCP_MODE": mode, "FAKE_MCP_START_FILE": str(starts)})
        process = subprocess.Popen(
            [sys.executable, "tools/chrome_mcp_daemon.py"], env=env, cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **popen_arguments,
        )
        self.addCleanup(self.kill_tree, process)
        return process, starts

    @staticmethod
    def kill_tree(process: subprocess.Popen) -> None:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, check=False)
            else:
                process.kill()
            process.communicate(timeout=5)

    def test_initialize_response_timeout_stops_child_and_removes_the_token(self):
        process, starts = self.run_daemon_process("init-hang", timeout="0.5")
        self.wait_for_file(starts)
        output, _ = process.communicate(timeout=10)
        self.assertNotEqual(process.returncode, 0, output)
        self.assertIn("timed out", output.lower())
        self.assertFalse(chrome_mcp_daemon.token_path(self.port).exists())
        self.assertFalse(process_is_running(int(starts.read_text(encoding="utf-8").split()[0])), "the MCP child must be stopped")

    @unittest.skipUnless(os.name == "nt", "Ctrl-Break is a Windows console event; the POSIX twin is test_sigterm_exits_cleanly_and_removes_the_socket")
    def test_ctrl_break_exits_cleanly_and_stops_the_child(self):
        process, starts = self.run_daemon_process("hang", creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        self.wait_until_listening()
        self.wait_for_file(starts)
        child = int(starts.read_text(encoding="utf-8").split()[0])
        process.send_signal(signal.CTRL_BREAK_EVENT)
        output, _ = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, output)
        self.assertNotIn("Traceback", output)
        self.assertFalse(chrome_mcp_daemon.token_path(self.port).exists())
        self.assertFalse(process_is_running(child), "the MCP child must be stopped")


def process_is_running(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        # A zombie child of an exited parent is reaped by init; treat it as gone once it is not runnable.
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8") as stat_file:
                return stat_file.read().split(") ", 1)[1][0] != "Z"
        except OSError:
            return True
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel32.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
