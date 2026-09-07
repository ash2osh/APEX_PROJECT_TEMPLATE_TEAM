"""Single-process SQLcl boundary used by all database-facing workflows."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import uuid
from typing import Any

from .config import Target


class SqlclError(RuntimeError):
    """Raised when SQLcl or the verified session contract fails."""


@dataclass(frozen=True)
class SqlResult:
    identity: dict[str, str]
    completion: dict[str, str]
    result_manifest: dict[str, Any]
    log_path: Path
    generated_driver: Path
    stdout: str
    stderr: str
    argv: tuple[str, ...]
    exit_code: int


_UNSAFE_ARG_RE = re.compile(r"[\x00-\x1f\x7f;&|<>`$]")
_OUTPUT_ERROR_RE = re.compile(
    r"(?:ORA-\d+|SP2-\d+|DPI-\d+|SQLcl\s+error|Exception in thread|startup\s+exception|Traceback \(most recent call last\)|java\.)",
    re.IGNORECASE,
)
_IDENTITY_KEYS = ("SESSION_USER", "CURRENT_SCHEMA", "DB_NAME", "SERVICE", "INSTANCE_ID")


def _assert_safe_argument(name: str, value: str) -> None:
    if not value or _UNSAFE_ARG_RE.search(value):
        raise SqlclError(f"{name} contains unsupported shell/control characters")


def _mask_sql_comments_and_literals(text: str) -> str:
    pattern = re.compile(r"--[^\n]*|/\*.*?\*/|'(?:''|[^'])*'", re.DOTALL)
    return pattern.sub(
        lambda match: "".join("\n" if char == "\n" else " " for char in match.group(0)),
        text,
    )


def _assert_production_read_only(driver_text: str) -> None:
    masked = _mask_sql_comments_and_literals(driver_text)
    forbidden = re.search(
        r"\b(?:INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|COMMIT|"
        r"ROLLBACK|GRANT|REVOKE|BEGIN|DECLARE|EXEC|EXECUTE)\b",
        masked,
        re.IGNORECASE,
    )
    if forbidden:
        raise SqlclError(
            "production read-only SQLcl operation contains a mutation/control statement: "
            + forbidden.group(0)
        )


def _sql_marker_query() -> str:
    # INSTANCE_NAME is the stable database/container identity used by the
    # configured Target. The adapter verifies the configured expected value;
    # it never treats a connection alias as identity evidence.
    return (
        "SELECT 'TEAM_IDENTITY|' || "
        "'SESSION_USER=' || REPLACE(SYS_CONTEXT('USERENV','SESSION_USER'),'|','/') || "
        "'|CURRENT_SCHEMA=' || REPLACE(SYS_CONTEXT('USERENV','CURRENT_SCHEMA'),'|','/') || "
        "'|DB_NAME=' || REPLACE(SYS_CONTEXT('USERENV','DB_NAME'),'|','/') || "
        "'|SERVICE=' || REPLACE(SYS_CONTEXT('USERENV','SERVICE_NAME'),'|','/') || "
        "'|INSTANCE_ID=' || REPLACE(SYS_CONTEXT('USERENV','INSTANCE_NAME'),'|','/') "
        "FROM DUAL;"
    )


def _driver_text(payload_name: str, operation: str) -> str:
    return (
        "SET DEFINE OFF\n"
        "SET ENCODING UTF-8\n"
        "SET HEADING OFF\n"
        "SET FEEDBACK OFF\n"
        "SET ECHO OFF\n"
        "SET VERIFY OFF\n"
        "WHENEVER SQLERROR EXIT SQL.SQLCODE\n"
        "WHENEVER OSERROR EXIT FAILURE\n"
        + _sql_marker_query()
        + "\n"
        + _sql_marker_query()
        + "\n"
        + f"@{payload_name}\n"
        + f"SELECT 'TEAM_COMPLETION|operation={operation}' FROM DUAL;\n"
        + "EXIT\n"
    )


def _redact_output(text: str) -> str:
    text = re.sub(r"(?i)(password|passphrase)(\s*[:=]\s*)\S+", r"\1\2<redacted>", text)
    text = re.sub(r"(?i)(oracle\.jdbc\.password=)\S+", r"\1<redacted>", text)
    return text


def _parse_identity_lines(stdout: str) -> list[dict[str, str]]:
    observations: list[dict[str, str]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip().rstrip("\r")
        if not line.startswith("TEAM_IDENTITY|"):
            continue
        fields: dict[str, str] = {}
        for part in line.split("|")[1:]:
            if "=" not in part:
                raise SqlclError("malformed TEAM_IDENTITY completion output")
            key, value = part.split("=", 1)
            if key not in _IDENTITY_KEYS or key in fields or not value:
                raise SqlclError("malformed TEAM_IDENTITY completion output")
            fields[key] = value
        if tuple(fields) != _IDENTITY_KEYS:
            raise SqlclError("incomplete TEAM_IDENTITY completion output")
        observations.append(fields)
    return observations


def _parse_completion(stdout: str) -> dict[str, str]:
    completions: list[dict[str, str]] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip().rstrip("\r")
        if not line.startswith("TEAM_COMPLETION|"):
            continue
        fields: dict[str, str] = {}
        for part in line.split("|")[1:]:
            if "=" not in part:
                raise SqlclError("malformed TEAM_COMPLETION output")
            key, value = part.split("=", 1)
            if key in fields or not key or not value:
                raise SqlclError("malformed TEAM_COMPLETION output")
            fields[key] = value
        completions.append(fields)
    if len(completions) != 1:
        raise SqlclError("missing or duplicate SQLcl completion record")
    return completions[0]


def _resolve_executable(executable: str | Path | None) -> str:
    requested = str(executable or os.environ.get("TEAM_SQLCL_EXECUTABLE", "sql"))
    _assert_safe_argument("SQLcl executable", requested)
    resolved = shutil.which(requested) if not Path(requested).is_absolute() else requested
    if not resolved or not Path(resolved).exists():
        raise SqlclError(f"SQLcl executable was not found: {requested}")
    return resolved


def _build_argv(executable: str, target: Target, generated_driver: Path) -> list[str]:
    _assert_safe_argument("SQLcl connection", target.connection)
    args = ["-S", "-noupdates", "-name", target.connection, f"@{generated_driver}"]
    if os.name == "nt" and Path(executable).suffix.casefold() in {".bat", ".cmd"}:
        # cmd.exe is required for batch launchers; the payload remains an argv
        # list and shell=True is never used.
        return ["cmd.exe", "/d", "/s", "/c", executable, *args]
    return [executable, *args]


def _identity_digest(identity: dict[str, str]) -> str:
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def run_sqlcl(
    target: Target,
    operation: str,
    driver: str | Path,
    work: str | Path,
    *,
    executable: str | Path | None = None,
    timeout: float = 120.0,
) -> SqlResult:
    """Run one verified SQLcl process with a regular empty stdin file."""
    if operation not in {"read", "write"}:
        raise SqlclError(f"unsupported SQLcl operation: {operation}")
    if target.environment == "production" and operation != "read":
        raise SqlclError("production database operations are always read-only")

    driver_path = Path(driver)
    if not driver_path.is_file() or driver_path.is_symlink():
        raise SqlclError(f"SQLcl driver is not a regular file: {driver_path}")
    work_path = Path(work)
    work_path.mkdir(parents=True, exist_ok=True)
    if work_path.is_symlink():
        raise SqlclError(f"SQLcl work directory must not be a symbolic link: {work_path}")

    try:
        payload = driver_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SqlclError(f"could not read SQLcl driver: {driver_path}") from exc
    if target.environment == "production" and operation == "read":
        _assert_production_read_only(payload)

    resolved_executable = _resolve_executable(executable)
    run_id = uuid.uuid4().hex
    generated_driver = work_path / f".team-driver-{run_id}.sql"
    payload_copy = work_path / f".team-payload-{run_id}.sql"
    stdin_path = work_path / f".team-stdin-{run_id}.empty"
    log_path = work_path / f".team-sqlcl-{run_id}.log"
    payload_copy.write_text(payload, encoding="utf-8", newline="")
    generated_driver.write_text(_driver_text(payload_copy.name, operation), encoding="utf-8", newline="")
    stdin_path.touch()
    if not stat.S_ISREG(stdin_path.stat().st_mode):
        raise SqlclError("SQLcl stdin is not a regular file")

    argv = _build_argv(resolved_executable, target, generated_driver)
    try:
        with stdin_path.open("rb") as empty:
            completed = subprocess.run(
                argv,
                stdin=empty,
                cwd=work_path,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
    except subprocess.TimeoutExpired as exc:
        raise SqlclError("SQLcl timed out; target state is unknown") from exc
    except OSError as exc:
        raise SqlclError(f"could not start SQLcl: {exc}") from exc

    try:
        stdout = completed.stdout.decode("utf-8")
        stderr = completed.stderr.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SqlclError("SQLcl output was not valid UTF-8") from exc
    stdout = stdout.replace("\r\n", "\n").replace("\r", "\n")
    stderr = stderr.replace("\r\n", "\n").replace("\r", "\n")
    safe_log = _redact_output(stdout + ("\n" + stderr if stderr else ""))
    log_path.write_text(safe_log, encoding="utf-8", newline="")

    if completed.returncode != 0:
        raise SqlclError(f"SQLcl failed with exit code {completed.returncode}; see {log_path}")
    if _OUTPUT_ERROR_RE.search(stdout) or _OUTPUT_ERROR_RE.search(stderr):
        match = _OUTPUT_ERROR_RE.search(stdout) or _OUTPUT_ERROR_RE.search(stderr)
        raise SqlclError(f"SQLcl reported an error ({match.group(0)}); see {log_path}")

    observations = _parse_identity_lines(stdout)
    if len(observations) < 2:
        raise SqlclError("SQLcl did not provide two identity observations")
    first = observations[0]
    if any(observation != first for observation in observations[1:]):
        raise SqlclError("SQLcl identity changed between observations")
    expected = {
        "SESSION_USER": target.session_user,
        "CURRENT_SCHEMA": target.current_schema,
        "DB_NAME": target.db_name,
        "SERVICE": target.service,
        "INSTANCE_ID": target.instance_id,
    }
    for key, value in expected.items():
        if first.get(key) != value:
            raise SqlclError(
                f"SQLcl identity mismatch for {key}: expected {value!r}, found {first.get(key)!r}"
            )

    completion = _parse_completion(stdout)
    if completion.get("operation") != operation:
        raise SqlclError("SQLcl completion operation does not match requested operation")
    result_manifest = {
        "version": 1,
        "status": "success",
        "operation": operation,
        "identity_digest": _identity_digest(first),
        "completion": completion,
    }
    return SqlResult(
        identity=first,
        completion=completion,
        result_manifest=result_manifest,
        log_path=log_path,
        generated_driver=generated_driver,
        stdout=stdout,
        stderr=stderr,
        argv=tuple(argv),
        exit_code=completed.returncode,
    )
