"""Persistent SQLcl sessions for one deployment command (design: docs/superpowers/specs/2026-11-22-persistent-sqlcl-sessions-design.md).

A deployment used to start one SQLcl process per generated script. Against a remote database every start
costs about 6.5 s, most of it SQLcl's own connect-time settings replay. This module keeps a few long-lived
SQLcl processes per saved connection alias for the whole command and sends each script to one of them:

* Transport: SQLcl reads piped stdin to end-of-file before it runs anything, so scripts arrive through
  named pipes instead. A loop file lists K slot FIFOs (``@s0.sql`` ... ``@s{K-1}.sql``) and then the next
  loop file; SQLcl blocks opening each slot until a segment is written into it. SQLcl allows 20 nested
  scripts, so a process is retired after ``MAX_BATCHES`` loop files.
* Roles: one writer process runs ``phase="apply"`` scripts; reader processes run everything else. A check
  after an apply therefore never runs in the session that wrote, and it sees only committed data.
* Sessions are reused. Before every script except the first, one PL/SQL block rolls back, re-enables
  COMMIT IN PROCEDURE, resets CURRENT_SCHEMA, NLS, PL/SQL compiler settings and DBMS_METADATA transforms,
  and clears package state (DBMS_SESSION.RESET_PACKAGE). Any failure ends the process through WHENEVER,
  and the pool replaces it. SQLcl's own SET options are restored from a STORE SET baseline.
* A failure inside a script still ends its SQLcl process through ``WHENEVER ... EXIT``, exactly as with
  one launch per script; the pool starts a new process for the next script.

Only drivers written while the pool is active end with the pooled epilogue (sqlcl_session.driver_epilogue);
any other driver, a disabled pool (SQLCL_PERSISTENT_SESSIONS=0) or an unsupported platform runs one-shot.
"""
from __future__ import annotations

import contextlib
import errno
import json
import os
import queue
import re
import secrets
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from . import sqlcl_session
from .sqlcl_session import ALIAS_RE, SQLCL_ERROR_RE, SqlclError, bash_command, safe_rmtree

DEFAULT_BATCH_SIZE = 500
# SQLcl refuses a 21st nested script (SP2-0309). Loop file b is nested b+1 deep, its slot one more and the
# driver one more; drivers may include a few levels of their own (@@payload files), so stay well below 20.
MAX_BATCHES = 12
START_TIMEOUT_SECONDS = 120.0
RESET_OK_MARKER = "TEAM_POOL_RESET_OK"
BASELINE_MARKER = "TEAM_POOL_BASELINE:"
DDL_MARKER = "TEAM_POOL_DDL_BEGIN"
DDL_END_MARKER = "TEAM_POOL_DDL_END"
COMPILER_PARAMETERS = ("plsql_optimize_level", "plsql_code_type", "plsql_warnings", "plsql_ccflags", "plscope_settings")
_DEFINE_NAME = re.compile(r"(?im)^\s*DEF(?:INE)?\s+([A-Za-z][A-Za-z0-9_$#]*)\s*=|&&([A-Za-z][A-Za-z0-9_$#]*)")
_SETTING_VALUE = re.compile(r"\A[^\x00-\x1f\x7f]*\Z")
_BARE_VALUE = re.compile(r"\A[A-Za-z0-9_]+\Z")


# The pool runs only where named pipes exist (POSIX); look these up so the module imports everywhere.
_KILLPG = getattr(os, "killpg", None)
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)
_OPTIONAL_SIGNALS = tuple(value for value in (getattr(signal, "SIGHUP", None),) if value is not None)


def _kill_tree(process: subprocess.Popen) -> None:
    """Kill the SQLcl bridge and everything under it (its own process group where groups exist)."""
    if _KILLPG is not None:
        _KILLPG(process.pid, _SIGKILL)
    else:
        process.kill()


class PoolUnavailable(Exception):
    """The persistent session could not be started; the script runs with its own SQLcl instead."""


class _ProcessEnded(Exception):
    pass


