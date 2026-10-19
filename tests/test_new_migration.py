import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
from scripts import new_migration
from scripts.migration_manifest import load_migration

ENV = {
    "DB_ENVIRONMENT": "development",
    "CODE_SQLCL_CONNECTION": "dev_code",
    "CODE_EXPECTED_USER": "APP",
    "CODE_SCHEMA": "APP",
}
BODY = ["package body pkg as\n", "  procedure p is begin null; end p;\n", "end pkg;"]
SPEC = ["package pkg as\n", "  procedure p;\n", "end pkg;"]
NOW = dt.datetime(2026, 10, 19, 12, 30, tzinfo=dt.timezone.utc)


def catalog(units):
    rows = [{"name": name, "type": kind, "line": number, "text": text}
            for (kind, name), lines in units.items() for number, text in enumerate(lines, start=1)]
    return {
        "coverage": {"sections": {s: {"complete": True} for s in new_migration.SECTIONS}},
        "unavailable": {},
        "sections": {"baseline-source": rows, "baseline-settings": []},
        "identity": {"db_name": "DEVDB", "service_name": "dev.svc", "current_schema": "APP"},
    }


class NewMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()
        self.calls = []

    def tearDown(self):
        self.temporary.cleanup()

    def capture(self, units):
        def fake(values, environment, schema, sections, scratch, **options):
            self.calls.append((environment, schema, tuple(sections), tuple(options.get("baseline_prefixes", ()))))
            return catalog(units)
        return fake

    def create(self, sources, units=None, name="change-pkg"):
        units = units if units is not None else {("PACKAGE BODY", "PKG"): BODY, ("PACKAGE", "PKG"): SPEC}
        return new_migration.create(self.root, name, sources, environ=ENV, capture=self.capture(units), now=lambda: NOW)

    def test_writes_live_source_steps_and_checks_that_refuse_a_changed_object(self):
        folder = self.create(["PACKAGE:PKG", "package_body:pkg"])
        self.assertEqual(folder.relative_to(self.root).as_posix(), "migrations/APP/2026-10-19_change-pkg-r001")
        self.assertEqual(self.calls, [("dev", "APP", new_migration.SECTIONS, ("PKG",))])
        self.assertEqual(sorted(p.name for p in folder.iterdir()),
                         ["001-pkg-spec.sql", "002-pkg-body.sql", "README.md", "checks.json"])
        body = (folder / "002-pkg-body.sql").read_text(encoding="utf-8")
        self.assertTrue(body.startswith('CREATE OR REPLACE PACKAGE BODY "APP"."PKG" as\n'))
        self.assertTrue(body.endswith("end pkg;\n/\n"))
        checks = json.loads((folder / "checks.json").read_text(encoding="utf-8"))
        pre_ids = [check["id"] for check in checks["preconditions"]]
        self.assertEqual(pre_ids[0], "code-owner-session")
        self.assertIn("unchanged-since-read-pkg-body-001", pre_ids)
        unchanged = next(c for c in checks["preconditions"] if c["id"] == "unchanged-since-read-pkg-body-001")["sql"]
        self.assertIn("ALL_SOURCE", unchanged)
        self.assertIn("'  procedure p is begin null; end p;", unchanged)
        self.assertEqual([c["id"] for c in checks["postconditions"]], ["code-owner-session", "valid-pkg-spec", "valid-pkg-body"])
        self.assertIn("2026-10-19 12:30:00 UTC", (folder / "README.md").read_text(encoding="utf-8"))
        load_migration(self.root, folder.relative_to(self.root).as_posix())
        self.assertFalse((self.root / "scratch" / "new-migration").exists() and any((self.root / "scratch" / "new-migration").iterdir()))

    def test_an_object_missing_on_dev_writes_nothing(self):
        with self.assertRaisesRegex(new_migration.NewMigrationError, "not found in APP on DEV: PROCEDURE NOPE"):
            self.create(["PACKAGE:PKG", "PROCEDURE:NOPE"])
        self.assertEqual(list((self.root / "migrations").iterdir()), [])

    def test_an_existing_family_is_refused_before_reading_the_database(self):
        (self.root / "migrations" / "APP" / "2026-10-01_change-pkg-r001").mkdir(parents=True)
        with self.assertRaisesRegex(new_migration.NewMigrationError, "already exists"):
            self.create(["PACKAGE:PKG"])
        self.assertEqual(self.calls, [])

    def test_bad_names_and_sources_are_refused(self):
        for name, sources, message in (
            ("Bad_Name", ["PACKAGE:PKG"], "lowercase words"),
            ("ok-name", [], "at least one object"),
            ("ok-name", ["VIEW:V"], "--source must be TYPE:NAME"),
            ("ok-name", ["PACKAGE"], "--source must be TYPE:NAME"),
        ):
            with self.subTest(name=name, sources=sources), self.assertRaisesRegex(new_migration.NewMigrationError, message):
                self.create(sources, name=name)
        self.assertEqual(self.calls, [])

    def test_main_needs_env_dev_and_reports_errors_with_exit_2(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                new_migration.main(["x", "--source", "PACKAGE:PKG"], environ=ENV, repo_root=self.root)
            with patch.object(new_migration, "_capture", self.capture({})):
                code = new_migration.main(["x", "--source", "PACKAGE:PKG", "--env", "dev"], environ=ENV, repo_root=self.root)
        self.assertEqual(code, 2)
        self.assertIn("new-migration error: not found in APP on DEV: PACKAGE PKG", err.getvalue())

    def test_step_header_is_schema_qualified_only_when_the_first_line_names_the_object(self):
        self.assertEqual(new_migration.step_sql("APP", "PACKAGE BODY", "PKG", ['PACKAGE BODY "PKG" IS\n', "end;"]).splitlines()[0],
                         'CREATE OR REPLACE PACKAGE BODY "APP"."PKG" IS')
        self.assertEqual(new_migration.step_sql("APP", "PACKAGE BODY", "PKG", ["PACKAGE BODY\n", "  pkg IS\n", "end;"]).splitlines()[0],
                         "CREATE OR REPLACE PACKAGE BODY")


if __name__ == "__main__":
    unittest.main()
