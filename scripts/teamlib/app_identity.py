"""Qualified APEX 26.1+ application identity and version observation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tempfile
from typing import Any
from collections.abc import Callable

from .config import Target
from .sqlcl import run_sqlcl


class AppIdentityError(RuntimeError):
    """Raised when an application's live identity or APEX version cannot be verified."""


MINIMUM_APEX_VERSION: tuple[int, ...] = (26, 1)


@dataclass(frozen=True)
class AppIdentity:
    status: str  # "PRESENT", "ABSENT", "UNKNOWN"
    app_id: int | None
    workspace_id: int | None
    parsing_schema: str | None
    workspace_schemas: frozenset[str]
    apex_version: str | None


def parse_version(version: str | None) -> tuple[int, ...]:
    if not version or not isinstance(version, str):
        raise AppIdentityError(f"unparseable APEX version: {version!r}")
    cleaned = version.strip()
    match = re.match(r"^([0-9]+(?:\.[0-9]+)*)", cleaned)
    if not match:
        raise AppIdentityError(f"unparseable APEX version: {version!r}")
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError as exc:
        raise AppIdentityError(f"unparseable APEX version: {version!r}") from exc


def require_apex_version(version: str | None) -> None:
    if not version:
        raise AppIdentityError("APEX version is unknown")
    parsed = parse_version(version)
    if parsed < MINIMUM_APEX_VERSION:
        raise AppIdentityError(
            f"APEX version {version} is below required minimum 26.1"
        )


def require_app_identity(
    target: Target,
    observed: AppIdentity,
    *,
    allow_absent: bool = False,
) -> None:
    require_apex_version(observed.apex_version)

    if observed.status == "UNKNOWN":
        raise AppIdentityError("application identity is UNKNOWN; state cannot be verified")

    if observed.status == "ABSENT":
        if not allow_absent:
            raise AppIdentityError(
                f"application {target.alias} (ID {target.app_id}) is absent from target database"
            )
        if not target.parsing_schema or target.parsing_schema.upper() not in observed.workspace_schemas:
            raise AppIdentityError(
                f"parsing schema {target.parsing_schema} is not assigned to workspace {target.workspace_id}"
            )
        return

    if observed.status == "PRESENT":
        if observed.app_id != target.app_id:
            raise AppIdentityError(
                f"observed app ID {observed.app_id} does not match target {target.app_id}"
            )
        if observed.workspace_id != target.workspace_id:
            raise AppIdentityError(
                f"observed workspace ID {observed.workspace_id} does not match target {target.workspace_id}"
            )
        if (
            not observed.parsing_schema
            or not target.parsing_schema
            or observed.parsing_schema.upper() != target.parsing_schema.upper()
        ):
            raise AppIdentityError(
                f"observed parsing schema {observed.parsing_schema} does not match target {target.parsing_schema}"
            )
        if target.parsing_schema.upper() not in observed.workspace_schemas:
            raise AppIdentityError(
                f"parsing schema {target.parsing_schema} is not assigned to workspace {target.workspace_id}"
            )
        return

    raise AppIdentityError(f"unsupported observed app status: {observed.status}")


def observe_app_identity(
    target: Target,
    runner: Callable[..., Any] = run_sqlcl,
    *,
    work_dir: str | Path | None = None,
) -> AppIdentity:
    if target.workspace_id is None or target.app_id is None:
        return AppIdentity(
            status="UNKNOWN",
            app_id=None,
            workspace_id=None,
            parsing_schema=None,
            workspace_schemas=frozenset(),
            apex_version=None,
        )

    # Use fixed whitelisted SQL with explicit integer conversions
    ws_id = int(target.workspace_id)
    app_id = int(target.app_id)
    driver_text = (
        "SET DEFINE OFF\n"
        "SET HEADING OFF\n"
        "SET FEEDBACK OFF\n"
        "SET PAGESIZE 0\n"
        "SELECT 'TEAM_APP_ID_APEX_VERSION|' || version_no FROM apex_release;\n"
        f"SELECT 'TEAM_APP_ID_WS_SCHEMA|' || workspace_id || '|' || schema FROM apex_workspace_schemas WHERE workspace_id = {ws_id};\n"
        f"SELECT 'TEAM_APP_ID_APP|' || workspace_id || '|' || application_id || '|' || owner FROM apex_applications WHERE application_id = {app_id};\n"
    )

    with tempfile.TemporaryDirectory(prefix="team-app-identity-") as temp_dir:
        root = Path(work_dir) if work_dir is not None else Path(temp_dir)
        driver_path = root / "app_identity_probe.sql"
        driver_path.write_text(driver_text, encoding="utf-8", newline="\n")

        try:
            result = runner(target, "read", driver_path, root)
        except Exception:
            return AppIdentity(
                status="UNKNOWN",
                app_id=None,
                workspace_id=None,
                parsing_schema=None,
                workspace_schemas=frozenset(),
                apex_version=None,
            )

    stdout = getattr(result, "stdout", "") or ""
    apex_version: str | None = None
    workspace_schemas: set[str] = set()
    apps: list[tuple[int, int, str]] = []

    for raw_line in stdout.splitlines():
        line = raw_line.strip().rstrip("\r")
        if line.startswith("TEAM_APP_ID_APEX_VERSION|"):
            parts = line.split("|", 1)
            if len(parts) == 2 and parts[1].strip():
                apex_version = parts[1].strip()
        elif line.startswith("TEAM_APP_ID_WS_SCHEMA|"):
            parts = line.split("|")
            if len(parts) == 3 and parts[2].strip():
                workspace_schemas.add(parts[2].strip().upper())
        elif line.startswith("TEAM_APP_ID_APP|"):
            parts = line.split("|")
            if len(parts) == 4:
                try:
                    row_ws = int(parts[1].strip())
                    row_app = int(parts[2].strip())
                    row_owner = parts[3].strip().upper()
                    apps.append((row_ws, row_app, row_owner))
                except (ValueError, TypeError):
                    continue

    if len(apps) > 1:
        return AppIdentity(
            status="UNKNOWN",
            app_id=None,
            workspace_id=None,
            parsing_schema=None,
            workspace_schemas=frozenset(workspace_schemas),
            apex_version=apex_version,
        )

    if len(apps) == 1:
        row_ws, row_app, row_owner = apps[0]
        return AppIdentity(
            status="PRESENT",
            app_id=row_app,
            workspace_id=row_ws,
            parsing_schema=row_owner,
            workspace_schemas=frozenset(workspace_schemas),
            apex_version=apex_version,
        )

    # 0 app rows found
    if len(workspace_schemas) > 0:
        return AppIdentity(
            status="ABSENT",
            app_id=None,
            workspace_id=ws_id,
            parsing_schema=None,
            workspace_schemas=frozenset(workspace_schemas),
            apex_version=apex_version,
        )

    return AppIdentity(
        status="UNKNOWN",
        app_id=None,
        workspace_id=None,
        parsing_schema=None,
        workspace_schemas=frozenset(),
        apex_version=apex_version,
    )
