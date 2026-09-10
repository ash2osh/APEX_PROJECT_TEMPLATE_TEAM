from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
import tempfile
import unittest

from teamlib.migration_bundle import BundleError, dependency_order, load_bundles


HEADER = """-- migration-version: 1
-- target: tables
-- destructive: false

"""
DOWN_HEADER = """-- migration-version: 1
-- destructive: true

"""


class MigrationBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-migration-bundle-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(
        self,
        migration_id: str,
        sql: str = "CREATE TABLE T_X (ID NUMBER);\n",
        verify: str = "",
        down_sql: str | None = None,
        down_verify: str | None = None,
    ) -> None:
        (self.root / f"{migration_id}.sql").write_text(HEADER + sql, encoding="utf-8", newline="\n")
        (self.root / f"{migration_id}.verify.sql").write_text(verify, encoding="utf-8", newline="\n")
        if down_sql is not None:
            (self.root / f"{migration_id}.down.sql").write_text(DOWN_HEADER + down_sql, encoding="utf-8", newline="\n")
        if down_verify is not None:
            (self.root / f"{migration_id}.down.verify.sql").write_text(down_verify, encoding="utf-8", newline="\n")

    def test_loads_complete_bundle_and_checksum_changes_with_member(self):
        migration_id = "20260907T100000__alice__create-table"
        self.write(migration_id, verify="SELECT 'table' assertion_name, 'PASS' status FROM dual;\n")
        bundles = load_bundles(self.root)
        self.assertEqual(bundles[migration_id].target, "tables")
        checksum = bundles[migration_id].checksum
        (self.root / f"{migration_id}.verify.sql").write_text("SELECT 'other' assertion_name, 'PASS' status FROM dual;\n", encoding="utf-8")
        self.assertNotEqual(load_bundles(self.root)[migration_id].checksum, checksum)

    def test_loads_complete_down_pair_and_checksums_both_members(self):
        migration_id = "20260907T100000__alice__reversible"
        self.write(
            migration_id,
            verify="SELECT 'table' assertion_name, 'PASS' status FROM dual;\n",
            down_sql="DROP TABLE T_X;\n",
            down_verify="SELECT 'table' assertion_name, 'PASS' status FROM dual;\n",
        )
        migration = load_bundles(self.root)[migration_id]
        self.assertTrue(migration.reversible)
        self.assertFalse(migration.destructive)
        self.assertTrue(migration.down_destructive)
        checksum = migration.checksum
        (self.root / f"{migration_id}.down.sql").write_text(DOWN_HEADER + "DROP TABLE T_Y;\n", encoding="utf-8")
        self.assertNotEqual(load_bundles(self.root)[migration_id].checksum, checksum)
        checksum = load_bundles(self.root)[migration_id].checksum
        (self.root / f"{migration_id}.down.verify.sql").write_text("SELECT 'other' assertion_name, 'PASS' status FROM dual;\n", encoding="utf-8")
        self.assertNotEqual(load_bundles(self.root)[migration_id].checksum, checksum)

    def test_down_pair_is_atomic_and_direction_header_is_closed(self):
        migration_id = "20260907T100000__alice__down-shape"
        self.write(migration_id, down_sql="DROP TABLE T_X;\n")
        with self.assertRaises(BundleError):
            load_bundles(self.root)
        (self.root / f"{migration_id}.down.verify.sql").write_text("", encoding="utf-8")
        down = self.root / f"{migration_id}.down.sql"
        down.unlink()
        down.symlink_to("/etc/passwd")
        with self.assertRaises(BundleError):
            load_bundles(self.root)
        down.unlink()
        down.write_bytes(DOWN_HEADER.replace("\n", "\r\n").encode())
        with self.assertRaises(BundleError):
            load_bundles(self.root)

    def test_down_members_reject_controls_writes_and_forbidden_directives(self):
        migration_id = "20260907T100000__alice__down-invalid"
        cases = (
            ("CONNECT other\nDROP TABLE T_X;\n", ""),
            ("DROP TABLE T_X;\n", "INSERT INTO T_X VALUES (1);\n"),
            ("-- target: tables\nDROP TABLE T_X;\n", ""),
            ("-- depends-on: 20260907T090000__alice__base sha256:" + "a" * 64 + "\nDROP TABLE T_X;\n", ""),
            ("-- migration-version: 1\n-- migration-version: 1\n-- destructive: true\nDROP TABLE T_X;\n", ""),
            ("-- migration-version: 1\n-- destructive: true\n-- destructive: false\nDROP TABLE T_X;\n", ""),
        )
        for sql, verify in cases:
            with self.subTest(sql=sql, verify=verify):
                self.write(migration_id, down_sql=sql, down_verify=verify)
                if not (self.root / f"{migration_id}.down.verify.sql").exists():
                    (self.root / f"{migration_id}.down.verify.sql").write_text("", encoding="utf-8")
                with self.assertRaises(BundleError):
                    load_bundles(self.root)
                for path in self.root.iterdir():
                    path.unlink()

    def test_down_invalid_utf8_is_rejected(self):
        migration_id = "20260907T100000__alice__down-utf8"
        self.write(migration_id, down_sql="DROP TABLE T_X;\n", down_verify="")
        (self.root / f"{migration_id}.down.sql").write_bytes(b"\xff")
        with self.assertRaises(BundleError):
            load_bundles(self.root)

    def test_ignores_unrelated_docs_but_rejects_orphan_and_extra_member(self):
        (self.root / "README.md").write_text("notes", encoding="utf-8")
        with self.assertRaises(BundleError):
            (self.root / "20260907T100000__alice__x.verify.sql").write_text("", encoding="utf-8")
            load_bundles(self.root)
        (self.root / "20260907T100000__alice__x.verify.sql").unlink()
        migration_id = "20260907T100000__alice__x"
        self.write(migration_id)
        (self.root / f"{migration_id}.notes").write_text("unexpected", encoding="utf-8")
        with self.assertRaises(BundleError):
            load_bundles(self.root)

    def test_invalid_calendar_duplicate_directive_unknown_directive_and_late_header_fail(self):
        bad = "20261307T100000__alice__bad"
        self.write(bad)
        (self.root / f"{bad}.sql").write_text(HEADER.replace("2026", "2026") + "-- foo: bar\nCREATE TABLE X(ID NUMBER);\n", encoding="utf-8")
        # Unknown header directive is rejected before any database operation.
        with self.assertRaises(BundleError):
            load_bundles(self.root)

    def test_control_commands_and_nested_includes_are_rejected(self):
        migration_id = "20260907T100000__alice__unsafe"
        self.write(migration_id, sql="CONNECT other\nCREATE TABLE T_X(ID NUMBER);\n")
        with self.assertRaises(BundleError):
            load_bundles(self.root)

    def test_dependency_order_uses_full_id_tie_break(self):
        first = "20260907T100000__alice__a"
        second = "20260907T100000__bob__b"
        self.write(first)
        self.write(second)
        bundles = load_bundles(self.root)
        self.assertEqual(dependency_order(bundles), tuple(sorted((first, second))))

    def test_missing_and_cyclic_dependencies_fail(self):
        first = "20260907T100000__alice__a"
        self.write(first)
        path = self.root / f"{first}.sql"
        path.write_text(HEADER.replace("\n\n", f"-- depends-on: 20260907T100000__bob__missing sha256:{'a'*64}\n\n") + "CREATE TABLE T_X(ID NUMBER);\n", encoding="utf-8")
        with self.assertRaises(BundleError):
            load_bundles(self.root)

    def test_q_quoted_literals_load_without_a_false_unterminated_error(self):
        from teamlib.migration_bundle import _mask_code
        for text in (
            "UPDATE t SET msg = q'[don't touch]' WHERE id = 1;",
            "UPDATE t SET msg = q'{it's a test}' WHERE id = 1;",
        ):
            masked = _mask_code(text)
            self.assertEqual(len(masked), len(text))
            self.assertNotIn("touch", masked)

    def test_genuinely_unterminated_literals_still_fail(self):
        from teamlib.migration_bundle import _mask_code, BundleError
        with self.assertRaisesRegex(BundleError, "unterminated"):
            _mask_code("UPDATE t SET msg = 'never closed;")

    def test_valid_oracle_constructs_are_not_mistaken_for_client_commands(self):
        from teamlib.migration_bundle import _assert_controls
        for sql in (
            "SELECT id FROM org\nSTART WITH id = 1\nCONNECT BY PRIOR id = parent_id;\n",
            "BEGIN\n  FOR r IN (SELECT 1 x FROM dual) LOOP\n    EXIT WHEN r.x > 5;\n  END LOOP;\nEND;\n/\n",
            "CREATE TABLE nodes (host VARCHAR2(255));\n",
            "CREATE TABLE t (\n  host VARCHAR2(512) NOT NULL,\n  id NUMBER\n);\n",
            "CREATE OR REPLACE PROCEDURE p IS\nBEGIN\n  EXIT;\nEND;\n/\n",
        ):
            _assert_controls(sql)

    def test_the_template_own_control_metadata_ddl_is_a_valid_migration_member(self):
        from teamlib.migration_bundle import _assert_controls
        root = Path(__file__).resolve().parents[2]
        _assert_controls((root / "scripts/sql/control_metadata.sql").read_text(encoding="utf-8"))

    def test_real_client_commands_are_still_rejected(self):
        from teamlib.migration_bundle import _assert_controls, BundleError
        for sql in (
            "HOST rm -rf /tmp/x\n",
            "SELECT 1 FROM dual;\nEXIT\n",
            "SELECT 1 FROM dual;\nWHENEVER SQLERROR CONTINUE\n",
            "SELECT 1 FROM dual;\nCONNECT scott/tiger@db\n",
            "SELECT 1 FROM dual;\nSPOOL /tmp/out.txt\n",
        ):
            with self.assertRaises(BundleError, msg=sql):
                _assert_controls(sql)


