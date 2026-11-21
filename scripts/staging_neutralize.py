#!/usr/bin/env python3
"""Make a freshly restored staging copy inert before any release is rehearsed on it.

A copy restored from production still runs production's scheduler jobs, APEX automations
(mail, SMS, payments) and outbound network grants. This module reports those (--capture,
--check), disables them (--apply) and runs the exact undo written by --apply (--restore).

Every run uses one saved DBA SQLcl alias (STAGING_DBA_SQLCL_CONNECTION) in one session. The
database refuses any change unless DB_UNIQUE_NAME equals --confirm-target, checked inside that
session before the first change. The APEX engine schema is discovered from DBA_REGISTRY.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import terminal_confirm
from scripts.db_targets import looks_like_production_identity, split_list
from scripts.local_config import ConfigError, read_project_env
from scripts.sqlcl_session import SqlclError, run_sqlcl


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRATCH_PARTS = ("scratch", "staging-neutralize")
RUNS_DIRECTORY = "runs"
PROGRAM = "scripts/team.sh staging-neutralize"

# APEX instance jobs that send mail or SMS, push, notify, sync REST sources or run automations.
APEX_JOB_NAMES = (
    "ORACLE_APEX_AUTOMATIONS",
    "ORACLE_APEX_MAIL_QUEUE",
    "ORACLE_APEX_PWA_PUSH_QUEUE",
    "ORACLE_APEX_WS_NOTIFICATIONS",
    "ORACLE_APEX_ISSUE_EMAILS",
    "ORACLE_APEX_REST_SOURCE_SYNC",
)

ORACLE_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
DB_UNIQUE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_$#.\-]{0,127}\Z", re.ASCII)
SQLCL_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z", re.ASCII)
TAG_LINE = re.compile(r"NEUTRALIZE\|(.*)\Z")
DIGITS = re.compile(r"[0-9]+\Z", re.ASCII)

# One quoted SQL string literal as this module writes it: doubled quotes inside, no line breaks.
_QUOTED = r"'(?:[^'\n]|'')*'"
# Every statement shape the undo file may contain. Anything else is refused before SQLcl starts.
UNDO_LINE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"--.*",
        r"",
        r"/",
        r"BEGIN",
        r"END;",
        rf"BEGIN DBMS_SCHEDULER\.ENABLE\(name => {_QUOTED}\); END;",
        rf"  APEX_UTIL\.SET_SECURITY_GROUP_ID\(APEX_UTIL\.FIND_SECURITY_GROUP_ID\({_QUOTED}\)\);",
        rf"  APEX_AUTOMATION\.ENABLE\(p_application_id => [1-9][0-9]*, p_static_id => {_QUOTED}\);",
        rf"  DBMS_NETWORK_ACL_ADMIN\.APPEND_HOST_ACE\(host => {_QUOTED}, lower_port => (?:[0-9]+|NULL), "
        rf"upper_port => (?:[0-9]+|NULL), ace => XS\$ACE_TYPE\(privilege_list => XS\$NAME_LIST\({_QUOTED}\), "
        rf"principal_name => {_QUOTED}, principal_type => XS_ACL\.PTYPE_DB\)\);",
    )
)

EXIT_OK = 0
EXIT_ACTIVE = 1  # --check: something is still active; --apply: some objects could not be changed
EXIT_ERROR = 2  # refusal, configuration error, or a SQLcl error

REMINDERS = (
    "Inbound access is not closed by this command: ORDS and the APEX apps answer on the copy's public endpoint. "
    "Block it outside the database (cloud access control list: operator addresses only).",
    "Run `" + PROGRAM + " --check` again after every APEX application import: an import can switch automations back on.",
)


class NeutralizeError(ValueError):
    """A refusal or configuration problem, reported without any change."""


@dataclass(frozen=True)
class DbaConnection:
    """The only target this module needs: a saved SQLcl alias for a DBA login."""

    connection: str


@dataclass
class Report:
    """What one SQLcl run printed, read from its NEUTRALIZE| lines."""

    identity: tuple[str, ...] | None = None
    apex: str | None = None
    rows: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    undo: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    failed: list[tuple[str, str, str]] = field(default_factory=list)
    summary: tuple[int, int] | None = None
    complete: bool = False
    malformed: list[str] = field(default_factory=list)

    def rows_of(self, kind: str) -> list[tuple[str, ...]]:
        return [values for row_kind, values in self.rows if row_kind == kind]


# ---- configuration -------------------------------------------------------------------------

def parse_principals(values: Mapping[str, str]) -> tuple[str, ...]:
    """Union, in order, of the staging schemas, the staging migration schemas and the extra principals."""
    names: list[str] = []
    for key in ("STAGING_SCHEMA", "STAGING_MIGRATION_SCHEMA", "STAGING_NEUTRALIZE_PRINCIPALS"):
        for name in split_list(values.get(key)):
            if ORACLE_IDENTIFIER.fullmatch(name) is None:
                raise NeutralizeError(f"{key} must list uppercase Oracle identifiers")
            if name not in names:
                names.append(name)
    if not names:
        raise NeutralizeError(
            "no principals to neutralize: set STAGING_SCHEMA, STAGING_MIGRATION_SCHEMA or STAGING_NEUTRALIZE_PRINCIPALS"
        )
    return tuple(names)


def dba_connection(values: Mapping[str, str]) -> str:
    connection = values.get("STAGING_DBA_SQLCL_CONNECTION", "").strip()
    if not connection:
        raise NeutralizeError("set STAGING_DBA_SQLCL_CONNECTION to a saved SQLcl alias for a DBA login")
    if SQLCL_ALIAS.fullmatch(connection) is None:
        raise NeutralizeError("STAGING_DBA_SQLCL_CONNECTION must be one saved SQLcl alias")
    if looks_like_production_identity(connection):
        raise NeutralizeError("refused: STAGING_DBA_SQLCL_CONNECTION looks like a production connection")
    return connection


def confirm_name(value: str | None) -> str:
    if value is None:
        raise NeutralizeError("--confirm-target <db_unique_name> is required for --apply and --restore; nothing was run")
    if DB_UNIQUE_NAME.fullmatch(value) is None:
        raise NeutralizeError("--confirm-target must be a db_unique_name (letters, digits, _ $ # . -)")
    return value


def scratch_directory(repo_root: Path) -> Path:
    return repo_root.joinpath(*SCRATCH_PARTS)


# ---- SQL generation ------------------------------------------------------------------------

def _sql_list(values: Sequence[str]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_HEADER = """SET ECHO OFF
SET FEEDBACK OFF
SET VERIFY OFF
SET HEADING OFF
SET DEFINE OFF
SET SERVEROUTPUT ON SIZE UNLIMITED
SET LINESIZE 32767
WHENEVER OSERROR EXIT FAILURE ROLLBACK
"""


def _guard_block(confirmed: str) -> str:
    """Refuse unless the session's DB_UNIQUE_NAME equals the confirmed target (case-insensitively)."""
    return f"""DECLARE
  c_confirmed CONSTANT VARCHAR2(128) := {_quote(confirmed)};
BEGIN
  IF UPPER(SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME')) <> UPPER(c_confirmed) THEN
    RAISE_APPLICATION_ERROR(-20001, 'Refusing: DB_UNIQUE_NAME is not the confirmed target; no change was made');
  END IF;
END;
/
"""


