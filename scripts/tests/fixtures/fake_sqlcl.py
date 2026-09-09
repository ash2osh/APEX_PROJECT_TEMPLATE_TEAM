#!/usr/bin/env python3
"""Deterministic SQLcl boundary fixture used by test_sqlcl.py."""

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys


def main() -> int:
    args = sys.argv[1:]
    wrapper_arg = next((arg for arg in args if arg.startswith("@")), "")
    wrapper = Path(wrapper_arg[1:]) if wrapper_arg else None
    text = wrapper.read_text(encoding="utf-8") if wrapper else ""
    log_path = os.environ.get("FAKE_SQLCL_LOG")
    if log_path:
        stdin_mode = os.fstat(sys.stdin.fileno()).st_mode
        Path(log_path).write_text(
            "argv=" + repr(args) + "\n"
            + "cwd=" + os.getcwd() + "\n"
            + "stdin_regular=" + str(stat.S_ISREG(stdin_mode)) + "\n"
            + "identity_count=" + str(text.count("TEAM_IDENTITY")) + "\n",
            encoding="utf-8",
        )

    if os.environ.get("FAKE_STARTUP_EXCEPTION") == "1":
        print("startup exception", file=sys.stderr)
        return 0
    if os.environ.get("FAKE_ERROR_ZERO") == "1":
        print("ORA-20099: simulated failure")
        return 0

    identity = os.environ.get("FAKE_IDENTITY", "DEMO|DEMO|FREEPDB1|freep1|FREE")
    second_identity = os.environ.get("FAKE_SECOND_IDENTITY", "")
    count = text.count("TEAM_IDENTITY")
    for index in range(count):
        current = second_identity if index == 1 and second_identity else identity
        session_user, current_schema, db_name, service, instance_id = current.split("|", 4)
        line = (
            "TEAM_IDENTITY|SESSION_USER=" + session_user
            + "|CURRENT_SCHEMA=" + current_schema
            + "|DB_NAME=" + db_name
            + "|SERVICE=" + service
            + "|INSTANCE_ID=" + instance_id
        )
        if os.environ.get("FAKE_CRLF") == "1":
            sys.stdout.write(line + "\r\n")
        else:
            print(line)
    if os.environ.get("FAKE_EXTRA_OUTPUT"):
        print(os.environ["FAKE_EXTRA_OUTPUT"])
    if os.environ.get("FAKE_NO_COMPLETION") != "1":
        completion = "read"
        for line in text.splitlines():
            if line.startswith("TEAM_COMPLETION|") and "operation=" in line:
                completion = line.split("operation=", 1)[1].strip()
                break
        print("TEAM_COMPLETION|operation=" + completion)
    return int(os.environ.get("FAKE_EXIT_CODE", "0"))


if __name__ == "__main__":
    raise SystemExit(main())
