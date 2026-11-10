#!/usr/bin/env python3
"""`feature doctor`: ownership across features, orphan checks, and drift of feature files against DEV."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import baseline, feature_compare, feature_sources
from .db_targets import TargetResolutionError, resolve_target
from .feature_claims import Claim, Unanalysed, checked_objects, collect_claims, duplicates, feature_schema_dirs, same_feature_duplicates
from .feature_compare import compare


ACCEPTED_FILE = "accepted-differences.json"
STORED_PACKAGE_KINDS = ("PACKAGE", "PACKAGE BODY")
ERROR = "error"
WARNING = "warning"
INFO = "info"


class FeatureDoctorError(ValueError):
    """The doctor could not run or an input file is invalid."""


@dataclass(frozen=True)
class Finding:
    level: str
    code: str
    subject: str
    message: str

    def render(self) -> str:
        return f"{self.code:<17s} {self.subject}: {self.message}"


def accepted_path(root: Path, feature: str) -> Path:
    return root / "features" / feature / ACCEPTED_FILE


def load_accepted(root: Path, feature: str) -> dict[tuple[str, str, str], dict]:
    """Owner-decided differences of one feature, keyed by (schema, type, name)."""
    path = accepted_path(root, feature)
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise FeatureDoctorError(f"{ACCEPTED_FILE} of {feature} is unreadable: {error}") from error
    if not isinstance(document, list):
        raise FeatureDoctorError(f"{ACCEPTED_FILE} of {feature} must be a list")
    entries: dict[tuple[str, str, str], dict] = {}
    for position, entry in enumerate(document, start=1):
        fields = {"schema", "type", "name", "decision", "featureOnly", "liveOnly"}
        if not isinstance(entry, dict) or set(entry) != fields:
            raise FeatureDoctorError(f"{ACCEPTED_FILE} of {feature}, entry {position}: fields must be exactly {sorted(fields)}")
        if not all(isinstance(entry[field], str) and entry[field] for field in ("schema", "type", "name", "decision")):
            raise FeatureDoctorError(f"{ACCEPTED_FILE} of {feature}, entry {position}: schema, type, name and decision must be text")
        for field in ("featureOnly", "liveOnly"):
            if not isinstance(entry[field], list) or not all(isinstance(line, str) for line in entry[field]):
                raise FeatureDoctorError(f"{ACCEPTED_FILE} of {feature}, entry {position}: {field} must be a list of lines")
        key = (entry["schema"], entry["type"], entry["name"])
        if key in entries:
            raise FeatureDoctorError(f"{ACCEPTED_FILE} of {feature}: {key} is listed twice")
        entries[key] = entry
    return entries


def ownership_findings(claims: Sequence[Claim], selected: set[str] | None = None) -> list[Finding]:
    """An object claimed by more than one feature. With `selected`, only duplicates that involve a selected feature."""
    findings: list[Finding] = []
    for (schema, kind, name), values in sorted(duplicates(claims).items()):
        features = sorted({claim.feature for claim in values})
        if selected is not None and not selected.intersection(features):
            continue
        paths = ", ".join(sorted(claim.path for claim in values))
        findings.append(Finding(
            ERROR, "DOUBLE OWNERSHIP", f"{schema} {kind} {name}",
            f"held by {', '.join(features)}; keep one owner ({paths})",
        ))
    return findings


def same_feature_findings(claims: Sequence[Claim], selected: set[str] | None = None) -> list[Finding]:
    """An object defined by two different files of one feature. With `selected`, only those features."""
    findings: list[Finding] = []
    for (feature, (schema, kind, name)), values in sorted(same_feature_duplicates(claims).items()):
        if selected is not None and feature not in selected:
            continue
        paths = ", ".join(sorted({claim.path for claim in values}))
        findings.append(Finding(
            ERROR, "DUPLICATE FILE", f"{schema} {kind} {name}",
            f"two files of feature {feature} define it; keep one ({paths})",
        ))
    return findings


def unanalysed_findings(unanalysed: Sequence[Unanalysed], feature: str | None = None) -> list[Finding]:
    """A SQL file whose objects are unknown. Its ownership cannot be checked, so it is an error, not a note."""
    return [
        Finding(
            ERROR, "NOT ANALYSED", item.path,
            f"the objects this file defines are unknown, so ownership cannot be checked: {item.reason}",
        )
        for item in unanalysed
        if feature is None or item.feature == feature
    ]


def orphan_check_findings(root: Path, claims: Sequence[Claim], feature: str | None = None) -> list[Finding]:
    """A postcondition that names a stored unit this feature does not hold."""
    findings: list[Finding] = []
    held = {(claim.feature, claim.schema, claim.kind, claim.name) for claim in claims}
    for slug, schema, schema_dir in feature_schema_dirs(root, feature):
        checks_path = schema_dir / "checks.json"
        if not checks_path.is_file():
            continue
        try:
            document = json.loads(checks_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for section in ("preconditions", "postconditions"):
            for check in document.get(section, []) if isinstance(document, dict) else []:
                sql = check.get("sql") if isinstance(check, dict) else None
                if not isinstance(sql, str):
                    continue
                for kind, name in checked_objects(sql):
                    if kind in {"PACKAGE", "PACKAGE BODY", "TRIGGER", "FUNCTION", "PROCEDURE", "TYPE", "TYPE BODY"} and (slug, schema, kind, name.upper()) not in held:
                        findings.append(Finding(
                            WARNING, "ORPHAN CHECK", f"{slug}/{schema}",
                            f"check {check.get('id')!r} names {kind} {name}, which no file of this feature holds",
                        ))
    return findings


def _dev_profile_available(environ: Mapping[str, str], schema: str) -> bool:
    try:
        resolve_target(environ, "dev", "read", schema=schema)
    except TargetResolutionError:
        return False
    return True


def read_dev_packages(
    environ: Mapping[str, str], schema: str, names: Sequence[str],
) -> tuple[dict[tuple[str, str], str], set[str]]:
    """Read the DEV spec and body of the named packages (one session). Returns sources by (type, name) and the missing names."""
    simple = all(name == name.upper() and name.replace("_", "").replace("$", "").replace("#", "").isalnum() for name in names)
    sections = ("baseline-source", "baseline-settings")
    with tempfile.TemporaryDirectory(prefix="feature-doctor-") as scratch:
        try:
            catalog = baseline._capture(environ, "dev", schema, sections, Path(scratch), baseline_prefixes=list(names) if simple else [])
            baseline._complete(catalog, sections)
            identity = baseline._source_identity(catalog.get("identity"), "DEV package read")
        except (baseline.BaselineError, OSError, ValueError) as error:
            raise FeatureDoctorError(f"DEV package read failed: {error}") from error
    if identity["current_schema"].upper() != schema:
        raise FeatureDoctorError(f"DEV package read current schema {identity['current_schema']!r} does not match {schema}")
    sources: dict[tuple[str, str], str] = {}
    missing: set[str] = set()
    for name in names:
        try:
            for record in feature_sources._capture_sources("doctor", schema, name, catalog):
                sources[(record["type"], record["name"])] = record["source"].decode("utf-8")
        except feature_sources.FeatureSourcesError:
            missing.add(name)
    return sources, missing


def live_findings(
    root: Path,
    environ: Mapping[str, str],
    claims: Sequence[Claim],
    features: set[str] | None = None,
    schemas: set[str] | None = None,
) -> tuple[list[Finding], int, int]:
    """Compare the package files of the selected features with DEV. Returns findings, files compared, files accepted."""
    stored = [
        claim for claim in claims
        if claim.kind in STORED_PACKAGE_KINDS
        and (features is None or claim.feature in features)
        and (schemas is None or claim.schema in schemas)
    ]
    findings: list[Finding] = []
    compared = accepted_count = 0
    accepted_cache: dict[str, dict] = {}
    for schema in sorted({claim.schema for claim in stored}):
        in_schema = [claim for claim in stored if claim.schema == schema]
        names = sorted({claim.name for claim in in_schema})
        sources, missing = read_dev_packages(environ, schema, names)
        for claim in sorted(in_schema, key=lambda item: item.path):
            subject = f"{claim.path}"
            if claim.name in missing or (claim.kind, claim.name) not in sources:
                findings.append(Finding(ERROR, "NOT ON DEV", subject, f"{claim.kind} {claim.name} was not found in {schema} on DEV"))
                continue
            compared += 1
            comparison = compare((root / claim.path).read_text(encoding="utf-8"), sources[(claim.kind, claim.name)])
            if comparison.state == feature_compare.EQUAL:
                continue
            if claim.feature not in accepted_cache:
                accepted_cache[claim.feature] = load_accepted(root, claim.feature)
            entry = accepted_cache[claim.feature].get(claim.key)
            if entry is not None and entry["featureOnly"] == list(comparison.only_feature) and entry["liveOnly"] == list(comparison.only_live):
                accepted_count += 1
                findings.append(Finding(INFO, "ACCEPTED", subject, f"owner decision: {entry['decision']}"))
                continue
            note = " (a recorded accepted difference no longer matches)" if entry is not None else ""
            baseline_path = root / "scratch" / "feature-source" / claim.feature / claim.schema / "captured" / Path(claim.path).name
            file_text = (root / claim.path).read_text(encoding="utf-8")
            if baseline_path.is_file():
                drift = feature_compare.with_baseline(file_text, sources[(claim.kind, claim.name)], baseline_path.read_text(encoding="utf-8"))
                state = drift.state
                detail = (
                    f"DEV changed since the last capture: {len(drift.missing)} line(s) added on DEV are missing from the file, "
                    f"{len(drift.reintroduced)} line(s) removed on DEV are still in it{note}; a release would undo those DEV changes"
                )
            else:
                state = comparison.state
                detail = (
                    f"{len(comparison.only_feature)} line(s) only in the file, {len(comparison.only_live)} only on DEV{note}; "
                    "no retained DEV baseline to tell the file's own edits from DEV changes "
                    "(run `feature capture --refresh` once the file equals DEV)"
                )
            if state == feature_compare.EQUAL:
                continue
            if state == feature_compare.AHEAD:
                # New work written into the file and not compiled on DEV yet: normal, and no DEV change would be lost.
                findings.append(Finding(
                    WARNING, state, subject,
                    f"{len(comparison.only_feature)} line(s) only in the file, not on DEV yet{note}",
                ))
                continue
            findings.append(Finding(ERROR, state, subject, detail))
    return findings, compared, accepted_count


def doctor(
    root: Path,
    environ: Mapping[str, str],
    *,
    feature: str | None = None,
    live: bool = False,
) -> tuple[list[Finding], list[str]]:
    """Run the checks. Returns findings and plain summary lines (what was and was not checked)."""
    claims, unanalysed = collect_claims(root)
    selected = {feature} if feature else None
    findings = ownership_findings(claims, selected)
    findings.extend(same_feature_findings(claims, selected))
    findings.extend(unanalysed_findings(unanalysed, feature))
    findings.extend(orphan_check_findings(root, claims, feature))
    summary = [f"ownership: {len(claims)} claim(s) in {len({claim.feature for claim in claims})} feature(s)"]
    if live:
        schemas = sorted({claim.schema for claim in claims if claim.kind in STORED_PACKAGE_KINDS and (selected is None or claim.feature in selected)})
        if not schemas:
            summary.append("live: no package files selected, nothing to compare with DEV")
        else:
            usable = [schema for schema in schemas if _dev_profile_available(environ, schema)]
            if not usable:
                raise FeatureDoctorError(
                    "no DEV profile is configured for schema(s) " + ", ".join(schemas)
                    + " (set DB_ENVIRONMENT and the CODE_* profile); the live check did not run"
                )
            for schema in sorted(set(schemas) - set(usable)):
                findings.append(Finding(WARNING, "LIVE SKIPPED", schema, "no DEV profile for this schema; its package files were not compared"))
            live_list, compared, accepted = live_findings(root, environ, claims, selected, set(usable))
            findings.extend(live_list)
            summary.append(f"live: {compared} package file(s) compared with DEV, {accepted} accepted difference(s)")
    else:
        summary.append("live: not run (use --live to compare package files with DEV)")
    return findings, summary


def render(findings: Sequence[Finding], summary: Sequence[str]) -> str:
    lines = [finding.render() for finding in findings if finding.level != INFO or finding.code == "ACCEPTED"]
    lines.extend(summary)
    errors = sum(1 for finding in findings if finding.level == ERROR)
    warnings = sum(1 for finding in findings if finding.level == WARNING)
    lines.append(f"result: {errors} error(s), {warnings} warning(s)")
    return "\n".join(lines)


def exit_code(findings: Sequence[Finding]) -> int:
    return 1 if any(finding.level == ERROR for finding in findings) else 0
