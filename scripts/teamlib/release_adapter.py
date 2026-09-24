"""Explicit non-production adapter for applying a verified release artifact."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
from collections.abc import Mapping
from dataclasses import dataclass

from .config import Config, ConfigError, Target, contract_target, load_config, parse_target_contract, profile_target, schema_set_digest
from .control_store import SqlControlStore
from .deploy import deploy_app
from .destructive_confirmation import ConfirmationRequirement, require_confirmations
from .migrate import apply_forward, apply_plan, apply_redo, apply_undo
from .migration_runtime import migration_profiles
from .migration_store import SqlMigrationStore
from .release import (
    ApplyReport,
    Manifest,
    ReleaseError,
    ReleasePlan,
    _database_release_replay_marker,
    apply_release,
    release_app_trees,
    release_master_contract,
    release_migration_files,
    plan_release,
    verify_release,
)


class ReleaseAdapterError(ReleaseError):
    pass


def verify_required_migrations(
    required: tuple[Mapping[str, str], ...] | list[Mapping[str, str]],
    history: Mapping[str, Any],
) -> None:
    history_data = history.get("history", history) if isinstance(history, Mapping) else {}
    if not isinstance(history_data, Mapping):
        raise ReleaseAdapterError("target history must contain a mapping")
    for item in required:
        migration_id = item["id"]
        row = history_data.get(migration_id)
        if row is None or row.get("status") != "APPLIED" or row.get("checksum") != item["checksum"]:
            raise ReleaseAdapterError(f"required migration unavailable: {migration_id}")


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
    applied_by: str = "release-test"
    deploy_root: Path | None = None


# Every field that binds a release target to a physical APEX application. The
# SQLcl result identity probe checks the database session only -- not the
# workspace or application ID -- so this comparison is the sole guard that a
# contract cannot redirect a deployment at a different application than the
# environment profile names. Both apply paths must check all of it.
_APEX_BINDING_FIELDS = (
    "connection", "instance_id", "db_name", "service", "session_user",
    "current_schema", "workspace_id", "app_id", "parsing_schema", "ownership_mode",
)


def _assert_binding_matches_profile(target: Target, apex_profile: Target, alias: str) -> None:
    for field in _APEX_BINDING_FIELDS:
        if getattr(target, field) != getattr(apex_profile, field):
            raise ReleaseAdapterError(
                f"release target binding does not match APEX profile {field} for {alias}"
            )


def _target_from_contract(path: str | Path, alias: str, *, expected_role: str | None = None) -> Target:
    """Bind a contract alias, answering with this module's refusal type.

    The binding itself -- and therefore the binding digest that becomes part of
    the target's state key -- comes from config.contract_target so the adapter
    and deploy-app cannot compute two different identities for one target.
    """
    try:
        return contract_target(path, alias, expected_role=expected_role)
    except ConfigError as exc:
        raise ReleaseAdapterError(str(exc)) from exc


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
    if target_document.get("role") != contract.role or target_document.get("environment") != contract.environment:
        raise ReleaseAdapterError("release target contract changed while loading")
    state_root = root if root is not None else repo / ".sync-state" / "release"
    migration_store = SqlMigrationStore(metadata, work_root=state_root / "metadata")
    control_store = SqlControlStore(metadata, work_root=state_root / "metadata")
    if getattr(manifest, "kind", None) == "schema":
        if manifest.app_tree_digests:
            raise ReleaseAdapterError("schema release archive must not contain application trees")
        app_targets: tuple[Target, ...] = ()
    elif getattr(manifest, "kind", None) == "app":
        alias = manifest.alias
        if not alias or alias not in contract.app_ids or alias not in config.apps:
            raise ReleaseAdapterError(
                f"release target contract application bindings do not include {alias}"
            )
        target = _target_from_contract(target_path, alias, expected_role="test")
        _assert_binding_matches_profile(target, profile_target(config, "APEX", alias=alias), alias)
        app_targets = (target,)
    else:
        if dict(contract.app_ids) != dict(config.apps):
            raise ReleaseAdapterError("release target contract application bindings differ from configuration")
        if set(manifest.app_tree_digests) != set(contract.app_ids):
            raise ReleaseAdapterError("release archive and test target application bindings differ")
        app_targets_list: list[Target] = []
        for alias in sorted(contract.app_ids):
            target = _target_from_contract(target_path, alias, expected_role="test")
            _assert_binding_matches_profile(target, profile_target(config, "APEX", alias=alias), alias)
            app_targets_list.append(target)
        app_targets = tuple(app_targets_list)
    return ReleaseApplyContext(
        manifest=manifest,
        target_document=target_document,
        config=config,
        metadata=metadata,
        migration_store=migration_store,
        control_store=control_store,
        schema_set_digest=schema_set_digest(config),
        app_targets=app_targets,
        applied_by="release-test",
        deploy_root=repo / ".sync-state" / "release" / "application",
    )


def _apply_release_context(
    context: ReleaseApplyContext,
    release_tar: str | Path,
    plan: ReleasePlan,
    history: Mapping[str, Any],
    *,
    repo: Path,
    confirmation: Mapping[str, Any] | None = None,
) -> ApplyReport:
    config = context.config
    with tempfile.TemporaryDirectory(prefix="team-release-apply-") as directory:
        work = Path(directory)
        migration_root = work / "migrations"
        migration_root.mkdir()
        for relative, data in release_migration_files(release_tar).items():
            (migration_root / relative).write_bytes(data)

        def apply_migrations(_pending: tuple[Mapping[str, Any], ...], reviewed: ReleasePlan) -> None:
            source_commit = context.manifest.source_commit
            source = getattr(context.manifest, "source", None)
            if source is not None:
                source_commit = "db-release:" + str(source["history_digest"])
            profiles = migration_profiles(
                config,
                context.metadata,
                context.migration_store,
                work,
                source_commit=source_commit,
                applied_by=context.applied_by,
            )
            if reviewed.events:
                replay_base = reviewed.replay_from
                # Carry the original target cut through later release archives.
                # This proves omitted no-op up/down pairs without changing the
                # migration store's local applied_sequence values.
                for row in history.values():
                    if isinstance(row, Mapping):
                        marker = _database_release_replay_marker(row.get("source_commit"))
                        if marker is not None:
                            replay_base = min(replay_base, marker[1])
                for event in reviewed.events:
                    if source is not None:
                        profiles["source_commit"] = (
                            f"db-release:{source['history_digest']}:{event['sequence']}:{replay_base}"
                        )
                    migration_id = str(event["id"])
                    current_history = context.migration_store.read_history(context.metadata)
                    row = current_history.get(migration_id)
                    operation = event["operation"]
                    if operation == "up":
                        if row is not None and row.get("status") == "APPLIED":
                            raise ReleaseAdapterError(
                                f"format-3 replay unexpectedly finds {migration_id} already APPLIED"
                            )
                        if row is not None and row.get("status") == "REVERTED":
                            action = "redo"
                            apply = apply_redo
                        else:
                            action = "migrate"
                            apply = apply_forward
                    elif operation == "down":
                        if row is None or row.get("status") != "APPLIED":
                            raise ReleaseAdapterError(
                                f"format-3 replay down event requires {migration_id} to be APPLIED"
                            )
                        action = "undo"
                        apply = apply_undo
                    else:
                        raise ReleaseAdapterError(f"unsupported format-3 replay operation: {operation}")
                    operation_confirmation = _operation_confirmation(
                        confirmation, migration_id, action
                    )
                    apply(
                        migration_root, migration_id, profiles,
                        confirmation=operation_confirmation,
                    )
                return
            apply_plan(
                migration_root,
                profiles,
                confirmation=confirmation,
                expected_plan={"pending": reviewed.pending},
            )

        target_by_alias = {target.alias: target for target in context.app_targets}
        # Validate against the contract the release was built with, not the
        # operator repository's copy, which may differ or be absent.
        packaged_contract: list[Mapping[str, Any] | None] = []
        deploy_root = context.deploy_root or (repo / ".sync-state" / "release" / "application")

        def deploy_application(alias: str, tree: Mapping[str, bytes], _reviewed: ReleasePlan) -> None:
            target = target_by_alias.get(alias)
            if target is None:
                raise ReleaseAdapterError(f"release application target is missing: {alias}")
            if not packaged_contract:
                try:
                    packaged_contract.append(release_master_contract(release_tar))
                except ReleaseError as exc:
                    raise ReleaseAdapterError(str(exc)) from exc
            source_identity = getattr(context.manifest, "source", None)
            deploy_source = context.manifest.source_commit
            if source_identity is not None:
                deploy_source = "db-release:" + str(source_identity.get("history_digest", ""))
            deploy_app(
                target,
                tree,
                deploy_source,
                {"verified": True, "target_key": target.physical_key},
                repo=repo,
                root=deploy_root,
                control_store=context.control_store,
                master_contract=packaged_contract[0],
            )

        try:
            return apply_release(
                release_tar,
                context.target_document,
                plan,
                history=history,
                apply_migrations=apply_migrations if getattr(context.manifest, "kind", None) != "app" else None,
                deploy_application=deploy_application if getattr(context.manifest, "kind", None) != "schema" else None,
                target_state_key=context.metadata.state_key,
            )
        except ReleaseAdapterError:
            raise
        except ReleaseError as exc:
            raise ReleaseAdapterError(str(exc)) from exc
        except Exception as exc:
            raise ReleaseAdapterError(f"release application failed: {exc}") from exc


def apply_verified_release_live(
    release_tar: str | Path,
    target_contract: str | Path,
    config: Config,
    *,
    repo: str | Path = ".",
    root: str | Path | None = None,
    confirmation: Mapping[str, Any] | None = None,
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
    if getattr(context.manifest, "format_version", None) == 3:
        requirements = _format3_confirmation_requirements(context.manifest, plan, history, config)
        try:
            require_confirmations(requirements, confirmation)
        except ValueError as exc:
            raise ReleaseAdapterError(str(exc)) from exc
    if getattr(context.manifest, "kind", None) == "schema":
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
        if destructive and getattr(context.manifest, "format_version", None) != 3:
            raise ReleaseAdapterError(
                "release-test requires reviewed destructive maintenance before apply: "
                + ", ".join(destructive)
            )
        context.control_store.setup_state(context.app_targets)
    elif getattr(context.manifest, "kind", None) == "app":
        verify_required_migrations(context.manifest.required_migrations, history)
        context.control_store.setup_state(context.app_targets)
    else:
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
        confirmation=confirmation,
    )


def _format3_confirmation_requirements(
    manifest: Manifest,
    plan: ReleasePlan,
    history: Mapping[str, Any],
    config: Config,
) -> tuple[ConfirmationRequirement, ...]:
    """Bind destructive replay confirmations to the exact payload target identities."""
    by_id = {item["id"]: item for item in manifest.migrations}
    state = {
        migration_id: (row.get("status") if isinstance(row, Mapping) else "")
        for migration_id, row in history.items()
    }
    requirements: list[ConfirmationRequirement] = []
    for event in plan.events:
        migration_id = str(event["id"])
        item = by_id.get(migration_id)
        if item is None:
            raise ReleaseAdapterError(f"format-3 replay migration metadata is missing: {migration_id}")
        if event["operation"] == "down":
            action = "undo"
            destructive = bool(item.get("down_destructive"))
        else:
            action = "redo" if state.get(migration_id) == "REVERTED" else "migrate"
            destructive = bool(item.get("destructive"))
        if destructive:
            profile = "TABLES" if item.get("target") == "tables" else "CODE"
            requirements.append(ConfirmationRequirement(
                migration_id=migration_id,
                action=action,
                bundle_checksum=str(item["checksum"]),
                payload_target_state_key=profile_target(config, profile).state_key,
            ))
        state[migration_id] = "APPLIED" if event["operation"] == "up" else "REVERTED"
    # The confirmation format intentionally admits one stable operation tuple
    # per migration. Reject repeated destructive tuple keys rather than claim
    # to distinguish identical operations that the document cannot represent.
    keys = [
        (item.migration_id, item.action, item.bundle_checksum, item.payload_target_state_key)
        for item in requirements
    ]
    if len(keys) != len(set(keys)):
        raise ReleaseAdapterError("format-3 replay repeats a destructive operation that its confirmation format cannot distinguish")
    return tuple(requirements)


def _operation_confirmation(
    confirmation: Mapping[str, Any] | None,
    migration_id: str,
    action: str,
) -> Mapping[str, Any] | None:
    """Pass a validated release confirmation's exact entry to one migration."""
    if confirmation is None:
        return None
    entries = confirmation.get("confirmations")
    if not isinstance(entries, list):
        raise ReleaseAdapterError("destructive confirmation must be a version-one document")
    selected = [
        entry for entry in entries
        if isinstance(entry, Mapping)
        and entry.get("migration_id") == migration_id
        and entry.get("action") == action
    ]
    if len(selected) > 1:
        raise ReleaseAdapterError(f"multiple confirmations match {migration_id}/{action}")
    if not selected:
        return None
    return {"version": confirmation.get("version"), "confirmations": selected}


