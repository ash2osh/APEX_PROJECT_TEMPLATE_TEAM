"""Strict DEV test suite manifest loader and scenario selector.

Public API (Task 1):
    DevTestManifestError(ValueError)  – structured refusal; carries .code str
    Suite                             – frozen dataclass(schemaVersion, id, root, scenarios)
    Scenario                          – frozen dataclass with exact spec section-6 fields
    load_suite(path: Path) -> Suite
    select_scenarios(suite, ids) -> tuple[Scenario, ...]

No project Python is imported; no SQL is executed at load time.
Requires Python 3.10+ standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 1

# Suite/scenario IDs: lowercase ASCII letters, digits and hyphens,
# starting with a letter, maximum 64 characters.
ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}\Z", re.ASCII)

# Oracle error code format: ORA-NNNNN
ORA_CODE_RE = re.compile(r"^ORA-[0-9]{5}\Z", re.ASCII)
CATALOG_ID_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
OBJECT_TYPE_RE = re.compile(r"[A-Z]+(?: [A-Z]+)*\Z", re.ASCII)
OBJECT_BINDING_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)
SUFFIX_RE = re.compile(r"[A-Z][A-Z0-9_]{0,13}\Z", re.ASCII)
SELECTOR_BINDING_RE = re.compile(r"DEVTEST_[A-Z][A-Z0-9_]{0,119}\Z", re.ASCII)

ALLOWED_TRANSACTION = frozenset(
    {"read-only", "caller-controlled", "commits", "ddl", "mixed", "unknown"}
)
ALLOWED_EXTERNAL = frozenset({"none", "present", "unknown"})
ALLOWED_STATUS = frozenset({"VALID", "ANY"})
ALLOWED_FIXTURE_TYPES = frozenset({"TABLE", "VIEW", "PACKAGE", "PROCEDURE", "FUNCTION"})
ALLOWED_COLUMN_TYPES = frozenset({"integer", "string"})

SUITE_ROOT_KEYS = frozenset({"schemaVersion", "id", "scenarios"})
SCENARIO_KEYS = frozenset({
    "id", "description", "dependsOn", "requirements",
    "effects", "fixtures", "steps", "checks", "observations", "cleanup",
})
EFFECTS_KEYS = frozenset({"writes", "transaction", "external", "affectedObjects", "backgroundConsumers"})
REQUIREMENTS_KEYS = frozenset({"objects", "checks"})
FIXTURES_KEYS = frozenset({"objects", "selectors"})
REQUIREMENT_OBJECT_KEYS = frozenset({"owner", "name", "type", "requiredStatus", "member"})
FIXTURE_OBJECT_KEYS = frozenset({"binding", "suffix", "type"})
SELECTOR_KEYS = frozenset({"id", "sql", "columns"})
SELECTOR_COLUMN_KEYS = frozenset({"name", "binding", "type"})
STEP_KEYS = frozenset({"id", "sqlFile", "expected"})
CHECK_KEYS = frozenset({"id", "sql", "expected"})
OBSERVATION_KEYS = frozenset({"id", "sql", "maxRows"})
CLEANUP_KEYS = frozenset({"sqlFiles", "checks"})


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------

class DevTestManifestError(ValueError):
    """Raised when a DEV test suite manifest is invalid or unsafe.

    Attributes:
        code: Machine-readable refusal code (e.g. ``MISSING_DEPENDENCY``).
        message: Safe human-readable message; never contains bare newlines.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        safe = message.replace("\r", "\\r").replace("\n", "\\n")
        safe = "".join(char if char.isprintable() else f"\\x{ord(char):02x}" for char in safe)
        self.message = safe
        super().__init__(f"[{code}] {safe}")


# ---------------------------------------------------------------------------
# Frozen data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scenario:
    """Frozen record for a single project scenario as defined in spec section 6."""

    id: str
    description: str
    dependsOn: tuple[str, ...]
    requirements: MappingProxyType  # {"objects": [...], "checks": [...]}
    effects: MappingProxyType       # spec effects shape
    fixtures: MappingProxyType      # {"objects": [...], "selectors": [...]}
    steps: tuple[MappingProxyType, ...]        # each entry has id/sqlFile/expected/path/source/sha256
    checks: tuple[MappingProxyType, ...]       # each entry has id/sql/expected
    observations: tuple[MappingProxyType, ...] # each entry has id/sql/maxRows
    cleanup: MappingProxyType | None           # None or {sqlFiles, checks}


