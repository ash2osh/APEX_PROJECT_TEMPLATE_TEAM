import unittest
import tempfile
from pathlib import Path

from scripts.apexlang_diff import compare_source_files
from scripts.apexlang_normalization import NormalizationError, normalize_apexlang


class ApexlangNormalizationTests(unittest.TestCase):
    def test_fenced_plsql_trailing_space_is_significant(self):
        source = b"application (\n    code: ```plsql\nvalue := 'first   \nsecond';\n```\n)\n"
        exported = b"application (\n    code: ```plsql\nvalue := 'first\nsecond';\n```\n)\n"

        self.assertNotEqual(normalize_apexlang(source), normalize_apexlang(exported))

    def test_javascript_template_comment_is_literal(self):
        source = (
            b"application (\n    code: ```javascript\n"
            b"const message = `first\n// original literal data\nlast`;\n```\n)\n"
        )
        exported = (
            b"application (\n    code: ```javascript\n"
            b"const message = `first\n// changed literal data\nlast`;\n```\n)\n"
        )

        self.assertNotEqual(normalize_apexlang(source), normalize_apexlang(exported))

    def test_unicode_line_separators_are_preserved(self):
        source = 'application (\n    title: "before\u2028middle\u2029after"\n)\n'.encode("utf-8")

        self.assertEqual(normalize_apexlang(source), source)
        self.assertEqual(normalize_apexlang(source.replace(b"\n", b"\r\n")), source)

    def test_unquoted_scalar_trailing_spaces_are_content(self):
        source = b"application (\n    message: keep   \n)\n"
        exported = b"application (\n    message: keep\n)\n"

        self.assertNotEqual(normalize_apexlang(source), normalize_apexlang(exported))

    def test_unquoted_array_entries_starting_with_comment_marker_are_content(self):
        source = b"application (\n    values: [\n        // source value\n    ]\n)\n"
        exported = b"application (\n    values: [\n        // changed value\n    ]\n)\n"

        self.assertNotEqual(normalize_apexlang(source), normalize_apexlang(exported))

    def test_unquoted_array_entry_trailing_spaces_are_content(self):
        source = b"application (\n    values: [\n        source value   \n    ]\n)\n"
        exported = b"application (\n    values: [\n        source value\n    ]\n)\n"

        self.assertNotEqual(normalize_apexlang(source), normalize_apexlang(exported))

    def test_escaped_triple_fence_does_not_close_literal(self):
        source = (
            b"application (\n    code: ```text\n"
            b"first \\``` embedded   \nlast\r\n```\n)\n"
        )
        exported = (
            b"application (\n    code: ```text\n"
            b"first \\``` embedded\nlast\n```\n)\n"
        )

        self.assertNotEqual(normalize_apexlang(source), normalize_apexlang(exported))

    def test_component_like_text_inside_literal_is_not_sorted(self):
        first = (
            b"application (\n    code: ```javascript\n"
            b"const messages = `\ntextMessage Z (\n language: en\n)\n"
            b"textMessage A (\n language: en\n)`;\n```\n)\n"
        )
        reordered = (
            b"application (\n    code: ```javascript\n"
            b"const messages = `\ntextMessage A (\n language: en\n)\n"
            b"textMessage Z (\n language: en\n)`;\n```\n)\n"
        )

        self.assertNotEqual(normalize_apexlang(first), normalize_apexlang(reordered))

    def test_quoted_parenthesis_does_not_break_component_reordering(self):
        source = (
            b'report R (\n    columns {\n'
            b'        column ZED (\n            name: ZED\n            heading: "text )"\n        )\n'
            b'        column ALPHA (\n            name: ALPHA\n            heading: "Alpha"\n        )\n'
            b'    }\n)\n'
        )
        exported = (
            b'report R (\n    columns {\n'
            b'        column ALPHA (\n            name: ALPHA\n            heading: "Alpha"\n        )\n'
            b'        column ZED (\n            name: ZED\n            heading: "text )"\n        )\n'
            b'    }\n)\n'
        )

        self.assertEqual(normalize_apexlang(source), normalize_apexlang(exported))

    def test_ambiguous_literal_gets_no_formatting_exemption(self):
        with self.assertRaises(ValueError):
            normalize_apexlang(b"application (\n    code: ```plsql\nunterminated\n")

    def test_ambiguous_literal_is_raw_mismatch_not_cosmetic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            exported = root / "exported"
            for directory in (source, exported):
                (directory / ".apex").mkdir(parents=True)
                (directory / "application.apx").write_bytes(
                    b"application (\n    code: ```plsql\nvalue := 'unterminated   \n"
                    if directory == source
                    else b"application (\n    code: ```plsql\nvalue := 'unterminated\n"
                )
                (directory / ".apex" / "apexlang.json").write_bytes(b'{"version":1}\n')

            try:
                mismatches, cosmetic = compare_source_files(source, exported, normalize_apx=True)
            except NormalizationError as error:
                self.fail(f"ambiguous source escaped comparison instead of failing closed: {error}")

            self.assertEqual(cosmetic, ())
            self.assertEqual([(item.path, item.kind) for item in mismatches], [("application.apx", "bytes")])

    def test_comments_messages_report_columns_and_trailing_space_are_canonicalized(self):
        first = b'''// serializer note
report R (
    columns {
        column ZED (
            name: ZED
            heading: `Zed`
        )
        column ALPHA (
            name: ALPHA
            heading: `Alpha`
        )
    }
    savedReport R_DEFAULT (
        displayColumns {
            displayColumn ZED (
                column: ZED
            )
            displayColumn ALPHA (
                column: ALPHA
            )
        }
    )
)
textMessage ZETA (
    message {
        text: `Zulu`
        language: en
    }
)
textMessage ALPHA (
    message {
        text: `Alpha`
        language: fr
    }
)
textMessage ALPHA (
    message {
        text: `Alpha English`
        language: en
    }
)

'''
        first = first.replace(b"column ZED (\n", b"column ZED (   \n", 1)
        second = b'''report R (
    columns {
        column ALPHA (
            name: ALPHA
            heading: `Alpha`
        )
        column ZED (
            name: ZED
            heading: `Zed`
        )
    }
    savedReport R_DEFAULT (
        displayColumns {
            displayColumn ALPHA (
                column: ALPHA
            )
            displayColumn ZED (
                column: ZED
            )
        }
    )
)
textMessage ALPHA (
    message {
        text: `Alpha English`
        language: en
    }
)
textMessage ALPHA (
    message {
        text: `Alpha`
        language: fr
    }
)
textMessage ZETA (
    message {
        text: `Zulu`
        language: en
    }
)
'''

        self.assertEqual(normalize_apexlang(first), normalize_apexlang(second))
        self.assertFalse(normalize_apexlang(first).endswith(b"\n\n"))

    def test_parentheses_inside_backtick_text_do_not_end_a_component_block(self):
        source = b'''textMessage SAMPLE (
    message {
        text: `keep (these) characters`
        language: en
    }
)
'''

        self.assertEqual(normalize_apexlang(source), source)


if __name__ == "__main__":
    unittest.main()
