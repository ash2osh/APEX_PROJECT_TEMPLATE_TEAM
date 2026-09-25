"""Deterministic release.tar construction, verification and planning."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import uuid
from typing import Any
from collections.abc import Mapping
from collections.abc import Callable

from .app_checks import AppCheckBundle, AppCheckError, build_app_check_bundle
from .migration_bundle import BundleError, Migration, _validate_id, load_bundles
from .migration_plan import plan_migrations
from .trees import tree_digest
from .runtime import RELEASE_SQLCL_BUILD
from .sqlcl import result_is_unknown


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
    app_checks_digest: str | None = None
    staging_dir: Path | None = None
    kind: str | None = None
    alias: str | None = None
    required_migrations: tuple[dict[str, str], ...] = ()
    # Format 3: built from the development database, not a Git commit.
    source: Mapping[str, Any] | None = None
    events: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ReleasePlan:
    archive_digest: str
    target_digest: str
    pending: tuple[str, ...]
    artifact_history_digest: str
    target: Mapping[str, Any]
    history_digest: str = ""
    foreign_applied: tuple[str, ...] = ()
    foreign_reverted: tuple[str, ...] = ()
    # Format 3 retains the exact source-database transition suffix.  A
    # migration ID can occur more than once here (up, down, then redo).
    events: tuple[Mapping[str, Any], ...] = ()
    replay_from: int = 0


@dataclass(frozen=True)
class ApplyReport:
    status: str
    pending: tuple[str, ...]
    archive_digest: str
    version: int = 1
    source_commit: str = ""
    target_state_key: str = ""
    target_digest: str = ""
    history_digest: str = ""
    source: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        value = {
            "version": self.version,
            "status": self.status,
            "archive_digest": self.archive_digest,
            "target_state_key": self.target_state_key,
            "target_digest": self.target_digest,
            "history_digest": self.history_digest,
            "pending": list(self.pending),
        }
        if self.source is None:
            value["source_commit"] = self.source_commit
        else:
            value["source"] = dict(self.source)
        return value


_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_DB_RELEASE_REPLAY_SOURCE_RE = re.compile(r"^db-release:([0-9a-f]{64}):([1-9][0-9]*):([0-9]+)$")
_DB_RELEASE_SOURCE_RE = re.compile(r"^db-release:[0-9a-f]{64}$")
_MANIFEST_KEYS_V1 = {
    "format_version", "version", "source_commit", "source_tree", "toolchain",
    "migrations", "app_tree_digests", "master_contract_digest",
    "app_checks_digest", "payload_paths", "payload",
}
_MANIFEST_KEYS_V2 = _MANIFEST_KEYS_V1 | {
    "kind", "alias", "required_migrations",
}
# Format 3 identifies its source by the development database ledger it was cut
# from; there is no Git commit.
_MANIFEST_KEYS_V3 = (_MANIFEST_KEYS_V2 - {"source_commit"}) | {"source", "events"}
_SCHEMA_SOURCE_KEYS_V3 = {"kind", "instance_id", "history_cut", "history_digest", "frontier_digest"}
# Format 3 schema releases carry the development frontier inventory manifest so
# qualification can prove the replayed target has the same structure.
_FRONTIER_PATH = "release/frontier/inventory.json"
_APP_SOURCE_KEYS_V3 = _SCHEMA_SOURCE_KEYS_V3 | {
    "app_generation", "app_tree_digest", "app_checks_digest", "master_contract_digest",
}
_EVENT_KEYS_V3 = {"sequence", "id", "operation", "checksum"}
_TOOLCHAIN_KEYS_V2 = {"python", "archive_format", "normalizer"}
_TOOLCHAIN_KEYS_V3 = _TOOLCHAIN_KEYS_V2 | {"sqlcl"}
_PAYLOAD_RECORD_KEYS = {"path", "length", "sha256"}
_REQUIRED_MIGRATION_RECORD_KEYS = {"id", "checksum"}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _migration_manifest(
    loaded: Mapping[str, Migration],
) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "id": item.id,
            "checksum": item.checksum,
            "target": item.target,
            "dependencies": [list(edge) for edge in item.dependencies],
            "destructive": item.destructive,
            "reversible": item.reversible,
            "down_destructive": item.down_destructive,
        }
        for item in sorted(loaded.values(), key=lambda value: (value.stamp, value.id))
    )


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
        format_version=int(data["format_version"]),
        version=str(data["version"]),
        source_commit=str(data.get("source_commit") or ""),
        source_tree=str(data["source_tree"]),
        migrations=tuple(data.get("migrations", ())),
        app_tree_digests=dict(data.get("app_tree_digests", {})),
        payload_paths=tuple(data.get("payload_paths", ())),
        archive_path=archive_path,
        archive_digest=archive_digest,
        payload=tuple(data.get("payload", ())),
        toolchain=data.get("toolchain", {}),
        app_checks_digest=data.get("app_checks_digest"),
        staging_dir=staging_dir,
        kind=data.get("kind"),
        alias=data.get("alias"),
        required_migrations=tuple(data.get("required_migrations", ())),
        source=data.get("source"),
        events=tuple(data.get("events", ())),
    )


def _write_release_archive(
    output: Path,
    payload: Mapping[str, bytes],
    manifest_fields: Mapping[str, Any],
) -> Manifest:
    """Write one deterministic USTAR archive plus its staging copy, then verify it.

    ``manifest_fields`` carries everything except the fields derived from the
    payload itself (``source_tree``, ``toolchain``, ``app_checks_digest``,
    ``payload_paths`` and ``payload``), which are computed here so every
    builder derives them the same way.
    """
    for path in payload:
        _ustar_split(path)

    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="team-release-stage-") as stage_name:
        stage = Path(stage_name)
        for path, data in payload.items():
            destination = stage / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)

        payload_records = tuple(
            {"path": path, "length": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for path, data in sorted(payload.items())
        )
        source_tree_digest = hashlib.sha256(_canonical(payload_records)).hexdigest()
        app_check_records = [record for record in payload_records if record["path"].startswith("release/checks/apps/")]
        app_checks_digest = hashlib.sha256(_canonical(app_check_records)).hexdigest() if app_check_records else None

        toolchain = manifest_fields.get("toolchain", {
            "python": "3.10+", "archive_format": "ustar", "normalizer": "team-v1",
        })
        if not isinstance(toolchain, Mapping):
            raise ReleaseError("release manifest toolchain is malformed")
        manifest_data = {
            **manifest_fields,
            "source_tree": source_tree_digest,
            "toolchain": dict(toolchain),
            "app_checks_digest": app_checks_digest,
            "payload_paths": list(sorted(payload)),
            "payload": list(payload_records),
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
                tar.addfile(info, io.BytesIO(data))
        staging_dir = output / "release"
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        shutil.copytree(stage / "release", staging_dir)
    archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    try:
        verified = verify_release(archive)
    except ReleaseError:
        archive.unlink(missing_ok=True)
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        raise
    if verified.archive_digest != archive_digest:
        archive.unlink(missing_ok=True)
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        raise ReleaseError("release archive digest changed during verification")
    return replace(verified, staging_dir=staging_dir)


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

    format_version = data.get("format_version")
    if type(format_version) is not int or format_version not in (1, 2, 3):
        raise ReleaseError("release manifest format version is unsupported")

    allowed_keys = {1: _MANIFEST_KEYS_V1, 2: _MANIFEST_KEYS_V2, 3: _MANIFEST_KEYS_V3}[format_version]
    missing_keys = sorted(allowed_keys - set(data))
    unknown_keys = sorted(set(data) - allowed_keys)
    if missing_keys or unknown_keys:
        detail = []
        if missing_keys:
            detail.append("missing=" + ",".join(missing_keys))
        if unknown_keys:
            detail.append("unknown=" + ",".join(unknown_keys))
        raise ReleaseError("release manifest keys are not closed: " + "; ".join(detail))

    if not isinstance(data["version"], str) or not _SEMVER_RE.fullmatch(data["version"]):
        raise ReleaseError("release manifest version is malformed")
    if format_version == 3:
        if data.get("kind") not in ("schema", "app"):
            raise ReleaseError("format 3 release kind is invalid")
        _verify_database_source(data["source"], data["kind"])
    elif not isinstance(data["source_commit"], str) or not _COMMIT_RE.fullmatch(data["source_commit"]):
        raise ReleaseError("release manifest source commit is malformed")
    if not isinstance(data["source_tree"], str) or not _DIGEST_RE.fullmatch(data["source_tree"]):
        raise ReleaseError("release manifest source tree digest is malformed")
    toolchain = data["toolchain"]
    expected_toolchain_keys = _TOOLCHAIN_KEYS_V3 if format_version == 3 else _TOOLCHAIN_KEYS_V2
    if (
        not isinstance(toolchain, Mapping)
        or set(toolchain) != expected_toolchain_keys
        or any(not isinstance(value, str) or not value.strip() for value in toolchain.values())
    ):
        raise ReleaseError("release manifest toolchain is malformed")

    for p in members:
        if "/deployments/" in f"/{p}" or p.endswith("/deployments") or p.endswith("default.json"):
            raise ReleaseError(f"rejected path in release archive: {p}")

    kind = data.get("kind", "schema")
    if format_version in (2, 3):
        if kind not in ("schema", "app"):
            raise ReleaseError("release manifest kind is invalid")
        if kind == "schema":
            if data["alias"] is not None:
                raise ReleaseError("schema release manifest alias must be null")
            if data["required_migrations"] != []:
                raise ReleaseError("schema release manifest cannot declare required migrations")
            if data["app_tree_digests"] != {}:
                raise ReleaseError("schema release manifest cannot contain app trees")
            if data["app_checks_digest"] is not None:
                raise ReleaseError("schema release manifest cannot contain app checks")
            for p in members:
                if p.startswith(("release/apps/", "release/checks/apps/", "release/app_context/")):
                    raise ReleaseError(f"cross-kind member in schema archive: {p}")
        elif kind == "app":
            alias = data["alias"]
            if not isinstance(alias, str) or not alias or "/" in alias:
                raise ReleaseError("app release manifest alias is invalid")
            if data["migrations"] != []:
                raise ReleaseError("app release manifest cannot contain migration metadata")
            req_migs = data["required_migrations"]
            if not isinstance(req_migs, list):
                raise ReleaseError("app release manifest required_migrations must be a list")
            seen_reqs: set[str] = set()
            for req in req_migs:
                if not isinstance(req, Mapping) or set(req) != _REQUIRED_MIGRATION_RECORD_KEYS:
                    raise ReleaseError("invalid required migration record shape")
                req_id = req.get("id")
                req_cs = req.get("checksum")
                if not isinstance(req_id, str) or not isinstance(req_cs, str) or len(req_cs) != 64:
                    raise ReleaseError("invalid required migration record values")
                try:
                    _validate_id(req_id)
                except BundleError as exc:
                    raise ReleaseError(f"invalid required migration ID: {req_id}") from exc
                if req_id in seen_reqs:
                    raise ReleaseError(f"duplicate required migration: {req_id}")
                seen_reqs.add(req_id)
            if set(data["app_tree_digests"].keys()) != {alias}:
                raise ReleaseError("app release manifest must contain exactly its selected app tree digest")
            app_prefix = f"release/apps/{alias}/"
            check_prefix = f"release/checks/apps/{alias}"
            context_file = f"release/app_context/{alias}/release.json"
            for p in members:
                if p == "release/MANIFEST.json":
                    continue
                if p.startswith("release/migrations/"):
                    raise ReleaseError(f"cross-kind member in app archive: {p}")
                if p.startswith("release/apps/"):
                    if not p.startswith(app_prefix):
                        raise ReleaseError(f"cross-kind member in app archive: {p}")
                elif p.startswith("release/checks/apps/"):
                    if p != f"{check_prefix}.json" and not p.startswith(f"{check_prefix}/"):
                        raise ReleaseError(f"cross-kind check in app archive: {p}")
                elif p.startswith("release/app_context/"):
                    if p != context_file:
                        raise ReleaseError(f"cross-kind context in app archive: {p}")
                elif p != "release/contracts/masters.json":
                    pass

    app_tree_digests = data["app_tree_digests"]
    if not isinstance(app_tree_digests, Mapping):
        raise ReleaseError("release manifest application tree digests are malformed")
    for alias_key, digest in app_tree_digests.items():
        if (
            not isinstance(alias_key, str)
            or not alias_key
            or "/" in alias_key
            or not isinstance(digest, str)
            or not _DIGEST_RE.fullmatch(digest)
        ):
            raise ReleaseError("release manifest application tree digests are malformed")
    migrations = data["migrations"]
    if not isinstance(migrations, list):
        raise ReleaseError("release manifest migration metadata must be a list")
    for migration in migrations:
        if not isinstance(migration, Mapping):
            raise ReleaseError("release manifest migration metadata is malformed")
    for field_name, label in (
        ("master_contract_digest", "master contract"),
        ("app_checks_digest", "app-check"),
    ):
        supplied = data[field_name]
        if supplied is not None and (not isinstance(supplied, str) or not _DIGEST_RE.fullmatch(supplied)):
            raise ReleaseError(f"release manifest {label} digest is malformed")
    payload_records = data.get("payload")
    if not isinstance(payload_records, list):
        raise ReleaseError("release manifest payload must be a list")
    expected: dict[str, Mapping[str, Any]] = {}
    for record in payload_records:
        if not isinstance(record, Mapping):
            raise ReleaseError("release manifest payload record must be an object")
        if set(record) != _PAYLOAD_RECORD_KEYS:
            raise ReleaseError("release manifest payload record keys are not closed")
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
    actual_records = tuple(
        {
            "path": path,
            "length": len(member_bytes),
            "sha256": hashlib.sha256(member_bytes).hexdigest(),
        }
        for path, member_bytes in sorted(members.items())
        if path != "release/MANIFEST.json"
    )
    actual_by_path = {record["path"]: record for record in actual_records}
    if set(expected) != set(actual_by_path):
        raise ReleaseError("release payload set does not match manifest")
    for path, record in expected.items():
        if dict(record) != actual_by_path[path]:
            raise ReleaseError(f"release payload hash mismatch: {path}")
    if tuple(payload_records) != actual_records:
        raise ReleaseError("release manifest payload records are not canonical")
    payload_paths = data.get("payload_paths")
    if not isinstance(payload_paths, list) or payload_paths != [record["path"] for record in actual_records]:
        raise ReleaseError("release manifest payload_paths does not match payload")
    actual_source_tree = hashlib.sha256(_canonical(actual_records)).hexdigest()
    if data["source_tree"] != actual_source_tree:
        raise ReleaseError("release manifest source tree does not match payload")
    for field_name, path, label in (
        ("master_contract_digest", "release/contracts/masters.json", "master contract"),
    ):
        actual_digest = hashlib.sha256(members[path]).hexdigest() if path in members else None
        if data[field_name] != actual_digest:
            raise ReleaseError(f"release manifest {label} digest does not match payload")
    check_records = [record for record in actual_records if record["path"].startswith("release/checks/apps/")]
    actual_checks = hashlib.sha256(_canonical(check_records)).hexdigest() if check_records else None
    if data["app_checks_digest"] != actual_checks:
        raise ReleaseError("release manifest app-check digest does not match payload")

    with tempfile.TemporaryDirectory(prefix="team-release-verify-") as directory:
        migration_root = Path(directory)
        for path, member_bytes in members.items():
            if not path.startswith("release/migrations/"):
                continue
            relative = path.removeprefix("release/migrations/")
            if not relative or "/" in relative or not relative.endswith(".sql"):
                raise ReleaseError(f"malformed packaged migration path: {path}")
            (migration_root / relative).write_bytes(member_bytes)
        try:
            derived_migrations = _migration_manifest(load_bundles(migration_root))
        except BundleError as exc:
            raise ReleaseError(f"release manifest migration metadata is invalid: {exc}") from exc
    if tuple(migrations) != derived_migrations:
        raise ReleaseError("release manifest migration metadata does not match payload")
    if kind == "schema" and not derived_migrations:
        raise ReleaseError("schema release has no migrations")
    if format_version == 3 and kind == "schema":
        _verify_database_events(data["events"], data["source"], derived_migrations)
        _verify_frontier_member(members.get(_FRONTIER_PATH), data["source"])
    elif _FRONTIER_PATH in members:
        raise ReleaseError("only format 3 schema releases carry a frontier inventory")
    if format_version == 3 and kind == "app" and data["events"] != []:
        raise ReleaseError("format 3 app releases cannot contain schema ledger events")
    if format_version == 3 and kind == "app":
        source = data["source"]
        alias = data["alias"]
        if (
            source["app_tree_digest"] != data["app_tree_digests"].get(alias)
            or source["app_checks_digest"] != data["app_checks_digest"]
            or source["master_contract_digest"] != data["master_contract_digest"]
        ):
            raise ReleaseError("format 3 app source digests do not match the manifest")
    manifest = _manifest_from_data(data, archive, archive_digest)
    _release_app_trees_from_members(manifest, members)
    return manifest


def _frontier_inventory(raw: bytes | None) -> Any:
    from .fingerprints import InventoryError, inventory_from_manifest

    if raw is None:
        raise ReleaseError("format 3 schema release is missing its frontier inventory")
    try:
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, Mapping):
            raise ValueError("not an object")
        return inventory_from_manifest(data)
    except (UnicodeError, ValueError, InventoryError) as exc:
        raise ReleaseError(f"release frontier inventory is malformed: {exc}") from exc


def _verify_frontier_member(raw: bytes | None, source: Mapping[str, Any]) -> None:
    if _frontier_inventory(raw).digest != source["frontier_digest"]:
        raise ReleaseError("release frontier inventory does not match its frontier digest")


def release_frontier_inventory(release_tar: str | Path) -> Any:
    """Return the verified development frontier inventory of a format 3 schema release."""
    archive = Path(release_tar)
    if archive.is_symlink() or not archive.is_file():
        raise ReleaseError("release archive is not a regular file")
    archive_digest, members = _read_archive_bytes(archive)
    manifest = _verify_archive_members(archive, archive_digest, members)
    if manifest.format_version != 3 or manifest.kind != "schema":
        raise ReleaseError("only format 3 schema releases carry a frontier inventory")
    return _frontier_inventory(members.get(_FRONTIER_PATH))


def release_master_contract(release_tar: str | Path) -> Mapping[str, Any] | None:
    """Return the verified master contract packaged in a release, or None when it carries none."""
    archive = Path(release_tar)
    if archive.is_symlink() or not archive.is_file():
        raise ReleaseError("release archive is not a regular file")
    archive_digest, members = _read_archive_bytes(archive)
    _verify_archive_members(archive, archive_digest, members)
    raw = members.get("release/contracts/masters.json")
    if raw is None:
        return None
    try:
        contract = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("packaged master contract is unreadable") from exc
    if not isinstance(contract, Mapping):
        raise ReleaseError("packaged master contract must be an object")
    return contract


def _verify_database_source(source: Any, kind: str) -> None:
    keys = _SCHEMA_SOURCE_KEYS_V3 if kind == "schema" else _APP_SOURCE_KEYS_V3
    if not isinstance(source, Mapping) or set(source) != keys:
        raise ReleaseError("release manifest database source is malformed")
    cut = source["history_cut"]
    if (
        source["kind"] != "dev-database"
        or not isinstance(source["instance_id"], str)
        or not source["instance_id"]
        or type(cut) is not int
        or cut < (1 if kind == "schema" else 0)
        or not isinstance(source["history_digest"], str)
        or not _DIGEST_RE.fullmatch(source["history_digest"])
        or not isinstance(source["frontier_digest"], str)
        or not _DIGEST_RE.fullmatch(source["frontier_digest"])
    ):
        raise ReleaseError("release manifest database source is malformed")
    if kind == "app":
        generation = source["app_generation"]
        if (
            type(generation) is not int
            or generation < 1
            or not isinstance(source["app_tree_digest"], str)
            or not _DIGEST_RE.fullmatch(source["app_tree_digest"])
            or any(
                value is not None and (not isinstance(value, str) or not _DIGEST_RE.fullmatch(value))
                for value in (source["app_checks_digest"], source["master_contract_digest"])
            )
        ):
            raise ReleaseError("release manifest application source is malformed")


def _verify_database_events(events: Any, source: Mapping[str, Any], migrations: tuple[dict[str, Any], ...]) -> None:
    """The archived ledger must be contiguous, well-ordered and carried by the packaged bundles."""
    if not isinstance(events, list) or not events:
        raise ReleaseError("format 3 schema release has no ledger events")
    packaged = {item["id"]: item for item in migrations}
    status: dict[str, str] = {}
    for index, event in enumerate(events, start=1):
        if not isinstance(event, Mapping) or set(event) != _EVENT_KEYS_V3:
            raise ReleaseError("release ledger event is malformed")
        if event["sequence"] != index:
            raise ReleaseError("release ledger events are not contiguous from sequence 1")
        migration_id, operation = event["id"], event["operation"]
        bundle = packaged.get(migration_id)
        if bundle is None or bundle["checksum"] != event["checksum"]:
            raise ReleaseError(f"release ledger event is not carried by a packaged bundle: {migration_id}")
        prior = status.get(migration_id, "")
        if operation == "up" and prior != "APPLIED":
            status[migration_id] = "APPLIED"
        elif operation == "down" and prior == "APPLIED" and bundle["reversible"]:
            status[migration_id] = "REVERTED"
        else:
            raise ReleaseError(f"release ledger event order is invalid: {migration_id} {operation}")
    if set(status) != set(packaged):
        raise ReleaseError("release carries migration bundles that no ledger event uses")
    if source["history_cut"] != len(events):
        raise ReleaseError("release history cut does not match its ledger events")
    if source["history_digest"] != hashlib.sha256(_canonical(events)).hexdigest():
        raise ReleaseError("release history digest does not match its ledger events")


def build_schema_release_from_database(
    store: Any,
    metadata: Any,
    version: str,
    out: str | Path,
    *,
    sqlcl_build: str,
    built_by: str,
    worker_identity: str,
    host: str,
    expected_frontier: str | None = None,
) -> Manifest:
    """Cut a schema release from the shared development database ledger.

    Everything the ledger records through its latest event ships: every up and
    down event in order, with each migration's stored files. Runs under the
    migration mutex so no migration can land mid-cut, refuses while any attempt
    is unresolved, and binds the version in the release ledger afterwards.
    """
    from .migration_store import release_key

    if getattr(metadata, "environment", None) == "production":
        raise ReleaseError("schema releases are cut from the development database, not production")
    if sqlcl_build != RELEASE_SQLCL_BUILD:
        raise ReleaseError(
            f"release builds require SQLcl build {RELEASE_SQLCL_BUILD}; observed {sqlcl_build}"
        )
    release_key("schema", None, version)
    output = Path(out)
    if output.exists() and any(output.iterdir()):
        raise ReleaseError(f"release output directory must be new: {output}")
    def unresolved_attempts(state: Mapping[str, Any]) -> list[str]:
        return sorted(
            str(attempt_id) for attempt_id, attempt in state.get("attempts", {}).items()
            if isinstance(attempt, Mapping) and attempt.get("state") in {"RUNNING", "FAILED", "UNKNOWN"}
        )

    # Refuse before taking the mutex: an unresolved attempt also blocks the
    # mutex release, so discovering it only after acquiring would strand it.
    unresolved = unresolved_attempts(store.read_state(metadata))
    if unresolved:
        raise ReleaseError("unresolved migration attempts block a release cut: " + ", ".join(unresolved))
    run_token = uuid.uuid4().hex
    store.acquire(metadata, run_token, worker_identity, host)
    try:
        state = store.read_state(metadata)
        unresolved = unresolved_attempts(state)
        if unresolved:
            raise ReleaseError("unresolved migration attempts block a release cut: " + ", ".join(unresolved))
        observations = state.get("observations") or []
        frontier = observations[-1].get("after") if observations and isinstance(observations[-1], Mapping) else None
        if not isinstance(frontier, str) or not _DIGEST_RE.fullmatch(frontier):
            raise ReleaseError("the development database has no accepted schema frontier to release")
        if expected_frontier is not None and frontier != expected_frontier:
            raise ReleaseError("the schema frontier moved after the drift check; a migration landed, cut again")
        raw_events = store.read_events(metadata)
        if not raw_events:
            raise ReleaseError("the development database ledger has no migrations to release")
        events = [
            {"sequence": int(event["sequence"]), "id": str(event["id"]),
             "operation": str(event["operation"]), "checksum": str(event["checksum"])}
            for event in raw_events
        ]
        payload: dict[str, bytes] = {}
        checksums: dict[str, str] = {}
        for event in events:
            migration_id, checksum = event["id"], event["checksum"]
            if checksums.setdefault(migration_id, checksum) != checksum:
                raise ReleaseError(f"ledger records two checksums for one migration: {migration_id}")
        for migration_id, checksum in sorted(checksums.items()):
            try:
                members = store.read_members(metadata, checksum)
            except Exception as exc:
                raise ReleaseError(
                    f"migration files are not stored for {migration_id}; the developer who holds them "
                    "must run adopt-migration-members"
                ) from exc
            for name, data in members.items():
                if not name.startswith(migration_id + "."):
                    raise ReleaseError(f"stored member does not belong to {migration_id}: {name}")
                payload[f"release/migrations/{name}"] = data
        with tempfile.TemporaryDirectory(prefix="team-release-db-bundles-") as directory:
            root = Path(directory)
            for path, data in payload.items():
                (root / path.removeprefix("release/migrations/")).write_bytes(data)
            try:
                migrations = _migration_manifest(load_bundles(root))
            except BundleError as exc:
                raise ReleaseError(f"stored migration files do not form valid bundles: {exc}") from exc
        frontier_manifest = store.read_inventories(metadata).get(frontier)
        if not isinstance(frontier_manifest, Mapping):
            raise ReleaseError("the accepted frontier inventory manifest is missing from the development database")
        payload[_FRONTIER_PATH] = (
            json.dumps(dict(frontier_manifest), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            + b"\n"
        )
        source = {
            "kind": "dev-database",
            "instance_id": str(metadata.instance_id),
            "history_cut": events[-1]["sequence"],
            "history_digest": hashlib.sha256(_canonical(events)).hexdigest(),
            "frontier_digest": frontier,
        }
        manifest = _write_release_archive(
            output,
            payload,
            {
                "format_version": 3,
                "kind": "schema",
                "alias": None,
                "version": version,
                "source": source,
                "events": events,
                "toolchain": {
                    "python": "3.10+", "archive_format": "ustar", "normalizer": "team-v1",
                    "sqlcl": sqlcl_build,
                },
                "migrations": list(migrations),
                "required_migrations": [],
                "app_tree_digests": {},
                "master_contract_digest": None,
            },
        )
        _record_or_keep(
            manifest,
            lambda: store.record_release(
                metadata, kind="schema", alias=None, version=version,
                archive_digest=manifest.archive_digest, source=source, built_by=built_by, run_token=run_token,
            ),
        )
        return manifest
    finally:
        store.release(metadata, run_token)


def _record_or_keep(manifest: Manifest, record: Callable[[], Any]) -> None:
    """Record a built release; keep the archive when the record may have committed."""
    try:
        record()
    except Exception as exc:
        if result_is_unknown(exc):
            # TEAM_RELEASE may now bind this version to this digest; an app
            # rebuild would not reproduce it, so the archive must survive.
            raise ReleaseError(
                f"release record outcome is unknown; the archive was kept at {manifest.archive_path} "
                f"(sha256 {manifest.archive_digest}). Check TEAM_RELEASE for this version before building it again"
            ) from exc
        manifest.archive_path.unlink(missing_ok=True)
        if manifest.staging_dir is not None:
            shutil.rmtree(manifest.staging_dir, ignore_errors=True)
        raise ReleaseError(str(exc)) from exc


def _read_app_check_assets(repo: Path, alias: str) -> dict[str, bytes]:
    """Read only the selected app's checked-in release checks, rejecting links."""
    if (repo / "ci").is_symlink():
        raise ReleaseError("application check parent cannot be a symlink")
    root = repo / "ci" / "app-checks"
    if not root.exists():
        return {}
    if root.is_symlink() or not root.is_dir():
        raise ReleaseError("application check directory must be a real directory")
    result: dict[str, bytes] = {}
    candidates = (root / f"{alias}.json", root / alias)
    for candidate in candidates:
        if not candidate.exists() and not candidate.is_symlink():
            continue
        if candidate.is_symlink():
            raise ReleaseError(f"application check source cannot be a symlink: {candidate.relative_to(repo)}")
        paths = [candidate] if candidate.is_file() else sorted(candidate.rglob("*"))
        for path in paths:
            if path.is_symlink():
                raise ReleaseError(f"application check source cannot contain a symlink: {path.relative_to(repo)}")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ReleaseError(f"application check source is not a regular file: {path.relative_to(repo)}")
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def _read_master_contract(repo: Path) -> bytes | None:
    if (repo / "targets").is_symlink():
        raise ReleaseError("master contract parent cannot be a symlink")
    path = repo / "targets" / "masters.json"
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink() or not path.is_file():
        raise ReleaseError("master contract must be a regular repository file")
    return path.read_bytes()


