#!/usr/bin/env python3
"""Resolve the configured SQLcl connection, expected user, and schema per environment."""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Iterable, Mapping


ORACLE_IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)
SQLCL_ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z", re.ASCII)
CLASSIFICATIONS = {"development", "test", "staging", "production"}
PRODUCTION_MARKER_RE = re.compile(r"(^|[-_.])(prod|prd|production|live)[0-9]*([-_.]|$)", re.IGNORECASE | re.ASCII)


def looks_like_production_identity(*values: str) -> bool:
    return any(PRODUCTION_MARKER_RE.search(str(value)) is not None for value in values)


class TargetResolutionError(ValueError):
    """Raised when an environment cannot be resolved without unsafe fallback."""


@dataclass(frozen=True)
class Target:
    environment: str
    connection: str
    expected_user: str
    schema: str
    classification: str


def _required(values: Mapping[str, str], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TargetResolutionError(f"set {key} for the selected database environment")
    return value


def _validate_identifier(value: str, key: str) -> None:
    if ORACLE_IDENTIFIER.fullmatch(value) is None:
        raise TargetResolutionError(f"{key} must be an uppercase Oracle identifier")


SCHEMA_KEYS = {"dev": "CODE_SCHEMA", "staging": "STAGING_SCHEMA", "prod": "PROD_SCHEMA"}


def split_list(value: str | None) -> tuple[str, ...]:
    """Split a comma-separated setting. An empty or missing value has no entries."""
    if not value:
        return ()
    return tuple(value.split(","))


def configured_schemas(values: Mapping[str, str], environment: str) -> tuple[str, ...]:
    """Return the schemas an environment's profile lists, in configured order."""
    try:
        key = SCHEMA_KEYS[environment]
    except KeyError:
        raise TargetResolutionError("environment must be dev, staging, or prod") from None
    return split_list(values.get(key))


def _select_index(
    values: Mapping[str, str],
    environment: str,
    schemas: tuple[str, ...],
    requested: str | None,
    schema_key: str,
) -> int:
    if requested is None:
        if len(schemas) == 1:
            return 0
        raise TargetResolutionError(
            f"{schema_key} lists {len(schemas)} schemas ({', '.join(schemas)}); name one with --schema"
        )
    _validate_identifier(requested, "--schema")
    if requested in schemas:
        return schemas.index(requested)
    multi = len(schemas) > 1 or len(split_list(values.get("CODE_SCHEMA"))) > 1
    if len(schemas) == 1 and not multi and environment != "dev":
        # A single-schema project may name its staging or production schema
        # differently from DEV. Several schemas map by name only.
        return 0
    raise TargetResolutionError(
        f"schema {requested} is not listed in {schema_key} for {environment}; "
        f"configured: {', '.join(schemas)}"
    )


def resolve_target(
    values: Mapping[str, str],
    environment: str,
    operation: str,
    schema: str | None = None,
) -> Target:
    """Resolve a read/migration target; never fall back from stage/prod to DEV.

    Several schemas are configured as position-aligned comma lists. Name the
    schema to pick one; a single-entry list needs no name.
    """
    if operation not in {"read", "migration"}:
        raise TargetResolutionError("operation must be read or migration")
    if environment == "dev":
        connection_key = "CODE_SQLCL_CONNECTION"
        user_key = "CODE_EXPECTED_USER"
        schema_key = "CODE_SCHEMA"
        classification = _required(values, "DB_ENVIRONMENT").lower()
        if classification not in CLASSIFICATIONS:
            raise TargetResolutionError("DB_ENVIRONMENT must be development, test, staging, or production")
        if operation == "migration" and classification not in {"development", "test"}:
            raise TargetResolutionError("DEV migrations require DB_ENVIRONMENT=development or test")
    elif environment in {"staging", "prod"}:
        prefix = "STAGING" if environment == "staging" else "PROD"
        connection_key = f"{prefix}_SQLCL_CONNECTION"
        user_key = f"{prefix}_EXPECTED_USER"
        schema_key = f"{prefix}_SCHEMA"
        classification = "staging" if environment == "staging" else "production"
    else:
        raise TargetResolutionError("environment must be dev, staging, or prod")

    if schema is not None and not values.get(schema_key):
        # The loader blanks a profile that does not list the selected schema.
        raise TargetResolutionError(f"schema {schema} is not listed in {schema_key} for {environment}")
    connections = split_list(_required(values, connection_key))
    users = split_list(_required(values, user_key))
    schemas = split_list(_required(values, schema_key))
    if not len(connections) == len(users) == len(schemas):
        raise TargetResolutionError(
            f"{connection_key}, {user_key} and {schema_key} must list the same number of entries"
        )
    if len(set(schemas)) != len(schemas):
        raise TargetResolutionError(f"{schema_key} must not repeat a schema")
    index = _select_index(values, environment, schemas, schema, schema_key)
    connection, expected_user, target_schema = connections[index], users[index], schemas[index]
    if SQLCL_ALIAS.fullmatch(connection) is None:
        raise TargetResolutionError(f"{connection_key} contains unsupported characters")
    if environment != "prod" and looks_like_production_identity(connection):
        raise TargetResolutionError(f"{connection_key} resembles a production connection; use --env prod after reviewing the target")
    _validate_identifier(expected_user, user_key)
    _validate_identifier(target_schema, schema_key)
    return Target(environment, connection, expected_user, target_schema, classification)


def batch_schema(
    schemas: Iterable[str | None],
    requested: str | None,
    values: Mapping[str, str],
) -> str | None:
    """Pick the one schema a batch of migration folders targets.

    ``schemas`` holds each folder's ``migrations/<SCHEMA>/`` name, or ``None``
    for a flat folder. ``requested`` is ``--schema`` or ``PROJECT_SCHEMA``.
    """
    folder_schemas = set(schemas)
    if len(folder_schemas) > 1:
        raise TargetResolutionError("selected migrations belong to different schemas; run one schema at a time")
    folder_schema = next(iter(folder_schemas), None)
    multi = values.get("PROJECT_MULTI_SCHEMA") == "true" or len(split_list(values.get("CODE_SCHEMA"))) > 1
    if multi and folder_schema is None:
        raise TargetResolutionError(
            "several schemas are configured, so migrations must live under migrations/<SCHEMA>/; "
            "move the folder into its schema directory"
        )
    if folder_schema is not None and requested is not None and folder_schema != requested:
        raise TargetResolutionError(
            f"--schema {requested} does not match the migration folder's schema {folder_schema}"
        )
    return folder_schema if folder_schema is not None else requested