@dataclass(frozen=True)
class Suite:
    """Frozen record for a loaded project test suite."""

    id: str
    root: Path
    scenarios: tuple[Scenario, ...]
    schemaVersion: int = SCHEMA_VERSION
    manifest_path: Path | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_exact_keys(d: dict, allowed: frozenset[str], context: str) -> None:
    """Raise DevTestManifestError if d has any key not in allowed."""
    extra = set(d) - allowed
    if extra:
        names = ", ".join(sorted(extra))
        raise DevTestManifestError(
            "UNKNOWN_FIELD",
            f"Unknown field(s) in {context}: {names}",
        )


def _require_keys(d: dict, required: frozenset[str], context: str) -> None:
    missing = required - set(d)
    if missing:
        names = ", ".join(sorted(missing))
        raise DevTestManifestError(
            "MISSING_FIELD",
            f"Missing required field(s) in {context}: {names}",
        )


def _validate_id(value: Any, context: str) -> str:
    if not isinstance(value, str):
        raise DevTestManifestError("INVALID_ID", f"{context} id must be a string")
    if not ID_RE.match(value):
        raise DevTestManifestError(
            "INVALID_ID",
            f"{context} id {value!r} is invalid; must match ^[a-z][a-z0-9-]{{0,63}}$",
        )
    return value


