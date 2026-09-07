"""Canonical structural inventory and drift comparison."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping


class InventoryError(ValueError):
    pass


SUPPORTED_OBJECT_TYPES = {
    "TABLE", "CONSTRAINT", "INDEX", "VIEW", "PACKAGE", "PACKAGE_BODY",
    "PROCEDURE", "FUNCTION", "TRIGGER", "SEQUENCE", "TYPE", "TYPE_BODY",
    "SYNONYM", "GRANT",
}
_RESERVED_PREFIX = re.compile(r"^TEAM_(?:MIGRATION_|APP_|CONTROL_)")


@dataclass(frozen=True)
class Inventory:
    version: int
    topology: str
    normalizer_version: str
    coverage_version: str
    objects: dict[str, str]
    invalid: tuple[str, ...] = ()
    schema_set_digest: str = ""

    @property
    def digest(self) -> str:
        payload = {
            "version": self.version,
            "topology": self.topology,
            "normalizer_version": self.normalizer_version,
            "coverage_version": self.coverage_version,
            "objects": self.objects,
            "invalid": self.invalid,
            "schema_set_digest": self.schema_set_digest,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        """Return the complete, stable manifest stored as migration evidence."""
        return {
            "version": self.version,
            "topology": self.topology,
            "normalizer_version": self.normalizer_version,
            "coverage_version": self.coverage_version,
            "schema_set_digest": self.schema_set_digest,
            "objects": dict(self.objects),
            "invalid": list(self.invalid),
            "inventory_digest": self.digest,
        }


def _normalize_text(value: Any) -> str:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InventoryError("object definition is not UTF-8") from exc
    if isinstance(value, Mapping):
        value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if not isinstance(value, str):
        value = str(value)
    return value.replace("\r\n", "\n").replace("\r", "\n")


def inventory_from_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    topology: str = "separate",
    schema_set_digest: str = "",
    normalizer_version: str = "inventory-v1",
    coverage_version: str = "coverage-v1",
) -> Inventory:
    if topology not in {"shared", "separate"}:
        raise InventoryError("inventory topology must be shared or separate")
    objects: dict[str, str] = {}
    invalid: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise InventoryError("inventory row is not an object")
        owner = row.get("logical_owner", row.get("owner"))
        object_type = row.get("object_type")
        name = row.get("object_name")
        if not all(isinstance(value, str) and value for value in (owner, object_type, name)):
            raise InventoryError("inventory row identity is incomplete")
        object_type = object_type.upper()
        key = f"{owner}|{object_type}|{name}"
        if _RESERVED_PREFIX.match(name.split("@", 1)[0].upper()):
            raise InventoryError(f"reserved controller object appears in application inventory: {key}")
        if object_type not in SUPPORTED_OBJECT_TYPES:
            raise InventoryError(f"unsupported schema object class: {object_type}")
        supplied_digest = row.get("definition_digest")
        if supplied_digest is not None:
            if not isinstance(supplied_digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", supplied_digest):
                raise InventoryError(f"invalid supplied definition digest: {key}")
            digest = supplied_digest.casefold()
        else:
            definition = _normalize_text(row.get("definition", ""))
            digest = hashlib.sha256(definition.encode("utf-8")).hexdigest()
        # Volatile dictionary fields are diagnostics, never part of the
        # structural fingerprint.  Status remains independently reportable.
        if key in objects:
            raise InventoryError(f"duplicate inventory object: {key}")
        objects[key] = digest
        if str(row.get("status", "VALID")).upper() not in {"VALID", "ENABLED", ""}:
            invalid.append(key)
    return Inventory(1, topology, normalizer_version, coverage_version, dict(sorted(objects.items())), tuple(sorted(invalid)), schema_set_digest)


def diff_inventory(expected: Inventory, actual: Inventory) -> dict[str, Any]:
    if expected.topology != actual.topology or expected.normalizer_version != actual.normalizer_version or expected.coverage_version != actual.coverage_version:
        return {"added": (), "missing": (), "changed": (), "invalid": tuple(sorted(set(actual.objects))), "topology_mismatch": True}
    expected_keys = set(expected.objects)
    actual_keys = set(actual.objects)
    return {
        "added": tuple(sorted(actual_keys - expected_keys)),
        "missing": tuple(sorted(expected_keys - actual_keys)),
        "changed": tuple(sorted(key for key in expected_keys & actual_keys if expected.objects[key] != actual.objects[key])),
        "invalid": tuple(sorted(actual.invalid)),
        "topology_mismatch": False,
    }


def save_inventory(inventory: Inventory, path: str | Path) -> None:
    data = inventory.as_dict()
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")


def load_inventory(path: str | Path) -> Inventory:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InventoryError("inventory evidence is unreadable") from exc
    try:
        inventory = Inventory(
            int(data["version"]), data["topology"], data["normalizer_version"],
            data["coverage_version"], dict(data["objects"]), tuple(data.get("invalid", [])),
            data.get("schema_set_digest", ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InventoryError("inventory evidence is malformed") from exc
    if data.get("inventory_digest") != inventory.digest:
        raise InventoryError("inventory digest does not match evidence")
    return inventory


def inventory_from_manifest(data: Mapping[str, Any]) -> Inventory:
    """Reconstruct and verify an inventory manifest from durable evidence."""
    try:
        inventory = Inventory(
            int(data["version"]),
            data["topology"],
            data["normalizer_version"],
            data["coverage_version"],
            dict(data["objects"]),
            tuple(data.get("invalid", [])),
            data.get("schema_set_digest", ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise InventoryError("inventory manifest is malformed") from exc
    if data.get("inventory_digest") != inventory.digest:
        raise InventoryError("inventory manifest digest does not match evidence")
    return inventory


def snapshot(profiles: Mapping[str, Any], out: str | Path | None = None) -> Inventory:
    """Build an offline inventory from pre-qualified dictionary rows.

    A live caller supplies rows captured through the SQLcl adapter; this pure
    function deliberately cannot turn an unframed or empty command output into
    an empty schema.
    """
    rows = profiles.get("rows") if isinstance(profiles, Mapping) else None
    if rows is None:
        raise InventoryError("snapshot requires qualified inventory rows")
    inventory = inventory_from_rows(rows, topology=str(profiles.get("topology", "separate")), schema_set_digest=str(profiles.get("schema_set_digest", "")))
    if out is not None:
        save_inventory(inventory, out)
    return inventory


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="snapshot")
    parser.add_argument("--rows", required=True, help="JSON file containing qualified rows")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(list(argv or []))
    data = json.loads(Path(args.rows).read_text(encoding="utf-8"))
    snapshot(data, args.out)
    return 0
