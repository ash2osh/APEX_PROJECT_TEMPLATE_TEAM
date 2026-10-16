#!/usr/bin/env python3
"""Tests for the shared backup manifest validator."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.backup_manifest import BackupManifestError, parse_manifest, verify_scope


class BackupManifestParserTests(unittest.TestCase):
    def test_valid_tables_manifest(self) -> None:
        self.assertEqual(parse_manifest("TABLE=1\n", "tables"), {"TABLE": 1})
        self.assertEqual(parse_manifest("TABLE=0\n", "tables"), {"TABLE": 0})
        self.assertEqual(parse_manifest("TABLE=42", "tables"), {"TABLE": 42})
        self.assertEqual(parse_manifest("TABLE=007\n", "tables"), {"TABLE": 7})
        self.assertEqual(
            parse_manifest("TABLE=9223372036854775807\n", "tables"),
            {"TABLE": 9223372036854775807},
        )

    def test_valid_code_manifest(self) -> None:
        source = (
            "FUNCTION=1\n"
            "PACKAGE=2\n"
            "PACKAGE BODY=3\n"
            "PROCEDURE=4\n"
            "SYNONYM=5\n"
            "TRIGGER=0\n"
            "VIEW=6\n"
        )
        expected = {
            "FUNCTION": 1,
            "PACKAGE": 2,
            "PACKAGE BODY": 3,
            "PROCEDURE": 4,
            "SYNONYM": 5,
            "TRIGGER": 0,
            "VIEW": 6,
        }
        self.assertEqual(parse_manifest(source, "code"), expected)

    def test_whitespace_and_crlf_handling(self) -> None:
        source = (
            "\r\n"
            "  FUNCTION = 0  \r\n"
            "\tPACKAGE\t=\t1\r\n"
            "PACKAGE BODY=0\r\n"
            "PROCEDURE=0\r\n"
            "SYNONYM=0\r\n"
            "TRIGGER=0\r\n"
            "VIEW=0"
        )
        parsed = parse_manifest(source, "code")
        self.assertEqual(parsed["FUNCTION"], 0)
        self.assertEqual(parsed["PACKAGE"], 1)

    def test_rejects_empty_or_whitespace_only(self) -> None:
        for source in ("", "   \n\n\t\r\n  "):
            with self.subTest(source=repr(source)), self.assertRaises(BackupManifestError) as cm:
                parse_manifest(source, "tables")
            self.assertIn("no readable object counts", str(cm.exception))

    def test_all_ascii_whitespace_is_allowed_at_field_boundaries(self) -> None:
        for whitespace in (" ", "\t", "\r", "\v", "\f", " \t\r\v\f"):
            with self.subTest(whitespace=repr(whitespace)):
                source = f"{whitespace}\r\n{whitespace}TABLE{whitespace}={whitespace}00{whitespace}\r\n"
                self.assertEqual(parse_manifest(source, "tables"), {"TABLE": 0})

    def test_non_ascii_whitespace_cannot_hide_invalid_rows(self) -> None:
        for whitespace in ("\u0085", "\u00a0", "\u2028", "\u2029"):
            for source in (f"TABLE=0{whitespace}\n", f"{whitespace}TABLE=0\n"):
                with self.subTest(source=repr(source)), self.assertRaises(BackupManifestError):
                    parse_manifest(source, "tables")

    def test_rejects_bom(self) -> None:
        with self.assertRaises(BackupManifestError) as cm:
            parse_manifest("\ufeffTABLE=1\n", "tables")
        self.assertIn("byte-order mark", str(cm.exception))

    def test_rejects_missing_required_type(self) -> None:
        with self.assertRaises(BackupManifestError) as cm:
            parse_manifest("FUNCTION=0\nVIEW=1\n", "code")
        self.assertIn("missing", str(cm.exception))

        with self.assertRaises(BackupManifestError) as cm:
            parse_manifest(
                "FUNCTION=0\nPACKAGE=0\nPACKAGE BODY=0\nPROCEDURE=0\nSYNONYM=0\nVIEW=1\n",
                "code",
            )
        self.assertIn("missing the TRIGGER row", str(cm.exception))

    def test_rejects_unknown_and_cross_scope_types(self) -> None:
        # Cross-scope type
        with self.assertRaises(BackupManifestError) as cm:
            parse_manifest("TABLE=1\nVIEW=0\n", "tables")
        self.assertIn("cross-scope", str(cm.exception))

        # Unknown type
        with self.assertRaises(BackupManifestError) as cm:
            parse_manifest("TABLE=1\nUNKNOWN=0\n", "tables")
        self.assertIn("unknown", str(cm.exception))

        with self.assertRaises(BackupManifestError) as cm:
            source = (
                "FUNCTION=0\nPACKAGE=0\nPACKAGE BODY=0\nPROCEDURE=0\n"
                "SYNONYM=0\nTRIGGER=0\nVIEW=1\nTABLE=0\n"
            )
            parse_manifest(source, "code")
        self.assertIn("cross-scope", str(cm.exception))

    def test_rejects_duplicate_types(self) -> None:
        with self.assertRaises(BackupManifestError) as cm:
            parse_manifest("TABLE=1\nTABLE=0\n", "tables")
        self.assertIn("duplicate", str(cm.exception))

    def test_rejects_malformed_separators(self) -> None:
        for line in ("TABLE 1", "TABLE", "TABLE=1=2", "TABLE==1"):
            with self.subTest(line=line), self.assertRaises(BackupManifestError) as cm:
                parse_manifest(line + "\n", "tables")
            self.assertIn("separator", str(cm.exception))

    def test_rejects_invalid_count_formats(self) -> None:
        invalid_counts = (
            "", "oops", "1a", "-1", "+1", "1.5", "1e5", "１", "0x10"
        )
        for count in invalid_counts:
            with self.subTest(count=count), self.assertRaises(BackupManifestError) as cm:
                parse_manifest(f"TABLE={count}\n", "tables")
            self.assertIn("invalid count", str(cm.exception).lower())

    def test_rejects_overflow(self) -> None:
        # Individual overflow: 2^63
        with self.assertRaises(BackupManifestError) as cm:
            parse_manifest("TABLE=9223372036854775808\n", "tables")
        self.assertIn("overflow", str(cm.exception).lower())

        # Aggregate overflow
        source = (
            "FUNCTION=9223372036854775800\n"
            "PACKAGE=10\n"
            "PACKAGE BODY=0\n"
            "PROCEDURE=0\n"
            "SYNONYM=0\n"
            "TRIGGER=0\n"
            "VIEW=0\n"
        )
        with self.assertRaises(BackupManifestError) as cm:
            parse_manifest(source, "code")
        self.assertIn("overflow", str(cm.exception).lower())


class BackupManifestVerificationTests(unittest.TestCase):
    def test_verify_scope_tables_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest-tables.txt"
            manifest.write_text("TABLE=2\n", encoding="utf-8")
            tables_dir = root / "tables"
            tables_dir.mkdir()
            (tables_dir / "T1.sql").write_text("CREATE TABLE T1;\n", encoding="utf-8")
            (tables_dir / "T2.sql").write_text("CREATE TABLE T2;\n", encoding="utf-8")
            verify_scope(manifest, root, "tables", schema="DEMO")

    def test_verify_scope_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "manifest-tables.txt"
            manifest.write_text("TABLE=2\n", encoding="utf-8")
            tables_dir = root / "tables"
            tables_dir.mkdir()
            (tables_dir / "T1.sql").write_text("CREATE TABLE T1;\n", encoding="utf-8")
            with self.assertRaises(BackupManifestError) as cm:
                verify_scope(manifest, root, "tables", schema="DEMO")
            self.assertIn("incomplete for DEMO (tables): manifest expects 2 object file(s) but 1 were written", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
