"""Explicit non-production adapter for applying a verified release artifact."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping

from .config import ConfigError, Target, load_config, parse_target_contract, profile_target
from .control_store import SqlControlStore
from .deploy import deploy_app
from .live_inventory import inventory_target
from .migrate import apply_plan
from .migration_store import SqlMigrationStore
from .release import (
    ApplyReport,
    ReleaseError,
    ReleasePlan,
    apply_release,
    release_app_trees,
    release_migration_files,
    verify_release,
)
from .sqlcl import run_sqlcl


class ReleaseAdapterError(RuntimeError):
    pass


def _target_from_contract(path: str | Path, alias: str, *, expected_role: str | None = None) -> Target:
    contract = parse_target_contract(path, expected_role=expected_role)
    if contract.role not in {"integration", "test", "replay"} or contract.environment == "production":
        raise ReleaseAdapterError("release application targets must be non-production integration, test or replay contracts")
    if alias not in contract.app_ids:
        raise ReleaseAdapterError(f"release target has no application binding for {alias}")
    binding = dict(contract.binding)
    connection = binding.get("connection") or binding.get("sqlcl_connection")
    if not isinstance(connection, str) or not connection:
        raise ReleaseAdapterError("release target binding must provide a credential-free SQLcl connection")
    binding.setdefault("profile", contract.role.upper())
    binding.setdefault("alias", alias)
    binding.setdefault("app_id", contract.app_ids[alias])
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    return Target(
        project=contract.project,
        role=contract.role,
        environment=contract.environment,
        connection=connection,
        instance_id=contract.instance_id or "",
        db_name=contract.db_name or "",
        service=contract.service or "",
        session_user=contract.session_user or "",
        current_schema=contract.current_schema or "",
        alias=alias,
        workspace_id=contract.workspace_id,
        app_id=contract.app_ids[alias],
        parsing_schema=binding.get("parsing_schema"),
        ownership_mode=str(binding.get("ownership_mode", "shared")),
        binding_digest=digest,
    )


def apply_verified_release(
    release_tar: str | Path,
    target_contract: str | Path,
    env_file: str | Path,
    plan: ReleasePlan,
    history: Mapping[str, Any],
    *,
    repo: str | Path = ".",
    root: str | Path | None = None,
) -> ApplyReport:
    """Apply one planned release through non-production SQLcl adapters only."""
    contract = parse_target_contract(target_contract)
    if contract.environment == "production":
        raise ReleaseAdapterError("production release application is refused; generate a runbook")
    config = load_config(env_file, require_verify=True)
    if config.environment == "production" or config.role != contract.role:
        raise ReleaseAdapterError("release environment and target contract role do not match a non-production target")
    manifest = verify_release(release_tar)
    repo_path = Path(repo)
    target_path = Path(target_contract)
    if target_path.is_symlink() or not target_path.is_file():
        raise ReleaseAdapterError(f"release target contract is not a regular file: {target_path}")
    try:
        target_document = json.loads(target_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseAdapterError("release target contract is unreadable") from exc
    if not isinstance(target_document, Mapping):
        raise ReleaseAdapterError("release target contract must contain an object")
    # plan-release hashes the complete target JSON.  Preserve that exact
    # document through apply-release; a reduced identity-only mapping would
    # make every legitimate plan fail its stale-target check.
    target_document = dict(target_document)
    if target_document.get("role") != contract.role or target_document.get("environment") != contract.environment:
        raise ReleaseAdapterError("release target contract changed while loading")
    state_root = Path(root) if root is not None else repo_path / ".sync-state" / "release"
    state_root.mkdir(parents=True, exist_ok=True)
    metadata = profile_target(config, "METADATA")
    if metadata.environment == "production":
        raise ReleaseAdapterError("metadata profile is classified as production")
    if contract.instance_id != metadata.instance_id:
        raise ReleaseAdapterError("release target contract and metadata profile identify different instances")
    migration_store = SqlMigrationStore(metadata, work_root=state_root / "metadata")
    schema_set_digest = hashlib.sha256(
        f"{config.tables_schema}|{config.code_schema}|{config.metadata_schema}".encode("ascii")
    ).hexdigest()
    migration_store.bootstrap(metadata, schema_set_digest="release")
    control_store = SqlControlStore(metadata, work_root=state_root / "metadata")
    apps = release_app_trees(release_tar)
    app_targets = []
    for alias in apps:
        target = _target_from_contract(target_contract, alias, expected_role=contract.role)
        apex_profile = profile_target(config, "APEX", alias=alias)
        if target.connection != apex_profile.connection:
            raise ReleaseAdapterError(f"release target binding does not match the APEX profile for {alias}")
        for field in ("instance_id", "db_name", "service", "session_user", "current_schema"):
            if getattr(target, field) != getattr(apex_profile, field):
                raise ReleaseAdapterError(f"release target binding does not match APEX profile {field} for {alias}")
        app_targets.append(target)
    control_store.setup_state(app_targets)

    with tempfile.TemporaryDirectory(prefix="team-release-apply-") as directory:
        work = Path(directory)
        migration_root = work / "migrations"
        migration_root.mkdir()
        for relative, data in release_migration_files(release_tar).items():
            (migration_root / relative).write_bytes(data)

        def apply_migrations(_pending: tuple[Mapping[str, Any], ...], reviewed: ReleasePlan) -> None:
            def execute(migration):
                target = profile_target(config, "TABLES" if migration.target == "tables" else "CODE")
                run_sqlcl(target, "write", migration.sql_path, work)

            def verify(migration):
                if migration.verify_path is not None and migration.verify_bytes.strip():
                    run_sqlcl(profile_target(config, "VERIFY"), "read", migration.verify_path, work)
                return True

            def observe(_migration, phase):
                return inventory_target(
                    profile_target(config, "TABLES"),
                    config.tables_schema,
                    config.code_schema,
                    work / "inventory" / phase,
                    schema_set_digest=schema_set_digest,
                )

            apply_plan(
                migration_root,
                {
                    "target": metadata,
                    "store": migration_store,
                    "bootstrap": False,
                    "execute": execute,
                    "verify": verify,
                    "observe": observe,
                    "require_observation": True,
                    "schema_set_digest": schema_set_digest,
                    "source_commit": manifest.source_commit,
                    "applied_by": "release-adapter",
                },
                expected_plan={"pending": reviewed.pending},
            )

        def deploy_application(alias: str, tree: Mapping[str, bytes], reviewed: ReleasePlan) -> None:
            target = _target_from_contract(target_contract, alias, expected_role=contract.role)
            deploy_app(
                target,
                tree,
                manifest.source_commit,
                {"verified": True, "target_key": target.physical_key},
                repo=repo_path,
                root=state_root / "application",
                control_store=control_store,
            )

        return apply_release(
            release_tar,
            target_document,
            plan,
            history=history,
            apply_migrations=apply_migrations,
            deploy_application=deploy_application,
        )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="apply-release")
    parser.add_argument("archive")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--history", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--env", default=None, help="environment profile file")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--state-root")
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] == "apply-release":
        raw_args = raw_args[1:]
    args = parser.parse_args(raw_args)
    env_file = args.env or os.environ.get("TEAM_ENV_FILE") or os.environ.get("PROJECT_ENV_FILE") or ".env"
    if not Path(env_file).is_file():
        raise SystemExit(f"environment profile file not found: {env_file}")
    try:
        history_raw = json.loads(Path(args.history).read_text(encoding="utf-8"))
        plan_raw = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        history = history_raw.get("history", history_raw) if isinstance(history_raw, Mapping) else {}
        target = plan_raw.get("target", {}) if isinstance(plan_raw, Mapping) else {}
        plan = ReleasePlan(
            plan_raw["archive_digest"], plan_raw["target_digest"], tuple(plan_raw.get("pending", ())),
            plan_raw["artifact_history_digest"], target, plan_raw.get("history_digest", ""),
        )
        report = apply_verified_release(
            args.archive, args.target, env_file, plan, history,
            repo=args.repo, root=args.state_root,
        )
        print(json.dumps({"status": report.status, "pending": report.pending, "archive_digest": report.archive_digest}, sort_keys=True))
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ConfigError, ReleaseError, ReleaseAdapterError, ValueError) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
