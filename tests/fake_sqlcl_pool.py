"""A fake `sql` that follows the persistent-session protocol of scripts/sqlcl_pool.py.

It interprets the small subset of SQLcl the pool and its tests use: nested @/@@ scripts (refusing a depth
above 20 like SP2-0309), cd, PROMPT, SET/STORE SET, DEFINE/UNDEFINE, WHENEVER SQLERROR EXIT/CONTINUE,
connect -name, COMMIT/ROLLBACK, the pool's session-init and reset PL/SQL blocks, and test-only statements:

    FAKE_SQL_ERROR;      print an ORA- error; exit 2 under WHENEVER SQLERROR EXIT
    FAKE_SLEEP <s>;      sleep
    FAKE_OPEN_TXN;       leave an open transaction       FAKE_SET_PKG;  set package state
    FAKE_SHOW_STATE;     print the session state as JSON  FAKE_EXIT_OS;  the process dies (exit 3)

Environment: FAKE_POOL_LOG (append one JSON line per event), FAKE_NO_COMPILER=1 (the baseline has no
compiler settings), FAKE_RESET_FAIL=<n> (the n-th reset block fails), FAKE_START_FAIL=1 (connect fails).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT = r'''#!/usr/bin/env python3
import json, os, sys, time

state = {"whenever": False, "defines": {}, "sets": {}, "txn": False, "schema": None, "pkg": False,
         "session": 1, "connects": 0, "resets": 0, "cwd": os.getcwd()}
LOG = os.environ.get("FAKE_POOL_LOG")


def record(**event):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(event, pid=os.getpid())) + "\n")


def emit(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def error(text):
    emit(text)
    if state["whenever"]:
        record(event="exit", code=2)
        sys.exit(2)


def unquote(value):
    value = value.strip()
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value


def run_file(path, depth):
    if depth > 20:
        emit("SP2-0309: SQLcl command procedures may only be nested to a depth of 20.")
        return
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    run_lines(text.split("\n"), depth, os.path.dirname(path))


def run_block(block):
    if "TEAM_POOL_BASELINE" in block:
        if os.environ.get("FAKE_START_FAIL"):
            emit("SP2-0640: Not connected")
            return
        compiler = None if os.environ.get("FAKE_NO_COMPILER") else {"plsql_optimize_level": "2", "plsql_code_type": "INTERPRETED"}
        emit("TEAM_POOL_BASELINE:" + json.dumps({"session_user": "APP", "edition": "ORA$BASE",
             "nls": {"NLS_LANGUAGE": "AMERICAN", "NLS_DATE_FORMAT": "DD-MON-RR"}, "compiler": compiler}))
        return
    if "DBMS_SESSION.RESET_PACKAGE" in block:
        state["resets"] += 1
        record(event="reset", number=state["resets"], session=state["session"])
        if os.environ.get("FAKE_RESET_FAIL") == str(state["resets"]):
            error("ORA-20961: TEAM_POOL_RESET: session edition changed")
            return
        state.update(txn=False, schema=None, pkg=False)


def run_lines(lines, depth, here):
    block = None
    for raw in lines:
        line = raw.strip()
        if block is not None:
            if line == "/":
                run_block("\n".join(block))
                block = None
            else:
                block.append(raw)
            continue
        upper = line.upper()
        if not line or line.startswith("--"):
            continue
        if upper.startswith(("DECLARE", "BEGIN")):
            block = [raw]
            continue
        if line.startswith("@@"):
            run_file(os.path.join(here, unquote(line[2:])), depth + 1)
        elif line.startswith("@"):
            target = unquote(line[1:])
            record(event="script", path=target, session=state["session"])
            run_file(target if os.path.isabs(target) else os.path.join(state["cwd"], target), depth + 1)
        elif upper.startswith("CD "):
            state["cwd"] = unquote(line[3:])
        elif upper == "SHOW DDL":
            emit("setting               value")
            emit("--------------------  -----")
            emit("collation_clause      NEVER")
            emit("storage               ON")
        elif upper.startswith("PROMPT"):
            emit(line[6:].strip())
        elif upper.startswith("STORE SET"):
            target = unquote(line[len("STORE SET"):].rsplit(None, 1)[0])
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("SET __BASELINE__\n")
        elif upper.startswith("SET "):
            parts = line[4:].split(None, 1)
            if parts and parts[0] == "__BASELINE__":
                state["sets"].clear()
            elif parts:
                state["sets"][parts[0].upper()] = parts[1] if len(parts) > 1 else ""
        elif upper.startswith("DEFINE "):
            name, _, value = line[7:].partition("=")
            state["defines"][name.strip().upper()] = value.strip()
        elif upper.startswith("UNDEFINE "):
            for name in line[9:].split():
                state["defines"].pop(name.upper(), None)
        elif upper.startswith("WHENEVER SQLERROR"):
            state["whenever"] = " EXIT" in upper
        elif upper.startswith("CONNECT"):
            state["connects"] += 1
            state["session"] += 1
            state.update(txn=False, schema=None, pkg=False)
            record(event="connect", session=state["session"])
        elif upper.startswith("ALTER SESSION SET CURRENT_SCHEMA"):
            state["schema"] = line.split("=", 1)[1].strip(" ;")
        elif upper in ("COMMIT;", "ROLLBACK;"):
            state["txn"] = False
        elif upper.startswith("EXIT"):
            record(event="exit", code=0)
            sys.exit(0)
        elif upper == "FAKE_SQL_ERROR;":
            error("ORA-00942: table or view does not exist")
        elif upper.startswith("FAKE_SLEEP"):
            time.sleep(float(line.split()[1].rstrip(";")))
        elif upper == "FAKE_OPEN_TXN;":
            state["txn"] = True
        elif upper == "FAKE_SET_PKG;":
            state["pkg"] = True
        elif upper == "FAKE_EXIT_OS;":
            sys.exit(3)
        elif upper == "FAKE_SHOW_STATE;":
            emit("STATE " + json.dumps({key: state[key] for key in ("session", "txn", "schema", "pkg", "sets")}
                                       | {"defines": sorted(state["defines"]), "pid": os.getpid()}))


args = sys.argv[1:]
if "-name" in args:
    state["alias"] = args[args.index("-name") + 1]
script = next((arg[1:] for arg in args if arg.startswith("@")), None)
record(event="start", args=args, sqlpath=os.environ.get("SQLPATH"), cwd=os.getcwd())
if script is not None:
    run_file(script, 1)
record(event="end")
sys.exit(0)
'''


def install(directory: Path) -> Path:
    """Write the protocol fake as `sql` into directory and return the directory."""
    import fake_sqlcl

    return fake_sqlcl.install(directory, SCRIPT.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))


def environment(directory: Path, **values: str) -> dict[str, str]:
    import fake_sqlcl

    return fake_sqlcl.environment(directory, SQLCL_PERSISTENT_SESSIONS="1", **values)


def events(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]

