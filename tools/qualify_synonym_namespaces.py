#!/usr/bin/env python3
"""Qualify mixed Oracle object-namespace synonym resolution on an owned fixture."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.apex_compatibility import require_sqlcl_version
from scripts.db_targets import Target
from scripts.migration_checks import CheckReport, preflight
from scripts.migration_manifest import load_batch
from scripts.schema_catalog import capture_inventory_snapshot_with_retries
from scripts.sqlcl_session import SqlclError, run_sqlcl
from tools.probe_apex_26_2 import client_version

ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER_RE = re.compile(r"[A-Z][A-Z0-9_$#]{0,29}\Z", re.ASCII)


class QualificationError(ValueError):
    """The requested target or fixture could not be qualified safely."""


def validate_identity(observed: Mapping[str, object], expected: Mapping[str, object]) -> None:
    fields = ("session_user", "current_schema", "server_host", "service_name")
    differences = [field for field in fields if observed.get(field) != expected.get(field)]
    if differences:
        raise QualificationError("observed SQLcl identity did not match the requested target identity")


def guarded_fixture_write(
    observed: Mapping[str, object],
    expected: Mapping[str, object],
    occupied: Mapping[str, bool],
    write: Callable[[], object],
) -> object:
    validate_identity(observed, expected)
    if any(occupied.values()):
        raise QualificationError("a generated fixture owner or synonym name already exists")
    return write()


def guarded_owned_cleanup(
    *,
    expected_owner: str,
    observed_owner: str | None,
    expected_objects: set[tuple[str, str, str]],
    observed_objects: set[tuple[str, str, str]],
    cleanup: Callable[[], object],
) -> object:
    if observed_owner is not None and observed_owner != expected_owner:
        raise QualificationError("cleanup refused because the fixture owner is not the owned name")
    if observed_owner is None and observed_objects:
        raise QualificationError("cleanup refused because owned objects remain without their recorded owner")
    unexpected = observed_objects - expected_objects
    if unexpected:
        raise QualificationError("cleanup refused because fixture inventory contains unexpected objects")
    return cleanup()


def report_interruption(path: Path, report: Mapping[str, object]) -> None:
    saved = dict(report)
    saved["status"] = "unknown"
    saved["cleanupRequired"] = True
    saved["fixtureCleanup"] = "pending"
    _write_report(path, saved)


def _write_report(path: Path, report: Mapping[str, object]) -> None:
    path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _output_path(output: Path, repo_root: Path) -> Path:
    root = repo_root.resolve()
    scratch = root / "scratch"
    candidate = Path(os.path.abspath(output if output.is_absolute() else root / output))
    try:
        candidate.relative_to(scratch)
    except ValueError as error:
        raise QualificationError("--report-dir must be a fresh directory under this checkout's scratch/") from error
    if candidate == scratch or candidate.exists() or candidate.is_symlink():
        raise QualificationError("qualification requires a fresh report directory")
    for path in (candidate, *candidate.parents):
        if path == root:
            break
        if path.is_symlink():
            raise QualificationError("qualification output cannot contain symlinked parents")
    if not candidate.resolve().is_relative_to(scratch):
        raise QualificationError("qualification output escaped scratch/")
    return candidate


def _identifier(value: str, label: str) -> str:
    if IDENTIFIER_RE.fullmatch(value) is None:
        raise QualificationError(f"{label} must be an uppercase Oracle identifier of at most 30 characters")
    return value


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _hex(value: str) -> str:
    return value.encode("utf-8").hex().upper()


def _names(schema: str, tag: str) -> dict[str, object]:
    schema = _identifier(schema, "schema")
    owner = _identifier(f"QSO_{tag}", "fixture owner")
    table = _identifier(f"QTB_{tag}", "table/index fixture")
    materialized_view = _identifier(f"QMV_{tag}", "materialized-view/index fixture")
    source_table = _identifier(f"QIS_{tag}", "table index source")
    source_mv = _identifier(f"QMS_{tag}", "materialized-view source")
    sequence = _identifier(f"QSE_{tag}", "sequence fixture")
    synonyms = {
        "table": _identifier(f"QST_{tag}", "table synonym"),
        "materializedView": _identifier(f"QSM_{tag}", "materialized-view synonym"),
        "sequence": _identifier(f"QSS_{tag}", "sequence synonym"),
        "missing": _identifier(f"QSN_{tag}", "missing-target synonym"),
    }
    targets = {
        "table": table,
        "materializedView": materialized_view,
        "sequence": sequence,
        "missing": _identifier(f"QNO_{tag}", "missing target"),
    }
    expected_objects = {
        (owner, "TABLE", source_table),
        (owner, "TABLE", source_mv),
        (owner, "TABLE", table),
        (owner, "INDEX", table),
        (owner, "TABLE", materialized_view),
        (owner, "MATERIALIZED VIEW", materialized_view),
        (owner, "INDEX", materialized_view),
        (owner, "SEQUENCE", sequence),
    }
    expected_grants = {
        (owner, object_name, schema, "SELECT")
        for object_name in (source_table, source_mv, table, materialized_view, sequence)
    }
    expected_grants.update({
        (owner, source_table, schema, "INDEX"),
        (owner, source_mv, schema, "INDEX"),
    })
    expected_synonyms = {
        (schema, synonyms[key], owner, targets[key]) for key in synonyms
    }
    temporary_privileges = {"CREATE ANY INDEX", "CREATE ANY MATERIALIZED VIEW"}
    return {
        "schema": schema,
        "owner": owner,
        "table": table,
        "materializedView": materialized_view,
        "sourceTable": source_table,
        "sourceMaterializedView": source_mv,
        "sequence": sequence,
        "synonyms": synonyms,
        "targets": targets,
        "expectedObjects": expected_objects,
        "expectedGrants": expected_grants,
        "expectedSynonyms": expected_synonyms,
        "temporaryPrivileges": temporary_privileges,
    }


class SqlclFixture:
    """Small identity-guarded SQLcl adapter; every log stays under the report dir."""

    def __init__(self, target: Target, host: str, service: str, report_dir: Path) -> None:
        self.target = target
        self.host = host
        self.service = service
        self.report_dir = report_dir
        self.run_counter = 0

    def _guard(self) -> str:
        return f"""ALTER SESSION SET CURRENT_SCHEMA = {self.target.schema};
