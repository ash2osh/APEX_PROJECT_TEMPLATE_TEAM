"""Live check of the baseline generator output and catalog assumptions against a DISPOSABLE Oracle database.

The scenarios create objects whose names start with LVF_ in the connected user's schema, run the
generator's real output through SQLcl, and drop every LVF_ object again at the end, also when a
scenario fails. Run it only against a scratch or otherwise disposable database: it creates and drops
objects in the schema it logs in as.

Usage:
    python3 scripts/live_verify.py --connection NAME --schema SCHEMA --confirm-target DB_UNIQUE_NAME \
        [--scenario NAME ...] [--sqlcl PATH]
    python3 scripts/live_verify.py --list

    --connection      saved SQLcl connection name (required unless --list)
    --schema          the schema the connection logs in as (required unless --list); stored upper-case
    --confirm-target  the database unique name the database reports (required unless --list)
    --scenario        run only this scenario; repeatable; the scenario_ prefix is optional
    --sqlcl           path to the SQLcl executable (default: the sql found on PATH)
    --list            print the scenario names, one per line, and exit

Exit codes:
    0  every check passed
    1  a check failed, a scenario raised, or cleanup left LVF_ objects behind
    2  usage error, SQLcl not found, or the safety guard refused the run (nothing was created or dropped)

Safety: before anything is created or dropped, one read-only query reads the connected user and the
database unique name. The run goes ahead only when the user equals --schema and the database unique
name equals --confirm-target (both case-insensitive). The operator must type the database unique name
that the database reports; this tool never guesses it. Importing this module does no work.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import baseline_bundles as bb

SQL_PREAMBLE = (
    "set feedback off\nset heading off\nset pagesize 0\nset linesize 4000\nset serveroutput on\n"
    "whenever sqlerror continue\n"
)
ANSI_COLOUR = re.compile(r"\x1b\[[0-9;]*m")
IDENTITY_SQL = "select user||'|'||sys_context('USERENV','DB_UNIQUE_NAME') from dual;\n"
LEFT_SQL = "select count(*) from user_objects where object_name like 'LVF\\_%' escape '\\';\n"
CLEANUP_SQL = """
begin
  for r in (select table_name from user_tables where table_name like 'LVF\\_%' escape '\\') loop
    execute immediate 'drop table "' || r.table_name || '" cascade constraints purge';
  end loop;
  for r in (select sequence_name from user_sequences where sequence_name like 'LVF\\_%' escape '\\') loop
    execute immediate 'drop sequence "' || r.sequence_name || '"';
  end loop;
  for r in (select type_name from user_types where type_name like 'LVF\\_%' escape '\\') loop
    execute immediate 'drop type "' || r.type_name || '" force';
  end loop;
