#!/usr/bin/env python3
"""Public team workflow entry point; all safety logic lives in teamlib."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from typing import Any
import uuid

from teamlib.apex import (
    ApexError,
    ExportConflict,
    adopt_app,
    bootstrap_app,
    capture_app,
    export_app,
    import_app,
    resolve_export,
)
from teamlib.announce import draft_all_clear, draft_import_announcement
from teamlib.app_checks import AppCheckError
from teamlib.assertions import run_verification_member
from teamlib.ci import CIError
from teamlib.config import ConfigError, OFFLINE_COMMANDS, Target, load_config, parse_target_contract, profile_target
from teamlib.control_store import ControlStore, ControlStoreError, SqlControlStore
from teamlib.deploy import DeployError, deploy_app
from teamlib.fingerprints import InventoryError, diff_inventory, inventory_from_manifest, load_inventory
from teamlib.destructive_confirmation import ConfirmationError, load_confirmation
from teamlib.migrate import MigrationRunError, apply_plan, apply_redo, apply_undo
from teamlib.migration_store import MigrationStoreError, SqlMigrationStore
from teamlib.sqlcl import run_sqlcl
from teamlib.live_inventory import inventory_target
from teamlib.qualification import QualificationError, qualify_target, write_report
from teamlib.patch import PatchError, recover_files
from teamlib.release import ReleaseError
from teamlib.runbook import RunbookError
from teamlib.runtime import preflight_online
from teamlib.state import StateError, load_baseline
from teamlib.migration_bundle import BundleError
from teamlib.trees import TreeError, read_git_tree

# Commands that write controller or metadata state and are therefore refused for
# a production classification. Kept in one place so the parser, the online
# dispatcher and the tests cannot drift apart.
PRODUCTION_REFUSED_COMMANDS = frozenset(
    {
        "setup-state", "adopt-frontier", "qualify-target", "recover-migration",
        "register-app", "recover-app-lock", "migrate", "undo-migration", "redo-migration",
    }
)


def _parser() -> argparse.ArgumentParser:
    env_parent = argparse.ArgumentParser(add_help=False)
    env_parent.add_argument("--env", dest="env_file", default=argparse.SUPPRESS, help="literal environment profile file")

    parser = argparse.ArgumentParser(
        prog="team.py",
        description="APEX team round-trip and promotion workflow",
        parents=[env_parent],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", parents=[env_parent])
    sub.add_parser("setup-state", parents=[env_parent])
    sub.add_parser("adopt-frontier", parents=[env_parent])
    qualify = sub.add_parser("qualify-target", parents=[env_parent])
    qualify.add_argument("--source-commit", required=True)
    qualify.add_argument("--aliases", required=True, help="comma-separated configured application aliases")
    qualify.add_argument("--out", required=True)
    qualify.add_argument("--release-archive")
    qualify.add_argument("--apply-report")
    migrate = sub.add_parser("migrate", parents=[env_parent])
    migrate.add_argument("--source", default="migrations")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--bootstrap", action="store_true")
    migrate.add_argument("--expected-inventory")
    migrate.add_argument("--actual-inventory")
    migrate.add_argument("--destructive-confirmation")
    for name in ("undo-migration", "redo-migration"):
        lifecycle = sub.add_parser(name, parents=[env_parent])
        lifecycle.add_argument("migration_id")
        lifecycle.add_argument("--source", default="migrations")
        lifecycle.add_argument("--dry-run", action="store_true")
        lifecycle.add_argument("--expected-inventory")
        lifecycle.add_argument("--actual-inventory")
        lifecycle.add_argument("--destructive-confirmation")
    drift = sub.add_parser("check-drift", parents=[env_parent])
    drift.add_argument("--expected-inventory")
    drift.add_argument("--actual-inventory")
    drift.add_argument("--out")
    history = sub.add_parser("export-history", parents=[env_parent])
    history.add_argument("--out", required=True)
    recover_migration = sub.add_parser("recover-migration", parents=[env_parent])
    recover_migration.add_argument("run_token")
    recover_migration.add_argument("--attempt")
    recover_migration.add_argument("--evidence", required=True)
    register = sub.add_parser("register-app", parents=[env_parent])
    register.add_argument("alias")
    register.add_argument("--transfer-from")
    register.add_argument("--capture-recovery-id")
    status = sub.add_parser("app-status", parents=[env_parent])
    status.add_argument("alias")
    recover_lock = sub.add_parser("recover-app-lock", parents=[env_parent])
    recover_lock.add_argument("alias")
    recover_lock.add_argument("--run-token")
    recover_lock.add_argument("--evidence", required=True)
    capture = sub.add_parser("capture-app", parents=[env_parent])
    capture.add_argument("alias")
    for name in ("bootstrap-app", "adopt-app", "export-app"):
        command = sub.add_parser(name, parents=[env_parent])
        command.add_argument("alias")
    resolve = sub.add_parser("resolve-export", parents=[env_parent])
    resolve.add_argument("recovery_id")
    resolve.add_argument("--resolved", required=True)
    imp = sub.add_parser("import-app", parents=[env_parent])
    imp.add_argument("alias")
    imp.add_argument("--ref", default="HEAD")
    imp.add_argument("--replace-from")
    imp.add_argument("--confirm-pause", action="store_true", help="confirm the independently posted team pause notice")
    announce = sub.add_parser("announce-import", parents=[env_parent])
    announce.add_argument("alias")
    announce_choice = announce.add_mutually_exclusive_group(required=True)
    announce_choice.add_argument("--ref")
    announce_choice.add_argument("--all-clear")
    deploy = sub.add_parser("deploy-app", parents=[env_parent])
    deploy.add_argument("alias")
    deploy.add_argument("--target", required=True)
    deploy.add_argument("--ref", required=True)
    files = sub.add_parser("recover-files", parents=[env_parent])
    files.add_argument("operation_id")
    files.add_argument("--action", choices=("finish", "restore"), required=True)

    # Offline commands deliberately have no --env dependency. Their concrete
    # options are parsed by their owning modules when those modules exist.
    for name in sorted(OFFLINE_COMMANDS):
        command = sub.add_parser(name)
        command.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def _env_path(args: argparse.Namespace) -> Path:
    explicit = getattr(args, "env_file", None) or os.environ.get("PROJECT_ENV_FILE")
    if explicit:
        return Path(explicit)
    return _repo_root() / ".env"


def _config(args: argparse.Namespace, *, require_verify: bool = False):
    return load_config(_env_path(args), require_verify=require_verify)


def _target(args: argparse.Namespace, alias: str, *, require_verify: bool = False):
    config = _config(args, require_verify=require_verify)
    if alias not in config.apps:
        raise ConfigError(f"unknown application alias: {alias}")
    return config, profile_target(config, "APEX", alias=alias)


def _store(repo: Path) -> ControlStore:
    return ControlStore(repo / ".sync-state")


def _sql_control_store(repo: Path, metadata: Target) -> SqlControlStore:
    return SqlControlStore(metadata, work_root=repo / "scratch" / "metadata")


def _sql_migration_store(repo: Path, metadata: Target) -> SqlMigrationStore:
    return SqlMigrationStore(metadata, work_root=repo / "scratch" / "metadata")


def _json(value: object) -> None:
    print(json.dumps(value, sort_keys=True, ensure_ascii=False))


def _target_from_contract(path: str | Path, alias: str) -> Target:
    contract = parse_target_contract(path)
    if contract.role not in {"integration", "test", "replay"}:
        raise ConfigError("deployment target must be integration, test, or replay")
    if alias not in contract.app_ids:
        raise ConfigError(f"target contract has no application binding for {alias}")
    binding = dict(contract.binding)
    connection = binding.get("connection") or binding.get("sqlcl_connection")
    if not isinstance(connection, str) or not connection:
        raise ConfigError("deployment target binding must name a credential-free SQLcl connection")
    binding.setdefault("profile", contract.role.upper())
    binding.setdefault("alias", alias)
    binding.setdefault("app_id", contract.app_ids[alias])
    binding_digest = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
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
        binding_digest=binding_digest,
    )


def _resolved_commit(repo: Path, ref: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ConfigError(f"could not resolve source ref: {ref}")
    return result.stdout.strip()


def _import_notice(
    args: argparse.Namespace,
    target: Target,
    store: Any,
    repo: Path,
    commit: str,
) -> str:
    """Build the pause notice from a fresh read-only capture and baseline."""
    try:
        baseline = load_baseline(target, root=repo / ".sync-state")
        observed = capture_app(
            target,
            repo=repo,
            root=repo / ".sync-state",
            control_store=store,
            persist=False,
        )
        selected = read_git_tree(repo, commit, target.alias or "")
        changed = sorted(
            path
            for path in set(baseline.tree) | set(observed.tree)
            if baseline.tree.get(path) != observed.tree.get(path)
        )
        planned = sorted(
            path
            for path in set(baseline.tree) | set(selected)
            if baseline.tree.get(path) != selected.get(path)
        )
        roster = [entry.checkout_uuid for entry in store.list_registry(target)]
    except (ApexError, ControlStoreError, StateError, TreeError) as exc:
        raise ConfigError(f"cannot draft import pause notice from observed state: {exc}") from exc
    return draft_import_announcement(
        target.alias or "",
        commit,
        target={"app_id": target.app_id, "workspace_id": target.workspace_id, "instance_id": target.instance_id},
        roster=roster,
        changed_paths=changed,
        planned_paths=planned,
        operator=os.environ.get("USER", "the import operator"),
    ).text


def _confirm_import_pause(args: argparse.Namespace, notice: str) -> None:
    print(notice)
    if args.confirm_pause:
        print("Import pause confirmed by --confirm-pause.")
        return
    if not sys.stdin.isatty():
        raise ConfigError("import-app requires --confirm-pause in a non-interactive session")
    answer = input("Has this pause notice been posted and has everyone stopped editing? Type 'proceed' to continue: ")
    if answer.strip().casefold() != "proceed":
        raise ConfigError("import-app cancelled; explicit pause confirmation was not received")


def _repo_root() -> Path:
    """Resolve the repository root rather than trusting the caller's directory.

    Every online command reads .env and writes .sync-state/ and scratch/. Those
    belong to the repository, not to whatever directory the developer happened
    to be standing in when they ran the launcher.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return Path(__file__).resolve().parent.parent


