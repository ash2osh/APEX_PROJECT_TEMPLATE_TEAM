#!/usr/bin/env python3
"""Offline, read-only plan for moving dated migration folders into feature files.

Every decision reuses an existing rule: the migration loader, the feature
stored-unit identity and DDL gate (one statement at a time), the SQL-script data
classifier, and the local evidence reader that migration-plan uses. The planner
writes nothing, reads no database and needs no .env.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import feature_manifest, feature_sources, migration_checks, migration_manifest as manifest, migration_plan, rollout
from .path_utils import relative_to_root
from .validate_migration import statement_spans


# Analysis identity for a flat migrations/ folder whose schema is unknown offline. It is only
# a stand-in for the analyser; every message that names it is rewritten before output.
PLACEHOLDER_SCHEMA = "UNRESOLVED_SCHEMA"
SCHEMA_LABEL = "<SCHEMA>"
SLUG_LABEL = "<slug>"
PLAIN_NAME_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
CONVERTIBLE = frozenset({"capture", "covered-by-capture", "ddl-form", "archive"})
STAYS = frozenset({"data", "refused"})
LIMITATIONS = (
    "Local evidence covers this checkout only; a folder applied to DEV without a local receipt or attempt looks unlocked.",
    "Capture reads DEV when it runs. The planner does not compare DEV with the migration or read the database.",
    "The planner does not split folders. A partly convertible folder stays in migrations/.",
    "A flat migrations/ folder's schema is unknown offline, so its verdict is provisional.",
    "Accepted DDL passed the freeze gate's static rules. That is not proof that a feature applies on DEV, staging or production.",
)


def _printable(value: object, limit: int = 240) -> str:
    text = "".join(character if character.isprintable() else repr(character)[1:-1] for character in str(value))
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _capture_argument(name: str) -> str:
    text = name if PLAIN_NAME_RE.fullmatch(name) else '"' + name.replace('"', '""') + '"'
    return shlex.quote(text)


def _capture_command(feature: str | None, group: str, name: str, schema: str | None) -> str:
    return (
        f"scripts/team.sh feature capture {feature or SLUG_LABEL} --{group} {_capture_argument(name)} "
        f"--schema {schema or SCHEMA_LABEL} --env dev"
    )


def _reason(message: object, provisional: bool) -> str:
    text = _printable(message, limit=600)
    return text.replace(PLACEHOLDER_SCHEMA, SCHEMA_LABEL) if provisional else text


def _with_terminator(text: str, start: int, end: int) -> str:
    """A statement's text with the ';' or standalone '/' that statement_spans leaves out."""
    terminator = text[end] if end < len(text) and text[end] in ";/" else ""
    return text[start:end] + terminator


def _excerpt(text: str, start: int, end: int) -> str:
    return _printable(" ".join(text[start:end].split()), limit=72)


def _statement_row(file_name: str, index: int, text: str, start: int, end: int, disposition: str, reason: str | None) -> dict[str, Any]:
    return {
        "file": file_name,
        "statement": index,
        "line": text.count("\n", 0, start) + 1,
        "excerpt": _excerpt(text, start, end),
        "disposition": disposition,
        "reason": reason,
    }


def _inspect_statement(file: manifest.MigrationFile, text: str, index: int, start: int, end: int, schema: str) -> dict[str, Any]:
    """What one statement is: a stored unit it creates or compiles, a compiler setting, or other SQL."""
    source = _with_terminator(text, start, end).encode("utf-8")
    row: dict[str, Any] = {
        "index": index, "start": start, "end": end, "source": source,
        "unit": None, "keyword": "", "setting": None, "problem": None,
    }
    tokens = migration_checks._leading_tokens(text[start:end])
    row["keyword"] = tokens[0].value if tokens and tokens[0].kind == "WORD" else ""
    row["setting"] = feature_manifest._setting_name(text[start:end])
    try:
        item = feature_manifest.FeatureInput(file.name, source, hashlib.sha256(source).hexdigest(), None, "ddl")
        units = migration_checks.compiled_units(feature_manifest._analysis_migration((item,), schema), schema)
    except (migration_checks.MigrationAnalysisError, feature_manifest.FeatureManifestError, ValueError) as error:
        row["problem"] = str(error)
        return row
    stored = [unit for unit in units if unit[3] and unit[1] in feature_manifest.STORED_SOURCE_TYPES]
    if stored:
        owner, object_type, name, _required = stored[0]
        if owner != schema:
            row["problem"] = f"stored unit owner {owner!r} is outside the folder schema {schema}"
        else:
            row["unit"] = (object_type, name)
    return row


