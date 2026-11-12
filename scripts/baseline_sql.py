"""SQL quoting and check helpers shared by the baseline build modules, moved out of scripts/baseline_build.py.

These are leaves: scripts/baseline_reference_data.py and scripts/baseline_bundles.py import them directly, and
scripts/baseline_build.py re-exports them. The one name that stays in scripts/baseline.py, BaselineError, is imported
inside the function that raises it: baseline imports the build modules at its own end, so a module-level import here
would fail whenever one of the build modules is imported before baseline.
"""

from __future__ import annotations

import hashlib
import re

from .migration_manifest import validate_check_query


def _sql_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or any(character in value for character in "\r\n\x00"):
        from . import baseline

        raise baseline.BaselineError("catalog returned an unsafe empty identifier")
    return '"' + value.replace('"', '""') + '"'


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_Q_DELIMITERS = (
    ("~", "~"), ("!", "!"), ("^", "^"), ("#", "#"), ("$", "$"), ("%", "%"),
    ("&", "&"), ("*", "*"), ("-", "-"), ("+", "+"), ("=", "="), ("?", "?"),
    (":", ":"), (";", ";"), ("@", "@"), ("_", "_"), (",", ","), (".", "."),
    ("/", "/"), ("[", "]"), ("{", "}"), ("(", ")"), ("<", ">"),
)


def _q_literal(value: str) -> str:
    for opening, closing in _Q_DELIMITERS:
        if closing + "'" not in value:
            return f"q'{opening}{value}{closing}'"
    return _sql_literal(value)


def _check(check_id: str, sql: str) -> dict:
    validate_check_query(sql)
    return {"id": check_id, "sql": sql, "expected": 1}


def _safe_check_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "object"
    return slug[:39].rstrip("-") + "-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
