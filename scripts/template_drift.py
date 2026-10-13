#!/usr/bin/env python3
"""Inspect local template-lock drift and, only on request, compare upstream."""

from __future__ import annotations

import argparse
import json
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.upgrade_template import (
    CONFLICT_SUFFIX,
    UpgradeError,
    classify,
    fetch_template,
    load_manifest,
    normalized_sha256,
    read_lock_with_hash,
    safe_project_path,
    sha256_pair,
    validate_lock_ownership,
)


REPORT_SCHEMA_VERSION = 1
SHA_RE = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z", re.ASCII)


def _original_template(repo_root: Path, manifest: dict) -> bool:
    """Use the same local origin/manifest signal as readiness; never contact GitHub."""
    from scripts.verify_local import is_original_template

    return is_original_template(repo_root, manifest)


def _ref_is_valid(repo_root: Path, ref: str) -> bool:
    if not isinstance(ref, str) or not ref or len(ref) > 255 or ref.startswith("-"):
        return False
    if any(ord(char) < 32 or ord(char) == 127 for char in ref):
        return False
    if SHA_RE.fullmatch(ref):
        return True
    result = subprocess.run(
        ["git", "-C", str(repo_root), "check-ref-format", "--branch", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _manifest_upstream(manifest: dict) -> str:
    value = manifest.get("upstream")
    parsed = urlsplit(value) if isinstance(value, str) else None
    if (
        parsed is None
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise UpgradeError("template manifest upstream must be a credential-free HTTPS URL")
    return value


def _local_file_state(repo_root: Path, relative: str, expected: str) -> dict:
    try:
        path = safe_project_path(repo_root, relative)
        pair = sha256_pair(path)
    except UpgradeError:
        return {"path": relative, "status": "unsafe"}
    if pair is None:
        return {"path": relative, "status": "missing"}
    return {
        "path": relative,
        "status": "matches-lock" if pair[1] == expected else "customized",
    }


def _conflict_state(repo_root: Path, relative: str) -> str:
    try:
        path = safe_project_path(repo_root, relative + CONFLICT_SUFFIX)
    except UpgradeError:
        return "unsafe"
    try:
        info = path.lstat()
    except FileNotFoundError:
        return "none"
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return "unsafe"
    return "pending"


def _compare_upstream(repo_root: Path, manifest: dict, lock: dict, ref: str) -> dict:
    if not _ref_is_valid(repo_root, ref):
        return {"status": "unavailable", "reason": "ref is not a valid branch, tag, or commit"}
    try:
        upstream = _manifest_upstream(manifest)
        with tempfile.TemporaryDirectory(prefix="template-status-") as temporary:
            checkout = Path(temporary) / "template"
            commit = fetch_template(upstream, ref, checkout)
            current_manifest = load_manifest(checkout)
            template_owned, _placeholders = classify(checkout, current_manifest)
            paths = set(template_owned) | set(lock.get("files", {}))
            rows: list[dict] = []
            for relative in sorted(paths):
                try:
                    local_path = safe_project_path(repo_root, relative)
                    local_pair = sha256_pair(local_path)
                except UpgradeError:
                    rows.append({"path": relative, "status": "unsafe"})
                    continue
                template_path = checkout / relative
                if relative not in template_owned:
                    rows.append({
                        "path": relative,
                        "status": "removed-upstream" if local_pair is not None else "removed-upstream-local-missing",
                    })
                    continue
                upstream_digest = normalized_sha256(template_path.read_bytes())
                if local_pair is None:
                    state = "not-installed"
                elif local_pair[1] == upstream_digest:
                    state = "matches-upstream"
                else:
                    state = "differs-from-upstream"
                rows.append({"path": relative, "status": state})
            return {
                "status": "compared",
                "ref": ref,
                "commit": commit,
                "apexRelease": current_manifest.get("apexRelease"),
                "files": rows,
            }
    except (UpgradeError, OSError, ValueError, subprocess.SubprocessError):
        return {"status": "unavailable", "reason": "upstream checkout or comparison failed"}


def inspect_template_drift(repo_root: Path, *, upstream: bool = False, ref: str | None = None) -> dict:
    """Return sanitized local state and an optional explicit upstream comparison."""
    root = Path(repo_root).resolve()
    report: dict = {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "localFiles": [],
        "upstreamComparison": {"status": "not-checked"},
        "conflicts": [],
    }
    try:
        manifest = load_manifest(root)
        lock, _lock_digest = read_lock_with_hash(root)
        validate_lock_ownership(lock, manifest)
    except (UpgradeError, OSError, ValueError) as error:
        report.update({"status": "unsafe", "reason": str(error)})
        return report

    has_lock = (root / ".template-lock.json").exists()
    original = _original_template(root, manifest)
    if not has_lock and original:
        report["lockStatus"] = "not-applicable"
    elif not has_lock:
        report["lockStatus"] = "unknown"
    else:
        report["lockStatus"] = "present"

    lock_release = lock.get("apexRelease")
    manifest_release = manifest.get("apexRelease")
    if lock_release is None or manifest_release is None:
        report["releaseStatus"] = "unknown"
    else:
        report["releaseStatus"] = "matches" if lock_release == manifest_release else "mismatch"

    if report["lockStatus"] == "present":
        for relative, expected in sorted(lock["files"].items()):
            state = _local_file_state(root, relative, expected)
            conflict = _conflict_state(root, relative)
            if conflict != "none":
                report["conflicts"].append({"path": relative + CONFLICT_SUFFIX, "status": conflict})
            report["localFiles"].append(state)

    if upstream:
        if ref is None:
            report["upstreamComparison"] = {"status": "unavailable", "reason": "--upstream requires --ref"}
        else:
            report["upstreamComparison"] = _compare_upstream(root, manifest, lock, ref)

    local_states = {row["status"] for row in report["localFiles"]}
    conflict_states = {row["status"] for row in report["conflicts"]}
    if "unsafe" in local_states | conflict_states or report["releaseStatus"] == "mismatch":
        report["status"] = "unsafe"
    elif (
        report["lockStatus"] == "unknown"
        or (report["lockStatus"] == "present" and report["releaseStatus"] == "unknown")
        or "missing" in local_states
        or "customized" in local_states
        or "pending" in conflict_states
    ):
        report["status"] = "attention"
    elif report["upstreamComparison"].get("status") == "unavailable":
        report["status"] = "attention"
    else:
        report["status"] = "current"
    return report


def _render_text(report: dict) -> str:
    lines = [
        f"Template status: {report.get('status', 'unsafe')}",
        f"Lock: {report.get('lockStatus', 'unsafe')}; release: {report.get('releaseStatus', 'unknown')}",
    ]
    for row in report.get("localFiles", []):
        lines.append(f"  {row['status']}: {row['path']}")
    for row in report.get("conflicts", []):
        lines.append(f"  conflict {row['status']}: {row['path']}")
    comparison = report.get("upstreamComparison", {})
    if comparison.get("status") == "not-checked":
        lines.append("Upstream: not checked (use --upstream --ref <ref> for an explicit comparison)")
    else:
        lines.append(f"Upstream: {comparison.get('status')}")
        if comparison.get("commit"):
            lines.append(f"  observed commit: {comparison['commit']}")
        for row in comparison.get("files", []):
            lines.append(f"  {row['status']}: {row['path']}")
        if comparison.get("reason"):
            lines.append(f"  reason: {comparison['reason']}")
    if report.get("reason"):
        lines.append(f"Reason: {report['reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--upstream", action="store_true", help="explicitly fetch and compare the manifest upstream")
    parser.add_argument("--ref", help="branch, tag, or commit to compare; required with --upstream")
    arguments = parser.parse_args(argv)
    if arguments.ref is not None and not arguments.upstream:
        parser.error("--ref requires --upstream")
    report = inspect_template_drift(arguments.repo_root, upstream=arguments.upstream, ref=arguments.ref)
    if arguments.format == "json":
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        print(_render_text(report))
    return {"current": 0, "attention": 1, "unsafe": 2}.get(report.get("status"), 2)


if __name__ == "__main__":
    raise SystemExit(main())
