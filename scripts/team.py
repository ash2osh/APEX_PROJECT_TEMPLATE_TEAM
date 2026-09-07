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
from teamlib.ci import CIError
from teamlib.config import ConfigError, OFFLINE_COMMANDS, Target, load_config, parse_target_contract, profile_target
from teamlib.control_store import ControlStore, ControlStoreError
from teamlib.deploy import DeployError, deploy_app
from teamlib.fingerprints import InventoryError, diff_inventory, load_inventory
from teamlib.migrate import MigrationRunError, apply_plan
from teamlib.migration_store import MigrationStore, MigrationStoreError
from teamlib.live_inventory import inventory_target
from teamlib.patch import PatchError, recover_files
from teamlib.release import ReleaseError
from teamlib.runbook import RunbookError
from teamlib.state import StateError
from teamlib.trees import read_git_tree


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="team.py", description="APEX team round-trip and promotion workflow")
    parser.add_argument("--env", dest="env_file", help="literal environment profile file")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor")
    sub.add_parser("setup-state")
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--source", default="migrations")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--bootstrap", action="store_true")
    migrate.add_argument("--expected-inventory")
    migrate.add_argument("--actual-inventory")
    drift = sub.add_parser("check-drift")
    drift.add_argument("--expected-inventory")
    drift.add_argument("--actual-inventory")
    drift.add_argument("--out")
    history = sub.add_parser("export-history")
    history.add_argument("--out", required=True)
    recover_migration = sub.add_parser("recover-migration")
    recover_migration.add_argument("run_token")
    recover_migration.add_argument("--attempt")
    recover_migration.add_argument("--evidence", required=True)
    register = sub.add_parser("register-app")
    register.add_argument("alias")
    register.add_argument("--transfer-from")
    status = sub.add_parser("app-status")
    status.add_argument("alias")
    recover_lock = sub.add_parser("recover-app-lock")
    recover_lock.add_argument("alias")
    recover_lock.add_argument("--run-token")
    recover_lock.add_argument("--evidence", required=True)
    capture = sub.add_parser("capture-app")
    capture.add_argument("alias")
    for name in ("bootstrap-app", "adopt-app", "export-app"):
        command = sub.add_parser(name)
        command.add_argument("alias")
    resolve = sub.add_parser("resolve-export")
    resolve.add_argument("recovery_id")
    resolve.add_argument("--resolved", required=True)
    imp = sub.add_parser("import-app")
    imp.add_argument("alias")
    imp.add_argument("--ref", default="HEAD")
    imp.add_argument("--replace-from")
    imp.add_argument("--confirm-pause", action="store_true", help="confirm the independently posted team pause notice")
    announce = sub.add_parser("announce-import")
    announce.add_argument("alias")
    announce_choice = announce.add_mutually_exclusive_group(required=True)
    announce_choice.add_argument("--ref")
    announce_choice.add_argument("--all-clear")
    deploy = sub.add_parser("deploy-app")
    deploy.add_argument("alias")
    deploy.add_argument("--target", required=True)
    deploy.add_argument("--ref", required=True)
    files = sub.add_parser("recover-files")
    files.add_argument("operation_id")
    files.add_argument("--action", choices=("finish", "restore"), required=True)

    # Offline commands deliberately have no --env dependency. Their concrete
    # options are parsed by their owning modules when those modules exist.
    for name in sorted(OFFLINE_COMMANDS):
        command = sub.add_parser(name)
        command.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def _env_path(args: argparse.Namespace) -> Path:
    return Path(args.env_file or os.environ.get("PROJECT_ENV_FILE", ".env"))


def _config(args: argparse.Namespace, *, require_verify: bool = False):
    return load_config(_env_path(args), require_verify=require_verify)


def _target(args: argparse.Namespace, alias: str, *, require_verify: bool = False):
    config = _config(args, require_verify=require_verify)
    if alias not in config.apps:
        raise ConfigError(f"unknown application alias: {alias}")
    return config, profile_target(config, "APEX", alias=alias)


def _store(repo: Path) -> ControlStore:
    return ControlStore(repo / ".sync-state")


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
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode != 0:
        raise ConfigError(f"could not resolve source ref: {ref}")
    return result.stdout.strip()


