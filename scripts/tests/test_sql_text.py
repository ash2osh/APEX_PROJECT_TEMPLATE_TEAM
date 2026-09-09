from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import re
import unittest

from teamlib.sql_text import SqlTextError, clob_builder, mask_sql, sql_literal


MUTATION = re.compile(r"\b(?:INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE)\b", re.IGNORECASE)


class MaskSqlTests(unittest.TestCase):
    def test_q_quote_with_embedded_apostrophe_is_terminated_and_fully_masked(self):
        for text in (
            "UPDATE t SET m = q'[don't touch]' WHERE id=1;",
            "UPDATE t SET m = q'{it's a test}' WHERE id=1;",
            "UPDATE t SET m = q'(it's fine)' WHERE id=1;",
            "UPDATE t SET m = q'<it's fine>' WHERE id=1;",
            "UPDATE t SET m = q'!it's fine!' WHERE id=1;",
            "UPDATE t SET m = nq'[naive's]' WHERE id=1;",
        ):
            masked, terminated = mask_sql(text)
            self.assertTrue(terminated, text)
            self.assertEqual(len(masked), len(text), text)
            self.assertNotIn("touch", masked)
            self.assertNotIn("test", masked)

    def test_keywords_inside_a_q_quote_do_not_leak_into_the_mask(self):
        masked, terminated = mask_sql("SELECT q'[don't drop this table]' FROM dual;")
        self.assertTrue(terminated)
        self.assertEqual(MUTATION.findall(masked), [])

    def test_even_numbers_of_embedded_apostrophes_are_masked_too(self):
        # The dangerous parity: the old lexer did not raise here, it silently
        # left the interior unmasked.
        masked, terminated = mask_sql("SELECT q'[it's Bob's DROP TABLE]' FROM dual;")
        self.assertTrue(terminated)
        self.assertEqual(MUTATION.findall(masked), [])

    def test_plain_literals_comments_and_identifiers_still_mask(self):
        masked, terminated = mask_sql(
            "SELECT 'a ''drop'' b', \"My Drop Col\" FROM t; -- drop tail\n/* drop block */\n"
        )
        self.assertTrue(terminated)
        self.assertEqual(MUTATION.findall(masked), [])

    def test_newlines_and_length_are_preserved(self):
        text = "SELECT 1\n  FROM dual;\n-- trailing\n"
        masked, _ = mask_sql(text)
        self.assertEqual(len(masked), len(text))
        self.assertEqual(masked.count("\n"), text.count("\n"))

    def test_unterminated_constructs_are_reported_not_raised(self):
        for text in ("SELECT 'x FROM dual;", "SELECT q'[x FROM dual;", "SELECT 1 /* x"):
            masked, terminated = mask_sql(text)
            self.assertFalse(terminated, text)
            self.assertEqual(len(masked), len(text))

    def test_q_preceded_by_an_identifier_character_is_not_a_q_quote(self):
        # myq'x' is the identifier myq followed by an ordinary literal.
        masked, terminated = mask_sql("SELECT myq'x' FROM dual;")
        self.assertTrue(terminated)
        self.assertIn("myq", masked)


class ClobBuilderTests(unittest.TestCase):
    def test_every_emitted_line_is_bounded(self):
        payload = "x" * 60000
        built = clob_builder("v_manifest", payload)
        self.assertTrue(built.splitlines())
        self.assertLess(max(len(line) for line in built.splitlines()), 1200)

    def test_builder_creates_then_appends_the_whole_value(self):
        built = clob_builder("v_x", "abc")
        self.assertIn("DBMS_LOB.CREATETEMPORARY(v_x, TRUE);", built)
        self.assertIn("DBMS_LOB.APPEND(v_x, TO_CLOB('abc'));", built)

    def test_empty_value_creates_an_empty_temporary(self):
        built = clob_builder("v_x", "")
        self.assertIn("CREATETEMPORARY", built)
        self.assertNotIn("APPEND", built)

    def test_quotes_are_doubled_and_nul_is_refused(self):
        self.assertIn("TO_CLOB('it''s')", clob_builder("v_x", "it's"))
        with self.assertRaises(SqlTextError):
            clob_builder("v_x", "bad\x00value")


class SqlLiteralTests(unittest.TestCase):
    def test_quotes_are_doubled_and_nul_is_refused(self):
        self.assertEqual(sql_literal("it's"), "'it''s'")
        with self.assertRaises(SqlTextError):
            sql_literal("bad\x00value")


if __name__ == "__main__":
    unittest.main()
