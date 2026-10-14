"""Pure composition helpers for generated structure, grant, and code baselines."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .migration_manifest import CHECK_ID_RE, SQL_FILE_RE, validate_check_query
from .migration_manifest import validate_sql_only


CATEGORY_ORDER = ("structure", "grants", "code")
BUNDLE_NAME_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z", re.ASCII)


class BaselineBundleError(ValueError):
    """A generated baseline component cannot safely join a combined bundle."""


@dataclass(frozen=True)
class BaselineComponent:
    kind: str
    steps: tuple[tuple[str, str], ...]
    preconditions: tuple[dict, ...]
    postconditions: tuple[dict, ...]


@dataclass(frozen=True)
class BundledBaseline:
    steps: dict[str, str]
    checks: dict
    provenance: tuple[dict, ...]


def validate_bundle_name(name: str) -> str:
    """Return a valid lowercase ASCII migration slug, or refuse it."""
    if not isinstance(name, str) or len(name) > 64 or BUNDLE_NAME_RE.fullmatch(name) is None:
        raise BaselineBundleError(
            "bundle name must be an ASCII lowercase slug matching "
            "[a-z][a-z0-9]*(?:-[a-z0-9]+)* and be at most 64 characters"
        )
    return name


def _ordered_steps(component: BaselineComponent) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(component.steps, Sequence) or isinstance(component.steps, (str, bytes)):
        raise BaselineBundleError(f"{component.kind} component steps must be a sequence")

    parsed: list[tuple[int, str, str, str]] = []
    for step in component.steps:
        if not isinstance(step, Sequence) or isinstance(step, (str, bytes)) or len(step) != 2:
            raise BaselineBundleError(f"{component.kind} component steps must contain filename and SQL pairs")
        filename, sql = step
        if not isinstance(filename, str):
            raise BaselineBundleError(f"{component.kind} component has a non-string SQL filename")
        match = SQL_FILE_RE.fullmatch(filename)
        if match is None:
            raise BaselineBundleError(f"{component.kind} component has an invalid SQL filename: {filename!r}")
        sequence = int(match.group("sequence"))
        if sequence == 0:
            raise BaselineBundleError(f"{component.kind} component SQL numbering must start at 001")
        if not isinstance(sql, str):
            raise BaselineBundleError(f"{component.kind} component SQL must be text: {filename}")
        if "\r" in sql or sql.startswith("\ufeff"):
            raise BaselineBundleError(f"{component.kind} component SQL must use UTF-8 without a BOM and LF line endings: {filename}")
        try:
            sql.encode("utf-8")
            validate_sql_only(sql)
        except (UnicodeError, ValueError) as error:
            raise BaselineBundleError(f"{component.kind} component SQL is invalid in {filename}: {error}") from error
        parsed.append((sequence, filename, match.group("name"), sql))

    actual = tuple(item[0] for item in parsed)
    expected = tuple(range(1, len(parsed) + 1))
    if actual != expected:
        raise BaselineBundleError(
            f"{component.kind} component SQL numbering must be consecutive from 001; found {actual}"
        )
    return tuple((filename, slug, sql) for _sequence, filename, slug, sql in parsed)


def _append_checks(
    values: object,
    kind: str,
    phase: str,
    seen_ids: set[str],
    output: list[dict],
) -> None:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise BaselineBundleError(f"{kind} component {phase} must be a sequence")
    for index, check in enumerate(values, start=1):
        if not isinstance(check, Mapping) or set(check) != {"id", "sql", "expected"}:
            raise BaselineBundleError(f"{kind} component {phase}[{index}] must contain exactly id, sql, and expected")
        check_id, sql, expected = check["id"], check["sql"], check["expected"]
        if not isinstance(check_id, str) or CHECK_ID_RE.fullmatch(check_id) is None:
            raise BaselineBundleError(f"{kind} component {phase}[{index}] has an invalid check id")
        if check_id in seen_ids:
            raise BaselineBundleError(f"{kind} component contains duplicate check id {check_id!r}")
        if not isinstance(sql, str):
            raise BaselineBundleError(f"{kind} component check {check_id} SQL must be a string")
        if type(expected) is not int or expected != 1:
            raise BaselineBundleError(f"{kind} component check {check_id} expected value must be integer 1")
        try:
            validate_check_query(sql)
        except ValueError as error:
            raise BaselineBundleError(f"{kind} component check {check_id} is invalid: {error}") from error
        seen_ids.add(check_id)
        output.append({"id": f"{kind}-{check_id}", "sql": sql, "expected": expected})


def combine_baseline_components(components: Sequence[BaselineComponent]) -> BundledBaseline:
    """Compose eligible generated components without reading or writing external state."""
    if not isinstance(components, Sequence) or isinstance(components, (str, bytes)):
        raise BaselineBundleError("baseline components must be a sequence")

    by_kind: dict[str, BaselineComponent] = {}
    for component in components:
        if not isinstance(component, BaselineComponent):
            raise BaselineBundleError("baseline components must use BaselineComponent values")
        if component.kind not in CATEGORY_ORDER:
            raise BaselineBundleError(f"unsupported baseline component kind: {component.kind!r}")
        if component.kind in by_kind:
            raise BaselineBundleError(f"duplicate baseline component kind: {component.kind}")
        by_kind[component.kind] = component

    ordered: dict[str, tuple[tuple[str, str, str], ...]] = {}
    for kind in CATEGORY_ORDER:
        component = by_kind.get(kind)
        if component is None:
            continue
        ordered[kind] = _ordered_steps(component)
        if kind in {"grants", "code"} and component.preconditions:
            raise BaselineBundleError(f"{kind} component preconditions cannot move across the combined initial boundary")

    step_count = sum(len(ordered.get(kind, ())) for kind in CATEGORY_ORDER)
    if step_count > 999:
        raise BaselineBundleError("combined baseline bundle cannot contain more than 999 SQL steps")

    steps: dict[str, str] = {}
    provenance: list[dict] = []
    preconditions: list[dict] = []
    postconditions: list[dict] = []
    for kind in CATEGORY_ORDER:
        component = by_kind.get(kind)
        if component is None:
            continue
        seen_ids: set[str] = set()
        _append_checks(component.preconditions, kind, "preconditions", seen_ids, preconditions)
        _append_checks(component.postconditions, kind, "postconditions", seen_ids, postconditions)
        for original_name, slug, sql in ordered[kind]:
            generated_name = f"{len(steps) + 1:03d}-{kind}-{slug}.sql"
            steps[generated_name] = sql
            provenance.append(
                {
                    "kind": kind,
                    "originalName": original_name,
                    "generatedName": generated_name,
                    "sqlSha256": hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                }
            )

    return BundledBaseline(
        steps=steps,
        checks={"preconditions": preconditions, "postconditions": postconditions},
        provenance=tuple(provenance),
    )
