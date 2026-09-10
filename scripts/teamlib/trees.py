"""Exact, byte-preserving source trees used by the round-trip workflow."""

from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import TypeAlias
from collections.abc import Mapping


Tree: TypeAlias = Mapping[str, bytes]


class TreeError(ValueError):
    """Raised when a source/export tree cannot be trusted."""


_ALIAS_RE = re.compile(r"[a-z][a-z0-9-]*\Z")
_ALLOWED_MODES = {0o100644, 0o100755}
_EXCLUDED_PREFIXES = {"deployments", "logs", ".logs"}
_EXCLUDED_LOG_NAMES = {"export.log", "apex_export.log", "apex-export.log"}
_TEXT_NORMALIZED_SUFFIXES = {".apx"}
_TEXT_NORMALIZED_NAMES = {"apexlang.json"}
# APEX static application files are arbitrary uploads, and static-files/ is part
# of the owned tree, so this list has to cover what a real application carries.
# It stays deny-by-default: executables, libraries and archives are excluded
# deliberately, because an application export has no reason to contain them.
_ALLOWED_SUFFIXES = {
    ".apex", ".apx", ".bin", ".css", ".csv", ".dat", ".eot", ".gif",
    ".html", ".ico", ".jpeg", ".jpg", ".js", ".json", ".map", ".md",
    ".mjs", ".mp3", ".mp4", ".otf", ".pdf", ".png", ".properties",
    ".sql", ".svg", ".ts", ".txt", ".ttf", ".wasm", ".webm", ".webp",
    ".woff", ".woff2", ".xml", ".yaml", ".yml", ".zip",
}
_ALLOWED_NAMES = {"application", "README", "LICENSE"}
_RESERVED_COMPONENTS = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _normalize_bytes(path: str, data: bytes) -> bytes:
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix in _TEXT_NORMALIZED_SUFFIXES or PurePosixPath(path).name.casefold() in _TEXT_NORMALIZED_NAMES:
        return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return data


def _validate_alias(alias: str) -> None:
    if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
        raise TreeError(f"unsafe application alias: {alias!r}")


def _validate_relative_path(path: str) -> str:
    if not isinstance(path, str) or not path or "\\" in path:
        raise TreeError(f"unsafe source path: {path!r}")
    posix = PurePosixPath(path)
    if posix.is_absolute() or any(part in {"", ".", ".."} for part in posix.parts):
        raise TreeError(f"unsafe source path: {path!r}")
    if any(part.casefold() in _RESERVED_COMPONENTS for part in posix.parts):
        raise TreeError(f"reserved source path component: {path!r}")
    return posix.as_posix()


def _validate_tree_paths(tree: Mapping[str, bytes]) -> None:
    seen: dict[str, str] = {}
    for raw_path, data in tree.items():
        path = _validate_relative_path(raw_path)
        if not isinstance(data, bytes):
            raise TreeError(f"tree value is not bytes: {path}")
        folded = path.casefold()
        if folded in seen and seen[folded] != path:
            raise TreeError(f"case-colliding source paths: {seen[folded]} and {path}")
        seen[folded] = path


