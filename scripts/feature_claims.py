#!/usr/bin/env python3
"""Find which feature claims which database object, offline."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .feature_manifest import (
    FEATURE_RE,
    SCHEMA_RE,
    FeatureInput,
    FeatureManifestError,
    _analysis_migration,
    _read_feature_source,
    _source_unit,
)
from .migration_checks import MigrationAnalysisError, analyze_batch
from .path_utils import relative_to_root


@dataclass(frozen=True)
class Claim:
    """One feature claims one object: a stored unit file, a created object, or an added column."""

    feature: str
    schema: str
    kind: str
    name: str
    path: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.schema, self.kind, self.name)


def feature_schema_dirs(root: Path, feature: str | None = None) -> list[tuple[str, str, Path]]:
    """Every features/<slug>/<SCHEMA> directory, in name order."""
    found: list[tuple[str, str, Path]] = []
    features_dir = root / "features"
    if not features_dir.is_dir():
        return found
    for feature_dir in sorted(features_dir.iterdir()):
        if not feature_dir.is_dir() or FEATURE_RE.fullmatch(feature_dir.name) is None:
            continue
        if feature is not None and feature_dir.name != feature:
            continue
        for schema_dir in sorted(feature_dir.iterdir()):
            if schema_dir.is_dir() and SCHEMA_RE.fullmatch(schema_dir.name) is not None:
                found.append((feature_dir.name, schema_dir.name, schema_dir))
    return found


def _claims_for_file(feature: str, schema: str, relative: str, source: bytes) -> list[Claim]:
    item = FeatureInput(relative, source, hashlib.sha256(source).hexdigest(), None, "ddl")
    unit = _source_unit(item, schema)
    if unit is not None:
        owner, object_type, name = unit
        return [Claim(feature, owner, object_type, name, relative)]
    claims: list[Claim] = []
    for operation in analyze_batch((_analysis_migration((item,), schema),), schema):
        kind = operation.get("kind")
        owner = operation.get("owner", schema)
        if kind == "ALTER_ADD_COLUMN" and isinstance(operation.get("table"), str):
            for column in operation.get("columns") or ():
                claims.append(Claim(feature, owner, "COLUMN", f"{operation['table']}.{column}", relative))
        elif isinstance(kind, str) and kind.startswith("CREATE_") and isinstance(operation.get("name"), str):
            object_type = operation.get("object_type")
            if isinstance(object_type, str):
                claims.append(Claim(feature, owner, object_type, operation["name"], relative))
    return claims


def collect_claims(root: Path, feature: str | None = None) -> tuple[list[Claim], list[str]]:
    """Return every claim under features/ and a note for each SQL file that could not be analysed."""
    claims: list[Claim] = []
    notes: list[str] = []
    for slug, schema, schema_dir in feature_schema_dirs(root, feature):
        for path in sorted(schema_dir.rglob("*.sql")):
            relative = relative_to_root(path, root).as_posix()
            if "data" in path.relative_to(schema_dir).parts[:-1]:
                continue
            try:
                source = _read_feature_source(path, relative)
                claims.extend(_claims_for_file(slug, schema, relative, source))
            except (FeatureManifestError, MigrationAnalysisError, ValueError, UnicodeError) as error:
                notes.append(f"{relative}: not analysed ({error})")
    return claims, notes


def holders(claims: Iterable[Claim], key: tuple[str, str, str]) -> set[str]:
    return {claim.feature for claim in claims if claim.key == key}


def duplicates(claims: Iterable[Claim]) -> dict[tuple[str, str, str], list[Claim]]:
    """Objects claimed by more than one feature."""
    by_key: dict[tuple[str, str, str], list[Claim]] = defaultdict(list)
    for claim in claims:
        by_key[claim.key].append(claim)
    return {key: values for key, values in by_key.items() if len({claim.feature for claim in values}) > 1}


CHECK_OBJECT_RE = re.compile(
    r"OBJECT_NAME\s*=\s*'([^']+)'\s+AND\s+OBJECT_TYPE\s*=\s*'([^']+)'"
    r"|OBJECT_TYPE\s*=\s*'([^']+)'\s+AND\s+OBJECT_NAME\s*=\s*'([^']+)'",
    re.IGNORECASE,
)


def checked_objects(sql: str) -> list[tuple[str, str]]:
    """(type, name) pairs a check query names through OBJECT_NAME / OBJECT_TYPE."""
    found: list[tuple[str, str]] = []
    for match in CHECK_OBJECT_RE.finditer(sql):
        name = match.group(1) or match.group(4)
        kind = match.group(2) or match.group(3)
        found.append((kind.upper(), name))
    return found
