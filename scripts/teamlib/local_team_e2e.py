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
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence


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
