#!/usr/bin/env python3
"""Upgrade a project created from the APEX team template to a newer template.

Only paths named by the template's template-manifest.json are touched. The
fixed .template-lock.json file is engine metadata used to remember the last
installed template hashes; it is not copied from the template. Customized files
are kept, and a real conflict leaves the new version beside the local file as
<path>.template-new. Project-owned placeholder files are created when missing
and never overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


MANIFEST_NAME = "template-manifest.json"
LOCK_NAME = ".template-lock.json"
CONFLICT_SUFFIX = ".template-new"
ATTENTION = {"CONFLICT", "KEEP-DELETED", "KEEP-REMOVED"}
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
PROTECTED_SAMPLES = (
    "apps/DEMO/100/application.apx",
    "database/DEMO/tables/T.sql",
    "migrations/2026-09-27_create-customers-r001/001-create-table.sql",
    "app_context/100/purpose.md",
    ".env",
    LOCK_NAME,
)


class UpgradeError(Exception):
    """A refusal or failure that leaves the project unchanged."""


@dataclass(frozen=True)
class Action:
    kind: str
    path: str
    # The project file's hash when the plan was made: None when it was absent,
    # "" when not recorded. apply_actions refuses a target that changed since.
    local: str | None = ""


@dataclass
class Mutation:
    target: Path
    backup: Path
    had_original: bool
    original_moved: bool = False
    replacement_installed: bool = False
    # Hash of the bytes this upgrade installed, so rollback can tell them from
    # a later save.
    installed_hash: str | None = None


def validate_relative_path(value: object, *, pattern: bool = False) -> str:
    """Validate a manifest path or lock path as a normalized POSIX relative path."""
    label = "unsafe path pattern" if pattern else "unsafe path"
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise UpgradeError(f"{label}: {value!r}")
    if value.startswith("/") or value.startswith("~") or ":" in value:
        raise UpgradeError(f"{label}: {value!r}")
    pieces = value.split("/")
    if any(piece in ("", ".", "..") for piece in pieces):
        raise UpgradeError(f"{label}: {value!r}")
    if PurePosixPath(value).as_posix() != value:
        raise UpgradeError(f"{label}: {value!r}")
    if not pattern and any(char in value for char in "*?"):
        raise UpgradeError(f"{label}: wildcard in lock path {value!r}")
    return value


def protected_project_path(path: str) -> bool:
    """Paths containing downstream data are never eligible for template writes."""
    if path == LOCK_NAME:
        return True
    if path == ".env" or (path.startswith(".env.") and path != ".env.example"):
        return True
    if path == "ai_generate/.gitkeep":
        return False
    if path.startswith("ai_generate/"):
        return True
    if path == "apps/.gitkeep" or path.startswith("apps/templates/"):
        return False
    if path.startswith("apps/"):
        return True
    if path == "database/.gitkeep":
        return False
    if path.startswith("database/"):
        return True
    if path in {"migrations/.gitkeep", "migrations/README.md"}:
        return False
    if path.startswith("migrations/"):
        return True
    if path == "app_context/README.md":
        return False
    return path.startswith("app_context/")


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    parts = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("^" + "".join(parts) + "$")


def run_git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    if result.returncode != 0:
        raise UpgradeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_link_or_reparse(path_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(path_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(path_stat.st_mode) or bool(attributes & reparse_flag)


def safe_project_path(project_root: Path, relative: str) -> Path:
    """Reject symlinks, reparse points, and non-directory parent components."""
    validate_relative_path(relative)
    current = project_root
    pieces = PurePosixPath(relative).parts
    for index, piece in enumerate(pieces):
        current = current / piece
        info = _lstat(current)
        if info is None:
            continue
        if _is_link_or_reparse(info):
            raise UpgradeError(f"symbolic link or reparse point is not supported: {relative}")
        is_last = index == len(pieces) - 1
        if not is_last and not stat.S_ISDIR(info.st_mode):
            raise UpgradeError(f"managed path has a non-directory parent: {relative}")
        if is_last and not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise UpgradeError(f"managed path is not a regular file: {relative}")
    return project_root.joinpath(*pieces)


def sha256(path: Path) -> str | None:
    info = _lstat(path)
    if info is None:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise UpgradeError(f"managed path is not a regular file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalized_sha256(data: bytes) -> str:
    """Hash text with CRLF folded to LF; binary data (any NUL byte) is hashed as is.

    Git for Windows checks text files out as CRLF by default (core.autocrlf), so
    the same template file has different bytes on different machines. Comparing
    the normalized hash keeps a project made on Linux, or from a ZIP, from showing
    every text file as a conflict when it is upgraded from a Windows clone.
    """
    if b"\0" not in data:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def sha256_pair(path: Path) -> tuple[str, str] | None:
    """Return (raw hash, line-ending-normalized hash) of a file, None when absent."""
    info = _lstat(path)
    if info is None:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise UpgradeError(f"managed path is not a regular file: {path}")
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), normalized_sha256(data)


def fetch_template(source: str, ref: str | None, destination: Path) -> str:
    run_git(destination.parent, "clone", "--quiet", source, str(destination))
    if ref:
        # A clone only creates its default local branch. Other maintenance
        # branches exist as remote refs and must be resolved explicitly.
        try:
            resolved = run_git(destination, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip()
        except UpgradeError:
            resolved = run_git(destination, "rev-parse", "--verify", "--end-of-options",
                               "refs/remotes/origin/" + ref + "^{commit}").strip()
        run_git(destination, "checkout", "--quiet", "--detach", resolved)
    return run_git(destination, "rev-parse", "HEAD").strip()


def _manifest_string_list(manifest: dict, key: str, *, pattern: bool) -> list[str]:
    entries = manifest.get(key)
    if not isinstance(entries, list):
        raise UpgradeError(f"template manifest key {key} must be a list")
    normalized = [validate_relative_path(value, pattern=pattern) for value in entries]
    if len(set(normalized)) != len(normalized):
        raise UpgradeError(f"template manifest key {key} contains duplicate paths")
    return normalized


def load_manifest(template_root: Path) -> dict:
    path = template_root / MANIFEST_NAME
    info = _lstat(path)
    if info is not None and _is_link_or_reparse(info):
        raise UpgradeError("template manifest cannot be a symbolic link")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpgradeError(f"template manifest is missing or invalid: {exc}") from exc
    if not isinstance(manifest, dict) or type(manifest.get("schemaVersion")) is not int:
        raise UpgradeError("template manifest must be an object with integer schemaVersion")
    if manifest["schemaVersion"] not in (1, 2):
        raise UpgradeError("unsupported template manifest schemaVersion; upgrade this script first")
    if manifest["schemaVersion"] == 2:
        if manifest.get("apexRelease") != "26.2":
            raise UpgradeError("schema-2 template manifest requires apexRelease 26.2")
        if manifest.get("minimumSqlclVersion") != "26.3.0.0":
            raise UpgradeError("schema-2 template manifest requires minimumSqlclVersion 26.3.0.0")
    if not isinstance(manifest.get("upstream"), str) or not manifest["upstream"]:
        raise UpgradeError("template manifest key upstream must be a non-empty string")

    owned_paths = _manifest_string_list(manifest, "templateOwned", pattern=True)
    project_paths = _manifest_string_list(manifest, "projectOwned", pattern=False)
    template_only = _manifest_string_list(manifest, "templateOnly", pattern=True)
    owned_patterns = [glob_to_regex(item) for item in owned_paths]
    only_patterns = [glob_to_regex(item) for item in template_only]

    for path in project_paths:
        if any(pattern.match(path) for pattern in owned_patterns):
            raise UpgradeError(f"manifest assigns multiple owners to {path}")
        if protected_project_path(path):
            raise UpgradeError(f"unsafe project-owned path: {path}")
    if any(pattern.match(LOCK_NAME) for pattern in owned_patterns):
        raise UpgradeError(f"unsafe path pattern: {LOCK_NAME} is reserved engine metadata")
    for pattern_text, pattern in zip(owned_paths, owned_patterns, strict=True):
        if any(pattern.match(path) for path in PROTECTED_SAMPLES):
            raise UpgradeError(f"unsafe path pattern can match project data: {pattern_text}")
    manifest["_templateOwnedPatterns"] = owned_patterns
    manifest["_templateOnlyPatterns"] = only_patterns
    manifest["_projectOwnedPaths"] = project_paths
    return manifest


def tracked_template_files(template_root: Path) -> dict[str, str]:
    """Map tracked paths to Git modes so managed symlinks and submodules fail closed."""
    listing = run_git(template_root, "ls-files", "-s", "-z")
    files: dict[str, str] = {}
    for entry in listing.split("\0"):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        files[path] = meta.split()[0]
    return files


def classify(template_root: Path, manifest: dict) -> tuple[list[str], list[str]]:
    owned_patterns = manifest["_templateOwnedPatterns"]
    only_patterns = manifest["_templateOnlyPatterns"]
    project_owned = set(manifest["_projectOwnedPaths"])
    template_owned: list[str] = []
    placeholders: list[str] = []
    tracked = tracked_template_files(template_root)

    for path, mode in sorted(tracked.items()):
        validate_relative_path(path)
        is_project_owned = path in project_owned
        is_template_owned = any(pattern.match(path) for pattern in owned_patterns)
        is_template_only = any(pattern.match(path) for pattern in only_patterns)
        if sum((is_project_owned, is_template_owned, is_template_only)) > 1:
            raise UpgradeError(f"manifest assigns multiple owners to {path}")
        if not (is_project_owned or is_template_owned or is_template_only):
            continue
        if is_template_only:
            continue
        if protected_project_path(path):
            raise UpgradeError(f"manifest attempts to manage project data: {path}")
        if mode == "120000":
            raise UpgradeError(f"template contains a symbolic link, which is not supported: {path}")
        if mode not in {"100644", "100755"}:
            raise UpgradeError(f"template contains an unsupported file type: {path}")
        if is_project_owned:
            placeholders.append(path)
        elif is_template_owned:
            template_owned.append(path)

    missing_placeholders = project_owned - set(tracked)
    if missing_placeholders:
        raise UpgradeError(
            "project-owned placeholders are missing from the template: "
            + ", ".join(sorted(missing_placeholders))
        )
    return template_owned, placeholders


def read_lock(project_root: Path) -> dict:
    return read_lock_with_hash(project_root)[0]


def read_lock_with_hash(project_root: Path) -> tuple[dict, str | None]:
    """Parse the lock and return the hash of the same bytes (None when absent)."""
    path = safe_project_path(project_root, LOCK_NAME)
    info = _lstat(path)
    if info is None:
        return {"files": {}}, None
    if not stat.S_ISREG(info.st_mode):
        raise UpgradeError(f"{LOCK_NAME} is not a regular file")
    try:
        raw = path.read_bytes()
        lock = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise UpgradeError(f"{LOCK_NAME} is invalid: {exc}") from exc
    return _validated_lock(lock), hashlib.sha256(raw).hexdigest()


def _validated_lock(lock: object) -> dict:
    if not isinstance(lock, dict) or type(lock.get("schemaVersion")) is not int or lock["schemaVersion"] != 1:
        raise UpgradeError(f"{LOCK_NAME} has an unsupported or missing schemaVersion")
    if not isinstance(lock.get("files"), dict):
        raise UpgradeError(f"{LOCK_NAME} has no files map")
    for path_text, digest in lock["files"].items():
        validate_relative_path(path_text)
        if not isinstance(digest, str) or not HASH_RE.fullmatch(digest):
            raise UpgradeError(f"{LOCK_NAME} has an invalid hash for {path_text}")
    for key in ("upstream", "commit", "templateRef"):
        if key in lock and not isinstance(lock[key], str):
            raise UpgradeError(f"{LOCK_NAME} has an invalid {key}")
    if "templateRef" in lock and (not lock["templateRef"] or lock["templateRef"].startswith("-")
                                  or any(char in lock["templateRef"] for char in "\0\r\n")):
        raise UpgradeError(f"{LOCK_NAME} has an invalid templateRef")
    if "apexRelease" in lock and lock["apexRelease"] not in ("26.1", "26.2"):
        raise UpgradeError(f"{LOCK_NAME} has an invalid apexRelease")
    return lock


def validate_lock_ownership(lock: dict, manifest: dict) -> None:
    patterns = manifest["_templateOwnedPatterns"]
    for path in lock["files"]:
        if protected_project_path(path):
            raise UpgradeError(f"{LOCK_NAME} path is protected project data: {path}")
        if not any(pattern.match(path) for pattern in patterns):
            raise UpgradeError(f"{LOCK_NAME} path is not template-owned by the current manifest: {path}")


def has_uncommitted_changes(project_root: Path) -> bool:
    """Whether the working tree differs from HEAD in content, file mode or file set.

    `git status` also lists a file whose line endings differ in a way Git's own conversions
    undo (core.autocrlf=true with eol=lf, after an editor saved it with CRLF), although
    nothing in it changed. `git diff` compares the converted content, so a file counts here
    only when its content really differs.
    """
    untracked = run_git(project_root, "ls-files", "--others", "--exclude-standard")
    unstaged = run_git(project_root, "diff", "--name-only")
    staged = run_git(project_root, "diff", "--cached", "--name-only")
    return bool((untracked + unstaged + staged).strip())


def check_project(project_root: Path) -> None:
    if has_uncommitted_changes(project_root):
        raise UpgradeError("the project has uncommitted changes; commit or stash them before upgrading")
    pending = [
        path
        for path in run_git(project_root, "ls-files", "-z").split("\0")
        if path.endswith(CONFLICT_SUFFIX)
    ]
    # This generated review copy is ignored by the .env.* rule, so Git's
    # tracked-file listing cannot see it after the lock alone is committed.
    environment_candidate = ".env.example" + CONFLICT_SUFFIX
    if _lstat(project_root / environment_candidate) is not None and environment_candidate not in pending:
        pending.append(environment_candidate)
    if pending:
        raise UpgradeError(f"resolve and delete {CONFLICT_SUFFIX} files first: {', '.join(pending)}")


def _new_file_hash(template_root: Path, relative: str) -> tuple[str, str]:
    digests = sha256_pair(template_root / relative)
    if digests is None:
        raise UpgradeError(f"managed template file is missing: {relative}")
    return digests


def plan_upgrade(
    project_root: Path,
    template_root: Path,
    template_owned: list[str],
    placeholders: list[str],
    lock: dict,
) -> tuple[list[Action], dict[str, str]]:
    actions: list[Action] = []
    new_lock: dict[str, str] = {}
    installed = lock["files"]
    current_files = set(template_owned)

    for path in template_owned:
        new_raw, new_normalized = _new_file_hash(template_root, path)
        target = safe_project_path(project_root, path)
        local_pair = sha256_pair(target)
        # `local` stays the raw hash: it is what the race guard in apply_actions
        # compares with the bytes it later moves aside. Decisions below ignore a
        # CRLF/LF difference; an older lock holds raw hashes, which are accepted too.
        local = local_pair[0] if local_pair is not None else None
        last = installed.get(path)
        new_lock[path] = new_normalized
        if local_pair is None:
            kind = "CREATE" if last is None else "KEEP-DELETED"
        elif local_pair[1] == new_normalized:
            kind = "UNCHANGED"
        elif last in local_pair:
            kind = "UPDATE"
        elif last in (new_raw, new_normalized):
            kind = "KEEP-LOCAL"
        else:
            kind = "CONFLICT"
        if kind == "CONFLICT":
            conflict_path = safe_project_path(project_root, path + CONFLICT_SUFFIX)
            if _lstat(conflict_path) is not None:
                raise UpgradeError(f"pending {CONFLICT_SUFFIX} file already exists: {path}{CONFLICT_SUFFIX}")
        actions.append(Action(kind, path, local))

    for path, last in sorted(installed.items()):
        if path in current_files:
            continue
        target = safe_project_path(project_root, path)
        local_pair = sha256_pair(target)
        if local_pair is None:
            continue
        actions.append(Action("DELETE" if last in local_pair else "KEEP-REMOVED", path, local_pair[0]))

    for path in placeholders:
        target = safe_project_path(project_root, path)
        local = sha256(target)
        actions.append(Action("KEEP-PLACEHOLDER" if local is not None else "PLACEHOLDER", path, local))
    return actions, new_lock


def _lock_bytes(upstream: str, commit: str, files: dict[str, str], metadata: dict | None = None) -> bytes:
    payload = {
        "schemaVersion": 1,
        "upstream": upstream,
        "commit": commit,
        "files": dict(sorted(files.items())),
    }
    if metadata:
        payload.update(metadata)
    _validated_lock(payload)
    return (json.dumps(payload, indent=2) + "\n").encode("utf-8")


def _read_template_bytes(template_root: Path, relative: str) -> tuple[bytes, int]:
    source = template_root / relative
    info = _lstat(source)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise UpgradeError(f"managed template file is not a regular file: {relative}")
    return source.read_bytes(), stat.S_IMODE(info.st_mode)


def _install_no_replace(source: Path, target: Path) -> None:
    """Install source at target without overwriting a file that appeared there.

    The old target was moved aside just before; an editor that saves a new
    file in that moment keeps it, and the upgrade stops instead.
    """
    try:
        os.link(source, target)
    except FileExistsError as exc:
        raise UpgradeError(f"{target} was recreated during the upgrade; rerun the upgrade") from exc
    except OSError:
        # No hard links on this filesystem (FAT, some network shares): fall
        # back to a checked rename. Only here can a save land between the
        # check and the rename; refusing would make upgrades impossible there.
        if _lstat(target) is not None:
            raise UpgradeError(f"{target} was recreated during the upgrade; rerun the upgrade") from None
        os.replace(source, target)
        return
    source.unlink()


def apply_actions(
    project_root: Path,
    template_root: Path,
    actions: list[Action],
    upstream: str,
    commit: str,
    files: dict[str, str],
    *,
    lock_hash: str | None = "",
    lock_metadata: dict | None = None,
) -> None:
    """Stage every write and roll back completed filesystem operations on failure.

    lock_hash is the lock file's hash when it was read (None when absent, ""
    when not recorded); a lock changed since then is refused, not overwritten.
    """
    writes: dict[str, tuple[bytes, int]] = {}
    deletes: set[str] = set()
    conflict_candidates: set[str] = set()
    # What each target held when the plan was made. A file saved after the
    # plan must not be overwritten or deleted with the plan's decision.
    planned_local: dict[str, str | None] = {}
    for action in actions:
        if action.kind in {"CREATE", "UPDATE", "PLACEHOLDER", "CONFLICT", "DELETE"} and protected_project_path(action.path):
            raise UpgradeError(f"upgrade plan targets protected project data: {action.path}")
        if action.kind in {"CREATE", "UPDATE", "PLACEHOLDER"}:
            writes[action.path] = _read_template_bytes(template_root, action.path)
            if action.local != "":
                planned_local[action.path] = action.local
        elif action.kind == "CONFLICT":
            conflict_path = action.path + CONFLICT_SUFFIX
            # The example is template-owned; only its generated review copy may
            # bypass the .env.* destination guard. Manifest and lock paths stay protected.
            if action.path == ".env.example":
                conflict_candidates.add(conflict_path)
            writes[conflict_path] = _read_template_bytes(template_root, action.path)
            planned_local[conflict_path] = None
        elif action.kind == "DELETE":
            deletes.add(action.path)
            if action.local != "":
                planned_local[action.path] = action.local
    writes[LOCK_NAME] = (_lock_bytes(upstream, commit, files, lock_metadata), 0o644)
    if lock_hash != "":
        planned_local[LOCK_NAME] = lock_hash

    overlap = deletes.intersection(writes)
    if overlap:
        raise UpgradeError("upgrade plan both deletes and writes: " + ", ".join(sorted(overlap)))

    for relative in deletes | set(writes):
        if relative != LOCK_NAME and relative not in conflict_candidates and protected_project_path(relative):
            raise UpgradeError(f"upgrade plan targets protected project data: {relative}")
        safe_project_path(project_root, relative)

    mutations: list[Mutation] = []
    created_directories: list[Path] = []
    scratch_root = Path(tempfile.mkdtemp(prefix=".apex-template-upgrade-", dir=project_root))
    preserve_scratch = False
    try:
        staged: dict[str, Path] = {}
        for index, (relative, (contents, mode)) in enumerate(sorted(writes.items())):
            stage = scratch_root / f"write-{index}"
            stage.write_bytes(contents)
            stage.chmod(mode)
            staged[relative] = stage

        # Check every target and parent before changing a file. Missing
        # parent directories are created only after all content is staged.
        directories: set[Path] = set()
        for relative in writes:
            target = safe_project_path(project_root, relative)
            for parent in target.parents:
                if parent == project_root.parent:
                    break
                if not parent.exists():
                    directories.add(parent)
                elif not parent.is_dir():
                    raise UpgradeError(f"managed target parent is not a directory: {relative}")
                if parent == project_root:
                    break
        for directory in sorted(directories, key=lambda path: len(path.parts)):
            if not directory.exists():
                directory.mkdir()
                created_directories.append(directory)

        operations: list[tuple[str, bool]] = [(path, False) for path in sorted(deletes)]
        operations += [(path, True) for path in sorted(writes)]
        for index, (relative, has_replacement) in enumerate(operations):
            target = safe_project_path(project_root, relative)
            info = _lstat(target)
            backup = scratch_root / f"backup-{index}"
            mutation = Mutation(target, backup, info is not None)
            mutations.append(mutation)
            if info is not None:
                if not stat.S_ISREG(info.st_mode):
                    raise UpgradeError(f"managed target is not a regular file: {relative}")
                os.replace(target, backup)
                mutation.original_moved = True
            if relative in planned_local:
                # Compare the bytes actually moved aside, so a save racing this
                # check cannot slip between the comparison and the replacement.
                observed = sha256(backup) if info is not None else None
                if observed != planned_local[relative]:
                    raise UpgradeError(f"{relative} changed after the upgrade was planned; rerun the upgrade")
            if has_replacement:
                mutation.installed_hash = hashlib.sha256(writes[relative][0]).hexdigest()
                _install_no_replace(staged[relative], target)
                mutation.replacement_installed = True
    except BaseException as exc:
        # Ctrl-C and SIGTERM (see _interrupt_on_sigterm) are not OSError, but
        # they stop an upgrade half-way just the same; restore for them too.
        rollback_errors: list[str] = []
        for mutation in reversed(mutations):
            try:
                # A signal can also land after the install but before
                # replacement_installed is set: a regular file holding exactly the
                # staged bytes is that install (installed_hash is set just before).
                if not mutation.replacement_installed and mutation.installed_hash is not None:
                    target_info = _lstat(mutation.target)
                    mutation.replacement_installed = (
                        target_info is not None
                        and stat.S_ISREG(target_info.st_mode)
                        and sha256(mutation.target) == mutation.installed_hash
                    )
                if mutation.replacement_installed:
                    target_info = _lstat(mutation.target)
                    if target_info is not None:
                        if not stat.S_ISREG(target_info.st_mode) or sha256(mutation.target) != mutation.installed_hash:
                            # Saved after this upgrade installed it; keep the save.
                            raise OSError(
                                f"{mutation.target} changed after the upgrade installed it; kept as is"
                                + (f", its previous version is {mutation.backup}" if mutation.original_moved else "")
                            )
                        mutation.target.unlink()
                # A signal can land after the move but before original_moved is
                # set, so look for the backup itself, not only the flag.
                if mutation.original_moved or (mutation.had_original and _lstat(mutation.backup) is not None):
                    if not mutation.replacement_installed and _lstat(mutation.target) is not None:
                        # Something saved a new file after the original moved
                        # aside; keep it and leave the original in the backups.
                        raise OSError(f"{mutation.target} was recreated; its previous version is {mutation.backup}")
                    os.replace(mutation.backup, mutation.target)
            except OSError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
        for directory in reversed(created_directories):
            try:
                directory.rmdir()
            except OSError:
                pass
        if rollback_errors:
            preserve_scratch = True
            detail = (
                f"filesystem update failed: {exc or type(exc).__name__}; rollback incomplete: "
                + "; ".join(rollback_errors)
                + f"; recovery backups retained at {scratch_root}"
            )
        elif isinstance(exc, (OSError, UpgradeError)):
            detail = f"filesystem update failed and was rolled back: {exc}"
        else:
            raise  # an interrupt: everything is back as it was
        raise UpgradeError(detail) from exc
    finally:
        if not preserve_scratch:
            shutil.rmtree(scratch_root, ignore_errors=True)


def _normalize_source(source: str) -> str:
    candidate = Path(source).expanduser()
    if candidate.exists():
        return str(candidate.resolve())
    return source


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument(
        "--source",
        help="template Git URL or local path (default: upstream recorded in the lock or manifest)",
    )
    parser.add_argument("--ref", help="template branch, tag, or commit (default: recorded templateRef, then source default branch)")
    parser.add_argument("--apex-release", choices=("26.1", "26.2"), help="explicitly choose the target template release; doctor still verifies the live instance")
    parser.add_argument("--dry-run", action="store_true", help="print the plan without changing files")
    args = parser.parse_args(argv)

    try:
        project_root = Path(run_git(args.project_root, "rev-parse", "--show-toplevel").strip()).resolve()
        check_project(project_root)
        lock, lock_hash = read_lock_with_hash(project_root)
        source = args.source or lock.get("upstream")
        if not source:
            source = load_manifest(project_root)["upstream"]
        if not source:
            raise UpgradeError("no template source recorded in lock or manifest; pass --source <template Git URL or path>")
        source = _normalize_source(source)
        with tempfile.TemporaryDirectory(prefix="apex-template-") as temporary:
            template_root = Path(temporary) / "template"
            chosen_ref = args.ref or lock.get("templateRef")
            commit = fetch_template(source, chosen_ref, template_root)
            manifest = load_manifest(template_root)
            release = manifest.get("apexRelease", "26.1")
            if release and args.apex_release and args.apex_release != release:
                raise UpgradeError(f"selected template targets APEX {release}, not {args.apex_release}")
            if (manifest["schemaVersion"] == 2 or lock.get("apexRelease") == "26.2") and lock.get("apexRelease") != release and args.apex_release != release:
                raise UpgradeError(f"template release is changing or unknown; review and pass --apex-release {release}")
            metadata = {}
            if chosen_ref:
                metadata["templateRef"] = chosen_ref
            if release or args.apex_release or lock.get("apexRelease"):
                metadata["apexRelease"] = release or args.apex_release or lock["apexRelease"]
            template_owned, placeholders = classify(template_root, manifest)
            validate_lock_ownership(lock, manifest)
            actions, new_lock = plan_upgrade(project_root, template_root, template_owned, placeholders, lock)
            for action in actions:
                print(f"{action.kind} {action.path}")
            if not args.dry_run:
                apply_actions(project_root, template_root, actions, source, commit, new_lock,
                              lock_hash=lock_hash, lock_metadata=metadata)
            if release:
                print(f"Template targets APEX {release}; run team doctor to verify the live instance before APEX operations.")
    except UpgradeError as exc:
        print(f"template upgrade error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"template upgrade error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("template upgrade interrupted; the project was left as it was", file=sys.stderr)
        return 130

    counts: dict[str, int] = {}
    for action in actions:
        counts[action.kind] = counts.get(action.kind, 0) + 1
    summary = ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))
    print(f"Template {commit} {summary}{' (dry run)' if args.dry_run else ''}")
    attention = [action for action in actions if action.kind in ATTENTION]
    if attention and not args.dry_run:
        print(
            f"Review {len(attention)} file(s): merge each {CONFLICT_SUFFIX} into its file and delete it; "
            "KEEP-* files were left as they are.",
            file=sys.stderr,
        )
        return 1
    return 0


def _interrupt_on_sigterm() -> None:
    """Handle SIGTERM like Ctrl-C: the default action ends the process at once,
    leaving a half-applied upgrade and its backups in a scratch directory."""
    if not hasattr(signal, "SIGTERM"):
        return

    def handler(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):  # not the main thread
        pass


if __name__ == "__main__":
    _interrupt_on_sigterm()
    raise SystemExit(main())