def _is_excluded(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts:
        return True
    if parts[0] in _EXCLUDED_PREFIXES:
        return True
    if PurePosixPath(path).name.casefold() in _EXCLUDED_LOG_NAMES:
        return True
    return False


def _is_known_export_file(path: str) -> bool:
    name = PurePosixPath(path).name
    suffix = PurePosixPath(path).suffix.casefold()
    if suffix in _ALLOWED_SUFFIXES:
        return True
    if name in _ALLOWED_NAMES:
        return True
    return False


def _read_regular_tree(root: Path) -> dict[str, bytes]:
    if not root.exists() or not root.is_dir() or root.is_symlink():
        raise TreeError(f"tree root is not a real directory: {root}")
    tree: dict[str, bytes] = {}
    folded: dict[str, str] = {}
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise TreeError(f"could not read tree directory: {directory}") from exc
        for entry in entries:
            rel_path = Path(entry.path).relative_to(root).as_posix()
            rel_path = _validate_relative_path(rel_path)
            if entry.is_symlink():
                raise TreeError(f"symlink is not allowed in source tree: {rel_path}")
            if entry.is_dir(follow_symlinks=False):
                stack.append(Path(entry.path))
                continue
            if not entry.is_file(follow_symlinks=False):
                raise TreeError(f"unsupported non-regular source entry: {rel_path}")
            if _is_excluded(rel_path):
                continue
            if not _is_known_export_file(rel_path):
                raise TreeError(
                    f"unsupported exported source class: {rel_path} "
                    "(extend _ALLOWED_SUFFIXES in teamlib/trees.py if APEX legitimately exports it)"
                )
            folded_path = rel_path.casefold()
            if folded_path in folded and folded[folded_path] != rel_path:
                raise TreeError(f"case-colliding source paths: {folded[folded_path]} and {rel_path}")
            folded[folded_path] = rel_path
            try:
                data = Path(entry.path).read_bytes()
            except OSError as exc:
                raise TreeError(f"could not read exported file: {rel_path}") from exc
            tree[rel_path] = _normalize_bytes(rel_path, data)
    _validate_tree_paths(tree)
    return dict(sorted(tree.items()))


def read_export_tree(path: str | Path) -> dict[str, bytes]:
    """Read an exported application while preserving every owned byte."""
    return _read_regular_tree(Path(path))


def _git(repo: Path, args: list[str], *, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=stdin,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise TreeError("git is unavailable") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise TreeError(f"git operation failed: {detail or result.returncode}")
    return result


def _validate_git_ref(repo: Path, commit: str) -> str:
    if not isinstance(commit, str) or not commit or "\x00" in commit:
        raise TreeError("invalid Git commit")
    return _git(repo, ["rev-parse", "--verify", f"{commit}^{{commit}}"]).stdout.decode("ascii").strip()


def read_git_tree(repo: str | Path, commit: str, alias: str) -> dict[str, bytes]:
    """Read exact blobs below ``apps/<alias>`` from a verified Git commit."""
    repo_path = Path(repo)
    if not repo_path.is_dir() or repo_path.is_symlink():
        raise TreeError(f"Git repository is not a real directory: {repo_path}")
    _validate_alias(alias)
    resolved = _validate_git_ref(repo_path, commit)
    prefix = f"apps/{alias}/"
    listing = _git(repo_path, ["ls-tree", "-r", "-z", "--full-tree", resolved, "--", f"apps/{alias}"]).stdout
    tree: dict[str, bytes] = {}
    folded: dict[str, str] = {}
    for record in listing.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode_raw, object_type, object_id = header.split()
            full_path = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise TreeError("malformed Git tree record") from exc
        try:
            mode = int(mode_raw, 8)
        except ValueError as exc:
            raise TreeError(f"malformed Git mode for {raw_path!r}") from exc
        if object_type != b"blob" or mode not in _ALLOWED_MODES:
            raise TreeError(f"unsupported Git source entry: {full_path}")
        if not full_path.startswith(prefix):
            raise TreeError(f"Git returned a path outside the selected alias: {full_path}")
        relative = _validate_relative_path(full_path[len(prefix):])
        if _is_excluded(relative):
            continue
        if not _is_known_export_file(relative):
            raise TreeError(
                f"unsupported Git source class: {relative} "
                "(extend _ALLOWED_SUFFIXES in teamlib/trees.py if APEX legitimately exports it)"
            )
        folded_path = relative.casefold()
        if folded_path in folded and folded[folded_path] != relative:
            raise TreeError(f"case-colliding source paths: {folded[folded_path]} and {relative}")
        folded[folded_path] = relative
        blob = _git(repo_path, ["cat-file", "blob", object_id.decode("ascii")]).stdout
        tree[relative] = _normalize_bytes(relative, blob)
    _validate_tree_paths(tree)
    return dict(sorted(tree.items()))


def assert_source_clean(repo: str | Path, alias: str) -> None:
    """Reject changes to owned source, including ignored/untracked collisions."""
    repo_path = Path(repo)
    _validate_alias(alias)
    prefix = f"apps/{alias}/"
    # -z is essential: paths may contain spaces and porcelain output must not
    # be interpreted as a shell or line-oriented command language.
    status = _git(
        repo_path,
        ["status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching", "-z", "--", f"apps/{alias}"],
    ).stdout
    for record in status.split(b"\0"):
        if not record:
            continue
        if len(record) < 3:
            raise TreeError("malformed Git status record")
        code = record[:2].decode("ascii", "replace")
        raw_path = record[3:]
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TreeError("Git status contains a non-UTF-8 path") from exc
        # --porcelain=v1 -z emits a rename as two NUL-separated records (new
        # path, then original) rather than an "orig -> new" pair, so there is no
        # arrow to split. The bare original-path record cannot start with the
        # alias prefix after the two-character status slice, and any record that
        # does is refused below by the non-clean status check.
        if not path.startswith(prefix):
            continue
        relative = _validate_relative_path(path[len(prefix):])
        # The same paths the tree readers skip are not owned source, so a
        # status entry for one of them is not an uncleanliness. Anything the
        # predicate does not name -- including an ignored file shadowing a real
        # owned path -- is still refused below.
        if _is_excluded(relative) or relative == "deployments":
            continue
        if code != "  ":
            raise TreeError(f"owned source is not clean: {path}")

    # A status query does not validate file modes, symlinks or case collisions
    # for a clean tree. Walk the directory for those invariants explicitly.
    source_root = repo_path / "apps" / alias
    if source_root.exists():
        _read_regular_tree(source_root)


def _manifest(tree: Tree) -> dict[str, object]:
    _validate_tree_paths(tree)
    files = [
        {
            "path": path,
            "length": len(tree[path]),
            "sha256": sha256(tree[path]).hexdigest(),
        }
        for path in sorted(tree)
    ]
    return {"version": 1, "files": files}


def tree_digest(tree: Tree) -> str:
    """Hash a canonical length- and path-framed manifest."""
    canonical = json.dumps(
        _manifest(tree), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def tree_manifest(tree: Tree) -> dict[str, object]:
    """Return the canonical manifest used by state and receipt storage."""
    return _manifest(tree)


def tree_contains(subset: Tree, superset: Tree) -> bool:
    _validate_tree_paths(subset)
    _validate_tree_paths(superset)
    return all(path in superset and superset[path] == value for path, value in subset.items())


def receipt_satisfied(result: Tree, required_absent: set[str], selected: Tree) -> bool:
    _validate_tree_paths(result)
    _validate_tree_paths(selected)
    absent = {_validate_relative_path(path) for path in required_absent}
    if set(result) & absent:
        raise ValueError("receipt contains contradictory presence/absence")
    return tree_contains(result, selected) and not absent.intersection(selected)