def apply_verified_release(
    release_tar: str | Path,
    target_contract: str | Path,
    env_file: str | Path,
    plan: ReleasePlan,
    history: Mapping[str, Any],
    *,
    repo: str | Path = ".",
    root: str | Path | None = None,
    confirmation: Mapping[str, Any] | None = None,
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
    target_document = _release_target_document(target_path)
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
    # The digest must be the one the live inventories carry. A placeholder here
    # is written into TEAM_MIGRATION_META and makes every record_inventory in
    # this run -- and every later `migrate` against the same metadata schema --
    # fail with ORA-20011 SCHEMA_SET_DIGEST_MISMATCH.
    migration_store.bootstrap(metadata, schema_set_digest=schema_set_digest(config))
    control_store = SqlControlStore(metadata, work_root=state_root / "metadata")
    app_targets = []
    if getattr(manifest, "kind", None) == "schema":
        pass
    elif getattr(manifest, "kind", None) == "app":
        alias = manifest.alias
        if not alias or alias not in contract.app_ids or alias not in config.apps:
            raise ReleaseAdapterError(
                f"release target contract application bindings do not include {alias}"
            )
        target = _target_from_contract(target_contract, alias, expected_role=contract.role)
        _assert_binding_matches_profile(target, profile_target(config, "APEX", alias=alias), alias)
        app_targets.append(target)
        verify_required_migrations(manifest.required_migrations, history)
    else:
        for alias in release_app_trees(release_tar):
            target = _target_from_contract(target_contract, alias, expected_role=contract.role)
            _assert_binding_matches_profile(target, profile_target(config, "APEX", alias=alias), alias)
            app_targets.append(target)
    control_store.setup_state(app_targets)

    context = ReleaseApplyContext(
        manifest=manifest,
        target_document=target_document,
        config=config,
        metadata=metadata,
        migration_store=migration_store,
        control_store=control_store,
        schema_set_digest=schema_set_digest(config),
        app_targets=tuple(app_targets),
        applied_by="release-adapter",
        deploy_root=state_root / "application",
    )
    if getattr(manifest, "format_version", None) == 3:
        requirements = _format3_confirmation_requirements(manifest, plan, history, config)
        try:
            require_confirmations(requirements, confirmation)
        except ValueError as exc:
            raise ReleaseAdapterError(str(exc)) from exc
    return _apply_release_context(
        context, release_tar, plan, history, repo=repo_path, confirmation=confirmation
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
    parser.add_argument("--destructive-confirmation")
    parser.add_argument("--confirmation-out", help="write a false-only template for format-3 destructive replay")
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
            events=tuple(plan_raw.get("events", ())), replay_from=plan_raw.get("replay_from", 0),
        )
        if args.confirmation_out:
            config = load_config(env_file, require_verify=True)
            manifest = verify_release(args.archive)
            current = plan_release(args.archive, history, target)
            if (
                current.archive_digest != plan.archive_digest
                or current.history_digest != plan.history_digest
                or current.events != plan.events
                or current.pending != plan.pending
            ):
                raise ReleaseAdapterError("plan or target history changed; regenerate the release plan")
            requirements = _format3_confirmation_requirements(manifest, current, history, config)
            if not requirements:
                raise ReleaseAdapterError("no destructive format-3 replay confirmation is required")
            from .destructive_confirmation import confirmation_template, write_confirmation_template
            destination = write_confirmation_template(confirmation_template(requirements), args.confirmation_out)
            print(json.dumps({"status": "confirmation_required", "path": str(destination)}, sort_keys=True))
            return 0
        confirmation = None
        if args.destructive_confirmation:
            from .destructive_confirmation import load_confirmation
            confirmation, _ = load_confirmation(args.destructive_confirmation)
        report = apply_verified_release(
            args.archive, args.target, env_file, plan, history,
            repo=args.repo, root=args.state_root, confirmation=confirmation,
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
