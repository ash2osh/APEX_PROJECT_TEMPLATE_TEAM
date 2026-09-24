#!/usr/bin/env python3
"""Public team workflow entry point; all safety logic lives in teamlib."""

from __future__ import annotations

import argparse
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
    resolve_export,
)
from teamlib.announce import draft_publish_all_clear
from teamlib.app_checks import AppCheckError
from teamlib.ci import CIError
from teamlib.config import ConfigError, OFFLINE_COMMANDS, Target, contract_target, load_config, profile_target, schema_set_digest
from teamlib.control_store import ControlStoreError, SqlControlStore
from teamlib.deploy import DeployError, deploy_app
from teamlib.drift import capture_live_inventory, drift_status, observed_frontier_drift
from teamlib.fingerprints import InventoryError, diff_inventory, drift_is_clean, load_inventory
from teamlib.page_locks import LockReport, format_lock_report, load_manual_page_locks, read_page_locks
from teamlib.publish import PublishError, format_publish_notice, prepare_publish, publish_prepared
from teamlib.destructive_confirmation import (
    ConfirmationError,
    load_confirmation,
    write_confirmation_template,
)
from teamlib.migrate import MigrationRunError, adopt_members, apply_plan, apply_redo, apply_undo
from teamlib.migration_runtime import migration_profiles
from teamlib.migration_store import MigrationStoreError, SqlMigrationStore
from teamlib.live_inventory import inventory_target
from teamlib.qualification import QualificationError, qualify_target, write_report
from teamlib.patch import PatchError, recover_files
from teamlib.release import ReleaseError
from teamlib.release_adapter import ReleaseAdapterError
from teamlib.runbook import RunbookError
from teamlib.runtime import preflight_online
from teamlib.online_workflows import OnlineWorkflowError, run_integration, run_release_test
from teamlib.state import StateError
from teamlib.migration_bundle import BundleError, load_bundles
from teamlib.source_snapshot import IntegrationSource, SourceSnapshotError, load_integration_source
from teamlib.trees import TreeError, read_git_tree

# Commands that write controller or metadata state and are therefore refused for
# a production classification. Kept in one place so the parser, the online
# dispatcher and the tests cannot drift apart.
PRODUCTION_REFUSED_COMMANDS = frozenset(
    {
        "setup-state", "adopt-frontier", "adopt-migration-members", "qualify-target", "recover-migration",
        "register-app", "recover-app-lock", "migrate", "undo-migration", "redo-migration",
        "run-integration",
        "run-release-test",
    }
)

# Commands that must not reach the database without a validated VERIFY profile.
# Declared beside the refusal set so that adding a command to the dispatcher is
# one decision about its privileges, not a guess buried in a branch.
VERIFY_REQUIRED_COMMANDS = frozenset(
    {
        "adopt-frontier", "qualify-target", "run-integration", "run-release-test",
        "migrate", "undo-migration", "redo-migration", "check-drift", "deploy-app",
    }
)


