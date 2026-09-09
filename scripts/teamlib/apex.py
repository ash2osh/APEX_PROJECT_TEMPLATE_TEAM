"""Verified APEX export/import orchestration built on the SQLcl boundary."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import uuid
from typing import Any
from collections.abc import Callable, Mapping

from .config import Target
from .control_store import ControlStore, ControlStoreError
from .masters import MasterError, apex_component_resolver, validate_masters
from .patch import PatchError, apply_tree
from .reconcile import Decision, reconcile
from .sqlcl import SqlclError, run_sqlcl
from .state import (
    Baseline,
    StateError,
    load_baseline,
    load_checkpoint,
    load_capture,
    required_absences,
    save_capture,
    save_checkpoint,
    save_receipt,
    save_verified_baseline,
)
from .trees import Tree, TreeError, read_git_tree, read_export_tree, tree_digest, assert_source_clean


class ApexError(RuntimeError):
    """Raised when a verified APEX operation cannot safely continue."""


class ExportConflict(ApexError):
    def __init__(self, message: str, *, decision: Decision, recovery_id: str):
        super().__init__(message)
        self.decision = decision
        self.recovery_id = recovery_id


@dataclass(frozen=True)
class ApexCapture:
    tree: dict[str, bytes]
    recovery_id: str
    target: Target
    before_sync: Any
    after_sync: Any
    work_dir: Path
    sql_result: Any


def _default_repo(repo: str | Path | None) -> Path:
    path = Path(repo or ".")
    if not path.is_dir() or path.is_symlink():
        raise ApexError(f"repository is not a real directory: {path}")
    return path


def _state_root(repo: Path, root: str | Path | None) -> Path:
    return Path(root) if root is not None else repo / ".sync-state"


def _store(repo: Path, root: Path, control_store: ControlStore | None) -> ControlStore:
    return control_store or ControlStore(root)


def _assert_app_target(target: Target) -> None:
    if target.alias is None or target.workspace_id is None or target.app_id is None:
        raise ApexError("APEX operations require a bound alias, workspace and application ID")


def _git_head(repo: Path, ref: str = "HEAD") -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ApexError("Git is unavailable") from exc
    if result.returncode != 0:
        raise ApexError(f"could not resolve Git ref: {ref}")
    return result.stdout.decode("ascii").strip()


def _export_driver(target: Target, export_dir: Path) -> str:
    if target.app_id is None:
        raise ApexError("export target has no application ID")
    escaped = str(export_dir).replace('"', '""')
    return (
        "SET DEFINE OFF\n"
        f"APEX EXPORT -APPLICATIONID {target.app_id} -EXPTYPE APEXLANG -OVERWRITE-FILES -DIR \"{escaped}\"\n"
    )


def _import_driver(target: Target, source_dir: Path) -> str:
    if target.app_id is None or target.workspace_id is None or target.parsing_schema is None:
        raise ApexError("import target has incomplete application identity")
    escaped = str(source_dir).replace('"', '""')
    return (
        "SET DEFINE OFF\n"
        f"APEX IMPORT -INPUT \"{escaped}\" -ID {target.app_id} "
        f"-WORKSPACEID {target.workspace_id} -SCHEMA {target.parsing_schema}\n"
    )


def _find_export_dir(work: Path) -> Path:
    candidates: list[Path] = []
    for application in work.rglob("application.apx"):
        if application.is_symlink() or not application.is_file():
            raise ApexError("APEX export contains a symlink or non-regular application.apx")
        parent = application.parent
        metadata = parent / ".apex" / "apexlang.json"
        if not metadata.is_file() or metadata.is_symlink():
            raise ApexError(f"APEX export is missing .apex/apexlang.json: {parent}")
        candidates.append(parent)
    if len(candidates) != 1:
        raise ApexError(f"expected exactly one complete APEX export, found {len(candidates)}")
    return candidates[0]


def _verify_result_identity(target: Target, result: Any) -> None:
    identity = getattr(result, "identity", None)
    if not isinstance(identity, Mapping):
        raise ApexError("APEX SQLcl result has no verified identity")
    expected = {
        "SESSION_USER": target.session_user,
        "CURRENT_SCHEMA": target.current_schema,
        "DB_NAME": target.db_name,
        "SERVICE": target.service,
        "INSTANCE_ID": target.instance_id,
    }
    if any(identity.get(key) != value for key, value in expected.items()):
        raise ApexError("APEX SQLcl result identity does not match target")
    completion = getattr(result, "completion", None)
    if not isinstance(completion, Mapping) or completion.get("operation") != "read" and completion.get("operation") != "write":
        raise ApexError("APEX SQLcl result has no operation completion")


def _sync_allowed(before: Any, after: Any, held_by: str | None) -> None:
    same_owner = held_by is not None and before.owner_token == held_by and after.owner_token == held_by
    if before.generation != after.generation:
        raise ApexError("application import generation changed during capture; retry export")
    if before.owner_token != after.owner_token:
        raise ApexError("application mutex changed during capture; retry export")
    if before.owner_token is not None and not same_owner:
        raise ApexError("application import is in progress; capture was discarded")
    if (before.is_uncertain or after.is_uncertain) and not same_owner:
        raise ApexError("application target is uncertain; reviewed recovery is required")


def capture_app(
    target: Target,
    held_by: str | None = None,
    *,
    repo: str | Path = ".",
    root: str | Path | None = None,
    control_store: ControlStore | None = None,
    runner: Callable[..., Any] = run_sqlcl,
    persist: bool = True,
) -> ApexCapture:
    """Export one app and accept it only across a stable sync-state window."""
    _assert_app_target(target)
    repo_path = _default_repo(repo)
    state_root = _state_root(repo_path, root)
    store = _store(repo_path, state_root, control_store)
    try:
        before = store.read_app_sync_state(target.physical_key)
    except ControlStoreError as exc:
        raise ApexError(str(exc)) from exc
    if before.owner_token is not None and before.owner_token != held_by:
        raise ApexError("application import mutex is held; capture refused")
    if before.is_uncertain and before.owner_token != held_by:
        raise ApexError("application target is uncertain; capture refused")

    work = repo_path / "scratch" / f"apex-capture-{uuid.uuid4().hex}"
    work.mkdir(parents=True, exist_ok=False)
    driver = work / "export.sql"
    driver.write_text(_export_driver(target, work), encoding="utf-8", newline="\n")
    try:
        result = runner(target, "read", driver, work)
    except Exception as exc:
        raise ApexError(f"APEX export failed: {exc}") from exc
    _verify_result_identity(target, result)
    export_dir = _find_export_dir(work)
    try:
        tree = read_export_tree(export_dir)
    except TreeError as exc:
        raise ApexError(str(exc)) from exc
    try:
        after = store.read_app_sync_state(target.physical_key)
    except ControlStoreError as exc:
        raise ApexError(str(exc)) from exc
    _sync_allowed(before, after, held_by)

    head = _git_head(repo_path)
    recovery_id = save_capture(
        target,
        {},
        {},
        head,
        tree,
        {
            "kind": "apex-capture",
            "tree_digest": tree_digest(tree),
            "before_sync": before.__dict__,
            "after_sync": after.__dict__,
            "work_dir": str(work),
        },
        root=state_root,
    ) if persist else ""
    return ApexCapture(tree, recovery_id, target, before, after, work, result)


def export_app(
    target: Target,
    *,
    repo: str | Path = ".",
    root: str | Path | None = None,
    control_store: ControlStore | None = None,
    runner: Callable[..., Any] = run_sqlcl,
) -> Decision:
    if target.role != "developer" or target.environment != "development":
        raise ApexError("export-app is restricted to the shared development target")
    _assert_app_target(target)
    repo_path = _default_repo(repo)
    state_root = _state_root(repo_path, root)
    store = _store(repo_path, state_root, control_store)
    try:
        assert_source_clean(repo_path, target.alias or "")
    except TreeError as exc:
        raise ApexError(str(exc)) from exc
    head = _git_head(repo_path)
    try:
        checkpoint = load_checkpoint(target, head, root=state_root)
    except StateError as exc:
        capture = capture_app(target, repo=repo_path, root=state_root, control_store=store, runner=runner)
        raise ApexError(
            f"no verified export checkpoint; retained recovery {capture.recovery_id} under {state_root / 'recovery'}"
        ) from exc
    capture = capture_app(target, repo=repo_path, root=state_root, control_store=store, runner=runner, persist=False)
    try:
        head_tree = read_git_tree(repo_path, head, target.alias or "")
    except TreeError as exc:
        raise ApexError(str(exc)) from exc
    decision = reconcile(checkpoint.captured, head_tree, capture.tree, source_base=checkpoint.reconciled)
    recovery_id = save_capture(
        target,
        checkpoint.captured,
        checkpoint.reconciled,
        head_tree,
        capture.tree,
        {
            "kind": "export",
            "tree_digest": tree_digest(capture.tree),
            "conflicts": list(decision.conflicts),
            "head_commit": head,
            "work_dir": str(capture.work_dir),
        },
        root=state_root,
    )
    if decision.conflicts:
        raise ExportConflict(
            f"export has conflicts in {', '.join(decision.conflicts)}; review recovery {recovery_id}",
            decision=decision,
            recovery_id=recovery_id,
        )
    absent = required_absences(
        checkpoint.required_absent,
        checkpoint.captured,
        checkpoint.reconciled,
        head_tree,
        capture.tree,
        decision.tree,
    )
    try:
        apply_tree(repo_path, target.alias or "", head, head_tree, decision.tree, recovery_id)
        receipt_id = save_receipt(
            target,
            decision.tree,
            head,
            absent,
            tree_digest(capture.tree),
            root=state_root,
        )
        save_checkpoint(
            target,
            capture.tree,
            decision.tree,
            head,
            receipt_id,
            anchor_commit=head,
            required_absent=absent,
            root=state_root,
        )
    except (PatchError, StateError) as exc:
        raise ApexError(f"export was not durably completed; recovery {recovery_id} retained") from exc
    return decision


def _materialize_tree(root: Path, tree: Tree) -> Path:
    if root.exists():
        if root.is_symlink():
            raise ApexError("staging directory must not be a symlink")
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=False)
    for relative, data in tree.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ApexError(f"staging path is a symlink: {relative}")
        path.write_bytes(data)
    return root


def _receipt_allows_import(target: Target, capture_tree: Tree, selected_tree: Tree, commit: str, state_root: Path) -> bool:
    receipt_dir = state_root / "receipts" / target.state_key
    if not receipt_dir.is_dir():
        return False
    for path in sorted(receipt_dir.glob("*.json")):
        try:
            from .state import load_receipt
            receipt = load_receipt(target, path.stem, root=state_root)
        except StateError:
            continue
        # A normal capture receipt is tied to the exact selected commit.  A
        # resolution receipt is tied to the reviewed result tree instead: the
        # normal workflow creates the receipt before the developer commits the
        # resolved source, so the eventual commit ID is necessarily newer.
        if receipt.selected_commit == commit or receipt.kind == "resolution":
            from .trees import receipt_satisfied
            try:
                if receipt.kind == "resolution":
                    # A resolution receipt is a two-sided proof: the live
                    # capture must be the exact capture the developer
                    # resolved, and the selected commit must be the exact
                    # resolved result.  Treating its result as a normal
                    # capture would make the baseline guard reject every
                    # legitimate conflict-resolution import.
                    if receipt.capture_digest != tree_digest(capture_tree):
                        continue
                    if receipt.resolved_digest != tree_digest(selected_tree):
                        continue
                    if receipt.result != dict(selected_tree):
                        continue
                    if receipt_satisfied(receipt.result, receipt.required_absent, selected_tree):
                        return True
                elif receipt_satisfied(capture_tree, receipt.required_absent, selected_tree):
                    return True
            except (ValueError, TreeError):
                continue
    return False


def import_app(
    target: Target,
    commit: str,
    replace_from: str | None = None,
    *,
    repo: str | Path = ".",
    root: str | Path | None = None,
    control_store: ControlStore | None = None,
    runner: Callable[..., Any] = run_sqlcl,
    checkout_uuid: str = "local-checkout",
    host: str = "local",
    user: str = "developer",
) -> Baseline:
    if target.role != "developer" or target.environment != "development":
        raise ApexError("import-app is restricted to the shared development target")
    _assert_app_target(target)
    repo_path = _default_repo(repo)
    state_root = _state_root(repo_path, root)
    store = _store(repo_path, state_root, control_store)
    try:
        assert_source_clean(repo_path, target.alias or "")
        resolved = _git_head(repo_path, commit)
        selected_tree = read_git_tree(repo_path, resolved, target.alias or "")
    except (TreeError, ApexError) as exc:
        raise ApexError(str(exc)) from exc
    try:
        store.register_app(target, checkout_uuid, host, user)
        run_token = uuid.uuid4().hex
        store.acquire_app(target.physical_key, run_token, checkout_uuid, host, user)
    except ControlStoreError as exc:
        raise ApexError(str(exc)) from exc
    payload_started = False
    try:
        current = capture_app(target, held_by=run_token, repo=repo_path, root=state_root, control_store=store, runner=runner)
        try:
            baseline = load_baseline(target, root=state_root)
        except StateError:
            baseline = None
        if baseline is None and replace_from is None:
            raise ApexError("no verified baseline; capture existing app and use explicit --replace-from")
        allowed_by_baseline = baseline is not None and current.tree == baseline.tree
        allowed_by_receipt = False
        allowed = allowed_by_baseline
        replacement_capture = None
        if replace_from is not None:
            try:
                replacement_capture = load_capture(target, replace_from, root=state_root)
            except StateError as exc:
                raise ApexError(f"--replace-from recovery is unreadable: {exc}") from exc
        if not allowed and _receipt_allows_import(target, current.tree, selected_tree, resolved, state_root):
            # The receipt is the durable proof that the current capture was
            # reviewed/reconciled for this source; carry that decision into
            # the second, pre-destructive capture check below.
            allowed = True
            allowed_by_receipt = True
        elif not allowed:
            if replace_from is not None:
                if replacement_capture is None or replacement_capture.mine != current.tree:
                    raise ApexError("--replace-from does not bind the current captured application")
                allowed = True
            else:
                raise ApexError(
                    "current application differs from the verified baseline; run export-app, review changes, commit, and retry"
                )
        # Re-check after all preconditions, while the same physical mutex is held.
        first_guard_tree = current.tree
        current = capture_app(target, held_by=run_token, repo=repo_path, root=state_root, control_store=store, runner=runner)
        if current.tree != first_guard_tree:
            raise ApexError("application changed during import pre-check; rerun export-app")
        if allowed_by_baseline and current.tree != baseline.tree:
            raise ApexError("application no longer matches the verified baseline")
        if replacement_capture is not None and allowed and current.tree != replacement_capture.mine:
            raise ApexError("application no longer matches --replace-from evidence")
        if allowed_by_receipt and not _receipt_allows_import(target, current.tree, selected_tree, resolved, state_root):
            raise ApexError("capture receipt no longer binds the pre-destructive application state")
        master_contract_path = repo_path / "targets" / "masters.json"
        if master_contract_path.is_file():
            try:
                contract = json.loads(master_contract_path.read_text(encoding="utf-8"))
                validate_masters(
                    selected_tree,
                    target,
                    contract,
                    component_resolver=apex_component_resolver(
                        runner=runner,
                        work_root=state_root / "master-checks",
                    ),
                )
            except (OSError, UnicodeError, json.JSONDecodeError, MasterError) as exc:
                raise ApexError(f"master/component contract validation failed: {exc}") from exc
        print(
            f"PAUSE: importing {target.alias} application {target.app_id} into {target.instance_id}/workspace {target.workspace_id}; "
            "all Builder edits must stop until verification completes."
        )
        store.mark_payload_starting(target.physical_key, run_token)
        payload_started = True
        work = repo_path / "scratch" / f"apex-import-{uuid.uuid4().hex}"
        staged = _materialize_tree(work / "source", selected_tree)
        driver = work / "import.sql"
        driver.write_text(_import_driver(target, staged), encoding="utf-8", newline="\n")
        result = runner(target, "write", driver, work)
        _verify_result_identity(target, result)
        verified = capture_app(target, held_by=run_token, repo=repo_path, root=state_root, control_store=store, runner=runner)
        if verified.tree != selected_tree:
            raise ApexError("post-import export does not match the selected source commit")
        save_verified_baseline(target, resolved, selected_tree, root=state_root)
        result_path = state_root / "recovery" / current.recovery_id / "result.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "status": "success",
                    "verified": True,
                    "operation_id": current.recovery_id,
                    "source_commit": resolved,
                    "tree_digest": tree_digest(selected_tree),
                    "recovery_path": str(result_path.parent),
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        store.release_app(target.physical_key, run_token, confirmed_success=True)
        return load_baseline(target, root=state_root)
    except Exception as exc:
        if payload_started:
            if isinstance(exc, SqlclError):
                # The SQLcl worker's result is unknown; retain the owner token.
                raise ApexError(f"import result is unknown; recover app lock with retained evidence: {exc}") from exc
            try:
                store.release_app(target.physical_key, run_token, confirmed_success=False)
            except ControlStoreError:
                pass
        else:
            try:
                store.release_app(target.physical_key, run_token, confirmed_success=False)
            except ControlStoreError:
                pass
        if isinstance(exc, ApexError):
            raise
        raise ApexError(f"import failed: {exc}") from exc


def bootstrap_app(
    target: Target,
    *,
    repo: str | Path = ".",
    root: str | Path | None = None,
    control_store: ControlStore | None = None,
    runner: Callable[..., Any] = run_sqlcl,
) -> dict[str, Any]:
    """Capture an existing untracked app into source without database writes."""
    if target.role != "developer" or target.environment != "development":
        raise ApexError("bootstrap-app is restricted to the shared development target")
    _assert_app_target(target)
    repo_path = _default_repo(repo)
    source_root = repo_path / "apps" / (target.alias or "")
    if source_root.exists():
        try:
            if read_export_tree(source_root):
                raise ApexError("bootstrap-app requires an absent tracked application source")
        except TreeError as exc:
            raise ApexError(str(exc)) from exc
    try:
        assert_source_clean(repo_path, target.alias or "")
    except TreeError as exc:
        raise ApexError(str(exc)) from exc
    head = _git_head(repo_path)
    capture = capture_app(target, repo=repo_path, root=root, control_store=control_store, runner=runner)
    try:
        apply_tree(repo_path, target.alias or "", head, {}, capture.tree, capture.recovery_id)
    except PatchError as exc:
        raise ApexError(f"bootstrap candidate was not written; recovery {capture.recovery_id} retained") from exc
    return {"recovery_id": capture.recovery_id, "tree_digest": tree_digest(capture.tree), "candidate_path": str(source_root)}


def adopt_app(
    target: Target,
    *,
    repo: str | Path = ".",
    root: str | Path | None = None,
    control_store: ControlStore | None = None,
    runner: Callable[..., Any] = run_sqlcl,
) -> Baseline:
    """Stamp a baseline only after clean source equals a fresh export."""
    if target.role != "developer" or target.environment != "development":
        raise ApexError("adopt-app is restricted to the shared development target")
    _assert_app_target(target)
    repo_path = _default_repo(repo)
    try:
        assert_source_clean(repo_path, target.alias or "")
    except TreeError as exc:
        raise ApexError(str(exc)) from exc
    head = _git_head(repo_path)
    source_tree = read_git_tree(repo_path, head, target.alias or "")
    capture = capture_app(target, repo=repo_path, root=root, control_store=control_store, runner=runner, persist=False)
    if capture.tree != source_tree:
        raise ApexError("adopt-app requires exact re-export equality with committed source")
    state_root = _state_root(repo_path, root)
    save_verified_baseline(target, head, source_tree, root=state_root)
    receipt_id = save_receipt(target, capture.tree, head, set(), tree_digest(capture.tree), root=state_root, kind="adoption")
    save_checkpoint(target, capture.tree, source_tree, head, receipt_id, anchor_commit=head, root=state_root)
    return load_baseline(target, root=state_root)


def resolve_export(
    target: Target,
    recovery_id: str,
    resolved: str | Path,
    *,
    repo: str | Path = ".",
    root: str | Path | None = None,
) -> Decision:
    """Apply a complete reviewed tree from an immutable export recovery."""
    if target.role != "developer" or target.environment != "development":
        raise ApexError("resolve-export is restricted to the shared development target")
    _assert_app_target(target)
    repo_path = _default_repo(repo)
    state_root = _state_root(repo_path, root)
    try:
        evidence = load_capture(target, recovery_id, root=state_root)
        current_head = _git_head(repo_path)
    except (StateError, ApexError) as exc:
        raise ApexError(f"cannot load export recovery {recovery_id}: {exc}") from exc
    if evidence.head != current_head:
        raise ApexError("HEAD moved since capture; rerun export/reconciliation before resolving")
    try:
        resolved_tree = read_export_tree(resolved)
        source_tree = read_git_tree(repo_path, current_head, target.alias or "")
    except TreeError as exc:
        raise ApexError(str(exc)) from exc
    if "application.apx" not in resolved_tree or ".apex/apexlang.json" not in resolved_tree:
        raise ApexError("resolved source must contain application.apx and .apex/apexlang.json")
    for path, value in resolved_tree.items():
        if Path(path).suffix.casefold() in {".apx", ".json", ".sql", ".js", ".css", ".html", ".txt", ".xml", ".yaml", ".yml"}:
            if any(marker in value for marker in (b"<<<<<<<", b"=======", b">>>>>>>")):
                raise ApexError(f"resolved source still contains conflict markers: {path}")
    try:
        expected_decision = reconcile(
            evidence.base,
            source_tree,
            evidence.mine,
            source_base=evidence.source_base,
        )
    except Exception as exc:
        raise ApexError(f"could not re-evaluate export recovery: {exc}") from exc
    declared_conflicts = evidence.diagnostics.get("conflicts", [])
    if not isinstance(declared_conflicts, list) or tuple(sorted(declared_conflicts)) != expected_decision.conflicts:
        raise ApexError("export recovery conflict list is inconsistent with its retained trees")
    conflict_paths = set(expected_decision.conflicts)
    for path in set(expected_decision.tree) | set(resolved_tree):
        if path in conflict_paths:
            continue
        if expected_decision.tree.get(path) != resolved_tree.get(path):
            raise ApexError(f"resolution changed a non-conflicting path: {path}")
    try:
        apply_tree(repo_path, target.alias or "", current_head, source_tree, resolved_tree, recovery_id)
    except PatchError as exc:
        raise ApexError(f"resolution was not applied; recovery {recovery_id} retained") from exc
    try:
        previous_checkpoint = load_checkpoint(target, current_head, root=state_root)
        previous_absent = previous_checkpoint.required_absent
    except StateError:
        previous_absent = set()
    absent = required_absences(previous_absent, evidence.base, evidence.source_base, source_tree, evidence.mine, resolved_tree)
    receipt_id = save_receipt(target, resolved_tree, current_head, absent, tree_digest(evidence.mine), kind="resolution", resolved_digest=tree_digest(resolved_tree), root=state_root)
    save_checkpoint(target, evidence.mine, resolved_tree, current_head, receipt_id, anchor_commit=current_head, required_absent=absent, root=state_root)
    return Decision(resolved_tree, ())
