"""Read-only schema inventory through the common qualified SQLcl adapter."""

from __future__ import annotations

from dataclasses import replace
import base64
import hashlib
from pathlib import Path
import re
from typing import Any
from collections.abc import Callable

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


def _marked_lines(stdout: str, prefix: str) -> list[str]:
    """Join SQLcl terminal-wrapped marker rows without accepting diagnostics."""
    rows: list[str] = []
    pending: str | None = None
    for raw in stdout.splitlines():
        line = raw.strip().rstrip("\r")
        if line.startswith(prefix):
            if pending is not None:
                rows.append(pending)
            pending = line
        elif pending is not None and re.fullmatch(r"[A-Za-z0-9+/=]+", line):
            pending += line
        elif pending is not None:
            rows.append(pending)
            pending = None
    if pending is not None:
        rows.append(pending)
    return rows


def _v2_rows(stdout: str, tables_schema: str, code_schema: str) -> tuple[list[dict[str, str]], str]:
    unsupported = [
        line.strip()
        for line in stdout.splitlines()
        if line.strip().startswith("TEAM_INVENTORY_UNSUPPORTED|")
    ]
    if unsupported:
        raise InventoryError("unsupported live inventory object class: " + unsupported[0])
    begins = [line.strip() for line in stdout.splitlines() if line.strip().startswith("TEAM_INVENTORY_BEGIN|")]
    ends = [line.strip() for line in stdout.splitlines() if line.strip().startswith("TEAM_INVENTORY_END|")]
    if len(begins) != 1 or len(ends) != 1:
        raise InventoryError("live inventory is missing its begin/count/end framing")
    begin_fields = dict(part.split("=", 1) for part in begins[0].split("|")[1:] if "=" in part)
    end_fields = dict(part.split("=", 1) for part in ends[0].split("|")[1:] if "=" in part)
    if begin_fields.get("version") != "2" or begin_fields.get("topology") not in {"shared", "separate"}:
        raise InventoryError("unsupported live inventory manifest version")
    try:
        expected_count = int(begin_fields["count"])
        ending_count = int(end_fields["count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InventoryError("live inventory manifest count is malformed") from exc
    if expected_count != ending_count or expected_count < 0:
        raise InventoryError("live inventory manifest count is empty or inconsistent")

    chunks: dict[tuple[str, str, str, str], dict[int, bytes]] = {}
    totals: dict[tuple[str, str, str, str], int] = {}
    for line in _marked_lines(stdout, "TEAM_INVENTORY_CHUNK|"):
        parts = line.split("|")
        if len(parts) != 8 or any(not part for part in parts[1:]):
            raise InventoryError("malformed live inventory definition chunk")
        _, owner, object_type, object_name, status, raw_part, raw_total, encoded = parts
        try:
            part = int(raw_part)
            total = int(raw_total)
            definition = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (ValueError, UnicodeError) as exc:
            raise InventoryError("live inventory definition chunk is not valid base64") from exc
        if part < 1 or total < part:
            raise InventoryError("live inventory definition chunk numbering is invalid")
        key = (owner, object_type, object_name, status)
        if part in chunks.setdefault(key, {}) or (key in totals and totals[key] != total):
            raise InventoryError(f"duplicate live inventory definition chunk: {key}")
        chunks[key][part] = definition
        totals[key] = total

    if len(chunks) != expected_count:
        raise InventoryError("live inventory object count does not match its framing")
    topology = begin_fields["topology"]
    rows: list[dict[str, str]] = []
    for (owner, object_type, object_name, status), pieces in sorted(chunks.items()):
        total = totals[(owner, object_type, object_name, status)]
        if set(pieces) != set(range(1, total + 1)):
            raise InventoryError(f"live inventory definition chunks are incomplete: {owner}|{object_type}|{object_name}")
        definition = b"".join(pieces[index] for index in range(1, total + 1))
        logical_owner = owner
        if topology == "shared" and owner in {tables_schema, code_schema}:
            logical_owner = "shared"
        elif topology == "separate":
            if owner == tables_schema:
                logical_owner = "tables"
            elif owner == code_schema:
                logical_owner = "code"
        rows.append({
            "logical_owner": logical_owner,
            "object_type": object_type,
            "object_name": object_name,
            "status": status,
            "definition_digest": hashlib.sha256(definition).hexdigest(),
        })
    return rows, topology


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
    if any(line.strip().startswith("TEAM_INVENTORY_BEGIN|") for line in stdout.splitlines()):
        rows, topology = _v2_rows(stdout, tables_schema, code_schema)
        schema_digest = hashlib.sha256(f"inventory-v2|{topology}|{tables_schema}|{code_schema}".encode()).hexdigest()
        return inventory_from_rows(rows, topology=topology, schema_set_digest=schema_digest)

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
    if not rows:
        raise InventoryError("live inventory output is unframed or empty")
    schema_digest = hashlib.sha256(f"{tables_schema}|{code_schema}".encode()).hexdigest()
    return inventory_from_rows(rows, topology="separate", schema_set_digest=schema_digest)


def inventory_target(
    target: Target,
    tables_schema: str,
    code_schema: str,
    work: str | Path,
    *,
    runner: Callable[..., Any] = run_sqlcl,
    schema_set_digest: str | None = None,
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
    driver_path.write_text(
        text.replace("__TABLES_SCHEMA__", tables_schema)
        .replace("__CODE_SCHEMA__", code_schema)
        .replace("__TOPOLOGY__", "shared" if tables_schema == code_schema else "separate"),
        encoding="utf-8",
        newline="\n",
    )
    result = runner(target, operation, driver_path, work)
    inventory = inventory_from_sqlcl_result(target, result, tables_schema, code_schema)
    if schema_set_digest is not None:
        if not re.fullmatch(r"[0-9a-f]{64}", schema_set_digest):
            raise InventoryError("schema-set digest must be a lowercase SHA-256")
        inventory = replace(inventory, schema_set_digest=schema_set_digest)
    return inventory
