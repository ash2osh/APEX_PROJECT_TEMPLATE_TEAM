"""Read-only schema inventory through the common qualified SQLcl adapter."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any, Callable, Mapping

from .config import Target
from .fingerprints import Inventory, InventoryError, inventory_from_rows
from .sqlcl import run_sqlcl


_IDENTITY = ("SESSION_USER", "CURRENT_SCHEMA", "DB_NAME", "SERVICE", "INSTANCE_ID")


def _identity_lines(stdout: str) -> list[dict[str, str]]:
    observations: list[dict[str, str]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("TEAM_IDENTITY|"):
            continue
        fields: dict[str, str] = {}
        for item in line.split("|")[1:]:
            if "=" not in item:
                raise InventoryError("malformed live inventory identity marker")
            key, value = item.split("=", 1)
            if key not in _IDENTITY or key in fields or not value:
                raise InventoryError("malformed live inventory identity marker")
            fields[key] = value
        if tuple(fields) != _IDENTITY:
            raise InventoryError("incomplete live inventory identity marker")
        observations.append(fields)
    return observations


def inventory_from_sqlcl_result(target: Target, result: Any, tables_schema: str, code_schema: str) -> Inventory:
    stdout = getattr(result, "stdout", "")
    if not isinstance(stdout, str):
        raise InventoryError("live inventory result has no text output")
    observations = _identity_lines(stdout)
    if len(observations) < 2 or any(item != observations[0] for item in observations[1:]):
        raise InventoryError("live inventory identity observations are incomplete or changed")
    expected = {
        "SESSION_USER": target.session_user, "CURRENT_SCHEMA": target.current_schema,
        "DB_NAME": target.db_name, "SERVICE": target.service, "INSTANCE_ID": target.instance_id,
    }
    if observations[0] != expected:
        raise InventoryError("live inventory identity does not match the target contract")
    rows: list[dict[str, str]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("TEAM_INVENTORY|"):
            continue
        parts = line.split("|")
        if len(parts) != 6 or any(not part for part in parts[1:]):
            raise InventoryError("malformed live inventory row")
        _, owner, object_type, object_name, status, digest = parts
        rows.append({"owner": owner, "object_type": object_type, "object_name": object_name, "status": status, "definition_digest": digest})
    schema_digest = hashlib.sha256(f"{tables_schema}|{code_schema}".encode("utf-8")).hexdigest()
    return inventory_from_rows(rows, topology="separate", schema_set_digest=schema_digest)


def inventory_target(
    target: Target,
    tables_schema: str,
    code_schema: str,
    work: str | Path,
    *,
    runner: Callable[..., Any] = run_sqlcl,
) -> Inventory:
    if target.environment == "production":
        operation = "read"
    else:
        operation = "read"
    if not re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", tables_schema) or not re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", code_schema):
        raise InventoryError("inventory schemas must be validated Oracle identifiers")
    template = Path(__file__).resolve().parents[1] / "sql" / "schema_inventory.sql"
    text = template.read_text(encoding="utf-8")
    driver_path = Path(work) / "schema-inventory.sql"
    Path(work).mkdir(parents=True, exist_ok=True)
    driver_path.write_text(text.replace("__TABLES_SCHEMA__", tables_schema).replace("__CODE_SCHEMA__", code_schema), encoding="utf-8", newline="\n")
    result = runner(target, operation, driver_path, work)
    return inventory_from_sqlcl_result(target, result, tables_schema, code_schema)

