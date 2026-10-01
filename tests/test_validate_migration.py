import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_migration.py"


class ValidateMigrationTests(unittest.TestCase):
    def run_validator(self, source: str) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "migration.sql"
            path.write_text(source, encoding="utf-8")
            return subprocess.run(
                ["python3", str(VALIDATOR), str(path)],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_sql_literals_and_comments_may_contain_client_command_text(self) -> None:
        result = self.run_validator(
            "-- PROMPT is only a comment here\n"
            "CREATE TABLE NOTES (TEXT_VALUE VARCHAR2(100) DEFAULT q'[SET DEFINE ON; PROMPT text]');\n"
            "INSERT INTO NOTES (TEXT_VALUE) VALUES ('x; y & z');\n"
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_anonymous_and_stored_plsql_blocks_are_accepted(self) -> None:
        for source in (
            "BEGIN\n  NULL;\nEND;\n/\n",
            "CREATE OR REPLACE PROCEDURE P_TEST AS\nBEGIN\n  NULL;\nEND;\n/\n",
            "CREATE OR REPLACE LIBRARY LIB_TEST AS '/tmp/libtest.so'\n/\n",
            'CREATE /* stored source */ JAVA SOURCE NAMED "Welcome" AS\n'
            'public class Welcome { String quote = "escaped \\\" quote"; }\n/\n',
            'CREATE JAVA SOURCE NAMED "TextBlock" AS\n'
            'public class TextBlock { String text = """\n;\n"""; }\n/\n',
            'CREATE AND RESOLVE JAVA SOURCE NAMED "Resolving" AS\n'
            'public class Resolving {}\n/\n',
            'CREATE OR REPLACE AND COMPILE NOFORCE JAVA SOURCE NAMED "Compiling" AS\n'
            'public class Compiling {}\n/\n',
            'CREATE JAVA IF NOT EXISTS SOURCE NAMED "Optional" AS\n'
            'public class Optional {}\n/\n',
            "CREATE OR REPLACE MLE MODULE M_TEST LANGUAGE JAVASCRIPT AS\n"
            "export function add(a, b) { return a + b; }\n/\n",
            "CREATE MLE MODULE IF NOT EXISTS M_TEST LANGUAGE JAVASCRIPT AS\n"
            "export const text = `a; b`;\n/\n",
        ):
            with self.subTest(source=source):
                result = self.run_validator(source)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sqlcl_directive_after_a_statement_is_rejected(self) -> None:
        for source in (
            "INSERT INTO T VALUES (1);\nSET DEFINE ON\n",
            "@@UPDATE.sql\nSELECT 1 FROM dual;\n",
            "@UPDATE.sql\n",
        ):
            with self.subTest(source=source):
                result = self.run_validator(source)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SQL-only migration", result.stderr)

    def test_non_nested_block_comment_cannot_hide_a_client_directive(self) -> None:
        result = self.run_validator(
            "/* outer /* inner */\n"
            "PROMPT CLIENT_DIRECTIVE_EXECUTED\n"
            "/* close */ -- */\n"
            "SELECT 1 FROM dual;\n"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SQL-only migration", result.stderr)

    def test_plsql_buffer_terminator_cannot_expose_a_client_directive(self) -> None:
        result = self.run_validator(
            "BEGIN\n"
            "NULL;\n"
            "END;\n"
            ".\n"
            "PROMPT CLIENT_DIRECTIVE_EXECUTED\n"
            "/\n"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SQL-only migration", result.stderr)

    def test_java_buffer_terminator_cannot_expose_a_client_directive(self) -> None:
        for source in (
            'CREATE JAVA SOURCE NAMED "Test" AS\n'
            "public class Test {}\n"
            ";\n"
            "PROMPT CLIENT_DIRECTIVE_EXECUTED\n"
            "/\n",
            'CREATE AND RESOLVE JAVA SOURCE NAMED "Test" AS\n'
            "public class Test {}\n"
            ";\n"
            "PROMPT CLIENT_DIRECTIVE_EXECUTED\n"
            "/\n",
            'CREATE JAVA IF NOT EXISTS SOURCE NAMED "Test" AS\n'
            "public class Test {}\n"
            ";\n"
            "PROMPT CLIENT_DIRECTIVE_EXECUTED\n"
            "/\n",
        ):
            with self.subTest(source=source):
                result = self.run_validator(source)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SQL-only migration", result.stderr)

    def test_java_comment_buffer_terminator_cannot_expose_a_client_directive(self) -> None:
        result = self.run_validator(
            'CREATE JAVA SOURCE NAMED "Test" AS\n'
            "public class Test {\n"
            "/*\n"
            ";\n"
            "*/\n"
            "}\n"
            "PROMPT CLIENT_DIRECTIVE_EXECUTED\n"
            "/\n"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SQL-only migration", result.stderr)

    def test_mle_module_period_buffer_terminator_is_rejected(self) -> None:
        # SQLcl ends an MLE module buffer at a lone period, so the next line
        # would run as a client command (verified with SQLcl under /nolog).
        result = self.run_validator(
            "CREATE OR REPLACE MLE MODULE M_TEST LANGUAGE JAVASCRIPT AS\n"
            "export function f() { return 1; }\n"
            ".\n"
            "PROMPT CLIENT_DIRECTIVE_EXECUTED\n"
            "/\n"
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("buffer terminator", result.stderr)

    def test_lone_slash_or_period_line_inside_a_sql_statement_is_rejected(self) -> None:
        # SQLcl ends a plain SQL statement at a line holding only '/' or '.', so
        # the next line would run as a client command although the validator
        # sees one SELECT. Verified with SQLcl against a database.
        for terminator in ("/", ".", "  /  ", " . ", "/ -- run", ". -- end"):
            for source in (
                f"SELECT 1 FROM dual\n{terminator}\nHOST echo CLIENT_DIRECTIVE_EXECUTED\n;\n",
                f"INSERT INTO T VALUES (1)\n{terminator}\n;\n",
                f"UPDATE T SET A = 1\n{terminator}\nWHERE B = 2;\n",
            ):
                with self.subTest(source=source):
                    result = self.run_validator(source)

                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("SQL-only migration", result.stderr)
                    self.assertIn("ends the statement", result.stderr)

    def test_at_sign_line_inside_a_statement_is_rejected(self) -> None:
        # SQLcl splices the text of the named file into the statement, so the
        # payload would run bytes that were never reviewed or hashed.
        for source in (
            "SELECT 1\n@other.sql\nFROM dual;\n",
            "SELECT 1\n  @@other.sql\nFROM dual;\n",
            "BEGIN\n  NULL;\n@other.sql\nEND;\n/\n",
            "CREATE OR REPLACE PROCEDURE P AS\nBEGIN\n  NULL;\n@other.sql\nEND;\n/\n",
        ):
            with self.subTest(source=source):
                result = self.run_validator(source)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SQL-only migration", result.stderr)
                self.assertIn("'@'", result.stderr)

    def test_lone_slash_line_inside_a_block_comment_is_rejected(self) -> None:
        # SQLcl ends the statement or PL/SQL block at the '/' even inside a
        # comment, then runs what follows the comment's first lines.
        for source in (
            "SELECT 1 /*\n/\n*/ FROM dual;\n",
            "BEGIN\n  NULL; /*\n/\nHOST echo CLIENT_DIRECTIVE_EXECUTED\n*/\nEND;\n/\n",
            "/*\n  /  \n*/\nSELECT 1 FROM dual;\n",
            # a '.' line before the '/' line must not hide it
            "SELECT 1 /* a\n.\n/\nHOST echo CLIENT_DIRECTIVE_EXECUTED\n b */ FROM dual;\n",
        ):
            with self.subTest(source=source):
                result = self.run_validator(source)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SQL-only migration", result.stderr)
                self.assertIn("comment", result.stderr)

    def test_the_comment_opener_slash_star_slash_is_rejected(self) -> None:
        # '/*/' is a valid Oracle comment opener, but SQLcl's parser throws on it
        # and then runs the comment's own lines as commands (HOST ran in a live
        # test). The validator saw only a comment.
        for source in (
            "SELECT 1 FROM dual;\n/*/\nHOST echo CLIENT_DIRECTIVE_EXECUTED\n*/\nSELECT 2 FROM dual;\n",
            "/*/ x */\nSELECT 1 FROM dual;\n",
            "SELECT 1 FROM dual;\n/*/*/\nSELECT 2 FROM dual;\n",
            "SELECT 1 /*/ y */ FROM dual;\n",
        ):
            with self.subTest(source=source):
                result = self.run_validator(source)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SQL-only migration", result.stderr)
                self.assertIn("/*/", result.stderr)

    def test_comment_forms_that_only_resemble_the_slash_star_slash_opener_are_accepted(self) -> None:
        for source in (
            "SELECT 1 FROM dual; /**/\nSELECT 2 FROM dual;\n",
            "SELECT 1 FROM dual; /***/\nSELECT 2 FROM dual;\n",
            "SELECT 1 FROM dual; /* / */\nSELECT 2 FROM dual;\n",
            "SELECT '/*/' FROM dual;\n",
            "-- /*/ in a line comment\nSELECT 1 FROM dual;\n",
        ):
            with self.subTest(source=source):
                result = self.run_validator(source)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_quoted_value_between_statements_is_rejected(self) -> None:
        # The validator blanked it, so it never saw it; SQLcl reads it as a
        # command ("Unknown Command") and the script still completes.
        for source in (
            "SELECT 1 FROM dual;\n'/'\nSELECT 2 FROM dual;\n",
            'INSERT INTO T VALUES (1);\n"x"\nUPDATE T SET A = 2;\n',
            "q'[stray]'\nSELECT 1 FROM dual;\n",
            "SELECT 1 FROM dual; 'trailing'\nSELECT 2 FROM dual;\n",
            # after the last statement, and a file that is one quoted region
            "SELECT 1 FROM dual;\n'-- stray\n--'\n",
            "BEGIN\n  NULL;\nEND;\n/\n' \n'--\n",
            "'/*END; \nFROM dual;\nBEGIN NULL; END;\n/\nq'\n",
        ):
            with self.subTest(source=source):
                result = self.run_validator(source)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SQL-only migration", result.stderr)
                self.assertIn("quoted value", result.stderr)

    def test_quoted_values_inside_statements_and_comments_between_them_are_accepted(self) -> None:
        for source in (
            "SELECT 'a' FROM dual;\nSELECT 'b' FROM dual;\n",
            "SELECT 1 FROM dual;\n-- 'note'\n/* \"x\" */\nSELECT 2 FROM dual;\n",
            "INSERT INTO T VALUES ('a;b');\nUPDATE T SET A = q'[x;y]';\n",
        ):
            with self.subTest(source=source):
                result = self.run_validator(source)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_statements_first_word_must_be_followed_by_whitespace(self) -> None:
        # SQLcl decides what a statement is from its first whitespace-delimited
        # word. 'DECLARE,~' is an unknown command, so SQLcl ran the lines after
        # it one by one (HOST ran in a live test) while the validator read the
        # keyword and treated everything up to the final '/' as one block.
        for source in (
            "DECLARE,~\nHOST echo CLIENT_DIRECTIVE_EXECUTED\nBEGIN\n  NULL;\nEND;\n/\n",
            "BEGIN,~\nHOST echo CLIENT_DIRECTIVE_EXECUTED\n  NULL;\nEND;\n/\n",
            "DECLARE(\nHOST echo CLIENT_DIRECTIVE_EXECUTED\nBEGIN\n  NULL;\nEND;\n/\n",
            "DECLARE/**/\nHOST echo CLIENT_DIRECTIVE_EXECUTED\nBEGIN\n  NULL;\nEND;\n/\n",
            "SELECT,~\nHOST echo CLIENT_DIRECTIVE_EXECUTED\nFROM dual;\n",
            "SELECT(1) AS x FROM dual;\n",
            "SELECT 1 FROM dual;\nCREATE,~\nTABLE x (a NUMBER);\n",
            "SELECT 1 FROM dual;SELECT,~ 2 FROM dual;\n",
        ):
            with self.subTest(source=source):
                result = self.run_validator(source)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SQL-only migration", result.stderr)
                self.assertIn("first word", result.stderr)

    def test_a_first_word_followed_by_whitespace_is_accepted(self) -> None:
        for source in (
            "DECLARE\n  x NUMBER;\nBEGIN\n  x := 1;\nEND;\n/\n",
            "BEGIN\n  NULL;\nEND;\n/\n",
            "BEGIN NULL; END;\n/\n",
            "SELECT 1 FROM dual;\nSELECT 2 FROM dual;\n",
            "SELECT 1 FROM dual;SELECT 2 FROM dual;\n",
            "INSERT INTO t (a) VALUES (1);\n",
            "WITH q AS (SELECT 1 AS v FROM dual) SELECT v FROM q;\n",
            "SELECT\t1 FROM dual;\n",
            "CREATE OR REPLACE VIEW v AS SELECT 1 AS a FROM dual;\n",
        ):
            with self.subTest(source=source):
                result = self.run_validator(source)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_quoted_identifier_spanning_lines_is_rejected(self) -> None:
        # SQLcl ends the statement at a '/' line inside the identifier, then
        # runs what follows as client commands.
        for source in (
            'SELECT 1 AS "a\n/\nHOST echo CLIENT_DIRECTIVE_EXECUTED\nb" FROM dual;\n',
            'SELECT 1 AS "a\nb" FROM dual;\n',
        ):
            with self.subTest(source=source):
                result = self.run_validator(source)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("quoted identifier", result.stderr)

    def test_statement_text_that_only_resembles_a_terminator_is_accepted(self) -> None:
        # SQLcl keeps these as part of the statement: a blank line (the apply
        # driver sets SQLBLANKLINES ON), a '/' or '.' line inside a string, a
        # q-quoted literal or a comment, and an '@' that does not start a line.
        for source in (
            "UPDATE T\nSET A = 1\n\nWHERE B = 2;\n",
            "SELECT 'a\n/\nb' FROM dual;\n",
            "SELECT 'a\n.\nb' FROM dual;\n",
            "SELECT q'[a\n/\nb]' FROM dual;\n",
            "SELECT 1 /* a\n.\n b */ FROM dual;\n",
            "SELECT 1 FROM dual@remote_link;\n",
            "SELECT 'two\nlines' AS \"ONE_LINE\" FROM dual;\n",
            "BEGIN\n  NULL;\n\n  NULL;\nEND;\n/\n",
            "SELECT 1\n  / 2 FROM dual;\n",
        ):
            with self.subTest(source=source):
                result = self.run_validator(source)

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_migration_cannot_take_transaction_completion_away_from_driver(self) -> None:
        for source in ("COMMIT;\n", "ROLLBACK;\n"):
            with self.subTest(source=source):
                result = self.run_validator(source)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SQL-only migration", result.stderr)


if __name__ == "__main__":
    unittest.main()