def _migration_drift(args: argparse.Namespace, config: Any, repo: Path) -> str | None:
    """Validate the reviewed inventory gate shared by every DB migration action."""
    expected_path = getattr(args, "expected_inventory", None)
    actual_path = getattr(args, "actual_inventory", None)
    dry_run = bool(getattr(args, "dry_run", False))
    if bool(expected_path) != bool(actual_path):
        raise ConfigError("migration drift gate requires both --expected-inventory and --actual-inventory")
    if not dry_run and not expected_path:
        raise ConfigError("migration requires a reviewed drift gate: provide --expected-inventory and --actual-inventory")
    if not expected_path:
        return None
    try:
        expected = load_inventory(expected_path)
        actual = load_inventory(actual_path)
        drift = diff_inventory(expected, actual)
    except InventoryError as exc:
        raise ConfigError(str(exc)) from exc
    if any(drift[key] for key in ("added", "missing", "changed", "invalid")) or drift.get("topology_mismatch"):
        raise MigrationRunError("migration drift gate is blocked: " + json.dumps(drift, sort_keys=True))
    return actual.digest


def _migration_callbacks(config: Any, repo: Path, schema_set_digest: str):
    """Create one directional callback set for migrate, undo, and redo."""
    def execute(migration, action, sql_path):
        payload_target = profile_target(config, "TABLES" if migration.target == "tables" else "CODE")
        run_sqlcl(payload_target, "write", sql_path, repo / "scratch" / "migration-payload" / action / migration.id)

    def verify(migration, action, verify_path):
        run_verification_member(
            profile_target(config, "VERIFY"),
            verify_path,
            repo / "scratch" / "migration-verify" / action / migration.id,
            runner=run_sqlcl,
        )
        return True

    def observe(migration, phase):
        observation_target = profile_target(config, "TABLES")
        observation_work = repo / "scratch" / "migration-observation" / phase / migration.id
        return inventory_target(
            observation_target,
            config.tables_schema,
            config.code_schema,
            observation_work,
            schema_set_digest=schema_set_digest,
        )

    return execute, verify, observe


