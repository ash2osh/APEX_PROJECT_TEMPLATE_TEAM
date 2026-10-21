#!/usr/bin/env python3
"""Start a code migration from the source that is live on DEV now.

`scripts/team.sh new-migration <name> --source "PACKAGE BODY:MY_PKG" --env dev` reads the
stored source of each named object from the DEV code schema through the same read-only,
identity-checked SQLcl capture as `baseline export-source`, and writes
migrations/<SCHEMA>/<today>_<name>-r001/ with:

- one step per object, `CREATE OR REPLACE` plus the live source, ready to edit;
- checks.json: the preconditions require the live source to still match, line by line,
  what was read, so `check-conflicts` and `migrate` refuse when a teammate changed one of
  these objects after the migration was started; the postconditions require each object
  to be VALID without compile errors;
- README.md naming the objects and when they were read.

The local mirror is not used: it is refreshed irregularly and teammates change live code.
Nothing is written to the database. A family that already exists is refused (edit it,
or use `scripts/team.sh revise`).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .baseline import (
    SOURCE_TYPES,
    BaselineError,
    _capture,
    _check,
    _complete,
    _exact_settings_check,
    _session_settings_sql,
    _setting_row,
    _source_check_queries,
    _source_lines,
    _sql_literal,
)
from .compare_schema import CatalogError
from .db_targets import TargetResolutionError, resolve_target
from .migration_manifest import FOLDER_RE, MigrationManifestError, _migration_directories, load_migration
from .path_utils import relative_to_root
from .sqlcl_session import SqlclError

ROOT = Path(__file__).resolve().parents[1]
SECTIONS = ("baseline-source", "baseline-settings")
STEP_SUFFIX = {
    "PACKAGE": "spec", "PACKAGE BODY": "body", "PROCEDURE": "procedure", "FUNCTION": "function",
    "TRIGGER": "trigger", "TYPE": "type", "TYPE BODY": "type-body",
}
NAME_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,127}\Z", re.ASCII)


class NewMigrationError(ValueError):
    """The migration cannot be started; nothing was written."""


def parse_source(value: str) -> tuple[str, str]:
    """`PACKAGE BODY:NAME` (also `package_body:name`) -> ("PACKAGE BODY", "NAME")."""
    kind, separator, name = value.partition(":")
    kind = re.sub(r"[\s_-]+", " ", kind.strip().upper())
    name = name.strip().upper()
    if not separator or kind not in SOURCE_TYPES or NAME_RE.fullmatch(name) is None:
        raise NewMigrationError(
            f"--source must be TYPE:NAME with TYPE one of {', '.join(sorted(SOURCE_TYPES))}; got {value!r}")
    return kind, name


def step_name(index: int, kind: str, name: str) -> str:
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-") or "object"
    if not slug[0].isalpha():
        slug = "obj-" + slug
    return f"{index:03d}-{slug}-{STEP_SUFFIX[kind]}.sql"


def step_sql(schema: str, kind: str, name: str, lines: Sequence[str], *, settings: Mapping | None = None) -> str:
    """CREATE OR REPLACE plus the stored source; the first line names the object schema-qualified when it can."""
    text = list(lines)
    kind_pattern = r"\s+".join(map(re.escape, kind.split()))
    owner_prefix = r'(?:"?[\w$#]+"?\s*\.\s*)?'
    header = re.compile(r"^\s*" + kind_pattern + r"\s+" + owner_prefix + '"?' + re.escape(name) + r'"?(?=[\s(;]|$)',
                        re.IGNORECASE)
    match = header.match(text[0])
    if match:
        text[0] = f'{kind} "{schema}"."{name}"' + text[0][match.end():]
    body = "".join(text)
    if not body.endswith("\n"):
        body += "\n"
    prefix = f"{_session_settings_sql(settings)}\n" if settings else ""
    return f"{prefix}CREATE OR REPLACE {body}/\n"


def owner_check(schema: str) -> dict:
    return _check("code-owner-session", (
        f"SELECT CASE WHEN SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') = {_sql_literal(schema)} "
        f"AND :target_schema = {_sql_literal(schema)} THEN 1 ELSE 0 END FROM DUAL"))


def valid_check(schema: str, kind: str, name: str) -> dict:
    owner, object_name, object_type = _sql_literal(schema), _sql_literal(name), _sql_literal(kind)
    slug = step_name(0, kind, name)[4:-4]
    return _check(f"valid-{slug}", (
        "SELECT CASE WHEN "
        f"(SELECT COUNT(*) FROM ALL_OBJECTS WHERE OWNER = {owner} AND OBJECT_NAME = {object_name} "
        f"AND OBJECT_TYPE = {object_type} AND STATUS = 'VALID') = 1 "
        f"AND (SELECT COUNT(*) FROM ALL_ERRORS WHERE OWNER = {owner} AND NAME = {object_name} AND TYPE = {object_type}) = 0 "
        "THEN 1 ELSE 0 END FROM DUAL"))


def settings_check(schema: str, kind: str, name: str, settings: Mapping) -> dict:
    slug = step_name(0, kind, name)[4:-4]
    check = _exact_settings_check(settings, name, kind, schema)
    return _check(f"settings-{slug}", check["sql"])


def renamed_checks(checks: Sequence[dict], kind: str, name: str) -> list[dict]:
    """The exact-source checks, named for what they prove here."""
    slug = step_name(0, kind, name)[4:-4]
    return [dict(check, id=f"unchanged-since-read-{slug}-{index:03d}") for index, check in enumerate(checks, start=1)]


def readme(name: str, schema: str, objects: Sequence[tuple[str, str]], read_at: str, identity: Mapping) -> str:
    listed = "\n".join(f"- {kind} `{object_name}`" for kind, object_name in objects)
    where = ", ".join(f"{key}={identity[key]}" for key in ("db_name", "service_name", "current_schema") if identity.get(key))
    return (
        f"# {name}\n\n"
        f"Started with `scripts/team.sh new-migration` from the source live on DEV ({schema}) at {read_at}"
        + (f" ({where})" if where else "") + ".\n\n"
        f"{listed}\n\n"
        "Edit the steps. The preconditions check that the live source still matches what was read,\n"
        "so `check-conflicts` and `migrate` refuse if someone changed these objects since. Then take\n"
        "their change into your steps, or start again. The postconditions check that every object is\n"
        "VALID without compile errors; add checks for what your change must do.\n"
    )


def create(
    repo_root: Path,
    name: str,
    sources: Sequence[str],
    *,
    environ: Mapping[str, str],
    schema: str | None = None,
    capture: Callable | None = None,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
) -> Path:
    root = Path(repo_root).resolve()
    if FOLDER_RE.fullmatch(f"2000-01-01_{name}-r001") is None:
        raise NewMigrationError("the name must be lowercase words joined by hyphens, e.g. add-customer-flag")
    objects = list(dict.fromkeys(parse_source(value) for value in sources))
    if not objects:
        raise NewMigrationError("name at least one object with --source TYPE:NAME")
    if any(family == name for _path, _schema, family, _revision in _migration_directories(root)):
        raise NewMigrationError(f"a migration named {name} already exists; edit it or use scripts/team.sh revise")
    try:
        schema = resolve_target(environ, "dev", "read", schema=schema).schema
    except TargetResolutionError as error:
        raise NewMigrationError(str(error)) from error
    started = now()
    read_at = started.strftime("%Y-%m-%d %H:%M:%S UTC")
    scratch = root / "scratch" / "new-migration" / started.strftime("%Y%m%dT%H%M%S%fZ")
    try:
        catalog = (capture or _capture)(environ, "dev", schema, SECTIONS, scratch,
                                        baseline_prefixes=sorted({n for _k, n in objects}))
        _complete(catalog, SECTIONS)
        rows = catalog["sections"]["baseline-source"]
        settings_rows = catalog["sections"].get("baseline-settings", [])
        units = {(kind, object_name): _source_lines(rows, object_name, kind) for kind, object_name in objects
                 if any(row.get("name") == object_name and row.get("type") == kind for row in rows)}
        unit_settings: dict[tuple[str, str], dict] = {}
        for kind, object_name in objects:
            if any(row.get("name") == object_name and row.get("type") == kind for row in settings_rows):
                unit_settings[(kind, object_name)] = _setting_row(settings_rows, object_name, kind)
    except (BaselineError, CatalogError, SqlclError) as error:
        raise NewMigrationError(f"reading DEV failed: {error}") from error
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    missing = [f"{kind} {object_name}" for kind, object_name in objects if (kind, object_name) not in units]
    if missing:
        raise NewMigrationError(f"not found in {schema} on DEV: {', '.join(missing)}")

    steps: dict[str, str] = {}
    preconditions, postconditions = [owner_check(schema)], [owner_check(schema)]
    for index, (kind, object_name) in enumerate(objects, start=1):
        lines = units[(kind, object_name)]
        settings = unit_settings.get((kind, object_name))
        steps[step_name(index, kind, object_name)] = step_sql(schema, kind, object_name, lines, settings=settings)
        unit = {"name": object_name, "type": kind, "lines": lines}
        preconditions += renamed_checks(_source_check_queries(unit, schema), kind, object_name)
        postconditions.append(valid_check(schema, kind, object_name))
        if settings:
            postconditions.append(settings_check(schema, kind, object_name, settings))

    folder = root / "migrations" / schema / f"{started.date().isoformat()}_{name}-r001"
    relative = relative_to_root(folder, root).as_posix()
    folder.parent.mkdir(parents=True, exist_ok=True)
    folder.mkdir()
    try:
        for file_name, sql in steps.items():
            (folder / file_name).write_text(sql, encoding="utf-8", newline="\n")
        checks = {"schemaVersion": 1, "preconditions": preconditions, "postconditions": postconditions}
        (folder / "checks.json").write_text(json.dumps(checks, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        (folder / "README.md").write_text(readme(name, schema, objects, read_at, catalog.get("identity") or {}),
                                          encoding="utf-8", newline="\n")
        load_migration(root, relative)
    except (OSError, MigrationManifestError) as error:
        shutil.rmtree(folder, ignore_errors=True)
        raise NewMigrationError(f"the generated migration did not validate and was removed: {error}") from error
    return folder


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None, repo_root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(prog="scripts/team.sh new-migration", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("name", help="migration name, lowercase words joined by hyphens")
    parser.add_argument("--source", action="append", default=[], metavar="TYPE:NAME",
                        help='object to start from, e.g. "PACKAGE BODY:MY_PKG"; repeat for several')
    parser.add_argument("--env", required=True, choices=("dev",), help="the source is always read from DEV")
    parser.add_argument("--schema", help="code schema, when several are configured")
    args = parser.parse_args(argv)
    values = os.environ if environ is None else environ
    try:
        folder = create(repo_root, args.name, args.source, environ=values,
                        schema=args.schema or values.get("PROJECT_SCHEMA") or None)
    except NewMigrationError as error:
        print(f"new-migration error: {error}", file=sys.stderr)
        return 2
    relative = relative_to_root(folder, Path(repo_root).resolve()).as_posix()
    print(f"Created migration: {relative}")
    for step in sorted(folder.glob("*.sql")):
        print(f"  {step.name}")
    print(f"Edit the steps, then: scripts/team.sh check-conflicts {relative} --env dev")
    return 0


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
