"""Git-backed archive fixtures for legacy format-2 verifier tests only."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import tempfile
from collections.abc import Mapping
from typing import Any

from teamlib.migration_bundle import BundleError, Migration, _validate_id, load_bundles
from teamlib.trees import tree_digest
from teamlib.release import (
    Manifest, ReleaseError, _canonical, _migration_manifest,
    _ustar_split, _write_release_archive,
)


def _git(repo: Path, args: list[str]) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if result.returncode != 0:
        raise ReleaseError(result.stderr.decode("utf-8", "replace").strip() or "Git operation failed")
    return result.stdout


def _resolve(repo: Path, ref: str) -> str:
    try:
        return _git(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"]).decode("ascii").strip()
    except (UnicodeError, ReleaseError) as exc:
        raise ReleaseError(f"could not resolve release ref: {ref}") from exc


def _git_files(repo: Path, commit: str) -> dict[str, bytes]:
    raw = _git(repo, ["ls-tree", "-r", "-z", "--full-tree", commit])
    files: dict[str, bytes] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            header, path_raw = record.split(b"\t", 1)
            mode_raw, object_type, object_id = header.split()
            path = path_raw.decode("utf-8")
            mode = int(mode_raw, 8)
        except (ValueError, UnicodeError) as exc:
            raise ReleaseError("malformed Git tree entry") from exc
        if object_type != b"blob":
            if path.startswith(("apps/", "migrations/", "targets/", "ci/", "evidence/", "app_context/")):
                raise ReleaseError(f"unsupported non-blob release path: {path}")
            continue
        if mode not in {0o100644, 0o100755}:
            raise ReleaseError(f"unsupported release file mode: {path}")
        files[path] = _git(repo, ["cat-file", "blob", object_id.decode("ascii")])
    return files


def _allowed(path: str) -> bool:
    if path.startswith("apps/"):
        if path.endswith("/.gitkeep"):
            return False
        return "/deployments/" not in f"/{path}" and not path.endswith("/deployments")
    if path.startswith("migrations/"):
        return path.endswith(".sql") and not path.startswith("migrations/operations/")
    if path == "targets/masters.json":
        return True
    return path.startswith(("contracts/", "evidence/schema/", "ci/app-checks/", "app_context/", "tools/"))


def _payload_path(source_path: str) -> str:
    if source_path == "targets/masters.json":
        return "release/contracts/masters.json"
    if source_path.startswith("ci/app-checks/"):
        return "release/checks/apps/" + source_path[len("ci/app-checks/"):]
    return "release/" + source_path


def _load_app_release_declaration(
    source_files: Mapping[str, bytes],
    alias: str,
    loaded_bundles: Mapping[str, Migration],
) -> tuple[dict[str, str], ...]:
    context_path = f"app_context/{alias}/release.json"
    raw = source_files.get(context_path)
    if raw is None:
        raise ReleaseError(f"missing application release declaration: {context_path}")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"application release declaration is unreadable: {context_path}") from exc
    if not isinstance(data, Mapping):
        raise ReleaseError(f"application release declaration must be a JSON object: {context_path}")
    if set(data) != {"version", "requires"}:
        raise ReleaseError(f"application release declaration keys are not closed: {context_path}")
    if type(data["version"]) is not int or data["version"] != 1:
        raise ReleaseError(f"unsupported application release declaration version: {context_path}")
    requires = data["requires"]
    if not isinstance(requires, list):
        raise ReleaseError(f"application release declaration requires must be a list: {context_path}")
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for item in requires:
        if not isinstance(item, str):
            raise ReleaseError(f"non-canonical requirement ID: {item}")
        try:
            _validate_id(item)
        except BundleError as exc:
            raise ReleaseError(f"non-canonical requirement ID: {item}") from exc
        if item in seen:
            raise ReleaseError(f"duplicate requirement in {context_path}: {item}")
        seen.add(item)
        if item not in loaded_bundles:
            raise ReleaseError(f"required migration not found in commit: {item}")
        result.append({"id": item, "checksum": loaded_bundles[item].checksum})
    return tuple(result)


def _build_git_release_fixture(
    repo: str | Path,
    ref: str,
    version: str,
    out: str | Path,
    *,
    kind: str | None = None,
    alias: str | None = None,
) -> Manifest:
    if kind is None:
        raise ReleaseError("release kind must be specified: 'schema' or 'app'")
    if kind not in ("schema", "app"):
        raise ReleaseError(f"unsupported release kind: {kind}")
    if kind == "schema" and alias is not None:
        raise ReleaseError("schema release cannot specify an application alias")
    if kind == "app":
        if not alias or not isinstance(alias, str) or "/" in alias:
            raise ReleaseError("app release requires an application alias")

    repo_path = Path(repo)
    if not repo_path.is_dir() or repo_path.is_symlink():
        raise ReleaseError("release repository is not a real directory")
    commit = _resolve(repo_path, ref)
    output = Path(out)
    if output.exists() and any(output.iterdir()):
        raise ReleaseError(f"release output directory must be new: {output}")
    source_files = _git_files(repo_path, commit)

    loaded_bundles: dict[str, Migration] = {}
    migrations_in_commit = {
        path[len("migrations/"):]: data
        for path, data in source_files.items()
        if path.startswith("migrations/") and path.endswith((".sql", ".verify.sql")) and not path.startswith("migrations/operations/")
    }
    if migrations_in_commit:
        with tempfile.TemporaryDirectory(prefix="team-release-bundles-") as tmp_bundles:
            tmp_root = Path(tmp_bundles)
            for rel_path, data in migrations_in_commit.items():
                dest = tmp_root / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            try:
                loaded_bundles = dict(load_bundles(tmp_root))
            except BundleError as exc:
                raise ReleaseError(str(exc)) from exc

    payload: dict[str, bytes] = {}
    required_migrations: tuple[dict[str, str], ...] = ()
    migrations: tuple[dict[str, Any], ...] = ()
    app_digests: dict[str, str] = {}

    if kind == "schema":
        for path, data in source_files.items():
            if path.startswith(("apps/", "ci/app-checks/", "app_context/")):
                continue
            if path == "targets/masters.json":
                continue
            if "/deployments/" in f"/{path}" or path.endswith("/deployments") or path.endswith("default.json"):
                continue
            if not _allowed(path):
                continue
            payload[_payload_path(path)] = data
        if not any(p.startswith("release/migrations/") for p in payload):
            raise ReleaseError("schema release has no migrations")
        migrations = _migration_manifest(loaded_bundles)

    elif kind == "app":
        assert alias is not None
        required_migrations = _load_app_release_declaration(source_files, alias, loaded_bundles)
        app_prefix = f"apps/{alias}/"
        check_prefix = f"ci/app-checks/{alias}"
        context_file = f"app_context/{alias}/release.json"
        has_app = False
        for path, data in source_files.items():
            if path.startswith("migrations/"):
                continue
            if path.startswith("apps/"):
                if not path.startswith(app_prefix):
                    continue
                if path.endswith("/.gitkeep") or "/deployments/" in f"/{path}" or path.endswith("/deployments") or path.endswith("default.json"):
                    continue
                has_app = True
                payload[_payload_path(path)] = data
                continue
            if path.startswith("ci/app-checks/"):
                if path == f"{check_prefix}.json" or path.startswith(f"{check_prefix}/"):
                    payload[_payload_path(path)] = data
                continue
            if path == context_file:
                payload[_payload_path(path)] = data
                continue
            if path == "targets/masters.json":
                payload[_payload_path(path)] = data
                continue
        if not has_app:
            raise ReleaseError(f"application {alias} not found in commit")

        app_tree = {
            path[len(f"release/apps/{alias}/"):]: data
            for path, data in payload.items()
            if path.startswith(f"release/apps/{alias}/")
        }
        app_digests = {alias: tree_digest(app_tree)}

    master_contract_digest = None
    if "release/contracts/masters.json" in payload:
        master_contract_digest = hashlib.sha256(payload["release/contracts/masters.json"]).hexdigest()
    manifest_fields = {
        "format_version": 2,
        "kind": kind,
        "alias": alias,
        "version": version,
        "source_commit": commit,
        "migrations": list(migrations),
        "required_migrations": [dict(r) for r in required_migrations],
        "app_tree_digests": app_digests,
        "master_contract_digest": master_contract_digest,
    }
    return _write_release_archive(output, payload, manifest_fields)