DECLARE
  c_user CONSTANT VARCHAR2(128) := UTL_RAW.CAST_TO_VARCHAR2(HEXTORAW('{_hex(self.target.expected_user)}'));
  c_schema CONSTANT VARCHAR2(128) := UTL_RAW.CAST_TO_VARCHAR2(HEXTORAW('{_hex(self.target.schema)}'));
  c_host CONSTANT VARCHAR2(256) := UTL_RAW.CAST_TO_VARCHAR2(HEXTORAW('{_hex(self.host)}'));
  c_service CONSTANT VARCHAR2(256) := UTL_RAW.CAST_TO_VARCHAR2(HEXTORAW('{_hex(self.service)}'));
BEGIN
  IF SYS_CONTEXT('USERENV','SESSION_USER') != c_user
     OR SYS_CONTEXT('USERENV','CURRENT_SCHEMA') != c_schema
     OR SYS_CONTEXT('USERENV','SERVER_HOST') != c_host
     OR SYS_CONTEXT('USERENV','SERVICE_NAME') != c_service THEN
    RAISE_APPLICATION_ERROR(-20970, 'SQLcl identity did not match the explicitly selected local target');
  END IF;
END;
/
"""

    def run(self, body: str, *, query: bool = False, phase: str = "inventory") -> str:
        self.run_counter += 1
        private_dir = self.report_dir / "runs" / f"{self.target.connection}-{self.run_counter:03d}"
        private_dir.mkdir(parents=True, mode=0o700)
        driver = private_dir / "qualification.sql"
        head = """SET ECHO OFF
