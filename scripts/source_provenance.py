"""Bind observed Oracle APEX releases to the exact canonical source file set."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .apexlang_diff import source_files
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.apexlang_diff import source_files


RELEASE_RE = re.compile(r"[0-9]{1,2}(?:\.[0-9]{1,3}){1,4}\Z", re.ASCII)
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
PROVENANCE_FIELDS = {"schemaVersion", "sourceRelease", "exportedAt", "canonicalDigest"}


@dataclass(frozen=True)
class VersionPair:
    source: str | None
    target: str | None


@dataclass(frozen=True)
class SourceProvenance:
    status: str
    source_release: str | None
    captured_at: str | None
    canonical_digest: str | None
    reason: str | None = None

    def marker_value(self) -> dict[str, object] | None:
        if (
            self.source_release is None
            or self.captured_at is None
            or self.canonical_digest is None
        ):
            return None
        return {
            "schemaVersion": 1,
            "sourceRelease": self.source_release,
            "exportedAt": self.captured_at,
            "canonicalDigest": self.canonical_digest,
        }


def canonical_source_digest(source_dir: Path) -> str:
    """Hash exactly the canonical source files used by strict publish comparison."""
    root = Path(source_dir)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("canonical source directory is missing or unsafe")
    files = source_files(root)
    digest = hashlib.sha256()
    digest.update(len(files).to_bytes(8, "big"))
    for relative in sorted(files, key=lambda path: path.as_posix()):
        path_bytes = relative.as_posix().encode("utf-8")
        content = files[relative].read_bytes()
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def make_source_provenance(source_dir: Path, source_release: str, exported_at: str) -> dict[str, object]:
    """Create a fresh schema-1 marker from two stable guarded observations."""
    if not isinstance(source_release, str) or RELEASE_RE.fullmatch(source_release) is None:
        raise ValueError("observed APEX source release is malformed")
    if source_release.split(".")[:2] != ["26", "2"]:
        raise ValueError("observed APEX source release does not match this template branch")
    _parse_utc(exported_at)
    return {
        "schemaVersion": 1,
        "sourceRelease": source_release,
        "exportedAt": exported_at,
        "canonicalDigest": canonical_source_digest(source_dir),
    }


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("source provenance exportedAt must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("source provenance exportedAt must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("source provenance exportedAt must include the UTC timezone")
    return parsed


def _strict_json(path: Path) -> object:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    if "\r" in text or text.startswith("\ufeff"):
        raise ValueError("export marker must use UTF-8 without BOM and LF line endings")

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict:
        result: dict = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"export marker contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    return json.loads(
        text,
        object_pairs_hook=unique_pairs,
        parse_constant=lambda item: (_ for _ in ()).throw(ValueError(f"unsupported JSON value {item}")),
    )


def read_source_provenance(source_dir: Path) -> SourceProvenance:
    """Report verified, stale, or unknown provenance without relaxing equality."""
    root = Path(source_dir)
    marker = root / "apex-team-export.json"
    if marker.is_symlink() or not marker.is_file():
        return SourceProvenance("unknown", None, None, None, "export marker is unavailable or unsafe")
    try:
        data = _strict_json(marker)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        return SourceProvenance("unknown", None, None, None, f"export marker cannot be read: {error}")
    if not isinstance(data, dict):
        return SourceProvenance("unknown", None, None, None, "export marker is not an object")
    record = data.get("sourceProvenance")
    if record is None:
        return SourceProvenance("unknown", None, None, None, "marker predates export-bound provenance")
    if (
        not isinstance(record, dict)
        or set(record) != PROVENANCE_FIELDS
        or type(record.get("schemaVersion")) is not int
        or record.get("schemaVersion") != 1
        or not isinstance(record.get("sourceRelease"), str)
        or RELEASE_RE.fullmatch(record["sourceRelease"]) is None
        or record["sourceRelease"].split(".")[:2] != ["26", "2"]
        or not isinstance(record.get("canonicalDigest"), str)
        or SHA256_RE.fullmatch(record["canonicalDigest"]) is None
    ):
        return SourceProvenance("unknown", None, None, None, "sourceProvenance fields are malformed")
    try:
        _parse_utc(record.get("exportedAt"))
    except ValueError as error:
        return SourceProvenance("unknown", None, None, None, str(error))
    try:
        observed = canonical_source_digest(root)
    except (OSError, ValueError) as error:
        return SourceProvenance("unknown", record["sourceRelease"], record["exportedAt"], record["canonicalDigest"], f"canonical source set is unavailable: {error}")
    if observed != record["canonicalDigest"]:
        return SourceProvenance("stale", record["sourceRelease"], record["exportedAt"], record["canonicalDigest"], "canonical source file set changed after export")
    return SourceProvenance("verified", record["sourceRelease"], record["exportedAt"], record["canonicalDigest"])


def describe_version_pair(pair: VersionPair) -> str:
    """Format a sanitized source/target release pair for diagnostic reports."""
    source = pair.source if isinstance(pair.source, str) and pair.source else "unknown"
    target = pair.target if isinstance(pair.target, str) and pair.target else "unknown"
    return f"source APEX {source}; target APEX {target}"


def preserve_source_provenance(source_dir: Path) -> Mapping[str, object] | None:
    """Return structurally valid prior metadata, including stale records, for diagnostics."""
    state = read_source_provenance(source_dir)
    return state.marker_value()
