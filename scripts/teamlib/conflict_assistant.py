"""Read-only, human-facing explanations for retained export conflicts."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import json
from pathlib import Path
import re
from typing import Any, Mapping


class ConflictAssistantError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConflictQuestion:
    path: str
    property: str
    head_value: str
    shared_value: str
    question: str


@dataclass(frozen=True)
class ConflictBriefing:
    recovery_id: str
    paths: tuple[str, ...]
    questions: tuple[ConflictQuestion, ...]
    text: str
    raw_fallbacks: tuple[str, ...] = ()


def _tree(data: Any, root: Path) -> dict[str, bytes]:
    if isinstance(data, dict):
        result: dict[str, bytes] = {}
        for path, value in data.items():
            if isinstance(value, str):
                result[path] = value.encode("utf-8")
            elif isinstance(value, bytes):
                result[path] = value
        return result
    if not isinstance(data, list):
        raise ConflictAssistantError("recovery tree is malformed")
    result = {}
    for record in data:
        if not isinstance(record, dict):
            raise ConflictAssistantError("recovery file record is malformed")
        path = record.get("path")
        if not isinstance(path, str):
            raise ConflictAssistantError("recovery path is malformed")
        blob = root.parent.parent / "blobs" / record.get("sha256", "")
        if not blob.is_file():
            raise ConflictAssistantError(f"retained conflict blob is missing: {path}")
        value = blob.read_bytes()
        if len(value) != record.get("length"):
            raise ConflictAssistantError(f"retained conflict blob is corrupt: {path}")
        result[path] = value
    return result


def _properties(value: bytes) -> dict[str, str] | None:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    properties: dict[str, str] = {}
    for line in text.splitlines():
        match = re.search(r"\b(page|region|item|button|subscription|title|label|name|symbol)\b\s*[:=]\s*(.+)$", line, re.IGNORECASE)
        if match:
            properties[match.group(1).casefold()] = match.group(2).strip()
    return properties or None


def _changed(before: Mapping[str, str], after: Mapping[str, str]) -> set[str]:
    return {
        key for key in set(before) | set(after)
        if before.get(key, "<absent>") != after.get(key, "<absent>")
    }


def _load_capture(recovery_id: str, root: Path) -> tuple[dict[str, Any], Path]:
    path = root / "recovery" / recovery_id / "capture.json"
    if path.is_symlink() or not path.is_file():
        raise ConflictAssistantError(f"recovery bundle not found: {recovery_id}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConflictAssistantError("recovery bundle is unreadable") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ConflictAssistantError("unsupported recovery bundle version")
    return data, path.parent


def explain_conflict(recovery_id: str, *, root: str | Path = ".sync-state") -> ConflictBriefing:
    data, bundle = _load_capture(recovery_id, Path(root))
    diagnostics = data.get("diagnostics", {})
    paths = tuple(sorted(diagnostics.get("conflicts", []))) if isinstance(diagnostics, dict) else ()
    base = _tree(data.get("base", {}), bundle)
    source_base = _tree(data.get("source_base", {}), bundle)
    head = _tree(data.get("head", data.get("head_tree", {})), bundle)
    mine = _tree(data.get("mine", {}), bundle)
    if not paths:
        paths = tuple(sorted(set(base) | set(source_base) | set(head) | set(mine)))
    questions: list[ConflictQuestion] = []
    raw: list[str] = []
    independent_paths: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    for path in paths:
        head_value = head.get(path, b"<absent>")
        shared_value = mine.get(path, b"<absent>")
        head_props = _properties(head_value)
        shared_props = _properties(shared_value)
        base_props = _properties(base.get(path, b"<absent>"))
        source_base_props = _properties(source_base.get(path, b"<absent>"))
        if head_props is None or shared_props is None or base_props is None or source_base_props is None:
            raw.append(path)
            continue
        head_changed = _changed(source_base_props, head_props)
        shared_changed = _changed(base_props, shared_props)
        independent_changes = (head_changed - shared_changed) | (shared_changed - head_changed)
        if independent_changes:
            independent_paths.append((path, tuple(sorted(head_changed - shared_changed)), tuple(sorted(shared_changed - head_changed))))
        for property_name in sorted(head_changed & shared_changed):
            left = head_props.get(property_name, "<absent>")
            right = shared_props.get(property_name, "<absent>")
            if left != right:
                questions.append(ConflictQuestion(path, property_name, left, right, f"For {path}, keep the committed {property_name}, the shared application value, or provide both values?"))
    lines = [f"Recovery {recovery_id} contains an export conflict with the shared application.", "The capture side is the shared application, not the operator's own change."]
    if paths:
        lines.append("Conflicted paths: " + ", ".join(paths))
    for question in questions:
        lines.append(f"{question.path}: {question.property}: committed={question.head_value!r}; shared application={question.shared_value!r}. {question.question}")
    for path, committed_only, shared_only in independent_paths:
        lines.append(f"{path}: independent properties detected (committed-only={', '.join(committed_only) or '<none>'}; shared-only={', '.join(shared_only) or '<none>'}). Review a candidate that preserves both before resolving; no value has been selected.")
    for path in raw:
        lines.append(f"{path}: the APEXlang structure could not be parsed safely; inspect the retained base/source/HEAD/shared comparison.")
    lines.append("No value has been selected and no tracked source has been written.")
    return ConflictBriefing(recovery_id, paths, tuple(questions), "\n".join(lines), tuple(raw))


def write_candidate(briefing: ConflictBriefing, answers: Mapping[str, str], *, out_root: str | Path = "scratch") -> Path:
    if set(answers) != set(briefing.paths):
        raise ConflictAssistantError("every conflicted path requires an explicit answer")
    root = Path(out_root)
    if ".." in root.parts:
        raise ConflictAssistantError("conflict assistant output must remain under scratch/")
    if root.name != "scratch" and root.parent.name != "scratch":
        raise ConflictAssistantError("conflict assistant output must remain under scratch/")
    destination = root / f"conflict-{briefing.recovery_id}"
    destination.mkdir(parents=True, exist_ok=False)
    for path, value in answers.items():
        if path.startswith("/") or ".." in Path(path).parts or "\\" in path:
            raise ConflictAssistantError(f"unsafe conflict output path: {path}")
        file_path = destination / path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(value, encoding="utf-8", newline="\n")
    return destination


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="explain-conflict")
    parser.add_argument("recovery_id")
    parser.add_argument("--root", default=".sync-state")
    args = parser.parse_args(list(argv or []))
    print(explain_conflict(args.recovery_id, root=args.root).text)
    return 0
