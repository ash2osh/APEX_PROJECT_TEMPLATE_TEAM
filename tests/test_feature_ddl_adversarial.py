from __future__ import annotations

import random
import unittest

from scripts import feature_ddl_forms as forms

SCHEMA = "APP_DATA"

BASE_STATEMENTS = [
    ('COMMENT ON TABLE "APP_DATA"."T" IS \'valid comment\'', "OPAQUE"),
    ('COMMENT ON COLUMN "APP_DATA"."T"."COL" IS \'col comment\'', "OPAQUE"),
    ('CREATE SYNONYM "APP_DATA"."S" FOR "APP_DATA"."T"', "OPAQUE"),
    ('CREATE PUBLIC SYNONYM "S" FOR "APP_DATA"."T"', "OPAQUE"),
    ('ALTER TABLE "APP_DATA"."T" ADD CONSTRAINT "PK_T" PRIMARY KEY ("ID")', "UNKNOWN"),
    ('ALTER TABLE "APP_DATA"."T" ADD CONSTRAINT "FK_T" FOREIGN KEY ("P_ID") REFERENCES "APP_DATA"."P" ("ID") ON DELETE CASCADE', "UNKNOWN"),
    ('ALTER TABLE "APP_DATA"."T" ADD CONSTRAINT "CK_T" CHECK ("ID" > 0 AND "VAL" IN (\'A\', \'B\'))', "UNKNOWN"),
    ('ALTER TABLE "APP_DATA"."T" ADD UNIQUE ("A", "B")', "UNKNOWN"),
    ('ALTER TABLE "APP_DATA"."T" MODIFY ("NAME" VARCHAR2(100 BYTE) NOT NULL, "CNT" NUMBER(10, 2))', "REVIEWED_ALTER"),
    ('ALTER TABLE "APP_DATA"."T" MODIFY "NAME" CLOB', "REVIEWED_ALTER"),
    ('CREATE VIEW "APP_DATA"."V" AS WITH CTE AS (SELECT "ID" FROM "APP_DATA"."T1") SELECT "ID" FROM CTE', "UNKNOWN"),
    ('CREATE INDEX "APP_DATA"."IX_T" ON "APP_DATA"."T" (UPPER("NAME"), "CNT")', "UNKNOWN"),
    ('CREATE UNIQUE INDEX "APP_DATA"."IX_U" ON "APP_DATA"."T" (CASE WHEN "A" = 1 THEN "B" END)', "UNKNOWN"),
    ('GRANT EXECUTE ON "APP_DATA"."PKG" TO "RUNERP_USER"', "REVIEWED_OPERATION"),
    ('GRANT EXECUTE ON "PKG" TO "RUNERP_USER"', "REVIEWED_OPERATION"),
    ('GRANT SELECT ON "APP_DATA"."MY_SEQ" TO "RUNERP_USER", "REPORTER" WITH GRANT OPTION', "REVIEWED_OPERATION"),
]


# Strict prefixes of a base statement that are themselves valid statements (a shorter name, an
# omitted optional clause, SELECT without FROM in 23ai). Any other prefix being modelled is a bug.
VALID_PREFIX_TAILS = {
    'ALTER TABLE "APP_DATA"."T" ADD CONSTRAINT "FK_T" FOREIGN KEY ("P_ID") REFERENCES "APP_DATA"."P" ("ID") ON DELETE CASCADE': (
        '."P" ("ID") ON DELETE CASCADE',
        ' ("ID") ON DELETE CASCADE',
        '("ID") ON DELETE CASCADE',
        ' ON DELETE CASCADE',
        'ON DELETE CASCADE',
    ),
    'CREATE PUBLIC SYNONYM "S" FOR "APP_DATA"."T"': (
        '."T"',
    ),
    'CREATE SYNONYM "APP_DATA"."S" FOR "APP_DATA"."T"': (
        '."T"',
    ),
    'CREATE VIEW "APP_DATA"."V" AS WITH CTE AS (SELECT "ID" FROM "APP_DATA"."T1") SELECT "ID" FROM CTE': (
        ' FROM CTE',
        'FROM CTE',
        'ROM CTE',
        'OM CTE',
        'M CTE',
        'TE',
        'E',
    ),
    'GRANT SELECT ON "APP_DATA"."MY_SEQ" TO "RUNERP_USER", "REPORTER" WITH GRANT OPTION': (
        ', "REPORTER" WITH GRANT OPTION',
        ' WITH GRANT OPTION',
        'WITH GRANT OPTION',
    ),
}


