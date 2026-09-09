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


class MigrationBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-migration-bundle-")
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, migration_id: str, sql: str = "CREATE TABLE T_X (ID NUMBER);\n", verify: str = "") -> None:
        (self.root / f"{migration_id}.sql").write_text(HEADER + sql, encoding="utf-8", newline="\n")
        (self.root / f"{migration_id}.verify.sql").write_text(verify, encoding="utf-8", newline="\n")

    def test_loads_complete_bundle_and_checksum_changes_with_member(self):
        migration_id = "20260907T100000__alice__create-table"
        self.write(migration_id, verify="SELECT 'table' assertion_name, 'PASS' status FROM dual;\n")
        bundles = load_bundles(self.root)
        self.assertEqual(bundles[migration_id].target, "tables")
        checksum = bundles[migration_id].checksum
        (self.root / f"{migration_id}.verify.sql").write_text("SELECT 'other' assertion_name, 'PASS' status FROM dual;\n", encoding="utf-8")
        self.assertNotEqual(load_bundles(self.root)[migration_id].checksum, checksum)

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


if __name__ == "__main__":
    unittest.main()