def _declarations() -> str:
    return """  v_apex VARCHAR2(128);
  PROCEDURE emit(p_line VARCHAR2) IS
  BEGIN
    DBMS_OUTPUT.PUT_LINE('NEUTRALIZE|' || p_line);
  END;
"""


def _discovery_and_identity() -> str:
    return """  emit('IDENT|' || SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME') || '|' || SYS_CONTEXT('USERENV', 'CON_NAME')
       || '|' || SYS_CONTEXT('USERENV', 'SESSION_USER'));
  BEGIN
    SELECT schema INTO v_apex FROM dba_registry WHERE comp_id = 'APEX';
  EXCEPTION
    WHEN NO_DATA_FOUND THEN
      RAISE_APPLICATION_ERROR(-20002, 'APEX is not registered in DBA_REGISTRY; this copy has no APEX engine to neutralize');
  END;
  emit('APEX|' || v_apex);
"""


def _job_selection(principals: Sequence[str]) -> str:
    return (
        f"owner IN ({_sql_list(principals)}) OR (owner = v_apex AND job_name IN ({_sql_list(APEX_JOB_NAMES)}))"
    )


def _ace_selection(principals: Sequence[str]) -> str:
    return (
        f"grant_type = 'GRANT' AND (principal IN ({_sql_list(principals)}) "
        "OR (principal = v_apex AND privilege = 'HTTP' AND host <> '*'))"
    )