@dataclass
class SessionBaseline:
    session_user: str
    edition: str
    nls: dict[str, str]
    compiler: dict[str, str] | None
    ddl: dict[str, str] | None = None  # SQLcl's own DDL settings (SHOW DDL), replayed on every connect


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def session_init_block(baseline_settings: Path) -> str:
    """First segment of a new process: store SQLcl's SET baseline, reset metadata transforms, print the baseline."""
    names = ",".join(_sql_literal(name) for name in COMPILER_PARAMETERS)
    return "\n".join((
        f'STORE SET "{baseline_settings.as_posix()}" REPLACE',
        "WHENEVER SQLERROR EXIT FAILURE ROLLBACK",
        "WHENEVER OSERROR EXIT FAILURE ROLLBACK",
        "SET SERVEROUTPUT ON SIZE UNLIMITED",
        "SET FEEDBACK OFF",
        "DECLARE",
        "  l_baseline JSON_OBJECT_T := JSON_OBJECT_T();",
        "  l_nls JSON_OBJECT_T := JSON_OBJECT_T();",
        "  l_compiler CLOB;",
        "BEGIN",
        "  l_baseline.put('session_user', SYS_CONTEXT('USERENV', 'SESSION_USER'));",
        "  l_baseline.put('edition', NVL(SYS_CONTEXT('USERENV', 'CURRENT_EDITION_NAME'), '<NONEDITIONED>'));",
        "  FOR r IN (SELECT parameter, value FROM nls_session_parameters) LOOP",
        "    l_nls.put(r.parameter, r.value);",
        "  END LOOP;",
        "  l_baseline.put('nls', l_nls);",
        "  BEGIN",
        "    EXECUTE IMMEDIATE 'SELECT JSON_OBJECTAGG(name VALUE NVL(value, '' '') RETURNING CLOB) FROM v$parameter "
        f"WHERE name IN ({names.replace(chr(39), chr(39) * 2)})' INTO l_compiler;",
        "    l_baseline.put('compiler', JSON_OBJECT_T(l_compiler));",
        "  EXCEPTION",
        "    WHEN OTHERS THEN",
        "      IF SQLCODE NOT IN (-942, -1031) THEN RAISE; END IF;",
        "      l_baseline.put_null('compiler');",
        "  END;",
        f"  DBMS_OUTPUT.PUT_LINE('{BASELINE_MARKER}' || l_baseline.to_string());",
        "END;",
        "/",
        "WHENEVER SQLERROR CONTINUE NONE",
        "WHENEVER OSERROR CONTINUE NONE",
        f"PROMPT {DDL_MARKER}",
        "SHOW DDL",
        f"PROMPT {DDL_END_MARKER}",
        f'@"{baseline_settings.as_posix()}"',
    ))


def parse_show_ddl(output: str) -> dict[str, str] | None:
    """SQLcl's DDL settings from SHOW DDL ("setting value" rows) between the init markers."""
    if DDL_MARKER not in output or DDL_END_MARKER not in output:
        return None
    section = output.split(DDL_MARKER, 1)[1].split(DDL_END_MARKER, 1)[0]
    settings: dict[str, str] = {}
    for line in section.splitlines():
        parts = line.split()
        if len(parts) == 2 and re.fullmatch(r"[a-z_]+", parts[0]) and parts[0] != "setting":
            settings[parts[0]] = parts[1]
    return settings or None


def _ddl_transform_statements(ddl: Mapping[str, str]) -> list[str]:
    """SQLcl applies each DDL setting as DBMS_METADATA.SET_TRANSFORM_PARAM on connect; do the same."""
    statements = ["  DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, 'DEFAULT');"]
    for name in sorted(ddl):
        value = ddl[name]
        if re.fullmatch(r"[a-z_]+", name) is None or re.fullmatch(r"[A-Za-z_]+", value) is None:
            raise PoolUnavailable(f"SQLcl DDL setting {name} cannot be restored safely")
        argument = {"ON": "TRUE", "OFF": "FALSE"}.get(value.upper(), _sql_literal(value))
        statements.append(
            f"  DBMS_METADATA.SET_TRANSFORM_PARAM(DBMS_METADATA.SESSION_TRANSFORM, {_sql_literal(name.upper())}, {argument});"
        )
    return statements


