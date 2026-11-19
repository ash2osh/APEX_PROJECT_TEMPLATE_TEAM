#!/usr/bin/env python3
"""Show which database connection, login and schema each operation resolves to. Offline and read-only.

`scripts/team.sh targets [--format text|json]` prints one row per environment (dev, staging, prod), purpose and schema.
The `read` purpose reads code metadata (new-migration --source, compare-schema, baseline export); the `migration` purpose
is what migrate applies. Each row comes from db_targets.resolve_target, the same resolver the commands use, so a REFUSED
row is the refusal the command would print. The dev tables, APEX and ORDS profiles are listed from their own keys.

The report never connects to a database, never runs SQLcl and never prints a credential (none is stored in .env).
The wrappers load .env before they call this module; tests pass a mapping as `environ`.
Exit status: 0 when the report prints (REFUSED rows included); 2 for a usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence

from .db_targets import (
    Target,
    TargetResolutionError,
    configured_migration_schemas,
    configured_schemas,
    resolve_target,
    split_list,
)

SCHEMA_VERSION = 1
ENVIRONMENTS = ("dev", "staging", "prod")
PURPOSES = ("read", "migration")
ROW_KEYS = ("environment", "purpose", "schema", "connection", "expectedUser", "actsAsSchema", "classification", "status", "message")
# Dev profiles outside the resolver: label, schema key, connection key, login key.
NAMED_PROFILES = (
    ("tables", "TABLES_SCHEMA", "TABLES_SQLCL_CONNECTION", "TABLES_EXPECTED_USER"),
    ("apex", "APEX_PARSING_SCHEMA", "APEX_SQLCL_CONNECTION", "APEX_EXPECTED_USER"),
    ("ords", "ORDS_SCHEMA", "ORDS_SQLCL_CONNECTION", "ORDS_EXPECTED_USER"),
)
SPLIT_OWNER_NOTE = (
    "`new-migration --source` reads the code profile while `migrate` uses the migration profile."
)


def _row(
    environment: str,
    purpose: str,
    *,
    schema: str | None = None,
    connection: str | None = None,
    expected_user: str | None = None,
    acts_as_schema: bool | None = None,
    classification: str | None = None,
    status: str = "resolved",
    message: str = "",
) -> dict:
    return {
        "environment": environment,
        "purpose": purpose,
        "schema": schema,
        "connection": connection,
        "expectedUser": expected_user,
        "actsAsSchema": acts_as_schema,
        "classification": classification,
        "status": status,
        "message": message,
    }


def _target_row(environment: str, purpose: str, target: Target) -> dict:
    return _row(
        environment,
        purpose,
        schema=target.schema,
        connection=target.connection,
        expected_user=target.expected_user,
        acts_as_schema=target.acts_as_other_schema,
        classification=target.classification,
    )


def _dev_other_profile(values: Mapping[str, str], purpose: str) -> tuple[str, ...]:
    """The other dev profile's schemas, so a split owner shows up. A refused migration profile lists nothing here."""
    try:
        if purpose == "migration":
            return configured_schemas(values, "dev")
        return configured_migration_schemas(values, "dev")
    except TargetResolutionError:
        return ()


def _unconfigured_reason(values: Mapping[str, str], environment: str, operation: str) -> str:
    try:
        resolve_target(values, environment, operation)
    except TargetResolutionError as error:
        return str(error)
    return "no schema is listed for this profile"


def _purpose_rows(values: Mapping[str, str], environment: str, purpose: str) -> list[dict]:
    """One row per schema the purpose's profile lists. On dev, the other profile's schemas are added as well."""
    try:
        own = configured_migration_schemas(values, environment) if purpose == "migration" else configured_schemas(values, environment)
    except TargetResolutionError as error:
        return [_row(environment, purpose, status="REFUSED", message=str(error))]
    extra: tuple[str, ...] = ()
    if environment == "dev":
        extra = tuple(schema for schema in _dev_other_profile(values, purpose) if schema not in own)
    schemas = own + extra
    if not schemas:
        return [_row(environment, purpose, status="not configured",
                     message=_unconfigured_reason(values, environment, purpose))]
    rows = []
    for schema in schemas:
        try:
            target = resolve_target(values, environment, purpose, schema=schema)
        except TargetResolutionError as error:
            rows.append(_row(environment, purpose, schema=schema, status="REFUSED", message=str(error)))
        else:
            rows.append(_target_row(environment, purpose, target))
    return rows


