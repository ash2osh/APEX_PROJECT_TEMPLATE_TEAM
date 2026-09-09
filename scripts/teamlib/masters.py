"""APEXlang master/subscription parsing and contract qualification."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import uuid
from typing import Any
from collections.abc import Callable, Mapping

from .config import Target
from .sqlcl import SqlclError, run_sqlcl


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


_COMPONENT_VIEWS = {
    "authentication": ("APEX_APPLICATION_AUTH", "AUTHENTICATION_SCHEME_NAME"),
    "authorization": ("APEX_APPLICATION_AUTHORIZATION", "AUTHORIZATION_SCHEME_NAME"),
    "theme": ("APEX_APPLICATION_THEMES", "STATIC_ID"),
}


def _sql_literal(value: str) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise MasterError("master lookup value is invalid")
    return "'" + value.replace("'", "''") + "'"


def apex_component_resolver(
    *,
    runner: Callable[..., Any] = run_sqlcl,
    work_root: str | Path | None = None,
) -> Callable[[MasterReference, Mapping[str, Any], Target], bool]:
    """Return a resolver that proves master identity through the target APEX views.

    The resolver deliberately supports only the APEX 26.1 component views that
    are qualified by this template.  An unknown component type is refused
    rather than silently reduced to an application-ID check.
    """

    root = Path(work_root) if work_root is not None else Path("scratch") / "master-checks"

    def resolve(reference: MasterReference, entry: Mapping[str, Any], target: Target) -> bool:
        if bool(entry.get("builtin", False)):
            # Built-in masters are contracted explicitly.  APEX's built-in
            # theme is resolved by the target engine rather than an
            # application row, so only the contracted type/symbol is accepted.
            return reference.master_app_id == 0 and reference.component_type.casefold() == "theme"
        view_spec = _COMPONENT_VIEWS.get(reference.component_type.casefold())
        if view_spec is None:
            return False
        view, name_column = view_spec
        expected_workspace = entry.get("workspace_id")
        alias = entry.get("alias")
        if not isinstance(expected_workspace, int) or expected_workspace <= 0 or not isinstance(alias, str) or not alias:
            return False
        work = root / uuid.uuid4().hex
        work.mkdir(parents=True, exist_ok=False)
        driver = work / "master-check.sql"
        driver.write_text(
            "SET DEFINE OFF\n"
            "SET HEADING OFF\n"
            "SET FEEDBACK OFF\n"
            "SET PAGESIZE 0\n"
            "SET LINESIZE 32767\n"
            "SELECT 'TEAM_MASTER_APP|' || COUNT(*) || '|' ||\n"
            f"       NVL(MAX(CASE WHEN workspace_id = {expected_workspace} THEN '1' ELSE '0' END), '0') || '|' ||\n"
            f"       NVL(MAX(CASE WHEN UPPER(alias) = UPPER({_sql_literal(alias)}) THEN '1' ELSE '0' END), '0')\n"
            "  FROM APEX_APPLICATIONS\n"
            f" WHERE application_id = {reference.master_app_id};\n"
            "SELECT 'TEAM_MASTER_COMPONENT|' || COUNT(*)\n"
            f"  FROM {view}\n"
            f" WHERE application_id = {reference.master_app_id}\n"
            f"   AND UPPER({name_column}) = UPPER({_sql_literal(reference.symbol)});\n",
            encoding="utf-8",
            newline="\n",
        )
        try:
            result = runner(target, "read", driver, work)
        except (OSError, SqlclError):
            return False
        app_row = re.search(r"(?:^|\n)TEAM_MASTER_APP\|(\d+)\|([01])\|([01])\s*$", result.stdout, re.MULTILINE)
        component_row = re.search(r"(?:^|\n)TEAM_MASTER_COMPONENT\|(\d+)\s*$", result.stdout, re.MULTILINE)
        if not app_row or not component_row:
            return False
        return (
            int(app_row.group(1)) == 1
            and app_row.group(2) == "1"
            and app_row.group(3) == "1"
            and int(component_row.group(1)) >= 1
        )

    return resolve
