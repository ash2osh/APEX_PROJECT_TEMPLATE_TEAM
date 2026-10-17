"""Report model and rendering for DEV test runs (Task 2).

Public API
----------
new_report(run, action, suite_id, scenario_ids, origin) -> dict
    - Build a fresh, schema-compliant report dictionary.
    - run: RunEvidence | None (None for offline/test construction).
    - action: 'plan' | 'run'.
    - suite_id: suite identifier string.
    - scenario_ids: list of selected scenario ID strings.
    - origin: 'builtin' | 'project'.
    - Returns a plain JSON-compatible dict with all required root fields set
      to safe initial values.

validate_report(value: object) -> dict
    - Validate a report dict against the Task 2 schema contract.
    - Raises ValueError on any contract violation.
    - Returns the validated dict (same object).

aggregate_exit(report: Mapping) -> int
    - Derive the correct process exit code from the report.
    - Exit 0: all selected execution scenarios PASSED; or plan with complete/
              eligible coverage; or supported cleanup COMPLETE with no failures.
    - Exit 1: at least one FAILED scenario, no higher-priority conditions.
    - Exit 2: refusal, blocked/incomplete/unknown coverage, or
              incomplete/refused cleanup.  Takes precedence over 1.
    - Exit 130: interrupted before any scenario/cleanup write may have started.

render_report(report: Mapping, format: str) -> str
    - Render report to 'text', 'json', or 'markdown'.
    - 'text' / 'markdown': sanitized; never echoes inputHashes paths or
      sensitive bindings.
    - Raises ValueError for unsupported format strings.

save_report(run: RunEvidence, report: Mapping) -> None
    - Write report.json and report.md to the run directory atomically.

No SQL executed; no database connections; stdlib only.  Requires Python 3.10+.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.dev_test_evidence import RunEvidence

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

_VALID_ACTIONS = frozenset({"plan", "run"})
_VALID_SCENARIO_STATUSES = frozenset(
    {"PASSED", "FAILED", "BLOCKED", "NOT_RUN", "UNKNOWN"}
)
_VALID_CLEANUP_STATUSES = frozenset(
    {"NOT_REQUESTED", "COMPLETE", "INCOMPLETE", "REFUSED", "UNSUPPORTED"}
)
_VALID_ORIGINS = frozenset({"builtin", "project"})
_VALID_RENDER_FORMATS = frozenset({"text", "json", "markdown"})

# Required root fields (spec section 10 + controller clarification)
_REQUIRED_ROOT_FIELDS = frozenset({
    "schemaVersion",
    "runId",
    "action",
    "startedAt",
    "finishedAt",
    "source",
    "selection",
    "target",
    "prerequisites",
    "scenarios",
    "inventory",
    "cleanup",
    "diagnostics",
    "limitations",
    "coverage",
})

# Required scenario record fields
_REQUIRED_SCENARIO_FIELDS = frozenset(
    {"id", "status", "eligible", "steps", "checks", "observations", "effects"}
)

# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def new_report(
    run: RunEvidence | None,
    action: str,
    suite_id: str,
    scenario_ids: list[str],
    origin: str,
) -> dict:
    """Build a fresh, schema-compliant report dictionary.

    Args:
        run:          RunEvidence handle (may be None for offline construction).
        action:       'plan' or 'run'.
        suite_id:     The selected suite identifier.
        scenario_ids: List of selected scenario IDs (may be empty = all).
        origin:       'builtin' or 'project'.

    Returns:
        A JSON-compatible dict with all required root fields.

    Notes:
        - runId is taken from run.run_id when run is not None, else a placeholder.
        - startedAt is set to the current UTC ISO-8601 timestamp.
        - finishedAt is initially None (set by the caller when the run completes).
        - cleanup.status starts as NOT_REQUESTED.
        - source.gitSha / dirty / inputHashes start as None / False / {}.
        - target.configured / observed / versions start as None / None / {}.
        - coverage.complete and eligible start as False.
    """
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = run.run_id if run is not None else "OFFLINE"

    return {
        "schemaVersion": SCHEMA_VERSION,
        "runId": run_id,
        "action": action,
        "startedAt": now_iso,
        "finishedAt": None,
        "source": {
            "gitSha": None,
            "dirty": False,
            "inputHashes": {},
        },
        "selection": {
            "suiteId": suite_id,
            "scenarioIds": list(scenario_ids),
            "origin": origin,
        },
        "target": {
            "configured": None,
            "observed": None,
            "versions": {},
        },
        "prerequisites": [],
        "scenarios": [],
        "inventory": {
            "objects": [],
            "rows": [],
        },
        "cleanup": {
            "status": "NOT_REQUESTED",
            "history": [],
        },
        "diagnostics": {},
        "limitations": [],
        "coverage": {
            "complete": False,
            "eligible": False,
        },
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_report(value: object) -> dict:
    """Validate a report dict against the schema contract.

    Args:
        value: The object to validate (should be a dict).

    Returns:
        The validated dict (same object).

    Raises:
        ValueError: On any contract violation.
        TypeError: If value is not a dict.
    """
    if not isinstance(value, dict):
        raise ValueError(
            f"Report must be a dict, got {type(value).__name__}"
        )

    # --- Required root fields ---
    for field in _REQUIRED_ROOT_FIELDS:
        if field not in value:
            raise ValueError(f"Report missing required field: {field!r}")

    # --- schemaVersion ---
    sv = value["schemaVersion"]
    _require(
        isinstance(sv, int) and not isinstance(sv, bool) and sv == SCHEMA_VERSION,
        f"schemaVersion must be integer {SCHEMA_VERSION}, got {sv!r}",
    )

    # --- action ---
    action = value["action"]
    _require(
        isinstance(action, str) and action in _VALID_ACTIONS,
        f"action must be one of {sorted(_VALID_ACTIONS)}, got {action!r}",
    )

    # --- runId ---
    _require(
        isinstance(value["runId"], str) and value["runId"],
        "runId must be a non-empty string",
    )

    # --- cleanup ---
    cleanup = value["cleanup"]
    _require(isinstance(cleanup, dict), "cleanup must be a dict")
    _require("status" in cleanup, "cleanup.status is required")
    cs = cleanup["status"]
    _require(
        isinstance(cs, str) and cs in _VALID_CLEANUP_STATUSES,
        f"cleanup.status must be one of {sorted(_VALID_CLEANUP_STATUSES)}, got {cs!r}",
    )

    # --- scenarios ---
    scenarios = value["scenarios"]
    _require(isinstance(scenarios, list), "scenarios must be a list")
    for i, sc in enumerate(scenarios):
        _require(isinstance(sc, dict), f"scenarios[{i}] must be a dict")
        for f in _REQUIRED_SCENARIO_FIELDS:
            _require(f in sc, f"scenarios[{i}] missing required field: {f!r}")
        status = sc["status"]
        _require(
            isinstance(status, str) and status in _VALID_SCENARIO_STATUSES,
            f"scenarios[{i}].status must be one of "
            f"{sorted(_VALID_SCENARIO_STATUSES)}, got {status!r}",
        )

    # --- source, selection, target, coverage (basic presence) ---
    for sub_field, required_keys in [
        ("source", {"gitSha", "dirty", "inputHashes"}),
        ("selection", {"suiteId", "scenarioIds", "origin"}),
        ("target", {"configured", "observed", "versions"}),
        ("coverage", {"complete", "eligible"}),
    ]:
        sub = value[sub_field]
        _require(isinstance(sub, dict), f"{sub_field} must be a dict")
        for k in required_keys:
            _require(k in sub, f"{sub_field}.{k} is required")

    _require(isinstance(value["startedAt"], str), "startedAt must be text")
    _require(value["finishedAt"] is None or isinstance(value["finishedAt"], str), "finishedAt must be text or null")
    _require(type(value["source"]["dirty"]) is bool, "source.dirty must be boolean")
    _require(value["source"]["gitSha"] is None or isinstance(value["source"]["gitSha"], str), "source.gitSha must be text or null")
    _require(isinstance(value["source"]["inputHashes"], dict), "source.inputHashes must be an object")
    selection = value["selection"]
    _require(selection["origin"] in {"builtin", "project"}, "selection.origin is invalid")
    _require(isinstance(selection["suiteId"], str), "selection.suiteId must be text")
    ids = selection["scenarioIds"]
    _require(isinstance(ids, list) and all(isinstance(item, str) for item in ids), "selection.scenarioIds must be text IDs")
    _require(len(ids) == len(set(ids)), "selection.scenarioIds must be unique")
    for key in ("complete", "eligible"):
        _require(type(value["coverage"][key]) is bool, f"coverage.{key} must be boolean")
    target = value["target"]
    for key in ("configured", "observed"):
        _require(target[key] is None or isinstance(target[key], dict), f"target.{key} must be an object or null")
    _require(isinstance(target["versions"], dict), "target.versions must be an object")
    _require(isinstance(value["inventory"], dict), "inventory must be an object")
    for key in ("objects", "rows"):
        _require(isinstance(value["inventory"].get(key), list), f"inventory.{key} must be an array")
    for key in ("prerequisites", "limitations"):
        _require(isinstance(value[key], list), f"{key} must be an array")
    _require(isinstance(value["diagnostics"], (dict, list)), "diagnostics must contain structured entries")
    for item in scenarios:
        _require(isinstance(item["id"], str) and bool(item["id"]), "scenario.id must be text")
        _require(type(item["eligible"]) is bool, "scenario.eligible must be boolean")
        for key in ("steps", "checks", "observations"):
            _require(isinstance(item[key], list), f"scenario.{key} must be an array")
        _require(isinstance(item["effects"], (dict, list)), "scenario.effects must contain structured effects")
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Report must contain finite JSON values") from exc

    return value


def validate_completed_report(value: object) -> dict:
    """Validate that a report represents a fully completed and verified run or plan.

    In addition to structural validation (via validate_report), requires:
      - finishedAt is a non-empty string.
      - target.observed is present and not None.
      - For action == 'run': scenarios is non-empty, and all selection.scenarioIds
        are present in scenarios.
      - For action == 'plan': coverage.complete and coverage.eligible are True.

    Raises:
        ValueError: If any completion evidence is missing or invalid.
    """
    report = validate_report(value)

    finished_at = report.get("finishedAt")
    _require(
        isinstance(finished_at, str) and bool(finished_at),
        "Completed report requires non-empty finishedAt timestamp",
    )

    observed = (report.get("target") or {}).get("observed")
    _require(_identity_complete(observed), "Completed report requires full observed target identity")
    coverage = report["coverage"]
    _require(coverage.get("complete") is True and coverage.get("eligible") is True,
             "Completed report requires complete and eligible coverage")

    action = report.get("action")
    if action == "run":
        scenarios = report.get("scenarios") or []
        _require(
            bool(scenarios),
            "Completed run report requires at least one executed scenario",
        )
        selected_ids = (report.get("selection") or {}).get("scenarioIds") or []
        executed_ids = [sc.get("id") for sc in scenarios]
        _require(bool(selected_ids) and len(executed_ids) == len(set(executed_ids))
                 and len(selected_ids) == len(set(selected_ids)) and set(selected_ids) == set(executed_ids),
                 "Completed run requires exactly one result for each selected scenario")
    elif action == "plan":
        coverage = report.get("coverage") or {}
        _require(
            bool(coverage.get("complete")) and bool(coverage.get("eligible")),
            "Completed plan report requires complete and eligible coverage",
        )

    return report


def _identity_complete(value: object) -> bool:
    fields = ("session_user", "current_schema", "db_name", "db_unique_name", "service_name",
              "container_id", "container_name", "edition", "database_version")
    return isinstance(value, dict) and all(isinstance(value.get(key), str) and value[key].strip() for key in fields)


def _diagnostic_info(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    result: dict = {}
    messages = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code", ""))
        if code.upper() == "INTERRUPTED":
            result["interrupted"] = True
            started = entry.get("writeStarted", True) is not False
            result["writeStarted"] = result.get("writeStarted", False) or started
        if _is_sensitive_key(code):
            continue
        if isinstance(entry.get("message"), str):
            messages.append(entry["message"])
    if messages:
        result["messages"] = "; ".join(messages)
    return result


# ---------------------------------------------------------------------------
# Exit code
# ---------------------------------------------------------------------------


def aggregate_exit(report: Mapping) -> int:
    """Return the process exit code for this report.

    Priority (highest first):
      130 — interrupted before any write started
        2 — refusal, blocked/incomplete/unknown/not-run coverage,
            incomplete/refused cleanup, unobserved target identity,
            missing finishedAt evidence, empty eligible run,
            or selected scenarios missing from executed records.
        1 — at least one FAILED scenario (complete evidence, no higher condition)
        0 — all selected scenarios PASSED (or eligible plan, or COMPLETE cleanup)

    Args:
        report: A validated (or compatible) report dict.

    Returns:
        int: 0, 1, 2, or 130.
    """
    # --- Exit 130: pre-write interrupt ---
    diag = _diagnostic_info(report.get("diagnostics"))
    if isinstance(diag, dict):
        if diag.get("interrupted") and not diag.get("writeStarted", True):
            return 130

    action = report.get("action", "run")
    scenarios: list[dict] = list(report.get("scenarios") or [])
    cleanup_status = (report.get("cleanup") or {}).get("status", "NOT_REQUESTED")

    # Incomplete/refused cleanup => exit 2
    if cleanup_status in ("INCOMPLETE", "REFUSED", "UNSUPPORTED"):
        return 2

    try:
        validate_completed_report(dict(report))
    except (ValueError, TypeError, KeyError):
        return 2

    # Missing finish evidence or unobserved target identity refuses exit 0
    finished_at = report.get("finishedAt")
    if not finished_at or not isinstance(finished_at, str):
        return 2

    observed = (report.get("target") or {}).get("observed")
    if observed is None:
        return 2

    # --- Plan action ---
    if action == "plan":
        coverage = report.get("coverage") or {}
        if coverage.get("complete") and coverage.get("eligible"):
            return 0
        return 2

    # --- Run action ---
    if not scenarios:
        return 2

    # Verify all selected scenarios were executed
    selected_ids = (report.get("selection") or {}).get("scenarioIds") or []
    executed_ids = {sc.get("id") for sc in scenarios}
    if any(sid not in executed_ids for sid in selected_ids):
        return 2

    statuses = [sc.get("status") for sc in scenarios]

    has_unknown = any(s == "UNKNOWN" for s in statuses)
    has_blocked = any(s == "BLOCKED" for s in statuses)
    has_not_run = "NOT_RUN" in statuses
    interrupted_post = isinstance(diag, dict) and diag.get("interrupted") and diag.get("writeStarted", False)

    if has_unknown or has_blocked or has_not_run or interrupted_post:
        return 2

    has_failed = any(s == "FAILED" for s in statuses)
    all_passed = all(s == "PASSED" for s in statuses)

    if all_passed:
        return 0

    if has_failed:
        return 1

    return 2


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_report(report: Mapping, format: str) -> str:
    """Render a report to a human-readable or machine-readable string.

    Args:
        report: The report dict (plain dict or Mapping).
        format: 'text', 'json', or 'markdown'.

    Returns:
        The rendered string.

    Raises:
        ValueError: For unsupported format values.

    Notes on sanitization:
        'text' and 'markdown' formats do not echo inputHashes path strings,
        raw database sample rows, or credentials/tokens from diagnostics.
        Raw log files are never auto-sanitized; see spec section 10.
    """
    if format not in _VALID_RENDER_FORMATS:
        raise ValueError(
            f"Unsupported render format {format!r}; "
            f"must be one of {sorted(_VALID_RENDER_FORMATS)}"
        )

    report_dict = dict(report)

    if format == "json":
        return json.dumps(report_dict, indent=2, default=str) + "\n"

    if format in ("text", "markdown"):
        return _render_summary(report_dict, as_markdown=(format == "markdown"))

    raise ValueError(f"Unhandled format: {format!r}")  # pragma: no cover


_SENSITIVE_KEY_RE = None


def _is_sensitive_key(key: str) -> bool:
    global _SENSITIVE_KEY_RE
    if _SENSITIVE_KEY_RE is None:
        import re
        _SENSITIVE_KEY_RE = re.compile(
            r"(?:pass(?:word)?|token|secret|cred(?:ential)?|auth|cookie|key|bearer)",
            re.IGNORECASE,
        )
    return bool(_SENSITIVE_KEY_RE.search(key))


def _render_summary(report: dict, *, as_markdown: bool) -> str:
    """Render a sanitized summary report.

    Sanitization: inputHashes paths, sample database rows, and sensitive
    credentials/tokens from diagnostics are excluded. Identifiers, counts,
    paths and diagnostic codes are safe to output.
    """
    lines: list[str] = []
    action = report.get("action", "?")
    run_id = report.get("runId", "?")
    selection = report.get("selection") or {}
    suite_id = selection.get("suiteId", "?")
    origin = selection.get("origin", "?")
    scenarios = list(report.get("scenarios") or [])
    cleanup = report.get("cleanup") or {}
    cleanup_status = cleanup.get("status", "NOT_REQUESTED")
    coverage = report.get("coverage") or {}
    diag = _diagnostic_info(report.get("diagnostics"))
    limitations = list(report.get("limitations") or [])
    started_at = report.get("startedAt", "?")
    finished_at = report.get("finishedAt", "")

    # Counts
    counts: dict[str, int] = {}
    for sc in scenarios:
        s = sc.get("status", "UNKNOWN")
        counts[s] = counts.get(s, 0) + 1

    exit_code = aggregate_exit(report)

    # Sanitize diagnostics for display
    sanitized_diag_notes = []
    if isinstance(diag, dict):
        if diag.get("interrupted"):
            sanitized_diag_notes.append("interrupted")
        for k in sorted(diag):
            if k not in ("interrupted", "writeStarted") and not _is_sensitive_key(k):
                sanitized_diag_notes.append(f"{k}={diag[k]}")

    if as_markdown:
        lines.append("# DEV Test Report")
        lines.append("")
        lines.append(f"**Run ID:** `{run_id}`")
        lines.append(f"**Action:** {action}")
        lines.append(f"**Suite:** {suite_id} ({origin})")
        lines.append(f"**Started:** {started_at}")
        if finished_at:
            lines.append(f"**Finished:** {finished_at}")
        lines.append("")
        lines.append("## Scenario Outcomes")
        lines.append("")
        if scenarios:
            lines.append("| ID | Status | Eligible |")
            lines.append("|---|---|---|")
            for sc in scenarios:
                sc_id = sc.get("id", "?")
                sc_status = sc.get("status", "?")
                sc_eligible = sc.get("eligible", "?")
                lines.append(f"| `{sc_id}` | {sc_status} | {sc_eligible} |")
        else:
            lines.append("_No scenarios recorded._")
        lines.append("")
        lines.append("## Summary")
        lines.append("")
        summary_parts = [f"{v} {k}" for k, v in sorted(counts.items())]
        lines.append(", ".join(summary_parts) if summary_parts else "No scenarios.")
        lines.append("")
        lines.append(f"**Coverage:** {'complete' if coverage.get('complete') else 'incomplete'}")
        lines.append(f"**Cleanup:** {cleanup_status}")
        lines.append("")
        if limitations:
            lines.append("## Limitations")
            lines.append("")
            for lim in limitations:
                lines.append(f"- {lim}")
            lines.append("")
        if diag.get("interrupted"):
            lines.append("> **Interrupted** — see run directory for evidence.")
            lines.append("")
        if sanitized_diag_notes:
            lines.append(f"**Diagnostics:** {', '.join(sanitized_diag_notes)}")
            lines.append("")
        lines.append(f"**Exit:** {exit_code}")
        lines.append("")
    else:
        # Text format
        lines.append(f"DEV Test: {action.upper()} / {suite_id}")
        summary_parts = [f"{v} {k.lower()}" for k, v in sorted(counts.items())]
        lines.append("Scenarios: " + (", ".join(summary_parts) if summary_parts else "none"))
        complete_str = "complete" if coverage.get("complete") else "incomplete"
        lines.append(f"Coverage: {complete_str}")
        lines.append(f"Cleanup: {cleanup_status}")
        if limitations:
            for lim in limitations:
                lines.append(f"  [limitation] {lim}")
        if diag.get("interrupted"):
            lines.append("  [INTERRUPTED] see run directory for evidence")
        if sanitized_diag_notes:
            lines.append(f"Diagnostics: {', '.join(sanitized_diag_notes)}")
        lines.append(f"Exit: {exit_code}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def save_report(run: RunEvidence, report: Mapping) -> None:
    """Write report.json and report.md to the run directory atomically.

    Args:
        run:    The RunEvidence handle.
        report: The report dict to save.

    Raises:
        OSError: On filesystem errors.
    """
    from scripts.dev_test_evidence import _atomic_write_json

    report_dict = dict(report)
    validate_report(report_dict)
    if report_dict["runId"] != run.run_id:
        raise ValueError("Report identity does not match its run directory")

    # Write JSON atomically
    _atomic_write_json(run.run_dir / "report.json", report_dict)

    # Write Markdown atomically
    md_content = render_report(report_dict, "markdown")
    md_target = run.run_dir / "report.md"
    _atomic_write_json_text(md_target, md_content)


def _atomic_write_json_text(target: Path, content: str) -> None:
    """Write a text string to *target* atomically (temp-file + rename)."""
    from scripts.dev_test_evidence import _atomic_write_bytes
    _atomic_write_bytes(target, content.encode("utf-8"))
