"""Shared construction of the live migration callbacks and profile mapping.

Every caller of :func:`teamlib.migrate.apply_plan` -- the ``migrate`` CLI, the
protected integration run, and both release-apply paths -- needs the same three
SQLcl callbacks and the same profile mapping.  Four hand-copied copies of that
mapping were four chances for one of them to quietly drop ``require_observation``
or pass a different schema-set digest, so it is built here once and the callers
supply only what genuinely differs: the scratch root, the source commit and the
``applied_by`` label that lands in the recorded history event.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .assertions import run_verification_member
from .config import Config, Target, profile_target, schema_set_digest
from .live_inventory import inventory_target
from .sqlcl import run_sqlcl


def migration_callbacks(config: Config, work_root: str | Path, digest: str):
    """Build the execute/verify/observe trio rooted at one scratch directory.

    Each callback keys its working directory by action/phase *and* migration ID
    so that two migrations in one run cannot overwrite each other's evidence.
    """
    root = Path(work_root)

    def execute(migration, action, sql_path):
        target = profile_target(config, "TABLES" if migration.target == "tables" else "CODE")
        run_sqlcl(target, "write", sql_path, root / "migration-payload" / action / migration.id)

    def verify(migration, action, verify_path):
        run_verification_member(
            profile_target(config, "VERIFY"),
            verify_path,
            root / "migration-verify" / action / migration.id,
            runner=run_sqlcl,
        )
        return True

    def observe(migration, phase):
        return inventory_target(
            profile_target(config, "TABLES"),
            config.tables_schema,
            config.code_schema,
            root / "migration-observation" / phase / migration.id,
            schema_set_digest=digest,
        )

    return execute, verify, observe


def migration_profiles(
    config: Config,
    metadata: Target,
    store: Any,
    work_root: str | Path,
    *,
    source_commit: str,
    applied_by: str,
    dry_run: bool = False,
    bootstrap: bool = False,
    verified_inventory_digest: str | None = None,
) -> dict[str, Any]:
    """Build the mapping apply_plan/apply_undo/apply_redo consume.

    ``require_observation`` is not a parameter on purpose: every live caller
    must record a before/after inventory, and making it optional here would let
    one caller silently opt out of the observation chain.
    """
    digest = schema_set_digest(config)
    execute, verify, observe = migration_callbacks(config, work_root, digest)
    return {
        "store": store,
        "target": metadata,
        "payload_targets": {
            "tables": profile_target(config, "TABLES"),
            "code": profile_target(config, "CODE"),
        },
        "dry_run": dry_run,
        "bootstrap": bootstrap,
        "schema_set_digest": digest,
        "verified_inventory_digest": verified_inventory_digest,
        "require_observation": True,
        "execute": execute,
        "verify": verify,
        "observe": observe,
        "source_commit": source_commit,
        "applied_by": applied_by,
    }