def _validate_str(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise DevTestManifestError("INVALID_TYPE", f"{field} must be a string, got {type(value).__name__}")
    return value


def _validate_nonempty_str(value: Any, field: str) -> str:
    s = _validate_str(value, field)
    if not s:
        raise DevTestManifestError("INVALID_TYPE", f"{field} must be a non-empty string")
    return s


def _validate_name(value: Any, pattern: re.Pattern[str], field: str) -> str:
    result = _validate_nonempty_str(value, field)
    if pattern.fullmatch(result) is None:
        raise DevTestManifestError("INVALID_IDENTIFIER", f"{field} is not a supported identifier")
    return result


def _validate_int_not_bool(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DevTestManifestError(
            "INVALID_TYPE",
            f"{field} must be an integer (not boolean), got {type(value).__name__}",
        )
    return value


def _validate_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise DevTestManifestError(
            "INVALID_TYPE",
            f"{field} must be a boolean, got {type(value).__name__}",
        )
    return value


def _validate_list(value: Any, field: str) -> list:
    if not isinstance(value, list):
        raise DevTestManifestError("INVALID_TYPE", f"{field} must be a list")
    return value


def _validate_dict(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise DevTestManifestError("INVALID_TYPE", f"{field} must be an object/dict")
    return value


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _detect_path_escape(raw: str) -> None:
    """Raise DevTestManifestError for absolute, drive, UNC, or traversal paths."""
    if not isinstance(raw, str) or not raw:
        raise DevTestManifestError("INVALID_PATH", "sqlFile must be a non-empty string")

    # UNC paths: \\server or //server
    if raw.startswith("\\\\") or raw.startswith("//"):
        raise DevTestManifestError("INVALID_PATH", f"UNC path not allowed: {raw!r}")

    # Absolute Unix path
    if raw.startswith("/"):
        raise DevTestManifestError("INVALID_PATH", f"Absolute path not allowed: {raw!r}")

    # Windows drive letter: C:\ or C:/ or C:anything
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise DevTestManifestError("INVALID_PATH", f"Drive-letter path not allowed: {raw!r}")

    # Backslash absolute (Windows \\, or just \ starting)
    if raw.startswith("\\"):
        raise DevTestManifestError("INVALID_PATH", f"Absolute backslash path not allowed: {raw!r}")

    # Directory traversal via .. components
    # Normalize separators to / for checking
    normalized = raw.replace("\\", "/")
    parts = normalized.split("/")
    for part in parts:
        if part == "..":
            raise DevTestManifestError(
                "INVALID_PATH",
                f"Directory traversal not allowed in path: {raw!r}",
            )


def _resolve_sql_file(raw: str, suite_root: Path, context: str) -> tuple[Path, bytes, str]:
    """Validate, resolve and read a relative SQL file path under suite_root.

    Returns (resolved_path, source_bytes, sha256_hex).
    Raises DevTestManifestError on any violation.
    """
    _detect_path_escape(raw)

    # Convert backslashes to forward slashes for cross-platform path join
    normalized = raw.replace("\\", "/")
    try:
        resolved = (suite_root / normalized).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise DevTestManifestError("INVALID_PATH", f"{context}: SQL file path cannot be resolved") from exc

    # After resolution, check containment (catches symlink escapes)
    try:
        resolved.relative_to(suite_root.resolve())
    except ValueError:
        raise DevTestManifestError(
            "PATH_ESCAPE",
            f"{context}: path escapes suite root: {raw!r}",
        ) from None

    # Must be a regular file (not a directory, device, etc.)
    if not resolved.exists():
        raise DevTestManifestError(
            "MISSING_FILE",
            f"{context}: SQL file not found: {raw!r}",
        )
    if not resolved.is_file():
        raise DevTestManifestError(
            "INVALID_PATH",
            f"{context}: path is not a regular file: {raw!r}",
        )

    # Symlink escape: the original path (before resolve) as a link must not point outside
    pre_resolved = (suite_root / normalized)
    if pre_resolved.is_symlink():
        # resolved already escaped check performed above; if we're still here, it's fine.
        # But double-check the link target
        link_target = pre_resolved.resolve()
        try:
            link_target.relative_to(suite_root.resolve())
        except ValueError:
            raise DevTestManifestError(
                "PATH_ESCAPE",
                f"{context}: symlink escapes suite root: {raw!r}",
            ) from None

    try:
        source = resolved.read_bytes()
    except OSError as exc:
        raise DevTestManifestError("IO_ERROR", f"{context}: SQL file cannot be read") from exc
    return resolved, source, _sha256_hex(source)


def _validate_check(entry: Any, idx: int, context: str) -> MappingProxyType:
    d = _validate_dict(entry, f"{context}[{idx}]")
    _check_exact_keys(d, CHECK_KEYS, f"{context}[{idx}]")
    _require_keys(d, CHECK_KEYS, f"{context}[{idx}]")
    _validate_id(d["id"], f"{context}[{idx}].id")
    _validate_nonempty_str(d["sql"], f"{context}[{idx}].sql")
    _validate_int_not_bool(d["expected"], f"{context}[{idx}].expected")
    return MappingProxyType({"id": d["id"], "sql": d["sql"], "expected": d["expected"]})


def _validate_observation(entry: Any, idx: int) -> MappingProxyType:
    d = _validate_dict(entry, f"observations[{idx}]")
    _check_exact_keys(d, OBSERVATION_KEYS, f"observations[{idx}]")
    _require_keys(d, OBSERVATION_KEYS, f"observations[{idx}]")
    _validate_id(d["id"], f"observations[{idx}].id")
    _validate_nonempty_str(d["sql"], f"observations[{idx}].sql")
    max_rows = _validate_int_not_bool(d["maxRows"], f"observations[{idx}].maxRows")
    if max_rows < 1 or max_rows > 100:
        raise DevTestManifestError(
            "INVALID_OBSERVATION",
            f"observations[{idx}].maxRows must be between 1 and 100, got {max_rows}",
        )
    return MappingProxyType({"id": d["id"], "sql": d["sql"], "maxRows": max_rows})


def _validate_step_expected(exp: Any, step_id: str) -> MappingProxyType:
    d = _validate_dict(exp, f"step {step_id!r} expected")
    kind = d.get("kind")
    if kind == "success":
        if set(d) != {"kind"}:
            extra = set(d) - {"kind"}
            raise DevTestManifestError(
                "UNKNOWN_FIELD",
                f"step {step_id!r} expected: unexpected fields for kind=success: {extra}",
            )
        return MappingProxyType({"kind": "success"})
    elif kind == "oracle-error":
        allowed = frozenset({"kind", "code"})
        extra = set(d) - allowed
        if extra:
            raise DevTestManifestError(
                "UNKNOWN_FIELD",
                f"step {step_id!r} expected: unexpected fields: {extra}",
            )
        if "code" not in d:
            raise DevTestManifestError(
                "MISSING_FIELD",
                f"step {step_id!r} expected kind=oracle-error requires 'code'",
            )
        code = _validate_str(d["code"], f"step {step_id!r} expected.code")
        if not ORA_CODE_RE.match(code):
            raise DevTestManifestError(
                "INVALID_ORA_CODE",
                f"step {step_id!r} expected.code {code!r} must match ORA-NNNNN",
            )
        return MappingProxyType({"kind": "oracle-error", "code": code})
    else:
        raise DevTestManifestError(
            "INVALID_STEP_EXPECTED",
            f"step {step_id!r} expected.kind must be 'success' or 'oracle-error', got {kind!r}",
        )


def _validate_step(entry: Any, idx: int, suite_root: Path) -> MappingProxyType:
    d = _validate_dict(entry, f"steps[{idx}]")
    _check_exact_keys(d, STEP_KEYS, f"steps[{idx}]")
    _require_keys(d, STEP_KEYS, f"steps[{idx}]")
    step_id = _validate_id(d["id"], f"steps[{idx}].id")
    sql_file_raw = _validate_str(d["sqlFile"], f"steps[{idx}].sqlFile")
    expected = _validate_step_expected(d["expected"], step_id)
    path, source, sha256 = _resolve_sql_file(sql_file_raw, suite_root, f"step {step_id!r}")
    return MappingProxyType({
        "id": step_id,
        "sqlFile": sql_file_raw,
        "expected": expected,
        "path": path,
        "source": source,
        "sha256": sha256,
    })


def _validate_requirements(reqs: Any) -> MappingProxyType:
    d = _validate_dict(reqs, "requirements")
    _check_exact_keys(d, REQUIREMENTS_KEYS, "requirements")
    _require_keys(d, REQUIREMENTS_KEYS, "requirements")

    objects_raw = _validate_list(d["objects"], "requirements.objects")
    checks_raw = _validate_list(d["checks"], "requirements.checks")

    objects_out = []
    for i, obj in enumerate(objects_raw):
        od = _validate_dict(obj, f"requirements.objects[{i}]")
        _check_exact_keys(od, REQUIREMENT_OBJECT_KEYS, f"requirements.objects[{i}]")
        _require_keys(od, REQUIREMENT_OBJECT_KEYS, f"requirements.objects[{i}]")
        if od["owner"] != "@selected":
            _validate_name(od["owner"], CATALOG_ID_RE, f"requirements.objects[{i}].owner")
        _validate_name(od["name"], CATALOG_ID_RE, f"requirements.objects[{i}].name")
        _validate_name(od["type"], OBJECT_TYPE_RE, f"requirements.objects[{i}].type")
        status = _validate_str(od["requiredStatus"], f"requirements.objects[{i}].requiredStatus")
        if status not in ALLOWED_STATUS:
            raise DevTestManifestError(
                "INVALID_STATUS",
                f"requirements.objects[{i}].requiredStatus must be VALID or ANY, got {status!r}",
            )
        # member is nullable; can be string or None
        member = od["member"]
        if member is not None:
            _validate_name(member, CATALOG_ID_RE, f"requirements.objects[{i}].member")
        objects_out.append(MappingProxyType({
            "owner": od["owner"],
            "name": od["name"],
            "type": od["type"],
            "requiredStatus": status,
            "member": member,
        }))

    checks_out = []
    check_ids_seen: set[str] = set()
    for i, chk in enumerate(checks_raw):
        validated = _validate_check(chk, i, "requirements.checks")
        if validated["id"] in check_ids_seen:
            raise DevTestManifestError(
                "DUPLICATE_ID",
                f"Duplicate check id {validated['id']!r} in requirements.checks",
            )
        check_ids_seen.add(validated["id"])
        checks_out.append(validated)

    return MappingProxyType({"objects": tuple(objects_out), "checks": tuple(checks_out)})


def _validate_effects(effects: Any) -> MappingProxyType:
    d = _validate_dict(effects, "effects")
    _check_exact_keys(d, EFFECTS_KEYS, "effects")
    _require_keys(d, EFFECTS_KEYS, "effects")

    writes = _validate_bool(d["writes"], "effects.writes")
    txn = _validate_str(d["transaction"], "effects.transaction")
    if txn not in ALLOWED_TRANSACTION:
        raise DevTestManifestError(
            "INVALID_ENUM",
            f"effects.transaction must be one of {sorted(ALLOWED_TRANSACTION)}, got {txn!r}",
        )
    ext = _validate_str(d["external"], "effects.external")
    if ext not in ALLOWED_EXTERNAL:
        raise DevTestManifestError(
            "INVALID_ENUM",
            f"effects.external must be one of {sorted(ALLOWED_EXTERNAL)}, got {ext!r}",
        )
    affected = _validate_list(d["affectedObjects"], "effects.affectedObjects")
    for i, item in enumerate(affected):
        _validate_str(item, f"effects.affectedObjects[{i}]")
    consumers = _validate_list(d["backgroundConsumers"], "effects.backgroundConsumers")
    for i, item in enumerate(consumers):
        _validate_str(item, f"effects.backgroundConsumers[{i}]")

    return MappingProxyType({
        "writes": writes,
        "transaction": txn,
        "external": ext,
        "affectedObjects": tuple(affected),
        "backgroundConsumers": tuple(consumers),
    })


def _validate_fixtures(fixtures: Any, suite_root: Path) -> MappingProxyType:
    d = _validate_dict(fixtures, "fixtures")
    _check_exact_keys(d, FIXTURES_KEYS, "fixtures")
    _require_keys(d, FIXTURES_KEYS, "fixtures")

    objects_raw = _validate_list(d["objects"], "fixtures.objects")
    selectors_raw = _validate_list(d["selectors"], "fixtures.selectors")

    binding_names: set[str] = {"DEVTEST_RUN_ID"}
    suffixes: set[str] = set()

    objects_out = []
    for i, obj in enumerate(objects_raw):
        od = _validate_dict(obj, f"fixtures.objects[{i}]")
        _check_exact_keys(od, FIXTURE_OBJECT_KEYS, f"fixtures.objects[{i}]")
        _require_keys(od, FIXTURE_OBJECT_KEYS, f"fixtures.objects[{i}]")
        binding = _validate_name(od["binding"], OBJECT_BINDING_RE, f"fixtures.objects[{i}].binding")
        if binding in binding_names:
            raise DevTestManifestError(
                "DUPLICATE_BINDING",
                f"Duplicate fixture binding name {binding!r} in fixtures.objects[{i}]",
            )
        binding_names.add(binding)
        suffix = _validate_name(od["suffix"], SUFFIX_RE, f"fixtures.objects[{i}].suffix")
        if suffix in suffixes:
            raise DevTestManifestError("DUPLICATE_SUFFIX", "Fixture objects must have distinct suffixes")
        suffixes.add(suffix)
        ftype = _validate_str(od["type"], f"fixtures.objects[{i}].type")
        if ftype not in ALLOWED_FIXTURE_TYPES:
            raise DevTestManifestError(
                "INVALID_FIXTURE_TYPE",
                f"fixtures.objects[{i}].type must be one of {sorted(ALLOWED_FIXTURE_TYPES)}, got {ftype!r}",
            )
        objects_out.append(MappingProxyType({
            "binding": binding,
            "suffix": od["suffix"],
            "type": ftype,
        }))

    selector_ids_seen: set[str] = set()
    selectors_out = []
    for i, sel in enumerate(selectors_raw):
        sd = _validate_dict(sel, f"fixtures.selectors[{i}]")
        _check_exact_keys(sd, SELECTOR_KEYS, f"fixtures.selectors[{i}]")
        _require_keys(sd, SELECTOR_KEYS, f"fixtures.selectors[{i}]")
        sel_id = _validate_id(sd["id"], f"fixtures.selectors[{i}].id")
        if sel_id in selector_ids_seen:
            raise DevTestManifestError(
                "DUPLICATE_ID",
                f"Duplicate selector id {sel_id!r} in fixtures.selectors",
            )
        selector_ids_seen.add(sel_id)
        _validate_nonempty_str(sd["sql"], f"fixtures.selectors[{i}].sql")
        cols_raw = _validate_list(sd["columns"], f"fixtures.selectors[{i}].columns")
        cols_out = []
        for j, col in enumerate(cols_raw):
            cd = _validate_dict(col, f"fixtures.selectors[{i}].columns[{j}]")
            _check_exact_keys(cd, SELECTOR_COLUMN_KEYS, f"fixtures.selectors[{i}].columns[{j}]")
            _require_keys(cd, SELECTOR_COLUMN_KEYS, f"fixtures.selectors[{i}].columns[{j}]")
            _validate_name(cd["name"], CATALOG_ID_RE, f"fixtures.selectors[{i}].columns[{j}].name")
            col_binding = _validate_name(cd["binding"], SELECTOR_BINDING_RE, f"fixtures.selectors[{i}].columns[{j}].binding")
            if col_binding in binding_names:
                raise DevTestManifestError(
                    "DUPLICATE_BINDING",
                    f"Duplicate binding name {col_binding!r} in fixtures.selectors[{i}].columns[{j}]",
                )
            binding_names.add(col_binding)
            col_type = _validate_str(cd["type"], f"fixtures.selectors[{i}].columns[{j}].type")
            if col_type not in ALLOWED_COLUMN_TYPES:
                raise DevTestManifestError(
                    "INVALID_COLUMN_TYPE",
                    f"fixtures.selectors[{i}].columns[{j}].type must be 'integer' or 'string', got {col_type!r}",
                )
            cols_out.append(MappingProxyType({"name": cd["name"], "binding": col_binding, "type": col_type}))
        selectors_out.append(MappingProxyType({
            "id": sel_id,
            "sql": sd["sql"],
            "columns": tuple(cols_out),
        }))

    return MappingProxyType({
        "objects": tuple(objects_out),
        "selectors": tuple(selectors_out),
    })


def _validate_cleanup(cleanup: Any, suite_root: Path) -> MappingProxyType | None:
    if cleanup is None:
        return None
    d = _validate_dict(cleanup, "cleanup")
    _check_exact_keys(d, CLEANUP_KEYS, "cleanup")
    _require_keys(d, CLEANUP_KEYS, "cleanup")

    sql_files_raw = _validate_list(d["sqlFiles"], "cleanup.sqlFiles")
    if not sql_files_raw:
        raise DevTestManifestError("INVALID_CLEANUP", "cleanup.sqlFiles must be nonempty")
    checks_raw = _validate_list(d["checks"], "cleanup.checks")
    if not checks_raw:
        raise DevTestManifestError("INVALID_CLEANUP", "cleanup.checks must be nonempty")

    sql_files_out = []
    for i, raw_path in enumerate(sql_files_raw):
        rp = _validate_str(raw_path, f"cleanup.sqlFiles[{i}]")
        path, source, sha256 = _resolve_sql_file(rp, suite_root, f"cleanup.sqlFiles[{i}]")
        sql_files_out.append(MappingProxyType({"sqlFile": rp, "path": path, "source": source, "sha256": sha256}))

    checks_out = []
    check_ids_seen: set[str] = set()
    for i, chk in enumerate(checks_raw):
        validated = _validate_check(chk, i, "cleanup.checks")
        if validated["id"] in check_ids_seen:
            raise DevTestManifestError(
                "DUPLICATE_ID",
                f"Duplicate check id {validated['id']!r} in cleanup.checks",
            )
        check_ids_seen.add(validated["id"])
        checks_out.append(validated)

    return MappingProxyType({
        "sqlFiles": tuple(sql_files_out),
        "checks": tuple(checks_out),
    })


def _validate_scenario(raw: Any, idx: int, suite_root: Path, seen_ids: set[str]) -> Scenario:
    d = _validate_dict(raw, f"scenarios[{idx}]")
    _check_exact_keys(d, SCENARIO_KEYS, f"scenarios[{idx}]")
    _require_keys(d, SCENARIO_KEYS, f"scenarios[{idx}]")

    sc_id = _validate_id(d["id"], f"scenarios[{idx}].id")
    if sc_id in seen_ids:
        raise DevTestManifestError(
            "DUPLICATE_ID",
            f"Duplicate scenario id {sc_id!r} in scenarios[{idx}]",
        )

    description = _validate_nonempty_str(d["description"], f"scenarios[{idx}].description")

    depends_raw = _validate_list(d["dependsOn"], f"scenarios[{idx}].dependsOn")
    depends_on: tuple[str, ...] = tuple(
        _validate_id(dep, f"scenarios[{idx}].dependsOn[{j}]") for j, dep in enumerate(depends_raw)
    )

    requirements = _validate_requirements(d["requirements"])
    effects = _validate_effects(d["effects"])
    fixtures = _validate_fixtures(d["fixtures"], suite_root)

    steps_raw = _validate_list(d["steps"], f"scenarios[{idx}].steps")
    step_ids_seen: set[str] = set()
    steps_out = []
    for i, step in enumerate(steps_raw):
        vstep = _validate_step(step, i, suite_root)
        if vstep["id"] in step_ids_seen:
            raise DevTestManifestError(
                "DUPLICATE_ID",
                f"Duplicate step id {vstep['id']!r} in scenarios[{idx}].steps",
            )
        step_ids_seen.add(vstep["id"])
        steps_out.append(vstep)

    checks_raw = _validate_list(d["checks"], f"scenarios[{idx}].checks")
    if not checks_raw:
        raise DevTestManifestError(
            "INVALID_CHECKS",
            f"scenarios[{idx}] ({sc_id!r}): checks must be nonempty",
        )
    check_ids_seen: set[str] = set()
    checks_out = []
    for i, chk in enumerate(checks_raw):
        validated = _validate_check(chk, i, f"scenarios[{idx}].checks")
        if validated["id"] in check_ids_seen:
            raise DevTestManifestError(
                "DUPLICATE_ID",
                f"Duplicate check id {validated['id']!r} in scenarios[{idx}].checks",
            )
        check_ids_seen.add(validated["id"])
        checks_out.append(validated)

    obs_raw = _validate_list(d["observations"], f"scenarios[{idx}].observations")
    obs_ids_seen: set[str] = set()
    obs_out = []
    for i, obs in enumerate(obs_raw):
        validated = _validate_observation(obs, i)
        if validated["id"] in obs_ids_seen:
            raise DevTestManifestError(
                "DUPLICATE_ID",
                f"Duplicate observation id {validated['id']!r} in scenarios[{idx}].observations",
            )
        obs_ids_seen.add(validated["id"])
        obs_out.append(validated)

    cleanup = _validate_cleanup(d["cleanup"], suite_root)

    return Scenario(
        id=sc_id,
        description=description,
        dependsOn=depends_on,
        requirements=requirements,
        effects=effects,
        fixtures=fixtures,
        steps=tuple(steps_out),
        checks=tuple(checks_out),
        observations=tuple(obs_out),
        cleanup=cleanup,
    )


def _validate_dependencies(scenarios: tuple[Scenario, ...]) -> None:
    """Validate all dependsOn references: no forward, self, or nonexistent refs."""
    # Build index of scenario positions
    id_to_pos: dict[str, int] = {sc.id: i for i, sc in enumerate(scenarios)}

    for i, sc in enumerate(scenarios):
        seen_deps: set[str] = set()
        for dep in sc.dependsOn:
            if dep == sc.id:
                raise DevTestManifestError(
                    "SELF_DEPENDENCY",
                    f"scenario {sc.id!r}: cannot depend on itself",
                )
            if dep not in id_to_pos:
                raise DevTestManifestError(
                    "UNKNOWN_DEPENDENCY",
                    f"scenario {sc.id!r}: depends on unknown scenario {dep!r}",
                )
            dep_pos = id_to_pos[dep]
            if dep_pos >= i:
                raise DevTestManifestError(
                    "FORWARD_DEPENDENCY",
                    f"scenario {sc.id!r} at position {i}: depends on {dep!r} at position {dep_pos} "
                    f"(forward/cyclic dependencies are forbidden)",
                )
            seen_deps.add(dep)


def _duplicate_key_hook(pairs: list[tuple[str, Any]]) -> dict:
    """JSON object_pairs_hook that rejects duplicate keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DevTestManifestError(
                "DUPLICATE_JSON_KEY",
                f"Duplicate JSON key {key!r} in suite file",
            )
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_suite(path: Path) -> Suite:
    """Load, validate and return a frozen Suite from the given JSON file path.

    Raises:
        DevTestManifestError: if any contract violation is detected.
        OSError: if the file cannot be read.
        json.JSONDecodeError: wrapped as DevTestManifestError.
    """
    suite_root = path.parent

    try:
        raw_text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise DevTestManifestError("INVALID_ENCODING", "Suite file must be UTF-8") from exc
    except OSError as exc:
        raise DevTestManifestError("IO_ERROR", f"Cannot read suite file: {exc}") from exc

    try:
        data = json.loads(raw_text, object_pairs_hook=_duplicate_key_hook)
    except DevTestManifestError:
        raise
    except json.JSONDecodeError as exc:
        raise DevTestManifestError("JSON_PARSE_ERROR", f"Invalid JSON in suite file: {exc}") from exc

    if not isinstance(data, dict):
        raise DevTestManifestError("INVALID_TYPE", "Suite file root must be a JSON object")

    # Exact root keys
    _check_exact_keys(data, SUITE_ROOT_KEYS, "suite root")
    _require_keys(data, SUITE_ROOT_KEYS, "suite root")

    # schemaVersion: must be integer 1, not boolean
    sv = data["schemaVersion"]
    _validate_int_not_bool(sv, "schemaVersion")
    if sv != SCHEMA_VERSION:
        raise DevTestManifestError(
            "UNSUPPORTED_SCHEMA_VERSION",
            f"schemaVersion must be {SCHEMA_VERSION}, got {sv}",
        )

    # suite id
    suite_id = _validate_id(data["id"], "suite")

    # scenarios: nonempty list
    scenarios_raw = _validate_list(data["scenarios"], "scenarios")
    if not scenarios_raw:
        raise DevTestManifestError("INVALID_SCENARIOS", "scenarios must be nonempty")

    seen_ids: set[str] = set()
    scenarios_out = []
    for i, sc_raw in enumerate(scenarios_raw):
        sc = _validate_scenario(sc_raw, i, suite_root, seen_ids)
        seen_ids.add(sc.id)
        scenarios_out.append(sc)

    scenarios_tuple = tuple(scenarios_out)

    # Validate dependencies after all scenarios are parsed (allows backward refs only)
    _validate_dependencies(scenarios_tuple)

    return Suite(
        schemaVersion=SCHEMA_VERSION,
        id=suite_id,
        root=suite_root,
        scenarios=scenarios_tuple,
        manifest_path=path.resolve(),
    )


def select_scenarios(suite: Suite, ids: Sequence[str]) -> tuple[Scenario, ...]:
    """Select and return scenarios from suite in manifest order.

    If *ids* is empty, all scenarios are returned.
    If *ids* is provided, only the named scenarios are returned, in manifest order.
    Raises DevTestManifestError if any id is unknown or if a required direct
    dependency of a selected scenario is not itself selected.
    """
    if not ids:
        return suite.scenarios

    id_to_scenario: dict[str, Scenario] = {sc.id: sc for sc in suite.scenarios}

    # Validate all requested IDs exist
    for sid in ids:
        if sid not in id_to_scenario:
            raise DevTestManifestError(
                "UNKNOWN_SCENARIO",
                f"No scenario {sid!r} in suite {suite.id!r}",
            )

    selected_set: set[str] = set(ids)

    # Check that all direct dependencies of selected scenarios are also selected
    for sid in selected_set:
        sc = id_to_scenario[sid]
        for dep in sc.dependsOn:
            if dep not in selected_set:
                raise DevTestManifestError(
                    "MISSING_DEPENDENCY",
                    f"Scenario {sid!r} depends on {dep!r}, which is not in the selection. "
                    f"Add {dep!r} explicitly or select all scenarios. "
                    f"Dependencies are not added automatically.",
                )

    # Return in manifest order
    return tuple(sc for sc in suite.scenarios if sc.id in selected_set)
