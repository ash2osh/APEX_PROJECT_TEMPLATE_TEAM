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
from .sql_text import BLOCK_START_RE, mask_sql, statement_starts


class SqlclError(RuntimeError):
    """Raised when SQLcl or the verified session contract fails."""


class SqlclUnknownResult(SqlclError):
    """Raised when a write may have completed but its result was not observed."""


def result_is_unknown(exc: BaseException) -> bool:
    """Return true when an exception chain indicates lost acknowledgement."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, SqlclUnknownResult):
            return True
        current = current.__cause__ or current.__context__
    return False


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
    r"(?:ORA-\d+|SP2-\d+|DPI-\d+|SQLcl\s+error|Option\s+not\s+recognized|Unexpected\s+token|"
    r"Expected\s+a\s+subcommand|Error\s+starting\s+at\s+line|Exception in thread|startup\s+exception|"
    r"Traceback \(most recent call last\)|java\.)",
    re.IGNORECASE,
)
_IDENTITY_KEYS = ("SESSION_USER", "CURRENT_SCHEMA", "DB_NAME", "SERVICE", "INSTANCE_ID")


def _assert_safe_argument(name: str, value: str) -> None:
    if not value or _UNSAFE_ARG_RE.search(value):
        raise SqlclError(f"{name} contains unsupported shell/control characters")


# A production read may only issue queries and the display settings the driver
# needs. Anything else -- SQL the allowlist does not name, or a SQLcl client
# command such as HOST, SCRIPT, @, SPOOL or CONNECT -- is refused. An allowlist
# fails closed on a SQLcl feature that does not exist yet; a denylist does not.
_PRODUCTION_READ_ALLOWED_RE = re.compile(r"^(?:SELECT|WITH)\b", re.IGNORECASE)
_PRODUCTION_READ_SETTINGS_RE = re.compile(
    r"^SET\s+(?:HEADING|FEEDBACK|LINESIZE|PAGESIZE|LONG|ECHO|VERIFY|DEFINE|ENCODING|"
    r"TERMOUT|TRIMSPOOL|SQLBLANKLINES|MARKUP|SERVEROUTPUT)\b",
    re.IGNORECASE,
)
_PRODUCTION_READ_DIRECTIVE_RE = re.compile(r"^(?:WHENEVER\s+(?:SQLERROR|OSERROR)\b|EXIT\b)", re.IGNORECASE)

DEFAULT_TIMEOUT_SECONDS = 120.0
# A full APEX application export or import is not comparable to a metadata
# query. Timing one out marks the shared application uncertain, which stops the
# whole team until a named recovery owner reviews evidence, so the budget has to
# fit a real application over a real network.
APEX_TIMEOUT_SECONDS = 1800.0


def _resolve_timeout(timeout: float | None) -> float:
    if timeout is not None:
        return float(timeout)
    raw = os.environ.get("TEAM_SQLCL_TIMEOUT")
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError as exc:
        raise SqlclError("TEAM_SQLCL_TIMEOUT must be a positive number of seconds") from exc
    if value <= 0:
        raise SqlclError("TEAM_SQLCL_TIMEOUT must be a positive number of seconds")
    return value


# DBMS_METADATA session transforms change how a definition is rendered for this
# session and touch no data. schema_inventory.sql needs them, and a production
# read of the schema is exactly the operation this guard exists to permit. The
# allowance is deliberately line-oriented and exhaustive: one call per line, and
# any other line inside the block leaves the whole block in place to be refused.
_PRODUCTION_READ_SETUP_LINE_RE = re.compile(
    r"^(?:BEGIN|END;?|EXCEPTION|WHEN\s+OTHERS\s+THEN|RAISE;|"
    r"DBMS_METADATA\.SET_TRANSFORM_PARAM\s*\(.*\);)$",
    re.IGNORECASE,
)

_PRODUCTION_READ_INVENTORY_REQUIRED = (
    "DBMS_METADATA.GET_DDL",
    "DBMS_LOB.SUBSTR",
    "DBMS_OUTPUT.PUT_LINE",
    "UTL_ENCODE.BASE64_ENCODE",
    "ALL_OBJECTS",
    "ALL_CONSTRAINTS",
    "ALL_TAB_PRIVS",
    "ALL_COL_PRIVS",
)
# Besides DML/DDL keywords, refuse the packages that run SQL or leave the
# session without naming a write keyword: dynamic SQL, jobs, files, network,
# pipes and autonomous transactions.
_PRODUCTION_READ_WRITE_RE = re.compile(
    r"\b(?:EXECUTE\s+IMMEDIATE|INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|"
    r"GRANT|REVOKE|COMMIT|ROLLBACK|LOCK|AUTONOMOUS_TRANSACTION|"
    r"DBMS_SQL|DBMS_JOB|DBMS_SCHEDULER|DBMS_PIPE|DBMS_AQ|UTL_FILE|UTL_HTTP|UTL_TCP|UTL_SMTP|UTL_MAIL)\b",
    re.IGNORECASE,
)


def _blank_metadata_setup_blocks(masked: str) -> str:
    """Blank PL/SQL blocks that only configure DBMS_METADATA session transforms.

    Offsets and line count are preserved so the refusal messages produced by the
    statement walk still name the right line. A block with even one line the
    allowance does not name is left untouched, so the walk sees its ``BEGIN``
    and refuses it.
    """
    lines = masked.splitlines(keepends=True)
    output = list(lines)
    index = 0
    total = len(lines)
    while index < total:
        if not BLOCK_START_RE.match(lines[index].strip()):
            index += 1
            continue
        end = index
        while end < total and lines[end].strip() != "/":
            end += 1
        body = [line.strip() for line in lines[index:end] if line.strip()]
        if body and all(_PRODUCTION_READ_SETUP_LINE_RE.match(item) for item in body):
            for position in range(index, min(end + 1, total)):
                output[position] = "\n" if lines[position].endswith("\n") else ""
        index = end + 1
    return "".join(output)


def _blank_readonly_inventory_blocks(masked: str) -> str:
    """Blank only the dictionary-to-DBMS_OUTPUT inventory block."""

    lines = masked.splitlines(keepends=True)
    output = list(lines)
    index = 0
    while index < len(lines):
        if lines[index].strip().upper() != "DECLARE":
            index += 1
            continue
        end = index
        while end < len(lines) and lines[end].strip() != "/":
            end += 1
        body = "".join(lines[index:end]).upper()
        if (
            all(token in body for token in _PRODUCTION_READ_INVENTORY_REQUIRED)
            and not _PRODUCTION_READ_WRITE_RE.search(body)
        ):
            for position in range(index, min(end + 1, len(lines))):
                output[position] = "\n" if lines[position].endswith("\n") else ""
        index = end + 1
    return "".join(output)


def _assert_production_read_only(driver_text: str) -> None:
    masked, terminated = mask_sql(driver_text)
    if not terminated:
        raise SqlclError(
            "production read-only SQLcl operation has an unterminated comment or literal"
        )
    masked = _blank_metadata_setup_blocks(masked)
    masked = _blank_readonly_inventory_blocks(masked)
    for number, statement in statement_starts(masked):
        if _PRODUCTION_READ_ALLOWED_RE.match(statement):
            continue
        if _PRODUCTION_READ_SETTINGS_RE.match(statement):
            continue
        if _PRODUCTION_READ_DIRECTIVE_RE.match(statement):
            continue
        raise SqlclError(
            "production read-only SQLcl operation contains a statement that is not a "
            f"query or a display setting (line {number})"
        )


IDENTITY_GUARD_CODE = 20901
# A payload may raise any code in the -20000..-20999 range itself, so the guard
# is recognised by its code together with this message, never by the code alone.
IDENTITY_GUARD_MESSAGE = "TEAM identity guard: session does not match the expected target; payload not run"

# INSTANCE_NAME alone is not unique across cloned Docker/Free databases.
# Pair it with the verified database server host so independent
# containers cannot share a physical application lock by accident.
_IDENTITY_EXPRESSION = (
    "'SESSION_USER=' || REPLACE(SYS_CONTEXT('USERENV','SESSION_USER'),'|','/') || "
    "'|CURRENT_SCHEMA=' || REPLACE(SYS_CONTEXT('USERENV','CURRENT_SCHEMA'),'|','/') || "
    "'|DB_NAME=' || REPLACE(SYS_CONTEXT('USERENV','DB_NAME'),'|','/') || "
    "'|SERVICE=' || REPLACE(SYS_CONTEXT('USERENV','SERVICE_NAME'),'|','/') || "
    "'|INSTANCE_ID=' || REPLACE(SYS_CONTEXT('USERENV','INSTANCE_NAME'),'|','/') || '@' || "
    "REPLACE(SYS_CONTEXT('USERENV','SERVER_HOST'),'|','/')"
)


def _sql_marker_query() -> str:
    return "SELECT 'TEAM_IDENTITY|' || " + _IDENTITY_EXPRESSION + " FROM DUAL;"


def _sql_literal(value: str) -> str:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise SqlclError("expected target identity values must be non-empty printable text")
    return "'" + value.replace("'", "''") + "'"


def _identity_guard_block(target: Target) -> str:
    """Refuse inside the SQLcl session before the payload runs.

    The Python comparison of the TEAM_IDENTITY observations only happens after
    SQLcl exits, which is after a write payload has already executed. This
    block stops the session on the database side first, so a saved connection
    that resolves to the wrong user, schema or database never runs the payload.
    """
    expected = (
        f"SESSION_USER={target.session_user}"
        f"|CURRENT_SCHEMA={target.current_schema}"
        f"|DB_NAME={target.db_name}"
        f"|SERVICE={target.service}"
        f"|INSTANCE_ID={target.instance_id}"
    )
    return (
        "BEGIN\n"
        f"  IF {_IDENTITY_EXPRESSION} <> {_sql_literal(expected)} THEN\n"
        f"    RAISE_APPLICATION_ERROR(-{IDENTITY_GUARD_CODE}, '{IDENTITY_GUARD_MESSAGE}');\n"
        "  END IF;\n"
        "END;\n"
        "/\n"
    )


_RESULT_BEGIN = "TEAM_RESULT_BEGIN"
_RESULT_END = "TEAM_RESULT_END"


def _diagnostic_region(stdout: str) -> str:
    """Return output outside the payload's own result rows.

    A successful query may legitimately select text that looks like a
    diagnostic -- a table of ORA- codes, a column holding a Java class name.
    Scanning it for error patterns fails the run on its own data. Markers that
    do not pair are treated as absent, so an aborted payload is still scanned
    in full.
    """
    lines = stdout.splitlines()
    starts = [index for index, line in enumerate(lines) if line.strip() == _RESULT_BEGIN]
    ends = [index for index, line in enumerate(lines) if line.strip() == _RESULT_END]
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        return stdout
    kept = lines[: starts[0]] + lines[ends[0] + 1 :]
    return "\n".join(kept)


def _driver_text(payload_name: str, operation: str, target: Target) -> str:
    return (
        "SET DEFINE OFF\n"
        "SET ENCODING UTF-8\n"
        "SET HEADING OFF\n"
        "SET FEEDBACK OFF\n"
        "SET LINESIZE 32767\n"
        "SET PAGESIZE 0\n"
        "SET LONG 1000000\n"
        "SET ECHO OFF\n"
        "SET VERIFY OFF\n"
        "WHENEVER SQLERROR EXIT SQL.SQLCODE\n"
        "WHENEVER OSERROR EXIT FAILURE\n"
        + _sql_marker_query()
        + "\n"
        + _sql_marker_query()
        + "\n"
        + _identity_guard_block(target)
        + f"PROMPT {_RESULT_BEGIN}\n"
        + f"@{payload_name}\n"
        + f"PROMPT {_RESULT_END}\n"
        + f"SELECT 'TEAM_COMPLETION|operation={operation}' FROM DUAL;\n"
        + "EXIT\n"
    )


def _redact_output(text: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
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


def resolve_sqlcl_executable(executable: str | Path | None = None) -> str:
    """Resolve the SQLcl binary used by every database-facing command."""
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
    timeout: float | None = None,
    secrets: tuple[str, ...] = (),
) -> SqlResult:
    """Run one verified SQLcl process with a regular empty stdin file.

    The driver, payload copy, stdin placeholder and log stay in ``work`` as run evidence
    (recovery records may point at them) unless ``secrets`` are given; ``team.py
    prune-scratch`` applies retention. Production read-only relies on a read-only
    database account; ``_assert_production_read_only`` is only a keyword safety net.
    """
    resolved_timeout = _resolve_timeout(timeout)
    if not isinstance(secrets, tuple) or any(not isinstance(secret, str) or not secret for secret in secrets):
        raise SqlclError("SQLcl secret scrub values must be non-empty strings")
    if operation not in {"read", "write"}:
        raise SqlclError(f"unsupported SQLcl operation: {operation}")
    if target.environment == "production" and operation != "read":
        raise SqlclError("production database operations are always read-only")

    # SQLcl changes its process directory to ``work``.  Resolve both paths
    # before building the @driver argument; otherwise a caller that supplies a
    # relative work directory makes SQLcl look for ``work/work/driver.sql``.
    driver_path = Path(driver).resolve()
    if not driver_path.is_file() or driver_path.is_symlink():
        raise SqlclError(f"SQLcl driver is not a regular file: {driver_path}")
    work_path = Path(work).resolve()
    work_path.mkdir(parents=True, exist_ok=True)
    if work_path.is_symlink():
        raise SqlclError(f"SQLcl work directory must not be a symbolic link: {work_path}")

    try:
        payload = driver_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SqlclError(f"could not read SQLcl driver: {driver_path}") from exc
    if target.environment == "production" and operation == "read":
        _assert_production_read_only(payload)

    resolved_executable = resolve_sqlcl_executable(executable)
    run_id = uuid.uuid4().hex
    generated_driver = work_path / f".team-driver-{run_id}.sql"
    payload_copy = work_path / f".team-payload-{run_id}.sql"
    stdin_path = work_path / f".team-stdin-{run_id}.empty"
    log_path = work_path / f".team-sqlcl-{run_id}.log"
    payload_copy.write_text(payload, encoding="utf-8", newline="")
    generated_driver.write_text(_driver_text(payload_copy.name, operation, target), encoding="utf-8", newline="")
    stdin_path.touch()
    for transient in (payload_copy, generated_driver, stdin_path):
        os.chmod(transient, 0o600)
    if not stat.S_ISREG(stdin_path.stat().st_mode):
        raise SqlclError("SQLcl stdin is not a regular file")

    argv = _build_argv(resolved_executable, target, generated_driver)
    try:
        try:
            with stdin_path.open("rb") as empty:
                completed = subprocess.run(
                    argv,
                    stdin=empty,
                    cwd=work_path,
                    shell=False,
                    capture_output=True,
                    timeout=resolved_timeout,
                )
        except subprocess.TimeoutExpired as exc:
            raise SqlclUnknownResult("SQLcl timed out; target state is unknown") from exc
        except OSError as exc:
            raise SqlclError(f"could not start SQLcl: {exc}") from exc

        try:
            stdout = completed.stdout.decode("utf-8")
            stderr = completed.stderr.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SqlclError("SQLcl output was not valid UTF-8") from exc
        stdout = stdout.replace("\r\n", "\n").replace("\r", "\n")
        stderr = stderr.replace("\r\n", "\n").replace("\r", "\n")
        redacted_stdout = _redact_output(stdout, secrets)
        redacted_stderr = _redact_output(stderr, secrets)
        safe_log = redacted_stdout + ("\n" + redacted_stderr if redacted_stderr else "")
        log_path.write_text(safe_log, encoding="utf-8", newline="")

        if completed.returncode != 0:
            guard_marker = f"ORA-{IDENTITY_GUARD_CODE}: {IDENTITY_GUARD_MESSAGE}"
            if guard_marker in _diagnostic_region(stdout) or guard_marker in stderr:
                raise SqlclError(
                    "SQLcl identity guard refused the session before the payload ran: "
                    f"connection {target.connection!r} does not reach the expected target; see {log_path}"
                )
            raise SqlclError(f"SQLcl failed with exit code {completed.returncode}; see {log_path}")
        diagnostics = _diagnostic_region(stdout)
        if _OUTPUT_ERROR_RE.search(diagnostics) or _OUTPUT_ERROR_RE.search(stderr):
            match = _OUTPUT_ERROR_RE.search(diagnostics) or _OUTPUT_ERROR_RE.search(stderr)
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
            stdout=redacted_stdout,
            stderr=redacted_stderr,
            argv=tuple(argv),
            exit_code=completed.returncode,
        )
    finally:
        if secrets:
            for transient in (payload_copy, generated_driver, stdin_path):
                try:
                    transient.unlink(missing_ok=True)
                except OSError:
                    pass
