"""A scriptable fake `sql` for the ORDS tests (run by the Bash `sql` that fake_sqlcl installs).

Everything it does is driven by the JSON file named in FAKE_ORDS_CONFIG:

    {"calls": "<log file>", "version": "<sql -V text>",
     "db": {"tables": null | "exit1", "code": null | "exit1"},
     "ords": {"<SCHEMA>": {"scenario": "ok", "counts": {...}, "user": "<session user>"}}}

It answers `sql -V`, the tables/code driver (backup_db.sql), the ORDS export
driver (ords_export.sql) and the doctor drivers, writes the files the real SQL
would spool into its working directory, and logs one line per call. It never
touches a database. Scenarios for the ORDS export are listed in ORDS_SCENARIOS.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ords_fixtures import COUNTS, export_text, inventory  # noqa: E402

LONG_ADVISORY = (
    "Warning: This LONG setting may cause Java memory problems.",
    "It is recommended to reduce the setting and/or increase the memory available to Java.",
)
EMPTY = {key: 0 for key in COUNTS}
ORDS_SCENARIOS = (
    "ok", "empty", "not-enabled-empty", "not-enabled-inconsistent", "wrong-owner-silent", "wrong-owner-exit1", "error-exit0", "old-ords",
    "no-files", "truncated", "no-inventory", "drift-counts", "drift-content", "oauth-clients", "hang", "no-complete", "exit1",
)


def log(config: dict, line: str) -> None:
    with open(config["calls"], "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def inventory_line(label: str, counts: dict[str, int], enabled: int = 1, **overrides: int) -> str:
    values = {
        "schemas": 1 if enabled else 0, "enabled": enabled, "modules": counts["modules"], "templates": counts["templates"],
        "handlers": counts["handlers"], "parameters": counts["parameters"], "roles": counts["roles"],
        "privileges": counts["privileges"], "privilege_roles": counts["privileges"], "privilege_modules": 0,
        "privilege_mappings": 2 * counts["privileges"], "enabled_objects": counts["enabled_objects"], "clients": 0,
    }
    values.update(overrides)
    return inventory(label, **values)


def ords_session(config: dict, schema: str, expected_user: str, spool_schema: str) -> int:
    settings = config.get("ords", {}).get(schema, {})
    scenario = settings.get("scenario", "ok")
    counts = {**COUNTS, **settings.get("counts", {})}
    user = settings.get("user", schema)
    log(config, f"ords|{schema}|{expected_user}|{os.environ.get('FAKE_CONNECTION', '')}|{scenario}")
    print(LONG_ADVISORY[0])
    print(LONG_ADVISORY[1])
    if scenario == "wrong-owner-exit1":
        print(f"ORA-20061: ORDS export must authenticate as the REST schema owner {schema} but the session user is DEPLOYER")
        return 1
    if scenario == "exit1":
        print("ORA-12154: TNS:could not resolve the connect identifier")
        return 1
    if scenario == "wrong-owner-silent":
        user = "DEPLOYER"
    print(f"SQLcl target: session_user={user}, current_schema={schema}, db_name=DEV, db_unique_name=DEV, service=dev")
    print("ORDS version: 24.2.2.r1871943")
    print(f"ORDS_ACCESS_VERIFIED:{schema}")
    if scenario == "oauth-clients":
        print(inventory_line("before", counts, clients=1))
        print(f"ORA-20064: Schema {schema} owns 1 ORDS OAuth client(s). SQLcl's REST export schema always includes OAuth clients, and this template exports none, so nothing was exported")
        return 1
    if scenario == "empty":
        counts = dict(EMPTY)
    not_enabled = scenario.startswith("not-enabled")
    if not_enabled:
        counts = dict(EMPTY)
    before_overrides = {"enabled": 0, "schemas": 0}
    if scenario == "not-enabled-inconsistent":
        before_overrides = {"enabled": 0, "schemas": 1}
        counts = {**EMPTY, "modules": 1}
    if scenario != "no-inventory":
        print(inventory_line("before", counts, **(before_overrides if not_enabled else {})))
    if scenario == "error-exit0":
        print("ORA-00942: table or view does not exist")
    if not_enabled:
        print("ORDS_EXPORT_MODE:skipped-not-enabled")
    else:
        print("ORDS_EXPORT_MODE:run")
        stage = Path.cwd()
        first = stage / "database" / spool_schema / "ords" / "schema.sql"
        second = stage / "verify" / spool_schema / "schema.sql"
        text = export_text(schema, counts)
        if scenario == "hang":
            marker = settings.get("started")
            if marker:
                Path(marker).write_text("started\n", encoding="utf-8")
            sys.stdout.flush()
            time.sleep(120)
        if scenario == "old-ords":
            message = "Failed to execute the ORDS export function. Please ensure you have latest ORDS version installed in your database.\n"
            print(message.strip())
            first.write_text(message, encoding="utf-8")
            second.write_text(message, encoding="utf-8")
        elif scenario != "no-files":
            first.write_text(text[: len(text) // 2] if scenario == "truncated" else text, encoding="utf-8", newline="")
            second.write_text(
                text.replace("p_name => 'item0'", "p_name => 'changed'") if scenario == "drift-content" else text,
                encoding="utf-8", newline="",
            )
    after_counts = {**counts, "handlers": counts["handlers"] + 1} if scenario == "drift-counts" else counts
    if scenario != "no-inventory":
        print(inventory_line("after", after_counts, **(before_overrides if not_enabled else {})))
    if scenario != "no-complete":
        print(f"ORDS_EXPORT_COMPLETE:{schema}")
    return 0


def database_session(config: dict, arguments: list[str]) -> int:
    schema, scope, spool_schema = arguments[0], arguments[1], arguments[5]
    log(config, f"{scope}|{schema}|{os.environ.get('FAKE_CONNECTION', '')}")
    print(LONG_ADVISORY[0])
    print(LONG_ADVISORY[1])
    print("FAKE SQLCL PROGRESS LINE")
    if config.get("db", {}).get(scope) == "exit1":
        print("ORA-00942: table or view does not exist")
        return 1
    directory = {"tables": "tables", "code": "views"}[scope]
    name = {"tables": "FAKE_TABLE", "code": "FAKE_VIEW"}[scope]
    target = Path.cwd() / "database" / spool_schema / directory
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{name}.sql").write_text(f"CREATE FAKE {name} FOR {schema};\n", encoding="utf-8")
    manifest = Path.cwd() / "database" / spool_schema / f"manifest-{scope}.txt"
    manifest.write_text(
        "TABLE=1\n" if scope == "tables" else "FUNCTION=0\nPACKAGE=0\nPACKAGE BODY=0\nPROCEDURE=0\nSYNONYM=0\nTRIGGER=0\nVIEW=1\n",
        encoding="utf-8",
    )
    return 0


def main(arguments: list[str]) -> int:
    sys.stdin.read()
    config = json.loads(Path(os.environ["FAKE_ORDS_CONFIG"]).read_text(encoding="utf-8"))
    if arguments == ["-V"]:
        log(config, "version")
        print(config.get("version", "SQLcl: Release 26.2.2.0 Production Build: 26.2.2.233.1901"))
        return 0
    connection = arguments[arguments.index("-name") + 1]
    os.environ["FAKE_CONNECTION"] = connection
    at = next(index for index, argument in enumerate(arguments) if argument.startswith("@"))
    script = Path(arguments[at][1:]).name
    rest = arguments[at + 1 :]
    if script == "ords_export.sql":
        return ords_session(config, rest[0], rest[2], rest[3])
    if script == "backup_db.sql":
        return database_session(config, rest)
    if script in {"doctor.sql", "doctor_ords.sql"}:
        schema, user = rest[0], rest[2]
        log(config, f"doctor|{script}|{schema}|{user}|{connection}")
        scenario = config.get("ords", {}).get(schema, {}).get("scenario", "ok") if script == "doctor_ords.sql" else "ok"
        if scenario.startswith("wrong-owner"):
            print(f"ORA-20061: ORDS export must authenticate as the REST schema owner {schema} but the session user is DEPLOYER")
            return 1
        print(f"APEX_DOCTOR_VERIFIED:{user}")
        return 0
    print(f"fake sql: unsupported script {script}", file=sys.stderr)
    return 4


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