def inventory_script(principals: Sequence[str]) -> str:
    """Read-only: every enabled job, running job, ACTIVE automation and network grant in scope."""
    return (
        _HEADER
        + "WHENEVER SQLERROR EXIT FAILURE ROLLBACK\n"
        + "DECLARE\n"
        + _declarations()
        + "BEGIN\n"
        + _discovery_and_identity()
        + f"""  FOR j IN (SELECT owner, job_name, state FROM dba_scheduler_jobs
            WHERE enabled = 'TRUE' AND ({_job_selection(principals)})
            ORDER BY owner, job_name) LOOP
    emit('ROW|JOB|' || j.owner || '|' || j.job_name || '|' || j.state);
  END LOOP;
  FOR r IN (SELECT owner, job_name, session_id FROM dba_scheduler_running_jobs
            WHERE {_job_selection(principals)} OR job_name LIKE 'APEX$AUTOMATION%'
            ORDER BY owner, job_name) LOOP
    emit('ROW|RUNNING|' || r.owner || '|' || r.job_name || '|' || r.session_id);
  END LOOP;
  FOR a IN (SELECT application_id, static_id, workspace, polling_status_code FROM apex_appl_automations
            WHERE polling_status_code = 'ACTIVE' ORDER BY application_id, static_id) LOOP
    emit('ROW|AUTOMATION|' || a.application_id || '|' || a.static_id || '|' || a.workspace
         || '|' || a.polling_status_code);
  END LOOP;
  FOR e IN (SELECT principal, host, lower_port, upper_port, privilege FROM dba_host_aces
            WHERE {_ace_selection(principals)} ORDER BY principal, host, privilege) LOOP
    emit('ROW|ACE|' || e.principal || '|' || e.host || '|' || e.lower_port || '|' || e.upper_port
         || '|' || e.privilege);
  END LOOP;
  emit('END');
END;
/
EXIT SUCCESS
"""
    )


