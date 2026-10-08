#!/usr/bin/env python3
"""Permit only the exact local source already verified by partial publication."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.source_evidence import tree_hashes
from scripts.validate_app_source import validate_app_source


def baseline_hash(source: Path) -> str | None:
    marker = source / 'apex-team-export.json'
    return hashlib.sha256(marker.read_bytes()).hexdigest() if marker.is_file() else None


def check_source(root: Path, relative: str, receipt: Path) -> None:
    root = root.resolve(strict=True)
    parts = relative.split('/')
    if len(parts) != 3 or parts[0] != 'apps':
        raise ValueError('verified source replacement is restricted to one APEX app')
    if receipt.is_symlink() or any(parent.is_symlink() for parent in receipt.parents):
        raise ValueError('verified source receipt must not be a link')
    receipt.resolve(strict=True).relative_to((root / 'scratch').resolve(strict=True))
    proof = json.loads(receipt.read_text(encoding='utf-8'))
    source = validate_app_source(root, root / relative)
    if not isinstance(proof, dict) or type(proof.get('schemaVersion')) is not int or proof['schemaVersion'] != 1 or proof.get('relativeDirectory') != relative:
        raise ValueError('verified source receipt does not match this destination')
    if proof.get('sourceFiles') != tree_hashes(source) or proof.get('baselineHash') != baseline_hash(source):
        raise ValueError('local source changed after partial publication began; keep verified staging for reconciliation')


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3:
        print('usage: check_mirror_source.py <repo> <app-relative-directory> <receipt>', file=sys.stderr)
        return 2
    try:
        check_source(Path(args[0]), args[1], Path(args[2]))
    except (OSError, ValueError) as exc:
        print(f'mirror replacement error: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
