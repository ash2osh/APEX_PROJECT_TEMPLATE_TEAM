"""Exact-source deployment to named non-production targets."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import uuid
from typing import Any, Callable, Mapping

from .apex import _export_driver, _find_export_dir, _import_driver, _verify_result_identity, _default_repo
from .config import Target
from .control_store import ControlStore, ControlStoreError
from .masters import MasterError, validate_masters
from .sqlcl import run_sqlcl, SqlclError
from .state import save_capture
from .trees import Tree, TreeError, _validate_tree_paths, read_export_tree, tree_digest


class DeployError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeployReport:
    source_commit: str
    target_key: str
    tree_digest: str
    verified_tree_digest: str
    recovery_id: str
    subscription_digest: str | None = None


def _capture_destination(target: Target, repo: Path, root: Path, store: ControlStore, runner: Callable[..., Any], held_by: str) -> tuple[dict[str, bytes], Any, Path]:
    before = store.read_app_sync_state(target.physical_key)
    if before.owner_token != held_by:
        raise DeployError("destination capture requires the current app mutex owner")
    work = repo / "scratch" / f"deploy-capture-{uuid.uuid4().hex}"
    work.mkdir(parents=True, exist_ok=False)
    driver = work / "export.sql"
    driver.write_text(_export_driver(target, work), encoding="utf-8", newline="\n")
    result = runner(target, "read", driver, work)
    _verify_result_identity(target, result)
    tree = read_export_tree(_find_export_dir(work))
    after = store.read_app_sync_state(target.physical_key)
    if before.generation != after.generation or before.owner_token != after.owner_token:
        raise DeployError("destination changed during capture")
    return tree, result, work


def deploy_app(
    target: Target,
    source_tree: Tree,
    source_commit: str,
    replay_proof: Mapping[str, Any] | None = None,
    *,
    repo: str | Path = ".",
    root: str | Path | None = None,
    control_store: ControlStore | None = None,
    runner: Callable[..., Any] = run_sqlcl,
) -> DeployReport:
    if target.role not in {"integration", "test", "replay"}:
        raise DeployError("deploy-app requires integration, test, or replay role")
    if target.environment == "production":
        raise DeployError("production deployment is refused")
    if not source_commit or any(char.isspace() for char in source_commit):
        raise DeployError("deployment requires an exact source commit")
    if target.role == "replay":
        if not isinstance(replay_proof, Mapping) or replay_proof.get("verified") is not True or replay_proof.get("target_key") != target.physical_key:
            raise DeployError("replay deployment requires a matching disposable provisioner proof")
    try:
        _validate_tree_paths(source_tree)
    except TreeError as exc:
        raise DeployError(str(exc)) from exc
    repo_path = _default_repo(repo)
    state_root = Path(root) if root is not None else repo_path / ".sync-state"
    store = control_store or ControlStore(state_root)
    token = uuid.uuid4().hex
    try:
        store.acquire_app(target.physical_key, token, "deploy-worker", "local", "deploy-worker")
    except ControlStoreError as exc:
        raise DeployError(str(exc)) from exc
    payload_started = False
    try:
        current, _, _ = _capture_destination(target, repo_path, state_root, store, runner, token)
        recovery_id = save_capture(target, {}, {}, source_commit, current, {"kind": "deployment", "tree_digest": tree_digest(current)}, root=state_root)
        contract_path = repo_path / "targets" / "masters.json"
        if contract_path.is_file():
            try:
                validate_masters(source_tree, target, json.loads(contract_path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, json.JSONDecodeError, MasterError) as exc:
                raise DeployError(f"master contract validation failed: {exc}") from exc
        store.mark_payload_starting(target.physical_key, token)
        payload_started = True
        work = repo_path / "scratch" / f"deploy-import-{uuid.uuid4().hex}"
        staged = work / "source"
        staged.mkdir(parents=True, exist_ok=False)
        for relative, data in source_tree.items():
            destination = staged / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        driver = work / "import.sql"
        driver.write_text(_import_driver(target, staged), encoding="utf-8", newline="\n")
        result = runner(target, "write", driver, work)
        _verify_result_identity(target, result)
        verified, _, _ = _capture_destination(target, repo_path, state_root, store, runner, token)
        if verified != dict(source_tree):
            raise DeployError("post-deployment export does not match exact source bytes")
        store.release_app(target.physical_key, token, confirmed_success=True)
        return DeployReport(source_commit, target.physical_key, tree_digest(source_tree), tree_digest(verified), recovery_id)
    except Exception as exc:
        if payload_started and isinstance(exc, SqlclError):
            raise DeployError(f"deployment result is unknown; retain mutex for recovery: {exc}") from exc
        try:
            store.release_app(target.physical_key, token, confirmed_success=False)
        except ControlStoreError:
            pass
        if isinstance(exc, DeployError):
            raise
        raise DeployError(f"deployment failed: {exc}") from exc