def reset_block(baseline: SessionBaseline, baseline_settings: Path, defines: set[str], *, reconnect_alias: str | None = None) -> str:
    """Segment run before a reused session takes the next script; any failure ends the process.

    One PL/SQL call (one round trip): DBMS_SESSION.RESET_PACKAGE is last because it discards package
    state only when the call ends. Success is shown by SQLcl's PROMPT after the block, not by DBMS_OUTPUT.
    """
    lines = ["WHENEVER SQLERROR EXIT FAILURE ROLLBACK", "WHENEVER OSERROR EXIT FAILURE ROLLBACK"]
    if reconnect_alias is not None:
        if ALIAS_RE.fullmatch(reconnect_alias) is None:
            raise SqlclError("configured SQLcl alias contains unsupported characters", baseline_settings.parent)
        lines.append(f"connect -name {reconnect_alias}")
    statements = [
        "  ROLLBACK;",
        "  EXECUTE IMMEDIATE 'ALTER SESSION ENABLE COMMIT IN PROCEDURE';",
        f"  IF NVL(SYS_CONTEXT('USERENV', 'CURRENT_EDITION_NAME'), '<NONEDITIONED>') <> {_sql_literal(baseline.edition)} THEN",
        "    RAISE_APPLICATION_ERROR(-20961, 'TEAM_POOL_RESET: session edition changed');",
        "  END IF;",
        f"  EXECUTE IMMEDIATE 'ALTER SESSION SET CURRENT_SCHEMA = \"{baseline.session_user.replace(chr(34), '')}\"';",
        *_ddl_transform_statements(baseline.ddl or {}),
    ]
    ordered_nls = sorted(baseline.nls, key=lambda name: (name not in {"NLS_LANGUAGE", "NLS_TERRITORY"}, name))
    for name in ordered_nls:
        statements.append(_alter_session(name, baseline.nls[name]))
    if baseline.compiler is not None and reconnect_alias is None:
        for name in COMPILER_PARAMETERS:
            if name in baseline.compiler:
                statements.append(_alter_session(name, baseline.compiler[name]))
    statements.append("  DBMS_SESSION.RESET_PACKAGE;")
    lines.extend(("BEGIN", *statements, "END;", "/"))
    lines.append(f'@"{baseline_settings.as_posix()}"')
    lines.extend(("CLEAR COLUMNS", "CLEAR BREAKS", "CLEAR COMPUTES"))
    lines.extend(f"UNDEFINE {name}" for name in sorted(defines))
    lines.extend(("WHENEVER SQLERROR CONTINUE NONE", "WHENEVER OSERROR CONTINUE NONE", f"PROMPT {RESET_OK_MARKER}"))
    return "\n".join(lines)


def _alter_session(name: str, value: str) -> str:
    if re.fullmatch(r"[A-Za-z_]+", name) is None or _SETTING_VALUE.fullmatch(value) is None:
        raise PoolUnavailable(f"session baseline value for {name} cannot be restored safely")
    value = value.strip()
    # Numbers and keywords (2, INTERPRETED, AMERICAN) are written bare; anything else as a quoted string.
    statement = f"ALTER SESSION SET {name} = {value if _BARE_VALUE.fullmatch(value) else _sql_literal(value)}"
    return f"  EXECUTE IMMEDIATE {_sql_literal(statement)};"


def driver_defines(text: str) -> set[str]:
    """Substitution variables a driver defines (DEFINE x = ... or &&x), to UNDEFINE before the next script."""
    return {match.group(1) or match.group(2) for match in _DEFINE_NAME.finditer(text)}