def _statement_disposition(
    file: manifest.MigrationFile, row: Mapping[str, Any], *, schema: str, locked: bool, provisional: bool,
) -> tuple[str, str | None]:
    """Judge one statement that is not a stored unit or a covered setting, with the feature DDL gate."""
    if rollout.classify_auxiliary_step({"type": "sql-script"}, row["source"]) == "data":
        return "data", None
    item = feature_manifest.FeatureInput(file.name, row["source"], hashlib.sha256(row["source"]).hexdigest(), None, "ddl")
    try:
        feature_manifest._validate_ddl(item, schema)
    except (feature_manifest.FeatureManifestError, ValueError) as error:
        return "refused", _reason(error, provisional)
    return ("archive" if locked else "ddl-form"), None


def _analyse_folder(root: Path, migration: manifest.Migration, path: str, evidence: Mapping[str, Any], feature: str | None) -> dict[str, Any]:
    schema = migration.schema
    provisional = schema is None
    analysis_schema = schema or PLACEHOLDER_SCHEMA
    status = evidence.get("status")
    record: dict[str, Any] = {
        "path": path,
        "analysed": False,
        "problem": None,
        "schema": schema,
        "schemaSource": "directory" if schema is not None else "unresolved",
        "family": migration.family,
        "revision": migration.revision,
        "payloadDigest": migration.payload_digest,
        "localEvidence": {"status": status, "reasons": list(evidence.get("reasons", []))},
        "verdict": None,
        "provisional": provisional,
        "storedUnits": [],
        "statements": [],
        "dataOnlyFiles": [],
        "notes": [],
    }
    if status not in {"locked", "unlocked"}:
        record["problem"] = "local evidence is unknown; reconcile it before planning (scripts/team.sh revise --check <folder>)"
        return record
    locked = status == "locked"

    units: dict[tuple[str, str], dict[str, Any]] = {}
    statements: list[dict[str, Any]] = []
    try:
        for file in migration.files:
            text = file.source.decode("utf-8")
            inspected = [
                _inspect_statement(file, text, index, start, end, analysis_schema)
                for index, (start, end) in enumerate(statement_spans(text), start=1)
            ]
            unit_positions = [position for position, row in enumerate(inspected) if row["unit"] is not None and row["keyword"] == "CREATE"]
            for position, row in enumerate(inspected):
                if row["problem"] is not None:
                    disposition, reason = "refused", _reason(row["problem"], provisional)
                elif row["unit"] is not None:
                    object_type, name = row["unit"]
                    disposition, reason = ("capture" if row["keyword"] == "CREATE" else "covered-by-capture"), None
                    entry = units.setdefault(
                        (feature_sources.UNIT_GROUP_BY_KIND[object_type].name, name),
                        {"group": feature_sources.UNIT_GROUP_BY_KIND[object_type].name, "name": name, "kinds": []},
                    )
                    if object_type not in entry["kinds"]:
                        entry["kinds"].append(object_type)
                elif row["setting"] is not None and any(later > position for later in unit_positions):
                    # The capture writes the unit with DEV's own compiler settings, so a setting that
                    # precedes a unit in the same file is covered by that capture.
                    disposition, reason = "covered-by-capture", None
                else:
                    disposition, reason = _statement_disposition(
                        file, row, schema=analysis_schema, locked=locked, provisional=provisional,
                    )
                statements.append(_statement_row(file.name, row["index"], text, row["start"], row["end"], disposition, reason))
    except (ValueError, OSError) as error:
        record["problem"] = f"statements could not be analysed: {_reason(error, provisional)}"
        return record

    record["statements"] = statements
    record["storedUnits"] = [
        {
            "group": entry["group"],
            "name": entry["name"],
            "kinds": list(entry["kinds"]),
            "capture": _capture_command(feature, entry["group"], entry["name"], schema),
        }
        for entry in units.values()
    ]
    dispositions_by_file: dict[str, set[str]] = {}
    for row in statements:
        dispositions_by_file.setdefault(row["file"], set()).add(row["disposition"])
    record["dataOnlyFiles"] = [name for name, found in dispositions_by_file.items() if found == {"data"}]
    record["verdict"] = _verdict(statements)
    record["analysed"] = True
    record["notes"] = _notes(record, locked=locked, feature=feature)
    return record


