#!/usr/bin/env python3
"""Validate dated migration folders and content-bound environment receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from collections.abc import Mapping, Sequence

from .validate_migration import validate_sql_only


FOLDER_RE = re.compile(
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})_"
    r"(?P<family>[a-z][a-z0-9]*(?:-[a-z0-9]+)*)-r(?P<revision>[0-9]{3})\Z",
    re.ASCII,
)
SQL_FILE_RE = re.compile(r"(?P<sequence>[0-9]{3})-(?P<name>[a-z][a-z0-9]*(?:-[a-z0-9]+)*)\.sql\Z", re.ASCII)
CHECK_ID_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z", re.ASCII)
SCHEMA_DIRECTORY_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
SQL_TOKEN_RE = re.compile(r"(?:[A-Za-z][A-Za-z0-9_$#]*|[0-9]+(?:\.[0-9]+)?|.)", re.ASCII | re.DOTALL)
SAFE_FUNCTIONS = {
    "avg",
    "cast",
    "coalesce",
    "count",
    "length",
    "max",
    "min",
    "nvl",
    "regexp_like",
    "substr",
    "sum",
    "sys_context",
    "to_char",
    "to_number",
    "upper",
    "lower",
}
SAFE_SQL_FORMS = {"as", "exists", "filter", "in", "over", "within"}
FORBIDDEN_WORDS = {
    "alter",
    "begin",
    "commit",
    "create",
    "currval",
    "delete",
    "drop",
    "execute",
    "exec",
    "exit",
    "grant",
    "host",
    "insert",
    "lock",
    "merge",
    "nextval",
    "prompt",
    "rollback",
    "revoke",
    "set",
    "truncate",
    "update",
    "whenever",
}
RECEIPT_FIELDS = {
    "schemaVersion",
    "state",
    "environment",
    "migration",
    "createdDate",
    "family",
    "revision",
    "files",
    "checksSha256",
    "payloadDigest",
    "target",
    "applyStartedAt",
    "applyCompletedAt",
    "verifiedAt",
    "checks",
    "verifier",
    "normalization",
}
STATUS_ENVIRONMENTS = {"dev", "staging", "prod"}


class MigrationManifestError(ValueError):
    """Raised when a migration folder, check manifest, or receipt is unsafe."""


@dataclass(frozen=True)
class MigrationFile:
    sequence: int
    name: str
    path: Path
    source: bytes
    sha256: str


@dataclass(frozen=True)
class QueryCheck:
    id: str
    sql: str
    expected: int


@dataclass(frozen=True)
class Migration:
    folder: Path
    date: str
    family: str
    revision: int
    files: tuple[MigrationFile, ...]
    preconditions: tuple[QueryCheck, ...]
    postconditions: tuple[QueryCheck, ...]
    checks_source: bytes
    checks_sha256: str
    payload_digest: str
    schema: str | None = None


def _decode_json(source: bytes, label: str) -> object:
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MigrationManifestError(f"{label} must be UTF-8") from exc
    if "\r" in text or text.startswith("\ufeff"):
        raise MigrationManifestError(f"{label} must use UTF-8 without a BOM and LF line endings")

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise MigrationManifestError(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            object_pairs_hook=unique_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                MigrationManifestError(f"{label} contains unsupported JSON value {value}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise MigrationManifestError(f"{label} is invalid JSON: {exc.msg}") from exc


def _strip_sql_comments_and_tokenize(source: str) -> list[str]:
    """Tokenize a small read-only query subset while preserving quoted tokens."""
    tokens: list[str] = []
    index = 0
    while index < len(source):
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith("--", index):
            end = source.find("\n", index + 2)
            index = len(source) if end < 0 else end + 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                raise MigrationManifestError("check query has an unterminated block comment")
            index = end + 2
            continue
        if char in "'\"":
            quote = char
            start = index
            index += 1
            while index < len(source):
                if source[index] == quote:
                    if index + 1 < len(source) and source[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            else:
                raise MigrationManifestError("check query has an unterminated quoted value")
            tokens.append(("STRING:" if quote == "'" else "QIDENT:") + source[start:index])
            continue
        if char in "qQ" and index + 1 < len(source) and source[index + 1] == "'":
            raise MigrationManifestError("Oracle q-quoted literals are not supported in checks")
        match = SQL_TOKEN_RE.match(source, index)
        if match is None:
            raise MigrationManifestError("check query contains an invalid token")
        token = match.group(0)
        if token == "@":
            raise MigrationManifestError("database links are not allowed in checks")
        if token == ";":
            tokens.append(token)
        elif token.isascii() and token.isalpha() or re.fullmatch(r"[A-Za-z][A-Za-z0-9_$#]*", token, re.ASCII):
            tokens.append("WORD:" + token.casefold())
        elif token[0].isdigit():
            tokens.append("NUMBER:" + token)
        else:
            tokens.append(token)
        index = match.end()
    return tokens


def validate_check_query(sql: str) -> None:
    """Allow one read-only SELECT/WITH-SELECT with a restricted function set."""
    if not isinstance(sql, str) or not sql.strip() or len(sql.encode("utf-8")) > 32768:
        raise MigrationManifestError("check SQL must be a nonempty UTF-8 query no longer than 32768 bytes")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in sql):
        raise MigrationManifestError("check SQL contains a control character")
    tokens = _strip_sql_comments_and_tokenize(sql)
    if not tokens:
        raise MigrationManifestError("check SQL is empty")
    if tokens[-1] == ";":
        tokens.pop()
    if ";" in tokens or any(token in {"/", "\\"} for token in tokens):
        raise MigrationManifestError("checks must contain exactly one SQL query and no SQLcl terminator")

    for index, token in enumerate(tokens):
        if token == ":" and (index + 1 >= len(tokens) or tokens[index + 1] != "WORD:target_schema"):
            raise MigrationManifestError("checks may bind only :target_schema")

    words = [token[5:] for token in tokens if token.startswith("WORD:")]
    if not words or words[0] not in {"select", "with"} or "select" not in words:
        raise MigrationManifestError("checks must be one SELECT or WITH-SELECT query")
    forbidden = sorted(FORBIDDEN_WORDS.intersection(words))
    if forbidden:
        raise MigrationManifestError(f"check query contains forbidden operation/token {forbidden[0].upper()}")
    if "for" in words and any(words[i + 1] == "update" for i, word in enumerate(words[:-1]) if word == "for"):
        raise MigrationManifestError("FOR UPDATE is not allowed in checks")

    depth = 0
    for token in tokens:
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
            if depth < 0:
                raise MigrationManifestError("check query has unbalanced parentheses")
    if depth:
        raise MigrationManifestError("check query has unbalanced parentheses")

    for index, token in enumerate(tokens[:-1]):
        if token.startswith("WORD:") and tokens[index + 1] == "(":
            name = token[5:]
            if name not in SAFE_FUNCTIONS and name not in SAFE_SQL_FORMS:
                raise MigrationManifestError(f"check query calls unsupported or user-defined function {name.upper()}")
            if name == "sys_context":
                arguments = tokens[index + 2 : index + 6]
                if arguments != ["STRING:'USERENV'", ",", "STRING:'SESSION_USER'", ")"] and arguments != [
                    "STRING:'USERENV'", ",", "STRING:'CURRENT_SCHEMA'", ")"
                ]:
                    raise MigrationManifestError("SYS_CONTEXT checks may inspect only USERENV session identity")
        if token == "." and index + 2 < len(tokens) and tokens[index + 2] == "(":
            raise MigrationManifestError("schema-qualified function calls are not allowed in checks")


def _folder_parts(folder_name: str) -> tuple[str, str, int]:
    match = FOLDER_RE.fullmatch(folder_name)
    if match is None:
        raise MigrationManifestError(
            f"invalid migration folder '{folder_name}'; use YYYY-MM-DD_<migration-name>-rNNN"
        )
    try:
        date.fromisoformat(match.group("date"))
    except ValueError as exc:
        raise MigrationManifestError(f"migration folder has an invalid calendar date: {folder_name}") from exc
    revision = int(match.group("revision"))
    if revision == 0:
        raise MigrationManifestError(f"migration revision must start at r001: {folder_name}")
    return match.group("date"), match.group("family"), revision


def _migration_directories(repo_root: Path) -> list[tuple[Path, str | None, str, int]]:
    root = repo_root.resolve()
    migrations_dir = root / "migrations"
    if migrations_dir.is_symlink() or not migrations_dir.is_dir():
        raise MigrationManifestError(f"migrations directory does not exist or is unsafe: {migrations_dir}")
    directories: list[tuple[Path, str | None, str, int]] = []

    def add_dated(path: Path, schema: str | None) -> None:
        if FOLDER_RE.fullmatch(path.name) is None:
            raise MigrationManifestError(
                f"legacy or invalid migration directory '{path.name}'; convert it to YYYY-MM-DD_<name>-rNNN"
            )
        _, family, revision = _folder_parts(path.name)
        directories.append((path, schema, family, revision))

    def skip_entry(path: Path, label: str) -> bool:
        if path.is_symlink():
            raise MigrationManifestError(f"symbolic link is not allowed in {label}: {path.name}")
        if path.name == ".gitkeep" and path.is_file():
            return True
        if not path.is_dir():
            if path.suffix.lower() == ".sql":
                raise MigrationManifestError("root-level SQL is not supported; put SQL in a dated migration folder")
            return True
        return False

    for path in sorted(migrations_dir.iterdir(), key=lambda entry: entry.name):
        if skip_entry(path, "migrations/"):
            continue
        if FOLDER_RE.fullmatch(path.name) is not None:
            add_dated(path, None)
            continue
        if SCHEMA_DIRECTORY_RE.fullmatch(path.name) is None:
            raise MigrationManifestError(
                f"legacy or invalid migration directory '{path.name}'; convert it to YYYY-MM-DD_<name>-rNNN"
            )
        for child in sorted(path.iterdir(), key=lambda entry: entry.name):
            if skip_entry(child, f"migrations/{path.name}/"):
                continue
            add_dated(child, path.name)

    seen: dict[tuple[str | None, str, int], Path] = {}
    revisions: dict[tuple[str | None, str], set[int]] = {}
    for path, schema, family, revision in directories:
        identity = (schema, family, revision)
        if identity in seen:
            raise MigrationManifestError(
                f"duplicate local migration identity {family}-r{revision:03d}: "
                f"{seen[identity].name} and {path.name}"
            )
        seen[identity] = path
        revisions.setdefault((schema, family), set()).add(revision)
    for (schema, family), values in revisions.items():
        expected = set(range(1, max(values) + 1))
        if values != expected:
            raise MigrationManifestError(f"migration family {family} has a revision gap; revisions start at r001")
    return directories


def list_migration_folders(repo_root: Path) -> tuple[Path, ...]:
    """List valid migration folders newest first by descending folder name."""
    directories = _migration_directories(repo_root)
    return tuple(sorted((path for path, _, _, _ in directories), key=lambda path: path.name, reverse=True))


def _validate_relative_folder(relative_folder: str) -> tuple[str, ...]:
    if not isinstance(relative_folder, str) or "\\" in relative_folder:
        raise MigrationManifestError("use a repository-relative migrations/<dated-folder> path")
    parts = tuple(relative_folder.split("/"))
    legacy = "use a repository-relative migrations/<dated-folder> path; legacy file paths are not supported"
    if len(parts) not in (2, 3) or parts[0] != "migrations" or any(part in {"", ".", ".."} for part in parts):
        raise MigrationManifestError(legacy)
    if len(parts) == 3 and FOLDER_RE.fullmatch(parts[1]) is not None:
        # migrations/<dated-folder>/<file>: a legacy file path, not a schema folder.
        raise MigrationManifestError(legacy)
    if len(parts) == 3 and SCHEMA_DIRECTORY_RE.fullmatch(parts[1]) is None:
        raise MigrationManifestError("the schema directory in migrations/<SCHEMA>/<dated-folder> must be an uppercase Oracle identifier")
    return parts


def _load_check_file(path: Path) -> tuple[tuple[QueryCheck, ...], tuple[QueryCheck, ...], bytes]:
    try:
        source = path.read_bytes()
    except OSError as exc:
        raise MigrationManifestError(f"required checks.json could not be read: {exc}") from exc
    raw = _decode_json(source, str(path))
    if not isinstance(raw, dict) or set(raw) != {"schemaVersion", "preconditions", "postconditions"}:
        raise MigrationManifestError("checks.json must contain exactly schemaVersion, preconditions, and postconditions")
    if type(raw["schemaVersion"]) is not int or raw["schemaVersion"] != 1:
        raise MigrationManifestError("checks.json schemaVersion must be integer 1")
    parsed: dict[str, tuple[QueryCheck, ...]] = {}
    for field in ("preconditions", "postconditions"):
        values = raw[field]
        if not isinstance(values, list) or (field == "postconditions" and not values):
            raise MigrationManifestError(f"checks.json {field} must be {'a nonempty' if field == 'postconditions' else 'an'} array")
        checks: list[QueryCheck] = []
        identifiers: set[str] = set()
        for index, value in enumerate(values, start=1):
            if not isinstance(value, dict) or set(value) != {"id", "sql", "expected"}:
                raise MigrationManifestError(f"checks.json {field}[{index}] must contain exactly id, sql, and expected")
            check_id, sql, expected = value["id"], value["sql"], value["expected"]
            if not isinstance(check_id, str) or CHECK_ID_RE.fullmatch(check_id) is None:
                raise MigrationManifestError(f"checks.json {field}[{index}] has an invalid check id")
            if check_id in identifiers:
                raise MigrationManifestError(f"checks.json {field} contains duplicate check id {check_id!r}")
            if not isinstance(sql, str):
                raise MigrationManifestError(f"checks.json check {check_id} SQL must be a string")
            if type(expected) is not int or expected != 1:
                raise MigrationManifestError(f"checks.json check {check_id} expected value must be integer 1")
            validate_check_query(sql)
            identifiers.add(check_id)
            checks.append(QueryCheck(check_id, sql, expected))
        parsed[field] = tuple(checks)
    return parsed["preconditions"], parsed["postconditions"], source


def _validate_folder_entries(folder: Path) -> tuple[MigrationFile, ...]:
    files: list[MigrationFile] = []
    found_sequences: dict[int, str] = {}
    for path in folder.iterdir():
        if path.is_symlink():
            raise MigrationManifestError(f"symbolic links are not allowed in migration folders: {path.name}")
        if path.is_dir():
            raise MigrationManifestError(f"nested directories are not allowed in migration folders: {path.name}")
        if not path.is_file():
            raise MigrationManifestError(f"unsupported filesystem entry in migration folder: {path.name}")
        if path.name == "checks.json" or path.name == "README.md":
            continue
        if path.name.startswith("status.") and path.name.endswith(".json"):
            environment = path.name[len("status.") : -len(".json")]
            if environment not in STATUS_ENVIRONMENTS:
                raise MigrationManifestError(f"unsupported migration status environment: {path.name}")
            continue
        match = SQL_FILE_RE.fullmatch(path.name)
        if match is None:
            raise MigrationManifestError(f"migration SQL must use NNN-<name>.sql: {path.name}")
        sequence = int(match.group("sequence"))
        if sequence == 0:
            raise MigrationManifestError("migration SQL sequence must start at 001")
        if sequence in found_sequences:
            raise MigrationManifestError(f"duplicate SQL file sequence {sequence:03d}: {found_sequences[sequence]} and {path.name}")
        found_sequences[sequence] = path.name
        try:
            source = path.read_bytes()
            decoded = source.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise MigrationManifestError(f"migration SQL must be readable UTF-8: {path.name}") from exc
        if b"\r" in source or source.startswith(b"\xef\xbb\xbf"):
            raise MigrationManifestError(f"migration SQL must use UTF-8 without a BOM and LF line endings: {path.name}")
        try:
            validate_sql_only(decoded)
        except ValueError as exc:
            raise MigrationManifestError(f"{path.name}: {exc}") from exc
        files.append(MigrationFile(sequence, path.name, path, source, hashlib.sha256(source).hexdigest()))

    files.sort(key=lambda file: file.sequence)
    if not files:
        raise MigrationManifestError(f"migration folder has no numbered SQL files: {folder.name}")
    actual = tuple(file.sequence for file in files)
    expected = tuple(range(1, len(files) + 1))
    if actual != expected:
        raise MigrationManifestError(f"SQL sequences in {folder.name} must be consecutive from 001; found {actual}")
    return tuple(files)


def load_migration(repo_root: Path, relative_folder: str) -> Migration:
    """Validate one folder and return its ordered, exact-byte payload."""
    parts = _validate_relative_folder(relative_folder)
    root = repo_root.resolve()
    folder = root.joinpath(*parts)
    if folder.is_symlink():
        raise MigrationManifestError("symbolic links are not allowed in migration paths")
    try:
        folder.resolve(strict=True).relative_to(root)
    except (OSError, ValueError) as exc:
        raise MigrationManifestError(f"migration folder is missing or outside the repository: {relative_folder}") from exc
    if not folder.is_dir():
        raise MigrationManifestError(f"migration path is not a folder: {relative_folder}")
    _, family, revision = _folder_parts(folder.name)
    directories = _migration_directories(root)
    schemas = [schema for candidate, schema, _, _ in directories if candidate == folder]
    if not schemas:
        raise MigrationManifestError(f"migration folder is not directly under migrations/ or migrations/<SCHEMA>/: {relative_folder}")
    files = _validate_folder_entries(folder)
    preconditions, postconditions, checks_source = _load_check_file(folder / "checks.json")
    checks_sha256 = hashlib.sha256(checks_source).hexdigest()
    payload = {
        "files": [{"name": file.name, "sequence": file.sequence, "sha256": file.sha256} for file in files],
        "checksSha256": checks_sha256,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()
    return Migration(
        folder=folder,
        date=folder.name[:10],
        family=family,
        revision=revision,
        files=files,
        preconditions=preconditions,
        postconditions=postconditions,
        checks_source=checks_source,
        checks_sha256=checks_sha256,
        payload_digest=digest,
        schema=schemas[0],
    )


def load_batch(repo_root: Path, relative_folders: Sequence[str]) -> tuple[Migration, ...]:
    """Load selected folders without changing the caller's execution order."""
    if isinstance(relative_folders, (str, bytes)) or not relative_folders:
        raise MigrationManifestError("select one or more migration folders in execution order")
    batch = tuple(load_migration(repo_root, folder) for folder in relative_folders)
    names = [migration.folder.name for migration in batch]
    if len(set(names)) != len(names):
        raise MigrationManifestError("a migration folder may be selected only once")
    previous: dict[tuple[str | None, str], int] = {}
    for migration in batch:
        key = (migration.schema, migration.family)
        revision = previous.get(key)
        if revision is not None and migration.revision <= revision:
            raise MigrationManifestError(f"migration family {migration.family} must be selected in ascending revision order")
        previous[key] = migration.revision
    return batch


