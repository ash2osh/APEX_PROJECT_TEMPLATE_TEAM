"""APEXlang master/subscription parsing and contract qualification."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Callable, Mapping

from .config import Target


class MasterError(ValueError):
    """Raised when a source subscription cannot be qualified."""


@dataclass(frozen=True)
class MasterReference:
    source_path: str
    master_app_id: int
    symbol: str
    component_type: str


@dataclass(frozen=True)
class MasterReport:
    valid: bool
    references: tuple[MasterReference, ...]
    checked_components: tuple[dict[str, Any], ...]
    contract_digest: str


def _mask_comments_and_strings(text: str) -> str:
    output: list[str] = []
    index = 0
    state = "normal"
    quote = ""
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if state == "normal":
            if char == "/" and next_char == "/":
                output.extend((" ", " "))
                index += 2
                state = "line-comment"
                continue
            if char == "/" and next_char == "*":
                output.extend((" ", " "))
                index += 2
                state = "block-comment"
                continue
            if char in {"'", '"'}:
                quote = char
                output.append(" ")
                index += 1
                state = "string"
                continue
            output.append(char)
            index += 1
            continue
        if state == "line-comment":
            if char == "\n":
                output.append("\n")
                state = "normal"
            else:
                output.append(" ")
            index += 1
            continue
        if state == "block-comment":
            if char == "*" and next_char == "/":
                output.extend((" ", " "))
                index += 2
                state = "normal"
            else:
                output.append("\n" if char == "\n" else " ")
                index += 1
            continue
        # A quoted SQL/APEXlang value is opaque. Handle doubled quote escapes.
        if char == quote:
            if next_char == quote:
                output.extend((" ", " "))
                index += 2
            else:
                output.append(" ")
                index += 1
                state = "normal"
        else:
            output.append("\n" if char == "\n" else " ")
            index += 1
    if state in {"block-comment", "string"}:
        raise MasterError("unterminated comment or quoted value in APEXlang")
    return "".join(output)


_SUBSCRIPTION_RE = re.compile(
    r"\bsubscription\s*\{\s*master\s*:\s*@/(\d+)/([A-Za-z][A-Za-z0-9_-]*)\s*\}",
    re.IGNORECASE | re.DOTALL,
)
_SUBSCRIPTION_START_RE = re.compile(r"\bsubscription\s*\{", re.IGNORECASE)
_COMPONENT_RE = re.compile(
    r"\b(authentication|authorization|theme|plugin|process|region)\b"
    r"[^\{\n]{0,120}\{\s*$",
    re.IGNORECASE,
)


def parse_subscriptions(source_tree: Mapping[str, bytes]) -> tuple[MasterReference, ...]:
    """Extract symbolic master references while ignoring comments/strings."""
    references: list[MasterReference] = []
    for path in sorted(source_tree):
        data = source_tree[path]
        if not isinstance(data, bytes):
            raise MasterError(f"source file is not bytes: {path}")
        if not path.casefold().endswith((".apx", ".apex")):
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MasterError(f"APEXlang source is not UTF-8: {path}") from exc
        masked = _mask_comments_and_strings(text)
        matches = list(_SUBSCRIPTION_RE.finditer(masked))
        starts = list(_SUBSCRIPTION_START_RE.finditer(masked))
        if len(matches) != len(starts):
            raise MasterError(f"unsupported or malformed subscription syntax in {path}")
        for match in matches:
            prefix = masked[max(0, match.start() - 300):match.start()]
            component_match = _COMPONENT_RE.search(prefix)
            component_type = component_match.group(1).casefold() if component_match else "unknown"
            references.append(
                MasterReference(
                    source_path=path,
                    master_app_id=int(match.group(1)),
                    symbol=match.group(2),
                    component_type=component_type,
                )
            )
    return tuple(references)


def _contract_digest(contract: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _component_entries(contract: Mapping[str, Any]) -> dict[tuple[int, str, str], dict[str, Any]]:
    if contract.get("version") != 1 or not isinstance(contract.get("masters"), list):
        raise MasterError("master contract version or masters list is invalid")
    entries: dict[tuple[int, str, str], dict[str, Any]] = {}
    for master in contract["masters"]:
        if not isinstance(master, dict):
            raise MasterError("master contract entry is malformed")
        app_id = master.get("app_id")
        alias = master.get("alias")
        workspace_id = master.get("workspace_id")
        builtin = bool(master.get("builtin", False))
        if not isinstance(app_id, int) or app_id < 0 or not isinstance(alias, str) or not alias:
            raise MasterError("master contract identity is malformed")
        if workspace_id is not None and (not isinstance(workspace_id, int) or workspace_id <= 0):
            raise MasterError("master contract workspace identity is malformed")
        if app_id == 0 and not builtin:
            raise MasterError("application 0 is reserved for explicitly built-in masters")
        components = master.get("components")
        if not isinstance(components, list) or not components:
            raise MasterError(f"master {alias} has no component contract")
        for component in components:
            if not isinstance(component, dict):
                raise MasterError("master component contract is malformed")
            component_type = component.get("type")
            symbol = component.get("symbol")
            if not isinstance(component_type, str) or not component_type or not isinstance(symbol, str) or not symbol:
                raise MasterError("master component type/symbol is malformed")
            key = (app_id, component_type.casefold(), symbol)
            if key in entries:
                raise MasterError(f"duplicate master component contract: {key}")
            entries[key] = {**master, **component}
    return entries


def validate_masters(
    source_tree: Mapping[str, bytes],
    target: Target,
    contract: Mapping[str, Any],
    *,
    component_resolver: Callable[[MasterReference, Mapping[str, Any], Target], bool] | None = None,
) -> MasterReport:
    references = parse_subscriptions(source_tree)
    entries = _component_entries(contract)
    checked: list[dict[str, Any]] = []
    for reference in references:
        key = (reference.master_app_id, reference.component_type.casefold(), reference.symbol)
        entry = entries.get(key)
        if entry is None:
            # An unknown component type can never be accepted by an ID-only
            # lookup; callers must make the source syntax explicit.
            raise MasterError(
                f"master component is not contracted: {reference.master_app_id}/{reference.component_type}/{reference.symbol}"
            )
        expected_workspace = entry.get("workspace_id")
        if expected_workspace is not None and expected_workspace <= 0:
            raise MasterError("master workspace identity is invalid")
        if component_resolver is not None and not component_resolver(reference, entry, target):
            raise MasterError(
                f"target cannot resolve master component {reference.master_app_id}/{reference.symbol}"
            )
        checked.append(
            {
                "master_app_id": reference.master_app_id,
                "alias": entry.get("alias"),
                "workspace_id": expected_workspace,
                "component_type": reference.component_type,
                "symbol": reference.symbol,
                "builtin": bool(entry.get("builtin", False)),
            }
        )
    return MasterReport(True, references, tuple(checked), _contract_digest(contract))
