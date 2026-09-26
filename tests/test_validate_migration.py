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

    def test_migration_cannot_take_transaction_completion_away_from_driver(self) -> None:
        for source in ("COMMIT;\n", "ROLLBACK;\n"):
            with self.subTest(source=source):
                result = self.run_validator(source)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("SQL-only migration", result.stderr)


if __name__ == "__main__":
    unittest.main()
