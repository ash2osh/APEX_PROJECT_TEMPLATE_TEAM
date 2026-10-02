import importlib
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

if os.name == "nt":
    raise unittest.SkipTest("tools/chrome_mcp_daemon.py is POSIX-only: it imports fcntl and relies on Unix sockets and process groups")

from tools import chrome_mcp_daemon  # noqa: E402
from tools.chrome_mcp_daemon import ChromeMcpDaemon, prepare_socket_path  # noqa: E402
from tools.chrome_devtools_client import ChromeDevToolsClient, ensure_daemon_running  # noqa: E402


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

    def test_default_socket_uses_xdg_runtime_dir(self):
        with tempfile.TemporaryDirectory() as runtime:
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}, clear=True):
                expected = Path(runtime) / "chrome-mcp/chrome-mcp.sock"
                self.assertEqual(chrome_mcp_daemon.default_socket_path(), expected)

    def test_default_socket_uses_uid_fallback_without_xdg_runtime_dir(self):
        with patch.dict(os.environ, {}, clear=True):
            expected = Path(f"/tmp/chrome-mcp-{os.getuid()}/chrome-mcp.sock")
            self.assertEqual(chrome_mcp_daemon.default_socket_path(), expected)


class ChromeMcpDaemonTests(unittest.TestCase):
    def test_socket_directory_is_private(self):
        with tempfile.TemporaryDirectory() as temp:
            path = prepare_socket_path(Path(temp) / "service/chrome.sock")
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_existing_public_socket_parent_is_rejected_without_chmod(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / "public"
            parent.mkdir(mode=0o755)
            os.chmod(parent, 0o755)
            with self.assertRaises(RuntimeError):
                prepare_socket_path(parent / "chrome.sock")
            self.assertEqual(parent.stat().st_mode & 0o777, 0o755)

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

    def test_existing_regular_file_is_never_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "service/chrome.sock"
            target.parent.mkdir()
            target.write_text("keep", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                prepare_socket_path(target)
            self.assertEqual(target.read_text(encoding="utf-8"), "keep")

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


class ChromeMcpDaemonLifecycleTests(unittest.TestCase):
    FAKE = str(Path(__file__).resolve().parent / "fixtures/mcp/fake_mcp_server.py")

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


if __name__ == "__main__":
    unittest.main()