def _validate_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise MigrationManifestError(f"receipt {field} must be an ISO-8601 timestamp")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationManifestError(f"receipt {field} must be an ISO-8601 timestamp") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise MigrationManifestError(f"receipt {field} must include a timezone")
    return timestamp


def validate_receipt(path: Path, migration: Migration, target_identity: Mapping[str, str]) -> dict:
    """Validate a local receipt against exact migration bytes and target identity."""
    if path.is_symlink() or path.name not in {f"status.{environment}.json" for environment in STATUS_ENVIRONMENTS}:
        raise MigrationManifestError("receipt path is unsafe or has an unsupported environment name")
    try:
        raw = _decode_json(path.read_bytes(), str(path))
    except OSError as exc:
        raise MigrationManifestError(f"receipt could not be read: {exc}") from exc
    if not isinstance(raw, dict) or set(raw) != RECEIPT_FIELDS:
        raise MigrationManifestError("receipt fields do not match schemaVersion 1")
    environment = path.name[len("status.") : -len(".json")]
    if type(raw["schemaVersion"]) is not int or raw["schemaVersion"] != 1 or raw["state"] != "verified":
        raise MigrationManifestError("receipt must use schemaVersion 1 and state verified")
    if raw["environment"] != environment or environment not in STATUS_ENVIRONMENTS:
        raise MigrationManifestError("receipt environment does not match its status filename")
    if raw["migration"] != migration.folder.name or raw["createdDate"] != migration.date:
        raise MigrationManifestError("receipt migration identity does not match this folder")
    if raw["family"] != migration.family or type(raw["revision"]) is not int or raw["revision"] != migration.revision:
        raise MigrationManifestError("receipt family/revision does not match this folder")
    expected_files = [
        {"name": file.name, "sequence": file.sequence, "sha256": file.sha256} for file in migration.files
    ]
    if raw["files"] != expected_files or raw["checksSha256"] != migration.checks_sha256:
        raise MigrationManifestError("receipt SQL/check hashes do not match the current migration payload")
    if raw["payloadDigest"] != migration.payload_digest:
        raise MigrationManifestError("receipt payload digest does not match the current migration")
    if raw["target"] != dict(target_identity):
        raise MigrationManifestError("receipt target identity does not match the selected target")
    if raw["target"].get("environment") != environment:
        raise MigrationManifestError("receipt target environment does not match the status filename")
    started = _validate_timestamp(raw["applyStartedAt"], "applyStartedAt")
    completed = _validate_timestamp(raw["applyCompletedAt"], "applyCompletedAt")
    verified = _validate_timestamp(raw["verifiedAt"], "verifiedAt")
    if started > completed or completed > verified:
        raise MigrationManifestError("receipt timestamps are out of order")
    if not isinstance(raw["verifier"], str) or not raw["verifier"] or not isinstance(raw["normalization"], str) or not raw["normalization"]:
        raise MigrationManifestError("receipt verifier and normalization versions are required")
    if not isinstance(raw["checks"], list) or not raw["checks"]:
        raise MigrationManifestError("receipt must include successful postcondition results")
    check_ids = {check.id for check in migration.postconditions}
    user_results = [result for result in raw["checks"] if isinstance(result, dict) and result.get("kind") != "catalog"]
    catalog_results = [result for result in raw["checks"] if isinstance(result, dict) and result.get("kind") == "catalog"]
    if any(not isinstance(result, dict) or result.get("id") not in check_ids or result.get("passed") is not True for result in user_results):
        raise MigrationManifestError("receipt contains a missing, unknown, or failed postcondition result")
    if {result["id"] for result in user_results} != check_ids:
        raise MigrationManifestError("receipt does not record every migration postcondition")
    if any(
        not isinstance(result.get("id"), str)
        or not re.fullmatch(r"catalog-[0-9]{3}-[0-9]{3}-[a-z0-9-]+", result["id"], re.ASCII)
        or result.get("passed") is not True
        or result.get("rows") != 1
        or result.get("value") != 1
        for result in catalog_results
    ):
        raise MigrationManifestError("receipt contains an invalid generated catalog assertion")
    if len(user_results) + len(catalog_results) != len(raw["checks"]):
        raise MigrationManifestError("receipt contains an unknown verification check kind")
    return raw