end;
/
"""

# Set by main() and read by run_sql() and the scenarios. Nothing is read from sys.argv at import time.
CONNECTION: str | None = None
SCHEMA: str | None = None
SQLCL: str | None = None
RESULTS: list[tuple[str, bool, str]] = []
# Every SQL output of the scenario that is running (reset by main() before each scenario).
SQL_OUTPUTS: list[str] = []


def _find_sqlcl(path: str | None) -> str | None:
    if path:
        return path if os.path.isfile(path) else None
    return shutil.which("sql")


def run_sql(text: str, timeout: int = 180) -> str:
    """Run a SQL script through SQLcl with the saved connection and return its stdout and stderr."""
    sqlcl = SQLCL or _find_sqlcl(None)
    if not sqlcl:
        raise RuntimeError("SQLcl (sql) not found")
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8", newline="\n") as handle:
        handle.write(SQL_PREAMBLE)
        handle.write(text)
        handle.write("\nexit\n")
        path = handle.name
    try:
        result = subprocess.run(
            [sqlcl, "-S", "-name", CONNECTION, f"@{path}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    finally:
        Path(path).unlink(missing_ok=True)
    return ANSI_COLOUR.sub("", result.stdout + result.stderr)


def sql(text: str, timeout: int = 180) -> str:
    """Run a scenario's SQL and keep its output for the scenario's own checks."""
    output = run_sql(text, timeout)
    SQL_OUTPUTS.append(output)
    return output


def _last_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def scalar(query: str) -> str:
    return _last_line(sql(query.rstrip(";") + ";"))


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def has_error(output: str, code: str) -> bool:
    return f"ORA-{code}" in output


def row(table: str, name: str, condition: str, status: str = "ENABLED", validated: str = "VALIDATED") -> dict:
    return {"table_name": table, "name": name, "constraint_type": "C", "columns": [], "condition": condition,
            "status": status, "validated": validated}


def constraint_state(table: str, name: str) -> str:
    return scalar(
        f"select status||'|'||validated||'|'||search_condition_vc from user_constraints where table_name = '{table}' and constraint_name = '{name}'"
    )


def cleanup() -> int | None:
    """Drop every LVF_ object in the schema; return how many are left (None when the count is unreadable)."""
    run_sql(CLEANUP_SQL)
    left = _last_line(run_sql(LEFT_SQL))
    count = int(left) if left.isdigit() else None
    print(f"cleanup: objects left = {left if count is not None else 'unknown'}", flush=True)
    return count


def _guarded(label: str, step) -> object:
    """Run one step; an exception is printed with its traceback and recorded as a failed check."""
    try:
        return step()
    except Exception:  # noqa: BLE001 - recorded as a failed check, the run goes on
        detail = traceback.format_exc()
        print(detail, end="", flush=True)
        check(f"{label} ran without an exception", False, detail.strip().splitlines()[-1])
        return None


def scenario_check_replacement() -> None:
    t = "LVF_T"
    sql(f"""
