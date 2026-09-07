"""CI runner qualification and disposable replay orchestration."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import uuid
from typing import Any, Callable, Mapping

from .release import ReleaseError, verify_release


class CIError(RuntimeError):
    pass


@dataclass(frozen=True)
class DoctorReport:
    valid: bool
    issues: tuple[str, ...]
    capabilities: Mapping[str, Any]


@dataclass(frozen=True)
class ReplayReport:
    status: str
    source_commit: str
    instance_token: str
    fresh: str
    upgrade: str
    previous_archive_digest: str | None = None
    evidence: Mapping[str, Any] = None  # type: ignore[assignment]


_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}$")
_REQUIRED_PROFILES = {"TABLES", "CODE", "APEX", "METADATA", "VERIFY"}
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


def _resolve_contract_path(value: str | Path, base: Path | None) -> Path:
    path = Path(value)
    if base is not None and not path.is_absolute():
        path = base / path
    return path


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
        data, base = _load_contract(contract)
    except CIError as exc:
        return DoctorReport(False, (str(exc),), {})
    issues: list[str] = []
    if data.get("version") != 1:
        issues.append("contract version must be 1")
    toolchain = data.get("toolchain")
    if not isinstance(toolchain, Mapping):
        issues.append("toolchain requirements are missing")
        toolchain = {}
    for name in ("sqlcl", "jdk", "apex", "database", "python"):
        if not isinstance(toolchain.get(name), str) or not toolchain[name].strip():
            issues.append(f"toolchain requirement is missing: {name}")
    profiles = data.get("profiles")
    if not isinstance(profiles, list) or not _REQUIRED_PROFILES.issubset(set(profiles)):
        issues.append("the runner must declare TABLES, CODE, APEX, METADATA and VERIFY profiles")
    provisioner = data.get("provisioner")
    if not isinstance(provisioner, Mapping):
        issues.append("provisioner contract is missing")
        provisioner = {}
    provisioner_path = provisioner.get("path")
    if not isinstance(provisioner_path, str) or not provisioner_path:
        issues.append("provisioner path is missing")
    else:
        path = Path(provisioner_path)
        if base is not None and not path.is_absolute():
            path = base / path
        if path.is_symlink() or not path.is_file() or not path.stat().st_mode & 0o111:
            issues.append(f"provisioner is not an executable regular file: {path}")
    image = provisioner.get("image")
    if not isinstance(image, str) or not _DIGEST_RE.search(image):
        issues.append("provisioner image must be pinned by a sha256 digest")
    ords_image = provisioner.get("ords_image")
    if ords_image is not None and (not isinstance(ords_image, str) or not _DIGEST_RE.search(ords_image)):
        issues.append("provisioner ORDS image must be pinned by a sha256 digest")
    apex_archive_sha256 = provisioner.get("apex_archive_sha256")
    if apex_archive_sha256 is not None and (not isinstance(apex_archive_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", apex_archive_sha256)):
        issues.append("provisioner APEX archive checksum must be a lowercase SHA-256")
    if data.get("production") not in ({"credentials": False}, {"credentials": False, "writes": False}, None):
        issues.append("CI contract must explicitly exclude production credentials")
    runner_path = data.get("runner")
    if runner_path is not None:
        if not isinstance(runner_path, str) or not runner_path:
            issues.append("runner path is malformed")
        else:
            candidate = Path(runner_path)
            if base is not None and not candidate.is_absolute():
                candidate = base / candidate
            if candidate.is_symlink() or not candidate.is_file() or not candidate.stat().st_mode & 0o111:
                issues.append(f"replay runner is not an executable regular file: {candidate}")
    secrets = _find_secret_keys(data)
    if secrets:
        issues.append("contract contains populated secret-like fields: " + ", ".join(sorted(secrets)))
    capabilities = {
        "toolchain": dict(toolchain),
        "profiles": tuple(sorted(set(profiles or []))),
        "provisioner": str(provisioner_path or ""),
        "ords_image": str(ords_image or ""),
        "apex_archive_sha256": str(apex_archive_sha256 or ""),
        "isolated": data.get("isolated") is not False,
        "production_credentials": False,
        "runner": str(runner_path or ""),
    }
    if capabilities["isolated"] is False:
        issues.append("runner must be disposable and isolated")
    return DoctorReport(not issues, tuple(dict.fromkeys(issues)), capabilities)


def _invoke_provisioner(provisioner: Any, argv: list[str], out: Path) -> dict[str, Any]:
    if callable(provisioner):
        value = provisioner(argv, out)
        if not isinstance(value, Mapping):
            raise CIError("provisioner callback did not return a JSON object")
        return dict(value)
    path = Path(str(provisioner))
    try:
        result = subprocess.run([str(path), *argv], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    except OSError as exc:
        raise CIError(f"could not run CI provisioner: {path}") from exc
    if result.returncode != 0:
        raise CIError(f"CI provisioner failed: {result.stderr.strip() or result.returncode}")
    text = result.stdout.strip()
    if text:
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CIError("CI provisioner stdout is not JSON") from exc
    else:
        result_file = out / "result.json"
        if not result_file.is_file():
            raise CIError("CI provisioner returned no result JSON")
        try:
            value = json.loads(result_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CIError("CI provisioner result JSON is unreadable") from exc
    if not isinstance(value, Mapping):
        raise CIError("CI provisioner result must be a JSON object")
    return dict(value)


def _validate_created(value: Mapping[str, Any], ref: str) -> dict[str, Any]:
    if value.get("version") != 1:
        raise CIError("CI provisioner result has unsupported version")
    if value.get("status") != "disposable":
        raise CIError("CI provisioner did not return an explicitly disposable target")
    token = value.get("instance_token")
    if not isinstance(token, str) or not token or any(char.isspace() for char in token):
        raise CIError("CI provisioner returned no safe instance token")
    env_path = value.get("env_path")
    if not isinstance(env_path, str) or not env_path:
        raise CIError("CI provisioner did not return an explicit replay environment path")
    result = dict(value)
    result["source_commit"] = ref
    return result


def ci_replay(
    ref: str,
    previous_artifact: str | Path | None,
    *,
    contract: str | Path | Mapping[str, Any],
    provisioner: Callable[[list[str], Path], Mapping[str, Any]] | str | Path,
    runner: Callable[[str, str | Path | None, Mapping[str, Any]], Mapping[str, Any]],
    scratch_root: str | Path | None = None,
) -> ReplayReport:
    """Run a qualified create/replay/destroy cycle against one disposable target."""
    if not isinstance(ref, str) or not ref or any(char.isspace() for char in ref):
        raise CIError("CI replay requires an exact source ref")
    doctor = ci_doctor(contract)
    if not doctor.valid:
        raise CIError("CI runner contract is not qualified: " + "; ".join(doctor.issues))
    previous_digest: str | None = None
    if previous_artifact is not None:
        try:
            previous_digest = verify_release(previous_artifact).archive_digest
        except ReleaseError as exc:
            raise CIError(f"previous release artifact is invalid: {exc}") from exc
    base = Path(scratch_root) if scratch_root is not None else Path("scratch") / "ci"
    base.mkdir(parents=True, exist_ok=True)
    # Provisioners mount the output directory into Docker/Podman.  A relative
    # host path is interpreted as a named volume by Docker and fails (or, on
    # some engines, silently targets the wrong host directory), so the argv
    # contract always receives an absolute path.
    base = base.resolve()
    run_id = str(uuid.uuid4())
    out = base / run_id
    out.mkdir(parents=True, exist_ok=False)
    created: dict[str, Any] | None = None
    cleanup_error: str | None = None
    try:
        create_argv = ["create", "--run-id", run_id, "--out", str(out)]
        created = _validate_created(_invoke_provisioner(provisioner, create_argv, out), ref)
        target = dict(created)
        target["run_id"] = run_id
        observed = runner(ref, previous_artifact, target)
        expected_upgrade = "PASS" if previous_artifact is not None else "NOT_APPLICABLE_INITIAL_RELEASE"
        if (
            not isinstance(observed, Mapping)
            or observed.get("status") != "PASS"
            or observed.get("source_commit") != ref
            or observed.get("fresh") != "PASS"
            or observed.get("upgrade") != expected_upgrade
        ):
            raise CIError("disposable replay did not produce a verified PASS for the selected source ref")
        upgrade = expected_upgrade
        evidence = {"version": 1, "status": "PASS", "source_commit": ref, "fresh": "PASS", "upgrade": upgrade, "target": {key: target[key] for key in ("instance_token", "status", "source_commit")}}
        return ReplayReport("PASS", ref, str(created["instance_token"]), "PASS", upgrade, previous_digest, evidence)
    finally:
        if created is not None:
            try:
                destroyed = _invoke_provisioner(provisioner, ["destroy", "--run-id", run_id, "--instance-token", str(created["instance_token"])], out)
                if destroyed.get("version") != 1 or destroyed.get("destroyed") is not True:
                    cleanup_error = "CI provisioner did not confirm exact-target destruction"
            except Exception as exc:
                cleanup_error = str(exc)
        shutil.rmtree(out, ignore_errors=True)
        if cleanup_error:
            raise CIError(cleanup_error)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="ci")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("ci-doctor")
    doctor.add_argument("--contract", required=True)
    replay = sub.add_parser("ci-replay")
    replay.add_argument("--ref", required=True)
    replay.add_argument("--previous")
    replay.add_argument("--contract", required=True)
    replay.add_argument("--provisioner", required=True)
    replay.add_argument("--runner", required=True, help="qualified runner executable")
    replay.add_argument("--scratch-root", default="scratch/ci")
    args = parser.parse_args(list(argv or []))
    if args.command == "ci-doctor":
        report = ci_doctor(args.contract)
        print(json.dumps({"valid": report.valid, "issues": report.issues, "capabilities": report.capabilities}, sort_keys=True))
        return 0 if report.valid else 3

    _, contract_base = _load_contract(args.contract)
    provisioner_path = _resolve_contract_path(args.provisioner, contract_base)
    runner_path = _resolve_contract_path(args.runner, contract_base)
    if runner_path.is_symlink() or not runner_path.is_file() or not runner_path.stat().st_mode & 0o111:
        raise SystemExit("qualified CI replay runner must be an executable regular file")

    def runner(ref: str, previous: str | Path | None, target: Mapping[str, Any]) -> Mapping[str, Any]:
        root = Path(args.scratch_root)
        root.mkdir(parents=True, exist_ok=True)
        target_path = root / f"runner-{uuid.uuid4().hex}.json"
        target_path.write_text(json.dumps(dict(target), sort_keys=True), encoding="utf-8", newline="\n")
        command = [str(runner_path), "--ref", ref, "--target-json", str(target_path)]
        if previous is not None:
            command.extend(["--previous", str(previous)])
        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        finally:
            target_path.unlink(missing_ok=True)
        if result.returncode != 0:
            raise CIError(result.stderr.strip() or "qualified CI replay runner failed")
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CIError("qualified CI replay runner did not return JSON") from exc
        if not isinstance(value, Mapping):
            raise CIError("qualified CI replay runner returned a non-object")
        return value

    report = ci_replay(
        args.ref, args.previous, contract=args.contract, provisioner=provisioner_path,
        runner=runner, scratch_root=args.scratch_root,
    )
    print(json.dumps({"status": report.status, "source_commit": report.source_commit, "fresh": report.fresh, "upgrade": report.upgrade, "instance_token": report.instance_token, "previous_archive_digest": report.previous_archive_digest}, sort_keys=True))
    return 0
