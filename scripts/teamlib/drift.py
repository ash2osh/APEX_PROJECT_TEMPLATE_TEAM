"""Drift comparison against both the canonical inventory and the live frontier.

``check-drift`` answers two different questions and a target is only clean when
both agree: does the live schema match the reviewed canonical inventory in
``database/schema-inventory.json``, and does it match the last inventory the
metadata store actually accepted?  A target can pass the first and fail the
second when someone applied a migration outside the workflow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Config, Target, profile_target, schema_set_digest
from .fingerprints import (
    Inventory,
    InventoryError,
    diff_inventory,
    drift_is_clean,
    inventory_from_manifest,
    save_inventory,
)
from .live_inventory import inventory_target
from .migration_store import MigrationStoreError


def capture_live_inventory(repo: Path, config: Config) -> tuple[Inventory, Path]:
    """Capture the live inventory and save it as the actual-side evidence."""
    inventory = inventory_target(
        profile_target(config, "TABLES"),
        config.tables_schema,
        config.code_schema,
        repo / "scratch" / "live-inventory",
        schema_set_digest=schema_set_digest(config),
    )
    path = repo / "scratch" / "live-inventory.json"
    save_inventory(inventory, path)
    return inventory, path


def observed_frontier_drift(store: Any, metadata: Target, actual: Inventory) -> dict[str, Any]:
    """Compare the live inventory against the last accepted frontier."""
    try:
        state = store.read_state(metadata)
        observations = state.get("observations", [])
        if not observations:
            return {"status": "unknown", "reason": "no observed migration frontier is adopted"}
        frontier_manifest = store.read_inventories(metadata).get(observations[-1].get("after"))
        if not isinstance(frontier_manifest, dict):
            return {"status": "unknown", "reason": "accepted frontier manifest is missing"}
        frontier = inventory_from_manifest(frontier_manifest)
    except (MigrationStoreError, InventoryError) as exc:
        return {"status": "unknown", "reason": str(exc)}
    difference = diff_inventory(frontier, actual)
    return {
        "status": "clean" if drift_is_clean(difference) else "drift",
        "digest": frontier.digest,
        "diff": difference,
    }


def drift_status(canonical: dict[str, Any], frontier: dict[str, Any]) -> str:
    """Fold both comparisons into one status.

    ``unknown`` is not ``clean``: an unreadable frontier means the tooling
    cannot prove the target matches what it last accepted, and a caller that
    treated that as clean would apply migrations onto an unverified schema.
    """
    canonical_clean = drift_is_clean(canonical)
    if canonical_clean and frontier.get("status") == "clean":
        return "clean"
    if frontier.get("status") == "drift" or not canonical_clean:
        return "drift"
    return "unknown"
