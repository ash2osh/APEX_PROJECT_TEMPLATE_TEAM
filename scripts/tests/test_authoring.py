from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from teamlib.authoring import add_dependency, new_migration
from teamlib.migration_bundle import load_bundles


class AuthoringTests(unittest.TestCase):
    def test_new_and_dependency_authoring_are_offline(self):
        with tempfile.TemporaryDirectory(prefix="team-authoring-") as directory:
            root = Path(directory)
            dependency = "20260907T100000__alice__base"
            (root / f"{dependency}.sql").write_text("-- migration-version: 1\n-- target: tables\n-- destructive: false\n\nCREATE TABLE BASE_T(ID NUMBER);\n", encoding="utf-8")
            (root / f"{dependency}.verify.sql").write_text("", encoding="utf-8")
            created = new_migration(root, "bob", "child", "tables", timestamp="20260907T100001")
            add_dependency(root, created.migration_id, dependency)
            bundles = load_bundles(root)
            self.assertEqual(bundles[created.migration_id].dependencies[0][0], dependency)
            self.assertEqual(created.sql_path.read_bytes().count(b"-- migration-version:"), 1)


if __name__ == "__main__":
    unittest.main()