SET VERIFY OFF
SET FEEDBACK OFF
SET DEFINE OFF
SET SERVEROUTPUT ON SIZE UNLIMITED
WHENEVER SQLERROR EXIT FAILURE ROLLBACK
WHENEVER OSERROR EXIT FAILURE ROLLBACK
"""
        if query:
            tail = "\nEXIT SUCCESS ROLLBACK\n"
        else:
            tail = "\nEXIT SUCCESS ROLLBACK\n"
        driver.write_text(head + self._guard() + body + tail, encoding="utf-8", newline="\n")
        driver.chmod(0o600)
        result = run_sqlcl(self.target, driver, private_dir, phase=phase)
        return result.output

    def json_query(self, query: str) -> list[dict]:
        output = self.run(
            "SET SQLFORMAT JSON\nPROMPT QSN_JSON_BEGIN\n" + query.rstrip(";\n") + ";\nPROMPT QSN_JSON_END\n",
            query=True,
        )
        start = output.find("QSN_JSON_BEGIN")
        end = output.find("QSN_JSON_END", start + len("QSN_JSON_BEGIN"))
        if start < 0 or end < 0:
            raise QualificationError("SQLcl did not return a complete JSON observation; inspect private run logs")
        try:
            payload = json.loads(output[start + len("QSN_JSON_BEGIN"):end].strip())
        except json.JSONDecodeError as error:
            raise QualificationError("SQLcl JSON observation was malformed; inspect private run logs") from error
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0].get("items"), list):
            raise QualificationError("SQLcl JSON observation did not match its result contract")
        return results[0]["items"]

    def execute(self, statements: Sequence[str]) -> str:
        body = "\n".join(statement.rstrip(";\n") + ";" for statement in statements) + "\n"
        return self.run(body, phase="apply")

    def identity(self) -> dict:
        rows = self.json_query(
            "SELECT SYS_CONTEXT('USERENV','SESSION_USER') session_user, "
            "SYS_CONTEXT('USERENV','CURRENT_SCHEMA') current_schema, "
            "SYS_CONTEXT('USERENV','SERVER_HOST') server_host, "
            "SYS_CONTEXT('USERENV','SERVICE_NAME') service_name FROM dual"
        )
        if len(rows) != 1:
            raise QualificationError("SQLcl identity query returned an unexpected row count")
        return rows[0]

    def privileges(self) -> set[str]:
        rows = self.json_query("SELECT privilege FROM session_privs ORDER BY privilege")
        return {str(row.get("privilege")) for row in rows}

    def rows(self, query: str) -> list[dict]:
        return self.json_query(query)


def _admin_snapshot_target(target: Target, admin: Target) -> Target:
    return Target(admin.environment, admin.connection, admin.expected_user, target.schema, admin.classification)


def _inventory_queries(names: Mapping[str, object], schema: str) -> dict[str, str]:
    owner = str(names["owner"])
    synonyms = list(names["synonyms"].values())
    syn_list = ",".join(_literal(str(value)) for value in synonyms)
    return {
        "users": f"SELECT username FROM dba_users WHERE username = {_literal(owner)}",
        "objects": f"SELECT owner, object_name, object_type FROM dba_objects WHERE owner = {_literal(owner)} ORDER BY object_name, object_type",
        "synonyms": (
            "SELECT owner, synonym_name, table_owner, table_name FROM dba_synonyms "
            f"WHERE owner = {_literal(schema)} AND synonym_name IN ({syn_list}) ORDER BY synonym_name"
        ),
        "grants": (
            "SELECT owner, table_name, grantee, privilege FROM dba_tab_privs "
            f"WHERE owner = {_literal(owner)} AND grantee = {_literal(schema)} ORDER BY table_name, privilege"
        ),
        "recyclebin": f"SELECT owner, object_name FROM dba_recyclebin WHERE owner = {_literal(owner)}",
        "targetObjects": (
            "SELECT owner, object_name, object_type FROM dba_objects "
            f"WHERE owner = {_literal(schema)} AND object_name IN ({syn_list}) ORDER BY object_name, object_type"
        ),
    }


def _read_inventory(admin: SqlclFixture, names: Mapping[str, object]) -> dict[str, list[dict]]:
    queries = _inventory_queries(names, str(names["schema"]))
    return {key: admin.rows(sql) for key, sql in queries.items()}


def _occupied(inventory: Mapping[str, Sequence[dict]]) -> dict[str, bool]:
    return {
        "owner": bool(inventory["users"]),
        "ownedObjects": bool(inventory["objects"]),
        "targetSynonyms": bool(inventory["synonyms"]),
        "targetObjects": bool(inventory["targetObjects"]),
    }


def _setup_statements(names: Mapping[str, object]) -> list[str]:
    owner = str(names["owner"])
    table = str(names["table"])
    source_table = str(names["sourceTable"])
    source_mv = str(names["sourceMaterializedView"])
    sequence = str(names["sequence"])
    schema = str(names["schema"])
    return [
        f"CREATE USER {owner} NO AUTHENTICATION DEFAULT TABLESPACE USERS QUOTA 20M ON USERS",
        f"GRANT CREATE TABLE TO {owner}",
        f"CREATE TABLE {owner}.{source_table} (id NUMBER NOT NULL)",
        f"CREATE TABLE {owner}.{source_mv} (id NUMBER NOT NULL)",
        f"CREATE TABLE {owner}.{table} (id NUMBER NOT NULL)",
        f"CREATE SEQUENCE {owner}.{sequence}",
        f"GRANT SELECT ON {owner}.{source_table} TO {schema}",
        f"GRANT SELECT ON {owner}.{source_mv} TO {schema}",
        f"GRANT INDEX ON {owner}.{source_table} TO {schema}",
        f"GRANT INDEX ON {owner}.{source_mv} TO {schema}",
        f"GRANT CREATE ANY INDEX, CREATE ANY MATERIALIZED VIEW TO {schema}",
        f"GRANT SELECT ON {owner}.{table} TO {schema}",
        f"GRANT SELECT ON {owner}.{sequence} TO {schema}",
    ]


def _materialized_view_statements(names: Mapping[str, object]) -> list[str]:
    owner = str(names["owner"])
    materialized_view = str(names["materializedView"])
    source_mv = str(names["sourceMaterializedView"])
    table = str(names["table"])
    source_table = str(names["sourceTable"])
    return [
        f"CREATE INDEX {owner}.{table} ON {owner}.{source_table} (id)",
        f"CREATE MATERIALIZED VIEW {owner}.{materialized_view} BUILD IMMEDIATE REFRESH COMPLETE ON DEMAND AS SELECT id FROM {owner}.{source_mv}",
        f"CREATE INDEX {owner}.{materialized_view} ON {owner}.{source_mv} (id)",
    ]


def _grant_materialized_view_select(names: Mapping[str, object]) -> list[str]:
    return [f"GRANT SELECT ON {names['owner']}.{names['materializedView']} TO {names['schema']}"]


def _synonym_statements(names: Mapping[str, object]) -> list[str]:
    schema = str(names["schema"])
    owner = str(names["owner"])
    synonyms = names["synonyms"]
    targets = names["targets"]
    return [
        f"CREATE SYNONYM {schema}.{synonyms[key]} FOR {owner}.{targets[key]}"
        for key in ("table", "materializedView", "sequence", "missing")
    ]


def _capture_snapshot(target: Target, report_dir: Path, label: str):
    _inventory, snapshot = capture_inventory_snapshot_with_retries(
        target,
        (),
        report_dir / "catalog" / label,
    )
    return snapshot


def _dictionary_rows(connection: SqlclFixture, names: Mapping[str, object], catalog: str) -> list[dict]:
    owner = str(names["owner"])
    selected = (str(names["table"]), str(names["materializedView"]))
    name_list = ",".join(_literal(value) for value in selected)
    return connection.rows(
        f"SELECT owner, object_name, object_type, namespace FROM {catalog}_objects "
        f"WHERE owner = {_literal(owner)} AND object_name IN ({name_list}) ORDER BY object_name, object_type"
    )


def _preflight_view(snapshot, schema: str, synonym: str, family: str, root: Path) -> dict:
    folder = root / "migrations" / schema / f"2026-10-12_{family}-r001"
    folder.mkdir(parents=True)
    (folder / "001-create-view.sql").write_text(
        f"CREATE VIEW QV_{family.replace('-', '_').upper()} AS SELECT ID FROM {synonym};\n",
        encoding="utf-8",
        newline="\n",
    )
    (folder / "checks.json").write_text(
        '{"schemaVersion":1,"preconditions":[],"postconditions":[{"id":"fixture-ready","sql":"SELECT 1 FROM dual","expected":1}]}\n',
        encoding="utf-8",
        newline="\n",
    )
    migrations = load_batch(root, [f"migrations/{schema}/{folder.name}"])
    checks = CheckReport(True, True, (), (), {"complete": True})
    return preflight(migrations, snapshot, checks).to_dict()


def _cleanup_owned(admin: SqlclFixture, target: SqlclFixture, names: Mapping[str, object]) -> dict:
    inventory = _read_inventory(admin, names)
    expected_objects = names["expectedObjects"]
    observed_objects = {
        (str(row.get("owner")), str(row.get("object_type")), str(row.get("object_name")))
        for row in inventory["objects"]
    }
    guarded_owned_cleanup(
        expected_owner=str(names["owner"]),
        observed_owner=(
            str(inventory["users"][0].get("username"))
            if inventory["users"] else None
        ),
        expected_objects=set(expected_objects),
        observed_objects=observed_objects,
        cleanup=lambda: None,
    )
    expected_synonyms = set(names["expectedSynonyms"])
    observed_synonyms = {
        (str(row.get("owner")), str(row.get("synonym_name")), str(row.get("table_owner")), str(row.get("table_name")))
        for row in inventory["synonyms"]
    }
    if not observed_synonyms <= expected_synonyms:
        raise QualificationError("cleanup refused because synonym inventory contains unowned rows")
    expected_grants = set(names["expectedGrants"])
    observed_grants = {
        (str(row.get("owner")), str(row.get("table_name")), str(row.get("grantee")), str(row.get("privilege")))
        for row in inventory["grants"]
    }
    if not observed_grants <= expected_grants:
        raise QualificationError("cleanup refused because grant inventory contains unowned rows")
    if inventory["recyclebin"]:
        raise QualificationError("cleanup refused because the fixture owner has recycle-bin rows")
    allowed_target_objects = {
        (str(names["schema"]), str(synonym), "SYNONYM")
        for synonym in names["synonyms"].values()
    }
    observed_target_objects = {
        (str(row.get("owner")), str(row.get("object_name")), str(row.get("object_type")))
        for row in inventory["targetObjects"]
    }
    if not observed_target_objects <= allowed_target_objects:
        raise QualificationError("cleanup refused because a synonym name belongs to an unexpected object")

    synonyms = names["synonyms"]
    if observed_synonyms:
        target.execute([f"DROP SYNONYM {names['schema']}.{synonyms[key]}" for key in synonyms if any(row[1] == synonyms[key] for row in observed_synonyms)])
    if inventory["users"]:
        admin.execute([f"DROP USER {names['owner']} CASCADE"])
    target_privileges = target.privileges()
    revoked_privileges = sorted(set(names["temporaryPrivileges"]) & target_privileges)
    if revoked_privileges:
        admin.execute([f"REVOKE {privilege} FROM {names['schema']}" for privilege in revoked_privileges])

    after = _read_inventory(admin, names)
    remaining = {key: len(value) for key, value in after.items()}
    remaining_privileges = set(names["temporaryPrivileges"]) & target.privileges()
    if any(remaining.values()) or remaining_privileges:
        raise QualificationError("independent cleanup inventory found retained fixture rows")
    return {
        "status": "pass",
        "remaining": remaining,
        "revokedSystemPrivileges": revoked_privileges,
        "remainingSystemPrivileges": sorted(remaining_privileges),
    }


def qualify(
    *,
    target: Target,
    expected_host: str,
    expected_service: str,
    admin_target: Target,
    admin_host: str,
    admin_service: str,
    report_dir: Path,
    repo_root: Path = ROOT,
    allow_writes: bool = False,
) -> dict:
    directory = _output_path(report_dir, repo_root)
    directory.mkdir(parents=True, mode=0o700)
    ordinary = SqlclFixture(target, expected_host, expected_service, directory)
    privileged = SqlclFixture(admin_target, admin_host, admin_service, directory)
    report: dict[str, object] = {
        "schemaVersion": 1,
        "status": "unknown",
        "commit": None,
        "sqlclVersion": None,
        "identity": {},
        "catalogRoutes": {},
        "dictionaryRows": {},
        "preflight": {},
        "fixtureOwnership": None,
        "fixtureCleanup": "not-started",
        "cleanupRequired": False,
        "limitations": [
            "The native result applies only to this observed disposable Docker target and tested commit.",
            "Offline fixtures remain necessary for malformed and impossible dictionary row combinations.",
        ],
    }
    fixture_names: dict[str, object] | None = None
    _write_report(directory / "qualification.json", report)
    try:
        version = client_version(directory / "version")
        require_sqlcl_version(version)
        report["sqlclVersion"] = version
        observed_target = ordinary.identity()
        expected_target = {
            "session_user": target.expected_user,
            "current_schema": target.schema,
            "server_host": expected_host,
            "service_name": expected_service,
        }
        validate_identity(observed_target, expected_target)
        observed_admin = privileged.identity()
        expected_admin = {
            "session_user": admin_target.expected_user,
            "current_schema": admin_target.schema,
            "server_host": admin_host,
            "service_name": admin_service,
        }
        validate_identity(observed_admin, expected_admin)
        admin_privileges = privileged.privileges()
        target_privileges = ordinary.privileges()
        required_admin = {
            "SELECT ANY DICTIONARY", "CREATE USER", "DROP USER", "CREATE ANY TABLE",
            "CREATE ANY INDEX", "CREATE ANY MATERIALIZED VIEW", "CREATE ANY SEQUENCE",
            "GRANT ANY OBJECT PRIVILEGE",
        }
        missing_admin = sorted(required_admin - admin_privileges)
        if missing_admin:
            raise QualificationError("verified admin alias lacks required fixture privileges; no fixture write was attempted")
        if "CREATE SYNONYM" not in target_privileges:
            raise QualificationError("verified owner alias lacks CREATE SYNONYM; no fixture write was attempted")
        if {"CREATE ANY INDEX", "CREATE ANY MATERIALIZED VIEW"} & target_privileges:
            raise QualificationError("owner alias already has fixture-only system privileges; no fixture write was attempted")
        report["identity"] = {"target": observed_target, "admin": observed_admin}
        report["status"] = "refused"
        if not allow_writes:
            report["reason"] = "--allow-writes is required before creating the disposable fixture"
            _write_report(directory / "qualification.json", report)
            return report

        try:
            commit = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo_root, check=True,
                text=True, encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            commit = None
        report["commit"] = commit

        tag = uuid.uuid4().hex[:8].upper()
        names = _names(target.schema, tag)
        fixture_names = names
        report["fixtureOwnership"] = {
            "owner": names["owner"],
            "tag": tag,
            "schema": target.schema,
            "synonyms": names["synonyms"],
            "expectedObjects": sorted([list(item) for item in names["expectedObjects"]]),
            "temporarySystemPrivileges": sorted(names["temporaryPrivileges"]),
            "writeAttempted": False,
        }
        pre_inventory = _read_inventory(privileged, names)
        occupied = _occupied(pre_inventory)
        report["preflightInventory"] = occupied
        if any(occupied.values()):
            report["reason"] = "a generated fixture name was already present; no fixture write was attempted"
            report["status"] = "refused"
            _write_report(directory / "qualification.json", report)
            return report

        report["cleanupRequired"] = True
        report["fixtureCleanup"] = "pending"
        report["status"] = "unknown"
        report["fixtureOwnership"]["writeAttempted"] = True
        _write_report(directory / "qualification.json", report)
        guarded_fixture_write(
            observed_target,
            expected_target,
            occupied,
            lambda: privileged.execute(_setup_statements(names)),
        )
        ordinary.execute(_materialized_view_statements(names))
        privileged.execute(_grant_materialized_view_select(names))
        ordinary.execute(_synonym_statements(names))

        owner_snapshot = _capture_snapshot(target, directory, "owner")
        admin_catalog_target = _admin_snapshot_target(target, admin_target)
        privileged_snapshot = _capture_snapshot(admin_catalog_target, directory, "privileged")
        owner_route = owner_snapshot.coverage.get("path")
        admin_route = privileged_snapshot.coverage.get("path")
        report["catalogRoutes"] = {"owner": owner_route, "privileged": admin_route}
        if owner_route != "OWNER_SESSION" or admin_route != "METADATA_PRIVILEGE":
            raise QualificationError("both ordinary-owner and privileged metadata catalog routes were not exercised")

        object_rows = {
            "owner": _dictionary_rows(ordinary, names, "all"),
            "privileged": _dictionary_rows(privileged, names, "dba"),
        }
        report["dictionaryRows"] = object_rows
        expected_owner_rows = {
            (str(names["table"]), "TABLE", 1),
            (str(names["table"]), "INDEX", 4),
            (str(names["materializedView"]), "TABLE", 1),
            (str(names["materializedView"]), "INDEX", 4),
        }
        expected_privileged_rows = expected_owner_rows | {
            (str(names["materializedView"]), "MATERIALIZED VIEW", 19),
        }
        for route, rows, expected_rows in (
            ("owner", object_rows["owner"], expected_owner_rows),
            ("privileged", object_rows["privileged"], expected_privileged_rows),
        ):
            observed_rows = {
                (str(row.get("object_name")), str(row.get("object_type")), int(row.get("namespace")))
                for row in rows
            }
            if observed_rows != expected_rows:
                raise QualificationError(f"{route} dictionary rows did not match the native mixed-namespace fixture")

        decoded: dict[str, dict[str, dict]] = {}
        for route, snapshot in (("owner", owner_snapshot), ("privileged", privileged_snapshot)):
            decoded_rows = {
                row.name: row
                for row in snapshot.synonyms
                if row.owner == target.schema and row.name in names["synonyms"].values()
            }
            decoded[route] = {
                name: {
                    "targetExists": decoded_rows[name].target_exists,
                    "targetType": decoded_rows[name].target_type,
                    "targetOwner": decoded_rows[name].table_owner,
                    "targetName": decoded_rows[name].table_name,
                }
                for name in decoded_rows
            }
            if set(decoded_rows) != set(names["synonyms"].values()):
                raise QualificationError(f"{route} catalog decoder did not return every selected fixture synonym")
            if decoded_rows[names["synonyms"]["table"]].target_exists is not True or decoded_rows[names["synonyms"]["table"]].target_type != "TABLE":
                raise QualificationError(f"{route} catalog decoder did not resolve the TABLE plus INDEX collision")
            expected_mv_type = "TABLE" if route == "owner" else "MATERIALIZED VIEW"
            if decoded_rows[names["synonyms"]["materializedView"]].target_exists is not True or decoded_rows[names["synonyms"]["materializedView"]].target_type != expected_mv_type:
                raise QualificationError(f"{route} catalog decoder returned an unexpected materialized-view target type")
        report["decodedSynonyms"] = decoded

        all_cases: dict[str, dict[str, dict]] = {}
        for route, snapshot in (("owner", owner_snapshot), ("privileged", privileged_snapshot)):
            cases: dict[str, dict] = {}
            for key, case in (("table", "table-index"), ("materializedView", "mv-index"), ("sequence", "sequence"), ("missing", "missing")):
                synonym = str(names["synonyms"][key])
                result = _preflight_view(
                    snapshot, target.schema, synonym,
                    f"qsn-{tag.lower()}-{route}-{case.lower()}", directory / "preflight",
                )
                cases[key] = result
            all_cases[route] = cases
            for key in ("table", "materializedView"):
                if cases[key]["exit_code"] != 0:
                    raise QualificationError(f"valid {route} {key} synonym did not pass CREATE VIEW migration preflight")
            for key in ("sequence", "missing"):
                if cases[key]["exit_code"] == 0 or not any(
                    conflict.get("code") == "MISSING_PREREQUISITE" for conflict in cases[key].get("conflicts", [])
                ):
                    raise QualificationError(f"invalid {route} {key} synonym unexpectedly passed CREATE VIEW migration preflight")
        report["preflight"] = all_cases

        report["status"] = "passed"
        _write_report(directory / "qualification.json", report)
        report["fixtureCleanup"] = "running"
        _write_report(directory / "qualification.json", report)
        cleanup = _cleanup_owned(privileged, ordinary, names)
        report["cleanupEvidence"] = cleanup
        report["fixtureCleanup"] = "pass"
        report["cleanupRequired"] = False
        _write_report(directory / "qualification.json", report)
        return report
    except KeyboardInterrupt:
        report_interruption(directory / "qualification.json", report)
        raise
    except (QualificationError, SqlclError, OSError, RuntimeError, ValueError) as error:
        report["status"] = "failed" if report.get("cleanupRequired") else "unavailable"
        report["reason"] = f"{type(error).__name__}: {error}"
        if report.get("cleanupRequired"):
            report["fixtureCleanup"] = "pending"
            if fixture_names is not None:
                try:
                    cleanup = _cleanup_owned(privileged, ordinary, fixture_names)
                except (QualificationError, SqlclError, OSError, RuntimeError, ValueError):
                    report["status"] = "unknown"
                else:
                    report["cleanupEvidence"] = cleanup
                    report["fixtureCleanup"] = "pass-after-failure"
                    report["cleanupRequired"] = False
        _write_report(directory / "qualification.json", report)
        return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection", required=True)
    parser.add_argument("--expected-user", required=True)
    parser.add_argument("--schema", required=True)
    parser.add_argument("--server-host", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--admin-connection", required=True)
    parser.add_argument("--admin-expected-user", required=True)
    parser.add_argument("--admin-server-host", required=True)
    parser.add_argument("--admin-service", required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--allow-writes", action="store_true", help="create and clean only the uniquely tagged disposable fixture")
    args = parser.parse_args(argv)
    try:
        schema = _identifier(args.schema, "schema")
        target = Target("dev", args.connection, args.expected_user, schema, "test")
        admin = Target("dev", args.admin_connection, args.admin_expected_user, args.admin_expected_user, "test")
        report = qualify(
            target=target,
            expected_host=args.server_host,
            expected_service=args.service,
            admin_target=admin,
            admin_host=args.admin_server_host,
            admin_service=args.admin_service,
            report_dir=args.report_dir,
            allow_writes=args.allow_writes,
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        return 0 if report["status"] == "passed" else 2
    except (QualificationError, ValueError) as error:
        print(f"qualification: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
