"""Observed prepared-runner and target preflight for online workflows."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
from typing import Any
from collections.abc import Callable, Mapping

from .config import Config, Target, profile_target
from .sqlcl import run_sqlcl


@dataclass(frozen=True)
class RuntimeReport:
    profiles: tuple[str, ...]
    versions: Mapping[str, str]
    capabilities: Mapping[str, str]
    target_state_keys: Mapping[str, str]
    toolchain_digest: str


_REQUIRED_TOOLCHAIN = {
    "python", "sqlcl", "jdk", "apex", "database", "cryptography",
}
_REQUIRED_PROFILES = {"TABLES", "CODE", "APEX", "METADATA", "VERIFY"}
_RUNTIME_RE = re.compile(r"^TEAM_RUNTIME\|([a-z]+)\|([^|\s][^|]*)$")
_VERSION_RE = re.compile(r"(?<![0-9])([0-9]+(?:\.[0-9]+)*(?:ai)?)(?![A-Za-z0-9])", re.IGNORECASE)
_REQUIREMENT_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)*)(ai)?\+$", re.IGNORECASE)
_IDENTITY_KEYS = ("SESSION_USER", "CURRENT_SCHEMA", "DB_NAME", "SERVICE", "INSTANCE_ID")


def _parse_runtime_versions(stdout: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in str(stdout).splitlines():
        line = raw_line.strip().rstrip("\r")
        if not line.startswith("TEAM_RUNTIME|"):
            continue
        match = _RUNTIME_RE.fullmatch(line)
        if match is None:
            raise RuntimeError("runtime version output contains a malformed marker")
        name, value = match.groups()
        if name not in {"database", "apex"} or name in values:
            raise RuntimeError("runtime version output contains an unknown or duplicate marker")
        values[name] = value.strip()
    if set(values) != {"database", "apex"}:
        raise RuntimeError("runtime version output must contain exactly database and apex markers")
    return values


def _command_version(
    argv: list[str],
    command_runner: Callable[..., Any],
) -> str:
    try:
        result = command_runner(
            argv, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise RuntimeError(f"could not execute {' '.join(argv)}: {exc}") from exc
    stdout = getattr(result, "stdout", "") or ""
    stderr = getattr(result, "stderr", "") or ""
    output = "\n".join(part for part in (stdout, stderr) if part)
    if getattr(result, "returncode", 1) != 0 or not output.strip():
        raise RuntimeError(f"could not observe version from {' '.join(argv)}")
    match = _VERSION_RE.search(output)
    if match is None:
        raise RuntimeError(f"version output from {' '.join(argv)} contains no numeric version")
    return match.group(1).lower()


def _numeric_tuple(value: str, label: str) -> tuple[int, ...]:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)*)(?:ai)?", value, re.IGNORECASE)
    if match is None:
        raise RuntimeError(f"observed {label} version is not numeric: {value}")
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError as exc:
        raise RuntimeError(f"observed {label} version is not numeric: {value}") from exc


def _require_contract(
    contract_path: str | Path,
    versions: Mapping[str, str],
    capabilities: Mapping[str, str],
) -> int:
    path = Path(contract_path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"runner contract is not a regular file: {path}")
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("runner contract is unreadable") from exc
    if not isinstance(contract, Mapping) or type(contract.get("version")) is not int:
        raise RuntimeError("runner contract version is malformed")
    contract_version = contract["version"]
    if contract_version != 1:
        raise RuntimeError("runner contract version is unsupported")
    toolchain = contract.get("toolchain")
    if not isinstance(toolchain, Mapping) or set(toolchain) != _REQUIRED_TOOLCHAIN:
        raise RuntimeError("runner contract toolchain keys are incomplete or unknown")
    profiles = contract.get("profiles")
    if not isinstance(profiles, list) or not _REQUIRED_PROFILES.issubset(set(profiles)):
        raise RuntimeError("runner contract profiles are incomplete")
    for name in _REQUIRED_TOOLCHAIN:
        requirement = toolchain[name]
        if not isinstance(requirement, str) or not requirement.strip():
            raise RuntimeError(f"runner contract requirement is missing: {name}")
        if requirement == "Ed25519-qualified":
            if capabilities.get(name) != requirement:
                raise RuntimeError("runner contract requires an Ed25519-qualified capability")
            continue
        match = _REQUIREMENT_RE.fullmatch(requirement)
        if match is None:
            raise RuntimeError(f"runner contract requirement is unparsable: {name}")
        observed = versions.get(name)
        if not isinstance(observed, str):
            raise RuntimeError(f"observed runner version is missing: {name}")
        required_tuple = tuple(int(part) for part in match.group(1).split("."))
        observed_tuple = _numeric_tuple(observed, name)
        width = max(len(required_tuple), len(observed_tuple))
        if observed_tuple + (0,) * (width - len(observed_tuple)) < required_tuple + (0,) * (width - len(required_tuple)):
            raise RuntimeError(
                f"observed {name} version {observed} does not satisfy required {requirement}"
            )
    return contract_version


def _probe_ed25519() -> str:
    try:
        import cryptography
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise RuntimeError("prepared runner lacks the cryptography package") from exc
    try:
        private = Ed25519PrivateKey.generate()
        signature = private.sign(b"team-runtime-ed25519-capability-v1")
        private.public_key().verify(signature, b"team-runtime-ed25519-capability-v1")
    except Exception as exc:  # cryptography backend errors vary by platform
        raise RuntimeError("prepared runner Ed25519 capability probe failed") from exc
    return str(getattr(cryptography, "__version__", "unknown"))


def _validate_profile_identity(
    name: str,
    target: Target,
    result: Any,
    physical: tuple[str, str, str] | None,
) -> tuple[str, str, str]:
    identity = getattr(result, "identity", None)
    if not isinstance(identity, Mapping) or any(identity.get(key) != getattr(target, _target_attribute(key)) for key in _IDENTITY_KEYS):
        raise RuntimeError(f"profile identity probe does not match {name}")
    observed = (str(identity["INSTANCE_ID"]), str(identity["DB_NAME"]), str(identity["SERVICE"]))
    if physical is not None and observed != physical:
        raise RuntimeError(f"profile identity differs across targets: {name}")
    return observed


def _target_attribute(identity_key: str) -> str:
    return {
        "SESSION_USER": "session_user",
        "CURRENT_SCHEMA": "current_schema",
        "DB_NAME": "db_name",
        "SERVICE": "service",
        "INSTANCE_ID": "instance_id",
    }[identity_key]


def preflight_online(
    config: Config,
    repo: str | Path,
    flow_executable: str | Path | None,
    *,
    runner: Callable[..., Any] = run_sqlcl,
    which: Callable[[str], str | None] = shutil.which,
    command_runner: Callable[..., Any] = subprocess.run,
) -> RuntimeReport:
    """Observe the prepared runner and every bound non-production target."""
    if config.environment == "production" or config.role == "production":
        raise RuntimeError("online qualification preflight refuses production")
    repo_path = Path(repo)
    if repo_path.is_symlink() or not repo_path.is_dir():
        raise RuntimeError(f"online preflight repository is not a real directory: {repo_path}")
    requested_sqlcl = os.environ.get("SQLCL_BIN", "sql")
    sqlcl = which(requested_sqlcl)
    java = which("java")
    if not sqlcl:
        raise RuntimeError("SQLcl executable is unavailable")
    if not java:
        raise RuntimeError("JDK executable is unavailable")
    if config.apps:
        if not flow_executable:
            raise RuntimeError("TEAM_FLOW_RUNNER is required for declared flow checks")
        flow = Path(flow_executable)
        if flow.is_symlink() or not flow.is_file() or not os.access(flow, os.X_OK):
            raise RuntimeError("TEAM_FLOW_RUNNER is not an executable regular file")

    sqlcl_version = _command_version([sqlcl, "-version"], command_runner)
    jdk_version = _command_version([java, "-version"], command_runner)
    targets: dict[str, Target] = {
        name: profile_target(config, name)
        for name in ("TABLES", "CODE", "METADATA", "VERIFY")
    }
    for alias in sorted(config.apps):
        targets[f"APEX:{alias}"] = profile_target(config, "APEX", alias=alias)
    identity_driver = repo_path / "scripts" / "sql" / "identity.sql"
    versions_driver = repo_path / "scripts" / "sql" / "runtime_versions.sql"
    if identity_driver.is_symlink() or not identity_driver.is_file():
        raise RuntimeError(f"identity driver is not a regular file: {identity_driver}")
    if versions_driver.is_symlink() or not versions_driver.is_file():
        raise RuntimeError(f"runtime version driver is not a regular file: {versions_driver}")
    physical: tuple[str, str, str] | None = None
    for name, target in targets.items():
        try:
            result = runner(
                target, "read", identity_driver,
                repo_path / "scratch" / "preflight" / "identity" / name,
            )
        except Exception as exc:
            raise RuntimeError(f"profile identity probe failed for {name}: {exc}") from exc
        physical = _validate_profile_identity(name, target, result, physical)
    try:
        versions_result = runner(
            targets["VERIFY"], "read", versions_driver,
            repo_path / "scratch" / "preflight" / "versions",
        )
    except Exception as exc:
        raise RuntimeError(f"runtime version probe failed: {exc}") from exc
    remote_versions = _parse_runtime_versions(getattr(versions_result, "stdout", ""))
    cryptography_version = _probe_ed25519()
    versions = {
        "python": platform.python_version(),
        "sqlcl": sqlcl_version,
        "jdk": jdk_version,
        "apex": remote_versions["apex"],
        "database": remote_versions["database"],
        "cryptography": cryptography_version,
    }
    capabilities = {"cryptography": "Ed25519-qualified"}
    contract_version = _require_contract(
        repo_path / "ci" / "runner-contract.json", versions, capabilities
    )
    target_state_keys = {name: target.state_key for name, target in targets.items()}
    digest = hashlib.sha256(
        json.dumps(
            {
                "contract_version": contract_version,
                "versions": versions,
                "capabilities": capabilities,
                "targets": target_state_keys,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return RuntimeReport(
        tuple(sorted(targets)), versions, capabilities, target_state_keys, digest
    )
