#!/usr/bin/env python3
"""Literal, strict parser and validator for local .env configuration.

Matches the validation and semantics of scripts/load_env.sh and scripts/load_env.ps1:
- Strips UTF-8 byte-order mark (BOM) if present.
- Rejects UTF-16 files.
- Strips outer matching quotes without shell evaluation or command substitution.
- Never discloses secret values or invalid raw lines in error messages.
- Rejects retired uc-apx settings, unknown keys, and duplicate keys.
- Validates identifier, alias, prefix, and aligned profile lists.
"""

from __future__ import annotations

import re
from pathlib import Path


class ConfigError(ValueError):
    """Configuration error without value disclosure."""


ALLOWED_KEYS = frozenset({
    "PROJECT_NAME",
    "DEVELOPER_NAME",
    "DB_ENVIRONMENT",
    "APEX_APP_ID",
    "APEX_WORKSPACE_USERNAME",
    "TABLES_SCHEMA",
    "TABLES_PREFIXES",
    "TABLES_SQLCL_CONNECTION",
    "TABLES_EXPECTED_USER",
    "CODE_SCHEMA",
    "CODE_PREFIXES",
    "CODE_SQLCL_CONNECTION",
    "CODE_EXPECTED_USER",
    "APEX_PARSING_SCHEMA",
    "APEX_SQLCL_CONNECTION",
    "APEX_EXPECTED_USER",
    "PROD_SQLCL_CONNECTION",
    "PROD_EXPECTED_USER",
    "PROD_SCHEMA",
    "STAGING_SQLCL_CONNECTION",
    "STAGING_EXPECTED_USER",
    "STAGING_SCHEMA",
    "ORDS_SCHEMA",
    "ORDS_SQLCL_CONNECTION",
    "ORDS_EXPECTED_USER",
    "MIGRATION_APPLY_TIMEOUT_SECONDS",
    "MIGRATION_CHECK_TIMEOUT_SECONDS",
    "MIGRATION_CHECK_BATCH_BYTES",
    "MIGRATION_PREFLIGHT_INVENTORY_RETRIES",
}) | frozenset(prefix + "MIGRATION_" + suffix
               for prefix in ("", "STAGING_", "PROD_")
               for suffix in ("SCHEMA", "SQLCL_CONNECTION", "EXPECTED_USER"))

REQUIRED_KEYS = (
    "PROJECT_NAME",
    "DEVELOPER_NAME",
    "DB_ENVIRONMENT",
    "APEX_APP_ID",
    "TABLES_SCHEMA",
    "TABLES_PREFIXES",
    "TABLES_SQLCL_CONNECTION",
    "TABLES_EXPECTED_USER",
    "CODE_SCHEMA",
    "CODE_PREFIXES",
    "CODE_SQLCL_CONNECTION",
    "CODE_EXPECTED_USER",
    "APEX_PARSING_SCHEMA",
    "APEX_SQLCL_CONNECTION",
    "APEX_EXPECTED_USER",
)

RE_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
RE_IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}$")
RE_ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RE_PREFIX_LIST = re.compile(r"^[A-Z][A-Z0-9_$#]*(,[A-Z][A-Z0-9_$#]*)*$")
RE_APP_ID_LIST = re.compile(r"^[1-9][0-9]{0,17}(,[1-9][0-9]{0,17})*$")
RE_DEVELOPER_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,29}$")
RE_NONNEGATIVE_INTEGER = re.compile(r"^(?:0|[1-9][0-9]*)$")


def _check_csv_shape(key: str, value: str) -> None:
    if value.startswith(",") or value.endswith(",") or ",," in value:
        raise ConfigError(f"{key} must not contain empty entries")


def _check_unique_csv(key: str, value: str) -> None:
    items = value.split(",")
    if len(items) != len(set(items)):
        raise ConfigError(f"{key} must not contain duplicate values")


def _check_identifier_list(key: str, value: str) -> None:
    _check_csv_shape(key, value)
    for item in value.split(","):
        if not RE_IDENTIFIER.fullmatch(item):
            raise ConfigError(f"{key} must be an uppercase Oracle identifier")


def _check_alias_list(key: str, value: str) -> None:
    _check_csv_shape(key, value)
    for item in value.split(","):
        if not RE_ALIAS.fullmatch(item):
            raise ConfigError(f"{key} contains unsupported characters")


