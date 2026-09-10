"""Persistent-target qualification and reusable application-check adapters.

Qualification is intentionally read-only with respect to application and
database payloads. The caller applies reviewed migrations and deployments
first; this module validates target identity/frontier/history, then runs
declared checks and emits deterministic evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any
from collections.abc import Callable, Mapping, Sequence

from .app_checks import AppCheckError, AppCheckReport, verify_candidate_apps
from .assertions import AssertionVerificationError, parse_team_assertions
from .ci import ci_doctor
from .config import Config, Target, profile_target
from .evidence import EvidenceError, canonical_json, validate_test_evidence
from .migration_store import MigrationStoreError
from .release import ReleaseError, verify_release
from .runtime import RuntimeReport
from .sqlcl import run_sqlcl


class QualificationError(RuntimeError):
    """Raised when a target cannot produce trustworthy qualification evidence."""

    def __init__(self, message: str, report: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.report = dict(report) if isinstance(report, Mapping) else None


_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _regular_file(path: str | Path, label: str) -> Path:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise QualificationError(f"{label} is not a regular file: {candidate}")
    return candidate


def _load_json(path: str | Path, label: str) -> tuple[bytes, dict[str, Any]]:
    candidate = _regular_file(path, label)
    try:
        raw = candidate.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"{label} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise QualificationError(f"{label} must contain a JSON object")
    return raw, value


def declaration_paths(repo: Path, aliases: Sequence[str]) -> dict[str, Path]:
    """Resolve one regular declaration file for every selected application."""
    result: dict[str, Path] = {}
    for alias in aliases:
        if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias):
            raise QualificationError(f"application alias is unsafe: {alias!r}")
        path = repo / "ci" / "app-checks" / f"{alias}.json"
        if path.is_symlink() or not path.is_file():
            raise QualificationError(f"candidate application declaration is missing: {path}")
        result[alias] = path
    return result


def _select_runner(*, profile: Any, repo: Path, work: Path, run_sqlcl: Callable[..., Any] = run_sqlcl):
    """Return a SELECT-only assertion adapter bound to the VERIFY profile."""
    checks_root = Path(repo) / "ci" / "app-checks"

    def resolve(alias: str, check: Mapping[str, Any]) -> dict[str, Any]:
        relative = str(check.get("verify_sql", ""))
        member = checks_root / relative
        if member.is_symlink() or not member.is_file():
            return {"status": "FAIL", "diagnostic": f"verification member is missing: {relative}"}
        driver_root = Path(work) / "app-checks" / alias / str(check.get("id", "check"))
        driver_root.mkdir(parents=True, exist_ok=True)
        driver = driver_root / "verify.sql"
        try:
            text = member.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return {"status": "FAIL", "diagnostic": f"verification member is unreadable: {exc}"}
        driver.write_text(
            "SET DEFINE OFF\nSET HEADING OFF\nSET FEEDBACK OFF\nSET PAGESIZE 0\n"
            + text,
            encoding="utf-8",
            newline="\n",
        )
        try:
            result = run_sqlcl(profile, "read", driver, driver_root)
        except Exception as exc:  # noqa: BLE001
            return {"status": "FAIL", "diagnostic": f"verification query failed: {exc}"}
        try:
            names = parse_team_assertions(getattr(result, "stdout", ""))
        except AssertionVerificationError as exc:
            return {"status": "FAIL", "diagnostic": str(exc)}
        return {"status": "PASS", "diagnostic": "", "assertions": list(names)}

    return resolve


def _require_flow_adapter(declarations: Mapping[str, Any], executable: str | None) -> None:
    if executable:
        return
    pending = [
        f"{alias}/{check.get('id')}"
        for alias, declaration in sorted(declarations.items())
        for check in declaration.get("checks", [])
        if check.get("kind") == "flow"
    ]
    if pending:
        raise QualificationError(
            "declared flow checks have no qualified browser adapter: "
            + ", ".join(pending)
            + ". Set TEAM_FLOW_RUNNER to an executable invoked as "
            + "executable --alias alias --check-json path that prints one JSON "
            + "object with a status of PASS, FAIL or UNKNOWN."
        )


def _flow_runner(executable: str, work: Path):
    def resolve(alias: str, check: Mapping[str, Any]) -> dict[str, Any]:
        payload_root = Path(work) / "flow" / alias
        payload_root.mkdir(parents=True, exist_ok=True)
        payload = payload_root / f"{check.get('id', 'check')}.json"
        payload.write_text(json.dumps(dict(check), sort_keys=True), encoding="utf-8", newline="\n")
        try:
            result = subprocess.run(
                [executable, "--alias", alias, "--check-json", str(payload)],
                capture_output=True, text=True, check=False,
            )
        except OSError as exc:
            return {"status": "FAIL", "diagnostic": f"flow adapter could not start: {exc}"}
        if result.returncode != 0:
            return {"status": "FAIL", "diagnostic": result.stderr.strip() or f"flow adapter exit {result.returncode}"}
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return {"status": "FAIL", "diagnostic": f"flow adapter did not return JSON: {exc}"}
        if not isinstance(value, dict):
            return {"status": "FAIL", "diagnostic": "flow adapter returned a non-object"}
        return value

    return resolve


def _target_identity(config: Config, targets: Mapping[str, Target]) -> dict[str, Any]:
    metadata = targets["METADATA"]
    return {
        "project": config.project,
        "role": config.role,
        "environment": config.environment,
        "target_kind": "persistent",
        "instance_id": metadata.instance_id,
        "db_name": metadata.db_name,
        "service": metadata.service,
        "workspace_id": config.workspace_id,
        "app_ids": dict(sorted(config.apps.items())),
        "state_key": metadata.state_key,
        "binding_digest": metadata.binding_digest,
    }


def _run_identity(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is not None:
        return {str(key): str(item) for key, item in value.items() if item not in (None, "")}
    return {
        key: os.environ[key]
        for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_WORKFLOW", "GITHUB_SHA")
        if os.environ.get(key)
    }


def _sha256_field(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise QualificationError(f"{label} must be a lowercase SHA-256")
    return value


def _check_apply_report(path: Path, *, source_commit: str, archive_digest: str, target_state_key: str) -> None:
    _, value = _load_json(path, "apply report")
    allowed = {
        "version", "status", "source_commit", "archive_digest", "target_state_key",
        "target_digest", "history_digest", "pending",
    }
    if set(value) != allowed:
        raise QualificationError("apply report has an unexpected shape")
    if value["version"] != 1 or value["status"] != "applied":
        raise QualificationError("apply report is not a successful version-1 report")
    if value["source_commit"] != source_commit or value["archive_digest"] != archive_digest:
        raise QualificationError("apply report does not match the selected release")
    if value["target_state_key"] != target_state_key:
        raise QualificationError("apply report target identity does not match qualification target")
    _sha256_field(value["target_digest"], "apply report target_digest")
    _sha256_field(value["history_digest"], "apply report history_digest")
    if not isinstance(value["pending"], list) or any(not isinstance(item, str) for item in value["pending"]):
        raise QualificationError("apply report pending must be a list of migration IDs")


def _base_report(
    *,
    source_commit: str,
    archive_digest: str | None,
    toolchain_digest: str,
    target_identity: Mapping[str, Any],
    run_identity: Mapping[str, Any],
    observation: Mapping[str, Any],
    history_digest: str,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "version": 2,
        "final_status": "PASS",
        "source_commit": source_commit,
        "toolchain_digest": toolchain_digest,
        "target_identity": dict(target_identity),
        "run_identity": dict(run_identity),
        "qualification_identity": {
            "target_kind": "persistent",
            "observation_sequence": int(observation["sequence"]),
            "observation_digest": _sha256_field(observation["after"], "observation digest"),
            "history_digest": _sha256_field(history_digest, "history digest"),
        },
        "application_checks": {
            "status": "PASS",
            "checks_digest": "",
            "coverage": {},
            "unknown": 0,
            "results": [],
        },
        "results": {
            "migrations": "PASS",
            "application_deploy": "PASS",
            "application_checks": "PASS",
        },
    }
    if archive_digest is not None:
        report["archive_digest"] = archive_digest
    return report


def qualify_target(
    repo: str | Path,
    config: Config,
    source_commit: str,
    aliases: Sequence[str],
    *,
    store: Any,
    work: str | Path,
    release_archive: str | Path | None = None,
    apply_report: str | Path | None = None,
    flow_executable: str | None = None,
    sql_runner: Callable[..., Any] = run_sqlcl,
    run_identity: Mapping[str, Any] | None = None,
    runner_contract: str | Path = "ci/runner-contract.json",
    runtime_report: RuntimeReport | None = None,
) -> dict[str, Any]:
    if config.environment == "production" or config.role not in {"integration", "test"}:
        raise QualificationError("qualification requires a non-production integration or test target")
    if not source_commit or any(char.isspace() for char in source_commit):
        raise QualificationError("qualification requires an exact source commit")
    if runtime_report is None:
        raise QualificationError("qualification requires an observed runtime preflight")
    runtime_toolchain_digest = getattr(runtime_report, "toolchain_digest", None)
    _sha256_field(runtime_toolchain_digest, "runtime toolchain digest")
    selected = tuple(alias.strip() for alias in aliases if alias and alias.strip())
    if len(set(selected)) != len(selected) or set(selected) != set(config.apps):
        raise QualificationError("qualification aliases must exactly match configured application bindings")
    if bool(release_archive) != bool(apply_report):
        raise QualificationError("--release-archive and --apply-report must be supplied together")

    repo_path = Path(repo)
    work_path = Path(work)
    work_path.mkdir(parents=True, exist_ok=True)
    targets = {
        profile: profile_target(config, profile, alias=alias)
        for profile, alias in [("APEX", selected[0])] if selected
    }
    targets.update({profile: profile_target(config, profile) for profile in ("TABLES", "CODE", "METADATA", "VERIFY")})
    metadata = targets["METADATA"]
    identity_driver = repo_path / "scripts" / "sql" / "identity.sql"
    try:
        for profile in ("TABLES", "CODE", "METADATA", "VERIFY"):
            sql_runner(targets[profile], "read", identity_driver, work_path / "identity" / profile)
        for alias in selected:
            apex_target = profile_target(config, "APEX", alias=alias)
            sql_runner(apex_target, "read", identity_driver, work_path / "identity" / "APEX" / alias)
    except Exception as exc:
        raise QualificationError(f"target identity qualification failed: {exc}") from exc

    contract_path = Path(runner_contract)
    doctor = ci_doctor(contract_path)
    if not doctor.valid:
        raise QualificationError("runner contract is invalid: " + "; ".join(doctor.issues))
    toolchain_digest = runtime_toolchain_digest

    try:
        store.validate_observation_chain(metadata)
        history = store.read_history(metadata)
        state = store.read_state(metadata)
    except (MigrationStoreError, KeyError, TypeError, ValueError) as exc:
        raise QualificationError(f"migration qualification state is unreadable: {exc}") from exc
    attempts = state.get("attempts", {})
    unresolved = [
        attempt_id for attempt_id, attempt in attempts.items()
        if isinstance(attempt, Mapping) and attempt.get("state") in {"RUNNING", "UNKNOWN", "FAILED"}
    ]
    if unresolved:
        raise QualificationError("qualification has unresolved migration attempts: " + ", ".join(sorted(unresolved)))
    observations = state.get("observations", [])
    if not observations or not isinstance(observations[-1], Mapping):
        raise QualificationError("qualification requires an accepted observed frontier")
    latest = observations[-1]
    if latest.get("sequence") is None or latest.get("after") is None:
        raise QualificationError("accepted observation is incomplete")
    target_identity = _target_identity(config, targets)
    report = _base_report(
        source_commit=source_commit,
        archive_digest=None,
        toolchain_digest=toolchain_digest,
        target_identity=target_identity,
        run_identity=_run_identity(run_identity),
        observation=latest,
        history_digest=_digest(history),
    )

    if release_archive is not None and apply_report is not None:
        try:
            manifest = verify_release(release_archive)
        except ReleaseError as exc:
            raise QualificationError(str(exc), report) from exc
        if manifest.source_commit != source_commit:
            raise QualificationError("release archive source commit does not match qualification", report)
        report["archive_digest"] = manifest.archive_digest
        _check_apply_report(
            Path(apply_report),
            source_commit=source_commit,
            archive_digest=manifest.archive_digest,
            target_state_key=targets["METADATA"].state_key,
        )

    declarations = declaration_paths(repo_path, selected)
    loaded = {}
    for alias, path in declarations.items():
        try:
            loaded[alias] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise QualificationError(f"candidate declaration is unreadable: {path}") from exc
    _require_flow_adapter(loaded, flow_executable)
    target_for_checks = {
        **target_identity,
        "source_commit": source_commit,
        "app_ids": dict(config.apps),
        "select_runner": _select_runner(
            profile=targets["VERIFY"], repo=repo_path, work=work_path, run_sqlcl=sql_runner
        ),
        "flow_runner": _flow_runner(flow_executable, work_path) if flow_executable else None,
    }
    try:
        app_report: AppCheckReport = verify_candidate_apps(
            {"commit": source_commit, "apps": {alias: {"app_id": config.apps[alias]} for alias in selected}},
            target_for_checks,
            declarations,
        )
    except AppCheckError as exc:
        if exc.report is not None:
            report["final_status"] = "FAIL"
            report["results"]["application_checks"] = "FAIL"
            report["application_checks"] = {
                "status": exc.report.status,
                "checks_digest": exc.report.checks_digest,
                "coverage": dict(exc.report.coverage),
                "unknown": int(exc.report.coverage.get("unknown", 0)),
                "results": [item.as_dict() for item in exc.report.results],
            }
        raise QualificationError(str(exc), report) from exc
    report["application_checks"] = {
        "status": app_report.status,
        "checks_digest": app_report.checks_digest,
        "coverage": dict(app_report.coverage),
        "unknown": int(app_report.coverage.get("unknown", 0)),
        "results": [item.as_dict() for item in app_report.results],
    }
    return report


def write_report(report: Mapping[str, Any], out: str | Path) -> None:
    destination = Path(out)
    if destination.is_symlink():
        raise QualificationError(f"qualification output must not be a symlink: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json(dict(report)) + b"\n"
    fd, name = tempfile.mkstemp(prefix=".qualification-", dir=str(destination.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise QualificationError(f"could not write qualification report: {destination}") from exc


def _private_key(raw: bytes):
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise QualificationError("signing test evidence requires the 'cryptography' package") from exc
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except Exception as exc:  # cryptography exception differs by version
        raise QualificationError("signing key is not a readable PEM private key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise QualificationError("signing key must be Ed25519")
    return key


def sign_test_evidence(evidence: str | Path, private_key: str | Path, signature_out: str | Path) -> str:
    raw, _ = _load_json(evidence, "test evidence")
    try:
        validate_test_evidence(raw)
    except EvidenceError as exc:
        raise QualificationError(str(exc)) from exc
    key = _private_key(_regular_file(private_key, "signing key").read_bytes())
    destination = Path(signature_out)
    if destination.is_symlink():
        raise QualificationError(f"signature output must not be a symlink: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    signature = key.sign(raw)
    fd, name = tempfile.mkstemp(prefix=".evidence-signature-", dir=str(destination.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(signature)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise QualificationError(f"could not write evidence signature: {destination}") from exc
    return hashlib.sha256(raw).hexdigest()


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="qualification")
    sub = parser.add_subparsers(dest="command", required=True)
    sign = sub.add_parser("sign-test-evidence")
    sign.add_argument("--evidence", required=True)
    sign.add_argument("--private-key", required=True)
    sign.add_argument("--out", required=True)
    args = parser.parse_args(list(argv or []))
    if args.command == "sign-test-evidence":
        digest = sign_test_evidence(args.evidence, args.private_key, args.out)
        print(json.dumps({"status": "signed", "evidence_digest": digest}, sort_keys=True))
        return 0
    raise SystemExit(f"unsupported qualification command: {args.command}")