COMMAND_HELP = {
    "doctor": ("Daily application work", "validate the selected credential-free target profile"),
    "export-app": ("Daily application work", "capture and reconcile the shared Builder application"),
    "prepare-publish": ("Daily application work", "prepare an app-scoped pause notice and durable evidence record"),
    "publish-app": ("Daily application work", "publish prepared and acknowledged changes to selected apps"),
    "run-integration": ("Protected qualification", "apply and qualify one exact commit on protected integration"),
    "qualify-target": ("Protected qualification", "read-only diagnosis of a persistent qualified target"),
    "migrate": ("Migration maintenance", "preview or apply reviewed forward migration bundles"),
    "undo-migration": ("Migration maintenance", "preview or apply one global-LIFO authored down bundle"),
    "redo-migration": ("Migration maintenance", "preview or reapply one explicitly reverted bundle"),
    "recover-migration": ("Recovery and diagnosis", "clear a retained migration mutex from reviewed evidence"),
    "build-release": ("Release and handoff", "build and self-verify one immutable release archive"),
    "run-release-test": ("Release and handoff", "apply and qualify one archive on the protected test target"),
    "sign-test-evidence": ("Release and handoff", "bind and sign canonical PASS evidence for one archive"),
    "gen-runbook": ("Release and handoff", "verify signed evidence and generate the production-owner handoff"),
    "setup-state": ("Daily application work", "initialize shared application controller metadata"),
    "register-app": ("Daily application work", "register this checkout for a configured shared application"),
    "app-status": ("Daily application work", "read current shared application controller status"),
    "capture-app": ("Daily application work", "capture the shared Builder application without reconciliation"),
    "bootstrap-app": ("Daily application work", "establish the first verified application baseline"),
    "adopt-app": ("Daily application work", "adopt an existing shared application with evidence"),
    "resolve-export": ("Daily application work", "apply a reviewed export conflict resolution"),
    "deploy-app": ("Protected qualification", "deploy exact committed application bytes to a qualified target"),
    "adopt-frontier": ("Migration maintenance", "adopt a sequence-zero observed schema frontier"),
    "check-drift": ("Migration maintenance", "compare live schema structure and accepted frontier"),
    "export-history": ("Migration maintenance", "export canonical migration history from metadata"),
    "adopt-migration-members": ("Migration maintenance", "store local files of already-applied migrations in shared metadata"),
    "new-migration": ("Migration maintenance", "author a new forward migration and verification pair"),
    "add-dependency": ("Migration maintenance", "add an exact checksum dependency to a migration"),
    "migration-plan": ("Migration maintenance", "calculate dependency-ordered pending migrations offline"),
    "recover-app-lock": ("Recovery and diagnosis", "clear an application mutex from reviewed evidence"),
    "recover-files": ("Recovery and diagnosis", "finish or restore an interrupted file journal"),
    "explain-conflict": ("Recovery and diagnosis", "explain an immutable export conflict bundle"),
    "prune-scratch": ("Recovery and diagnosis", "prune bounded disposable scratch outputs"),
    "snapshot": ("Recovery and diagnosis", "build a canonical schema inventory from framed rows"),
    "verify-history": ("Recovery and diagnosis", "validate migration bundle history offline"),
    "replay": ("Recovery and diagnosis", "run deterministic migration replay against an explicit target"),
    "adopt-baseline": ("Recovery and diagnosis", "adopt reviewed replay output as a baseline"),
    "verify-release": ("Release and handoff", "verify immutable release archive bytes and manifest"),
    "plan-release": ("Release and handoff", "plan a verified archive against supplied target history"),
    "apply-release": ("Release and handoff", "apply a verified archive to a non-production target"),
    "ci-doctor": ("Release and handoff", "validate the offline runner contract shape"),
}

COMMAND_DETAILS = {
    "export-app": (
        "Captures the shared Builder application and reconciles it with tracked source; "
        "concurrent Builder changes may require reconciliation."
    ),
    "prepare-publish": (
        "Prepares an app-scoped pause notice and durable evidence record for selected apps; "
        "read-only, does not modify Builder."
    ),
    "publish-app": (
        "Publishes prepared and acknowledged changes into shared development applications "
        "with all-app preflight and verified re-export."
    ),
    "run-integration": (
        "Performs non-production writes while applying and qualifying one immutable Git source."
    ),
    "qualify-target": (
        "Provides read-only diagnosis of the selected persistent target from one exact Git source."
    ),
    "sign-test-evidence": "Required argument: --archive RELEASE_TAR.",
    "build-release": (
        "Required arguments: --kind schema|app, --ref REF, --version SEMVER, --out DIR "
        "(and --alias for app releases)."
    ),
}

_HELP_CATEGORIES = (
    "Daily application work",
    "Protected qualification",
    "Migration maintenance",
    "Recovery and diagnosis",
    "Release and handoff",
)