def build_app_release_from_database(
    repo: str | Path,
    target: Any,
    metadata: Any,
    app_store: Any,
    migration_store: Any,
    alias: str,
    version: str,
    out: str | Path,
    *,
    sqlcl_build: str,
    checkout_uuid: str,
    built_by: str,
    host: str,
    capture: Callable[..., Any] | None = None,
    page_locks: Callable[..., Any] | None = None,
    capture_runner: Callable[..., Any] | None = None,
    page_lock_runner: Callable[..., Any] | None = None,
    expected_frontier: str | None = None,
) -> Manifest:
    """Build a format-3 app release from two stable paused live captures.

    Both the application mutex and migration mutex are held across the cut.
    The app is only read; the only durable write is the release version record
    in the non-production METADATA store.
    """
    from .apex import capture_app as capture_live_app
    from .migration_store import release_key
    from .page_locks import read_page_locks as read_live_page_locks
    from .trees import tree_digest as digest_tree
    from .app_checks import AppCheckError, build_app_check_bundle

    if not isinstance(alias, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", alias):
        raise ReleaseError("application alias is malformed")
    if getattr(target, "alias", None) != alias or getattr(target, "role", None) != "developer" or getattr(target, "environment", None) != "development":
        raise ReleaseError("application releases require the selected shared development application")
    if getattr(metadata, "environment", None) != "development" or any(
        getattr(metadata, field, None) != getattr(target, field, None) for field in ("instance_id", "db_name", "service")
    ):
        raise ReleaseError("application release metadata must be on the same development database service")
    if sqlcl_build != RELEASE_SQLCL_BUILD:
        raise ReleaseError(f"release builds require SQLcl build {RELEASE_SQLCL_BUILD}; observed {sqlcl_build}")
    if not all(isinstance(value, str) and value.strip() for value in (checkout_uuid, built_by, host)):
        raise ReleaseError("application release requires checkout, actor and host identities")
    release_key("app", alias, version)
    repo_path = Path(repo)
    if not repo_path.is_dir() or repo_path.is_symlink():
        raise ReleaseError("release repository is not a real directory")
    output = Path(out)
    if output.exists() and (output.is_symlink() or not output.is_dir() or any(output.iterdir())):
        raise ReleaseError(f"release output directory must be new: {output}")

    capture_fn = capture or capture_live_app
    lock_fn = page_locks or read_live_page_locks
    app_token = uuid.uuid4().hex
    migration_token = uuid.uuid4().hex
    app_acquired = False
    migration_acquired = False
    capture_work: list[Path] = []
    try:
        try:
            roster = app_store.list_registry(target)
        except Exception as exc:
            raise ReleaseError(f"could not verify registered release checkout: {exc}") from exc
        owner = next((entry for entry in roster if entry.checkout_uuid == checkout_uuid), None)
        if owner is None or owner.host != host or owner.registered_by_user != built_by:
            raise ReleaseError("application release checkout must match a registered checkout, host and user")
        app_store.acquire_app(target.physical_key, app_token, checkout_uuid, host, built_by)
        app_acquired = True
        app_state = app_store.read_app_sync_state(target.physical_key)
        generation = app_state.generation

        report = lock_fn(target, runner=page_lock_runner) if page_lock_runner is not None else lock_fn(target)
        if getattr(report, "status", None) != "KNOWN" or tuple(getattr(report, "pages", ())) != ():
            raise ReleaseError("application release requires a KNOWN empty page-lock report")
        if getattr(report, "alias", None) != alias or getattr(report, "app_id", None) != target.app_id:
            raise ReleaseError("page-lock report identity does not match the selected application")

        try:
            migration_store.acquire(metadata, migration_token, built_by, host)
            migration_acquired = True
            state = migration_store.read_state(metadata)
            unresolved = sorted(
                str(attempt_id) for attempt_id, attempt in state.get("attempts", {}).items()
                if isinstance(attempt, Mapping) and attempt.get("state") in {"RUNNING", "FAILED", "UNKNOWN"}
            )
            if unresolved:
                raise ReleaseError("unresolved migration attempts block an application release: " + ", ".join(unresolved))
            observations = state.get("observations") or []
            frontier = observations[-1].get("after") if observations and isinstance(observations[-1], Mapping) else None
            if not isinstance(frontier, str) or not _DIGEST_RE.fullmatch(frontier):
                raise ReleaseError("the development database has no accepted schema frontier for an application release")
            if expected_frontier is not None and frontier != expected_frontier:
                raise ReleaseError("the schema frontier moved after the drift check; a migration landed, cut again")
            events = migration_store.read_events(metadata)
            canonical_events = []
            current: dict[str, dict[str, Any]] = {}
            for index, event in enumerate(events, start=1):
                sequence = event.get("sequence", event.get("applied_sequence"))
                if type(sequence) is not int or sequence != index:
                    raise ReleaseError("schema history is not contiguous at the application release cut")
                if event.get("operation") not in {"up", "down"}:
                    raise ReleaseError("schema history operation is invalid at the application release cut")
                migration_id = event.get("id", event.get("migration_id"))
                checksum = event.get("checksum")
                if not isinstance(migration_id, str) or not isinstance(checksum, str) or not _DIGEST_RE.fullmatch(checksum):
                    raise ReleaseError("schema history contains an invalid application prerequisite")
                canonical_events.append({
                    "sequence": sequence, "id": migration_id,
                    "operation": event["operation"], "checksum": checksum,
                })
                current[migration_id] = {
                    "id": migration_id, "checksum": checksum,
                    "status": "APPLIED" if event["operation"] == "up" else "REVERTED",
                }
            history_cut = len(canonical_events)
            if observations[-1].get("sequence") != history_cut:
                raise ReleaseError("schema observation frontier is not at the application release history cut")
            required = tuple(
                {"id": value["id"], "checksum": value["checksum"]}
                for value in sorted(current.values(), key=lambda item: item["id"])
                if value["status"] == "APPLIED"
            )
            capture_args = {
                "held_by": app_token, "repo": repo_path,
                "control_store": app_store, "persist": False,
            }
            if capture_runner is not None:
                capture_args["runner"] = capture_runner
            first = capture_fn(target, **capture_args)
            capture_work.append(Path(first.work_dir))
            second = capture_fn(target, **capture_args)
            capture_work.append(Path(second.work_dir))
            if first.tree != second.tree:
                raise ReleaseError("application changed between paused captures; release was discarded")
            if first.before_sync.generation != generation or second.after_sync.generation != generation:
                raise ReleaseError("application generation changed during the release capture")

            report = lock_fn(target, runner=page_lock_runner) if page_lock_runner is not None else lock_fn(target)
            if getattr(report, "status", None) != "KNOWN" or tuple(getattr(report, "pages", ())) != ():
                raise ReleaseError("application release requires a KNOWN empty page-lock report throughout capture")
            if getattr(report, "alias", None) != alias or getattr(report, "app_id", None) != target.app_id:
                raise ReleaseError("page-lock report identity does not match the selected application")
            final_app_state = app_store.read_app_sync_state(target.physical_key)
            if final_app_state.owner_token != app_token or final_app_state.generation != generation or final_app_state.is_uncertain:
                raise ReleaseError("application mutex or generation changed during the release capture")

            check_members = _read_app_check_assets(repo_path, alias)
            if not check_members:
                raise ReleaseError(f"application release requires builder-repository checks for {alias}")
            try:
                check_bundle = build_app_check_bundle(check_members, (alias,))
            except AppCheckError as exc:
                raise ReleaseError(f"builder repository app checks are invalid: {exc}") from exc
            validated_checks = dict(check_bundle.members)
            payload = {
                f"release/apps/{alias}/{path}": data for path, data in first.tree.items()
            }
            payload.update({
                f"release/checks/apps/{path}": data for path, data in validated_checks.items()
            })
            master_bytes = _read_master_contract(repo_path)
            if master_bytes is not None:
                payload["release/contracts/masters.json"] = master_bytes
            tree_digest_value = digest_tree(first.tree)
            app_checks_digest = hashlib.sha256(_canonical([
                {"path": path, "length": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                for path, data in sorted(payload.items())
                if path.startswith("release/checks/apps/")
            ])).hexdigest()
            master_digest = hashlib.sha256(master_bytes).hexdigest() if master_bytes is not None else None
            source = {
                "kind": "dev-database",
                "instance_id": str(metadata.instance_id),
                "history_cut": history_cut,
                "history_digest": hashlib.sha256(_canonical(canonical_events)).hexdigest(),
                "frontier_digest": frontier,
                "app_generation": generation,
                "app_tree_digest": tree_digest_value,
                "app_checks_digest": app_checks_digest,
                "master_contract_digest": master_digest,
            }
            manifest = _write_release_archive(
                output,
                payload,
                {
                    "format_version": 3,
                    "kind": "app",
                    "alias": alias,
                    "version": version,
                    "source": source,
                    "events": [],
                    "toolchain": {
                        "python": "3.10+", "archive_format": "ustar", "normalizer": "team-v1",
                        "sqlcl": sqlcl_build,
                    },
                    "migrations": [],
                    "required_migrations": list(required),
                    "app_tree_digests": {alias: tree_digest_value},
                    "master_contract_digest": master_digest,
                },
            )
            _record_or_keep(
                manifest,
                lambda: migration_store.record_release(
                    metadata, kind="app", alias=alias, version=version,
                    archive_digest=manifest.archive_digest, source=source, built_by=built_by,
                    run_token=migration_token,
                ),
            )
            return manifest
        finally:
            if migration_acquired:
                migration_store.release(metadata, migration_token)
    finally:
        for work_dir in capture_work:
            shutil.rmtree(work_dir, ignore_errors=True)
        if app_acquired:
            app_store.release_app(target.physical_key, app_token, confirmed_success=False)


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


def release_app_check_bundle(release_tar: str | Path) -> AppCheckBundle:
    """Return exact packaged application checks after complete verification."""
    archive = Path(release_tar)
    if archive.is_symlink() or not archive.is_file():
        raise ReleaseError("release archive is not a regular file")
    archive_digest, members = _read_archive_bytes(archive)
    manifest = _verify_archive_members(archive, archive_digest, members)
    raw = {
        path.removeprefix("release/checks/apps/"): data
        for path, data in members.items()
        if path.startswith("release/checks/apps/")
    }
    try:
        bundle = build_app_check_bundle(
            raw, tuple(sorted(manifest.app_tree_digests))
        )
    except AppCheckError as exc:
        raise ReleaseError(str(exc)) from exc
    archive_records = [
        record
        for record in manifest.payload
        if record["path"].startswith("release/checks/apps/")
    ]
    if not archive_records and manifest.app_checks_digest is None:
        actual = None
    else:
        actual = hashlib.sha256(_canonical(archive_records)).hexdigest()
    if manifest.app_checks_digest != actual:
        raise ReleaseError("release app-check bundle does not match manifest")
    return bundle


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
    def member_order(path: str) -> tuple[str, int]:
        if path.endswith(".down.verify.sql"):
            return path[: -len(".down.verify.sql")], 3
        if path.endswith(".down.sql"):
            return path[: -len(".down.sql")], 2
        if path.endswith(".verify.sql"):
            return path[: -len(".verify.sql")], 1
        return path[: -len(".sql")], 0

    return dict(sorted(files.items(), key=lambda item: member_order(item[0])))


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
    if manifest.format_version == 3:
        if manifest.kind == "app":
            target_digest = hashlib.sha256(_canonical(target)).hexdigest()
            artifact_history_digest = hashlib.sha256(_canonical(manifest.required_migrations)).hexdigest()
            history_digest = hashlib.sha256(_canonical(history_data)).hexdigest()
            return ReleasePlan(
                manifest.archive_digest, target_digest, (), artifact_history_digest,
                target, history_digest, (), (), events=(), replay_from=0,
            )
        return _plan_database_release(manifest, history_data, target)
    if getattr(manifest, "kind", None) == "app":
        target_digest = hashlib.sha256(_canonical(target)).hexdigest()
        artifact_history_digest = hashlib.sha256(_canonical(manifest.required_migrations)).hexdigest()
        history_digest = hashlib.sha256(_canonical(history_data)).hexdigest()
        return ReleasePlan(
            manifest.archive_digest, target_digest, (), artifact_history_digest, target, history_digest,
            (), (),
        )
    bundles: dict[str, Migration] = {}
    for item in manifest.migrations:
        if not isinstance(item, Mapping):
            raise ReleaseError("release manifest migration entry must be an object")
        migration_id = item.get("id")
        checksum = item.get("checksum")
        target_name = item.get("target")
        dependencies = item.get("dependencies", [])
        if (
            not isinstance(migration_id, str)
            or not isinstance(checksum, str)
            or not re.fullmatch(r"[0-9a-f]{64}", checksum)
            or target_name not in {"tables", "code"}
            or not isinstance(dependencies, list)
        ):
            raise ReleaseError("release manifest migration entry is malformed")
        try:
            dependency_tuple = tuple((str(edge[0]), str(edge[1])) for edge in dependencies)
        except (IndexError, TypeError, ValueError) as exc:
            raise ReleaseError(f"release manifest migration dependencies are malformed: {migration_id}") from exc
        reversible = bool(item.get("reversible", False))
        bundles[migration_id] = Migration(
            id=migration_id,
            stamp=migration_id.split("__", 1)[0],
            target=target_name,
            checksum=checksum,
            dependencies=dependency_tuple,
            destructive=bool(item.get("destructive", False)),
            down_sql_path=Path(f"{migration_id}.down.sql") if reversible else None,
            down_verify_path=Path(f"{migration_id}.down.verify.sql") if reversible else None,
            down_destructive=bool(item.get("down_destructive", False)),
        )
    role = target.get("role") if isinstance(target, Mapping) else None
    mode = target.get("mode") if isinstance(target, Mapping) else None
    mode = mode if mode in {"shared", "strict"} else ("shared" if role in {"developer", "integration"} else "strict")
    plan = plan_migrations(bundles, history_data, mode)
    if plan.errors:
        raise ReleaseError("; ".join(plan.errors))
    target_digest = hashlib.sha256(_canonical(target)).hexdigest()
    artifact_history_digest = hashlib.sha256(_canonical(manifest.migrations)).hexdigest()
    history_digest = hashlib.sha256(_canonical(history_data)).hexdigest()
    return ReleasePlan(
        manifest.archive_digest, target_digest, plan.pending, artifact_history_digest, target, history_digest,
        plan.foreign_applied, plan.foreign_reverted,
    )


def _plan_database_release(
    manifest: Manifest, history: Mapping[str, Any], target: Mapping[str, Any]
) -> ReleasePlan:
    """Plan the verified event suffix after proving target history is an exact prefix.

    SQL migration history is a collapsed view (latest event per migration).
    Format-3 replay rows keep their local execution sequence and also carry
    the source event sequence plus the target's replay base in ``source_commit``.
    Those markers account for source no-op pairs omitted during replay while
    still proving the target state against the immutable ledger prefix.
    """
    if manifest.kind != "schema" or manifest.source is None or not manifest.events:
        raise ReleaseError("format 3 replay requires a verified database schema ledger")
    event_by_sequence = {int(event["sequence"]): event for event in manifest.events}
    # A marker's ledger digest must name a prefix of this archive's ledger: the
    # release that wrote it was cut from the same development history.
    prefix_cuts = {
        hashlib.sha256(_canonical(list(manifest.events[:cut]))).hexdigest(): cut
        for cut in range(1, len(manifest.events) + 1)
    }
    normalized: dict[str, tuple[str, str, int]] = {}
    replay_markers: list[tuple[int, int]] = []
    cursor = 0
    for migration_id, raw in history.items():
        if not isinstance(migration_id, str) or not isinstance(raw, Mapping):
            raise ReleaseError("target history is malformed and is not an exact archive prefix")
        status = raw.get("status")
        checksum = raw.get("checksum")
        local_sequence = raw.get("sequence", raw.get("applied_sequence"))
        if (
            status not in {"APPLIED", "REVERTED"}
            or not isinstance(checksum, str)
            or type(local_sequence) is not int
            or local_sequence < 1
        ):
            raise ReleaseError("target history is not an exact archive prefix")
        if "sequence" in raw and "applied_sequence" in raw and raw["sequence"] != raw["applied_sequence"]:
            raise ReleaseError("target history is not an exact archive prefix")
        replay_marker = _database_release_replay_marker(raw.get("source_commit"))
        if replay_marker is None:
            sequence = local_sequence
        else:
            ledger_digest, marker_sequence, replay_base = replay_marker
            ledger_cut = prefix_cuts.get(ledger_digest)
            sequence = local_sequence if marker_sequence is None else marker_sequence
            if ledger_cut is None or sequence > ledger_cut:
                raise ReleaseError("target history is not an exact archive prefix")
            if replay_base is not None:
                if replay_base >= sequence:
                    raise ReleaseError("target history is not an exact archive prefix")
                replay_markers.append((sequence, replay_base))
        if sequence > len(manifest.events):
            raise ReleaseError("target history is not an exact archive prefix")
        event = event_by_sequence.get(sequence)
        expected_status = "APPLIED" if event and event.get("operation") == "up" else "REVERTED"
        if (
            event is None
            or event.get("id") != migration_id
            or event.get("checksum") != checksum
            or expected_status != status
        ):
            raise ReleaseError("target history is not an exact archive prefix")
        normalized[migration_id] = (status, checksum, sequence)
        cursor = max(cursor, sequence)

    expected: dict[str, tuple[str, str, int]] = {}
    for event in manifest.events[:cursor]:
        expected[event["id"]] = (
            "APPLIED" if event["operation"] == "up" else "REVERTED",
            event["checksum"],
            event["sequence"],
        )
    if set(normalized) - set(expected):
        raise ReleaseError("target history is not an exact archive prefix")
    for migration_id, source_state in expected.items():
        target_state = normalized.get(migration_id)
        if target_state == source_state:
            continue
        if target_state is not None or source_state[0] != "REVERTED":
            raise ReleaseError("target history is not an exact archive prefix")
        # A release replay can intentionally omit an up/down pair when the
        # migration was absent at the start of that replay and reverted before
        # a later retained event. The later event's source marker proves that
        # the no-op pair was consumed even though target history has no row for
        # it. Other missing REVERTED rows still refuse as non-prefix history.
        down_sequence = int(source_state[2])
        skipped_as_noop = any(
            replay_base < down_sequence < sequence
            and _release_state_at(manifest.events, migration_id, replay_base) is None
            for sequence, replay_base in replay_markers
        )
        if not skipped_as_noop:
            raise ReleaseError("target history is not an exact archive prefix")

    # Opposite transitions cancel only for a migration absent from target
    # history: its up/down pair has no target effect. Existing target rows must
    # replay down/redo transitions even when their final status is unchanged,
    # so each source sequence is consumed and each authored operation runs.
    retained: list[dict[str, Any] | None] = []
    stacks: dict[str, list[int]] = {}
    for event in manifest.events[cursor:]:
        migration_id = str(event["id"])
        index = len(retained)
        retained.append(dict(event))
        stack = stacks.setdefault(migration_id, [])
        if (
            migration_id not in normalized
            and stack
            and retained[stack[-1]] is not None
            and retained[stack[-1]]["operation"] != event["operation"]
        ):
            previous = stack.pop()
            retained[previous] = None
            retained[index] = None
        else:
            stack.append(index)
    replay = tuple(event for event in retained if event is not None)
    pending = tuple(dict.fromkeys(str(event["id"]) for event in replay))
    target_digest = hashlib.sha256(_canonical(target)).hexdigest()
    artifact_history_digest = hashlib.sha256(_canonical(list(manifest.events))).hexdigest()
    history_digest = hashlib.sha256(_canonical(history)).hexdigest()
    return ReleasePlan(
        archive_digest=manifest.archive_digest,
        target_digest=target_digest,
        pending=pending,
        artifact_history_digest=artifact_history_digest,
        target=target,
        history_digest=history_digest,
        events=replay,
        replay_from=cursor,
    )


def _database_release_replay_marker(source_commit: Any) -> tuple[str, int | None, int | None] | None:
    """Read the ledger digest, source event sequence and replay base from a format-3 history row.

    Early format-3 replay rows recorded only the ledger digest and used local
    sequence numbers; their sequence and replay base are None, but the digest
    is still returned so it can be bound to the archive ledger.
    """
    if not isinstance(source_commit, str) or not source_commit.startswith("db-release:"):
        return None
    match = _DB_RELEASE_REPLAY_SOURCE_RE.fullmatch(source_commit)
    if match is not None:
        return match.group(1), int(match.group(2)), int(match.group(3))
    if _DB_RELEASE_SOURCE_RE.fullmatch(source_commit):
        return source_commit[len("db-release:"):], None, None
    raise ReleaseError("target history is not an exact archive prefix")


def _release_state_at(events: tuple[Mapping[str, Any], ...], migration_id: str, sequence: int) -> str | None:
    state = None
    for event in events:
        if int(event["sequence"]) > sequence:
            break
        if event["id"] == migration_id:
            state = "APPLIED" if event["operation"] == "up" else "REVERTED"
    return state


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
    target_state_key: str | None = None,
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
    if (
        current.history_digest != plan.history_digest
        or current.pending != plan.pending
        or current.target_digest != plan.target_digest
        or current.events != plan.events
        or current.replay_from != plan.replay_from
    ):
        raise ReleaseError("target history or pending release work changed after plan generation")
    if getattr(manifest, "kind", None) == "app":
        history_data = history.get("history", history) if isinstance(history, Mapping) else {}
        for item in manifest.required_migrations:
            migration_id = item["id"]
            row = history_data.get(migration_id)
            if row is None or row.get("status") != "APPLIED" or row.get("checksum") != item["checksum"]:
                raise ReleaseError(f"required migration unavailable: {migration_id}")
    if apply_migrations is None and deploy_application is None:
        return ApplyReport(
            "planned", plan.pending, manifest.archive_digest,
            source_commit=manifest.source_commit,
            target_state_key=target_state_key or (str(target.get("state_key", "")) if isinstance(target, Mapping) else ""),
            target_digest=plan.target_digest,
            history_digest=plan.history_digest,
            source=manifest.source,
        )
    pending = tuple(item for item in manifest.migrations if item.get("id") in plan.pending)
    if pending and apply_migrations is None:
        raise ReleaseError("release contains pending migrations but no non-production migration adapter was supplied")
    apps = _release_app_trees_from_members(manifest, members)
    if apps and deploy_application is None:
        raise ReleaseError("release contains applications but no non-production deployment adapter was supplied")
    if apply_migrations is not None and pending:
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
    return ApplyReport(
        "applied", plan.pending, manifest.archive_digest,
        source_commit=manifest.source_commit,
        target_state_key=target_state_key or (str(target.get("state_key", "")) if isinstance(target, Mapping) else ""),
        target_digest=plan.target_digest,
        history_digest=plan.history_digest,
        source=manifest.source,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="release")
    sub = parser.add_subparsers(dest="command", required=True)
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
    apply_parser.add_argument("--env", default=None)
    args = parser.parse_args(list(argv or []))
    if args.command == "verify-release":
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
            "events": [dict(event) for event in result.events],
            "replay_from": result.replay_from,
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
            events=tuple(plan_raw.get("events", ())), replay_from=plan_raw.get("replay_from", 0),
        )
        if getattr(args, "env", None):
            from .release_adapter import apply_verified_release
            report = apply_verified_release(args.archive, args.target, args.env, plan, history)
        else:
            report = apply_release(args.archive, target, plan, history=history)
        print(json.dumps({"status": report.status, "pending": report.pending, "archive_digest": report.archive_digest}, sort_keys=True))
    return 0