create table {t} (id number primary key, price number, "A B" number, "AB" number);
insert into {t} values (1, 3, 1, 1);
insert into {t} values (2, 4, 1, 1);
commit;
alter table {t} add constraint {t}_CK check (price >= 2) enable validate;
""")
    # a. replace the reviewed condition with a stricter one that existing rows satisfy
    block = bb._wrap_constraint(row(t, f"{t}_CK", "price >= 3"), SCHEMA, SCHEMA, replace_check=True,
                                reviewed_condition="price >= 2", reviewed_status="ENABLED")
    out = sql(block)
    state = constraint_state(t, f"{t}_CK")
    check("check replace: compiles, replaces the reviewed condition", state.startswith("ENABLED|VALIDATED|price >= 3"), out[-300:] + "|" + state)
    # b. idempotent rerun: the live condition already equals the desired one
    out = sql(block)
    check("check replace: idempotent re-run keeps the new condition", constraint_state(t, f"{t}_CK").startswith("ENABLED|VALIDATED|price >= 3") and "ORA-" not in out, out[-300:])
    # c. live changed after review: block reviewed `price >= 1`, live is `price >= 3` -> refuse, no drop
    stale = bb._wrap_constraint(row(t, f"{t}_CK", "price >= 4"), SCHEMA, SCHEMA, replace_check=True,
                                reviewed_condition="price >= 1", reviewed_status="ENABLED")
    out = sql(stale)
    check("check replace: stale review refused with ORA-20994 and constraint untouched",
          has_error(out, "20994") and constraint_state(t, f"{t}_CK").startswith("ENABLED|VALIDATED|price >= 3"), out[-300:])
    # d. reviewed status mismatch: live ENABLED, reviewed says DISABLED -> refuse
    wrong_status = bb._wrap_constraint(row(t, f"{t}_CK", "price >= 4"), SCHEMA, SCHEMA, replace_check=True,
                                       reviewed_condition="price >= 3", reviewed_status="DISABLED")
    out = sql(wrong_status)
    check("check replace: status changed after review refused with ORA-20994", has_error(out, "20994") and constraint_state(t, f"{t}_CK").startswith("ENABLED"), out[-300:])
    # e. same name but not a check constraint
    pk = scalar(f"select constraint_name from user_constraints where table_name = '{t}' and constraint_type = 'P'")
    out = sql(bb._wrap_constraint(row(t, pk, "price >= 4"), SCHEMA, SCHEMA, replace_check=True,
                                  reviewed_condition="price >= 3", reviewed_status="ENABLED"))
    check("check replace: a primary key with the same name is refused with ORA-20993", has_error(out, "20993"), out[-300:])
    # f. precondition SQL: rows violating the new condition are counted
    pre = f"SELECT COUNT(*) FROM {SCHEMA}.{t} WHERE NOT (price >= 4)"
    check("check replace: precondition query runs and counts the violating row", scalar(pre) == "1", scalar(pre))
    # g. violating replacement: precondition would block; without it the ADD fails and the drop already happened
    bad = bb._wrap_constraint(row(t, f"{t}_CK", "price >= 4"), SCHEMA, SCHEMA, replace_check=True,
                              reviewed_condition="price >= 3", reviewed_status="ENABLED")
    out = sql(bad)
    left = constraint_state(t, f"{t}_CK")
    check("check replace: known hazard confirmed live (ADD fails after DROP) - the precondition is what prevents it",
          has_error(out, "02293") and left == "", f"{out[-200:]}|{left}")
    # restore the constraint for the next checks
    sql(f"alter table {t} add constraint {t}_CK check (price >= 3) enable validate;")
    # h. disabled stays disabled on re-run
    sql(f"alter table {t} modify constraint {t}_CK disable;")
    dis = bb._wrap_constraint(row(t, f"{t}_CK", "price >= 3", status="DISABLED", validated="NOT VALIDATED"), SCHEMA, SCHEMA,
                              replace_check=True, reviewed_condition="price >= 3", reviewed_status="DISABLED")
    out = sql(dis)
    check("check replace: a DISABLED desired constraint stays DISABLED on a re-run", constraint_state(t, f"{t}_CK").startswith("DISABLED"), out[-300:] + "|" + constraint_state(t, f"{t}_CK"))
    # i. quoted identifiers: whitespace differences outside quotes are ignored, inside quotes they matter
    sql(f"alter table {t} drop constraint {t}_CK;\nalter table {t} add constraint {t}_QK check (\"A B\" + \"AB\" > 0) enable validate;")
    desired_same = bb._wrap_constraint(row(t, f"{t}_QK", '"A B"+"AB">0'), SCHEMA, SCHEMA, replace_check=True,
                                       reviewed_condition='"A B" + "AB" > 0', reviewed_status="ENABLED")
    out = sql(desired_same)
    check("check replace: quoted-identifier condition equal after normalising spaces outside quotes (no drop)",
          "ORA-" not in out and constraint_state(t, f"{t}_QK").startswith("ENABLED"), out[-300:])
    collide = bb._wrap_constraint(row(t, f"{t}_QK", '"A B" + "AB" > 5'), SCHEMA, SCHEMA, replace_check=True,
                                  reviewed_condition='"AB" + "AB" > 0', reviewed_status="ENABLED")
    out = sql(collide)
    check("check replace: a reviewed condition that differs inside an identifier is refused (ORA-20994)", has_error(out, "20994"), out[-300:])
    # j. no PL/SQL compile error in any block this scenario ran (every output is collected by sql())
    bad_output = next((o for o in SQL_OUTPUTS if "PLS-" in o), "")
    check("check replace: no PLS compile error in any block run", bad_output == "", bad_output[-200:])


def scenario_sequence() -> None:
    sql("""
create sequence LVF_SEQ increment by 10 start with 1;
create table LVF_S (id number primary key);
insert into LVF_S select level * 7 from dual connect by level <= 30;
commit;
""")
    seq_row = {"name": "LVF_SEQ", "increment_by": 1}
    block = bb._sync_sequence_sql(SCHEMA, seq_row, {"table": "LVF_S", "column": "ID"})
    out = sql(block)
    inc = scalar("select increment_by from user_sequences where sequence_name = 'LVF_SEQ'")
    nxt = scalar("select LVF_SEQ.nextval from dual")
    check("sequence sync: restores the target's own increment (10), not the source's (1)", inc == "10", f"{inc} {out[-200:]}")
    check("sequence sync: next value is past MAX(id)=210", nxt.isdigit() and int(nxt) > 210, nxt)
    out = sql(block)
    check("sequence sync: second run is a no-op without errors", "ORA-" not in out and scalar("select increment_by from user_sequences where sequence_name = 'LVF_SEQ'") == "10", out[-200:])


def scenario_type_owner() -> None:
    sql("""