def _online(args: argparse.Namespace) -> object:
    repo = Path.cwd()
    command = args.command
    if command == "doctor":
        config = _config(args)
        _json({"status": "valid", "project": config.project, "role": config.role, "environment": config.environment, "profiles": sorted(config.profiles)})
        return 0
    if command == "setup-state":
        config = _config(args)
        if config.environment == "production":
            raise ConfigError("setup-state is refused for production targets")
        store = _store(repo)
        store.setup_state([profile_target(config, "APEX", alias=alias) for alias in config.apps])
        _json({"status": "success", "operation": "setup-state", "targets": sorted(config.apps)})
        return 0
    if command == "migrate":
        config = _config(args, require_verify=True)
        if bool(args.expected_inventory) != bool(args.actual_inventory):
            raise ConfigError("migrate drift gate requires both --expected-inventory and --actual-inventory")
        verified_digest = None
        if not args.dry_run and not args.expected_inventory:
            raise ConfigError("migrate requires a reviewed drift gate: provide --expected-inventory and --actual-inventory")
        if args.expected_inventory:
            try:
                drift = diff_inventory(load_inventory(args.expected_inventory), load_inventory(args.actual_inventory))
            except InventoryError as exc:
                raise ConfigError(str(exc)) from exc
            if any(drift[key] for key in ("added", "missing", "changed", "invalid")) or drift.get("topology_mismatch"):
                raise MigrationRunError("migration drift gate is blocked: " + json.dumps(drift, sort_keys=True))
            verified_digest = load_inventory(args.actual_inventory).digest
        metadata = profile_target(config, "METADATA")
        store = MigrationStore(repo / ".sync-state" / "migration")
        report = apply_plan(args.source, {"store": store, "target": metadata, "dry_run": args.dry_run, "bootstrap": args.bootstrap, "verified_inventory_digest": verified_digest})
        _json({"status": "success", "operation": command, "applied": report.applied, "foreign_applied": report.foreign_applied, "blocked_attempt": report.blocked_attempt})
        return 0
    if command == "check-drift":
        config = _config(args, require_verify=True)
        if bool(args.expected_inventory) != bool(args.actual_inventory):
            raise ConfigError("check-drift requires both --expected-inventory and --actual-inventory")
        if not args.expected_inventory:
            live_target = profile_target(config, "TABLES")
            try:
                actual_inventory = inventory_target(live_target, config.tables_schema, config.code_schema, repo / "scratch" / "live-inventory")
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
        if args.out:
            Path(args.out).write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
        _json({"status": "clean" if not any(result[key] for key in ("added", "missing", "changed", "invalid")) and not result.get("topology_mismatch") else "drift", "operation": command, "diff": result})
        return 0 if not any(result[key] for key in ("added", "missing", "changed", "invalid")) and not result.get("topology_mismatch") else 3
    if command in {"export-history", "recover-migration"}:
        config = _config(args)
        if command == "recover-migration" and config.environment == "production":
            raise ConfigError("recover-migration is refused for production targets")
        metadata = profile_target(config, "METADATA")
        store = MigrationStore(repo / ".sync-state" / "migration")
        if command == "export-history":
            store.export_history(metadata, args.out)
            _json({"status": "success", "operation": command, "path": args.out})
        else:
            store.recover(metadata, args.run_token, args.evidence)
            _json({"status": "success", "operation": command})
        return 0
    if command in {"register-app", "app-status", "recover-app-lock", "capture-app", "bootstrap-app", "adopt-app", "export-app", "import-app"}:
        config, target = _target(args, args.alias)
        if target.environment == "production" and command in {"register-app", "recover-app-lock"}:
            raise ConfigError(f"{command} is refused for production targets")
        store = _store(repo)
        if command == "register-app":
            checkout = os.environ.get("TEAM_CHECKOUT_UUID") or str(uuid.uuid4())
            entry = store.register_app(target, checkout, socket.gethostname(), os.environ.get("USER", "unknown"), transfer_from=args.transfer_from)
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
        if not args.confirm_pause:
            raise ConfigError("import-app requires --confirm-pause after the pause notice has been posted")
        baseline = import_app(target, args.ref, replace_from=args.replace_from, repo=repo, control_store=store)
        _json({"status": "success", "operation": command, "source_commit": baseline.source_commit})
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
        commit = _resolved_commit(repo, args.ref)
        tree = read_git_tree(repo, commit, args.alias)
        report = deploy_app(target, tree, commit, repo=repo, control_store=_store(repo))
        _json({"status": "success", "operation": command, "source_commit": report.source_commit, "tree_digest": report.tree_digest, "verified_tree_digest": report.verified_tree_digest, "recovery_id": report.recovery_id})
        return 0
    if command == "announce-import":
        config, target = _target(args, args.alias)
        if args.ref:
            store = _store(repo)
            roster = [entry.checkout_uuid for entry in store.list_registry(target)] if (repo / ".sync-state" / "control.json").is_file() else []
            announcement = draft_import_announcement(args.alias, _resolved_commit(repo, args.ref), target={"app_id": target.app_id, "workspace_id": target.workspace_id, "instance_id": target.instance_id}, roster=roster)
            print(announcement.text)
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
        "ci-replay": "ci",
        "apply-release": "release",
    }
    module_name = module_names[args.command]
    try:
        module = __import__(f"teamlib.{module_name}", fromlist=["main"])
    except ImportError as exc:
        raise ConfigError(f"offline command implementation is unavailable: {args.command}") from exc
    handler = getattr(module, "main", None)
    if handler is None:
        raise ConfigError(f"offline command has no public handler: {args.command}")
    command_prefixed = {"new-migration", "add-dependency", "build-release", "verify-release", "plan-release", "apply-release", "adopt-baseline", "ci-doctor", "ci-replay"}
    handler_args = [args.command, *args.args] if args.command in command_prefixed else list(args.args)
    result = handler(handler_args)
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
    except (ConfigError, ControlStoreError, StateError, PatchError, ApexError, MigrationRunError, MigrationStoreError, DeployError, ReleaseError, RunbookError, CIError, AppCheckError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2 if isinstance(exc, ConfigError) else 3


if __name__ == "__main__":
    sys.exit(main())