def _top_level_help() -> str:
    lines = ["Command groups:"]
    for category in _HELP_CATEGORIES:
        lines.append(f"\n{category}:")
        for name, (command_category, description) in COMMAND_HELP.items():
            if command_category == category:
                lines.append(f"  {name:<22} {description}")
    lines.extend(
        (
            "\nNormal paths:",
            "  Daily:       export-app -> review/stage -> commit -> pull/rebase -> push",
            "  Integration: run-integration",
            "  Release:     build/verify -> run-release-test -> sign-test-evidence -> gen-runbook",
        )
    )
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    env_parent = argparse.ArgumentParser(add_help=False)
    env_parent.add_argument("--env", dest="env_file", default=argparse.SUPPRESS, help="literal environment profile file")

    parser = argparse.ArgumentParser(
        prog="team.py",
        description="APEX team round-trip and promotion workflow",
        parents=[env_parent],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_top_level_help(),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_command(name: str, *, online: bool = True) -> argparse.ArgumentParser:
        _category, description = COMMAND_HELP[name]
        return sub.add_parser(
            name,
            parents=[env_parent] if online else [],
            help=description,
            description=description,
            epilog=COMMAND_DETAILS.get(name),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

    add_command("doctor")
    add_command("setup-state")
    add_command("adopt-frontier")
    qualify = add_command("qualify-target")
    qualify.add_argument("--source-commit", required=True)
    qualify.add_argument("--aliases", required=True, help="comma-separated configured application aliases")
    qualify.add_argument("--out", required=True)
    qualify.add_argument("--release-archive")
    qualify.add_argument("--apply-report")
    integration = add_command("run-integration")
    integration.add_argument("--out", required=True)
    release_test = add_command("run-release-test")
    release_test.add_argument("archive")
    release_test.add_argument("--target", required=True)
    release_test.add_argument("--out", required=True)
    migrate = add_command("migrate")
    migrate.add_argument("--source", default="migrations")
    migrate.add_argument("--dry-run", action="store_true")
    migrate.add_argument("--bootstrap", action="store_true")
    migrate.add_argument("--expected-inventory")
    migrate.add_argument("--actual-inventory")
    migrate.add_argument("--destructive-confirmation")
    migrate.add_argument(
        "--confirmation-out",
        help="atomically write the false-only destructive review template",
    )
    for name in ("undo-migration", "redo-migration"):
        lifecycle = add_command(name)
        lifecycle.add_argument("migration_id")
        lifecycle.add_argument("--source", default="migrations")
        lifecycle.add_argument("--dry-run", action="store_true")
        lifecycle.add_argument("--expected-inventory")
        lifecycle.add_argument("--actual-inventory")
        lifecycle.add_argument("--destructive-confirmation")
        lifecycle.add_argument(
            "--confirmation-out",
            help="atomically write the false-only destructive review template",
        )
    drift = add_command("check-drift")
    drift.add_argument("--expected-inventory")
    drift.add_argument("--actual-inventory")
    drift.add_argument("--out")
    history = add_command("export-history")
    history.add_argument("--out", required=True)
    adopt_members_parser = add_command("adopt-migration-members")
    adopt_members_parser.add_argument("--source", default="migrations")
    adopt_members_parser.add_argument("--dry-run", action="store_true")
    recover_migration = add_command("recover-migration")
    recover_migration.add_argument("run_token")
    recover_migration.add_argument("--attempt")
    recover_migration.add_argument("--evidence", required=True)
    register = add_command("register-app")
    register.add_argument("alias")
    register.add_argument("--transfer-from")
    register.add_argument("--capture-recovery-id")
    status = add_command("app-status")
    status.add_argument("alias")
    recover_lock = add_command("recover-app-lock")
    recover_lock.add_argument("alias")
    recover_lock.add_argument("--run-token")
    recover_lock.add_argument("--evidence", required=True)
    capture = add_command("capture-app")
    capture.add_argument("alias")
    for name in ("bootstrap-app", "adopt-app", "export-app"):
        command = add_command(name)
        command.add_argument("alias")
    resolve = add_command("resolve-export")
    resolve.add_argument("recovery_id")
    resolve.add_argument("--resolved", required=True)
    prep = add_command("prepare-publish")
    prep.add_argument("aliases", nargs="+", help="one or more configured application aliases to publish")
    prep.add_argument("--ref", required=True, help="exact 40-character hex commit to publish")
    prep.add_argument(
        "--manual-lock-report",
        action="append",
        default=[],
        help="manual page lock report in format <alias>:<path.json>",
    )
    prep.add_argument(
        "--replace-from",
        action="append",
        default=[],
        help="replace from recovery capture in format <alias>:<recovery-id>",
    )
    pub = add_command("publish-app")
    pub.add_argument("--prepared", required=True, help="preparation ID from prepare-publish")
    pub.add_argument("--confirm-pause", action="store_true", help="confirm that the publish pause notice has been posted and editors stopped")
    pub.add_argument(
        "--ack",
        action="append",
        default=[],
        help="registered teammate checkout acknowledgement in format <alias>:<uuid>",
    )
    deploy = add_command("deploy-app")
    deploy.add_argument("alias")
    deploy.add_argument("--target", required=True)
    deploy.add_argument("--ref", required=True)
    files = add_command("recover-files")
    files.add_argument("operation_id")
    files.add_argument("--action", choices=("finish", "restore"), required=True)

    # Offline commands deliberately have no --env dependency. Their concrete
    # options are parsed by their owning modules when those modules exist.
    for name in sorted(OFFLINE_COMMANDS):
        command = add_command(name, online=False)
        command.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def _env_path(args: argparse.Namespace) -> Path:
    explicit = getattr(args, "env_file", None) or os.environ.get("PROJECT_ENV_FILE")
    if explicit:
        return Path(explicit)
    return _repo_root() / ".env"


def _target(config: Any, alias: str) -> Target:
    if alias not in config.apps:
        raise ConfigError(f"unknown application alias: {alias}")
    return profile_target(config, "APEX", alias=alias)


def _sql_control_store(repo: Path, metadata: Target) -> SqlControlStore:
    return SqlControlStore(metadata, work_root=repo / "scratch" / "metadata")


def _sql_migration_store(repo: Path, metadata: Target) -> SqlMigrationStore:
    return SqlMigrationStore(metadata, work_root=repo / "scratch" / "metadata")


def _json(value: object) -> None:
    print(json.dumps(value, sort_keys=True, ensure_ascii=False))


def _resolved_commit(repo: Path, ref: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ConfigError(f"could not resolve source ref: {ref}")
    return result.stdout.strip()


def _qualification_source(
    repo: Path, source_commit: str, aliases: tuple[str, ...]
) -> IntegrationSource:
    """Load qualification checks only from the caller's exact Git commit."""
    try:
        return load_integration_source(repo, source_commit, aliases)
    except SourceSnapshotError as exc:
        raise ConfigError(f"qualification source snapshot failed: {exc}") from exc


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
    if not drift_is_clean(drift):
        raise MigrationRunError("migration drift gate is blocked: " + json.dumps(drift, sort_keys=True))
    return actual.digest


def _migration_output(
    command: str,
    report: Any,
    *,
    dry_run: bool,
    confirmation_out: str | Path | None = None,
) -> None:
    if confirmation_out and not dry_run:
        raise ConfigError("--confirmation-out requires --dry-run")
    confirmation_path = None
    if confirmation_out:
        if report.confirmation_template is None:
            raise ConfigError("no destructive confirmation is required")
        try:
            confirmation_path = str(
                write_confirmation_template(report.confirmation_template, confirmation_out)
            )
        except ConfirmationError as exc:
            raise ConfigError(str(exc)) from exc
    payload = {
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
    }
    if confirmation_path is not None:
        payload["confirmation_path"] = confirmation_path
    _json(payload)


def _online(args: argparse.Namespace) -> object:
    repo = _repo_root()
    command = args.command
    config = load_config(_env_path(args), require_verify=command in VERIFY_REQUIRED_COMMANDS)
    # One gate for every production-refused command. Repeating this per branch
    # let three spellings of the same rule drift apart -- two consulted the
    # frozenset, one hand-wrote the command name, and two commands relied on a
    # guard inside the workflow function instead. Adding a command to
    # PRODUCTION_REFUSED_COMMANDS must be sufficient to refuse it.
    if config.environment == "production" and command in PRODUCTION_REFUSED_COMMANDS:
        raise ConfigError(f"{command} is refused for production targets")
    if command == "doctor":
        _json({"status": "valid", "project": config.project, "role": config.role, "environment": config.environment, "profiles": sorted(config.profiles)})
        return 0
    if command == "setup-state":
        metadata = profile_target(config, "METADATA")
        store = _sql_control_store(repo, metadata)
        store.setup_state([profile_target(config, "APEX", alias=alias) for alias in config.apps])
        _json({"status": "success", "operation": "setup-state", "targets": sorted(config.apps)})
        return 0
    if command == "adopt-frontier":
        metadata = profile_target(config, "METADATA")
        store = _sql_migration_store(repo, metadata)
        digest = schema_set_digest(config)
        store.bootstrap(metadata, schema_set_digest=digest)
        inventory = inventory_target(
            profile_target(config, "TABLES"),
            config.tables_schema,
            config.code_schema,
            repo / "scratch" / "frontier-inventory",
            schema_set_digest=digest,
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
        if bool(args.release_archive) != bool(args.apply_report):
            raise ConfigError("--release-archive and --apply-report must be supplied together")
        if _resolved_commit(repo, "HEAD") != args.source_commit:
            raise ConfigError("qualification checkout is not the exact source commit")
        aliases = tuple(part.strip() for part in args.aliases.split(",") if part.strip())
        source = _qualification_source(repo, args.source_commit, aliases)
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
                check_bundle=source.check_bundle,
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
    if command == "run-integration":
        result = run_integration(
            repo,
            config,
            Path(args.out),
            flow_executable=os.environ.get("TEAM_FLOW_RUNNER", ""),
        )
        _json({"operation": command, **result.as_dict()})
        return 0 if result.status == "PASS" else 3
    if command == "run-release-test":
        result = run_release_test(
            repo,
            config,
            args.archive,
            args.target,
            Path(args.out),
            flow_executable=os.environ.get("TEAM_FLOW_RUNNER", ""),
        )
        _json({"operation": command, **result.as_dict()})
        return 0 if result.status == "PASS" else 3
    if command in {"migrate", "undo-migration", "redo-migration"}:
        if args.confirmation_out and not args.dry_run:
            raise ConfigError("--confirmation-out requires --dry-run")
        verified_digest = _migration_drift(args, config, repo)
        metadata = profile_target(config, "METADATA")
        store = _sql_migration_store(repo, metadata)
        confirmation = None
        if args.destructive_confirmation:
            try:
                confirmation, _confirmation_digest = load_confirmation(args.destructive_confirmation)
            except ConfirmationError as exc:
                raise ConfigError(str(exc)) from exc
        profiles = migration_profiles(
            config,
            metadata,
            store,
            repo / "scratch",
            source_commit=_resolved_commit(repo, "HEAD"),
            applied_by=os.environ.get("USER", "migration-worker"),
            dry_run=args.dry_run,
            bootstrap=getattr(args, "bootstrap", False),
            verified_inventory_digest=verified_digest,
        )
        if command == "migrate":
            report = apply_plan(args.source, profiles, confirmation=confirmation)
        elif command == "undo-migration":
            report = apply_undo(args.source, args.migration_id, profiles, confirmation=confirmation)
        else:
            report = apply_redo(args.source, args.migration_id, profiles, confirmation=confirmation)
        _migration_output(
            command,
            report,
            dry_run=args.dry_run,
            confirmation_out=args.confirmation_out,
        )
        return 0
    if command == "check-drift":
        if bool(args.expected_inventory) != bool(args.actual_inventory):
            raise ConfigError("check-drift requires both --expected-inventory and --actual-inventory")
        if not args.expected_inventory:
            try:
                _actual_inventory, actual_path = capture_live_inventory(repo, config)
            except Exception as exc:
                raise MigrationRunError(f"live drift inventory failed: {exc}") from exc
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
        metadata = profile_target(config, "METADATA")
        frontier_result = observed_frontier_drift(_sql_migration_store(repo, metadata), metadata, actual)
        result["observed_frontier"] = frontier_result
        if args.out:
            Path(args.out).write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
        status = drift_status(result, frontier_result)
        _json({"status": status, "operation": command, "diff": result})
        return 0 if status == "clean" else 3
    if command == "adopt-migration-members":
        metadata = profile_target(config, "METADATA")
        store = _sql_migration_store(repo, metadata)
        # Bootstrap is idempotent and adds the member tables to an older metadata schema.
        store.bootstrap(metadata, schema_set_digest=schema_set_digest(config))
        report = adopt_members(
            store,
            metadata,
            load_bundles(args.source),
            dry_run=args.dry_run,
            actor=os.environ.get("USER", "member-backfill"),
            worker_identity=os.environ.get("USER", "member-backfill"),
            host=socket.gethostname(),
        )
        _json({"operation": command, **report})
        return 0
    if command in {"export-history", "recover-migration"}:
        metadata = profile_target(config, "METADATA")
        store = _sql_migration_store(repo, metadata)
        if command == "export-history":
            store.export_history(metadata, args.out)
            _json({"status": "success", "operation": command, "path": args.out})
        else:
            store.recover(metadata, args.run_token, args.evidence, attempt_id=args.attempt)
            _json({"status": "success", "operation": command})
        return 0
    if command == "prepare-publish":
        if config.role != "developer" or config.environment != "development":
            raise ConfigError("prepare-publish is restricted to the shared development environment")
        if not args.aliases:
            raise ConfigError("prepare-publish requires at least one application alias")
        if len(set(args.aliases)) != len(args.aliases):
            raise ConfigError("duplicate application aliases specified for prepare-publish")
        manual_reports: dict[str, Path] = {}
        for entry in args.manual_lock_report:
            if ":" not in entry:
                raise ConfigError(f"invalid --manual-lock-report argument: {entry}; expected <alias>:<path>")
            alias, path_str = entry.split(":", 1)
            if alias not in args.aliases:
                raise ConfigError(f"--manual-lock-report alias '{alias}' is not among selected aliases")
            manual_reports[alias] = Path(path_str)
        replace_map: dict[str, str] = {}
        for entry in args.replace_from:
            if ":" not in entry:
                raise ConfigError(f"invalid --replace-from argument: {entry}; expected <alias>:<recovery-id>")
            alias, rec_id = entry.split(":", 1)
            if alias not in args.aliases:
                raise ConfigError(f"--replace-from alias '{alias}' is not among selected aliases")
            replace_map[alias] = rec_id

        targets: list[Target] = []
        for alias in args.aliases:
            targets.append(_target(config, alias))

        lock_reports: dict[str, LockReport] = {}
        for target in targets:
            alias = target.alias or ""
            if alias in manual_reports:
                report = load_manual_page_locks(manual_reports[alias], target)
            else:
                report = read_page_locks(target)
            lock_reports[alias] = report
            if report.status == "UNKNOWN":
                print(format_lock_report(report), file=sys.stderr)
                raise ConfigError(f"page lock report for '{alias}' is UNKNOWN; publish preparation refused")

        metadata = profile_target(config, "METADATA")
        store = _sql_control_store(repo, metadata)
        try:
            prep = prepare_publish(
                repo,
                tuple(targets),
                args.ref,
                lock_reports,
                store,
                replace_from=replace_map,
            )
        except PublishError as exc:
            raise ConfigError(str(exc)) from exc

        notice = format_publish_notice(prep, operator=os.environ.get("USER", "the import operator"))
        print(notice)
        _json({
            "status": "success",
            "operation": command,
            "preparation_id": prep.preparation_id,
            "record_digest": prep.record_digest,
            "aliases": list(prep.aliases),
            "source_commit": prep.source_commit,
            "record_path": str(prep.path),
        })
        return 0
    if command == "publish-app":
        if config.role != "developer" or config.environment != "development":
            raise ConfigError("publish-app is restricted to the shared development environment")
        if not args.confirm_pause:
            if not sys.stdin.isatty():
                raise ConfigError("publish-app requires --confirm-pause in a non-interactive session")
            answer = input("Has the publish pause notice been posted and has everyone acknowledged? Type 'proceed' to continue: ")
            if answer.strip().casefold() != "proceed":
                raise ConfigError("publish-app cancelled; explicit pause confirmation was not received")
        acknowledgements: dict[str, list[str]] = {}
        for entry in args.ack:
            if ":" not in entry:
                raise ConfigError(f"invalid --ack argument: '{entry}'; expected <alias>:<uuid>")
            alias, ack_uuid = entry.split(":", 1)
            acknowledgements.setdefault(alias, []).append(ack_uuid)

        metadata = profile_target(config, "METADATA")
        store = _sql_control_store(repo, metadata)
        try:
            report = publish_prepared(
                repo,
                args.prepared,
                acknowledgements,
                confirm_pause=True,
                config=config,
                store=store,
            )
        except PublishError as exc:
            raise ConfigError(str(exc)) from exc

        if report.all_clear_allowed:
            print(draft_publish_all_clear(
                list(report.app_results.keys()),
                {alias: {"verified": r.verified, "recovery_path": r.recovery_path} for alias, r in report.app_results.items()},
            ))

        _json({
            "status": "success" if report.overall_status == "VERIFIED" else "partial",
            "operation": command,
            "preparation_id": report.preparation_id,
            "overall_status": report.overall_status,
            "all_clear_allowed": report.all_clear_allowed,
            "source_commit": report.source_commit,
            "apps": {
                alias: {
                    "status": r.status,
                    "verified": r.verified,
                    "operation_id": r.operation_id,
                    "recovery_path": r.recovery_path,
                    "error": r.error,
                }
                for alias, r in report.app_results.items()
            },
            "journal_path": str(report.journal_path),
        })
        return 0
    if command in {"register-app", "app-status", "recover-app-lock", "capture-app", "bootstrap-app", "adopt-app", "export-app"}:
        target = _target(config, args.alias)
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
    if command == "resolve-export":
        # Resolve uses the target selected by the environment; the recovery ID
        # itself remains immutable and is checked by the core implementation.
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
        target = contract_target(args.target, args.alias)
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
    except (ConfigError, ControlStoreError, StateError, PatchError, ApexError, MigrationRunError, MigrationStoreError, DeployError, ReleaseError, ReleaseAdapterError, RunbookError, CIError, AppCheckError, QualificationError, TreeError, BundleError, InventoryError, OnlineWorkflowError) as exc:
        print(str(exc), file=sys.stderr)
        recovery = getattr(exc, "recovery", None)
        if isinstance(recovery, dict):
            print(json.dumps({"recovery": recovery}, sort_keys=True), file=sys.stderr)
        return 2 if isinstance(exc, ConfigError) else 3


if __name__ == "__main__":
    sys.exit(main())
