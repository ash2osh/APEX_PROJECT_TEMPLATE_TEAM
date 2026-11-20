from __future__ import annotations

import io
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import database_support


class ParseMajorTests(unittest.TestCase):
    def test_reads_the_integer_before_the_first_dot(self):
        cases = {
            "23.26": 23,
            "19.0": 19,
            "23.26.3.0.0": 23,
            "19.28.0.0.0": 19,
            "23": 23,
            " 19.0 ": 19,
        }
        for version, major in cases.items():
            with self.subTest(version=version):
                self.assertEqual(database_support.parse_major(version), major)

    def test_unknown_or_unparsable_input_returns_none(self):
        for version in (None, "", "   ", "abc", ".5", "19 .0", 19, 23.26, b"19.0", ["19.0"]):
            with self.subTest(version=version):
                self.assertIsNone(database_support.parse_major(version))


class SupportWarningTests(unittest.TestCase):
    def test_older_releases_return_a_single_line_warning(self):
        for version in ("19.0", "12.2"):
            with self.subTest(version=version):
                text = database_support.support_warning(version)
                self.assertIsNotNone(text)
                self.assertTrue(text.startswith("warning:"))
                self.assertIn(version, text)
                self.assertIn("23ai", text)
                self.assertNotIn("\n", text)

    def test_supported_releases_return_none(self):
        for version in ("23.26", "23.6", "24.1", "26.1"):
            with self.subTest(version=version):
                self.assertIsNone(database_support.support_warning(version))

    def test_unknown_releases_return_none(self):
        for version in (None, "", "abc", 19):
            with self.subTest(version=version):
                self.assertIsNone(database_support.support_warning(version))


class WarnOnceTests(unittest.TestCase):
    def setUp(self):
        database_support._warned_versions.clear()
        self.addCleanup(database_support._warned_versions.clear)

    def test_prints_once_per_distinct_version(self):
        stream = io.StringIO()
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            self.assertTrue(database_support.warn_once("19.0", stream))
            self.assertFalse(database_support.warn_once("19.0", stream))
            self.assertTrue(database_support.warn_once("12.2", stream))
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("19.0", lines[0])
        self.assertIn("12.2", lines[1])
        self.assertEqual(stdout.getvalue(), "")

    def test_defaults_to_stderr(self):
        stderr = io.StringIO()
        with patch("sys.stderr", stderr):
            self.assertTrue(database_support.warn_once("19.0.0.0"))
        self.assertIn("19.0.0.0", stderr.getvalue())

    def test_supported_versions_print_nothing(self):
        stream = io.StringIO()
        self.assertFalse(database_support.warn_once("23.26.3.0.0", stream))
        self.assertFalse(database_support.warn_once(None, stream))
        self.assertEqual(stream.getvalue(), "")

    def test_warning_contains_no_paths_or_quoted_names(self):
        stream = io.StringIO()
        database_support.warn_once("19.0", stream)
        text = stream.getvalue()
        for marker in ("/", "\\", '"', "'"):
            self.assertNotIn(marker, text)



class DoctorSqlWarningTests(unittest.TestCase):
    def test_the_doctor_database_query_warns_below_23_with_the_same_wording(self) -> None:
        sql = (Path(__file__).resolve().parents[1] / "scripts" / "doctor_database_info.sql").read_text(encoding="utf-8")
        self.assertIn("< 23 THEN", sql)
        text = database_support.support_warning("19.0")
        assert text is not None
        # The SQL builds the same message around the version, so the fixed parts must appear in both.
        self.assertTrue(text.startswith("warning: Oracle Database 19.0 is older than 23ai"))
        self.assertIn("is older than 23ai, the supported floor. ", sql)
        self.assertIn("The template tools are tested on 23ai and newer; this run continues on a best-effort basis.", sql)
        self.assertIn("warning: Oracle Database ", sql)


if __name__ == "__main__":
    unittest.main()
