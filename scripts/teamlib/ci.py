"""Validate the non-secret CI runner contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any
from collections.abc import Mapping


class CIError(RuntimeError):
    pass


@dataclass(frozen=True)
class DoctorReport:
    valid: bool
    issues: tuple[str, ...]
    capabilities: Mapping[str, Any]


_REQUIRED_PROFILES = {"TABLES", "CODE", "APEX", "METADATA", "VERIFY"}
_REQUIRED_TOOLCHAIN = ("python", "sqlcl", "jdk", "apex", "database", "cryptography")
_SECRET_KEY = re.compile(r"(?:password|passwd|secret|credential|wallet|private[_-]?key)", re.IGNORECASE)


def _load_contract(contract: str | Path | Mapping[str, Any]) -> tuple[Mapping[str, Any], Path | None]:
    if isinstance(contract, Mapping):
        return contract, None
    path = Path(contract)
    if path.is_symlink() or not path.is_file():
        raise CIError(f"CI runner contract is not a regular file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CIError("CI runner contract is unreadable") from exc
    if not isinstance(data, Mapping):
        raise CIError("CI runner contract must contain an object")
    return data, path.parent


def _find_secret_keys(value: Any, prefix: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            if _SECRET_KEY.search(str(key)) and item not in (False, None, ""):
                found.append(name)
            found.extend(_find_secret_keys(item, name))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_find_secret_keys(item, f"{prefix}[{index}]"))
    return found


def ci_doctor(contract: str | Path | Mapping[str, Any]) -> DoctorReport:
    """Validate the non-secret runner contract without probing production."""
    try:
        data, _ = _load_contract(contract)
    except CIError as exc:
        return DoctorReport(False, (str(exc),), {})
    issues: list[str] = []
    if data.get("version") != 1:
        issues.append("contract version must be 1")
    toolchain = data.get("toolchain")
    if not isinstance(toolchain, Mapping):
        issues.append("toolchain requirements are missing")
        toolchain = {}
    for name in _REQUIRED_TOOLCHAIN:
        if not isinstance(toolchain.get(name), str) or not toolchain[name].strip():
            issues.append(f"toolchain requirement is missing: {name}")
    profiles = data.get("profiles")
    if not isinstance(profiles, list) or not _REQUIRED_PROFILES.issubset(set(profiles)):
        issues.append("the runner must declare TABLES, CODE, APEX, METADATA and VERIFY profiles")
    production = data.get("production")
    if not isinstance(production, Mapping):
        issues.append("production safety declaration is missing")
        production = {}
    if production.get("credentials") is not False:
        issues.append("CI contract must set production credentials to false")
    if production.get("writes") is not False:
        issues.append("CI contract must set production writes to false")
    secrets = _find_secret_keys(data)
    if secrets:
        issues.append("contract contains populated secret-like fields: " + ", ".join(sorted(secrets)))
    capabilities = {
        "toolchain": dict(toolchain),
        "profiles": tuple(sorted(set(profiles or []))),
        "production_credentials": False,
        "production_writes": False,
    }
    return DoctorReport(not issues, tuple(dict.fromkeys(issues)), capabilities)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="ci")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("ci-doctor")
    doctor.add_argument("--contract", required=True)
    args = parser.parse_args(list(argv or []))
    if args.command == "ci-doctor":
        report = ci_doctor(args.contract)
        print(json.dumps({"valid": report.valid, "issues": report.issues, "capabilities": report.capabilities}, sort_keys=True))
        return 0 if report.valid else 3
    raise AssertionError(f"unsupported CI command: {args.command}")
