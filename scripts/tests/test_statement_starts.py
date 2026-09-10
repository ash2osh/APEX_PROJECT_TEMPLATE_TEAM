from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import unittest

from teamlib.sql_text import mask_sql, statement_starts


def starts(text: str) -> list[tuple[int, str]]:
    masked, terminated = mask_sql(text)
    assert terminated, "fixture must be lexically complete"
    return list(statement_starts(masked))


class StatementStartTests(unittest.TestCase):
    def test_second_statement_on_one_line_is_offered(self):
        self.assertEqual(
            starts("SELECT 1 FROM dual; DROP TABLE audit_log;\n"),
            [(1, "SELECT 1 FROM dual"), (1, "DROP TABLE audit_log")],
        )

    def test_client_command_is_terminated_by_end_of_line(self):
        # SET has no ";". Every following line must still be offered.
        self.assertEqual(
            starts("SET DEFINE OFF\nSET HEADING OFF\nSELECT 1 FROM dual;\n"),
            [(1, "SET DEFINE OFF"), (2, "SET HEADING OFF"), (3, "SELECT 1 FROM dual")],
        )

    def test_continuation_line_of_a_query_is_not_a_statement_start(self):
        self.assertEqual(
            starts("SELECT a\nFROM t\nWHERE b = 1;\n"),
            [(1, "SELECT a")],
        )

    def test_plsql_block_is_one_statement_ending_at_a_lone_slash(self):
        text = (
            "BEGIN\n"
            "  DBMS_OUTPUT.PUT_LINE('x');\n"
            "  HOST rm -rf /;\n"
            "END;\n"
            "/\n"
            "SELECT 1 FROM dual;\n"
        )
        # Nothing inside the block is a statement start; the block itself is.
        self.assertEqual(starts(text), [(1, "BEGIN"), (6, "SELECT 1 FROM dual")])

    def test_semicolon_inside_a_literal_does_not_split(self):
        # The literal is masked to spaces of the same length, so the piece
        # keeps that internal whitespace; only the statement count matters
        # here -- the fake ";" inside the literal must not start a second one.
        result = starts("SELECT 'a; DROP TABLE t' FROM dual;\n")
        self.assertEqual(len(result), 1)
        number, text = result[0]
        self.assertEqual(number, 1)
        self.assertEqual(text.split(), ["SELECT", "FROM", "dual"])

    def test_slash_terminates_a_statement_outside_a_block(self):
        self.assertEqual(
            starts("SELECT 1 FROM dual\n/\nSELECT 2 FROM dual;\n"),
            [(1, "SELECT 1 FROM dual"), (3, "SELECT 2 FROM dual")],
        )

    def test_create_table_is_not_a_plsql_block(self):
        self.assertEqual(
            starts("CREATE TABLE t (a NUMBER);\nSELECT 1 FROM dual;\n"),
            [(1, "CREATE TABLE t (a NUMBER)"), (2, "SELECT 1 FROM dual")],
        )


if __name__ == "__main__":
    unittest.main()