def apply_script(principals: Sequence[str], confirmed: str) -> str:
    """One PL/SQL block: guard, then each job, automation and grant in its own handler."""
    return (
        _HEADER
        + "WHENEVER SQLERROR EXIT FAILURE ROLLBACK\n"
        + "DECLARE\n"
        + "  c_confirmed CONSTANT VARCHAR2(128) := " + _quote(confirmed) + ";\n"
        + "  v_ws NUMBER;\n"
        + "  v_changed PLS_INTEGER := 0;\n"
        + "  v_failed PLS_INTEGER := 0;\n"
        + _declarations()
        + "BEGIN\n"
        + """  IF UPPER(SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME')) <> UPPER(c_confirmed) THEN
    RAISE_APPLICATION_ERROR(-20001, 'Refusing: DB_UNIQUE_NAME is not the confirmed target; no change was made');
  END IF;
"""
        + _discovery_and_identity()
        + f"""  -- 1. scheduler jobs of the principals and the APEX jobs that send, push, notify, sync or run automations
  FOR j IN (SELECT owner, job_name FROM dba_scheduler_jobs
            WHERE enabled = 'TRUE' AND ({_job_selection(principals)})
            ORDER BY owner, job_name) LOOP
    BEGIN
      DBMS_SCHEDULER.DISABLE(name => j.owner || '.' || j.job_name, force => TRUE);
      v_changed := v_changed + 1;
      emit('UNDO|JOB|' || j.owner || '|' || j.job_name);
    EXCEPTION
      WHEN OTHERS THEN
        v_failed := v_failed + 1;
        emit('FAILED|JOB|' || j.owner || '.' || j.job_name || '|' || REPLACE(REPLACE(SQLERRM, CHR(10), ' '), '|', '/'));
    END;
  END LOOP;
  -- 2. every ACTIVE APEX automation (a later App import can switch automations back on: run --check afterwards)
  FOR a IN (SELECT application_id, static_id, workspace FROM apex_appl_automations
            WHERE polling_status_code = 'ACTIVE' ORDER BY application_id, static_id) LOOP
    BEGIN
      v_ws := APEX_UTIL.FIND_SECURITY_GROUP_ID(a.workspace);
      APEX_UTIL.SET_SECURITY_GROUP_ID(v_ws);
      APEX_AUTOMATION.DISABLE(p_application_id => a.application_id, p_static_id => a.static_id);
      v_changed := v_changed + 1;
      emit('UNDO|AUTOMATION|' || a.application_id || '|' || a.static_id || '|' || a.workspace);
    EXCEPTION
      WHEN OTHERS THEN
        v_failed := v_failed + 1;
        emit('FAILED|AUTOMATION|' || a.application_id || '/' || a.static_id || '|' || REPLACE(REPLACE(SQLERRM, CHR(10), ' '), '|', '/'));
    END;
  END LOOP;
  -- 3. outbound network grants of the principals; the APEX engine keeps CONNECT and loses its named-host HTTP grants
  FOR e IN (SELECT host, lower_port, upper_port, principal, privilege FROM dba_host_aces
            WHERE {_ace_selection(principals)} ORDER BY principal, host, privilege) LOOP
    BEGIN
      DBMS_NETWORK_ACL_ADMIN.REMOVE_HOST_ACE(host => e.host, lower_port => e.lower_port, upper_port => e.upper_port,
        ace => XS$ACE_TYPE(privilege_list => XS$NAME_LIST(LOWER(e.privilege)), principal_name => e.principal,
                           principal_type => XS_ACL.PTYPE_DB));
      v_changed := v_changed + 1;
      emit('UNDO|ACE|' || e.principal || '|' || e.host || '|' || NVL(TO_CHAR(e.lower_port), '') || '|'
           || NVL(TO_CHAR(e.upper_port), '') || '|' || e.privilege);
    EXCEPTION
      WHEN OTHERS THEN
        v_failed := v_failed + 1;
        emit('FAILED|ACE|' || e.principal || ' ' || e.host || ' ' || e.privilege || '|' || REPLACE(REPLACE(SQLERRM, CHR(10), ' '), '|', '/'));
    END;
  END LOOP;
  COMMIT;
  emit('SUMMARY|' || v_changed || '|' || v_failed);
END;
/
EXIT SUCCESS
"""
    )


def restore_script(confirmed: str, restore_file: Path) -> str:
    """Guard first (driver stops on error), then the undo file with per-statement error reporting."""
    return (
        _HEADER
        + "WHENEVER SQLERROR EXIT FAILURE ROLLBACK\n"
        + _guard_block(confirmed)
        + "WHENEVER SQLERROR CONTINUE\n"
        + f'@"{restore_file.as_posix()}"\n'
        + "COMMIT;\n"
        + "EXIT SUCCESS\n"
    )


# ---- undo file -----------------------------------------------------------------------------

