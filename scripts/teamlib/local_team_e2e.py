"""Safety-first local three-developer APEX acceptance harness primitives.

The live orchestration is intentionally built on top of the repository's
qualified adapters.  This module starts with the durable run contract and
strict subprocess boundary; later phases add database and Git adapters without
changing those safety invariants.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence
import uuid


class E2EError(RuntimeError):
    """Raised when the local acceptance harness cannot prove a safe action."""


_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{8}$")
_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_ORACLE_RE = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SHELL_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f;&|<>`$]")
_PHASES = (
    "preflight",
    "provision",
    "topology",
    "scenario",
    "convergence",
    "cleanup",
)
_STATUSES = {"PENDING", "RUNNING", "PASS", "FAIL", "UNKNOWN", "SKIPPED"}
_SENSITIVE_KEY_RE = re.compile(r"(?:pass(word)?|secret|token|credential|private|access[_-]?key)", re.IGNORECASE)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONNECTION_TREE_RE = re.compile(r"(?:├──|└──)\s+(.+?)\s*$")
_CONNECTION_PASSWORD_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or _CONTROL_RE.search(value):
        raise E2EError(f"{label} must be a non-empty control-free string")
    return value


def _safe_mapping(value: Mapping[str, Any], *, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or not value:
        raise E2EError(f"{label} must be a non-empty mapping")
    result: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = _safe_text(raw_key, label=f"{label} key")
        if _SENSITIVE_KEY_RE.search(key):
            raise E2EError(f"{label} must not contain sensitive key {key}")
        result[key] = _safe_text(raw_value, label=f"{label}.{key}")
    return dict(sorted(result.items()))


def parse_saved_connections(output: str) -> set[str]:
    """Parse SQLcl ``connmgr list`` tree output after removing terminal color."""

    if not isinstance(output, str):
        raise E2EError("SQLcl connection-manager output must be text")
    clean = _ANSI_ESCAPE_RE.sub("", output)
    names: set[str] = set()
    for line in clean.splitlines():
        match = _CONNECTION_TREE_RE.search(line)
        if not match:
            continue
        name = match.group(1).strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            names.add(name)
    return names


def save_connection_script(name: str, schema: str, password: str) -> str:
    """Build the transient SQLcl save script without a prompt-only password line."""

    _safe_text(name, label="saved connection name")
    if not name.startswith("docker-team-e2e-meta-") or _CONTROL_RE.search(name):
        raise E2EError("metadata saved connection name is outside fixture namespace")
    if not _ORACLE_RE.fullmatch(schema):
        raise E2EError("metadata schema is not a safe Oracle identifier")
    if not _CONNECTION_PASSWORD_RE.fullmatch(password):
        raise E2EError("metadata password is not a safe generated connection secret")
    return (
        "SET DEFINE OFF\n"
        f"CONNECT -SAVE {name} -SAVEPWD {schema}/{password}@//127.0.0.1:1521/FREEPDB1\n"
        "EXIT\n"
    )


@dataclass(frozen=True)
class FixtureSpec:
    """Immutable names for the one disposable Docker/APEX fixture."""

    seed_app_id: int = 103
    fixture_app_id: int = 9099
    tracked_alias: str = "team-e2e"
    apex_alias: str = "TEAM-E2E-9099"
    project_id: str = "local-team-e2e"
    metadata_schema: str = "TEAM_E2E_META"

    def __post_init__(self) -> None:
        if not isinstance(self.seed_app_id, int) or isinstance(self.seed_app_id, bool) or self.seed_app_id <= 0:
            raise E2EError("seed_app_id must be a positive integer")
        if not isinstance(self.fixture_app_id, int) or isinstance(self.fixture_app_id, bool) or self.fixture_app_id <= 0:
            raise E2EError("fixture_app_id must be a positive integer")
        if self.seed_app_id == self.fixture_app_id:
            raise E2EError("seed_app_id and fixture_app_id must differ")
        if self.tracked_alias != "team-e2e" or not _ALIAS_RE.fullmatch(self.tracked_alias):
            raise E2EError("tracked_alias must be the reserved team-e2e alias")
        if self.apex_alias != "TEAM-E2E-9099" or _CONTROL_RE.search(self.apex_alias):
            raise E2EError("apex_alias must be the reserved TEAM-E2E-9099 alias")
        if self.project_id != "local-team-e2e" or _CONTROL_RE.search(self.project_id):
            raise E2EError("project_id must be the reserved local-team-e2e project")
        if not _ORACLE_RE.fullmatch(self.metadata_schema) or self.metadata_schema == "DEMO":
            raise E2EError("metadata_schema must be a distinct uppercase Oracle identifier")


@dataclass(frozen=True)
class CleanupTargets:
    application_id: int
    application_alias: str
    metadata_schema: str
    schemas: tuple[str, ...]
    saved_connection: str
    run_root: Path


@dataclass(frozen=True)
class PreflightEvidence:
    """Read-only facts that must remain true before fixture writes."""

    spec: FixtureSpec
    payload_connection: str
    admin_connection: str
    payload_identity: dict[str, str]
    admin_identity: dict[str, str]
    workspace_id: int
    workspace_name: str
    apps: dict[int, dict[str, Any]]
    schemas: frozenset[str]
    saved_connections: frozenset[str]
    saved_connection: str
    seed_app_id: int
    seed_alias: str
    seed_parsing_schema: str
    seed_tree_path: Path
    seed_tree_digest: str
    run_root: Path


@dataclass(frozen=True)
class ProvisionedFixture:
    metadata_connection: str
    metadata_schema: str
    application_id: int
    application_alias: str
    workspace_id: int
    application_tree_digest: str
    marker: str


@dataclass(frozen=True)
class CleanupReport:
    status: str
    removed: tuple[str, ...]
    verified_absent: tuple[str, ...]
    retained_evidence: Path


def _physical_identity(identity: Mapping[str, Any]) -> tuple[str, str, str]:
    required = ("DB_NAME", "SERVICE", "INSTANCE_ID")
    try:
        values = tuple(_safe_text(identity[key], label=f"identity.{key}") for key in required)
    except KeyError as exc:
        raise E2EError(f"identity is missing {exc.args[0]}") from exc
    return values  # type: ignore[return-value]


def _connection_name_for_run(run_root: Path, explicit: str | None = None) -> str:
    if explicit is not None:
        _safe_text(explicit, label="metadata connection")
        if not explicit.startswith("docker-team-e2e-meta-"):
            raise E2EError("metadata connection is outside the fixture namespace")
        return explicit
    manifest_path = run_root / "manifest.json"
    if manifest_path.is_file():
        return RunManifest.load(run_root).cleanup_targets().saved_connection
    raise E2EError("a manifest or explicit fixture metadata connection is required")


def _validate_apps(
    apps: Mapping[Any, Any],
    spec: FixtureSpec,
    *,
    reject_fixture: bool = True,
) -> dict[int, dict[str, Any]]:
    if not isinstance(apps, Mapping):
        raise E2EError("application inventory is not a mapping")
    normalized: dict[int, dict[str, Any]] = {}
    aliases: dict[str, int] = {}
    for raw_id, raw_app in apps.items():
        try:
            app_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise E2EError("application inventory contains a non-numeric ID") from exc
        if not isinstance(raw_app, Mapping):
            raise E2EError(f"application {app_id} metadata is malformed")
        try:
            alias = _safe_text(raw_app["alias"], label=f"application {app_id} alias")
            workspace_id = int(raw_app["workspace_id"])
            parsing_schema = _safe_text(raw_app["parsing_schema"], label=f"application {app_id} parsing schema")
        except (KeyError, TypeError, ValueError) as exc:
            raise E2EError(f"application {app_id} metadata is incomplete") from exc
        key = alias.casefold()
        if key in aliases and aliases[key] != app_id:
            raise E2EError(f"application alias is duplicated: {alias}")
        aliases[key] = app_id
        normalized[app_id] = {
            "id": app_id,
            "alias": alias,
            "workspace_id": workspace_id,
            "parsing_schema": parsing_schema,
        }
    if reject_fixture and spec.fixture_app_id in normalized:
        raise E2EError(f"fixture application ID already exists: {spec.fixture_app_id}")
    if reject_fixture and spec.apex_alias.casefold() in aliases:
        raise E2EError(f"fixture application alias already exists: {spec.apex_alias}")
    return normalized


def inspect_fixture(
    payload_connection: str,
    admin_connection: str,
    spec: FixtureSpec,
    *,
    adapter: Any | None = None,
    run_root: str | Path | None = None,
    saved_connection: str | None = None,
) -> PreflightEvidence:
    """Perform all read-only checks before the first fixture write."""

    root = Path(run_root or Path.cwd()).resolve()
    if not root.is_dir() or root.is_symlink():
        raise E2EError("fixture preflight run root must be a real directory")
    adapter = adapter or SqlclFixtureAdapter(payload_connection, admin_connection, root)
    if getattr(adapter, "environment", "development") == "production":
        raise E2EError("fixture preflight refuses production environment")
    payload_identity = dict(adapter.read_identity(payload_connection))
    admin_identity = dict(adapter.read_identity(admin_connection))
    if _physical_identity(payload_identity) != _physical_identity(admin_identity):
        raise E2EError("payload and admin sessions do not share the same physical database identity")
    workspace, raw_apps = adapter.read_workspace_and_apps(payload_connection)
    if not isinstance(workspace, Mapping):
        raise E2EError("APEX workspace metadata is missing")
    try:
        workspace_id = int(workspace["id"])
        workspace_name = _safe_text(workspace["name"], label="workspace name")
    except (KeyError, TypeError, ValueError) as exc:
        raise E2EError("APEX workspace ID/name is missing") from exc
    if workspace_id <= 0:
        raise E2EError("APEX workspace ID is missing")
    apps = _validate_apps(raw_apps, spec)
    if spec.seed_app_id not in apps:
        raise E2EError(f"seed application is missing: {spec.seed_app_id}")
    seed = apps[spec.seed_app_id]
    if seed["workspace_id"] != workspace_id:
        raise E2EError("seed application is not in the observed workspace")
    schemas = frozenset(str(value) for value in adapter.read_schemas_and_users(admin_connection))
    if spec.metadata_schema in schemas:
        raise E2EError(f"fixture metadata schema already exists: {spec.metadata_schema}")
    connection_name = _connection_name_for_run(root, saved_connection)
    saved = frozenset(str(value) for value in adapter.read_saved_connections())
    if connection_name in saved:
        raise E2EError(f"fixture metadata connection already exists: {connection_name}")
    captured_seed_path, seed_digest = adapter.capture_seed(payload_connection, spec.seed_app_id, root / "seed")
    raw_seed_path = Path(captured_seed_path)
    if raw_seed_path.is_symlink():
        raise E2EError("seed capture must not be a symbolic link")
    seed_path = raw_seed_path.resolve()
    application_file = seed_path / "application.apx"
    if (
        not _inside(seed_path, root)
        or not seed_path.is_dir()
        or seed_path.is_symlink()
        or application_file.is_symlink()
        or not application_file.is_file()
    ):
        raise E2EError("seed capture escaped the run root or is incomplete")
    return PreflightEvidence(
        spec=spec,
        payload_connection=payload_connection,
        admin_connection=admin_connection,
        payload_identity=payload_identity,
        admin_identity=admin_identity,
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        apps=apps,
        schemas=schemas,
        saved_connections=saved,
        saved_connection=connection_name,
        seed_app_id=spec.seed_app_id,
        seed_alias=seed["alias"],
        seed_parsing_schema=seed["parsing_schema"],
        seed_tree_path=seed_path,
        seed_tree_digest=seed_digest,
        run_root=root,
    )


def _json_digest(value: Mapping[str, Any]) -> str:
    return _sha256_bytes(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def provision_fixture(
    evidence: PreflightEvidence,
    manifest: RunManifest,
    *,
    adapter: Any | None = None,
    password: str | None = None,
) -> ProvisionedFixture:
    """Create only the run-owned schema, connection, and cloned application."""

    if evidence.spec != manifest.spec or evidence.run_root != manifest.run_root.resolve():
        raise E2EError("preflight evidence is not bound to this manifest")
    if _physical_identity(evidence.payload_identity) != _physical_identity(manifest.expected_identity):
        raise E2EError("manifest identity does not match preflight identity")
    adapter = adapter or SqlclFixtureAdapter(evidence.payload_connection, evidence.admin_connection, evidence.run_root)
    secret = password or secrets.token_urlsafe(24)
    if not secret or _CONTROL_RE.search(secret):
        raise E2EError("generated metadata password is invalid")
    adapter.create_metadata_user(evidence.admin_connection, manifest.spec.metadata_schema, secret)
    adapter.save_metadata_connection(
        evidence.admin_connection,
        manifest.cleanup_targets().saved_connection,
        manifest.spec.metadata_schema,
        secret,
    )
    adapter.clone_seed_to_fixture(
        evidence.payload_connection,
        evidence.seed_tree_path,
        manifest.spec,
        evidence.workspace_id,
    )
    observed = dict(adapter.verify_fixture_export(evidence.payload_connection, manifest.spec))
    if int(observed.get("id", -1)) != manifest.spec.fixture_app_id:
        raise E2EError("fixture export has the wrong application ID")
    if str(observed.get("alias", "")).casefold() != manifest.spec.apex_alias.casefold():
        raise E2EError("fixture export has the wrong application alias")
    if int(observed.get("workspace_id", -1)) != evidence.workspace_id:
        raise E2EError("fixture export has the wrong workspace")
    app_digest = str(observed.get("tree_digest") or _json_digest(observed))
    marker = f"{manifest.run_id}:{manifest.spec.fixture_app_id}:{app_digest}"
    metadata_connection = manifest.cleanup_targets().saved_connection
    adapter.record_marker(metadata_connection, manifest.spec.metadata_schema, manifest.run_id, app_digest)
    manifest.transition(
        "provision",
        "PASS",
        {
            "application_id": manifest.spec.fixture_app_id,
            "application_alias": manifest.spec.apex_alias,
            "metadata_schema": manifest.spec.metadata_schema,
            "metadata_connection": metadata_connection,
            "application_tree_digest": app_digest,
            "marker": marker,
        },
    )
    return ProvisionedFixture(
        metadata_connection=metadata_connection,
        metadata_schema=manifest.spec.metadata_schema,
        application_id=manifest.spec.fixture_app_id,
        application_alias=manifest.spec.apex_alias,
        workspace_id=evidence.workspace_id,
        application_tree_digest=app_digest,
        marker=marker,
    )


def cleanup_fixture(
    manifest: RunManifest,
    live_evidence: PreflightEvidence,
    *,
    adapter: Any | None = None,
) -> CleanupReport:
    """Remove only resources proven to belong to this manifest."""

    if live_evidence.spec != manifest.spec or live_evidence.run_root != manifest.run_root.resolve():
        raise E2EError("cleanup evidence is not bound to this manifest")
    adapter = adapter or SqlclFixtureAdapter(live_evidence.payload_connection, live_evidence.admin_connection, manifest.run_root)
    payload_identity = dict(adapter.read_identity(live_evidence.payload_connection))
    admin_identity = dict(adapter.read_identity(live_evidence.admin_connection))
    if _physical_identity(payload_identity) != _physical_identity(admin_identity):
        raise E2EError("cleanup identity is inconsistent")
    if _physical_identity(payload_identity) != _physical_identity(live_evidence.payload_identity):
        raise E2EError("cleanup identity differs from preflight identity")
    workspace, raw_apps = adapter.read_workspace_and_apps(live_evidence.payload_connection)
    apps = _validate_apps(raw_apps, manifest.spec, reject_fixture=False)
    fixture = apps.get(manifest.spec.fixture_app_id)
    if fixture is None or fixture["alias"].casefold() != manifest.spec.apex_alias.casefold():
        raise E2EError("cleanup fixture application identity does not match manifest")
    if int(workspace.get("id", -1)) != live_evidence.workspace_id or str(workspace.get("name")) != live_evidence.workspace_name:
        raise E2EError("cleanup workspace identity does not match manifest")
    marker_verifier = getattr(adapter, "verify_marker", None)
    if marker_verifier is None or not marker_verifier(
        manifest.cleanup_targets().saved_connection,
        manifest.spec.metadata_schema,
        manifest.run_id,
        manifest.spec.fixture_app_id,
    ):
        raise E2EError("cleanup metadata marker is missing or does not match manifest")
    targets = manifest.cleanup_targets()
    adapter.remove_fixture_app(live_evidence.admin_connection, manifest.spec, live_evidence.workspace_name)
    adapter.drop_metadata_user(live_evidence.admin_connection, manifest.spec.metadata_schema, manifest.run_id)
    adapter.delete_saved_connection(targets.saved_connection)
    for path in (manifest.run_root / "dev", manifest.run_root / "remote.git"):
        if path.exists():
            if path.is_symlink() or not _inside(path.resolve(), manifest.run_root.resolve()):
                raise E2EError("cleanup path escaped the manifest run root")
            import shutil

            shutil.rmtree(path)
    workspace_after, raw_apps_after = adapter.read_workspace_and_apps(live_evidence.payload_connection)
    apps_after = _validate_apps(raw_apps_after, manifest.spec)
    if manifest.spec.fixture_app_id in apps_after or manifest.spec.metadata_schema in adapter.read_schemas_and_users(live_evidence.admin_connection):
        raise E2EError("cleanup verification found an owned resource still present")
    if manifest.spec.seed_app_id not in apps_after or "DEMO" not in adapter.read_schemas_and_users(live_evidence.admin_connection):
        raise E2EError("cleanup verification cannot prove unrelated resources survived")
    if targets.saved_connection in adapter.read_saved_connections():
        raise E2EError("cleanup verification found saved connection still present")
    report = CleanupReport(
        status="PASS",
        removed=(f"application:{manifest.spec.fixture_app_id}", f"schema:{manifest.spec.metadata_schema}", f"connection:{targets.saved_connection}"),
        verified_absent=(f"application:{manifest.spec.fixture_app_id}", f"schema:{manifest.spec.metadata_schema}", f"connection:{targets.saved_connection}"),
        retained_evidence=manifest.run_root,
    )
    manifest.transition("cleanup", "PASS", {"removed": list(report.removed), "verified_absent": list(report.verified_absent)})
    return report


class SqlclFixtureAdapter:
    """Qualified SQLcl adapter for the disposable Docker fixture.

    The first identity probe is deliberately a read-only bootstrap because a
    target contract cannot be constructed until SQLcl tells us its physical
    identity. Every subsequent database operation uses ``run_sqlcl`` with the
    observed identity bound into a ``Target``.
    """

    environment = "development"

    def __init__(self, payload_connection: str, admin_connection: str, run_root: Path):
        from .config import Target
        from .sqlcl import run_sqlcl

        self.payload_connection = _safe_text(payload_connection, label="payload connection")
        self.admin_connection = _safe_text(admin_connection, label="admin connection")
        self.run_root = run_root.resolve()
        self._run_sqlcl = run_sqlcl
        self._target_type = Target
        self._sql_executable = os.environ.get("TEAM_SQLCL_EXECUTABLE", "sql")
        self.payload_identity = self._bootstrap_identity(self.payload_connection, "payload")
        self.admin_identity = self._bootstrap_identity(self.admin_connection, "admin")
        self.payload_target = self._target(
            self.payload_connection,
            self.payload_identity,
            alias="fixture",
            workspace_id=1,
            app_id=1,
            parsing_schema="DEMO",
        )
        self.admin_target = self._target(
            self.admin_connection,
            self.admin_identity,
            alias=None,
            workspace_id=None,
            app_id=None,
            parsing_schema=None,
        )
        self.metadata_target = None
        self.workspace_id: int | None = None

    def _bootstrap_identity(self, connection: str, label: str) -> dict[str, str]:
        directory = self.run_root / "adapter" / "identity"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        driver = directory / f"{label}.sql"
        driver.write_text(
            "SET DEFINE OFF\n"
            "SET HEADING OFF\n"
            "SET FEEDBACK OFF\n"
            "SET LINESIZE 32767\n"
            "SET PAGESIZE 0\n"
            "WHENEVER SQLERROR EXIT SQL.SQLCODE\n"
            "SELECT 'TEAM_IDENTITY|' ||\n"
            "       'SESSION_USER=' || REPLACE(SYS_CONTEXT('USERENV','SESSION_USER'),'|','/') ||\n"
            "       '|CURRENT_SCHEMA=' || REPLACE(SYS_CONTEXT('USERENV','CURRENT_SCHEMA'),'|','/') ||\n"
            "       '|DB_NAME=' || REPLACE(SYS_CONTEXT('USERENV','DB_NAME'),'|','/') ||\n"
            "       '|SERVICE=' || REPLACE(SYS_CONTEXT('USERENV','SERVICE_NAME'),'|','/') ||\n"
            "       '|INSTANCE_ID=' || REPLACE(SYS_CONTEXT('USERENV','INSTANCE_NAME'),'|','/') || '@' ||\n"
            "       REPLACE(SYS_CONTEXT('USERENV','SERVER_HOST'),'|','/')\n"
            "FROM dual;\n"
            "SELECT 'TEAM_IDENTITY|' ||\n"
            "       'SESSION_USER=' || REPLACE(SYS_CONTEXT('USERENV','SESSION_USER'),'|','/') ||\n"
            "       '|CURRENT_SCHEMA=' || REPLACE(SYS_CONTEXT('USERENV','CURRENT_SCHEMA'),'|','/') ||\n"
            "       '|DB_NAME=' || REPLACE(SYS_CONTEXT('USERENV','DB_NAME'),'|','/') ||\n"
            "       '|SERVICE=' || REPLACE(SYS_CONTEXT('USERENV','SERVICE_NAME'),'|','/') ||\n"
            "       '|INSTANCE_ID=' || REPLACE(SYS_CONTEXT('USERENV','INSTANCE_NAME'),'|','/') || '@' ||\n"
            "       REPLACE(SYS_CONTEXT('USERENV','SERVER_HOST'),'|','/')\n"
            "FROM dual;\nEXIT\n",
            encoding="utf-8",
            newline="",
        )
        result = run_command(
            [self._sql_executable, "-S", "-noupdates", "-name", connection, f"@{driver}"],
            cwd=self.run_root,
            run_root=self.run_root,
        )
        if result.returncode != 0:
            raise E2EError(f"{label} identity probe failed; see {result.stderr_path}")
        lines = [line.strip() for line in result.stdout_path.read_text(encoding="utf-8").splitlines() if line.strip().startswith("TEAM_IDENTITY|")]
        if len(lines) != 2 or lines[0] != lines[1]:
            raise E2EError(f"{label} identity probe did not produce two stable observations")
        fields: dict[str, str] = {}
        for part in lines[0].split("|")[1:]:
            if "=" not in part:
                raise E2EError(f"{label} identity marker is malformed")
            key, value = part.split("=", 1)
            if key in fields or key not in {"SESSION_USER", "CURRENT_SCHEMA", "DB_NAME", "SERVICE", "INSTANCE_ID"}:
                raise E2EError(f"{label} identity marker is malformed")
            fields[key] = _safe_text(value, label=f"{label} identity {key}")
        if set(fields) != {"SESSION_USER", "CURRENT_SCHEMA", "DB_NAME", "SERVICE", "INSTANCE_ID"}:
            raise E2EError(f"{label} identity marker is incomplete")
        return fields

    def _target(
        self,
        connection: str,
        identity: Mapping[str, str],
        *,
        alias: str | None,
        workspace_id: int | None,
        app_id: int | None,
        parsing_schema: str | None,
    ):
        return self._target_type(
            project="local-team-e2e",
            role="developer",
            environment="development",
            connection=connection,
            instance_id=identity["INSTANCE_ID"],
            db_name=identity["DB_NAME"],
            service=identity["SERVICE"],
            session_user=identity["SESSION_USER"],
            current_schema=identity["CURRENT_SCHEMA"],
            alias=alias,
            workspace_id=workspace_id,
            app_id=app_id,
            parsing_schema=parsing_schema,
            ownership_mode="shared",
            binding_digest="0" * 64,
        )

    def _driver(self, name: str, payload: str) -> tuple[Path, Path]:
        work = self.run_root / "adapter" / name
        work.mkdir(mode=0o700, parents=True, exist_ok=True)
        driver = work / "payload.sql"
        driver.write_text(payload, encoding="utf-8", newline="")
        return driver, work

    @staticmethod
    def _rows(stdout: str, prefix: str) -> list[list[str]]:
        return [line.strip().split("|")[1:] for line in stdout.splitlines() if line.strip().startswith(prefix)]

    def _read_payload(self, target: Any, name: str, payload: str) -> str:
        from .sqlcl import SqlclError

        driver, work = self._driver(name, payload)
        try:
            result = self._run_sqlcl(target, "read", driver, work)
        except Exception as exc:
            if isinstance(exc, SqlclError):
                raise E2EError(f"qualified SQLcl read failed for {name}: {exc}") from exc
            raise E2EError(f"qualified SQLcl read failed for {name}: {exc}") from exc
        return str(result.stdout)

    def _write_payload(self, target: Any, name: str, payload: str) -> str:
        driver, work = self._driver(name, payload)
        try:
            result = self._run_sqlcl(target, "write", driver, work)
        except Exception as exc:
            raise E2EError(f"qualified SQLcl write failed for {name}: {exc}") from exc
        return str(result.stdout)

    def read_identity(self, connection: str) -> dict[str, str]:
        if connection == self.payload_connection:
            return dict(self.payload_identity)
        if connection == self.admin_connection:
            return dict(self.admin_identity)
        if connection.startswith("docker-team-e2e-meta-"):
            return dict(self._bootstrap_identity(connection, "metadata-refresh"))
        raise E2EError(f"unregistered fixture connection: {connection}")

    def _ensure_metadata_target(self, connection: str):
        if not connection.startswith("docker-team-e2e-meta-"):
            raise E2EError(f"unregistered metadata connection: {connection}")
        if self.metadata_target is None or self.metadata_target.connection != connection:
            identity = self._bootstrap_identity(connection, "metadata")
            self.metadata_target = self._target(
                connection,
                identity,
                alias=None,
                workspace_id=None,
                app_id=None,
                parsing_schema=None,
            )
        return self.metadata_target

    def read_workspace_and_apps(self, connection: str) -> tuple[dict[str, object], dict[int, dict[str, object]]]:
        stdout = self._read_payload(
            self.payload_target,
            "workspace-apps",
            "SET DEFINE OFF\n"
            "SET HEADING OFF\nSET FEEDBACK OFF\n"
            "SELECT 'TEAM_APP|' || application_id || '|' || REPLACE(alias,'|','/') || '|' || workspace_id || '|' || REPLACE(owner,'|','/') FROM apex_applications;\n"
            "SELECT 'TEAM_WORKSPACE|' || workspace_id || '|' || REPLACE(workspace,'|','/') FROM apex_workspaces;\n",
        )
        apps: dict[int, dict[str, object]] = {}
        for row in self._rows(stdout, "TEAM_APP|"):
            if len(row) != 4:
                raise E2EError("APEX application inventory row is malformed")
            app_id = int(row[0])
            apps[app_id] = {"id": app_id, "alias": row[1], "workspace_id": int(row[2]), "parsing_schema": row[3]}
        workspaces = self._rows(stdout, "TEAM_WORKSPACE|")
        if not apps or not workspaces:
            raise E2EError("APEX application/workspace inventory is empty")
        workspace_by_id = {int(row[0]): row[1] for row in workspaces if len(row) == 2}
        first_workspace = next(iter(apps.values()))["workspace_id"]
        if first_workspace not in workspace_by_id:
            raise E2EError("APEX workspace inventory does not contain application workspace")
        self.workspace_id = int(first_workspace)
        return {"id": first_workspace, "name": workspace_by_id[first_workspace]}, apps

    def read_schemas_and_users(self, connection: str) -> set[str]:
        target = self.admin_target if connection == self.admin_connection else self._ensure_metadata_target(connection)
        stdout = self._read_payload(
            target,
            "schemas",
            "SET DEFINE OFF\nSET HEADING OFF\nSET FEEDBACK OFF\n"
            "SELECT 'TEAM_SCHEMA|' || username FROM all_users WHERE username IN ('DEMO','TEAM_E2E_META');\n",
        )
        return {row[0] for row in self._rows(stdout, "TEAM_SCHEMA|") if len(row) == 1}

    def read_saved_connections(self) -> set[str]:
        completed = subprocess.run(
            [self._sql_executable, "-S", "/nolog"],
            input=b"connmgr list\nexit\n",
            capture_output=True,
            check=False,
            shell=False,
            cwd=self.run_root,
        )
        if completed.returncode != 0:
            raise E2EError("SQLcl connection-manager listing failed")
        text = completed.stdout.decode("utf-8", "replace")
        return parse_saved_connections(text)

    def capture_fixture_tree(self, developer: Any, spec: FixtureSpec, destination: Path):
        from .trees import read_export_tree, tree_digest

        workspace_id = self.workspace_id or 1
        target = self._target(
            self.payload_connection,
            self.payload_identity,
            alias=spec.tracked_alias,
            workspace_id=workspace_id,
            app_id=spec.fixture_app_id,
            parsing_schema="DEMO",
        )
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise E2EError("fixture capture destination already exists")
        destination.mkdir(mode=0o700, parents=True)
        work = destination / "sqlcl"
        output = destination / "export"
        work.mkdir(mode=0o700)
        output.mkdir(mode=0o700)
        driver = work / "export.sql"
        driver.write_text(
            f"SET DEFINE OFF\nAPEX EXPORT -APPLICATIONID {spec.fixture_app_id} -EXPTYPE APEXLANG -OVERWRITE-FILES -DIR \"{output}\"\n",
            encoding="utf-8",
            newline="",
        )
        result = self._run_sqlcl(target, "read", driver, work)
        if result.exit_code != 0:
            raise E2EError("fixture APEX export failed")
        candidates = [path.parent for path in output.rglob("application.apx")]
        if len(candidates) != 1:
            raise E2EError("fixture APEX export did not produce one complete application")
        tree = read_export_tree(candidates[0])
        return candidates[0], tree, tree_digest(tree)

    def import_fixture_tree(
        self,
        developer: Any,
        spec: FixtureSpec,
        source: Path,
        workspace_id: int,
        parsing_schema: str,
    ) -> None:
        source = Path(source).resolve()
        if not source.is_dir() or source.is_symlink() or not (source / "application.apx").is_file():
            raise E2EError("fixture Builder source is not a complete application directory")
        target = self._target(
            self.payload_connection,
            self.payload_identity,
            alias=spec.tracked_alias,
            workspace_id=workspace_id,
            app_id=spec.fixture_app_id,
            parsing_schema=parsing_schema,
        )
        driver, work = self._driver(
            "fixture-builder-save",
            "SET DEFINE OFF\n"
            f"APEX IMPORT -INPUT \"{source}\" -ID {spec.fixture_app_id} -ALIAS {spec.apex_alias} "
            f"-WORKSPACEID {workspace_id} -SCHEMA {parsing_schema}\n",
        )
        self._run_sqlcl(target, "write", driver, work)

    def capture_seed(self, connection: str, app_id: int, destination: Path) -> tuple[Path, str]:
        from .trees import read_export_tree, tree_digest

        target = self._target(self.payload_connection, self.payload_identity, alias="seed", workspace_id=1, app_id=app_id, parsing_schema="DEMO")
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        work = destination / "sqlcl"
        output = destination / "export"
        work.mkdir(mode=0o700, exist_ok=True)
        output.mkdir(mode=0o700, exist_ok=True)
        driver = work / "export.sql"
        driver.write_text(f"SET DEFINE OFF\nAPEX EXPORT -APPLICATIONID {app_id} -EXPTYPE APEXLANG -OVERWRITE-FILES -DIR \"{output}\"\n", encoding="utf-8")
        result = self._run_sqlcl(target, "read", driver, work)
        if result.exit_code != 0:
            raise E2EError("seed APEX export failed")
        candidates = [path.parent for path in output.rglob("application.apx")]
        if len(candidates) != 1:
            raise E2EError("seed APEX export did not produce one complete application")
        tree = read_export_tree(candidates[0])
        return candidates[0], tree_digest(tree)

    def create_metadata_user(self, admin_connection: str, schema: str, password: str) -> None:
        target = self.admin_target
        payload = (
            "SET DEFINE OFF\n"
            f"CREATE USER {schema} IDENTIFIED BY \"{password}\" DEFAULT TABLESPACE USERS TEMPORARY TABLESPACE TEMP QUOTA 100M ON USERS;\n"
            f"GRANT CREATE SESSION, CREATE TABLE TO {schema};\n"
        )
        self._write_payload(target, "create-metadata-user", payload)

    def save_metadata_connection(self, admin_connection: str, name: str, schema: str, password: str) -> None:
        script = self.run_root / "adapter" / "save-metadata-connection.sql"
        script.write_text(save_connection_script(name, schema, password), encoding="utf-8", newline="")
        os.chmod(script, 0o600)
        completed = subprocess.run(
            [self._sql_executable, "-S", "/nolog", f"@{script}"],
            capture_output=True,
            check=False,
            shell=False,
            cwd=self.run_root,
        )
        script.unlink(missing_ok=True)
        if completed.returncode != 0:
            raise E2EError("SQLcl could not save the fixture metadata connection")
        self.metadata_target = self._target(
            name,
            self._bootstrap_identity(name, "metadata"),
            alias=None,
            workspace_id=None,
            app_id=None,
            parsing_schema=None,
        )

    def clone_seed_to_fixture(self, payload_connection: str, seed_path: Path, spec: FixtureSpec, workspace_id: int) -> None:
        self.workspace_id = workspace_id
        target = self._target(self.payload_connection, self.payload_identity, alias=spec.tracked_alias, workspace_id=workspace_id, app_id=spec.fixture_app_id, parsing_schema="DEMO")
        driver, work = self._driver("clone-fixture", "SET DEFINE OFF\n"
            f"APEX IMPORT -INPUT \"{seed_path}\" -ID {spec.fixture_app_id} -ALIAS {spec.apex_alias} -WORKSPACEID {workspace_id} -SCHEMA DEMO\n")
        self._run_sqlcl(target, "write", driver, work)

    def verify_fixture_export(self, payload_connection: str, spec: FixtureSpec) -> dict[str, object]:
        from .trees import read_export_tree, tree_digest

        target = self._target(self.payload_connection, self.payload_identity, alias=spec.tracked_alias, workspace_id=self.workspace_id or 1, app_id=spec.fixture_app_id, parsing_schema="DEMO")
        work = self.run_root / "adapter" / "verify-fixture" / "sqlcl"
        output = self.run_root / "adapter" / "verify-fixture" / "export"
        work.mkdir(mode=0o700, parents=True, exist_ok=True)
        output.mkdir(mode=0o700, parents=True, exist_ok=True)
        driver = work / "export.sql"
        driver.write_text(f"SET DEFINE OFF\nAPEX EXPORT -APPLICATIONID {spec.fixture_app_id} -EXPTYPE APEXLANG -OVERWRITE-FILES -DIR \"{output}\"\n", encoding="utf-8")
        self._run_sqlcl(target, "read", driver, work)
        candidates = [path.parent for path in output.rglob("application.apx")]
        if len(candidates) != 1:
            raise E2EError("fixture verification export is incomplete")
        tree = read_export_tree(candidates[0])
        application = tree.get("application.apx", b"").decode("utf-8", "strict")
        match = re.search(r"^app\s+([^\s(]+)", application, re.MULTILINE)
        if not match:
            raise E2EError("fixture application.apx has no app alias")
        return {"id": spec.fixture_app_id, "alias": match.group(1), "workspace_id": self.workspace_id or 1, "parsing_schema": "DEMO", "tree_digest": tree_digest(tree)}

    def record_marker(self, metadata_connection: str, schema: str, run_id: str, app_digest: str) -> None:
        if self.metadata_target is None:
            raise E2EError("metadata connection was not initialized")
        payload = (
            "SET DEFINE OFF\n"
            "BEGIN\n"
            "  EXECUTE IMMEDIATE 'CREATE TABLE TEAM_E2E_FIXTURE_MARKER (RUN_ID VARCHAR2(128) PRIMARY KEY, APP_ID NUMBER NOT NULL, APP_DIGEST VARCHAR2(64) NOT NULL, CREATED_AT TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL)';\n"
            "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -955 THEN RAISE; END IF; END;\n/\n"
            f"MERGE INTO TEAM_E2E_FIXTURE_MARKER d USING (SELECT '{run_id}' run_id, {9099} app_id, '{app_digest}' app_digest FROM dual) s ON (d.run_id=s.run_id) WHEN NOT MATCHED THEN INSERT (run_id, app_id, app_digest) VALUES (s.run_id,s.app_id,s.app_digest) WHEN MATCHED THEN UPDATE SET app_id=s.app_id, app_digest=s.app_digest;\n"
        )
        self._write_payload(self.metadata_target, "record-marker", payload)

    def verify_marker(self, metadata_connection: str, schema: str, run_id: str, app_id: int) -> bool:
        target = self._ensure_metadata_target(metadata_connection)
        stdout = self._read_payload(target, "verify-marker", f"SET DEFINE OFF\nSET HEADING OFF\nSET FEEDBACK OFF\nSELECT 'TEAM_MARKER|' || run_id || '|' || app_id FROM TEAM_E2E_FIXTURE_MARKER WHERE run_id='{run_id}' AND app_id={app_id};\n")
        return any(len(row) == 2 and row[0] == run_id and int(row[1]) == app_id for row in self._rows(stdout, "TEAM_MARKER|"))

    def remove_fixture_app(self, admin_connection: str, spec: FixtureSpec, workspace_name: str) -> None:
        payload = f"SET DEFINE OFF\nBEGIN apex_application_install.set_workspace('{workspace_name}'); apex_application_install.set_keep_sessions(false); apex_application_install.remove_application({spec.fixture_app_id}); END;\n/\n"
        self._write_payload(self.admin_target, "remove-fixture", payload)

    def drop_metadata_user(self, admin_connection: str, schema: str, run_id: str) -> None:
        self._write_payload(self.admin_target, "drop-metadata-user", f"SET DEFINE OFF\nDROP USER {schema} CASCADE;\n")

    def delete_saved_connection(self, name: str) -> None:
        completed = subprocess.run(
            [self._sql_executable, "-S", "/nolog"],
            input=f"connmgr delete -conn {name}\nexit\n".encode("utf-8"),
            capture_output=True,
            check=False,
            shell=False,
            cwd=self.run_root,
        )
        if completed.returncode != 0:
            raise E2EError("SQLcl could not delete the fixture metadata connection")


@dataclass(frozen=True)
class CommandResult:
    """Redacted, durable result for one run-owned subprocess."""

    argv: tuple[str, ...]
    cwd: Path
    returncode: int
    stdout_path: Path
    stderr_path: Path
    started_at: str
    finished_at: str
    stdout_sha256: str
    stderr_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv),
            "cwd": str(self.cwd),
            "returncode": self.returncode,
            "stdout_path": str(self.stdout_path),
            "stderr_path": str(self.stderr_path),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
        }


def assert_safe_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(argv, Sequence) or isinstance(argv, (str, bytes)) or not argv:
        raise E2EError("argv must be a non-empty sequence")
    normalized: list[str] = []
    for index, raw in enumerate(argv):
        if not isinstance(raw, str) or not raw or _SHELL_CONTROL_RE.search(raw):
            raise E2EError(f"argv[{index}] contains unsupported shell/control characters")
        normalized.append(raw)
    return tuple(normalized)


def _redact(text: str, secrets_to_scrub: Sequence[str]) -> str:
    redacted = text
    for secret in secrets_to_scrub:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    return redacted


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def run_command(
    argv: Sequence[str],
    *,
    cwd: str | Path,
    run_root: str | Path,
    env: Mapping[str, str] | None = None,
    secrets: Sequence[str] = (),
    timeout: float | None = None,
) -> CommandResult:
    """Run an argv-only child and persist only redacted output under ``run_root``."""

    safe_argv = assert_safe_argv(argv)
    scrub = tuple(secret for secret in secrets if isinstance(secret, str) and secret)
    if any(secret in " ".join(safe_argv) for secret in scrub):
        raise E2EError("credentials must not be passed in subprocess arguments")
    root = Path(run_root).resolve()
    work = Path(cwd).resolve()
    if not root.is_dir() or root.is_symlink():
        raise E2EError("run root must be an existing real directory")
    if not work.is_dir() or work.is_symlink() or not _inside(work, root):
        raise E2EError("command cwd must be inside the run root")
    command_dir = root / "commands"
    command_dir.mkdir(mode=0o700, exist_ok=True)
    if command_dir.is_symlink():
        raise E2EError("command output directory must not be a symlink")
    command_id = secrets_module_token()
    stdout_path = command_dir / f"{command_id}.stdout"
    stderr_path = command_dir / f"{command_id}.stderr"
    child_env = os.environ.copy()
    if env is not None:
        for key, value in env.items():
            _safe_text(key, label="environment key")
            _safe_text(value, label=f"environment {key}")
            child_env[key] = value
    started = _now()
    try:
        completed = subprocess.run(
            list(safe_argv),
            cwd=work,
            env=child_env,
            shell=False,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise E2EError("subprocess timed out; result is unknown") from exc
    except OSError as exc:
        raise E2EError(f"could not start subprocess: {exc}") from exc
    finished = _now()
    stdout_text = _redact(completed.stdout.decode("utf-8", "replace"), scrub)
    stderr_text = _redact(completed.stderr.decode("utf-8", "replace"), scrub)
    stdout_path.write_text(stdout_text, encoding="utf-8", newline="")
    stderr_path.write_text(stderr_text, encoding="utf-8", newline="")
    for path in (stdout_path, stderr_path):
        os.chmod(path, 0o600)
    return CommandResult(
        argv=tuple(_redact(value, scrub) for value in safe_argv),
        cwd=work,
        returncode=completed.returncode,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        started_at=started,
        finished_at=finished,
        stdout_sha256=_sha256_bytes(stdout_text.encode("utf-8")),
        stderr_sha256=_sha256_bytes(stderr_text.encode("utf-8")),
    )


def secrets_module_token() -> str:
    """Return a path-safe token without exposing the secrets module itself."""

    return secrets.token_hex(12)


@dataclass
class RunManifest:
    run_root: Path
    run_id: str
    spec: FixtureSpec
    source_commit: str
    expected_identity: dict[str, str]
    created_at: str
    phases: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return self.run_root / "manifest.json"

    @classmethod
    def create(
        cls,
        run_root: str | Path,
        spec: FixtureSpec,
        source_commit: str,
        expected_identity: Mapping[str, Any],
    ) -> "RunManifest":
        root = Path(run_root)
        if any(part in {".", ".."} for part in root.parts):
            raise E2EError("run root must not contain path traversal components")
        if root.exists() or root.is_symlink():
            raise E2EError("run root already exists; refusing to rebind or overwrite it")
        if not root.is_absolute():
            root = (Path.cwd() / root).resolve()
        else:
            root = root.resolve()
        if root.exists() or root.is_symlink():
            raise E2EError("run root already exists after path resolution; refusing broad or rebound path")
        if root == Path(root.anchor) or root.name in {"", ".", ".."}:
            raise E2EError("run root is too broad")
        try:
            git_root = Path(
                subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
            ).resolve()
            if root == git_root:
                raise E2EError("run root must not be the repository root")
        except (OSError, subprocess.CalledProcessError):
            pass
        if not _SHA1_RE.fullmatch(source_commit):
            raise E2EError("source_commit must be a 40-character lowercase Git SHA")
        identity = _safe_mapping(expected_identity, label="expected_identity")
        root.parent.mkdir(parents=True, exist_ok=True)
        root.mkdir(mode=0o700)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"-{secrets.token_hex(4)}"
        phases = {phase: {"status": "PENDING"} for phase in _PHASES}
        manifest = cls(root, run_id, spec, source_commit, identity, _now(), phases)
        manifest._write()
        return manifest

    @classmethod
    def load(cls, run_root: str | Path) -> "RunManifest":
        root = Path(run_root).resolve()
        if not root.is_dir() or root.is_symlink():
            raise E2EError("run root is not a real directory")
        path = root / "manifest.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise E2EError("manifest is unreadable") from exc
        if not isinstance(data, dict) or data.get("version") != 1:
            raise E2EError("unsupported manifest version")
        spec_data = data.get("spec")
        if not isinstance(spec_data, dict):
            raise E2EError("manifest has no fixture specification")
        manifest = cls(
            run_root=root,
            run_id=_safe_text(data.get("run_id"), label="run_id"),
            spec=FixtureSpec(**spec_data),
            source_commit=_safe_text(data.get("source_commit"), label="source_commit"),
            expected_identity=_safe_mapping(data.get("expected_identity"), label="expected_identity"),
            created_at=_safe_text(data.get("created_at"), label="created_at"),
            phases=data.get("phases") if isinstance(data.get("phases"), dict) else {},
        )
        if not _RUN_ID_RE.fullmatch(manifest.run_id) or not _SHA1_RE.fullmatch(manifest.source_commit):
            raise E2EError("manifest identity is malformed")
        if set(manifest.phases) != set(_PHASES):
            raise E2EError("manifest phase set is incomplete")
        return manifest

    def _write(self) -> None:
        payload = json.dumps(self.to_dict(), sort_keys=True, indent=2) + "\n"
        fd, temp_name = tempfile.mkstemp(prefix=".manifest-", dir=str(self.run_root))
        temporary = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise E2EError("could not persist manifest") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "source_commit": self.source_commit,
            "expected_identity": dict(self.expected_identity),
            "spec": asdict(self.spec),
            "phases": self.phases,
        }

    def transition(self, phase: str, status: str, evidence: Mapping[str, Any]) -> None:
        if phase not in _PHASES:
            raise E2EError(f"unsupported phase: {phase}")
        if status not in _STATUSES:
            raise E2EError(f"unsupported status: {status}")
        if not isinstance(evidence, Mapping):
            raise E2EError("phase evidence must be a mapping")
        try:
            json.dumps(evidence)
        except (TypeError, ValueError) as exc:
            raise E2EError("phase evidence must be JSON serializable") from exc
        self.phases[phase] = {"status": status, "updated_at": _now(), "evidence": dict(evidence)}
        self._write()

    def cleanup_targets(self) -> CleanupTargets:
        suffix = self.run_id.rsplit("-", 1)[-1]
        return CleanupTargets(
            application_id=self.spec.fixture_app_id,
            application_alias=self.spec.apex_alias,
            metadata_schema=self.spec.metadata_schema,
            schemas=(self.spec.metadata_schema,),
            saved_connection=f"docker-team-e2e-meta-{suffix}",
            run_root=self.run_root,
        )


@dataclass(frozen=True)
class Developer:
    name: str
    clone: Path
    branch: str
    checkout_uuid: str
    git_email: str
    env_file: Path


@dataclass(frozen=True)
class TeamTopology:
    run_root: Path
    remote: Path
    source_commit: str
    developers: tuple[Developer, ...]
    env_values: dict[str, str]
    commands: tuple[CommandResult, ...] = ()


def _safe_application_path(value: str) -> str:
    _safe_text(value, label="application relative path")
    if "\\" in value:
        raise E2EError("application relative path must use POSIX separators")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise E2EError("application relative path must stay inside the captured application")
    return path.as_posix()


@dataclass(frozen=True)
class ApexMutation:
    relative_path: str
    expected_old: str
    replacement: str
    actor: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "relative_path", _safe_application_path(self.relative_path))
        for label, value in (
            ("expected mutation line", self.expected_old),
            ("replacement mutation line", self.replacement),
            ("mutation actor", self.actor),
            ("mutation reason", self.reason),
        ):
            _safe_text(value, label=label)
            if "\n" in value or "\r" in value:
                raise E2EError(f"{label} must be one line")


@dataclass(frozen=True)
class CapturedTree:
    developer: Any
    tree_root: Path
    tree: dict[str, bytes]
    digest: str
    application_id: int
    workspace_id: int
    parsing_schema: str
    capture_id: str


@dataclass(frozen=True)
class FixtureMutationEvidence:
    developer: Any
    event: str
    mutation: ApexMutation
    before_tree: dict[str, bytes]
    after_tree: dict[str, bytes]
    before_digest: str
    after_digest: str
    before_member_sha256: str
    after_member_sha256: str
    before_line: str
    after_line: str
    source_root: Path
    verified_root: Path
    application_id: int
    workspace_id: int
    parsing_schema: str
    coverage: str


def _tree_digest(tree: Mapping[str, bytes]) -> str:
    from .trees import tree_digest

    return tree_digest(dict(tree))


def _write_mutated_tree(root: Path, tree: Mapping[str, bytes], mutation: ApexMutation) -> tuple[dict[str, bytes], str, str]:
    if mutation.relative_path not in tree:
        raise E2EError("mutation path is absent from the captured live application")
    data = tree[mutation.relative_path]
    if not isinstance(data, bytes):
        raise E2EError("captured application member is not bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise E2EError("mutation member is not UTF-8") from exc
    lines = text.splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == mutation.expected_old]
    if len(matches) != 1:
        raise E2EError("mutation expected exactly one matching line")
    index = matches[0]
    line = lines[index]
    ending = line[len(line.rstrip("\r\n")):]
    lines[index] = mutation.replacement + ending
    after_member = "".join(lines).encode("utf-8")
    mutated = dict(tree)
    mutated[mutation.relative_path] = after_member
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    for relative, member in sorted(mutated.items()):
        path = root / relative
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise E2EError(f"mutation output path is a symlink: {relative}")
        path.write_bytes(member)
    return mutated, hashlib.sha256(data).hexdigest(), hashlib.sha256(after_member).hexdigest()


def capture_live_tree(
    developer: Any,
    *,
    adapter: Any,
    spec: FixtureSpec | None = None,
    workspace_id: int | None = None,
) -> CapturedTree:
    """Capture the current shared fixture through the qualified adapter."""

    if adapter is None:
        raise E2EError("capture_live_tree requires the qualified fixture adapter")
    fixture_spec = spec or FixtureSpec()
    run_root = Path(getattr(developer, "run_root", Path(developer.clone).resolve().parent.parent)).resolve()
    destination = run_root / "builder" / str(developer.name) / f"capture-{secrets.token_hex(8)}"
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    result = adapter.capture_fixture_tree(developer, fixture_spec, destination)
    if isinstance(result, CapturedTree):
        return result
    try:
        tree_root, tree, digest = result
    except (TypeError, ValueError) as exc:
        raise E2EError("fixture capture adapter returned an invalid result") from exc
    tree_root = Path(tree_root).resolve()
    if not _inside(tree_root, run_root) or tree_root.is_symlink() or not tree_root.is_dir():
        raise E2EError("fixture capture escaped the developer run root")
    if not isinstance(tree, Mapping) or any(not isinstance(path, str) or not isinstance(data, bytes) for path, data in tree.items()):
        raise E2EError("fixture capture returned a malformed tree")
    calculated = _tree_digest(tree)
    if str(digest) != calculated:
        raise E2EError("fixture capture digest does not match its members")
    observed_workspace = int(workspace_id if workspace_id is not None else getattr(adapter, "workspace_id", 0))
    if observed_workspace <= 0:
        raise E2EError("fixture capture has no verified workspace ID")
    return CapturedTree(
        developer=developer,
        tree_root=tree_root,
        tree=dict(sorted(tree.items())),
        digest=calculated,
        application_id=fixture_spec.fixture_app_id,
        workspace_id=observed_workspace,
        parsing_schema="DEMO",
        capture_id=destination.name,
    )


def fixture_builder_save(
    developer: Any,
    mutation: ApexMutation,
    *,
    adapter: Any,
    spec: FixtureSpec | None = None,
    workspace_id: int | None = None,
    parsing_schema: str = "DEMO",
) -> FixtureMutationEvidence:
    """Emulate one reviewed Builder save using a fresh live capture and import."""

    fixture_spec = spec or FixtureSpec()
    before = capture_live_tree(developer, adapter=adapter, spec=fixture_spec, workspace_id=workspace_id)
    root = before.tree_root.parent.parent / "mutations" / str(developer.name) / f"{secrets.token_hex(8)}"
    after_tree, before_hash, after_hash = _write_mutated_tree(root, before.tree, mutation)
    verified_workspace = before.workspace_id if workspace_id is None else workspace_id
    adapter.import_fixture_tree(developer, fixture_spec, root, verified_workspace, parsing_schema)
    after = capture_live_tree(developer, adapter=adapter, spec=fixture_spec, workspace_id=verified_workspace)
    if after.tree != after_tree:
        raise E2EError("fixture Builder save did not round-trip to the intended live tree")
    return FixtureMutationEvidence(
        developer=developer,
        event="fixture_builder_save",
        mutation=mutation,
        before_tree=before.tree,
        after_tree=after.tree,
        before_digest=before.digest,
        after_digest=after.digest,
        before_member_sha256=before_hash,
        after_member_sha256=after_hash,
        before_line=mutation.expected_old,
        after_line=mutation.replacement,
        source_root=root,
        verified_root=after.tree_root,
        application_id=fixture_spec.fixture_app_id,
        workspace_id=verified_workspace,
        parsing_schema=parsing_schema,
        coverage="shared database export state only; not Builder page locks or browser save UX",
    )


def _topology_git(
    run_root: Path,
    cwd: Path,
    arguments: Sequence[str],
    commands: list[CommandResult],
) -> str:
    argv = ["git"]
    if cwd != run_root:
        argv.extend(("-C", str(cwd)))
    argv.extend(arguments)
    result = run_command(argv, cwd=run_root, run_root=run_root)
    commands.append(result)
    stdout = result.stdout_path.read_text(encoding="utf-8")
    if result.returncode != 0:
        stderr = result.stderr_path.read_text(encoding="utf-8")
        detail = stderr.strip() or f"exit code {result.returncode}"
        raise E2EError(f"Git topology command failed: {detail}; see {result.stderr_path}")
    return stdout.strip()


def _topology_value(values: Mapping[str, Any], *names: str) -> str:
    for name in names:
        if name in values:
            return _safe_text(values[name], label=name)
    raise E2EError(f"topology environment is missing {names[0]}")


def _topology_env(spec: FixtureSpec, values: Mapping[str, Any]) -> dict[str, str]:
    supplied = {str(key): value for key, value in values.items()}
    for key in supplied:
        if _SENSITIVE_KEY_RE.search(key):
            raise E2EError(f"topology environment contains a sensitive key: {key}")
    connection = _topology_value(supplied, "payload_connection", "PAYLOAD_CONNECTION")
    metadata_connection = _topology_value(supplied, "metadata_connection", "METADATA_CONNECTION")
    db_name = _topology_value(supplied, "db_name", "DB_NAME")
    service = _topology_value(supplied, "service", "SERVICE")
    instance_id = _topology_value(supplied, "instance_id", "INSTANCE_ID")
    workspace_id = _topology_value(supplied, "workspace_id", "APEX_WORKSPACE_ID")
    metadata_schema = _topology_value(supplied, "metadata_schema", "METADATA_SCHEMA")
    if not workspace_id.isdigit() or int(workspace_id) <= 0:
        raise E2EError("topology workspace_id must be a positive integer")
    if metadata_schema != spec.metadata_schema or not _ORACLE_RE.fullmatch(metadata_schema):
        raise E2EError("topology metadata schema does not match the fixture specification")
    for label, value in (
        ("payload connection", connection),
        ("metadata connection", metadata_connection),
        ("database name", db_name),
        ("service", service),
        ("instance ID", instance_id),
    ):
        if _CONTROL_RE.search(value):
            raise E2EError(f"topology {label} contains a control character")
    values_out: dict[str, str] = {
        "PROJECT_NAME": spec.project_id,
        "TARGET_ROLE": "developer",
        "DB_ENVIRONMENT": "development",
        "APEX_APPS": f"{spec.tracked_alias}:{spec.fixture_app_id}",
        "TABLES_SCHEMA": "DEMO",
        "CODE_SCHEMA": "DEMO",
        "APEX_PARSING_SCHEMA": "DEMO",
        "METADATA_SCHEMA": metadata_schema,
        "APEX_WORKSPACE_ID": workspace_id,
        "APP_OWNERSHIP_MODE": "shared",
    }
    profiles = (
        ("TABLES", connection, "DEMO"),
        ("CODE", connection, "DEMO"),
        ("APEX", connection, "DEMO"),
        ("METADATA", metadata_connection, metadata_schema),
        ("VERIFY", connection, "DEMO"),
    )
    for profile, profile_connection, schema in profiles:
        values_out.update(
            {
                f"{profile}_SQLCL_CONNECTION": profile_connection,
                f"{profile}_EXPECTED_USER": schema,
                f"{profile}_EXPECTED_CURRENT_SCHEMA": schema,
                f"{profile}_EXPECTED_DB_NAME": db_name,
                f"{profile}_EXPECTED_SERVICE": service,
                f"{profile}_EXPECTED_INSTANCE_ID": instance_id,
            }
        )
    return values_out


def _write_topology_env(path: Path, values: Mapping[str, str]) -> None:
    if path.exists() or path.is_symlink():
        raise E2EError(f"topology environment already exists: {path}")
    if not path.parent.is_dir() or path.parent.is_symlink():
        raise E2EError("topology environment parent is not a real directory")
    lines = []
    for key, value in values.items():
        if not _ENV_KEY_RE.fullmatch(key):
            raise E2EError(f"topology environment key is unsafe: {key}")
        _safe_text(value, label=f"topology environment {key}")
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")
    os.chmod(path, 0o600)


def _ignore_topology_env(clone: Path) -> None:
    exclude = clone / ".git" / "info" / "exclude"
    if not exclude.is_file() or exclude.is_symlink():
        raise E2EError("developer clone has no regular Git exclude file")
    text = exclude.read_text(encoding="utf-8")
    if ".env.local-team-e2e" not in {line.strip() for line in text.splitlines()}:
        suffix = "" if not text or text.endswith("\n") else "\n"
        exclude.write_text(text + suffix + ".env.local-team-e2e\n", encoding="utf-8", newline="")


def create_team_topology(
    manifest: RunManifest,
    env_values: Mapping[str, Any],
    *,
    source_repo: str | Path | None = None,
) -> TeamTopology:
    """Create the run-owned bare remote and three local developer clones."""

    root = manifest.run_root.resolve()
    if not root.is_dir() or root.is_symlink():
        raise E2EError("topology run root is not a real directory")
    source = Path(source_repo) if source_repo is not None else Path(
        subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
    )
    if source.is_symlink() or not source.is_dir():
        raise E2EError("topology source repository is not a real directory")
    source = source.resolve()
    source_commit = _topology_git(root, root, ["-C", str(source), "rev-parse", "--verify", f"{manifest.source_commit}^{{commit}}"], [])
    # The first command above is intentionally not retained; all subsequent
    # commands are durable evidence under the manifest root.
    if source_commit != manifest.source_commit:
        raise E2EError("topology source commit does not match the manifest")
    status_commands: list[CommandResult] = []
    status = _topology_git(root, root, ["-C", str(source), "status", "--porcelain=v1", "--untracked-files=all"], status_commands)
    if status:
        raise E2EError("topology source repository is not clean; commit source changes first")
    remote = root / "remote.git"
    dev_root = root / "dev"
    if remote.exists() or remote.is_symlink() or dev_root.exists() or dev_root.is_symlink():
        raise E2EError("topology remote or developer root already exists")
    dev_root.mkdir(mode=0o700)
    commands = list(status_commands)
    _topology_git(root, root, ["init", "--bare", str(remote)], commands)
    _topology_git(root, root, ["--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"], commands)
    _topology_git(root, root, ["-C", str(source), "push", str(remote), f"{manifest.source_commit}:refs/heads/main"], commands)
    generated_env = _topology_env(manifest.spec, env_values)
    developers: list[Developer] = []
    for name in ("alice", "bob", "carol"):
        clone = dev_root / name
        branch = f"e2e/{name}"
        _topology_git(root, root, ["clone", "--no-local", "--no-tags", "--branch", "main", str(remote), str(clone)], commands)
        _topology_git(root, clone, ["checkout", "-b", branch, "origin/main"], commands)
        email = f"{name}@local-team-e2e.invalid"
        _topology_git(root, clone, ["config", "user.name", f"{name.title()} Developer"], commands)
        _topology_git(root, clone, ["config", "user.email", email], commands)
        _ignore_topology_env(clone)
        checkout_uuid = uuid.uuid5(uuid.NAMESPACE_URL, f"{manifest.spec.project_id}:{manifest.run_id}:{name}").hex
        env_file = clone / ".env.local-team-e2e"
        _write_topology_env(env_file, generated_env)
        developers.append(Developer(name, clone, branch, checkout_uuid, email, env_file))
    return TeamTopology(root, remote, manifest.source_commit, tuple(developers), generated_env, tuple(commands))


def run_team_command(developer: Developer, *arguments: str, timeout: float | None = None) -> CommandResult:
    """Run the public team CLI with checkout identity scoped to this child."""

    if not isinstance(developer, Developer) or not developer.clone.is_dir() or developer.clone.is_symlink():
        raise E2EError("developer clone is not a real directory")
    script = developer.clone / "scripts" / "team.py"
    if not script.is_file() or script.is_symlink():
        raise E2EError("developer clone has no regular scripts/team.py")
    run_root = developer.clone.parent.parent
    env_file = developer.env_file
    result = run_command(
        [sys.executable, str(script), "--env", str(env_file), *arguments],
        cwd=developer.clone,
        run_root=run_root,
        env={
            "USER": developer.name,
            "TEAM_CHECKOUT_UUID": developer.checkout_uuid,
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        timeout=timeout,
    )
    return result


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="local-team-e2e.py",
        description="Run the disposable local three-developer APEX acceptance harness",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "run", "status"):
        command = sub.add_parser(name)
        command.add_argument("--run-root", required=True, type=Path)
        if name == "run":
            command.add_argument("--keep-on-success", action="store_true")
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--run-root", required=True, type=Path)
    cleanup.add_argument("--confirm-run-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "status":
            print(json.dumps(RunManifest.load(args.run_root).to_dict(), sort_keys=True, indent=2))
            return 0
        raise E2EError(f"{args.command} phase is not implemented yet")
    except E2EError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