def _migration_output(command: str, report: Any, *, dry_run: bool) -> None:
    _json({
        "status": "dry-run" if dry_run else "success",
        "operation": command,
        "action": report.action,
        "selected": report.selected,
        "applied": report.applied,
        "reverted": report.reverted,
        "foreign_applied": report.foreign_applied,
        "foreign_reverted": report.foreign_reverted,
        "blocked_attempt": report.blocked_attempt,
        "verified_inventory_digest": report.verified_inventory_digest,
        "confirmation_template": report.confirmation_template,
    })


def _online(args: argparse.Namespace) -> object:
    repo = _repo_root()
    command = args.command
    if command == "doctor":
        config = _config(args)
        _json({"status": "valid", "project": config.project, "role": config.role, "environment": config.environment, "profiles": sorted(config.profiles)})
        return 0
    if command == "setup-state":
        config = _config(args)
        if config.environment == "production" and command in PRODUCTION_REFUSED_COMMANDS:
            raise ConfigError(f"{command} is refused for production targets")
        metadata = profile_target(config, "METADATA")
        store = _sql_control_store(repo, metadata)
        store.setup_state([profile_target(config, "APEX", alias=alias) for alias in config.apps])
        _json({"status": "success", "operation": "setup-state", "targets": sorted(config.apps)})
        return 0
    if command == "adopt-frontier":
        config = _config(args, require_verify=True)
        if config.environment == "production" and command in PRODUCTION_REFUSED_COMMANDS:
            raise ConfigError(f"{command} is refused for production targets")
        metadata = profile_target(config, "METADATA")
        store = _sql_migration_store(repo, metadata)
        schema_set_digest = hashlib.sha256(
            f"{config.tables_schema}|{config.code_schema}|{config.metadata_schema}".encode("ascii")
        ).hexdigest()
        store.bootstrap(metadata, schema_set_digest=schema_set_digest)
        inventory = inventory_target(
            profile_target(config, "TABLES"),
            config.tables_schema,
            config.code_schema,
            repo / "scratch" / "frontier-inventory",
            schema_set_digest=schema_set_digest,
        )
        run_token = uuid.uuid4().hex
        store.acquire(metadata, run_token, os.environ.get("USER", "frontier-worker"), socket.gethostname())
        try:
            digest = store.record_inventory(metadata, inventory.as_dict(), run_token=run_token)
            store.ensure_observation(metadata, digest, run_token=run_token)
        finally:
            # A failure here leaves the mutex held on purpose; recover-migration
            # clears it with evidence, exactly as a failed migrate does.
            store.release(metadata, run_token)
        _json({"status": "success", "operation": command, "inventory_digest": digest})
        return 0
    if command == "qualify-target":
        config = _config(args, require_verify=True)
        if config.environment == "production":
            raise ConfigError("qualify-target is refused for production targets")
        if bool(args.release_archive) != bool(args.apply_report):
            raise ConfigError("--release-archive and --apply-report must be supplied together")
        if _resolved_commit(repo, "HEAD") != args.source_commit:
            raise ConfigError("qualification checkout is not the exact source commit")
        aliases = tuple(part.strip() for part in args.aliases.split(",") if part.strip())
        metadata = profile_target(config, "METADATA")
        store = _sql_migration_store(repo, metadata)
        try:
            try:
                runtime_report = preflight_online(
                    config,
                    repo,
                    os.environ.get("TEAM_FLOW_RUNNER") or None,
                )
            except RuntimeError as exc:
                raise ConfigError(str(exc)) from exc
            report = qualify_target(
                repo,
                config,
                args.source_commit,
                aliases,
                store=store,
                work=repo / "scratch" / "qualification",
                release_archive=args.release_archive,
                apply_report=args.apply_report,
                flow_executable=os.environ.get("TEAM_FLOW_RUNNER") or None,
                runner_contract=repo / "ci" / "runner-contract.json",
                runtime_report=runtime_report,
            )
        except QualificationError as exc:
            if exc.report is not None:
                write_report(exc.report, args.out)
            raise
        write_report(report, args.out)
        _json({"status": report["final_status"], "operation": command, "out": str(args.out)})
        return 0
    if command in {"migrate", "undo-migration", "redo-migration"}:
        config = _config(args, require_verify=True)
        if config.environment == "production" and command in PRODUCTION_REFUSED_COMMANDS:
            raise ConfigError(f"{command} is refused for production targets")
        verified_digest = _migration_drift(args, config, repo)
        metadata = profile_target(config, "METADATA")
        store = _sql_migration_store(repo, metadata)
        schema_set_digest = hashlib.sha256(
            f"{config.tables_schema}|{config.code_schema}|{config.metadata_schema}".encode("ascii")
        ).hexdigest()
        execute, verify, observe = _migration_callbacks(config, repo, schema_set_digest)
        confirmation = None
        if args.destructive_confirmation:
            try:
                confirmation, _confirmation_digest = load_confirmation(args.destructive_confirmation)
            except ConfirmationError as exc:
                raise ConfigError(str(exc)) from exc
        profiles = {
            "store": store,
            "target": metadata,
            "payload_targets": {
                "tables": profile_target(config, "TABLES"),
                "code": profile_target(config, "CODE"),
            },
            "dry_run": args.dry_run,
            "bootstrap": getattr(args, "bootstrap", False),
            "schema_set_digest": schema_set_digest,
            "verified_inventory_digest": verified_digest,
            "require_observation": True,
            "observe": observe,
            "source_commit": _resolved_commit(repo, "HEAD"),
            "applied_by": os.environ.get("USER", "migration-worker"),
            "execute": execute,
            "verify": verify,
        }
        if command == "migrate":
            report = apply_plan(args.source, profiles, confirmation=confirmation)
        elif command == "undo-migration":
            report = apply_undo(args.source, args.migration_id, profiles, confirmation=confirmation)
        else:
            report = apply_redo(args.source, args.migration_id, profiles, confirmation=confirmation)
        _migration_output(command, report, dry_run=args.dry_run)
        return 0
    if command == "check-drift":
        config = _config(args, require_verify=True)
        if bool(args.expected_inventory) != bool(args.actual_inventory):
            raise ConfigError("check-drift requires both --expected-inventory and --actual-inventory")
        if not args.expected_inventory:
            live_target = profile_target(config, "TABLES")
            schema_set_digest = hashlib.sha256(
                f"{config.tables_schema}|{config.code_schema}|{config.metadata_schema}".encode("ascii")
            ).hexdigest()
            try:
                actual_inventory = inventory_target(
                    live_target,
                    config.tables_schema,
                    config.code_schema,
                    repo / "scratch" / "live-inventory",
                    schema_set_digest=schema_set_digest,
                )
            except Exception as exc:
                raise MigrationRunError(f"live drift inventory failed: {exc}") from exc
            actual_path = repo / "scratch" / "live-inventory.json"
            from teamlib.fingerprints import save_inventory
            save_inventory(actual_inventory, actual_path)
            canonical_path = repo / "database" / "schema-inventory.json"
            if not canonical_path.is_file():
                _json({"status": "unknown", "operation": command, "project": config.project, "actual_inventory": str(actual_path), "reason": "canonical schema inventory is not adopted"})
                return 3
            args.expected_inventory = str(canonical_path)
            args.actual_inventory = str(actual_path)
        try:
            expected = load_inventory(args.expected_inventory)
            actual = load_inventory(args.actual_inventory)
        except InventoryError as exc:
            raise ConfigError(str(exc)) from exc
        result = diff_inventory(expected, actual)
        frontier_result: dict[str, object] = {"status": "unavailable"}
        try:
            metadata = profile_target(config, "METADATA")
            migration_store = _sql_migration_store(repo, metadata)
            state = migration_store.read_state(metadata)
            observations = state.get("observations", [])
            if observations:
                frontier_digest = observations[-1].get("after")
                inventories = migration_store.read_inventories(metadata)
                frontier_manifest = inventories.get(frontier_digest)
                if not isinstance(frontier_manifest, dict):
                    frontier_result = {"status": "unknown", "reason": "accepted frontier manifest is missing"}
                else:
                    frontier = inventory_from_manifest(frontier_manifest)
                    frontier_result = {
                        "status": "clean" if not any(diff_inventory(frontier, actual)[key] for key in ("added", "missing", "changed", "invalid")) else "drift",
                        "digest": frontier.digest,
                        "diff": diff_inventory(frontier, actual),
                    }
            else:
                frontier_result = {"status": "unknown", "reason": "no observed migration frontier is adopted"}
        except (MigrationStoreError, InventoryError) as exc:
            frontier_result = {"status": "unknown", "reason": str(exc)}
        result["observed_frontier"] = frontier_result
        if args.out:
            Path(args.out).write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
        canonical_clean = not any(result[key] for key in ("added", "missing", "changed", "invalid")) and not result.get("topology_mismatch")
        frontier_clean = frontier_result.get("status") == "clean"
        status = "clean" if canonical_clean and frontier_clean else "drift" if frontier_result.get("status") == "drift" or not canonical_clean else "unknown"
        _json({"status": status, "operation": command, "diff": result})
        return 0 if status == "clean" else 3
    if command in {"export-history", "recover-migration"}:
        config = _config(args)
        if config.environment == "production" and command in PRODUCTION_REFUSED_COMMANDS:
            raise ConfigError(f"{command} is refused for production targets")
        metadata = profile_target(config, "METADATA")
        store = _sql_migration_store(repo, metadata)
        if command == "export-history":
            store.export_history(metadata, args.out)
            _json({"status": "success", "operation": command, "path": args.out})
        else:
            store.recover(metadata, args.run_token, args.evidence, attempt_id=args.attempt)
            _json({"status": "success", "operation": command})
        return 0
    if command in {"register-app", "app-status", "recover-app-lock", "capture-app", "bootstrap-app", "adopt-app", "export-app", "import-app"}:
        config, target = _target(args, args.alias)
        if target.environment == "production" and command in PRODUCTION_REFUSED_COMMANDS:
            raise ConfigError(f"{command} is refused for production targets")
        metadata = profile_target(config, "METADATA")
        store = _sql_control_store(repo, metadata)
        if command == "register-app":
            checkout = os.environ.get("TEAM_CHECKOUT_UUID") or str(uuid.uuid4())
            entry = store.register_app(
                target,
                checkout,
                socket.gethostname(),
                os.environ.get("USER", "unknown"),
                transfer_from=args.transfer_from,
                capture_recovery_id=args.capture_recovery_id,
            )
            _json({"status": "success", "operation": command, "checkout_uuid": entry.checkout_uuid})
            return 0
        if command == "app-status":
            state = store.read_app_sync_state(target.physical_key)
            _json({"status": "success", "operation": command, "registry": [entry.__dict__ for entry in store.list_registry(target)], "mutex": state.__dict__})
            return 0
        if command == "recover-app-lock":
            state = store.recover_app_lock(target.physical_key, evidence=args.evidence, run_token=args.run_token)
            _json({"status": "success", "operation": command, "mutex": state.__dict__})
            return 0
        if command == "capture-app":
            result = capture_app(target, repo=repo, control_store=store)
            _json({"status": "success", "operation": command, "recovery_path": str(repo / ".sync-state" / "recovery" / result.recovery_id), "tree_digest": result.sql_result.result_manifest.get("identity_digest") if hasattr(result.sql_result, "result_manifest") else None})
            return 0
        if command == "bootstrap-app":
            result = bootstrap_app(target, repo=repo, control_store=store)
            _json({"status": "success", "operation": command, **result})
            return 0
        if command == "adopt-app":
            baseline = adopt_app(target, repo=repo, control_store=store)
            _json({"status": "success", "operation": command, "source_commit": baseline.source_commit})
            return 0
        if command == "export-app":
            decision = export_app(target, repo=repo, control_store=store)
            _json({"status": "success", "operation": command, "changed_paths": sorted(decision.tree), "conflicts": list(decision.conflicts)})
            return 0
        resolved = _resolved_commit(repo, args.ref)
        notice = _import_notice(args, target, store, repo, resolved)
        _confirm_import_pause(args, notice)
        baseline = import_app(target, args.ref, replace_from=args.replace_from, repo=repo, control_store=store)
        operation_id = None
        recovery_path = None
        for result_path in sorted((repo / ".sync-state" / "recovery").glob("*/result.json"), key=lambda path: path.stat().st_mtime, reverse=True):
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if result.get("verified") is True and result.get("source_commit") == baseline.source_commit:
                operation_id = result.get("operation_id")
                recovery_path = result.get("recovery_path")
                break
        _json({"status": "success", "operation": command, "source_commit": baseline.source_commit, "verified": True, "operation_id": operation_id, "recovery_path": recovery_path})
        return 0
    if command == "resolve-export":
        # Resolve uses the target selected by the environment; the recovery ID
        # itself remains immutable and is checked by the core implementation.
        config = _config(args)
        if len(config.apps) != 1:
            raise ConfigError("resolve-export requires an explicit single-app environment in this entry point")
        target = profile_target(config, "APEX", alias=next(iter(config.apps)))
        decision = resolve_export(target, args.recovery_id, Path(args.resolved), repo=repo)
        _json({"status": "success", "operation": command, "changed_paths": sorted(decision.tree), "conflicts": list(decision.conflicts)})
        return 0
    if command == "recover-files":
        recover_files(args.operation_id, args.action, repo=repo)
        _json({"status": "success", "operation": command, "operation_id": args.operation_id, "action": args.action})
        return 0
    if command == "deploy-app":
        target = _target_from_contract(args.target, args.alias)
        config = _config(args, require_verify=True)
        if config.role != target.role or config.environment != target.environment:
            raise ConfigError("deployment target contract and environment profile roles do not match")
        apex_profile = config.profiles.get("APEX")
        if apex_profile is None or apex_profile.connection != target.connection:
            raise ConfigError("deployment target binding does not match the configured APEX SQLcl profile")
        for field, expected in (
            ("instance_id", apex_profile.expected_instance_id),
            ("db_name", apex_profile.expected_db_name),
            ("service", apex_profile.expected_service),
            ("session_user", apex_profile.expected_user),
            ("current_schema", apex_profile.expected_current_schema),
        ):
            if getattr(target, field) != expected:
                raise ConfigError(f"deployment target binding does not match APEX profile {field}")
        commit = _resolved_commit(repo, args.ref)
        tree = read_git_tree(repo, commit, args.alias)
        metadata = profile_target(config, "METADATA")
        report = deploy_app(target, tree, commit, repo=repo, control_store=_sql_control_store(repo, metadata))
        _json({"status": "success", "operation": command, "source_commit": report.source_commit, "tree_digest": report.tree_digest, "verified_tree_digest": report.verified_tree_digest, "recovery_id": report.recovery_id})
        return 0
    if command == "announce-import":
        config, target = _target(args, args.alias)
        if args.ref:
            metadata = profile_target(config, "METADATA")
            store = _sql_control_store(repo, metadata)
            print(_import_notice(args, target, store, repo, _resolved_commit(repo, args.ref)))
        else:
            result_path = repo / ".sync-state" / "recovery" / args.all_clear / "result.json"
            if not result_path.is_file():
                raise ConfigError(f"verified import result was not found: {result_path}")
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ConfigError("all-clear result is unreadable") from exc
            print(draft_all_clear(args.alias, result))
        return 0
    raise ConfigError(f"unsupported online command: {command}")


