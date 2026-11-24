import unittest

from scripts.sqlcl_transport import generated_clob_assembly, lint_sqlcl_transport


class SqlclTransportTests(unittest.TestCase):
    def test_direct_quoted_trailing_hyphen_is_a_proven_transport_error(self):
        findings = lint_sqlcl_transport("BEGIN\n  v_text := 'value-\nnext';\nEND;\n/\n", "direct")

        self.assertEqual([(item.code, item.line, item.severity) for item in findings], [
            ("quoted-trailing-hyphen", 2, "error"),
        ])

    def test_assembled_source_downgrades_the_same_case_to_a_warning(self):
        findings = lint_sqlcl_transport("v_text := 'value-\nnext';\n", "assembled")

        self.assertEqual([item.severity for item in findings], ["warning"])

    def test_comment_and_literal_hyphens_are_not_code_findings(self):
        sql = "-- comment-\nselect 'ordinary-value' from dual; -- trailing-\n"

        self.assertEqual(lint_sqlcl_transport(sql, "direct"), ())

    def test_code_trailing_hyphen_and_quoted_blank_line_are_warnings(self):
        code = lint_sqlcl_transport("select 1 -\n  2 from dual;\n", "direct")
        blank = lint_sqlcl_transport("v_text := 'first\n   \nlast';\n", "direct")

        self.assertIn(("code-trailing-hyphen", "warning"), [(item.code, item.severity) for item in code])
        self.assertIn(("quoted-blank-line", "warning"), [(item.code, item.severity) for item in blank])

    def test_generated_clob_assembly_requires_the_recognized_wrapper(self):
        self.assertFalse(generated_clob_assembly("BEGIN NULL; END;\n/\n"))
        self.assertTrue(generated_clob_assembly(
            "-- Exact stored source for APP.P assembled as a CLOB.\n"
            "DECLARE\n  l_source CLOB := TO_CLOB('x');\nBEGIN\n"
            "  EXECUTE IMMEDIATE l_source;\n  DBMS_LOB.FREETEMPORARY(l_source);\nEND;\n/\n"
        ))

    def test_invalid_mode_refuses_and_malformed_sql_stays_with_primary_validator(self):
        with self.assertRaises(ValueError):
            lint_sqlcl_transport("select 1 from dual", "guess")

        self.assertEqual(lint_sqlcl_transport("v_text := 'unterminated", "direct"), ())


if __name__ == "__main__":
    unittest.main()
