from __future__ import annotations

import unittest

from scripts.dev_test_bindings import (
    DevTestBindingError,
    RenderContext,
    render_object_tokens,
    validate_bindings,
)


class DevTestBindingsValidationTests(unittest.TestCase):
    def test_valid_integer_in_signed_json_safe_range(self) -> None:
        safe_integers = (
            0,
            1,
            -1,
            42,
            -42,
            9007199254740991,   # 2**53 - 1
            -9007199254740991,  # -(2**53 - 1)
        )
        for value in safe_integers:
            with self.subTest(value=value):
                result = validate_bindings({"DEVTEST_INT": value})
                self.assertEqual(result["DEVTEST_INT"], value)
                self.assertIs(type(result["DEVTEST_INT"]), int)

    def test_valid_string_up_to_4000_utf8_bytes(self) -> None:
        ascii_4000 = "x" * 4000
        arabic_text = "تجربة قيمة نصية بالعربية مع رمز ' و سطر جديد\n"
        result = validate_bindings({
            "DEVTEST_ASCII": ascii_4000,
            "DEVTEST_ARABIC": arabic_text,
        })
        self.assertEqual(result["DEVTEST_ASCII"], ascii_4000)
        self.assertEqual(result["DEVTEST_ARABIC"], arabic_text)

    def test_refuse_bool_values(self) -> None:
        for bad in (True, False):
            with self.subTest(bad=bad), self.assertRaises(DevTestBindingError):
                validate_bindings({"DEVTEST_FLAG": bad})

    def test_refuse_float_and_null_values(self) -> None:
        for bad in (3.14, 0.0, None, [1], {"a": 1}):
            with self.subTest(bad=bad), self.assertRaises(DevTestBindingError):
                validate_bindings({"DEVTEST_BAD": bad})  # type: ignore[arg-type]

    def test_refuse_integer_out_of_json_safe_range(self) -> None:
        for out_of_bounds in (
            9007199254740992,
            -9007199254740992,
            2**63 - 1,
            -(2**63),
        ):
            with self.subTest(val=out_of_bounds), self.assertRaises(DevTestBindingError):
                validate_bindings({"DEVTEST_OVERFLOW": out_of_bounds})

    def test_refuse_string_over_4000_utf8_bytes(self) -> None:
        too_long = "x" * 4001
        with self.assertRaises(DevTestBindingError):
            validate_bindings({"DEVTEST_TOOLONG": too_long})

        # 1001 4-byte characters = 4004 bytes
        too_long_unicode = "\U0001F600" * 1001
        with self.assertRaises(DevTestBindingError):
            validate_bindings({"DEVTEST_TOO_LONG_UNICODE": too_long_unicode})

    def test_refuse_arbitrary_namespaces_and_lowercase(self) -> None:
        invalid_names = (
            "USER_ID",
            "APP_SCHEMA",
            "target_schema",
            "devtest_run_id",
            "DEVTEST_run_id",
            "1",
            ":1",
            "DEVTEST-RUN-ID",
            "",
            "DEVTEST_",
        )
        for name in invalid_names:
            with self.subTest(name=name), self.assertRaises(DevTestBindingError):
                validate_bindings({name: 1})

    def test_accept_and_normalize_leading_colon(self) -> None:
        result = validate_bindings({":DEVTEST_RUN_ID": "run-123"})
        self.assertEqual(result, {"DEVTEST_RUN_ID": "run-123"})


class RenderContextTests(unittest.TestCase):
    def test_valid_context_is_frozen(self) -> None:
        ctx = RenderContext(
            owner="DEMO",
            run_id="20261016T120000Z-0123456789abcdef0123456789abcdef",
            objects={"items": "DT_0123456789AB_ITEMS"},
            values={"DEVTEST_RUN_ID": "20261016T120000Z-0123456789abcdef0123456789abcdef"},
        )
        self.assertEqual(ctx.owner, "DEMO")
        self.assertEqual(ctx.objects["items"], "DT_0123456789AB_ITEMS")
        self.assertEqual(ctx.values["DEVTEST_RUN_ID"], ctx.run_id)

        with self.assertRaises((TypeError, AttributeError)):
            ctx.owner = "OTHER"  # type: ignore[misc]

    def test_owner_must_be_uppercase_valid_oracle_identifier(self) -> None:
        for bad_owner in ("demo", "1DEMO", "DEMO-SCHEMA", "DEMO SCHEMA", ""):
            with self.subTest(owner=bad_owner), self.assertRaises(DevTestBindingError):
                RenderContext(
                    owner=bad_owner,
                    run_id="run-1",
                    objects={},
                    values={},
                )

    def test_owner_and_object_identifiers_must_respect_budget(self) -> None:
        # Owner exceeds 128 bytes
        with self.assertRaises(DevTestBindingError):
            RenderContext(
                owner="A" * 129,
                run_id="run-1",
                objects={},
                values={},
            )

        # Generated object identifier exceeds 30 bytes budget
        with self.assertRaises(DevTestBindingError):
            RenderContext(
                owner="DEMO",
                run_id="run-1",
                objects={"items": "DT_0123456789AB_TOOLONGNAMEHERE"},
                values={},
            )

    def test_reserved_run_id_cannot_be_overridden_with_conflicting_value(self) -> None:
        with self.assertRaises(DevTestBindingError):
            RenderContext(
                owner="DEMO",
                run_id="run-expected",
                objects={},
                values={"DEVTEST_RUN_ID": "run-conflicting"},
            )

    def test_reserved_run_id_automatically_populated_if_omitted(self) -> None:
        ctx = RenderContext(
            owner="DEMO",
            run_id="run-expected",
            objects={},
            values={"DEVTEST_COUNT": 5},
        )
        self.assertEqual(ctx.values["DEVTEST_RUN_ID"], "run-expected")
        self.assertEqual(ctx.values["DEVTEST_COUNT"], 5)


class RenderObjectTokensTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = RenderContext(
            owner="DEMO",
            run_id="20261016T120000Z-0123456789abcdef0123456789abcdef",
            objects={
                "items": "DT_0123456789AB_ITEMS",
                "orders": "DT_0123456789AB_ORDERS",
            },
            values={},
        )

    def test_render_single_and_multiple_object_tokens(self) -> None:
        sql = "SELECT id FROM ${object.items} i JOIN ${object.orders} o ON i.order_id = o.id"
        rendered = render_object_tokens(sql, self.context)
        expected = 'SELECT id FROM "DEMO"."DT_0123456789AB_ITEMS" i JOIN "DEMO"."DT_0123456789AB_ORDERS" o ON i.order_id = o.id'
        self.assertEqual(rendered, expected)

    def test_render_preserves_original_sql_line_endings(self) -> None:
        crlf_sql = "SELECT *\r\nFROM ${object.items}\r\nWHERE id = 1\r\n"
        rendered = render_object_tokens(crlf_sql, self.context)
        self.assertIn("\r\n", rendered)
        self.assertEqual(
            rendered,
            'SELECT *\r\nFROM "DEMO"."DT_0123456789AB_ITEMS"\r\nWHERE id = 1\r\n',
        )

    def test_reject_unresolved_object_token(self) -> None:
        sql = "SELECT * FROM ${object.unknown_table}"
        with self.assertRaises(DevTestBindingError):
            render_object_tokens(sql, self.context)

    def test_reject_arbitrary_text_expansion(self) -> None:
        for bad in (
            "SELECT * FROM ${table.items}",
            "SELECT * FROM ${items}",
            "SELECT * FROM ${}",
            "SELECT * FROM ${object.}",
            "SELECT * FROM ${object.ITEMS}",
            "SELECT * FROM ${object.items.extra}",
        ):
            with self.subTest(bad=bad), self.assertRaises(DevTestBindingError):
                render_object_tokens(bad, self.context)

    def test_reject_token_in_single_and_national_quoted_strings(self) -> None:
        cases = (
            "SELECT '${object.items}' FROM dual",
            "SELECT N'${object.items}' FROM dual",
            "SELECT 'prefix ' || '${object.items}' FROM dual",
        )
        for sql in cases:
            with self.subTest(sql=sql), self.assertRaises(DevTestBindingError):
                render_object_tokens(sql, self.context)

    def test_reject_token_in_q_quoted_strings(self) -> None:
        delimiters = (
            "q'[SELECT ${object.items} FROM dual]'",
            "q'(SELECT ${object.items} FROM dual)'",
            "q'<SELECT ${object.items} FROM dual>'",
            "q'{SELECT ${object.items} FROM dual}'",
            "q'#SELECT ${object.items} FROM dual#'",
            "Q'!${object.items}!'",
            "nq'[${object.items}]'",
        )
        for sql in delimiters:
            with self.subTest(sql=sql), self.assertRaises(DevTestBindingError):
                render_object_tokens(sql, self.context)

    def test_reject_token_in_double_quoted_identifier(self) -> None:
        sql = 'SELECT "${object.items}" FROM dual'
        with self.assertRaises(DevTestBindingError):
            render_object_tokens(sql, self.context)

    def test_reject_token_in_line_and_block_comments(self) -> None:
        cases = (
            "SELECT 1 FROM dual -- ${object.items}",
            "SELECT 1 FROM dual /* ${object.items} */",
            "SELECT 1 /* multi\nline\n${object.items}\ncomment */ FROM dual",
        )
        for sql in cases:
            with self.subTest(sql=sql), self.assertRaises(DevTestBindingError):
                render_object_tokens(sql, self.context)

    def test_reject_sqlcl_ampersand_substitution(self) -> None:
        cases = (
            "SELECT &SUBST FROM dual",
            "SELECT &&SUBST FROM dual",
            "SELECT &1 FROM dual",
            "SELECT &var FROM ${object.items}",
        )
        for sql in cases:
            with self.subTest(sql=sql), self.assertRaises(DevTestBindingError):
                render_object_tokens(sql, self.context)

    def test_fake_tokens_hidden_inside_escaped_delimiters(self) -> None:
        # Doubled single-quotes inside string literals keeping token inside literal
        cases = (
            "SELECT 'escaped''quote ${object.items} still in string' FROM dual",
            "SELECT q'[nested [brackets] ${object.items} inside]'",
        )
        for sql in cases:
            with self.subTest(sql=sql), self.assertRaises(DevTestBindingError):
                render_object_tokens(sql, self.context)

    def test_escaped_quotes_outside_token_expand_correctly(self) -> None:
        sql = "SELECT 'it''s fine' AS note, id FROM ${object.items}"
        rendered = render_object_tokens(sql, self.context)
        self.assertEqual(
            rendered,
            'SELECT \'it\'\'s fine\' AS note, id FROM "DEMO"."DT_0123456789AB_ITEMS"',
        )


if __name__ == "__main__":
    unittest.main()