# Offline commands take explicit local inputs, but apply-release binds a
# non-production target and therefore accepts an environment profile. A global
# --env must reach it rather than being silently dropped.
ENV_AWARE_OFFLINE_COMMANDS = frozenset({"apply-release"})


def _offline(args: argparse.Namespace) -> int:
    module_names = {
        "new-migration": "authoring",
        "add-dependency": "authoring",
        "migration-plan": "migration_plan",
        "build-release": "release",
        "verify-release": "release",
        "plan-release": "release",
        "gen-runbook": "runbook",
        "explain-conflict": "conflict_assistant",
        "snapshot": "fingerprints",
        "verify-history": "migration_bundle",
        "replay": "replay",
        "adopt-baseline": "replay",
        "ci-doctor": "ci",
        "sign-test-evidence": "qualification",
        "apply-release": "release_adapter",
        "prune-scratch": "prune",
    }
    module_name = module_names[args.command]
    try:
        module = __import__(f"teamlib.{module_name}", fromlist=["main"])
    except ImportError as exc:
        raise ConfigError(f"offline command implementation is unavailable: {args.command}") from exc
    handler = getattr(module, "main", None)
    if handler is None:
        raise ConfigError(f"offline command has no public handler: {args.command}")
    command_prefixed = {"new-migration", "add-dependency", "build-release", "verify-release", "plan-release", "apply-release", "adopt-baseline", "ci-doctor", "sign-test-evidence", "prune-scratch"}
    handler_args = [args.command, *args.args] if args.command in command_prefixed else list(args.args)
    env_file = getattr(args, "env_file", None)
    if env_file and args.command in ENV_AWARE_OFFLINE_COMMANDS and "--env" not in handler_args:
        handler_args.extend(["--env", env_file])
    try:
        result = handler(handler_args)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        # Offline handlers take explicit local inputs. A missing or malformed
        # one is a user error and must answer with the same structured refusal
        # every other command produces, not a traceback.
        raise ConfigError(f"{args.command} could not read its input: {exc}") from exc
    return int(result or 0)


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    env_file = None
    command_index = 0
    if len(raw) >= 2 and raw[0] == "--env":
        env_file = raw[1]
        command_index = 2
    if command_index < len(raw) and raw[command_index] in OFFLINE_COMMANDS:
        args = argparse.Namespace(env_file=env_file, command=raw[command_index], args=raw[command_index + 1:])
    else:
        args, unknown = parser.parse_known_args(raw)
        if unknown:
            if args.command in OFFLINE_COMMANDS:
                args.args = list(getattr(args, "args", ())) + unknown
            else:
                parser.error("unrecognized arguments: " + " ".join(unknown))
    try:
        if args.command in OFFLINE_COMMANDS:
            return _offline(args)
        return int(_online(args))
    except ExportConflict as exc:
        print(json.dumps({"status": "conflict", "operation": "export-app", "conflicts": list(exc.decision.conflicts), "recovery_path": exc.recovery_id}, sort_keys=True))
        return 3
    except (ConfigError, ControlStoreError, StateError, PatchError, ApexError, MigrationRunError, MigrationStoreError, DeployError, ReleaseError, RunbookError, CIError, AppCheckError, QualificationError, TreeError, BundleError, InventoryError) as exc:
        print(str(exc), file=sys.stderr)
        return 2 if isinstance(exc, ConfigError) else 3


if __name__ == "__main__":
    sys.exit(main())