class SqlclProcess:
    """One long-lived SQLcl reading segments from slot FIFOs (POSIX)."""

    def __init__(self, alias: str, role: str, run_root: Path, environment: Mapping[str, str], *, batch_size: int = DEFAULT_BATCH_SIZE) -> None:
        if ALIAS_RE.fullmatch(alias) is None:
            raise PoolUnavailable("configured SQLcl alias contains unsupported characters")
        self.alias = alias
        self.role = role
        self.environment = dict(environment)
        self.batch_size = batch_size
        self.run_dir = Path(tempfile.mkdtemp(prefix=f"{role}-", dir=run_root))
        self.run_dir.chmod(0o700)
        self.baseline_settings = self.run_dir / "client-baseline.sql"
        self.baseline: SessionBaseline | None = None
        self.next_slot = 0
        self.scripts = 0
        self.connects = 0
        self.defines: set[str] = set()
        self.process: subprocess.Popen | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self.lock = threading.Lock()

    # -- transport -------------------------------------------------------------------------------------
    def _slot(self, number: int) -> Path:
        return self.run_dir / f"s{number}.sql"

    def _loop(self, batch: int) -> Path:
        return self.run_dir / f"loop{batch}.sql"

    def _write_loop(self, batch: int) -> None:
        first = batch * self.batch_size
        text = "".join(f'@"{self._slot(first + offset).as_posix()}"\n' for offset in range(self.batch_size))
        text += f'@"{self._loop(batch + 1).as_posix()}"\n'
        descriptor = os.open(self._loop(batch), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    @property
    def batches(self) -> int:
        return self.next_slot // self.batch_size

    def retired(self) -> bool:
        return self.batches >= MAX_BATCHES or not self.alive()

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self, timeout_seconds: float = START_TIMEOUT_SECONDS) -> None:
        if os.name == "nt":
            raise PoolUnavailable("windows-pipes")
        self._write_loop(0)
        self._write_loop(1)
        os.mkfifo(self._slot(0), 0o600)
        bridge = Path(__file__).with_name("sqlcl_session.sh").resolve(strict=True)
        try:
            self.process = subprocess.Popen(
                [bash_command(), bridge.as_posix(), self.run_dir.as_posix(), self.alias, self._loop(0).as_posix()],
                cwd=self.run_dir, env=self.environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
            )
        except OSError as error:
            raise PoolUnavailable(f"SQLcl could not be started: {error}") from error
        threading.Thread(target=self._pump, name=f"sqlcl-pool-{self.role}", daemon=True).start()
        self.connects = 1
        returncode, output = self.run_segment(session_init_block(self.baseline_settings), timeout_seconds=timeout_seconds)
        baseline = None
        for line in output.splitlines():
            if line.startswith(BASELINE_MARKER):
                try:
                    document = json.loads(line[len(BASELINE_MARKER):])
                    baseline = SessionBaseline(
                        str(document["session_user"]), str(document["edition"]),
                        {str(key): str(value) for key, value in document["nls"].items()},
                        None if document.get("compiler") is None
                        else {str(key): str(value) for key, value in document["compiler"].items()},
                    )
                except (KeyError, TypeError, ValueError, AttributeError):
                    baseline = None
        if baseline is not None:
            baseline.ddl = parse_show_ddl(output)
        if (
            returncode != 0 or baseline is None or baseline.ddl is None
            or SQLCL_ERROR_RE.search(output) or not self.baseline_settings.is_file()
        ):
            self.kill()
            raise PoolUnavailable("persistent SQLcl session did not start; output: " + output[-2000:])
        self.baseline = baseline

    def _pump(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            for raw in iter(self.process.stdout.readline, b""):
                self._lines.put(raw.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(None)
            with contextlib.suppress(OSError):
                self.process.stdout.close()

    def _send(self, text: str, deadline: float) -> None:
        """Write one segment into the current slot; create the next slot (and loop file) first."""
        current = self._slot(self.next_slot)
        os.mkfifo(self._slot(self.next_slot + 1), 0o600)
        if (self.next_slot + 1) % self.batch_size == 0:
            self._write_loop((self.next_slot + 1) // self.batch_size + 1)
        while True:
            try:
                descriptor = os.open(current, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError as error:
                if error.errno != errno.ENXIO:
                    raise
            if not self.alive():
                raise _ProcessEnded()
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("sqlcl-pool", 0)
            time.sleep(0.005)
        try:
            os.set_blocking(descriptor, True)
            data = text.encode("utf-8")
            while data:
                written = os.write(descriptor, data)
                data = data[written:]
        finally:
            os.close(descriptor)
        with contextlib.suppress(OSError):
            current.unlink()
        self.next_slot += 1

    def run_segment(
        self,
        body: str,
        *,
        timeout_seconds: float,
        output_limit_bytes: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> tuple[int, str]:
        """Run one segment. Returns (0, output) at the sentinel, or (exit status, output) if SQLcl ended."""
        sentinel = f"__TEAM_SQLCL_DONE_{secrets.token_hex(16)}__"
        deadline = time.monotonic() + timeout_seconds
        collected: list[str] = []
        size = 0
        try:
            self._send(f"{body}\nPROMPT {sentinel}\n", deadline)
        except _ProcessEnded:
            return self._ended(collected)
        except subprocess.TimeoutExpired:
            self.kill()
            raise subprocess.TimeoutExpired("sqlcl-pool", timeout_seconds, output="".join(collected)) from None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                self.kill()
                raise sqlcl_session._BridgeCancelled("".join(collected))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.kill()
                raise subprocess.TimeoutExpired("sqlcl-pool", timeout_seconds, output="".join(collected))
            try:
                line = self._lines.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                continue
            if line is None:
                return self._ended(collected)
            if line.strip() == sentinel:
                return 0, "".join(collected)
            collected.append(line)
            size += len(line.encode("utf-8"))
            if output_limit_bytes is not None and size > output_limit_bytes:
                self.kill()
                raise sqlcl_session._BridgeOutputLimitExceeded("".join(collected))

    def _ended(self, collected: list[str]) -> tuple[int, str]:
        while True:
            try:
                line = self._lines.get(timeout=5)
            except queue.Empty:
                break
            if line is None:
                break
            collected.append(line)
        returncode = self.process.wait() if self.process is not None else 1
        return (returncode if returncode != 0 else 1), "".join(collected)

    def reset(self, timeout_seconds: float) -> bool:
        """Return this reused session to its baseline; False means the process must be replaced."""
        if self.baseline is None or not self.alive():
            return False
        reconnect = self.alias if self.role == "writer" and self.baseline.compiler is None else None
        try:
            text = reset_block(self.baseline, self.baseline_settings, self.defines, reconnect_alias=reconnect)
            returncode, output = self.run_segment(text, timeout_seconds=timeout_seconds)
        except (PoolUnavailable, subprocess.TimeoutExpired):
            self.kill()
            return False
        if returncode != 0 or RESET_OK_MARKER not in output.split() or SQLCL_ERROR_RE.search(output):
            self.kill()
            return False
        if reconnect is not None:
            self.connects += 1
        self.defines.clear()
        return True

    def close(self, grace_seconds: float = 5.0) -> None:
        if self.alive():
            with contextlib.suppress(Exception):
                self._send("EXIT ROLLBACK\n", time.monotonic() + grace_seconds)
            with contextlib.suppress(subprocess.TimeoutExpired):
                assert self.process is not None
                self.process.wait(timeout=grace_seconds)
        self.kill()
        safe_rmtree(self.run_dir)

    def kill(self) -> None:
        if self.process is not None and self.process.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                _kill_tree(self.process)
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.process.wait(timeout=10)


class SessionPool:
    """Long-lived SQLcl processes per saved alias: one writer and up to ``max_readers`` readers."""

    def __init__(self, *, max_readers: int = 2, batch_size: int = DEFAULT_BATCH_SIZE) -> None:
        self.max_readers = max(1, max_readers)
        self.batch_size = batch_size
        self.root = Path(tempfile.mkdtemp(prefix="team-sqlcl-pool-"))
        self.root.chmod(0o700)
        self._processes: dict[tuple[str, str], list[SqlclProcess]] = {}
        self._condition = threading.Condition()
        self.processes_started = 0
        self.scripts = 0
        self.fallback_reason: str | None = None
        self.closed = False
        self._retired_connects = 0

    def stats(self) -> dict:
        return {
            "mode": "pooled" if self.fallback_reason is None else "one-shot",
            "fallbackReason": self.fallback_reason,
            "processesStarted": self.processes_started,
            "databaseSessions": sum(process.connects for group in self._processes.values() for process in group)
            + self._retired_connects,
            "scripts": self.scripts,
        }

    @contextlib.contextmanager
    def _acquire(self, alias: str, role: str, environment: Mapping[str, str]) -> Iterator[SqlclProcess]:
        key = (alias, role)
        limit = 1 if role == "writer" else self.max_readers
        with self._condition:
            while True:
                group = self._processes.setdefault(key, [])
                for process in list(group):
                    if process.retired() and not process.lock.locked():
                        group.remove(process)
                        self._retired_connects += process.connects
                        process.close()
                idle = next((process for process in group if not process.lock.locked()), None)
                if idle is not None:
                    idle.lock.acquire()
                    chosen, fresh = idle, False
                    break
                if len(group) < limit:
                    chosen = SqlclProcess(alias, role, self.root, environment, batch_size=self.batch_size)
                    chosen.lock.acquire()
                    group.append(chosen)
                    fresh = True
                    break
                self._condition.wait()
        try:
            if fresh:
                try:
                    chosen.start()
                    self.processes_started += 1
                except BaseException:
                    with self._condition:
                        group.remove(chosen)
                    chosen.kill()
                    safe_rmtree(chosen.run_dir)
                    raise
            yield chosen
        finally:
            chosen.lock.release()
            with self._condition:
                self._condition.notify_all()

    def run_driver(
        self,
        connection: str,
        driver_path: Path,
        run_dir: Path,
        *,
        phase: str | None,
        timeout_seconds: float,
        environment: Mapping[str, str],
        output_limit_bytes: int | None = None,
        cancel_event: threading.Event | None = None,
    ) -> subprocess.CompletedProcess:
        """Run one pooled driver; raise PoolUnavailable before anything of it ran if no session is available."""
        if self.fallback_reason is not None or self.closed:
            raise PoolUnavailable(self.fallback_reason or "pool closed")
        role = "writer" if phase == "apply" else "reader"
        started = time.monotonic()
        attempts = 0
        while True:
            attempts += 1
            try:
                with self._acquire(connection, role, environment) as process:
                    if process.scripts > 0 and not process.reset(timeout_seconds):
                        replace = True
                    else:
                        replace = False
                        reset_seconds = round(time.monotonic() - started, 3)
                        text = driver_path.read_text(encoding="utf-8", errors="replace")
                        segment = f'cd "{run_dir.as_posix()}"\n@"{driver_path.as_posix()}"'
                        process.scripts += 1
                        self.scripts += 1
                        slot = process.next_slot
                        process.defines |= driver_defines(text)
                        returncode, output = process.run_segment(
                            segment, timeout_seconds=max(1.0, timeout_seconds - (time.monotonic() - started)),
                            output_limit_bytes=output_limit_bytes, cancel_event=cancel_event,
                        )
                        _write_evidence(run_dir, {
                            "mode": "pooled", "alias": connection, "role": role,
                            "pid": process.process.pid if process.process else None, "slot": slot,
                            "processScripts": process.scripts, "resetSeconds": reset_seconds,
                            "scriptSeconds": round(time.monotonic() - started - reset_seconds, 3),
                            "returncode": returncode,
                        })
                        return subprocess.CompletedProcess(["sqlcl-pool", connection], returncode, output, None)
            except PoolUnavailable as error:
                self.fallback_reason = str(error).splitlines()[0][:200]
                raise
            if replace and attempts >= 3:
                raise PoolUnavailable("persistent SQLcl session could not be reset")

    def close(self) -> None:
        self.closed = True
        with self._condition:
            groups = list(self._processes.values())
            self._processes.clear()
        for group in groups:
            for process in group:
                self._retired_connects += process.connects
                process.close()
        safe_rmtree(self.root)


def _write_evidence(run_dir: Path, record: Mapping) -> None:
    path = Path(run_dir) / "sqlcl-session.json"
    with contextlib.suppress(OSError):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, sort_keys=True)
            handle.write("\n")


def _enabled(environment: Mapping[str, str]) -> tuple[bool, str | None]:
    if str(environment.get("SQLCL_PERSISTENT_SESSIONS", "1")).strip() == "0":
        return False, "disabled by SQLCL_PERSISTENT_SESSIONS=0"
    if os.name == "nt":
        return False, "windows-pipes"
    return True, None


def with_deployment_sessions(function):
    """Decorate a command entry point so one pool serves the whole command (os.environ decides)."""
    import functools

    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with deployment_sessions():
            return function(*args, **kwargs)

    return wrapper


@contextlib.contextmanager
def deployment_sessions(environment: Mapping[str, str] | None = None) -> Iterator[SessionPool | None]:
    """Keep persistent SQLcl sessions for the duration of one deployment command.

    A nested call reuses the outer pool. With SQLCL_PERSISTENT_SESSIONS=0, or where named pipes are not
    available, it yields None and every script keeps its own SQLcl launch.
    """
    env = os.environ if environment is None else environment
    outer = sqlcl_session.active_pool()
    if outer is not None:
        yield outer  # type: ignore[misc]
        return
    enabled, _reason = _enabled(env)
    if not enabled:
        yield None
        return
    try:
        max_readers = int(str(env.get("SQLCL_POOL_MAX_READERS", "2")).strip() or "2")
    except ValueError:
        max_readers = 2
    pool = SessionPool(max_readers=max_readers)
    previous_handlers: dict[int, object] = {}

    def terminate(signum, frame):
        pool.close()
        previous = previous_handlers.get(signum)
        if callable(previous):
            previous(signum, frame)
        else:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, *_OPTIONAL_SIGNALS):
            previous = signal.getsignal(signum)
            if previous == signal.SIG_IGN:
                continue
            previous_handlers[signum] = previous
            signal.signal(signum, terminate)
    sqlcl_session._set_active_pool(pool)
    try:
        yield pool
    finally:
        sqlcl_session._set_active_pool(None)
        pool.close()
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)  # type: ignore[arg-type]