def assert_stays_in_scope(test: unittest.TestCase, text: str, result: dict) -> None:
    """Whatever the input, a modelled statement never names another schema or hides a control character."""
    test.assertTrue(result.get("supported"), text)
    test.assertIn(result["owner"], {SCHEMA, "PUBLIC"}, text)
    for field in ("target_owner",):
        if field in result:
            test.assertEqual(SCHEMA, result[field], text)
    test.assertFalse(any(ord(c) < 32 and c not in "\n\r\t" for c in text), f"control character modelled: {text!r}")


class AdversarialDdlFormsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = random.Random(42)

    def _try_model(self, text: str, kind: str) -> dict | None:
        try:
            return forms.model_statement(text, {"kind": kind, "file": "f.sql", "statement_index": 1}, SCHEMA)
        except forms.FeatureDdlError:
            return None
        except forms.MigrationAnalysisError:
            return None

    def test_all_base_statements_are_valid(self) -> None:
        for stmt, kind in BASE_STATEMENTS:
            with self.subTest(stmt=stmt):
                result = self._try_model(stmt, kind)
                self.assertIsNotNone(result, f"Failed to model base statement: {stmt}")
                self.assertTrue(result.get("supported"))

    def test_truncation_is_never_modelled_unless_the_prefix_is_itself_valid_sql(self) -> None:
        for stmt, kind in BASE_STATEMENTS:
            allowed = set(VALID_PREFIX_TAILS.get(stmt, ()))
            for length in range(1, len(stmt)):
                if self._try_model(stmt[:length], kind) is not None:
                    with self.subTest(stmt=stmt[:50], kept=stmt[:length][-40:]):
                        self.assertIn(stmt[length:], allowed, f"partial statement modelled: {stmt[:length]!r}")

    def test_trailing_junk_is_never_modelled(self) -> None:
        junk_suffixes = [
            " DROP TABLE T",
            "; DROP TABLE T",
            " extra1 extra2",
            " 123",
            " /",
            " -- comment with extra code\nDROP TABLE T",
            " /* comment */ DROP TABLE T",
        ]
        for stmt, kind in BASE_STATEMENTS:
            for junk in junk_suffixes:
                tampered = stmt + junk
                with self.subTest(tampered=tampered):
                    result = self._try_model(tampered, kind)
                    self.assertIsNone(result, f"Statement with trailing junk was modelled: {tampered}")

    def test_comment_and_quote_injection_mutation(self) -> None:
        injections = [
            "/* */",
            "/* comment */",
            "*/",
            "/*",
            "--\n",
            "-- injection\n",
            "'",
            "''",
            "q'[injection]'",
            "q'(injection)'",
            '"',
            '""',
            "\x00",
            "\x01",
            "مورد",
            "A" * 150,
        ]
        for stmt, kind in BASE_STATEMENTS:
            words = stmt.split()
            for _ in range(15):
                pos = self.rng.randint(0, len(words))
                inj = self.rng.choice(injections)
                mutated_words = list(words)
                mutated_words.insert(pos, inj)
                mutated = " ".join(mutated_words)
                result = self._try_model(mutated, kind)
                if result is not None:
                    assert_stays_in_scope(self, mutated, result)

    def test_seeded_random_character_mutations(self) -> None:
        # 2000 randomized mutated queries
        for _ in range(2000):
            stmt, kind = self.rng.choice(BASE_STATEMENTS)
            mutation_type = self.rng.choice(["delete", "insert", "replace", "splice"])
            chars = list(stmt)
            if not chars:
                continue
            if mutation_type == "delete" and len(chars) > 5:
                idx = self.rng.randint(0, len(chars) - 1)
                del chars[idx]
            elif mutation_type == "insert":
                idx = self.rng.randint(0, len(chars))
                chars.insert(idx, self.rng.choice(" ()',;/*-\\\"\n\t123_qQ[]@~`!#$\x00\x07\x1bمورد"))
            elif mutation_type == "replace":
                idx = self.rng.randint(0, len(chars) - 1)
                chars[idx] = self.rng.choice(" ()',;/*-\\\"\n\t123_qQ[]@~`!#$\x00\x07\x1bمورد")
            elif mutation_type == "splice":
                idx = self.rng.randint(0, len(chars))
                chars[idx:idx] = list(self.rng.choice([
                    "/* injection */", "q'[test]'", "--\n", "12345", "مورد",
                    "A" * 150, "*/", "/*", "''", '""', "; DROP TABLE T",
                ]))
            mutated = "".join(chars)
            # Must never raise unhandled exception
            result = self._try_model(mutated, kind)
            if result is not None:
                assert_stays_in_scope(self, mutated, result)


if __name__ == "__main__":
    unittest.main()