def _verdict(statements: Sequence[Mapping[str, Any]]) -> str:
    if not statements:
        return "stays a migration"
    dispositions = {row["disposition"] for row in statements}
    if not dispositions & STAYS:
        return "convertible"
    if not dispositions & CONVERTIBLE:
        return "stays a migration"
    return "partly convertible"


def _notes(record: Mapping[str, Any], *, locked: bool, feature: str | None) -> list[str]:
    notes: list[str] = []
    if record["provisional"]:
        notes.append(
            "This flat migrations/ folder's schema is not known offline, so the verdict is provisional and the capture "
            f"commands use {SCHEMA_LABEL}. A schema-qualified name is judged against that unknown schema."
        )
    if record["verdict"] == "partly convertible":
        notes.append(
            "The folder stays active: it cannot be split, so anything copied or captured from it would also run from the migration."
        )
    if record["storedUnits"] and not locked:
        notes.append(
            "Capture reads DEV when it runs: run it after this folder's change is applied to DEV, or the captured source is older than the migration."
        )
    return notes


def _unanalysed(path: str, problem: str) -> dict[str, Any]:
    return {
        "path": path,
        "analysed": False,
        "problem": problem,
        "schema": None,
        "schemaSource": "unresolved",
        "family": None,
        "revision": None,
        "payloadDigest": None,
        "localEvidence": {"status": "unknown", "reasons": []},
        "verdict": None,
        "provisional": False,
        "storedUnits": [],
        "statements": [],
        "dataOnlyFiles": [],
        "notes": [],
    }


def _blocked_by_earlier_revision(records: Sequence[Mapping[str, Any]]) -> set[str]:
    """Convertible folders whose family has an earlier revision that is not convertible in this selection.

    archive-migrations archives only the contiguous low prefix of a family, so such a folder is not listed for it.
    """
    blocked: set[str] = set()
    for record in records:
        if record["verdict"] != "convertible":
            continue
        earlier = [
            other for other in records
            if other is not record and other["family"] == record["family"] and other["schema"] == record["schema"]
            and other["revision"] is not None and record["revision"] is not None and other["revision"] < record["revision"]
        ]
        if any(other["verdict"] != "convertible" for other in earlier):
            blocked.add(record["path"])
            record["notes"].append(
                "Not listed for archive-migrations: an earlier revision of this family stays a migration, and "
                "archive-migrations archives only the contiguous low prefix of a family."
            )
    return blocked


def convert_plan(repo_root: Path, folders: Sequence[str], *, feature: str | None = None) -> dict[str, Any]:
    """Plan the conversion of each selected migration folder. Reads files only; writes nothing."""
    root = Path(repo_root).resolve()
    diagnostics: list[dict[str, str | None]] = []
    selections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in folders:
        try:
            migration = manifest.load_migration(root, raw)
        except (manifest.MigrationManifestError, OSError, ValueError) as error:
            selections.append({"path": _printable(raw), "migration": None, "problem": _printable(str(error)).replace(str(root), "<repo>")})
            continue
        relative = relative_to_root(migration.folder, root).as_posix()
        if relative in seen:
            selections.append({"path": relative, "migration": None, "problem": "a migration folder may be selected only once"})
            continue
        seen.add(relative)
        selections.append({"path": relative, "migration": migration, "problem": None})

    evidence_by_path: dict[str, Mapping[str, Any]] = {}
    loadable = [item["path"] for item in selections if item["migration"] is not None]
    if loadable:
        plan = migration_plan.build_migration_plan(root, loadable)
        evidence_by_path = {record["path"]: record["localEvidence"] for record in plan["migrations"]}

    folder_records: list[dict[str, Any]] = []
    for item in selections:
        if item["migration"] is None:
            folder_records.append(_unanalysed(item["path"], item["problem"]))
            continue
        evidence = evidence_by_path.get(item["path"], {"status": "unknown", "reasons": ["local evidence was not read"]})
        folder_records.append(_analyse_folder(root, item["migration"], item["path"], evidence, feature))

    for record in folder_records:
        if not record["analysed"]:
            diagnostics.append({"code": "not-analysed", "path": record["path"], "message": record["problem"]})
    if not folders:
        diagnostics.append({"code": "no-folder", "path": None, "message": "select one or more migration folders"})

    blocked = _blocked_by_earlier_revision(folder_records)
    archive = [record["path"] for record in folder_records if record["verdict"] == "convertible" and record["path"] not in blocked]
    counts = {verdict: sum(1 for record in folder_records if record["verdict"] == verdict)
              for verdict in ("convertible", "partly convertible", "stays a migration")}
    not_analysed = sum(not record["analysed"] for record in folder_records)
    summary = {
        "folderCount": len(folder_records),
        "convertible": counts["convertible"],
        "partlyConvertible": counts["partly convertible"],
        "staysMigration": counts["stays a migration"],
        "notAnalysed": not_analysed,
    }
    return {
        "schemaVersion": 1,
        "mode": "offline",
        "feature": feature,
        "folders": folder_records,
        "archive": archive,
        "archiveCommand": ("scripts/team.sh archive-migrations " + " ".join(archive)) if archive else None,
        "summary": summary,
        "diagnostics": diagnostics,
        "limitations": list(LIMITATIONS),
        "exitCode": 0 if not_analysed == 0 and folder_records else 2,
    }