def install_receipt(path: Path, receipt: dict) -> None:
    """Atomically install a UTF-8 receipt without replacing an existing path."""
    if path.name not in {f"status.{environment}.json" for environment in STATUS_ENVIRONMENTS}:
        raise MigrationManifestError("receipt filename must be status.dev.json, status.staging.json, or status.prod.json")
    if path.is_symlink() or not path.parent.is_dir():
        raise MigrationManifestError("receipt directory is missing or unsafe")
    if not isinstance(receipt, dict) or receipt.get("schemaVersion") != 1 or receipt.get("state") != "verified":
        raise MigrationManifestError("only schemaVersion 1 verified receipts can be installed")
    if receipt.get("environment") != path.name[len("status.") : -len(".json")]:
        raise MigrationManifestError("receipt environment does not match its status filename")
    data = (json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise MigrationManifestError(f"receipt already exists and will not be replaced: {path.name}") from exc
        except OSError as exc:
            raise MigrationManifestError(f"atomic no-overwrite receipt installation is unavailable: {exc}") from exc
        try:
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        except OSError:
            # The receipt is already installed. Directory fsync is unavailable on some filesystems.
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", nargs="*")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--list", action="store_true", help="list valid folders newest first; does not imply execution order")
    arguments = parser.parse_args(argv)
    try:
        if arguments.list:
            if arguments.folders:
                raise MigrationManifestError("--list does not accept folder arguments")
            for folder in list_migration_folders(arguments.repo_root):
                print(folder.relative_to(arguments.repo_root.resolve()).as_posix())
            return 0
        batch = load_batch(arguments.repo_root, arguments.folders)
        for migration in batch:
            print(f"{migration.folder.name}: {len(migration.files)} SQL file(s), payload {migration.payload_digest}")
        return 0
    except MigrationManifestError as exc:
        print(f"migration manifest error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
