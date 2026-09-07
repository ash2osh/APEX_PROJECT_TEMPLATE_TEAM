"""Deterministic release.tar construction, verification and planning."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import io
import json
from pathlib import Path
import posixpath
import re
import shutil
import subprocess
import tarfile
import tempfile
from typing import Any, Mapping
from typing import Callable

from .migration_bundle import BundleError, Migration, load_bundles
from .migration_plan import plan_migrations
from .trees import tree_digest


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class Manifest:
    format_version: int
    version: str
    source_commit: str
    source_tree: str
    migrations: tuple[dict[str, Any], ...]
    app_tree_digests: dict[str, str]
    payload_paths: tuple[str, ...]
    archive_path: Path
    archive_digest: str
    payload: tuple[dict[str, Any], ...] = ()
    toolchain: Mapping[str, Any] = field(default_factory=dict)
    staging_dir: Path | None = None


@dataclass(frozen=True)
class ReleasePlan:
    archive_digest: str
    target_digest: str
    pending: tuple[str, ...]
    artifact_history_digest: str
    target: Mapping[str, Any]
    history_digest: str = ""


@dataclass(frozen=True)
class ApplyReport:
    status: str
    pending: tuple[str, ...]
    archive_digest: str


_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


def validate_release_identity(ref: str, version: str, source_commit: str, records: Mapping[str, Any]) -> None:
    """Bind a semver tag and immutable version record before artifact build."""
    if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
        raise ReleaseError("release version must be semantic MAJOR.MINOR.PATCH")
    ref_text = str(ref)
    tag = ref_text.rsplit("/", 1)[-1]
    if tag.startswith("v") and tag[1:] != version:
        raise ReleaseError(f"tag {tag} does not match release version {version}")
    existing = records.get(version) if isinstance(records, Mapping) else None
    if existing is not None:
        if not isinstance(existing, Mapping) or existing.get("source_commit") != source_commit:
            raise ReleaseError(f"release version {version} is already bound to a different source commit")
        if existing.get("archive_digest") not in (None, "") and len(str(existing["archive_digest"])) != 64:
            raise ReleaseError(f"release record for {version} has an invalid archive digest")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _git(repo: Path, args: list[str]) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
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


def _ustar_split(name: str) -> tuple[str, str]:
    encoded = name.encode("utf-8")
    if len(encoded) <= 100:
        return "", name
    candidates = [index for index, char in enumerate(name) if char == "/"]
    for index in reversed(candidates):
        prefix, suffix = name[:index], name[index + 1:]
        if len(prefix.encode("utf-8")) <= 155 and len(suffix.encode("utf-8")) <= 100:
            return prefix, suffix
    raise ReleaseError(f"release path cannot be represented in deterministic ustar: {name} ({len(encoded)} bytes)")


def _manifest_from_data(data: Mapping[str, Any], archive_path: Path, archive_digest: str, staging_dir: Path | None = None) -> Manifest:
    return Manifest(
        int(data["format_version"]), str(data["version"]), str(data["source_commit"]), str(data["source_tree"]),
        tuple(data.get("migrations", ())), dict(data.get("app_tree_digests", {})), tuple(data.get("payload_paths", ())),
        archive_path, archive_digest, tuple(data.get("payload", ())), data.get("toolchain", {}), staging_dir,
    )


def build_release(repo: str | Path, ref: str, version: str, out: str | Path) -> Manifest:
    repo_path = Path(repo)
    if not repo_path.is_dir() or repo_path.is_symlink():
        raise ReleaseError("release repository is not a real directory")
    commit = _resolve(repo_path, ref)
    record_path = repo_path / "release-record.json"
    records: Mapping[str, Any] = {}
    if record_path.is_file():
        try:
            raw_records = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("release record is unreadable") from exc
        records = raw_records.get("releases", raw_records) if isinstance(raw_records, Mapping) else {}
    validate_release_identity(ref, version, commit, records)
    output = Path(out)
    if output.exists() and any(output.iterdir()):
        raise ReleaseError(f"release output directory must be new: {output}")
    source_files = _git_files(repo_path, commit)
    payload: dict[str, bytes] = {}
    for path, data in source_files.items():
        if not _allowed(path):
            continue
        if path.startswith("apps/") and ("/deployments/" in f"/{path}" or path.endswith("default.json")):
            continue
        payload[_payload_path(path)] = data
    if not payload:
        raise ReleaseError("release has no allowlisted payload")
    for path in payload:
        _ustar_split(path)
    # Do all path representability checks before creating the output directory
    # or opening the archive. A rejected long path therefore cannot leave a
    # misleading partial artifact behind.
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="team-release-stage-") as stage_name:
        stage = Path(stage_name)
        for path, data in payload.items():
            destination = stage / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        migrations_data = {path[len("release/"):]: data for path, data in payload.items() if path.startswith("release/migrations/")}
        migrations: tuple[dict[str, Any], ...] = ()
        if migrations_data:
            migration_root = stage / "migration-input"
            for relative, data in migrations_data.items():
                destination = migration_root / relative[len("migrations/"):]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            try:
                loaded = load_bundles(migration_root)
            except BundleError as exc:
                raise ReleaseError(str(exc)) from exc
            migrations = tuple({"id": item.id, "checksum": item.checksum, "target": item.target, "dependencies": list(item.dependencies), "destructive": item.destructive} for item in sorted(loaded.values(), key=lambda value: (value.stamp, value.id)))
        app_groups: dict[str, dict[str, bytes]] = {}
        for path, data in payload.items():
            if path.startswith("release/apps/"):
                parts = path.split("/", 3)
                if len(parts) == 4:
                    app_groups.setdefault(parts[2], {})[parts[3]] = data
        app_digests = {alias: tree_digest(tree) for alias, tree in sorted(app_groups.items())}
        payload_records = tuple({"path": path, "length": len(data), "sha256": hashlib.sha256(data).hexdigest()} for path, data in sorted(payload.items()))
        source_tree_digest = hashlib.sha256(_canonical(payload_records)).hexdigest()
        master_contract_digest = None
        if "release/contracts/masters.json" in payload:
            master_contract_digest = hashlib.sha256(payload["release/contracts/masters.json"]).hexdigest()
        app_check_records = [record for record in payload_records if record["path"].startswith("release/checks/apps/")]
        app_checks_digest = hashlib.sha256(_canonical(app_check_records)).hexdigest() if app_check_records else None
        manifest_data = {
            "format_version": 1, "version": version, "source_commit": commit, "source_tree": source_tree_digest,
            "toolchain": {"python": "3.10+", "archive_format": "ustar", "normalizer": "team-v1"},
            "migrations": list(migrations), "app_tree_digests": app_digests,
            "master_contract_digest": master_contract_digest,
            "app_checks_digest": app_checks_digest,
            "payload_paths": list(sorted(payload)), "payload": list(payload_records),
        }
        manifest_bytes = json.dumps(manifest_data, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n"
        (stage / "release" / "MANIFEST.json").write_bytes(manifest_bytes)
        archive = output / "release.tar"
        archive_payload = {**payload, "release/MANIFEST.json": manifest_bytes}
        for path in archive_payload:
            _ustar_split(path)
        with tarfile.open(archive, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for path in sorted(archive_payload):
                data = archive_payload[path]
                info = tarfile.TarInfo(path)
                info.size = len(data)
                info.mtime = 0
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                info.mode = 0o644
                import io
                tar.addfile(info, io.BytesIO(data))
        staging_dir = output / "release"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        shutil.copytree(stage / "release", staging_dir)
    archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    existing_record = records.get(version) if isinstance(records, Mapping) else None
    if isinstance(existing_record, Mapping) and existing_record.get("archive_digest") not in (None, "", archive_digest):
        archive.unlink(missing_ok=True)
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        raise ReleaseError(f"release version {version} is already bound to a different archive digest")
    return _manifest_from_data(manifest_data, archive, archive_digest, staging_dir)


def verify_release(release_tar: str | Path) -> Manifest:
    archive = Path(release_tar)
    if archive.is_symlink() or not archive.is_file():
        raise ReleaseError("release archive is not a regular file")
    archive_digest, members = _read_archive_bytes(archive)
    return _verify_archive_members(archive, archive_digest, members)


def _verify_archive_members(archive: Path, archive_digest: str, members: Mapping[str, bytes]) -> Manifest:
    manifest_bytes = members.get("release/MANIFEST.json")
    if manifest_bytes is None:
        raise ReleaseError("release manifest is missing")
    try:
        data = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("release manifest is unreadable") from exc
    if not isinstance(data, Mapping):
        raise ReleaseError("release manifest must be a JSON object")
    payload_records = data.get("payload")
    if not isinstance(payload_records, list):
        raise ReleaseError("release manifest payload must be a list")
    expected: dict[str, Mapping[str, Any]] = {}
    for record in payload_records:
        if not isinstance(record, Mapping):
            raise ReleaseError("release manifest payload record must be an object")
        path = record.get("path")
        length = record.get("length")
        checksum = record.get("sha256")
        if (
            not isinstance(path, str)
            or path == "release/MANIFEST.json"
            or not path.startswith("release/")
            or path in expected
            or not isinstance(length, int)
            or isinstance(length, bool)
            or length < 0
            or not isinstance(checksum, str)
            or not re.fullmatch(r"[0-9a-f]{64}", checksum)
        ):
            raise ReleaseError("release manifest contains an invalid payload record")
        expected[path] = record
    actual = {name: name for name in members if name != "release/MANIFEST.json"}
    if set(expected) != set(actual):
        raise ReleaseError("release payload set does not match manifest")
    for path, record in expected.items():
        data_bytes = members[path]
        if len(data_bytes) != record["length"] or hashlib.sha256(data_bytes).hexdigest() != record["sha256"]:
            raise ReleaseError(f"release payload hash mismatch: {path}")
    payload_paths = data.get("payload_paths")
    if not isinstance(payload_paths, list) or payload_paths != sorted(expected):
        raise ReleaseError("release manifest payload_paths does not match payload")
    for field, path, label in (
        ("master_contract_digest", "release/contracts/masters.json", "master contract"),
    ):
        supplied = data.get(field)
        if supplied is not None and (not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied)):
            raise ReleaseError(f"release manifest {label} digest is malformed")
        actual_digest = hashlib.sha256(members[path]).hexdigest() if path in members else None
        if supplied != actual_digest:
            raise ReleaseError(f"release manifest {label} digest does not match payload")
    supplied_checks = data.get("app_checks_digest")
    if supplied_checks is not None and (not isinstance(supplied_checks, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied_checks)):
        raise ReleaseError("release manifest app-check digest is malformed")
    check_records = [record for path, record in sorted(expected.items()) if path.startswith("release/checks/apps/")]
    actual_checks = hashlib.sha256(_canonical(check_records)).hexdigest() if check_records else None
    if supplied_checks != actual_checks:
        raise ReleaseError("release manifest app-check digest does not match payload")
    return _manifest_from_data(data, archive, archive_digest)


def _read_archive_bytes(archive: Path) -> tuple[str, dict[str, bytes]]:
    try:
        raw = archive.read_bytes()
    except OSError as exc:
        raise ReleaseError("release archive is unreadable") from exc
    digest = hashlib.sha256(raw).hexdigest()
    members_data: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tar:
            for member in tar.getmembers():
                name = member.name
                if name.startswith("/") or ".." in Path(name).parts or not name.startswith("release/") or not member.isfile():
                    raise ReleaseError(f"unsafe release archive member: {name}")
                if name in members_data:
                    raise ReleaseError(f"duplicate release archive member: {name}")
                stream = tar.extractfile(member)
                if stream is None:
                    raise ReleaseError(f"release archive member cannot be read: {name}")
                members_data[name] = stream.read()
    except ReleaseError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseError("release archive is unreadable") from exc
    return digest, members_data


def release_app_trees(release_tar: str | Path) -> dict[str, dict[str, bytes]]:
    """Return exact packaged application bytes after one complete verification."""
    archive = Path(release_tar)
    if archive.is_symlink() or not archive.is_file():
        raise ReleaseError("release archive is not a regular file")
    archive_digest, members = _read_archive_bytes(archive)
    manifest = _verify_archive_members(archive, archive_digest, members)
    return _release_app_trees_from_members(manifest, members)


def release_migration_files(release_tar: str | Path) -> dict[str, bytes]:
    """Return verified migration bundle members from the immutable archive."""
    archive = Path(release_tar)
    if archive.is_symlink() or not archive.is_file():
        raise ReleaseError("release archive is not a regular file")
    archive_digest, members = _read_archive_bytes(archive)
    _verify_archive_members(archive, archive_digest, members)
    files: dict[str, bytes] = {}
    for path, data in members.items():
        if not path.startswith("release/migrations/"):
            continue
        relative = path[len("release/migrations/"):]
        if not relative or "/" in relative or not relative.endswith((".sql", ".verify.sql")):
            raise ReleaseError(f"malformed packaged migration path: {path}")
        files[relative] = data
    return dict(sorted(files.items()))


def _release_app_trees_from_members(manifest: Manifest, members: Mapping[str, bytes]) -> dict[str, dict[str, bytes]]:
    apps: dict[str, dict[str, bytes]] = {}
    for path, data in members.items():
        if not path.startswith("release/apps/"):
            continue
        relative = path[len("release/apps/"):]
        alias, separator, app_path = relative.partition("/")
        if not separator or not alias or not app_path:
            raise ReleaseError(f"malformed packaged application path: {path}")
        apps.setdefault(alias, {})[app_path] = data
    actual = {alias: tree_digest(tree) for alias, tree in apps.items()}
    if actual != manifest.app_tree_digests:
        raise ReleaseError("packaged application tree digest does not match manifest")
    return {alias: dict(sorted(tree.items())) for alias, tree in sorted(apps.items())}


def _release_app_order_from_members(members: Mapping[str, bytes], apps: Mapping[str, Mapping[str, bytes]]) -> tuple[str, ...]:
    contract_bytes = members.get("release/contracts/masters.json") or members.get("release/targets/masters.json")
    masters: set[str] = set()
    if contract_bytes is not None:
        try:
            contract = json.loads(contract_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("packaged master contract is unreadable") from exc
        if not isinstance(contract, Mapping):
            raise ReleaseError("packaged master contract must be an object")
        entries = contract.get("masters", [])
        if not isinstance(entries, list):
            raise ReleaseError("packaged master contract masters must be a list")
        for item in entries:
            if isinstance(item, Mapping) and isinstance(item.get("alias"), str):
                masters.add(item["alias"])
    return tuple(sorted(alias for alias in apps if alias in masters) + sorted(alias for alias in apps if alias not in masters))


def release_app_order(release_tar: str | Path) -> tuple[str, ...]:
    """Return packaged aliases with contracted masters before subscribers."""
    archive = Path(release_tar)
    if archive.is_symlink() or not archive.is_file():
        raise ReleaseError("release archive is not a regular file")
    archive_digest, members = _read_archive_bytes(archive)
    manifest = _verify_archive_members(archive, archive_digest, members)
    apps = _release_app_trees_from_members(manifest, members)
    return _release_app_order_from_members(members, apps)


def _plan_from_manifest(manifest: Manifest, history: Mapping[str, Any], target: Mapping[str, Any]) -> ReleasePlan:
    history_data = history.get("history", history) if isinstance(history, Mapping) else {}
    if not isinstance(history_data, Mapping):
        raise ReleaseError("target history must contain a mapping")
    errors = []
    pending = []
    artifact_ids = {str(item.get("id")) for item in manifest.migrations if isinstance(item, Mapping)}
    for migration_id, entry in history_data.items():
        if not isinstance(migration_id, str) or not isinstance(entry, Mapping):
            errors.append(f"malformed target history entry: {migration_id}")
            continue
        status = entry.get("status")
        if status in {"RUNNING", "UNKNOWN", "FAILED"}:
            errors.append(f"unresolved target migration attempt: {migration_id}")
        elif status not in {"APPLIED", None, ""}:
            errors.append(f"unknown target migration history status: {migration_id}")
        if migration_id not in artifact_ids and status == "APPLIED":
            errors.append(f"target history contains foreign migration: {migration_id}")
    for migration in manifest.migrations:
        entry = history_data.get(migration["id"]) if isinstance(history_data, Mapping) else None
        if isinstance(entry, Mapping) and entry.get("status") == "APPLIED":
            if entry.get("checksum") != migration["checksum"]:
                errors.append(f"history checksum mismatch: {migration['id']}")
        else:
            pending.append(migration["id"])
    if errors:
        raise ReleaseError("; ".join(dict.fromkeys(errors)))
    target_digest = hashlib.sha256(_canonical(target)).hexdigest()
    artifact_history_digest = hashlib.sha256(_canonical(manifest.migrations)).hexdigest()
    history_digest = hashlib.sha256(_canonical(history_data)).hexdigest()
    return ReleasePlan(manifest.archive_digest, target_digest, tuple(pending), artifact_history_digest, target, history_digest)


def plan_release(release_tar: str | Path, history: Mapping[str, Any], target: Mapping[str, Any]) -> ReleasePlan:
    manifest = verify_release(release_tar)
    return _plan_from_manifest(manifest, history, target)


def apply_release(
    release_tar: str | Path,
    target: Mapping[str, Any],
    plan: ReleasePlan,
    *,
    history: Mapping[str, Any] | None = None,
    apply_migrations: Callable[[tuple[Mapping[str, Any], ...], ReleasePlan], Any] | None = None,
    deploy_application: Callable[[str, Mapping[str, bytes], ReleasePlan], Any] | None = None,
) -> ApplyReport:
    environment = target.get("environment") if isinstance(target, Mapping) else getattr(target, "environment", None)
    if environment == "production":
        raise ReleaseError("production release apply is refused; generate a runbook")
    archive = Path(release_tar)
    if archive.is_symlink() or not archive.is_file():
        raise ReleaseError("release archive is not a regular file")
    archive_digest, members = _read_archive_bytes(archive)
    manifest = _verify_archive_members(archive, archive_digest, members)
    if manifest.archive_digest != plan.archive_digest:
        raise ReleaseError("release archive changed after plan generation")
    if hashlib.sha256(_canonical(target)).hexdigest() != plan.target_digest:
        raise ReleaseError("release target changed after plan generation")
    if history is None:
        raise ReleaseError("apply-release requires a fresh owner-supplied target history")
    current = _plan_from_manifest(manifest, history, target)
    if current.history_digest != plan.history_digest or current.pending != plan.pending or current.target_digest != plan.target_digest:
        raise ReleaseError("target history or pending release work changed after plan generation")
    if apply_migrations is None and deploy_application is None:
        return ApplyReport("planned", plan.pending, manifest.archive_digest)
    pending = tuple(item for item in manifest.migrations if item.get("id") in plan.pending)
    if pending and apply_migrations is None:
        raise ReleaseError("release contains pending migrations but no non-production migration adapter was supplied")
    apps = _release_app_trees_from_members(manifest, members)
    if apps and deploy_application is None:
        raise ReleaseError("release contains applications but no non-production deployment adapter was supplied")
    if apply_migrations is not None:
        try:
            apply_migrations(pending, plan)
        except Exception as exc:
            raise ReleaseError(f"release migration adapter failed: {exc}") from exc
    if deploy_application is not None:
        for alias in _release_app_order_from_members(members, apps):
            try:
                deploy_application(alias, apps[alias], plan)
            except Exception as exc:
                raise ReleaseError(f"release deployment adapter failed for {alias}: {exc}") from exc
    return ApplyReport("applied", plan.pending, manifest.archive_digest)


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="release")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build-release")
    build.add_argument("--repo", default=".")
    build.add_argument("--ref", required=True)
    build.add_argument("--version", required=True)
    build.add_argument("--out", required=True)
    verify = sub.add_parser("verify-release")
    verify.add_argument("archive")
    plan_parser = sub.add_parser("plan-release")
    plan_parser.add_argument("archive")
    plan_parser.add_argument("--history", required=True)
    plan_parser.add_argument("--target", required=True)
    plan_parser.add_argument("--out", required=True)
    apply_parser = sub.add_parser("apply-release")
    apply_parser.add_argument("archive")
    apply_parser.add_argument("--target", required=True)
    apply_parser.add_argument("--plan", required=True)
    apply_parser.add_argument("--history", required=True)
    args = parser.parse_args(list(argv or []))
    if args.command == "build-release":
        result = build_release(args.repo, args.ref, args.version, args.out)
        print(result.archive_digest)
    elif args.command == "verify-release":
        print(verify_release(args.archive).archive_digest)
    elif args.command == "plan-release":
        history_raw = json.loads(Path(args.history).read_text(encoding="utf-8"))
        target = json.loads(Path(args.target).read_text(encoding="utf-8"))
        history = history_raw.get("history", history_raw) if isinstance(history_raw, Mapping) else {}
        result = plan_release(args.archive, history, target)
        data = {
            "version": 1,
            "archive_digest": result.archive_digest,
            "target_digest": result.target_digest,
            "pending": list(result.pending),
            "artifact_history_digest": result.artifact_history_digest,
            "history_digest": result.history_digest,
            "target": dict(result.target),
        }
        Path(args.out).write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps(data, sort_keys=True))
    else:
        history_raw = json.loads(Path(args.history).read_text(encoding="utf-8"))
        target = json.loads(Path(args.target).read_text(encoding="utf-8"))
        plan_raw = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        history = history_raw.get("history", history_raw) if isinstance(history_raw, Mapping) else {}
        plan = ReleasePlan(
            plan_raw["archive_digest"], plan_raw["target_digest"], tuple(plan_raw.get("pending", ())),
            plan_raw["artifact_history_digest"], plan_raw.get("target", target), plan_raw.get("history_digest", ""),
        )
        report = apply_release(args.archive, target, plan, history=history)
        print(json.dumps({"status": report.status, "pending": report.pending, "archive_digest": report.archive_digest}, sort_keys=True))
    return 0