def _undo_statements(undo: Sequence[tuple[str, tuple[str, ...]]]) -> list[str]:
    lines: list[str] = []
    for kind, values in undo:
        if kind == "JOB":
            owner, job = values
            lines.append(f"BEGIN DBMS_SCHEDULER.ENABLE(name => {_quote(owner + '.' + job)}); END;")
            lines.append("/")
        elif kind == "AUTOMATION":
            application, static_id, workspace = values
            lines += [
                "BEGIN",
                f"  APEX_UTIL.SET_SECURITY_GROUP_ID(APEX_UTIL.FIND_SECURITY_GROUP_ID({_quote(workspace)}));",
                f"  APEX_AUTOMATION.ENABLE(p_application_id => {application}, p_static_id => {_quote(static_id)});",
                "END;",
                "/",
            ]
        elif kind == "ACE":
            principal, host, lower, upper, privilege = values
            lines += [
                "BEGIN",
                f"  DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE(host => {_quote(host)}, lower_port => {lower or 'NULL'}, "
                f"upper_port => {upper or 'NULL'}, ace => XS$ACE_TYPE(privilege_list => XS$NAME_LIST({_quote(privilege.lower())}), "
                f"principal_name => {_quote(principal)}, principal_type => XS_ACL.PTYPE_DB));",
                "END;",
                "/",
            ]
    return lines


def render_restore_file(confirmed: str, stamp: str, undo: Sequence[tuple[str, tuple[str, ...]]]) -> str:
    header = [
        "-- staging-neutralize undo: restores what --apply disabled on the copy named below.",
        f"-- target db_unique_name: {confirmed}",
        f"-- generated (UTC): {stamp}",
        f"-- run only with: {PROGRAM} --restore <this file> --confirm-target {confirmed}",
        "",
    ]
    return "\n".join(header + _undo_statements(undo)) + "\n"


