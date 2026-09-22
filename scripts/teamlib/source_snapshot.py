"""Exact Git-source snapshot for protected integration qualification."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
from typing import Iterator
from collections.abc import Mapping, Sequence

from .app_checks import AppCheckBundle, AppCheckError, build_app_check_bundle
from .fingerprints import Inventory, InventoryError, load_inventory_bytes
from .migration_bundle import BundleError, Migration, load_bundles


class SourceSnapshotError(RuntimeError):
    """Raised when an exact committed integration source cannot be trusted."""


_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_ALLOWED_MODES = {0o100644, 0o100755}


def _git(repo: Path, args: list[str]) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, check=False
        )
    except OSError as exc:
        raise SourceSnapshotError("Git is unavailable") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise SourceSnapshotError(detail or "Git operation failed")
    return result.stdout


def _safe_path(path: str) -> str:
    if not path or "\\" in path:
        raise SourceSnapshotError(f"unsafe Git source path: {path!r}")
    posix = PurePosixPath(path)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
        raise SourceSnapshotError(f"unsafe Git source path: {path!r}")
    return posix.as_posix()


def _owned_specs(aliases: Sequence[str]) -> tuple[str, ...]:
    selected = tuple(aliases)
    if not selected or len(set(selected)) != len(selected):
        raise SourceSnapshotError("integration source requires unique application aliases")
    if any(not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias) for alias in selected):
        raise SourceSnapshotError("integration source contains an unsafe application alias")
    return (
        "migrations",
        "database/schema-inventory.json",
        *(f"apps/{alias}" for alias in sorted(selected)),
        "ci/app-checks",
    )


def _read_owned_git_blobs(
    repo: str | Path, commit: str, aliases: Sequence[str]
) -> dict[str, bytes]:
    repo_path = Path(repo)
    if repo_path.is_symlink() or not repo_path.is_dir():
        raise SourceSnapshotError(
            f"integration repository is not a real directory: {repo_path}"
        )
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise SourceSnapshotError("integration source must be an exact commit")
    try:
        resolved = _git(
            repo_path, ["rev-parse", "--verify", f"{commit}^{{commit}}"]
        ).decode("ascii").strip()
    except UnicodeError as exc:
        raise SourceSnapshotError("integration source commit is unreadable") from exc
    if resolved != commit:
        raise SourceSnapshotError("integration source must be an exact commit")
    specs = _owned_specs(aliases)
    listing = _git(
        repo_path,
        ["ls-tree", "-r", "-z", "--full-tree", commit, "--", *specs],
    )
    owned: dict[str, bytes] = {}
    folded: dict[str, str] = {}
    for record in listing.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode_raw, object_type, object_id = header.split()
            path = _safe_path(raw_path.decode("utf-8"))
            mode = int(mode_raw, 8)
            oid = object_id.decode("ascii")
        except (ValueError, UnicodeError) as exc:
            raise SourceSnapshotError("malformed Git tree record") from exc
        if object_type != b"blob" or mode not in _ALLOWED_MODES:
            raise SourceSnapshotError(f"unsupported Git mode or entry: {path}")
        if not any(path == spec or path.startswith(spec + "/") for spec in specs):
            raise SourceSnapshotError(f"Git returned a path outside owned inputs: {path}")
        key = path.casefold()
        if key in folded and folded[key] != path:
            raise SourceSnapshotError(
                f"case-colliding Git source paths: {folded[key]}, {path}"
            )
        folded[key] = path
        owned[path] = _git(repo_path, ["cat-file", "blob", oid])
    return dict(sorted(owned.items()))


def _migration_members(owned: Mapping[str, bytes]) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path, data in owned.items():
        if not path.startswith("migrations/"):
            continue
        relative = path.removeprefix("migrations/")
        if relative == ".gitkeep":
            continue
        if "/" in relative or not relative.endswith(".sql"):
            raise SourceSnapshotError(f"unsupported migration source path: {path}")
        result[relative] = data
    return dict(sorted(result.items()))


def load_bundles_from_bytes(members: Mapping[str, bytes]) -> dict[str, Migration]:
    with tempfile.TemporaryDirectory(prefix="team-integration-bundles-") as directory:
        root = Path(directory)
        for name, data in sorted(members.items()):
            if PurePosixPath(name).name != name:
                raise SourceSnapshotError(f"unsafe migration member: {name}")
            (root / name).write_bytes(data)
        try:
            return load_bundles(root)
        except BundleError as exc:
            raise SourceSnapshotError(str(exc)) from exc


def _required_blob(owned: Mapping[str, bytes], path: str, label: str) -> bytes:
    try:
        return owned[path]
    except KeyError as exc:
        raise SourceSnapshotError(f"integration source is missing {label}: {path}") from exc


def _application_trees(
    owned: Mapping[str, bytes], aliases: Sequence[str]
) -> dict[str, dict[str, bytes]]:
    result: dict[str, dict[str, bytes]] = {}
    for alias in sorted(aliases):
        prefix = f"apps/{alias}/"
        tree = {
            path.removeprefix(prefix): data
            for path, data in owned.items()
            if path.startswith(prefix)
            and not path.removeprefix(prefix).startswith(
                ("deployments/", "logs/", ".logs/")
            )
        }
        if not tree:
            raise SourceSnapshotError(
                f"integration source is missing application {alias}"
            )
        result[alias] = dict(sorted(tree.items()))
    return result


def _check_members(owned: Mapping[str, bytes]) -> dict[str, bytes]:
    prefix = "ci/app-checks/"
    return {
        path.removeprefix(prefix): data
        for path, data in owned.items()
        if path.startswith(prefix)
    }


@dataclass(frozen=True)
class IntegrationSource:
    commit: str
    migrations: Mapping[str, bytes]
    canonical_inventory: Inventory
    app_trees: Mapping[str, Mapping[str, bytes]]
    check_bundle: AppCheckBundle

    @contextmanager
    def materialize_migrations(self, root: str | Path) -> Iterator[Path]:
        root_path = Path(root)
        if root_path.is_symlink():
            raise SourceSnapshotError(
                f"migration scratch root must not be a symlink: {root_path}"
            )
        root_path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".integration-migrations-", dir=str(root_path)
        ) as directory:
            destination = Path(directory)
            for name, data in sorted(self.migrations.items()):
                if PurePosixPath(name).name != name:
                    raise SourceSnapshotError(f"unsafe migration member: {name}")
                (destination / name).write_bytes(data)
            try:
                load_bundles(destination)
            except BundleError as exc:
                raise SourceSnapshotError(str(exc)) from exc
            yield destination


def load_integration_source(
    repo: str | Path, commit: str, aliases: Sequence[str]
) -> IntegrationSource:
    owned = _read_owned_git_blobs(repo, commit, aliases)
    migrations = _migration_members(owned)
    load_bundles_from_bytes(migrations)
    inventory_bytes = _required_blob(
        owned,
        "database/schema-inventory.json",
        "canonical inventory",
    )
    try:
        inventory = load_inventory_bytes(
            inventory_bytes,
            source=f"<git:{commit}:database/schema-inventory.json>",
        )
    except InventoryError as exc:
        raise SourceSnapshotError(str(exc)) from exc
    app_trees = _application_trees(owned, aliases)
    try:
        check_bundle = build_app_check_bundle(_check_members(owned), aliases)
    except AppCheckError as exc:
        raise SourceSnapshotError(str(exc)) from exc
    return IntegrationSource(
        commit=commit,
        migrations=migrations,
        canonical_inventory=inventory,
        app_trees=app_trees,
        check_bundle=check_bundle,
    )
