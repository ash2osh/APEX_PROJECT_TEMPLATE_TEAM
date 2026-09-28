#!/usr/bin/env python3
"""Resolve the configured SQLcl connection, expected user, and schema per environment."""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections.abc import Mapping


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


def resolve_target(values: Mapping[str, str], environment: str, operation: str) -> Target:
    """Resolve a read/migration target; never fall back from stage/prod to DEV."""
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

    connection = _required(values, connection_key)
    expected_user = _required(values, user_key)
    schema = _required(values, schema_key)
    if SQLCL_ALIAS.fullmatch(connection) is None:
        raise TargetResolutionError(f"{connection_key} contains unsupported characters")
    if environment != "prod" and looks_like_production_identity(connection):
        raise TargetResolutionError(f"{connection_key} resembles a production connection; use --env prod after reviewing the target")
    _validate_identifier(expected_user, user_key)
    _validate_identifier(schema, schema_key)
    return Target(environment, connection, expected_user, schema, classification)
