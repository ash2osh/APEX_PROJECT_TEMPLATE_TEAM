"""Pure four-way reconciliation for exact application trees."""

from __future__ import annotations

from dataclasses import dataclass

from .trees import Tree, _validate_tree_paths


@dataclass(frozen=True)
class Decision:
    tree: dict[str, bytes]
    conflicts: tuple[str, ...]


def reconcile(
    base: Tree,
    head: Tree,
    mine: Tree,
    source_base: Tree | None = None,
) -> Decision:
    """Apply the specified four-way state table without text-specific merging."""
    if source_base is None:
        source_base = base
    for tree in (base, source_base, head, mine):
        _validate_tree_paths(tree)

    absent = object()
    output: dict[str, bytes] = {}
    conflicts: list[str] = []
    for path in sorted(set(base) | set(source_base) | set(head) | set(mine)):
        b, s, h, m = (
            tree.get(path, absent) for tree in (base, source_base, head, mine)
        )
        if m == h:
            selected = m
        elif m == b:
            selected = h
        elif h == s == b:
            selected = m
        else:
            conflicts.append(path)
            continue
        if selected is not absent:
            output[path] = selected
    return Decision(output, tuple(conflicts))