class StatementBoundaryGuardTests(unittest.TestCase):
    def test_inline_client_command_after_a_query_is_refused(self):
        from teamlib.migration_bundle import BundleError, _assert_controls

        with self.assertRaises(BundleError) as raised:
            _assert_controls("SELECT 1 FROM dual; HOST rm -rf /;\n")
        self.assertIn("control command", str(raised.exception))

    def test_inline_nested_include_after_a_query_is_refused(self):
        from teamlib.migration_bundle import BundleError, _assert_controls

        with self.assertRaises(BundleError) as raised:
            _assert_controls("SELECT 1 FROM dual; @malicious.sql;\n")
        self.assertIn("nested SQLcl includes", str(raised.exception))

    def test_command_after_an_unterminated_set_is_refused(self):
        from teamlib.migration_bundle import BundleError, _assert_controls

        with self.assertRaises(BundleError):
            _assert_controls("SET HEADING OFF\nHOST rm -rf /;\n")

    def test_ordinary_sql_using_reserved_words_still_passes(self):
        from teamlib.migration_bundle import _assert_controls

        _assert_controls(
            "SELECT level\n"
            "  FROM dual\n"
            "CONNECT BY level <= 3;\n"
            "CREATE TABLE t (host VARCHAR2(30));\n"
        )

    def test_directive_inside_a_q_quoted_literal_is_not_a_directive(self):
        from teamlib.migration_bundle import _comment_directives

        text = "SELECT q'[It's a trap -- depends-on: forged]' FROM dual;\n"
        self.assertEqual(_comment_directives(text), [])

    def test_real_directives_are_still_parsed(self):
        from teamlib.migration_bundle import _comment_directives

        text = "-- migration-version: 1\n-- target: tables\nSELECT 1 FROM dual;\n"
        self.assertEqual(
            _comment_directives(text),
            [(0, "migration-version", "1"), (24, "target", "tables")],
        )


if __name__ == "__main__":
    unittest.main()