def _named_profile_rows(values: Mapping[str, str], purpose: str, schema_key: str, connection_key: str, user_key: str) -> list[dict]:
    """A dev profile outside the resolver: its three position-aligned lists, listed without a connection."""
    keys = (schema_key, connection_key, user_key)
    names = ", ".join(keys)
    if not any(values.get(key) for key in keys):
        return [_row("dev", purpose, status="not configured", message=f"set {names} to list this profile")]
    missing = [key for key in keys if not values.get(key)]
    if missing:
        return [_row("dev", purpose, status="REFUSED", message=f"{names} must be configured together; missing {', '.join(missing)}")]
    schemas, connections, users = (split_list(values[key]) for key in keys)
    if not len(schemas) == len(connections) == len(users):
        return [_row("dev", purpose, status="REFUSED", message=f"{names} must list the same number of entries")]
    if any(not entry for entry in (*schemas, *connections, *users)):
        return [_row("dev", purpose, status="REFUSED", message=f"{names} must not contain an empty entry")]
    return [
        _row("dev", purpose, schema=schema, connection=connection, expected_user=user,
             acts_as_schema=user != schema, status="listed")
        for schema, connection, user in zip(schemas, connections, users, strict=True)
    ]


def _notes(values: Mapping[str, str]) -> list[str]:
    try:
        code = configured_schemas(values, "dev")
        migration = configured_migration_schemas(values, "dev")
    except TargetResolutionError as error:
        return [f"the dev migration profile is refused ({error}); see the REFUSED migration row."]
    notes = []
    for schema in code:
        if schema not in migration:
            notes.append(
                f"{schema} is in CODE_SCHEMA but not in the dev migration profile (MIGRATION_SCHEMA). "
                f"{SPLIT_OWNER_NOTE} migrate refuses a migration folder for {schema}."
            )
    for schema in migration:
        if schema not in code:
            notes.append(
                f"{schema} is in the dev migration profile (MIGRATION_SCHEMA) but not in CODE_SCHEMA. "
                f"{SPLIT_OWNER_NOTE} new-migration cannot start a folder for {schema}."
            )
    return notes


def build_report(values: Mapping[str, str]) -> dict:
    """The report as plain data. It reads the mapping only; nothing is connected or run."""
    rows: list[dict] = []
    for environment in ENVIRONMENTS:
        for purpose in PURPOSES:
            rows.extend(_purpose_rows(values, environment, purpose))
    for purpose, schema_key, connection_key, user_key in NAMED_PROFILES:
        rows.extend(_named_profile_rows(values, purpose, schema_key, connection_key, user_key))
    return {"schemaVersion": SCHEMA_VERSION, "rows": rows, "notes": _notes(values)}


def _cell(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def render_text(report: Mapping) -> str:
    headers = ("environment", "purpose", "schema", "connection", "expected login", "acts as other", "classification", "status")
    table = [headers]
    for row in report["rows"]:
        status = row["status"] if not row["message"] else f"{row['status']}: {row['message']}"
        table.append((
            _cell(row["environment"]), _cell(row["purpose"]), _cell(row["schema"]), _cell(row["connection"]),
            _cell(row["expectedUser"]), _cell(row["actsAsSchema"]), _cell(row["classification"]), status,
        ))
    widths = [max(len(line[index]) for line in table) for index in range(len(headers) - 1)]
    lines = ["Effective database targets (offline: no connection is made and no SQLcl runs)"]
    for line in table:
        padded = [line[index].ljust(widths[index]) for index in range(len(widths))]
        lines.append("  ".join(padded + [line[-1]]).rstrip())
    notes = report["notes"]
    if notes:
        lines.append("Notes:")
        lines.extend(f"  - {note}" for note in notes)
    else:
        lines.append("Notes: none")
    return "\n".join(lines)


def main(
    argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
    prog: str = "scripts/team.sh targets",
) -> int:
    parser = argparse.ArgumentParser(
        prog=prog,
        # The docstring names the shell wrapper; a PowerShell caller must not see that name in its help.
        description=(__doc__ or "").replace("scripts/team.sh targets", prog),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="a text table (default) or the JSON report")
    args = parser.parse_args(argv)
    values = os.environ if environ is None else environ
    report = build_report(values)
    if args.format == "json":
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
