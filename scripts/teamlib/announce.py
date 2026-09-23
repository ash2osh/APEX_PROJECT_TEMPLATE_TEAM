"""Read-only import pause and verified-only all-clear drafting.

Drafting an announcement is diagnostic and informational; it does not authorize
or perform application publishing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from collections.abc import Mapping, Sequence

from .publish import format_publish_notice


@dataclass(frozen=True)
class Announcement:
    text: str
    observed_state: Mapping[str, Any]
    findings: tuple[str, ...]
    duration: str


def draft_import_announcement(
    alias: str,
    commit: str,
    *,
    target: Mapping[str, Any],
    roster: Sequence[str] = (),
    changed_paths: Sequence[str] = (),
    planned_paths: Sequence[str] = (),
    operator: str = "the import operator",
    duration: str | None = None,
) -> Announcement:
    """Draft an informational pause notice for an application."""
    required = ("app_id", "workspace_id", "instance_id")
    if any(key not in target for key in required):
        raise ValueError("target identity is unknown; cannot draft a reassuring import announcement")
    findings = tuple(sorted(set(changed_paths)))
    planned = tuple(sorted(set(planned_paths)))
    duration_text = duration or "unknown"
    lines = [
        f"PAUSE NOTICE: {operator} is preparing to import application {target['app_id']} ({alias}) in workspace {target['workspace_id']} on {target['instance_id']}.",
        f"Selected commit: {commit}. Everyone must stop editing in App Builder now and must not save changes until an all-clear is posted.",
        f"Expected duration: {duration_text}. An all-clear will follow only after the import and verified re-export complete.",
    ]
    if planned:
        lines.append("The selected source will replace these owned paths: " + ", ".join(planned) + ".")
    if findings:
        lines.append(
            "The fresh observed capture differs from the verified baseline at: "
            + ", ".join(findings)
            + ". Export/reconcile this work before proceeding if it is not already accounted for."
        )
    else:
        lines.append(
            "The fresh observed capture has no named differences from the verified baseline at this moment; "
            "this snapshot is not a guarantee about edits made after the draft."
        )
    if roster:
        lines.append("Registered checkouts: " + ", ".join(roster))
    return Announcement("\n".join(lines), dict(target), findings, duration_text)


def draft_all_clear(alias: str, result: Mapping[str, Any]) -> str:
    """Draft an all-clear notice after verified import of a single application."""
    if result.get("verified") is not True and result.get("status") not in {"success", "verified"}:
        raise ValueError("all-clear requires a verified import result")
    recovery = result.get("recovery_path", "the retained recovery bundle")
    return (
        f"ALL CLEAR: verified import of {alias} completed and the application was re-exported successfully. "
        f"Editing may resume now. Recovery evidence remains at {recovery}."
    )


def draft_publish_all_clear(
    aliases: Sequence[str],
    results: Mapping[str, Mapping[str, Any]],
) -> str:
    """Draft an all-clear notice only when all selected applications verified successfully."""
    if not aliases:
        raise ValueError("all-clear requires at least one application alias")
    for alias in aliases:
        res = results.get(alias)
        if not res or (res.get("verified") is not True and res.get("status") not in {"success", "verified"}):
            raise ValueError(f"all-clear requires verified result for '{alias}'; publishing incomplete or unknown")

    lines = [
        f"ALL CLEAR: verified import of selected applications ({', '.join(sorted(aliases))}) completed successfully.",
        "Editing in App Builder may resume now for these applications.",
        "Recovery evidence:",
    ]
    for alias in sorted(aliases):
        res = results[alias]
        recovery = res.get("recovery_path", "the retained recovery bundle")
        lines.append(f"  - {alias}: {recovery}")
    return "\n".join(lines)


__all__ = [
    "Announcement",
    "draft_all_clear",
    "draft_import_announcement",
    "draft_publish_all_clear",
    "format_publish_notice",
]
