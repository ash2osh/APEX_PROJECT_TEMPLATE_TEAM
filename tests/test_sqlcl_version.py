from __future__ import annotations

import unittest

from scripts import apex_compatibility, migration_runtime, sqlcl_version


class SqlclVersionTests(unittest.TestCase):
    def test_shared_parser_accepts_existing_sqlcl_output_fixtures(self) -> None:
        self.assertEqual(
            "26.3",
            sqlcl_version.parse_sqlcl_version("SQLcl: Release 26.3 Production\nBuild: 26.3.0.260.1620\n"),
        )
        self.assertEqual("26.3.0.0", sqlcl_version.parse_sqlcl_version("SQLcl: Release 26.3.0.0 Production\n"))

    def test_shared_floor_accepts_26_3_or_newer_and_rejects_older_versions(self) -> None:
        for version in ("26.3", "26.3.0.0", "26.3.0.1", "27.1"):
            with self.subTest(version=version):
                sqlcl_version.require_sqlcl_version(version)
        for version in ("26.2.2.0", "bad"):
            with self.subTest(version=version), self.assertRaises(sqlcl_version.CompatibilityError):
                sqlcl_version.require_sqlcl_version(version)

    def test_release_adapter_and_migration_runtime_share_the_neutral_api(self) -> None:
        self.assertEqual(sqlcl_version.MINIMUM_SQLCL, apex_compatibility.MINIMUM_SQLCL)
        self.assertIs(apex_compatibility.CompatibilityError, sqlcl_version.CompatibilityError)
        self.assertIs(apex_compatibility.parse_sqlcl_version, sqlcl_version.parse_sqlcl_version)
        self.assertIs(apex_compatibility.require_sqlcl_version, sqlcl_version.require_sqlcl_version)
        self.assertIs(migration_runtime.CompatibilityError, sqlcl_version.CompatibilityError)
        self.assertIs(migration_runtime.parse_sqlcl_version, sqlcl_version.parse_sqlcl_version)
        self.assertIs(migration_runtime.require_sqlcl_version, sqlcl_version.require_sqlcl_version)

    def test_ambiguous_or_missing_release_is_rejected(self) -> None:
        for output in (
            "Java 26.3\n",
            "SQLcl: Release 26.3preview\n",
            "SQLcl: Release 26.3 Production\nSQLcl: Release 26.2 Production\n",
        ):
            with self.subTest(output=output), self.assertRaises(sqlcl_version.CompatibilityError):
                sqlcl_version.parse_sqlcl_version(output)


if __name__ == "__main__":
    unittest.main()