def exit_code(report: Mapping[str, Any]) -> int:
    return int(report["exitCode"])


_DISPOSITION_TEXT = {
    "capture": "stored unit; capture it with the command under Stored units",
    "covered-by-capture": "compiler setting or recompile of a stored unit; covered by its capture",
    "data": "data; stays a migration",
}


def _statement_text(row: Mapping[str, Any], feature: str | None, schema: str | None) -> str:
    where = f"{row['file']} #{row['statement']} (line {row['line']}) \"{row['excerpt']}\""
    disposition = row["disposition"]
    if disposition == "ddl-form":
        outcome = f"accepted; copy into features/{feature or SLUG_LABEL}/{schema or SCHEMA_LABEL}/ddl.sql"
    elif disposition == "archive":
        outcome = "accepted; the folder has local attempt or receipt evidence; archive, do not copy"
    elif disposition == "refused":
        outcome = f"refused; stays a migration: {row['reason']}"
    else:
        outcome = _DISPOSITION_TEXT[disposition]
    return f"    {where}: {outcome}"


def _folder_text(record: Mapping[str, Any], feature: str | None) -> list[str]:
    lines = [record["path"]]
    if not record["analysed"]:
        lines.append(f"  Not analysed: {record['problem']}")
        return lines
    schema = record["schema"]
    if schema is None:
        lines.append("  Schema: not known offline (flat migrations/ folder); capture commands use <SCHEMA>")
    else:
        lines.append(f"  Schema: {schema} (from the folder)")
    reasons = " ".join(record["localEvidence"]["reasons"])
    lines.append(f"  Local evidence: {record['localEvidence']['status']}" + (f". {reasons}" if reasons else ""))
    if record["storedUnits"]:
        lines.append("  Stored units (capture from DEV):")
        for unit in record["storedUnits"]:
            lines.append(f"    {unit['group']} {unit['name']}: {', '.join(unit['kinds'])}")
            lines.append(f"      {unit['capture']}")
    if record["statements"]:
        lines.append("  Statements:")
        lines.extend(_statement_text(row, feature, schema) for row in record["statements"])
    if record["dataOnlyFiles"]:
        lines.append("  Data-only files: " + ", ".join(record["dataOnlyFiles"]))
    lines.append(f"  Verdict: {record['verdict']}" + (" (provisional)" if record["provisional"] else ""))
    if record["notes"]:
        lines.append("  Notes:")
        lines.extend(f"    - {note}" for note in record["notes"])
    return lines


def _text(report: Mapping[str, Any]) -> str:
    feature = report["feature"]
    lines = [
        "Feature conversion plan (offline and read-only: nothing is written, captured or archived)",
        "",
    ]
    for record in report["folders"]:
        lines.extend(_folder_text(record, feature))
        lines.append("")
    lines.append("Archive (dry run by default; the archive command checks its own evidence):")
    if report["archiveCommand"]:
        lines.append(f"  {report['archiveCommand']}")
        lines.append(
            "  Before archiving, reconcile every old attempt (scripts/team.sh revise --check <folder>). One-time DDL from "
            "folders with local evidence is archived, not copied into a feature."
        )
    else:
        lines.append("  No folder is fully convertible, so there is nothing to archive.")
    lines.append("")
    summary = report["summary"]
    lines.append(
        f"Summary: {summary['folderCount']} folder(s): {summary['convertible']} convertible, "
        f"{summary['partlyConvertible']} partly convertible, {summary['staysMigration']} kept as a migration, "
        f"{summary['notAnalysed']} not analysed. Exit code {report['exitCode']}."
    )
    lines.append("Limits:")
    lines.extend(f"  - {limit}" for limit in report["limitations"])
    return "\n".join(lines)


def render(report: Mapping[str, Any], output_format: str) -> str:
    if output_format == "json":
        return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    return _text(report)