create type LVF_ADDR_T as object (street varchar2(40), city varchar2(40));
/
create table LVF_O (id number primary key);
""")
    col = {"name": "DELIVERY", "table_name": "LVF_O", "data_type": "LVF_ADDR_T", "data_type_owner": SCHEMA, "nullable": "Y"}
    ddl = f"ALTER TABLE \"{SCHEMA}\".\"LVF_O\" ADD ({bb._column_sql(col, SCHEMA, SCHEMA)})"
    out = sql(ddl + ";")
    check("type owner: owner-qualified ADD COLUMN executes", "ORA-" not in out, out[-300:] + "|" + ddl)
    post = bb._column_definition_check(SCHEMA, col, SCHEMA)["sql"]
    check("type owner: postcondition query returns 1", scalar(post) == "1", scalar(post))
    other = dict(col, data_type_owner="SOMEONE_ELSE")
    post2 = bb._column_definition_check(SCHEMA, other, SCHEMA)["sql"]
    check("type owner: postcondition returns 0 when the owner differs", scalar(post2) == "0", scalar(post2))


def scenario_fk_order() -> None:
    names = ["LVF_A_CHILD", "LVF_Z_PARENT"]
    constraints = {("LVF_A_CHILD", "FK1"): {"constraint_type": "R", "referenced_table": "<CONFIGURED_SCHEMA>.LVF_Z_PARENT"}}
    order = bb._new_table_order(names, constraints, SCHEMA)
    check("fk order: parent first for catalog-shaped references", order == ["LVF_Z_PARENT", "LVF_A_CHILD"], str(order))
    out = sql("""
create table LVF_Z_PARENT (id number primary key);
create table LVF_A_CHILD (id number primary key, pid number, constraint LVF_A_CHILD_FK foreign key (pid) references LVF_Z_PARENT(id));
""")
    check("fk order: tables created in that order with the foreign key succeed", "ORA-" not in out, out[-200:])


def scenario_grants_and_catalog() -> None:
    sql("create table LVF_G (id number primary key);\ngrant select on LVF_G to public;")
    q = ("select count(*) from all_tab_privs p where p.table_schema = '" + SCHEMA + "' and p.table_name = 'LVF_G' "
         "and p.grantee = 'PUBLIC' and p.privilege = 'SELECT' and p.grantable = 'NO'")
    check("grants: ALL_TAB_PRIVS.GRANTABLE column exists and reads NO for a plain grant", scalar(q) == "1", scalar(q))
    q2 = q.replace("grantable = 'NO'", "grantable = 'YES'")
    check("grants: the YES predicate returns 0 for the same grant", scalar(q2) == "0", scalar(q2))
    # ALL / column grants as Oracle records them
    sql("create table LVF_G2 (id number primary key, total number);\ngrant update (total) on LVF_G2 to public;\ngrant all on LVF_G to public;")
    privs = scalar("select listagg(privilege, ',') within group (order by privilege) from all_tab_privs where table_schema = '" + SCHEMA + "' and table_name = 'LVF_G' and grantee = 'PUBLIC'")
    check("grants: GRANT ALL is recorded as individual privileges (never ALL/ALLPRIVILEGES)", "ALL" not in privs.split(",") and "ALLPRIVILEGES" not in privs and "SELECT" in privs and "UPDATE" in privs, privs)
    col = scalar("select count(*) from all_tab_privs where table_schema = '" + SCHEMA + "' and table_name = 'LVF_G2' and privilege like 'UPDATE%'")
    check("grants: a column grant is not in ALL_TAB_PRIVS as UPDATE(TOTAL)", scalar("select count(*) from all_tab_privs where table_schema = '" + SCHEMA + "' and table_name = 'LVF_G2' and privilege = 'UPDATE(TOTAL)'") == "0", col)


def scenario_hidden_columns() -> None:
    sql("""
