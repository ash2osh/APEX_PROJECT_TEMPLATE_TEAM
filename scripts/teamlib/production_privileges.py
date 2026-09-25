"""Fail-closed audit for production SQLcl account privileges."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Any

from .config import ConfigError, Target
from .sqlcl import run_sqlcl


class ProductionPrivilegeError(ConfigError):
    """Raised when a production profile cannot prove read-only access."""


@dataclass(frozen=True)
class ProductionPrivilegeReport:
    system_privileges: tuple[str, ...]
    roles: tuple[str, ...]
    object_privileges: tuple[str, ...]
    owned_objects: tuple[str, ...]
    # Oracle's own grants to PUBLIC on Oracle-maintained objects: counted, not judged.
    public_oracle_grants: int = 0


# Use object-level SELECT/READ grants for the required dictionary and APEX
# views. These effective system privileges are also read-only, but any system
# privilege not listed here fails closed (including EXECUTE and DDL grants).
_READ_ONLY_SYSTEM_PRIVILEGES = frozenset(
    {
        "CREATE SESSION",
        "READ ANY TABLE",
        "SELECT ANY DICTIONARY",
        "SELECT ANY TABLE",
    }
)
_READ_ONLY_OBJECT_PRIVILEGES = frozenset({"READ", "SELECT"})
# Oracle packages PUBLIC may hold by default that change database state or
# reach outside the database without any further grant the audit would see.
# Hardening guides (CIS) revoke these from PUBLIC; a read-only production
# account must not inherit them. Other Oracle packages run with the caller's
# own (audited, read-only) privileges or need a separate grant (directory,
# network ACL, CREATE JOB) that the audit already refuses.
_PUBLIC_EXECUTE_DENYLIST = frozenset(
    {
        "DBMS_ADVISOR", "DBMS_BACKUP_RESTORE", "DBMS_FILE_TRANSFER", "DBMS_IJOB",
        "DBMS_JOB", "DBMS_LDAP", "DBMS_PIPE", "DBMS_SYS_SQL", "HTTPURITYPE",
        "UTL_FILE", "UTL_HTTP", "UTL_INADDR", "UTL_MAIL", "UTL_SMTP", "UTL_TCP",
    }
)
_TEMPORARY_TABLE_DML = frozenset({"INSERT", "UPDATE", "DELETE", "SELECT", "READ"})


def _public_oracle_baseline(privilege: str, object_type: str, object_name: str, temporary: str) -> bool:
    """Whether one PUBLIC grant on an Oracle-maintained object is the accepted baseline."""
    if object_type == "SEQUENCE":
        return False
    if privilege in _READ_ONLY_OBJECT_PRIVILEGES:
        return True
    if privilege == "EXECUTE":
        return object_name not in _PUBLIC_EXECUTE_DENYLIST
    # Session-private data (e.g. SYS.PLAN_TABLE$): DML on it changes nothing
    # another session or a later session can observe.
    return temporary == "Y" and object_type == "TABLE" and privilege in _TEMPORARY_TABLE_DML


def parse_production_privileges(stdout: str) -> ProductionPrivilegeReport:
    """Parse the SQLcl markers and refuse any privilege beyond the read allowlist."""
    if not isinstance(stdout, str):
        raise ProductionPrivilegeError("production privilege query returned non-text output")
    systems: set[str] = set()
    roles: set[str] = set()
    granted_roles: set[str] = set()
    objects: set[str] = set()
    owned_objects: set[str] = set()
    public_oracle_grants = 0
    seen_system_marker = False
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line.startswith("TEAM_PRIV|"):
            continue
        fields = line.split("|")
        kind = fields[1] if len(fields) > 1 else ""
        # OBJECT rows may carry "grantee|oracle_maintained|object_name|temporary".
        # A row without them is judged as a direct grant (fail-closed).
        expected_fields = {"OBJECT": (4, 8)}.get(kind, (3,))
        if len(fields) not in expected_fields or not fields[2].strip():
            raise ProductionPrivilegeError("production privilege query returned a malformed record")
        _marker, kind, value = fields[:3]
        value = value.strip().upper()
        if kind == "OBJECT" and len(fields) == 8:
            grantee, maintained, object_name, temporary = (field.strip().upper() for field in fields[4:8])
            if maintained not in {"Y", "N"} or temporary not in {"Y", "N"} or not grantee or not object_name:
                raise ProductionPrivilegeError("production privilege query returned a malformed record")
            if grantee == "PUBLIC" and maintained == "Y":
                if not _public_oracle_baseline(value, fields[3].strip().upper(), object_name, temporary):
                    raise ProductionPrivilegeError(
                        f"PUBLIC holds {value} on Oracle object {object_name}; revoke it from PUBLIC "
                        "for a read-only production account"
                    )
                public_oracle_grants += 1
                continue
        if kind == "SYSTEM":
            seen_system_marker = True
            systems.add(value)
        elif kind == "ROLE":
            roles.add(value)
        elif kind == "GRANTED":
            granted_roles.add(value)
        elif kind == "OBJECT":
            if value not in _READ_ONLY_OBJECT_PRIVILEGES:
                raise ProductionPrivilegeError(
                    f"production account has non-read-only object privilege(s): {value}"
                )
            object_type = fields[3].strip().upper()
            if object_type == "SEQUENCE":
                raise ProductionPrivilegeError(
                    "production account has a privilege on a SEQUENCE; NEXTVAL changes database state"
                )
            if object_type not in {"TABLE", "VIEW", "MATERIALIZED VIEW"}:
                raise ProductionPrivilegeError(
                    f"production account has a grant on unsupported object type: {object_type}"
                )
            objects.add(value)
        elif kind == "COLUMN":
            objects.add(value)
        elif kind == "OWNED":
            owned_objects.add(value)
        else:
            raise ProductionPrivilegeError(
                f"production privilege query returned an unknown record type: {kind}"
            )

    if not seen_system_marker or "CREATE SESSION" not in systems:
        raise ProductionPrivilegeError(
            "production privilege query is incomplete or the account lacks CREATE SESSION evidence"
        )
    # Only enabled roles contribute to the audited session privileges; a granted
    # role that is not enabled could be switched on later with SET ROLE.
    disabled_roles = sorted(granted_roles - roles)
    if disabled_roles:
        raise ProductionPrivilegeError(
            "production account holds role(s) not enabled in this session, whose privileges cannot be audited: "
            + ", ".join(disabled_roles)
            + "; revoke them or make them default roles"
        )
    unsafe_system = sorted(systems - _READ_ONLY_SYSTEM_PRIVILEGES)
    if unsafe_system:
        raise ProductionPrivilegeError(
            "production account has non-read-only system privilege(s): "
            + ", ".join(unsafe_system)
        )
    unsafe_objects = sorted(objects - _READ_ONLY_OBJECT_PRIVILEGES)
    if unsafe_objects:
        raise ProductionPrivilegeError(
            "production account has non-read-only object privilege(s): "
            + ", ".join(unsafe_objects)
        )
    if owned_objects:
        raise ProductionPrivilegeError(
            "production account has owned object(s) that can execute or change data: "
            + ", ".join(sorted(owned_objects))
        )
    return ProductionPrivilegeReport(
        tuple(sorted(systems)), tuple(sorted(roles)), tuple(sorted(objects)), tuple(),
        public_oracle_grants,
    )


def audit_production_profile(
    target: Target,
    *,
    repo: str | Path,
    runner: Callable[..., Any] = run_sqlcl,
) -> ProductionPrivilegeReport:
    """Query one production session's effective system and object privileges."""
    if target.environment != "production" or target.role != "production":
        raise ProductionPrivilegeError("privilege audit requires a production target")
    repo_path = Path(repo)
    driver = repo_path / "scripts" / "sql" / "production_privileges.sql"
    if driver.is_symlink() or not driver.is_file():
        raise ProductionPrivilegeError(f"production privilege query is missing or unsafe: {driver}")
    try:
        result = runner(
            target,
            "read",
            driver,
            repo_path / "scratch" / "production-privilege-audit" / target.connection,
        )
    except Exception as exc:  # noqa: BLE001
        raise ProductionPrivilegeError(
            f"production privilege audit failed for profile {target.connection}: {exc}"
        ) from exc
    return parse_production_privileges(getattr(result, "stdout", ""))
