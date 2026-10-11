import unittest

from scripts.apexlang_normalization import normalize_apexlang


class ApexlangNormalizationTests(unittest.TestCase):
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