create table LVF_H (id number primary key, secret varchar2(10) invisible, shown varchar2(10), v1 number as (id * 2) virtual);
create index LVF_H_FN on LVF_H (upper(shown));
""")
    rows = sql("select column_name||'|hidden='||hidden_column||'|user_generated='||user_generated||'|virtual='||virtual_column||'|default='||substr(data_default_vc,1,40) from user_tab_cols where table_name = 'LVF_H' order by internal_column_id;")
    print("hidden/virtual column catalog rows:\n" + rows, flush=True)
    secret = [line for line in rows.splitlines() if line.startswith("SECRET|")]
    check("invisible column: catalog shows HIDDEN_COLUMN=YES and USER_GENERATED=YES", bool(secret) and "hidden=YES" in secret[0] and "user_generated=YES" in secret[0], str(secret))
    v1 = [line for line in rows.splitlines() if line.startswith("V1|")]
    check("virtual column: expression is available in DATA_DEFAULT", bool(v1) and "virtual=YES" in v1[0] and "ID\"*2" in v1[0].replace(" ", "").upper(), str(v1))
    sysgen = [line for line in rows.splitlines() if line.startswith("SYS_NC")]
    check("function-based index: a system-generated hidden column exists (hidden=YES, user_generated=NO)", bool(sysgen) and "user_generated=NO" in sysgen[0], str(sysgen))


def scenario_invisible() -> None:
    sql("create table LVF_I (id number primary key, shown varchar2(10));")
    plain = {"name": "NOTE_X", "table_name": "LVF_I", "data_type": "NUMBER", "nullable": "Y", "hidden": "Y", "user_generated": "YES", "virtual": "N"}
    ddl = f"ALTER TABLE \"{SCHEMA}\".\"LVF_I\" ADD ({bb._column_sql(plain, SCHEMA, SCHEMA)})"
    out = sql(ddl + ";")
    check("invisible: ADD COLUMN ... INVISIBLE executes", "ORA-" not in out and "INVISIBLE" in ddl, out[-300:] + "|" + ddl)
    post = bb._column_definition_check(SCHEMA, plain, SCHEMA)["sql"]
    check("invisible: postcondition returns 1 for the invisible column", scalar(post) == "1", scalar(post))
    visible = dict(plain, hidden="N")
    check("invisible: the same postcondition shape for a visible column does not require HIDDEN_COLUMN",
          "HIDDEN_COLUMN" not in bb._column_definition_check(SCHEMA, visible, SCHEMA)["sql"])
    check("invisible: USER_TAB_COLS reports it hidden and user generated", scalar("select count(*) from user_tab_cols where table_name = 'LVF_I' and column_name = 'NOTE_X' and hidden_column = 'YES' and user_generated = 'YES'") == "1")
    ident = {"name": "SEQ_X", "table_name": "LVF_I", "data_type": "NUMBER", "nullable": "Y", "hidden": "Y", "user_generated": "YES", "virtual": "N"}
    ddl2 = f"ALTER TABLE \"{SCHEMA}\".\"LVF_I\" ADD ({bb._column_sql(ident, SCHEMA, SCHEMA)} GENERATED BY DEFAULT AS IDENTITY)"
    out2 = sql(ddl2 + ";")
    check("invisible: INVISIBLE followed by GENERATED ... AS IDENTITY executes (clause order)", "ORA-" not in out2, out2[-300:] + "|" + ddl2)
    # kind classification matches what the live catalog reports for the three kinds of hidden/virtual columns
    sql("create table LVF_K (id number primary key, secret varchar2(5) invisible, shown varchar2(5), v1 number as (id * 2) virtual);\ncreate index LVF_K_FN on LVF_K (upper(shown));")
    rows = scalar("select listagg(column_name||':'||case when hidden_column='YES' then 'Y' else 'N' end||':'||user_generated||':'||case when virtual_column='YES' then 'Y' else 'N' end, ';') within group (order by internal_column_id) from user_tab_cols where table_name = 'LVF_K'")
    kinds = {}
    for item in rows.split(";"):
        name, hid, usr, virt = item.split(":")
        kinds[name] = bb._column_kind({"table_name": "LVF_K", "name": name, "hidden": hid, "user_generated": usr, "virtual": virt})
    check("invisible: the generator classifies the live catalog's columns correctly",
          kinds.get("SECRET") == "hidden" and bb._is_invisible({"hidden": "Y", "user_generated": "YES", "virtual": "N"}) and kinds.get("V1") == "virtual" and any(v == "system" for k, v in kinds.items() if k.startswith("SYS_NC")), str(kinds))


SCENARIOS = (
    scenario_check_replacement,
    scenario_sequence,
    scenario_type_owner,
    scenario_fk_order,
    scenario_grants_and_catalog,
    scenario_hidden_columns,
    scenario_invisible,
)


def _short_name(scenario) -> str:
    return scenario.__name__.removeprefix("scenario_")


def _scenario_name(value: str) -> str:
    name = value if value.startswith("scenario_") else "scenario_" + value
    if name not in {scenario.__name__ for scenario in SCENARIOS}:
        raise argparse.ArgumentTypeError(f"unknown scenario {value!r}; see --list")
    return name


def _read_identity(output: str) -> tuple[str, str]:
    line = _last_line(output)
    user, separator, database = line.partition("|")
    if not separator or not user.strip() or not database.strip():
        raise ValueError(f"unexpected identity output {line[:80]!r}")
    return user.strip(), database.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts/live_verify.py",
        description="Live check of the baseline generator against a disposable Oracle schema (creates and drops LVF_ objects).",
    )
    parser.add_argument("--connection", help="saved SQLcl connection name (required unless --list)")
    parser.add_argument("--schema", help="schema the connection logs in as (required unless --list)")
    parser.add_argument("--confirm-target", help="database unique name the database reports (required unless --list)")
    parser.add_argument("--scenario", action="append", type=_scenario_name, help="run only this scenario (repeatable)")
    parser.add_argument("--sqlcl", help="path to the SQLcl executable (default: sql on PATH)")
    parser.add_argument("--list", action="store_true", help="print the scenario names and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    global CONNECTION, SCHEMA, SQLCL
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list:
        for scenario in SCENARIOS:
            print(_short_name(scenario))
        return 0
    missing = [flag for flag, value in (("--connection", args.connection), ("--schema", args.schema),
                                        ("--confirm-target", args.confirm_target)) if not value]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))
    sqlcl = _find_sqlcl(args.sqlcl)
    if sqlcl is None:
        print("error: SQLcl not found; put sql on PATH or pass --sqlcl PATH", file=sys.stderr, flush=True)
        return 2
    CONNECTION = args.connection
    SCHEMA = args.schema.upper()
    SQLCL = sqlcl
    RESULTS.clear()
    SQL_OUTPUTS.clear()

    try:
        user, database = _read_identity(run_sql(IDENTITY_SQL))
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        print(f"refused: could not read the connected user and database unique name ({exc}); "
              "nothing was created or dropped", file=sys.stderr, flush=True)
        return 2
    if user.upper() != SCHEMA or database.upper() != args.confirm_target.upper():
        print("refused: this run creates and drops LVF_ objects in the schema. The connection reports user "
              f"{user} and database unique name {database}. --schema and --confirm-target must match what the "
              "database reports; type the observed database unique name to continue. "
              "Nothing was created or dropped.", file=sys.stderr, flush=True)
        return 2

    selected = [scenario for scenario in SCENARIOS if not args.scenario or scenario.__name__ in args.scenario]
    print(f"target: {user}@{database}", flush=True)
    try:
        _guarded("cleanup", cleanup)
        for scenario in selected:
            SQL_OUTPUTS.clear()
            _guarded(scenario.__name__, scenario)
    finally:
        left = _guarded("cleanup", cleanup)
        check("cleanup leaves no LVF_ objects", left == 0, f"objects left = {left}")

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    for name in failed:
        print("  FAILED:", name)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
