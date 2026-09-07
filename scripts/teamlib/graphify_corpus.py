"""Optional, read-only corpus selection for an alias-keyed Graphify index."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from graphify_apexlang_extractor import extract_apexlang


_ALIAS = re.compile(r"^[a-z][a-z0-9-]*$")


def _alias(path: Path, root: Path) -> str | None:
    relative = path.relative_to(root)
    if len(relative.parts) >= 2 and relative.parts[0] == "apps":
        alias = relative.parts[1]
        if not _ALIAS.fullmatch(alias):
            raise ValueError(f"application directory must use a lowercase alias: {alias}")
        return alias
    if len(relative.parts) >= 2 and relative.parts[0] == "app_context":
        alias = relative.parts[1]
        if not _ALIAS.fullmatch(alias):
            raise ValueError(f"context directory must use a lowercase alias: {alias}")
        return alias
    return None


def _ignored(relative: Path) -> bool:
    parts = relative.parts
    if not parts or parts[0] not in {"apps", "database", "app_context"}:
        return True
    if any(part in {"deployments", ".apex", "static-files", "logs", ".sync-state", "scratch"} for part in parts):
        return True
    return False


def collect_corpus(root: str | Path) -> list[dict[str, Any]]:
    base = Path(root)
    if not base.is_dir() or base.is_symlink():
        raise ValueError("Graphify root must be a real directory")
    nodes: list[dict[str, Any]] = []
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(base)
        if _ignored(relative):
            continue
        alias = _alias(path, base)
        if alias is None:
            continue
        if path.suffix.casefold() == ".apx":
            result = extract_apexlang(path)
            if result.get("error"):
                raise ValueError(str(result["error"]))
            for node in result.get("nodes", []):
                node = dict(node)
                node["alias"] = alias
                node["relative_path"] = relative.as_posix()
                nodes.append(node)
        elif relative.parts[0] == "app_context":
            nodes.append({"kind": "context", "alias": alias, "relative_path": relative.as_posix()})
    return nodes
