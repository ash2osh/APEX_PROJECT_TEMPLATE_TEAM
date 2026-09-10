"""Linear, fail-closed orchestration for the protected integration run."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
from typing import Any
from collections.abc import Callable, Mapping

from .assertions import run_verification_member
from .config import Config, Target, profile_target
from .control_store import SqlControlStore
from .deploy import DeployReport, deploy_app
from .fingerprints import Inventory, InventoryError, diff_inventory, load_inventory
from .live_inventory import inventory_target
from .migrate import RunReport, apply_plan
from .migration_store import MigrationStoreError, SqlMigrationStore
from .qualification import qualify_target, write_report
from .release import ApplyReport, Manifest, verify_release
from .release_adapter import apply_verified_release_live
from .runtime import RuntimeReport, preflight_online
from .sqlcl import run_sqlcl
from .trees import read_git_tree


class OnlineWorkflowError(RuntimeError):
    """Raised when the protected integration sequence cannot continue."""

    def __init__(self, message: str, report: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.report = dict(report) if isinstance(report, Mapping) else None


@dataclass(frozen=True)
class OnlineDependencies:
    """Injectable boundaries for testing the online sequence without SQLcl."""

    resolve_head: Callable[[Path], str]
    preflight: Callable[..., RuntimeReport]
    setup_control: Callable[[Path, Config], None]
    bootstrap_metadata: Callable[[Path, Config], Any]
    read_state: Callable[[Any, Target], Mapping[str, Any]]
    adopt_frontier: Callable[[Path, Config, Any, Target, Any], str]
    capture_inventory: Callable[[Path, Config, str], Any]
    check_drift: Callable[[Path, Any], None]
    apply_migrations: Callable[[Path, Config, Any, str], Any]
    deploy_apps: Callable[[Path, Config, str], tuple[Any, ...]]
    qualify_integration: Callable[..., Mapping[str, Any]]
    verify_release: Callable[[Path], Manifest]
    apply_release_live: Callable[..., ApplyReport]
    qualify_release: Callable[..., Mapping[str, Any]]
    write_report: Callable[[Mapping[str, Any], Path], None]


@dataclass(frozen=True)
class OnlineRunResult:
    status: str
    source_commit: str
    report: Mapping[str, Any] | None
    confirmation_template: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_commit": self.source_commit,
            "report": dict(self.report) if self.report is not None else None,
            "confirmation_template": (
                dict(self.confirmation_template)
                if self.confirmation_template is not None
                else None
            ),
        }


def _schema_set_digest(config: Config) -> str:
    return hashlib.sha256(
        f"{config.tables_schema}|{config.code_schema}|{config.metadata_schema}".encode("ascii")
    ).hexdigest()


def _resolve_head(repo: Path) -> str:
    if repo.is_symlink() or not repo.is_dir():
        raise OnlineWorkflowError(f"integration repository is not a real directory: {repo}")
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD^{commit}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise OnlineWorkflowError("integration checkout does not resolve HEAD")
    commit = result.stdout.strip()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise OnlineWorkflowError("integration HEAD is not an exact commit")
    return commit


def _metadata(config: Config) -> Target:
    return profile_target(config, "METADATA")


def _setup_control(repo: Path, config: Config) -> None:
    metadata = _metadata(config)
    store = SqlControlStore(metadata, work_root=repo / "scratch" / "metadata")
    store.setup_state(
        [profile_target(config, "APEX", alias=alias) for alias in sorted(config.apps)]
    )


def _bootstrap_metadata(repo: Path, config: Config) -> SqlMigrationStore:
    metadata = _metadata(config)
    store = SqlMigrationStore(metadata, work_root=repo / "scratch" / "metadata")
    store.bootstrap(metadata, schema_set_digest=_schema_set_digest(config))
    return store


def _read_state(store: Any, target: Target) -> Mapping[str, Any]:
    try:
        state = store.read_state(target)
    except (MigrationStoreError, KeyError, TypeError, ValueError) as exc:
        raise OnlineWorkflowError(f"migration frontier state is unreadable: {exc}") from exc
    if not isinstance(state, Mapping):
        raise OnlineWorkflowError("migration frontier state is not an object")
    return state


def _adopt_frontier(
    repo: Path,
    config: Config,
    store: Any,
    metadata: Target,
    before: Any,
) -> str:
    digest = getattr(before, "digest", None)
    manifest_builder = getattr(before, "as_dict", None)
    manifest = manifest_builder() if callable(manifest_builder) else before
    if not isinstance(digest, str) or len(digest) != 64:
        raise OnlineWorkflowError("fresh integration frontier requires a verified inventory digest")
    if not callable(getattr(store, "record_inventory", None)) or not callable(getattr(store, "ensure_observation", None)):
        raise OnlineWorkflowError("migration store does not support observed frontier adoption")
    run_token = hashlib.sha256(os.urandom(32)).hexdigest()
    try:
        store.acquire(
            metadata,
            run_token,
            os.environ.get("USER", "integration-worker"),
            socket.gethostname(),
        )
        recorded = store.record_inventory(metadata, manifest, run_token=run_token)
        if recorded != digest:
            raise OnlineWorkflowError("adopted inventory digest changed during recording")
        store.ensure_observation(metadata, digest, run_token=run_token)
        store.release(metadata, run_token)
        return digest
    except OnlineWorkflowError:
        raise
    except Exception as exc:
        # A failed adoption deliberately leaves the mutex for the recovery
        # owner; importing over a shared workspace is never a repair path.
        raise OnlineWorkflowError(f"observed frontier adoption failed: {exc}") from exc


def _capture_inventory(repo: Path, config: Config, phase: str) -> Inventory:
    return inventory_target(
        profile_target(config, "TABLES"),
        config.tables_schema,
        config.code_schema,
        repo / "scratch" / "integration" / phase,
        schema_set_digest=_schema_set_digest(config),
    )


def _check_drift(expected_path: Path, before: Any) -> None:
    try:
        expected = load_inventory(expected_path)
    except InventoryError as exc:
        raise OnlineWorkflowError(f"canonical schema inventory is unreadable: {exc}") from exc
    if not isinstance(before, Inventory):
        digest = getattr(before, "digest", None)
        if not isinstance(digest, str):
            raise OnlineWorkflowError("live integration inventory is not qualified")
        raise OnlineWorkflowError(
            f"migration drift gate cannot compare the live inventory ({digest})"
        )
    drift = diff_inventory(expected, before)
    if any(drift[key] for key in ("added", "missing", "changed", "invalid")) or drift.get("topology_mismatch"):
        raise OnlineWorkflowError("migration drift gate is blocked: " + json.dumps(drift, sort_keys=True))


def _migration_callbacks(repo: Path, config: Config, schema_set_digest: str):
    def execute(migration, action, sql_path):
        target = profile_target(config, "TABLES" if migration.target == "tables" else "CODE")
        run_sqlcl(
            target,
            "write",
            sql_path,
            repo / "scratch" / "integration" / "migration-payload" / action / migration.id,
        )

    def verify(migration, action, verify_path):
        run_verification_member(
            profile_target(config, "VERIFY"),
            verify_path,
            repo / "scratch" / "integration" / "migration-verify" / action / migration.id,
            runner=run_sqlcl,
        )
        return True

    def observe(migration, phase):
        return inventory_target(
            profile_target(config, "TABLES"),
            config.tables_schema,
            config.code_schema,
            repo / "scratch" / "integration" / "migration-observation" / phase / migration.id,
            schema_set_digest=schema_set_digest,
        )

    return execute, verify, observe


def _migration_profiles(repo: Path, config: Config, store: Any, *, dry_run: bool, before_digest: str, source_commit: str):
    metadata = _metadata(config)
    schema_set_digest = _schema_set_digest(config)
    execute, verify, observe = _migration_callbacks(repo, config, schema_set_digest)
    return {
        "store": store,
        "target": metadata,
        "payload_targets": {
            "tables": profile_target(config, "TABLES"),
            "code": profile_target(config, "CODE"),
        },
        "dry_run": dry_run,
        "bootstrap": False,
        "schema_set_digest": schema_set_digest,
        "verified_inventory_digest": before_digest,
        "require_observation": True,
        "observe": observe,
        "source_commit": source_commit,
        "applied_by": os.environ.get("USER", "integration-worker"),
        "execute": execute,
        "verify": verify,
    }


def _apply_migrations(repo: Path, config: Config, store: Any, before_digest: str) -> RunReport:
    source_commit = _resolve_head(repo)
    migration_root = repo / "migrations"
    preview = apply_plan(
        migration_root,
        _migration_profiles(
            repo, config, store, dry_run=True,
            before_digest=before_digest, source_commit=source_commit,
        ),
    )
    if preview.confirmation_template is not None:
        return preview
    return apply_plan(
        migration_root,
        _migration_profiles(
            repo, config, store, dry_run=False,
            before_digest=before_digest, source_commit=source_commit,
        ),
        expected_plan={"pending": preview.selected},
    )


def _deploy_apps(repo: Path, config: Config, source_commit: str) -> tuple[DeployReport, ...]:
    metadata = _metadata(config)
    control_store = SqlControlStore(metadata, work_root=repo / "scratch" / "metadata")
    reports: list[DeployReport] = []
    for alias in sorted(config.apps):
        target = profile_target(config, "APEX", alias=alias)
        tree = read_git_tree(repo, source_commit, alias)
        reports.append(
            deploy_app(
                target,
                tree,
                source_commit,
                repo=repo,
                control_store=control_store,
            )
        )
    return tuple(reports)


def _qualify_integration(
    repo: Path,
    config: Config,
    source_commit: str,
    aliases: tuple[str, ...],
    *,
    store: Any,
    flow_executable: str,
    runtime_report: RuntimeReport,
) -> Mapping[str, Any]:
    return qualify_target(
        repo,
        config,
        source_commit,
        aliases,
        store=store,
        work=repo / "scratch" / "integration" / "qualification",
        flow_executable=flow_executable,
        runner_contract=repo / "ci" / "runner-contract.json",
        runtime_report=runtime_report,
    )


def _qualify_release(
    repo: Path,
    config: Config,
    source_commit: str,
    aliases: tuple[str, ...],
    *,
    release_archive: Path,
    apply_report: Mapping[str, Any],
    flow_executable: str,
    runtime_report: RuntimeReport,
) -> Mapping[str, Any]:
    return qualify_target(
        repo,
        config,
        source_commit,
        aliases,
        store=SqlMigrationStore(
            profile_target(config, "METADATA"),
            work_root=repo / "scratch" / "metadata",
        ),
        work=repo / "scratch" / "test" / "qualification",
        release_archive=release_archive,
        apply_report=apply_report,
        flow_executable=flow_executable,
        runner_contract=repo / "ci" / "runner-contract.json",
        runtime_report=runtime_report,
    )


def _default_dependencies() -> OnlineDependencies:
    return OnlineDependencies(
        resolve_head=_resolve_head,
        preflight=preflight_online,
        setup_control=_setup_control,
        bootstrap_metadata=_bootstrap_metadata,
        read_state=_read_state,
        adopt_frontier=_adopt_frontier,
        capture_inventory=_capture_inventory,
        check_drift=_check_drift,
        apply_migrations=_apply_migrations,
        deploy_apps=_deploy_apps,
        qualify_integration=_qualify_integration,
        verify_release=verify_release,
        apply_release_live=apply_verified_release_live,
        qualify_release=_qualify_release,
        write_report=write_report,
    )


def _confirmation_template(report: Any) -> Mapping[str, Any] | None:
    if isinstance(report, Mapping):
        value = report.get("confirmation_template")
    else:
        value = getattr(report, "confirmation_template", None)
    return dict(value) if isinstance(value, Mapping) else None


def run_integration(
    repo: str | Path,
    config: Config,
    out: str | Path,
    *,
    flow_executable: str,
    dependencies: OnlineDependencies | None = None,
) -> OnlineRunResult:
    """Run the one protected, observed integration qualification sequence."""
    if config.role != "integration" or config.environment == "production":
        raise OnlineWorkflowError(
            "run-integration requires a non-production integration target"
        )
    repo_path = Path(repo)
    deps = dependencies or _default_dependencies()
    source_commit = deps.resolve_head(repo_path)
    if not isinstance(source_commit, str) or len(source_commit) != 40 or any(
        char not in "0123456789abcdef" for char in source_commit
    ):
        raise OnlineWorkflowError("integration source must be an exact commit")
    aliases = tuple(sorted(config.apps))
    try:
        runtime = deps.preflight(config, repo_path, flow_executable)
    except RuntimeError as exc:
        raise OnlineWorkflowError(str(exc)) from exc
    deps.setup_control(repo_path, config)
    store = deps.bootstrap_metadata(repo_path, config)
    metadata = profile_target(config, "METADATA")
    before = deps.capture_inventory(repo_path, config, "integration-before")
    state = deps.read_state(store, metadata)
    if not isinstance(state, Mapping):
        raise OnlineWorkflowError("migration frontier state is not an object")
    observations = state.get("observations", [])
    if not isinstance(observations, list):
        raise OnlineWorkflowError("migration frontier observations are malformed")
    try:
        history = store.read_history(metadata)
    except Exception as exc:
        raise OnlineWorkflowError(f"migration history is unreadable: {exc}") from exc
    if not isinstance(history, Mapping):
        raise OnlineWorkflowError("migration history is not an object")
    if not observations:
        if history:
            raise OnlineWorkflowError("migration history exists without an observed frontier")
        deps.adopt_frontier(repo_path, config, store, metadata, before)
    deps.check_drift(repo_path / "database" / "schema-inventory.json", before)
    migration_report = deps.apply_migrations(
        repo_path, config, store, str(getattr(before, "digest", ""))
    )
    confirmation = _confirmation_template(migration_report)
    if confirmation is not None:
        return OnlineRunResult("maintenance-required", source_commit, None, confirmation)
    try:
        deps.deploy_apps(repo_path, config, source_commit)
        report = deps.qualify_integration(
            repo_path,
            config,
            source_commit,
            aliases,
            store=store,
            flow_executable=flow_executable,
            runtime_report=runtime,
        )
    except Exception as exc:
        evidence = getattr(exc, "report", None)
        if isinstance(evidence, Mapping):
            deps.write_report(evidence, Path(out))
        raise
    if not isinstance(report, Mapping):
        raise OnlineWorkflowError("integration qualification did not return a report")
    deps.write_report(report, Path(out))
    return OnlineRunResult("PASS", source_commit, report)


def run_release_test(
    repo: str | Path,
    config: Config,
    release_tar: str | Path,
    target_contract: str | Path,
    out: str | Path,
    *,
    flow_executable: str,
    dependencies: OnlineDependencies | None = None,
) -> OnlineRunResult:
    """Run the protected test qualification from a verified release archive."""
    if config.role != "test" or config.environment != "test":
        raise OnlineWorkflowError("run-release-test requires the protected test target")
    deps = dependencies or _default_dependencies()
    repo_path = Path(repo)
    archive_path = Path(release_tar)
    target_path = Path(target_contract)
    manifest = deps.verify_release(archive_path)
    app_digests = getattr(manifest, "app_tree_digests", None)
    if not isinstance(app_digests, Mapping):
        raise OnlineWorkflowError("release manifest application bindings are malformed")
    archive_aliases = tuple(sorted(app_digests))
    config_aliases = tuple(sorted(config.apps))
    if archive_aliases != config_aliases:
        raise OnlineWorkflowError(
            "release archive and test configuration application bindings differ"
        )
    try:
        runtime = deps.preflight(config, repo_path, flow_executable)
    except RuntimeError as exc:
        raise OnlineWorkflowError(str(exc)) from exc
    apply_report = deps.apply_release_live(
        archive_path,
        target_path,
        config,
        repo=repo_path,
    )
    if hasattr(apply_report, "as_dict") and callable(apply_report.as_dict):
        apply_document = apply_report.as_dict()
    elif isinstance(apply_report, Mapping):
        apply_document = dict(apply_report)
    else:
        raise OnlineWorkflowError("release application did not return a report")
    source_commit = getattr(manifest, "source_commit", None)
    if not isinstance(source_commit, str) or len(source_commit) != 40:
        raise OnlineWorkflowError("release manifest source commit is malformed")
    try:
        report = deps.qualify_release(
            repo_path,
            config,
            source_commit,
            config_aliases,
            release_archive=archive_path,
            apply_report=apply_document,
            flow_executable=flow_executable,
            runtime_report=runtime,
        )
    except Exception as exc:
        evidence = getattr(exc, "report", None)
        if isinstance(evidence, Mapping):
            deps.write_report(evidence, Path(out))
        raise
    if not isinstance(report, Mapping):
        raise OnlineWorkflowError("release qualification did not return a report")
    deps.write_report(report, Path(out))
    return OnlineRunResult("PASS", source_commit, report)
