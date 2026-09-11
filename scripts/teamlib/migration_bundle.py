"""Immutable migration bundles and strict directional SQL headers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from collections.abc import Mapping

from .sql_text import SqlTextError, comment_spans, mask_sql, statement_starts


class BundleError(ValueError):
    """Raised when a migration bundle is incomplete, unsafe or ambiguous."""


_ID_BODY = r"(?P<stamp>[0-9]{8}T[0-9]{6})__(?P<author>[a-z0-9][a-z0-9-]*)__(?P<slug>[a-z0-9][a-z0-9-]*)"
_ID_RE = re.compile(r"^" + _ID_BODY + r"$")
_PRIMARY_RE = re.compile(r"^" + _ID_BODY + r"\.sql$")
_VERIFY_RE = re.compile(r"^" + _ID_BODY + r"\.verify\.sql$")
_DOWN_PRIMARY_RE = re.compile(r"^" + _ID_BODY + r"\.down\.sql$")
_DOWN_VERIFY_RE = re.compile(r"^" + _ID_BODY + r"\.down\.verify\.sql$")
_DIRECTIVE_RE = re.compile(r"^\s*--\s*([A-Za-z][A-Za-z0-9-]*)\s*:\s*(.*?)\s*$")
_DEP_RE = re.compile(r"^([0-9]{8}T[0-9]{6}__[a-z0-9][a-z0-9-]*__[a-z0-9][a-z0-9-]*)\s+sha256:([0-9a-f]{64})$")


@dataclass(frozen=True)
class Migration:
    id: str
    stamp: str
    target: str
    checksum: str
    dependencies: tuple[tuple[str, str], ...]
    destructive: bool
    sql_path: Path | None = field(default=None, compare=False)
    verify_path: Path | None = field(default=None, compare=False)
    sql_bytes: bytes = field(default=b"", compare=False, repr=False)
    verify_bytes: bytes = field(default=b"", compare=False, repr=False)
    version: int = 1
    down_sql_path: Path | None = field(default=None, compare=False)
    down_verify_path: Path | None = field(default=None, compare=False)
    down_sql_bytes: bytes = field(default=b"", compare=False, repr=False)
    down_verify_bytes: bytes = field(default=b"", compare=False, repr=False)
    down_destructive: bool = False

    @property
    def reversible(self) -> bool:
        return self.down_sql_path is not None and self.down_verify_path is not None


def _validate_id(migration_id: str) -> tuple[str, str]:
    match = _ID_RE.fullmatch(migration_id)
    if not match:
        raise BundleError(f"invalid migration ID: {migration_id}")
    stamp = match.group("stamp")
    try:
        datetime.strptime(stamp, "%Y%m%dT%H%M%S")
    except ValueError as exc:
        raise BundleError(f"invalid UTC timestamp in migration ID: {migration_id}") from exc
    return stamp, migration_id


def _mask_code(text: str) -> str:
    """Mask strings and comments while retaining positions/newlines."""
    masked, terminated = mask_sql(text)
    if not terminated:
        raise BundleError("unterminated SQL comment or literal")
    return masked


def _comment_directives(text: str) -> list[tuple[int, str, str]]:
    """Return real line-comment directives, not strings or block comments.

    Comment locations come from the shared Oracle-aware scanner, so an
    apostrophe inside a ``q'[...]'`` literal can no longer end string state
    early and turn the literal's own text into a directive.
    """
    result: list[tuple[int, str, str]] = []
    try:
        spans = comment_spans(text)
    except SqlTextError as exc:
        raise BundleError(str(exc)) from exc
    for start, stop in spans:
        match = _DIRECTIVE_RE.match(text[start:stop])
        if match:
            result.append((start, match.group(1).casefold(), match.group(2)))
    return result


def _first_code_position(masked: str) -> int | None:
    for index, char in enumerate(masked):
        if not char.isspace():
            return index
    return None


def _parse_header(text: str, migration_id: str) -> tuple[int, str, bool, tuple[tuple[str, str], ...]]:
    if "\r" in text:
        raise BundleError(f"migration member must use LF line endings: {migration_id}")
    masked = _mask_code(text)
    first_code = _first_code_position(masked)
    found: dict[str, list[str]] = {}
    dependencies: list[tuple[str, str]] = []
    allowed = {"migration-version", "target", "destructive", "depends-on"}
    for position, key, value in _comment_directives(text):
        if first_code is not None and position > first_code:
            raise BundleError(f"migration directive appears after executable SQL: {migration_id}")
        if key not in allowed:
            raise BundleError(f"unknown migration directive: {key}")
        if key == "depends-on":
            match = _DEP_RE.fullmatch(value)
            if not match:
                raise BundleError(f"malformed dependency directive in {migration_id}")
            if any(existing_id == match.group(1) for existing_id, _ in dependencies):
                raise BundleError(f"duplicate dependency in {migration_id}")
            dependencies.append((match.group(1), match.group(2)))
        else:
            found.setdefault(key, []).append(value)
    for required in ("migration-version", "target", "destructive"):
        if len(found.get(required, [])) != 1:
            raise BundleError(f"migration {migration_id} requires exactly one {required} directive")
    try:
        version = int(found["migration-version"][0])
    except ValueError as exc:
        raise BundleError(f"migration-version must be an integer: {migration_id}") from exc
    if version != 1:
        raise BundleError(f"unsupported migration-version in {migration_id}")
    target = found["target"][0]
    if target not in {"tables", "code"}:
        raise BundleError(f"migration target must be tables or code: {migration_id}")
    destructive = found["destructive"][0].casefold()
    if destructive not in {"true", "false"}:
        raise BundleError(f"destructive must be true or false: {migration_id}")
    return version, target, destructive == "true", tuple(sorted(dependencies))


def _parse_down_header(text: str, migration_id: str) -> tuple[int, bool]:
    """Parse the deliberately smaller header allowed for a down member."""
    if "\r" in text:
        raise BundleError(f"migration member must use LF line endings: {migration_id}")
    masked = _mask_code(text)
    first_code = _first_code_position(masked)
    found: dict[str, list[str]] = {}
    for position, key, value in _comment_directives(text):
        if first_code is not None and position > first_code:
            raise BundleError(f"migration directive appears after executable SQL: {migration_id}")
        if key not in {"migration-version", "destructive"}:
            raise BundleError(f"down migration has forbidden directive: {key}")
        found.setdefault(key, []).append(value)
    for required in ("migration-version", "destructive"):
        if len(found.get(required, [])) != 1:
            raise BundleError(f"down migration {migration_id} requires exactly one {required} directive")
    try:
        version = int(found["migration-version"][0])
    except ValueError as exc:
        raise BundleError(f"migration-version must be an integer: {migration_id}") from exc
    if version != 1:
        raise BundleError(f"unsupported migration-version in {migration_id}")
    destructive = found["destructive"][0].casefold()
    if destructive not in {"true", "false"}:
        raise BundleError(f"destructive must be true or false: {migration_id}")
    return version, destructive == "true"


# SQLcl interprets a client command only where a new statement begins. Matching
# these words anywhere in a body rejects CONNECT BY, EXIT WHEN and any column
# named host -- including this template's own control_metadata.sql. Statement
# boundaries come from sql_text.statement_starts, so an inline ";" starts a new
# statement here exactly as it does in SQLcl.
_CLIENT_COMMAND_RE = re.compile(
    r"^(?:@{1,2}|!)|^(?:CONNECT|CONN|HOST|EXIT|QUIT|WHENEVER|SPOOL|SCRIPT|START)\b",
    re.IGNORECASE,
)


def _assert_controls(text: str, *, verify: bool = False) -> None:
    code = _mask_code(text)
    for number, statement in statement_starts(code):
        if re.match(r"^(?:@{1,2}|START\b|SCRIPT\b)", statement, re.IGNORECASE):
            raise BundleError(f"nested SQLcl includes are prohibited (line {number})")
        if _CLIENT_COMMAND_RE.match(statement):
            raise BundleError(
                f"SQLcl control command is prohibited in migration members (line {number})"
            )
    if re.search(r"(?i)\bSET\s+(?:DEFINE\s+ON|SQLTERMINATOR\s+OFF|ESCAPE\s+ON)\b", code):
        raise BundleError("SQLcl substitution/error-policy changes are prohibited")
    if verify:
        if re.search(r"\b(?:INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|BEGIN|DECLARE|EXEC|EXECUTE|COMMIT|ROLLBACK|GRANT|REVOKE)\b", code, re.IGNORECASE):
            raise BundleError("verification members must be SELECT-only")


def _validate_verify(text: str, migration_id: str) -> None:
    if not text.strip():
        return
    _assert_controls(text, verify=True)
    code = _mask_code(text)
    statements = [part.strip() for part in code.split(";") if part.strip()]
    if not statements or any(not statement.upper().startswith("SELECT") for statement in statements):
        raise BundleError(f"verification member is not a SELECT assertion: {migration_id}")
    if any(not re.search(r"\bASSERTION_NAME\b", statement, re.IGNORECASE) or not re.search(r"\bSTATUS\b", statement, re.IGNORECASE) for statement in statements):
        raise BundleError(f"verification member must return assertion_name and status: {migration_id}")
    # A verification result is intentionally constrained to PASS/FAIL literals
    # when present; a dynamic status cannot be qualified by the adapter.
    if re.search(r"(?i)\bstatus\s*=\s*", code):
        raise BundleError(f"verification status must be a selected PASS/FAIL value: {migration_id}")


def _bundle_checksum(
    migration_id: str,
    sql_bytes: bytes,
    verify_bytes: bytes,
    down_sql_bytes: bytes = b"",
    down_verify_bytes: bytes = b"",
    *,
    reversible: bool = False,
) -> str:
    members = []
    for path, data in ((f"{migration_id}.sql", sql_bytes), (f"{migration_id}.verify.sql", verify_bytes)):
        members.append({"path": path, "length": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    if reversible:
        for path, data in (
            (f"{migration_id}.down.sql", down_sql_bytes),
            (f"{migration_id}.down.verify.sql", down_verify_bytes),
        ):
            members.append({"path": path, "length": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    canonical = json.dumps(members, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_bundles(directory: str | Path) -> dict[str, Migration]:
    root = Path(directory)
    if not root.is_dir() or root.is_symlink():
        raise BundleError(f"migration directory is not a real directory: {root}")
    members: dict[str, dict[str, Path]] = {}
    orphan_verify: list[str] = []
    for path in sorted(root.iterdir(), key=lambda item: item.name):
        primary = _PRIMARY_RE.fullmatch(path.name)
        verify = _VERIFY_RE.fullmatch(path.name)
        down_primary = _DOWN_PRIMARY_RE.fullmatch(path.name)
        down_verify = _DOWN_VERIFY_RE.fullmatch(path.name)
        if not any((primary, verify, down_primary, down_verify)):
            continue
        if path.is_symlink() or not path.is_file():
            raise BundleError(f"migration bundle member must be a regular file: {path}")
        match = primary or verify or down_primary or down_verify
        migration_id = match.group("stamp") + "__" + match.group("author") + "__" + match.group("slug")
        _validate_id(migration_id)
        entry = members.setdefault(migration_id, {})
        if primary:
            entry["sql"] = path
        elif verify:
            entry["verify"] = path
        elif down_primary:
            entry["down_sql"] = path
        elif down_verify:
            entry["down_verify"] = path
        if verify and "sql" not in entry:
            orphan_verify.append(verify.group(0)[:-len(".verify.sql")])
    missing_orphans = [migration_id for migration_id in orphan_verify if migration_id not in members or "sql" not in members[migration_id]]
    if missing_orphans:
        missing = ", ".join(sorted(missing_orphans))
        raise BundleError(f"orphan verification member: {missing}")
    bundles: dict[str, Migration] = {}
    for migration_id, entry in members.items():
        sql_path = entry.get("sql")
        verify_path = entry.get("verify")
        if sql_path is None:
            raise BundleError(f"missing SQL member: {migration_id}")
        if verify_path is None:
            raise BundleError(f"missing verification member: {migration_id}")
        down_sql_path = entry.get("down_sql")
        down_verify_path = entry.get("down_verify")
        if (down_sql_path is None) != (down_verify_path is None):
            raise BundleError(f"down migration requires both SQL and verification members: {migration_id}")
        sibling_prefix = migration_id + "."
        siblings = [path.name for path in root.iterdir() if path.name.startswith(sibling_prefix)]
        allowed_siblings = {
            f"{migration_id}.sql", f"{migration_id}.verify.sql",
            f"{migration_id}.down.sql", f"{migration_id}.down.verify.sql",
        }
        extras = sorted(set(siblings) - allowed_siblings)
        if extras:
            raise BundleError(f"unexpected migration bundle member(s): {', '.join(extras)}")
        try:
            sql_bytes = sql_path.read_bytes()
            verify_bytes = verify_path.read_bytes()
            down_sql_bytes = down_sql_path.read_bytes() if down_sql_path is not None else b""
            down_verify_bytes = down_verify_path.read_bytes() if down_verify_path is not None else b""
            sql_text = sql_bytes.decode("utf-8")
            verify_text = verify_bytes.decode("utf-8")
            down_sql_text = down_sql_bytes.decode("utf-8") if down_sql_path is not None else ""
            down_verify_text = down_verify_bytes.decode("utf-8") if down_verify_path is not None else ""
        except (OSError, UnicodeError) as exc:
            raise BundleError(f"migration bundle is not valid UTF-8: {migration_id}") from exc
        stamp, _ = _validate_id(migration_id)
        version, target, destructive, dependencies = _parse_header(sql_text, migration_id)
        _assert_controls(sql_text)
        _validate_verify(verify_text, migration_id)
        down_version = version
        down_destructive = False
        if down_sql_path is not None and down_verify_path is not None:
            down_version, down_destructive = _parse_down_header(down_sql_text, migration_id)
            _assert_controls(down_sql_text)
            _validate_verify(down_verify_text, migration_id)
        checksum = _bundle_checksum(
            migration_id, sql_bytes, verify_bytes, down_sql_bytes, down_verify_bytes,
            reversible=down_sql_path is not None,
        )
        bundles[migration_id] = Migration(
            id=migration_id,
            stamp=stamp,
            target=target,
            checksum=checksum,
            dependencies=dependencies,
            destructive=destructive,
            sql_path=sql_path,
            verify_path=verify_path,
            sql_bytes=sql_bytes,
            verify_bytes=verify_bytes,
            version=version,
            down_sql_path=down_sql_path,
            down_verify_path=down_verify_path,
            down_sql_bytes=down_sql_bytes,
            down_verify_bytes=down_verify_bytes,
            down_destructive=down_destructive,
        )
    # A dependency that exists locally must match its immutable checksum now.
    for migration in bundles.values():
        for dependency_id, declared_checksum in migration.dependencies:
            dependency = bundles.get(dependency_id)
            if dependency is not None and dependency.checksum != declared_checksum:
                raise BundleError(f"dependency checksum mismatch: {migration.id} -> {dependency_id}")
    return bundles


def dependency_order(bundles: Mapping[str, Migration]) -> tuple[str, ...]:
    remaining = set(bundles)
    incoming = {migration_id: {dependency_id for dependency_id, _ in migration.dependencies} for migration_id, migration in bundles.items()}
    missing = sorted({dependency for values in incoming.values() for dependency in values if dependency not in bundles})
    if missing:
        raise BundleError(f"missing migration dependency: {', '.join(missing)}")
    order: list[str] = []
    while remaining:
        ready = sorted((bundles[migration_id] for migration_id in remaining if not (incoming[migration_id] & remaining)), key=lambda migration: (migration.stamp, migration.id))
        if not ready:
            raise BundleError("migration dependency cycle")
        chosen = ready[0].id
        order.append(chosen)
        remaining.remove(chosen)
    return tuple(order)


def bundle_checksum(directory: str | Path, migration_id: str) -> str:
    bundles = load_bundles(directory)
    try:
        return bundles[migration_id].checksum
    except KeyError as exc:
        raise BundleError(f"migration not found: {migration_id}") from exc


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="verify-history")
    parser.add_argument("--source", required=True)
    args = parser.parse_args(list(argv or []))
    bundles = load_bundles(args.source)
    dependency_order(bundles)
    print(json.dumps({"status": "valid", "migrations": sorted(bundles)}, sort_keys=True))
    return 0
