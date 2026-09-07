"""Explicit non-production adapter for applying a verified release artifact."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping

from .config import ConfigError, Target, load_config, parse_target_contract, profile_target
from .control_store import ControlStore
from .deploy import deploy_app
from .migrate import apply_plan
from .migration_store import MigrationStore
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
    state_root = Path(root) if root is not None else repo_path / ".sync-state" / "release"
    state_root.mkdir(parents=True, exist_ok=True)
    metadata = profile_target(config, "METADATA")
    if metadata.environment == "production":
        raise ReleaseAdapterError("metadata profile is classified as production")
    migration_store = MigrationStore(state_root / "migration")
    migration_store.bootstrap(metadata, schema_set_digest="release")
    control_store = ControlStore(state_root / "application")
    apps = release_app_trees(release_tar)
    app_targets = [_target_from_contract(target_contract, alias, expected_role=contract.role) for alias in apps]
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

            apply_plan(
                migration_root,
                {
                    "target": metadata,
                    "store": migration_store,
                    "bootstrap": False,
                    "execute": execute,
                    "verify": verify,
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
            {
                "role": contract.role,
                "environment": contract.environment,
                "instance_id": contract.instance_id,
                "workspace_id": contract.workspace_id,
                "app_ids": dict(contract.app_ids),
            },
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
    parser.add_argument("--env", required=True)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--state-root")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
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
            args.archive, args.target, args.env, plan, history,
            repo=args.repo, root=args.state_root,
        )
        print(json.dumps({"status": report.status, "pending": report.pending, "archive_digest": report.archive_digest}, sort_keys=True))
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ConfigError, ReleaseError, ReleaseAdapterError, ValueError) as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
