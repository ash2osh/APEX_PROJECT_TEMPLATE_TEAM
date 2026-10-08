#!/usr/bin/env python3
"""Raw source hashes for canonical 26.2 export and selected-page evidence."""
from __future__ import annotations

import hashlib
import json
import re
import stat
from pathlib import Path, PurePosixPath
from datetime import datetime


def tree_hashes(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("source evidence requires a regular directory")
    result = {}
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        if path.is_symlink() or getattr(metadata, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
            raise ValueError("source evidence refuses symbolic links")
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise ValueError("source evidence requires regular files and directories")
        if stat.S_ISREG(metadata.st_mode) and path.relative_to(root).as_posix() != "apex-team-export.json":
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def content_evidence(root: Path) -> dict:
    hashes = tree_hashes(root)
    if "application.apx" not in hashes or ".apex/apexlang.json" not in hashes:
        raise ValueError("canonical evidence requires application and format metadata")
    try:
        version = json.loads((root / ".apex/apexlang.json").read_text(encoding="utf-8"))["mmdVersion"]
    except (KeyError, OSError, ValueError, TypeError):
        raise ValueError("canonical evidence requires authentic APEXlang mmdVersion") from None
    if not isinstance(version, str) or re.fullmatch(r"26\.2\.\d+\+\d+", version) is None:
        raise ValueError("canonical source must match the qualified APEX 26.2 format")
    return {"schemaVersion": 2, "sourceFormat": {"apexRelease": "26.2", "mmdVersion": version}, "sourceFiles": hashes}


def read_content_baseline(root: Path, app_id: int) -> dict[str, str]:
    marker = root / "apex-team-export.json"
    if marker.is_symlink():
        raise ValueError("source baseline must not be a symbolic link")
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("canonical schema-2 baseline unavailable; export or upgrade first") from None
    if not isinstance(data, dict) or type(data.get("applicationId")) is not int or data["applicationId"] != app_id or data.get("schemaVersion") != 2 or data.get("applicationPresent") is not True:
        raise ValueError("canonical schema-2 baseline unavailable; export or upgrade first")
    metadata = data.get("sourceFormat")
    if not isinstance(metadata, dict) or metadata.get("apexRelease") != "26.2" or not isinstance(metadata.get("mmdVersion"), str) or re.fullmatch(r"26\.2\.\d+\+\d+", metadata["mmdVersion"]) is None:
        raise ValueError("canonical source format evidence is invalid")
    hashes = data.get("sourceFiles")
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("canonical source hashes unavailable")
    for name, digest in hashes.items():
        path = PurePosixPath(name)
        if path.is_absolute() or name != path.as_posix() or ".." in path.parts or "\\" in name or not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise ValueError("canonical source hashes contain invalid paths or digests")
    if "application.apx" not in hashes or ".apex/apexlang.json" not in hashes:
        raise ValueError("canonical source hashes lack required files")
    return hashes


def validate_page_locks(rows, app_id: int) -> list[dict]:
    if not isinstance(rows, list):
        raise ValueError('invalid native page lock evidence')
    ids = set()
    required = {'applicationId', 'workspace', 'pageId', 'lockId', 'owner', 'comment', 'lockedOn'}
    for row in rows:
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError('invalid native page lock evidence')
        for field in ('applicationId', 'pageId', 'lockId'):
            if type(row[field]) is not int or not 0 <= row[field] < 10**18:
                raise ValueError('invalid native page lock identity')
        if row['applicationId'] != app_id or row['lockId'] == 0 or row['pageId'] in ids:
            raise ValueError('ambiguous native page lock identity')
        ids.add(row['pageId'])
        if any(not isinstance(row[key], str) or not row[key].strip() for key in ('workspace', 'owner')):
            raise ValueError('invalid native page lock owner/workspace')
        if row['comment'] is not None and not isinstance(row['comment'], str):
            raise ValueError('invalid native page lock comment')
        if not isinstance(row['lockedOn'], str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}', row['lockedOn']) is None:
            raise ValueError('invalid native page lock timestamp')
        datetime.fromisoformat(row['lockedOn'])
    return sorted(rows, key=lambda row: row['pageId'])


def read_page_lock_evidence(before: Path, after: Path, app_id: int) -> list[dict] | None:
    if not before.exists() and not after.exists():
        return None  # Legacy/mocked exports cannot qualify a notification waiver.
    if not before.is_file() or not after.is_file():
        raise ValueError('both native page lock observations are required')
    if before.is_symlink() or after.is_symlink():
        raise ValueError('native page lock evidence must not be a link')
    first = validate_page_locks(json.loads(before.read_text(encoding='utf-8')), app_id)
    second = validate_page_locks(json.loads(after.read_text(encoding='utf-8')), app_id)
    if first != second:
        raise ValueError('native page locks changed while export was running; retry before editing')
    return first
