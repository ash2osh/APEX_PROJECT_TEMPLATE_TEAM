"""Explicit non-production adapter for applying a verified release artifact."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
from collections.abc import Mapping
from dataclasses import dataclass

from .assertions import run_verification_member
from .config import Config, ConfigError, Target, load_config, parse_target_contract, profile_target
from .control_store import SqlControlStore
from .deploy import deploy_app
from .live_inventory import inventory_target
from .migrate import apply_plan
from .migration_store import SqlMigrationStore
from .release import (
    ApplyReport,
    Manifest,
    ReleaseError,
    ReleasePlan,
    apply_release,
    release_app_trees,
    release_migration_files,
    plan_release,
    verify_release,
)
from .sqlcl import run_sqlcl


class ReleaseAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseApplyContext:
    manifest: Manifest
    target_document: Mapping[str, Any]
    config: Config
    metadata: Target
    migration_store: SqlMigrationStore
    control_store: SqlControlStore
    schema_set_digest: str
    app_targets: tuple[Target, ...]


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


def _release_target_document(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ReleaseAdapterError(f"release target contract is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseAdapterError("release target contract is unreadable") from exc
    if not isinstance(value, Mapping):
        raise ReleaseAdapterError("release target contract must contain an object")
    return dict(value)


def _validated_release_context(
    release_tar: str | Path,
    target_contract: str | Path,
    config: Config,
    *,
    repo: Path,
    root: Path | None,
) -> ReleaseApplyContext:
    if config.role != "test" or config.environment != "test":
        raise ReleaseAdapterError("release-test requires the protected test target")
    if repo.is_symlink() or not repo.is_dir():
        raise ReleaseAdapterError(f"release repository is not a real directory: {repo}")
    manifest = verify_release(release_tar)
    target_path = Path(target_contract)
    target_document = _release_target_document(target_path)
    try:
        contract = parse_target_contract(target_document, expected_role="test")
    except ConfigError as exc:
        raise ReleaseAdapterError(str(exc)) from exc
    if contract.environment != "test":
        raise ReleaseAdapterError("release target contract must use the test environment")
    for field in ("project", "role", "environment"):
        if getattr(contract, field) != getattr(config, field):
            raise ReleaseAdapterError(f"release target contract does not match config {field}")
    metadata = profile_target(config, "METADATA")
    if contract.instance_id != metadata.instance_id:
        raise ReleaseAdapterError("release target contract and metadata profile identify different instances")
    if contract.db_name != metadata.db_name or contract.service != metadata.service:
        raise ReleaseAdapterError("release target contract and metadata profile identify different database services")
    if contract.workspace_id != config.workspace_id:
        raise ReleaseAdapterError("release target contract workspace does not match configuration")
    if dict(contract.app_ids) != dict(config.apps):
        raise ReleaseAdapterError("release target contract application bindings differ from configuration")
    if set(manifest.app_tree_digests) != set(contract.app_ids):
        raise ReleaseAdapterError("release archive and test target application bindings differ")
    if target_document.get("role") != contract.role or target_document.get("environment") != contract.environment:
        raise ReleaseAdapterError("release target contract changed while loading")
    state_root = root if root is not None else repo / ".sync-state" / "release"
    migration_store = SqlMigrationStore(metadata, work_root=state_root / "metadata")
    control_store = SqlControlStore(metadata, work_root=state_root / "metadata")
    app_targets: list[Target] = []
    for alias in sorted(contract.app_ids):
        target = _target_from_contract(target_path, alias, expected_role="test")
        apex_profile = profile_target(config, "APEX", alias=alias)
        for field in (
            "connection", "instance_id", "db_name", "service", "session_user",
            "current_schema", "workspace_id", "app_id", "parsing_schema", "ownership_mode",
        ):
            if getattr(target, field) != getattr(apex_profile, field):
                raise ReleaseAdapterError(f"release target binding does not match APEX profile {field} for {alias}")
        app_targets.append(target)
    schema_set_digest = hashlib.sha256(
        f"{config.tables_schema}|{config.code_schema}|{config.metadata_schema}".encode("ascii")
    ).hexdigest()
    return ReleaseApplyContext(
        manifest=manifest,
        target_document=target_document,
        config=config,
        metadata=metadata,
        migration_store=migration_store,
        control_store=control_store,
        schema_set_digest=schema_set_digest,
        app_targets=tuple(app_targets),
    )


def _apply_release_context(
    context: ReleaseApplyContext,
    release_tar: str | Path,
    plan: ReleasePlan,
    history: Mapping[str, Any],
    *,
    repo: Path,
) -> ApplyReport:
    config = context.config
    with tempfile.TemporaryDirectory(prefix="team-release-apply-") as directory:
        work = Path(directory)
        migration_root = work / "migrations"
        migration_root.mkdir()
        for relative, data in release_migration_files(release_tar).items():
            (migration_root / relative).write_bytes(data)

        def apply_migrations(_pending: tuple[Mapping[str, Any], ...], reviewed: ReleasePlan) -> None:
            def execute(migration, action, sql_path):
                target = profile_target(config, "TABLES" if migration.target == "tables" else "CODE")
                run_sqlcl(target, "write", sql_path, work / "payload" / action / migration.id)

            def verify(migration, action, verify_path):
                run_verification_member(
                    profile_target(config, "VERIFY"),
                    verify_path,
                    work / "verify" / action / migration.id,
                    runner=run_sqlcl,
                )
                return True

            def observe(_migration, phase):
                return inventory_target(
                    profile_target(config, "TABLES"),
                    config.tables_schema,
                    config.code_schema,
                    work / "inventory" / phase,
                    schema_set_digest=context.schema_set_digest,
                )

            apply_plan(
                migration_root,
                {
                    "target": context.metadata,
                    "payload_targets": {
                        "tables": profile_target(config, "TABLES"),
                        "code": profile_target(config, "CODE"),
                    },
                    "store": context.migration_store,
                    "bootstrap": False,
                    "execute": execute,
                    "verify": verify,
                    "observe": observe,
                    "require_observation": True,
                    "schema_set_digest": context.schema_set_digest,
                    "source_commit": context.manifest.source_commit,
                    "applied_by": "release-test",
                },
                expected_plan={"pending": reviewed.pending},
            )

        target_by_alias = {target.alias: target for target in context.app_targets}

        def deploy_application(alias: str, tree: Mapping[str, bytes], _reviewed: ReleasePlan) -> None:
            target = target_by_alias.get(alias)
            if target is None:
                raise ReleaseAdapterError(f"release application target is missing: {alias}")
            deploy_app(
                target,
                tree,
                context.manifest.source_commit,
                {"verified": True, "target_key": target.physical_key},
                repo=repo,
                root=(repo / ".sync-state" / "release" / "application"),
                control_store=context.control_store,
            )

        try:
            return apply_release(
                release_tar,
                context.target_document,
                plan,
                history=history,
                apply_migrations=apply_migrations,
                deploy_application=deploy_application,
                target_state_key=context.metadata.state_key,
            )
        except ReleaseAdapterError:
            raise
        except ReleaseError:
            raise
        except Exception as exc:
            raise ReleaseAdapterError(f"release application failed: {exc}") from exc


def apply_verified_release_live(
    release_tar: str | Path,
    target_contract: str | Path,
    config: Config,
    *,
    repo: str | Path = ".",
    root: str | Path | None = None,
) -> ApplyReport:
    """Apply a verified release against the live test metadata history."""
    context = _validated_release_context(
        release_tar,
        target_contract,
        config,
        repo=Path(repo),
        root=Path(root) if root is not None else None,
    )
    context.migration_store.bootstrap(
        context.metadata,
        schema_set_digest=context.schema_set_digest,
    )
    history = context.migration_store.read_history(context.metadata)
    plan = plan_release(release_tar, history, context.target_document)
    pending_by_id = {
        item["id"]: item
        for item in context.manifest.migrations
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    missing = [migration_id for migration_id in plan.pending if migration_id not in pending_by_id]
    if missing:
        raise ReleaseAdapterError("release plan references missing migration metadata: " + ", ".join(missing))
    destructive = tuple(
        migration_id
        for migration_id in plan.pending
        if bool(pending_by_id[migration_id].get("destructive"))
    )
    if destructive:
        raise ReleaseAdapterError(
            "release-test requires reviewed destructive maintenance before apply: "
            + ", ".join(destructive)
        )
    context.control_store.setup_state(context.app_targets)
    return _apply_release_context(
        context,
        release_tar,
        plan,
        history,
        repo=Path(repo),
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
    # The digest must be the one the live inventories carry. A placeholder here
    # is written into TEAM_MIGRATION_META and makes every record_inventory in
    # this run -- and every later `migrate` against the same metadata schema --
    # fail with ORA-20011 SCHEMA_SET_DIGEST_MISMATCH.
    migration_store.bootstrap(metadata, schema_set_digest=schema_set_digest)
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
            def execute(migration, action, sql_path):
                target = profile_target(config, "TABLES" if migration.target == "tables" else "CODE")
                run_sqlcl(target, "write", sql_path, work / "payload" / action / migration.id)

            def verify(migration, action, verify_path):
                run_verification_member(
                    profile_target(config, "VERIFY"),
                    verify_path,
                    work / "verify" / action / migration.id,
                    runner=run_sqlcl,
                )
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
                    "payload_targets": {
                        "tables": profile_target(config, "TABLES"),
                        "code": profile_target(config, "CODE"),
                    },
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
            target_state_key=metadata.state_key,
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
    parser.add_argument("--out", required=True, help="canonical apply report output")
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
        destination = Path(args.out)
        if destination.is_symlink():
            raise ReleaseAdapterError("apply report output must not be a symlink")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(
            json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        )
        print(json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ConfigError, ReleaseError, ReleaseAdapterError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())
