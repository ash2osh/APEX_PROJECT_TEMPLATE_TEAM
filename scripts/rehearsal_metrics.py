"""Scoped numeric observations for rollback-only migration rehearsals."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .db_targets import Target
from .migration_checks import _check_driver_budget, _parse_check_output, render_in_session_check_driver
from .migration_manifest import MigrationManifestError, QueryCheck, RehearsalMetric


METRIC_PHASES = {"metric-before", "metric-after", "metric-rollback"}
IDENTITY_FIELDS = (
    "session_user", "current_schema", "db_name", "db_unique_name", "service_name",
    "container_id", "container_name", "edition", "database_version",
)
SAFE_JSON_INTEGER = 9007199254740991
ERROR_CODE_RE = re.compile(r"\b(?:ORA|SP2)-\d{4,5}\b")


@dataclass(frozen=True)
class MetricCapture:
    complete: bool
    identity: dict[str, str]
    values: dict[str, int]
    errors: tuple[str, ...]


def render_metric_driver(
    target: Target,
    metrics: Sequence[RehearsalMetric],
    phase: str,
) -> str:
    """Render metric SELECTs through the existing guarded DBMS_SQL scalar path."""
    if phase not in METRIC_PHASES:
        raise ValueError("metric phase must be metric-before, metric-after, or metric-rollback")
    if not metrics:
        raise ValueError("at least one rehearsal metric is required")
    checks = tuple(QueryCheck(metric.id, metric.sql, 1) for metric in metrics)
    driver = render_in_session_check_driver(target, checks, phase)
    if len(driver.encode("utf-8")) > _check_driver_budget():
        raise MigrationManifestError("rehearsal metric driver exceeds MIGRATION_CHECK_BATCH_BYTES")
    return driver


def _frame_error_codes(errors: Sequence[Mapping]) -> tuple[str, ...]:
    messages = []
    for error in errors:
        observed = error.get("observed")
        detail = observed.get("error") if isinstance(observed, Mapping) else None
        match = ERROR_CODE_RE.search(str(detail or ""))
        messages.append(match.group(0) if match else "metric capture is incomplete")
    return tuple(dict.fromkeys(messages))


def parse_metric_frame(
    output: str,
    metrics: Sequence[RehearsalMetric],
    phase: str,
    expected_identity: Mapping[str, str],
) -> MetricCapture:
    """Accept only one complete ordered frame on the exact expected database."""
    if phase not in METRIC_PHASES:
        return MetricCapture(False, {}, {}, ("unsupported metric phase",))
    if not metrics:
        return MetricCapture(False, {}, {}, ("no metrics were requested",))
    checks = tuple(QueryCheck(metric.id, metric.sql, 1) for metric in metrics)
    lines = output.splitlines()
    begin = f"CHECK_PAYLOAD_BEGIN:{phase}"
    end = f"CHECK_PAYLOAD_END:{phase}"
    sentinel = f"CHECK_VERIFIED:{phase}"
    if lines.count(begin) != 1 or lines.count(end) != 1 or lines.count(sentinel) != 1:
        return MetricCapture(False, {}, {}, ("metric result frame is missing, duplicated, or truncated",))
    start, finish, verified = lines.index(begin), lines.index(end), lines.index(sentinel)
    if not start < finish < verified:
        return MetricCapture(False, {}, {}, ("metric result frame is malformed",))

    report = _parse_check_output("\n".join(lines), checks, phase)
    identity = report.coverage.get("identity")
    if not isinstance(identity, dict):
        return MetricCapture(False, {}, {}, ("metric result has no database identity",))
    normalized_identity = {key: value for key, value in identity.items() if isinstance(value, str)}
    identity_errors = [
        "metric result database identity differs from the selected target"
        for field in IDENTITY_FIELDS
        if not str(expected_identity.get(field, "")).strip()
        or not str(normalized_identity.get(field, "")).strip()
        or expected_identity[field] != normalized_identity[field]
    ]
    if not report.complete:
        return MetricCapture(False, normalized_identity, {}, _frame_error_codes(report.errors))
    if identity_errors:
        return MetricCapture(False, normalized_identity, {}, tuple(dict.fromkeys(identity_errors)))

    values: dict[str, int] = {}
    errors: list[str] = []
    for metric, result in zip(metrics, report.results, strict=True):
        if (
            result.get("row_count") != 1
            or result.get("column_count") != 1
            or result.get("numeric") is not True
            or result.get("error")
        ):
            detail = str(result.get("error") or "metric must return exactly one numeric value")
            match = ERROR_CODE_RE.search(detail)
            errors.append(match.group(0) if match else "metric query failed or returned a non-scalar result")
            continue
        value = result.get("value")
        if type(value) is int:
            integer = value
        elif type(value) is float and math.isfinite(value) and value.is_integer():
            integer = int(value)
        else:
            errors.append("metric value is not a finite integer")
            continue
        if abs(integer) > SAFE_JSON_INTEGER:
            errors.append("metric value is outside the safe JSON integer range")
            continue
        values[metric.id] = integer

    if errors or len(values) != len(metrics):
        return MetricCapture(False, normalized_identity, {}, tuple(dict.fromkeys(errors or ["metric result set is incomplete"])))
    return MetricCapture(True, normalized_identity, values, ())


__all__ = ["MetricCapture", "parse_metric_frame", "render_metric_driver"]