def validate_restore_file(path: Path, confirmed: str) -> int:
    """Refuse a file that is not our undo file for this target; return the number of statements."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    targets = [line for line in lines if line.startswith("-- target db_unique_name: ")]
    if targets != [f"-- target db_unique_name: {confirmed}"]:
        raise NeutralizeError("restore file does not name this --confirm-target as its target; nothing was run")
    for number, line in enumerate(lines, start=1):
        if not any(pattern.fullmatch(line) for pattern in UNDO_LINE_PATTERNS):
            raise NeutralizeError(f"restore file has an unexpected statement at line {number}; nothing was run")
    return sum(1 for line in lines if line == "/")


# ---- SQLcl output ----------------------------------------------------------------------------

ROW_WIDTHS = {"JOB": 3, "RUNNING": 3, "AUTOMATION": 4, "ACE": 5}
UNDO_WIDTHS = {"JOB": 2, "AUTOMATION": 3, "ACE": 5}


def _undo_values_ok(kind: str, values: tuple[str, ...]) -> bool:
    if kind == "AUTOMATION":
        return DIGITS.fullmatch(values[0]) is not None
    if kind == "ACE":
        ports_ok = all(port == "" or DIGITS.fullmatch(port) for port in values[2:4])
        return ports_ok and bool(values[4]) and re.fullmatch(r"[A-Za-z_]+", values[4]) is not None
    return True


def parse_report(output: str) -> Report:
    report = Report()
    for raw in output.splitlines():
        match = TAG_LINE.match(raw.rstrip("\r"))
        if match is None:
            continue
        fields = match.group(1).split("|")
        kind = fields[0]
        if kind == "IDENT" and len(fields) == 4:
            report.identity = tuple(fields[1:])
        elif kind == "APEX" and len(fields) == 2:
            report.apex = fields[1]
        elif kind == "ROW" and len(fields) >= 3 and ROW_WIDTHS.get(fields[1]) == len(fields) - 2:
            report.rows.append((fields[1], tuple(fields[2:])))
        elif kind == "UNDO" and len(fields) >= 3 and UNDO_WIDTHS.get(fields[1]) == len(fields) - 2:
            values = tuple(fields[2:])
            if _undo_values_ok(fields[1], values):
                report.undo.append((fields[1], values))
            else:
                report.malformed.append(raw)
        elif kind == "FAILED" and len(fields) >= 4:
            report.failed.append((fields[1], fields[2], "|".join(fields[3:])))
        elif kind == "SUMMARY" and len(fields) == 3 and DIGITS.fullmatch(fields[1]) and DIGITS.fullmatch(fields[2]):
            report.summary = (int(fields[1]), int(fields[2]))
        elif kind == "END" and len(fields) == 1:
            report.complete = True
        else:
            report.malformed.append(raw)
    return report


# ---- presentation ---------------------------------------------------------------------------

def _table(title: str, headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    lines = [f"{title} ({len(rows)})"]
    if not rows:
        return lines + ["  none"]
    widths = [max(len(str(headers[i])), *(len(str(row[i])) for row in rows)) for i in range(len(headers))]
    lines.append("  " + "  ".join(str(h).ljust(widths[i]) for i, h in enumerate(headers)).rstrip())
    for row in rows:
        lines.append("  " + "  ".join(str(value).ljust(widths[i]) for i, value in enumerate(row)).rstrip())
    return lines


def _print_inventory(report: Report, principals: Sequence[str]) -> None:
    if report.identity is not None:
        db_unique, con_name, session_user = report.identity
        print(f"SYS_CONTEXT DB_UNIQUE_NAME={db_unique}  CON_NAME={con_name}  SESSION_USER={session_user}")
    print(f"APEX engine schema: {report.apex or 'unknown'} (from DBA_REGISTRY)")
    print("Principals: " + ", ".join(principals))
    sections = (
        _table("Enabled scheduler jobs", ("OWNER", "JOB", "STATE"), report.rows_of("JOB")),
        _table("Running jobs", ("OWNER", "JOB", "SESSION_ID"), report.rows_of("RUNNING")),
        _table("ACTIVE APEX automations", ("APP", "STATIC_ID", "WORKSPACE", "STATUS"), report.rows_of("AUTOMATION")),
        _table(
            "Network ACEs (principals and APEX named-host HTTP)",
            ("PRINCIPAL", "HOST", "LOWER", "UPPER", "PRIVILEGE"),
            report.rows_of("ACE"),
        ),
    )
    for section in sections:
        print("")
        print("\n".join(section))


def _print_reminders() -> None:
    print("")
    for reminder in REMINDERS:
        print("Reminder: " + reminder)


def _confirm(args_yes: bool, confirmed: str | None, action: str, connection: str,
             principals: Sequence[str] | None, restore_file: Path | None) -> bool:
    if args_yes:
        print(terminal_confirm.confirmed_line(True, confirmed))
        return True
    print(f"About to {action} on the DBA alias {connection!r} for target db_unique_name {confirmed!r}.")
    if principals is not None:
        print("Principals: " + ", ".join(principals))
    if restore_file is not None:
        print(f"Undo file: {restore_file}")
    if not terminal_confirm.may_read_yes_no():
        return False
    try:
        answer = input("Type y to continue [y/N]: ")
    except (EOFError, OSError):
        return False
    return answer.strip().casefold() in {"y", "yes"}


def _run(connection: str, driver_text: str, run_base: Path, label: str) -> tuple[str, SqlclError | None, Path]:
    run_base.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=f"{label}-", dir=str(run_base)))
    driver = run_dir / "staging-neutralize-driver.sql"
    driver.write_text(driver_text, encoding="utf-8", newline="\n")
    try:
        result = run_sqlcl(DbaConnection(connection), driver, run_dir)
        return result.output, None, run_dir
    except SqlclError as error:
        return error.output, error, error.run_dir


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ---- modes ----------------------------------------------------------------------------------

def _check_restore_path(path_text: str, repo_root: Path) -> Path:
    if '"' in path_text or "\n" in path_text or "\r" in path_text:
        raise NeutralizeError("--restore path must not contain a double quote or a line break")
    scratch = scratch_directory(repo_root)
    candidate = Path(path_text)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    if candidate.is_symlink():
        raise NeutralizeError("--restore file must not be a symbolic link")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise NeutralizeError("--restore file does not exist") from error
    try:
        resolved.relative_to(scratch.resolve())
    except ValueError as error:
        raise NeutralizeError(f"--restore file must be inside {Path(*SCRATCH_PARTS).as_posix()}/") from error
    if not resolved.is_file() or resolved.suffix != ".sql":
        raise NeutralizeError("--restore file must be an existing .sql undo file")
    return resolved


def do_capture(connection: str, principals: Sequence[str], run_base: Path) -> int:
    output, error, _ = _run(connection, inventory_script(principals), run_base, "capture")
    if error is not None:
        print(f"team error: {error}", file=sys.stderr)
        return EXIT_ERROR
    report = parse_report(output)
    if not report.complete or report.identity is None or report.malformed:
        print("team error: the inventory could not be read completely; the result is unknown", file=sys.stderr)
        return EXIT_ERROR
    _print_inventory(report, principals)
    _print_reminders()
    return EXIT_OK


def do_check(connection: str, principals: Sequence[str], run_base: Path) -> int:
    output, error, _ = _run(connection, inventory_script(principals), run_base, "check")
    if error is not None:
        print(f"team error: {error}", file=sys.stderr)
        return EXIT_ERROR
    report = parse_report(output)
    if not report.complete or report.identity is None or report.malformed:
        print("team error: the inventory could not be read completely; the result is unknown", file=sys.stderr)
        return EXIT_ERROR
    _print_inventory(report, principals)
    jobs, running = len(report.rows_of("JOB")), len(report.rows_of("RUNNING"))
    automations, aces = len(report.rows_of("AUTOMATION")), len(report.rows_of("ACE"))
    print("")
    print(f"--apply would change: {jobs} job(s), {automations} automation(s), {aces} ACE(s); running jobs: {running}")
    _print_reminders()
    if jobs or running or automations or aces:
        print("")
        print(f"ACTIVE: the copy is not inert; run `{PROGRAM} --apply --confirm-target <db_unique_name>`.")
        return EXIT_ACTIVE
    print("")
    print("INERT: no enabled principal jobs, running jobs, ACTIVE automations or network grants in scope.")
    return EXIT_OK


def do_apply(args: argparse.Namespace, connection: str, principals: Sequence[str],
             repo_root: Path, run_base: Path) -> int:
    confirmed = args.confirm_target
    scratch = scratch_directory(repo_root)
    scratch.mkdir(parents=True, exist_ok=True)
    stamp = _utc_stamp()
    restore_path = scratch / f"{confirmed}-{stamp}-restore.sql"
    if restore_path.exists():
        raise NeutralizeError(f"undo file already exists for this second: {restore_path}; retry in a moment")
    if not _confirm(args.yes, confirmed, "neutralize the staging copy (disable jobs, automations and grants)",
                    connection, principals, None):
        print("Not confirmed; nothing was changed.", file=sys.stderr)
        return EXIT_ERROR
    # Exclusive create: the undo path is claimed before the first change, and replaced with the full file after the run.
    with restore_path.open("x", encoding="utf-8", newline="\n") as placeholder:
        placeholder.write(render_restore_file(confirmed, stamp, []))

    output, error, run_dir = _run(connection, apply_script(principals, confirmed), run_base, "apply")
    report = parse_report(output)
    if report.undo:
        restore_path.write_text(render_restore_file(confirmed, stamp, report.undo), encoding="utf-8", newline="\n")
        print(f"Undo file: {restore_path}")
    else:
        restore_path.unlink(missing_ok=True)
        print("No change was recorded, so no undo file was written.")

    if report.identity is not None:
        db_unique, con_name, session_user = report.identity
        print(f"SYS_CONTEXT DB_UNIQUE_NAME={db_unique}  CON_NAME={con_name}  SESSION_USER={session_user}")
    if error is not None:
        print(f"team error: the apply stopped: {error}", file=sys.stderr)
        return EXIT_ERROR
    if report.summary is None:
        print("team error: SQLcl did not confirm the apply; the result is unknown. Run --check.", file=sys.stderr)
        return EXIT_ERROR
    changed, failed = report.summary
    for kind, name, message in report.failed:
        print(f"FAILED {kind} {name}: {message}", file=sys.stderr)
    print(f"Changed {changed} object(s), {failed} failure(s). Run logs: {run_dir}")
    if report.malformed:
        print("warning: some output lines could not be read; the undo file may be incomplete", file=sys.stderr)
    _print_reminders()
    if failed or report.malformed:
        return EXIT_ACTIVE
    return EXIT_OK


def do_restore(args: argparse.Namespace, connection: str, repo_root: Path, run_base: Path) -> int:
    confirmed = args.confirm_target
    restore_file = _check_restore_path(args.restore, repo_root)
    statements = validate_restore_file(restore_file, confirmed)
    if not _confirm(args.yes, confirmed, "run the undo file", connection, None, restore_file):
        print("Not confirmed; nothing was changed.", file=sys.stderr)
        return EXIT_ERROR
    print(f"Running {statements} undo statement(s) from the file; the database identity is checked first.")
    output, error, run_dir = _run(connection, restore_script(confirmed, restore_file), run_base, "restore")
    report = parse_report(output)
    if report.identity is not None:
        db_unique, con_name, session_user = report.identity
        print(f"SYS_CONTEXT DB_UNIQUE_NAME={db_unique}  CON_NAME={con_name}  SESSION_USER={session_user}")
    if error is not None:
        print(f"team error: the undo reported errors; some statements may not have run. Logs: {run_dir}", file=sys.stderr)
        return EXIT_ERROR
    print(f"Undo finished. Run logs: {run_dir}")
    _print_reminders()
    return EXIT_OK


# ---- entry point ----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Report, check, disable or restore the jobs, APEX automations and network grants of a staging copy.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture", action="store_true", help="read-only report of what is live")
    mode.add_argument("--check", action="store_true", help="read-only; exit 0 when inert, 1 when anything is active")
    mode.add_argument("--apply", action="store_true", help="disable jobs, automations and grants; write the undo file")
    mode.add_argument("--restore", metavar="FILE", help="run an undo file written by --apply (path inside scratch/staging-neutralize/)")
    parser.add_argument("--confirm-target", metavar="DB_UNIQUE_NAME",
                        help="required for --apply and --restore: the db_unique_name the database must report")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the [y/N] confirmation for --apply or --restore")
    parser.add_argument("--env-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if (args.confirm_target is not None or args.yes) and not (args.apply or args.restore):
        parser.error("--confirm-target and --yes apply only to --apply and --restore")
    repo_root = Path(args.repo_root).resolve()
    run_base = scratch_directory(repo_root) / RUNS_DIRECTORY
    try:
        if args.apply or args.restore:
            confirm_name(args.confirm_target)
        env_file = args.env_file or Path(os.environ.get("PROJECT_ENV_FILE") or repo_root / ".env")
        values = read_project_env(env_file)
        connection = dba_connection(values)
        if args.restore is not None:
            return do_restore(args, connection, repo_root, run_base)
        principals = parse_principals(values)
        if args.capture:
            return do_capture(connection, principals, run_base)
        if args.check:
            return do_check(connection, principals, run_base)
        return do_apply(args, connection, principals, repo_root, run_base)
    except (NeutralizeError, ConfigError) as error:
        print(f"team error: {error}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