def _check_aligned(schema_key: str, connection_key: str, user_key: str, values: dict[str, str]) -> None:
    schemas = values[schema_key].split(",")
    conns = values[connection_key].split(",")
    users = values[user_key].split(",")
    if len(schemas) != len(conns) or len(schemas) != len(users):
        raise ConfigError(f"{connection_key}, {user_key} and {schema_key} must list the same number of entries")
    _check_unique_csv(schema_key, values[schema_key])


def read_project_env(path: Path) -> dict[str, str]:
    """Parse and strictly validate a project .env file.

    Returns the parsed key-value mapping. Raises ConfigError on any discrepancy,
    ensuring that values or raw invalid lines never appear in error messages.
    """
    if not path.is_file():
        raise ConfigError(f"configuration file not found: {path} (copy .env.example to .env)")

    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file: {path}") from exc

    if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ConfigError(f"{path} is UTF-16; save it as UTF-8 (a UTF-8 byte-order mark is fine)")

    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raw_bytes = raw_bytes[3:]

    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} contains invalid UTF-8 bytes") from exc

    values: dict[str, str] = {}
    seen_keys: set[str] = set()

    for line in content.splitlines():
        line = line.rstrip("\r")
        if not line or line.startswith("#") or not line.strip():
            continue

        match = RE_LINE.fullmatch(line)
        if match is None:
            raise ConfigError(f"invalid line in {path}")

        key = match.group(1)
        raw_val = match.group(2)

        if key in ("INSTALL_UC_APX", "UC_APX_SKILLS_AGENT"):
            raise ConfigError("uc-apx settings are retired; remove INSTALL_UC_APX and UC_APX_SKILLS_AGENT from .env")

        if key not in ALLOWED_KEYS:
            raise ConfigError(f"unsupported setting in {path}: {key}")

        if key in seen_keys:
            raise ConfigError(f"duplicate setting in {path}: {key}")

        quoted = False
        val = raw_val
        if len(val) >= 2 and (
            (val.startswith('"') and val.endswith('"')) or
            (val.startswith("'") and val.endswith("'"))
        ):
            val = val[1:-1]
            quoted = True
        elif val == '"' or val == "'":
            raise ConfigError(f"{key} has an unterminated quoted value")

        if not quoted and re.search(r"\s#", val):
            raise ConfigError(
                f"{key} has an inline comment; .env values are parsed literally, "
                "so put the comment on its own line, or quote the value to keep a literal '#'"
            )

        values[key] = val
        seen_keys.add(key)

    # Required keys verification
    for key in REQUIRED_KEYS:
        if key not in values or not values[key].strip():
            raise ConfigError(f"{key} is required in {path}")

    retry_key = "MIGRATION_PREFLIGHT_INVENTORY_RETRIES"
    if retry_key in values and not RE_NONNEGATIVE_INTEGER.fullmatch(values[retry_key]):
        raise ConfigError(f"{retry_key} must be a non-negative integer")

    # Developer name validation
    if not RE_DEVELOPER_NAME.fullmatch(values["DEVELOPER_NAME"]):
        raise ConfigError("DEVELOPER_NAME must be uppercase letters, digits, or underscores (at most 30), such as ASHARIF")

    # DB Environment validation
    if values["DB_ENVIRONMENT"] not in ("development", "test", "staging", "production"):
        raise ConfigError("DB_ENVIRONMENT must be development, test, staging, or production")

    # APEX_APP_ID validation
    if not RE_APP_ID_LIST.fullmatch(values["APEX_APP_ID"]):
        raise ConfigError("APEX_APP_ID must be a comma-separated list of positive integers of at most 18 digits, without spaces")
    _check_unique_csv("APEX_APP_ID", values["APEX_APP_ID"])

    # Prefix lists validation
    for key in ("TABLES_PREFIXES", "CODE_PREFIXES"):
        val = values[key]
        if val == "*":
            continue
        if not RE_PREFIX_LIST.fullmatch(val):
            raise ConfigError(f"{key} must be * or a comma-separated list of uppercase Oracle identifier prefixes without spaces")
        _check_unique_csv(key, val)
        for prefix in val.split(","):
            if len(prefix) > 128:
                raise ConfigError(f"{key} prefixes must be at most 128 characters")

    # Base profile lists validation
    for key in (
        "TABLES_SCHEMA", "TABLES_EXPECTED_USER",
        "CODE_SCHEMA", "CODE_EXPECTED_USER",
        "APEX_PARSING_SCHEMA", "APEX_EXPECTED_USER",
    ):
        _check_identifier_list(key, values[key])

    for key in ("TABLES_SQLCL_CONNECTION", "CODE_SQLCL_CONNECTION", "APEX_SQLCL_CONNECTION"):
        _check_alias_list(key, values[key])

    _check_aligned("TABLES_SCHEMA", "TABLES_SQLCL_CONNECTION", "TABLES_EXPECTED_USER", values)
    _check_aligned("CODE_SCHEMA", "CODE_SQLCL_CONNECTION", "CODE_EXPECTED_USER", values)
    _check_aligned("APEX_PARSING_SCHEMA", "APEX_SQLCL_CONNECTION", "APEX_EXPECTED_USER", values)

    for prefix in ("", "STAGING_", "PROD_"):
        schema_key, conn_key, user_key = (prefix + "MIGRATION_" + suffix
                                         for suffix in ("SCHEMA", "SQLCL_CONNECTION", "EXPECTED_USER"))
        present = sum(key in seen_keys for key in (schema_key, conn_key, user_key))
        if present not in (0, 3):
            raise ConfigError(f"{schema_key}, {conn_key} and {user_key} must be configured together")
        if present:
            for key in (schema_key, conn_key, user_key):
                if not values[key].strip():
                    raise ConfigError(f"{key} must not be empty")
            _check_identifier_list(schema_key, values[schema_key])
            _check_identifier_list(user_key, values[user_key])
            _check_alias_list(conn_key, values[conn_key])
            _check_aligned(schema_key, conn_key, user_key, values)

    # Optional PROD and STAGING profiles validation
    for prefix in ("PROD", "STAGING"):
        conn_key = f"{prefix}_SQLCL_CONNECTION"
        user_key = f"{prefix}_EXPECTED_USER"
        schema_key = f"{prefix}_SCHEMA"
        has_conn = conn_key in seen_keys
        has_user = user_key in seen_keys
        has_schema = schema_key in seen_keys

        if has_conn != has_user:
            raise ConfigError(f"{conn_key} and {user_key} must be configured together")
        if has_schema and not has_conn:
            raise ConfigError(f"{schema_key} requires {conn_key} and {user_key}")

        if has_conn:
            if not values[conn_key].strip() or not values[user_key].strip():
                raise ConfigError(f"{conn_key} and {user_key} must not be empty")
            _check_alias_list(conn_key, values[conn_key])
            _check_identifier_list(user_key, values[user_key])
            if has_schema:
                _check_identifier_list(schema_key, values[schema_key])
                _check_aligned(schema_key, conn_key, user_key, values)
            else:
                conns = values[conn_key].split(",")
                users = values[user_key].split(",")
                if len(conns) != len(users):
                    raise ConfigError(f"{conn_key} and {user_key} must list the same number of entries")
                if len(conns) > 1:
                    raise ConfigError(f"{schema_key} is required when {conn_key} lists several connections")

    # Optional ORDS profile validation
    ords_keys = ("ORDS_SCHEMA", "ORDS_SQLCL_CONNECTION", "ORDS_EXPECTED_USER")
    ords_count = sum(1 for k in ords_keys if k in seen_keys)
    if ords_count not in (0, 3):
        raise ConfigError("ORDS_SCHEMA, ORDS_SQLCL_CONNECTION and ORDS_EXPECTED_USER must be configured together (all three, or none to leave ORDS disabled)")

    if ords_count == 3:
        for k in ords_keys:
            if not values[k].strip():
                raise ConfigError(f"{k} must not be empty; remove all three ORDS_* settings to leave ORDS disabled")
        _check_identifier_list("ORDS_SCHEMA", values["ORDS_SCHEMA"])
        _check_identifier_list("ORDS_EXPECTED_USER", values["ORDS_EXPECTED_USER"])
        _check_alias_list("ORDS_SQLCL_CONNECTION", values["ORDS_SQLCL_CONNECTION"])
        _check_aligned("ORDS_SCHEMA", "ORDS_SQLCL_CONNECTION", "ORDS_EXPECTED_USER", values)
        ords_schemas = values["ORDS_SCHEMA"].split(",")
        ords_users = values["ORDS_EXPECTED_USER"].split(",")
        for s_item, u_item in zip(ords_schemas, ords_users, strict=True):
            if s_item != u_item:
                raise ConfigError("ORDS_EXPECTED_USER must equal ORDS_SCHEMA entry for entry: the ORDS export logs in as the REST schema owner")

    return values
