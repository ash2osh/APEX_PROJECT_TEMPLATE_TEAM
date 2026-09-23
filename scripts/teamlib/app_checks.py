"""Declared candidate-application checks for qualified non-production targets.

This module validates the reviewable check declarations and executes them only
through callbacks supplied by a qualified target adapter. It does not infer
application correctness from export equality or schema fingerprints.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from collections.abc import Callable, Mapping, Sequence

from .migration_bundle import BundleError, _validate_verify


class AppCheckError(RuntimeError):
    def __init__(self, message: str, report: AppCheckReport | None = None):
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class CheckResult:
    alias: str
    check_id: str
    page_id: int
    kind: str
    status: str
    expected_objects: tuple[str, ...] = ()
    diagnostic: str = ""
    observed: Mapping[str, Any] = None  # type: ignore[assignment]

    def as_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "check_id": self.check_id,
            "page_id": self.page_id,
            "kind": self.kind,
            "status": self.status,
            "expected_objects": list(self.expected_objects),
            "diagnostic": self.diagnostic,
            "observed": dict(self.observed or {}),
        }


@dataclass(frozen=True)
class AppCheckReport:
    source_commit: str
    target_identity: Mapping[str, Any]
    results: tuple[CheckResult, ...]
    source_digest: str
    checks_digest: str
    coverage: Mapping[str, Any]
    status: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "status": self.status,
            "source_commit": self.source_commit,
            "target_identity": dict(self.target_identity),
            "source_digest": self.source_digest,
            "checks_digest": self.checks_digest,
            "coverage": dict(self.coverage),
            "results": [item.as_dict() for item in self.results],
        }


@dataclass(frozen=True)
class AppCheckBundle:
    declarations: Mapping[str, Mapping[str, Any]]
    members: Mapping[str, bytes]
    checks_digest: str
    artifact_digest: str

    def member_bytes(self, relative: str) -> bytes:
        try:
            return self.members[relative]
        except KeyError as exc:
            raise AppCheckError(
                f"referenced check member is missing: {relative}"
            ) from exc


_ALIAS_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9._-]*$")
_ACTIONS = {"navigate", "fill", "click"}
_STATUS = {"PASS", "FAIL", "UNKNOWN"}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AppCheckError(f"{label} must be an object")
    return value


def _safe_relative(value: Any, label: str, suffix: str | None = None) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value or ".." in Path(value).parts:
        raise AppCheckError(f"{label} must be a safe relative path")
    if suffix and not value.endswith(suffix):
        raise AppCheckError(f"{label} must end with {suffix}")
    return value


def _load_declaration(alias: str, value: Any) -> dict[str, Any]:
    if isinstance(value, (str, Path)):
        path = Path(value)
        if path.is_symlink() or not path.is_file():
            raise AppCheckError(f"candidate declaration is not a regular file: {path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AppCheckError(f"candidate declaration is unreadable: {path}") from exc
    declaration = dict(_mapping(value, f"candidate declaration for {alias}"))
    if declaration.get("version") != 1:
        raise AppCheckError(f"candidate declaration for {alias} has unsupported version")
    declared_alias = declaration.get("alias")
    if declared_alias != alias or not isinstance(declared_alias, str) or not _ALIAS_RE.fullmatch(declared_alias):
        raise AppCheckError(f"candidate declaration alias does not match {alias}")
    pages = declaration.get("page_ids")
    if not isinstance(pages, list) or not pages or any(type(page) is not int or page <= 0 for page in pages) or len(set(pages)) != len(pages):
        raise AppCheckError(f"candidate declaration {alias} must declare unique positive page IDs")
    checks = declaration.get("checks")
    if not isinstance(checks, list) or not checks:
        raise AppCheckError(f"candidate declaration {alias} must contain checks")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for check_value in checks:
        check = dict(_mapping(check_value, f"check in {alias}"))
        check_id = check.get("id")
        if not isinstance(check_id, str) or not _CHECK_ID_RE.fullmatch(check_id) or check_id in seen:
            raise AppCheckError(f"candidate check IDs must be unique safe names: {alias}")
        seen.add(check_id)
        page_id = check.get("page_id")
        if type(page_id) is not int or page_id not in pages:
            raise AppCheckError(f"candidate check {alias}/{check_id} references an undeclared page")
        kind = check.get("kind")
        if kind not in {"select", "flow"}:
            raise AppCheckError(f"candidate check {alias}/{check_id} has unsupported kind")
        if kind == "select":
            _safe_relative(check.get("verify_sql"), f"verify SQL for {alias}/{check_id}", ".verify.sql")
            objects = check.get("expected_objects", [])
            if not isinstance(objects, list) or any(not isinstance(item, str) or not item.strip() for item in objects):
                raise AppCheckError(f"candidate check {alias}/{check_id} has invalid expected objects")
            sql = check.get("sql")
            if sql is not None:
                if not isinstance(sql, str):
                    raise AppCheckError(f"candidate check {alias}/{check_id} SQL must be text")
                try:
                    _validate_verify(sql, f"{alias}/{check_id}")
                except BundleError as exc:
                    raise AppCheckError(str(exc)) from exc
        else:
            _safe_relative(check.get("flow"), f"flow for {alias}/{check_id}", ".json")
            steps = check.get("steps")
            if not isinstance(steps, list) or not steps:
                raise AppCheckError(f"candidate flow {alias}/{check_id} must contain steps")
            has_expectation = False
            for step_value in steps:
                step = dict(_mapping(step_value, f"flow step in {alias}/{check_id}"))
                action = step.get("action")
                if action not in _ACTIONS:
                    raise AppCheckError(f"candidate flow {alias}/{check_id} contains an unsupported action")
                if action == "navigate":
                    path = step.get("path")
                    if not isinstance(path, str) or not path.startswith("/") or any(token in path for token in ("\n", "\r")):
                        raise AppCheckError(f"candidate navigate step {alias}/{check_id} needs a safe absolute path")
                else:
                    selector = step.get("selector")
                    if not isinstance(selector, str) or not selector.strip() or "\n" in selector or "\r" in selector:
                        raise AppCheckError(f"candidate action step {alias}/{check_id} needs a selector")
                if action == "fill":
                    has_value = isinstance(step.get("value"), str)
                    has_secret_ref = isinstance(step.get("test_secret_ref"), str) and bool(step["test_secret_ref"])
                    if has_value == has_secret_ref:
                        raise AppCheckError(f"candidate fill step {alias}/{check_id} needs value or test_secret_ref")
                    if any(key in step for key in ("password", "credential", "token", "secret")):
                        raise AppCheckError(f"candidate fill step {alias}/{check_id} contains an embedded credential")
                if isinstance(step.get("expected_visible_text"), str) and step["expected_visible_text"]:
                    has_expectation = True
                if isinstance(step.get("expected_url"), str) and step["expected_url"].startswith("/"):
                    has_expectation = True
            if not has_expectation:
                raise AppCheckError(f"candidate flow {alias}/{check_id} has no expected visible text or URL")
        normalized.append(check)
    if not any(item["kind"] == "select" for item in normalized):
        raise AppCheckError(f"candidate application {alias} has no SELECT dependency assertion")
    if not any(item["kind"] == "flow" for item in normalized):
        raise AppCheckError(f"candidate application {alias} has no page smoke flow")
    declaration["checks"] = normalized
    return declaration


def _validate_bundle_aliases(aliases: Sequence[str]) -> tuple[str, ...]:
    if isinstance(aliases, (str, bytes)):
        raise AppCheckError("application check bundle aliases must not be string or bytes")
    if not aliases:
        return ()
    selected = tuple(aliases)
    if any(not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias) for alias in selected):
        raise AppCheckError("application check bundle contains an unsafe alias")
    if len(set(selected)) != len(selected):
        raise AppCheckError("application check bundle contains duplicate aliases")
    return tuple(sorted(selected))


def _validate_bundle_members(members: Mapping[str, bytes]) -> dict[str, bytes]:
    if not isinstance(members, Mapping):
        raise AppCheckError("application check bundle members must be a mapping")
    validated: dict[str, bytes] = {}
    folded: dict[str, str] = {}
    for path, data in members.items():
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise AppCheckError(f"application check member must be a safe relative path: {path!r}")
        if not isinstance(data, bytes):
            raise AppCheckError(f"application check member must contain bytes: {path}")
        case_key = path.casefold()
        if case_key in folded and folded[case_key] != path:
            raise AppCheckError(
                f"application check members contain case-colliding paths: "
                f"{folded[case_key]}, {path}"
            )
        folded[case_key] = path
        validated[path] = bytes(data)
    return dict(sorted(validated.items()))


def _normalize_declaration_values(
    declarations: Mapping[str, Any], aliases: Sequence[str]
) -> dict[str, dict[str, Any]]:
    selected = set(aliases)
    supplied = set(declarations)
    missing = sorted(selected - supplied)
    extra = sorted(supplied - selected)
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append("missing declarations: " + ", ".join(missing))
        if extra:
            details.append("declarations for unknown applications: " + ", ".join(extra))
        raise AppCheckError(
            "candidate application coverage is incomplete (" + "; ".join(details) + ")"
        )
    normalized = {
        alias: _load_declaration(alias, declarations[alias])
        for alias in sorted(declarations)
    }
    # Detach the result from every caller-owned nested list and dictionary.
    return json.loads(_canonical(normalized).decode("utf-8"))


def _normalize_bundle_declarations(
    members: Mapping[str, bytes], aliases: Sequence[str]
) -> dict[str, dict[str, Any]]:
    declaration_members = {
        path.removesuffix(".json"): data
        for path, data in members.items()
        if "/" not in path and path.endswith(".json")
    }
    decoded: dict[str, Any] = {}
    for alias, data in declaration_members.items():
        try:
            decoded[alias] = json.loads(data.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AppCheckError(
                f"candidate declaration is unreadable: {alias}.json"
            ) from exc
    return _normalize_declaration_values(decoded, aliases)


def _validate_bundle_references(
    members: Mapping[str, bytes], declarations: Mapping[str, Mapping[str, Any]]
) -> None:
    for alias, declaration in declarations.items():
        for check in declaration["checks"]:
            field = "verify_sql" if check["kind"] == "select" else "flow"
            relative = str(check[field])
            if not relative.startswith(f"{alias}/"):
                raise AppCheckError(
                    f"referenced check member must belong to {alias}: {relative}"
                )
            if relative not in members:
                raise AppCheckError(
                    f"referenced check member is missing: {relative}"
                )
            raw = members[relative]
            try:
                text = raw.decode("utf-8")
            except UnicodeError as exc:
                raise AppCheckError(
                    f"referenced check member is not UTF-8: {relative}"
                ) from exc
            if check["kind"] == "select":
                try:
                    _validate_verify(text, f"{alias}/{check['id']}")
                except BundleError as exc:
                    raise AppCheckError(str(exc)) from exc
            else:
                try:
                    flow = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise AppCheckError(
                        f"referenced flow member is unreadable: {relative}"
                    ) from exc
                if not isinstance(flow, Mapping):
                    raise AppCheckError(
                        f"referenced flow member must be a JSON object: {relative}"
                    )


def _member_digest(members: Mapping[str, bytes]) -> str:
    records = [
        {
            "path": path,
            "length": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for path, data in sorted(members.items())
    ]
    return _digest(records)


def build_app_check_bundle(
    members: Mapping[str, bytes], aliases: Sequence[str]
) -> AppCheckBundle:
    selected = _validate_bundle_aliases(aliases)
    validated_members = _validate_bundle_members(members)
    declarations = _normalize_bundle_declarations(validated_members, selected)
    _validate_bundle_references(validated_members, declarations)
    return AppCheckBundle(
        declarations=declarations,
        members=validated_members,
        checks_digest=_digest(declarations),
        artifact_digest=_member_digest(validated_members),
    )


def _source_details(source: Any) -> tuple[str, Mapping[str, Any]]:
    if not isinstance(source, Mapping):
        raise AppCheckError("candidate source must include an exact commit and app map")
    commit = source.get("commit") or source.get("source_commit")
    apps = source.get("apps")
    if not isinstance(commit, str) or not commit or any(char.isspace() for char in commit):
        raise AppCheckError("candidate source commit is required")
    if not isinstance(apps, Mapping) or not apps:
        raise AppCheckError("candidate source must contain at least one application")
    return commit, apps


def _target_details(target: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(target, Mapping):
        raise AppCheckError("qualification target must be a contract mapping")
    if target.get("target_kind") != "persistent":
        raise AppCheckError("candidate application checks require a persistent qualification target")
    if target.get("environment") == "production":
        raise AppCheckError("candidate application checks refuse production targets")
    if target.get("role") not in {"integration", "test"}:
        raise AppCheckError("candidate application checks require an integration or test role")
    instance = target.get("instance_id")
    if not isinstance(instance, str) or not instance:
        raise AppCheckError("qualification target identity is incomplete")
    identity = {
        key: target[key]
        for key in (
            "target_kind", "role", "environment", "instance_id",
            "workspace_id", "app_ids", "state_key", "binding_digest",
        )
        if key in target
    }
    return instance, identity


def _runner(target: Mapping[str, Any], kind: str) -> Callable[..., Any] | None:
    value = target.get(f"{kind}_runner")
    return value if callable(value) else None


def _result(alias: str, check: Mapping[str, Any], callback: Callable[..., Any] | None) -> CheckResult:
    expected = tuple(check.get("expected_objects", ()))
    if callback is None:
        return CheckResult(alias, str(check["id"]), int(check["page_id"]), str(check["kind"]), "UNKNOWN", expected, "qualified check runner is unavailable", {})
    try:
        observed = callback(alias, check)
    except Exception as exc:
        return CheckResult(alias, str(check["id"]), int(check["page_id"]), str(check["kind"]), "FAIL", expected, f"check runner failed: {exc}", {})
    if isinstance(observed, bool):
        observed = {"status": "PASS" if observed else "FAIL"}
    if not isinstance(observed, Mapping):
        return CheckResult(alias, str(check["id"]), int(check["page_id"]), str(check["kind"]), "UNKNOWN", expected, "check runner returned no structured result", {})
    status = observed.get("status")
    if status not in _STATUS:
        return CheckResult(alias, str(check["id"]), int(check["page_id"]), str(check["kind"]), "UNKNOWN", expected, "check runner returned an invalid status", dict(observed))
    diagnostic = str(observed.get("diagnostic", ""))
    return CheckResult(alias, str(check["id"]), int(check["page_id"]), str(check["kind"]), status, expected, diagnostic, dict(observed))


def verify_candidate_apps(source: Mapping[str, Any], replay_target: Mapping[str, Any], checks: Mapping[str, Any] | Sequence[Any]) -> AppCheckReport:
    """Run all declared app checks on a persistent qualified target.

    A report is returned only when every required check passes.  On a failed
    or unknown result an :class:`AppCheckError` carries the complete report so
    CI can publish the failed app/page/object identities.
    """
    source_commit, apps = _source_details(source)
    _, identity = _target_details(replay_target)
    target_source_commit = replay_target.get("source_commit")
    if target_source_commit is not None and target_source_commit != source_commit:
        raise AppCheckError("qualification target was not prepared from the selected source commit")
    if isinstance(checks, Mapping):
        raw_declarations = dict(checks)
    elif isinstance(checks, Sequence) and not isinstance(checks, (str, bytes)):
        raw_declarations = {}
        for declaration in checks:
            if not isinstance(declaration, Mapping) or not isinstance(declaration.get("alias"), str):
                raise AppCheckError("candidate declarations must include aliases")
            raw_declarations[declaration["alias"]] = declaration
    else:
        raise AppCheckError("candidate checks must be an alias map or declaration list")
    source_aliases = set(apps)
    normalized = _normalize_declaration_values(
        raw_declarations, tuple(sorted(source_aliases))
    )
    app_ids = replay_target.get("app_ids")
    if isinstance(app_ids, Mapping) and any(alias not in app_ids for alias in source_aliases):
        raise AppCheckError("replay target has no application binding for every candidate app")
    fixture_ids = set(replay_target.get("fixture_ids", ())) if isinstance(replay_target.get("fixture_ids", ()), (list, tuple, set)) else set()
    results: list[CheckResult] = []
    for alias, declaration in normalized.items():
        required = declaration.get("required_fixture_ids", [])
        if not isinstance(required, list) or any(not isinstance(item, str) or not item for item in required):
            raise AppCheckError(f"candidate application {alias} has invalid required fixture IDs")
        missing_fixtures = sorted(set(required) - fixture_ids)
        if missing_fixtures:
            raise AppCheckError(f"candidate application {alias} is missing fixtures: {', '.join(missing_fixtures)}")
        for check in declaration["checks"]:
            kind = str(check["kind"])
            results.append(_result(alias, check, _runner(replay_target, kind)))
    status = "PASS" if results and all(item.status == "PASS" for item in results) else "FAIL"
    source_digest = _digest({"commit": source_commit, "apps": source})
    checks_digest = _digest(normalized)
    coverage = {
        "apps": sorted(source_aliases),
        "pages": {alias: declaration["page_ids"] for alias, declaration in normalized.items()},
        "checks": len(results),
        "unknown": sum(item.status == "UNKNOWN" for item in results),
    }
    report = AppCheckReport(source_commit, identity, tuple(results), source_digest, checks_digest, coverage, status)
    if status != "PASS":
        failures = [f"{item.alias}/{item.check_id}={item.status}" for item in results if item.status != "PASS"]
        raise AppCheckError("candidate application checks did not pass: " + ", ".join(failures), report)
    return report
