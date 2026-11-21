import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _test_subprocess_safety  # noqa: F401
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
from scripts import baseline, new_migration
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
        # create() returns resolved paths; on Windows the temp dir is a short (RUNNER~1) name.
        self.root = Path(self.temporary.name).resolve()
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

    def test_captured_compiler_settings_are_included_in_steps_and_postconditions(self):
        settings_rows = [
            {
                "name": "PKG",
                "type": "PACKAGE BODY",
                "plsql_optimize_level": 2,
                "plsql_code_type": "INTERPRETED",
                "plsql_debug": "FALSE",
                "plsql_warnings": "ENABLE:ALL",
                "plsql_ccflags": "FEATURE_X:TRUE",
                "nls_length_semantics": "CHAR",
                "plscope_settings": "IDENTIFIERS:ALL",
            },
            {
                "name": "PKG",
                "type": "PACKAGE",
                "plsql_optimize_level": 3,
                "plsql_code_type": "INTERPRETED",
                "plsql_debug": "FALSE",
                "plsql_warnings": "DISABLE:ALL",
                "plsql_ccflags": "FEATURE_X:TRUE",
                "nls_length_semantics": "CHAR",
                "plscope_settings": "",
            },
        ]

        def fake_capture(values, environment, schema, sections, scratch, **options):
            cat = catalog({("PACKAGE BODY", "PKG"): BODY, ("PACKAGE", "PKG"): SPEC})
            cat["sections"]["baseline-settings"] = settings_rows
            return cat

        folder = new_migration.create(
            self.root, "change-pkg", ["PACKAGE:PKG", "package_body:pkg"],
            environ=ENV, capture=fake_capture, now=lambda: NOW
        )
        spec = (folder / "001-pkg-spec.sql").read_text(encoding="utf-8")
        self.assertIn("ALTER SESSION SET PLSQL_CCFLAGS = 'FEATURE_X:TRUE';", spec)
        self.assertIn("ALTER SESSION SET PLSQL_OPTIMIZE_LEVEL = 3;", spec)
        body = (folder / "002-pkg-body.sql").read_text(encoding="utf-8")
        self.assertIn("ALTER SESSION SET PLSQL_CCFLAGS = 'FEATURE_X:TRUE';", body)
        self.assertIn("ALTER SESSION SET PLSQL_OPTIMIZE_LEVEL = 2;", body)

        checks = json.loads((folder / "checks.json").read_text(encoding="utf-8"))
        post_ids = [c["id"] for c in checks["postconditions"]]
        self.assertIn("settings-pkg-spec", post_ids)
        self.assertIn("settings-pkg-body", post_ids)
        spec_check = next(c for c in checks["postconditions"] if c["id"] == "settings-pkg-spec")
        self.assertIn("ALL_PLSQL_OBJECT_SETTINGS", spec_check["sql"])
        self.assertIn("S.PLSQL_CCFLAGS = 'FEATURE_X:TRUE'", spec_check["sql"])
        load_migration(self.root, folder.relative_to(self.root).as_posix())

    def test_an_object_missing_on_dev_writes_nothing(self):
        with self.assertRaisesRegex(new_migration.NewMigrationError, "not found in APP on DEV: PROCEDURE NOPE"):
            self.create(["PACKAGE:PKG", "PROCEDURE:NOPE"])
        self.assertEqual(list((self.root / "migrations").iterdir()), [])

    def test_an_existing_family_is_refused_before_reading_the_database(self):
        (self.root / "migrations" / "APP" / "2026-10-01_change-pkg-r001").mkdir(parents=True)
        with self.assertRaisesRegex(new_migration.NewMigrationError, "already exists"):
            self.create(["PACKAGE:PKG"])
        self.assertEqual(self.calls, [])

    def test_split_owners_are_refused_before_dev_is_read(self):
        # migrate runs migrations as MIGRATION_SCHEMA, so a folder under the CODE_SCHEMA owner could never apply.
        split = {
            **ENV, "CODE_SCHEMA": "APPCODE", "CODE_EXPECTED_USER": "APPCODE",
            "MIGRATION_SCHEMA": "APPDATA", "MIGRATION_SQLCL_CONNECTION": "dev_mig", "MIGRATION_EXPECTED_USER": "APPDATA",
        }
        with self.assertRaisesRegex(new_migration.NewMigrationError, "schema APPCODE") as raised:
            new_migration.create(self.root, "change-pkg", ["PACKAGE:PKG"], environ=split,
                                 capture=self.capture({("PACKAGE", "PKG"): SPEC}), now=lambda: NOW)
        message = str(raised.exception)
        self.assertIn("MIGRATION_SCHEMA APPDATA", message)
        self.assertIn("scripts/team.sh targets", message)
        self.assertEqual(self.calls, [])
        self.assertEqual(list((self.root / "migrations").iterdir()), [])

    def test_overlapping_owners_are_accepted(self):
        # APP is in both lists, so the folder lands under APP where migrate looks for it; APPX is refused.
        overlap = {
            **ENV, "CODE_SCHEMA": "APP,APPX", "CODE_SQLCL_CONNECTION": "dev_code,dev_code",
            "CODE_EXPECTED_USER": "APP,APPX", "MIGRATION_SCHEMA": "APP",
            "MIGRATION_SQLCL_CONNECTION": "dev_code", "MIGRATION_EXPECTED_USER": "APP",
        }
        units = {("PACKAGE BODY", "PKG"): BODY, ("PACKAGE", "PKG"): SPEC}
        folder = new_migration.create(self.root, "change-pkg", ["PACKAGE:PKG"], environ=overlap,
                                      schema="APP", capture=self.capture(units), now=lambda: NOW)
        self.assertEqual(folder.relative_to(self.root).as_posix(), "migrations/APP/2026-10-19_change-pkg-r001")
        self.assertEqual(self.calls, [("dev", "APP", new_migration.SECTIONS, ("PKG",))])
        with self.assertRaisesRegex(new_migration.NewMigrationError, "schema APPX"):
            new_migration.create(self.root, "other-pkg", ["PACKAGE:PKG"], environ=overlap,
                                 schema="APPX", capture=self.capture(units), now=lambda: NOW)
        self.assertEqual(len(self.calls), 1)

    def test_no_migration_profile_leaves_the_code_schemas_accepted(self):
        # Without MIGRATION_* keys the migration list is the CODE list, so every named code schema is accepted.
        two_codes = {**ENV, "CODE_SCHEMA": "APP,APPX", "CODE_SQLCL_CONNECTION": "dev_code,dev_code",
                     "CODE_EXPECTED_USER": "APP,APPX"}
        folder = new_migration.create(self.root, "change-pkg", ["PACKAGE:PKG"], environ=two_codes,
                                      schema="APPX", capture=self.capture({("PACKAGE", "PKG"): SPEC}), now=lambda: NOW)
        self.assertEqual(folder.relative_to(self.root).as_posix(), "migrations/APPX/2026-10-19_change-pkg-r001")
        self.assertEqual(self.calls, [("dev", "APPX", new_migration.SECTIONS, ("PKG",))])

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

    def test_package_source_points_to_the_feature_path_and_trigger_source_does_not(self):
        units = {("PACKAGE BODY", "PKG"): BODY, ("PACKAGE", "PKG"): SPEC, ("TRIGGER", "TRG"): ["trigger trg\n", "end;"]}
        for name, source, noted in (("pkg-note", "PACKAGE:PKG", True), ("trg-note", "TRIGGER:TRG", False)):
            with self.subTest(source=source):
                err = io.StringIO()
                with patch.object(new_migration, "_capture", self.capture(units)), \
                        contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                    code = new_migration.main([name, "--source", source, "--env", "dev"], environ=ENV, repo_root=self.root)
                self.assertEqual(code, 0)
                self.assertEqual(new_migration.FEATURE_NOTE in err.getvalue(), noted, err.getvalue())

    def test_step_header_is_schema_qualified_only_when_the_first_line_names_the_object(self):
        self.assertEqual(new_migration.step_sql("APP", "PACKAGE BODY", "PKG", ['PACKAGE BODY "PKG" IS\n', "end;"]).splitlines()[0],
                         'CREATE OR REPLACE PACKAGE BODY "APP"."PKG" IS')
        self.assertEqual(new_migration.step_sql("APP", "PACKAGE BODY", "PKG", ["PACKAGE BODY\n", "  pkg IS\n", "end;"]).splitlines()[0],
                         "CREATE OR REPLACE PACKAGE BODY")


PROC = ["procedure proc1 is\n", "begin null; end proc1;\n"]
SETTING_KEYS = {
    "plsql_optimize_level": 2, "plsql_code_type": "INTERPRETED", "plsql_debug": "FALSE",
    "plsql_warnings": "DISABLE:ALL", "plsql_ccflags": "", "nls_length_semantics": "CHAR", "plscope_settings": "",
}
IDENTITY = {
    "session_user": "APP", "current_schema": "APP", "db_name": "DEVDB", "db_unique_name": "DEVDB",
    "service_name": "dev.svc", "container_id": "1", "container_name": "DEVPDB", "edition": "ORA$BASE",
    "database_version": "26.1.0.0.0",
}


def feature_catalog(units):
    """A DEV capture of the units, with the compiler settings and identity a feature capture reads."""
    result = catalog(units)
    result["sections"]["baseline-settings"] = [
        {"name": name, "type": kind, **SETTING_KEYS} for kind, name in units
    ]
    result["identity"] = dict(IDENTITY)
    return result


class FeatureRoutingTests(unittest.TestCase):
    """new-migration in a feature project: feature files by default, or --feature, or --dated-folder."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "migrations").mkdir()
        self.units = {("PACKAGE BODY", "PKG"): BODY, ("PACKAGE", "PKG"): SPEC, ("PROCEDURE", "PROC1"): PROC}
        self.captures = []

    def fake_capture(self, units=None):
        units = self.units if units is None else units

        def fake(values, environment, schema, sections, scratch, **options):
            self.captures.append((schema, tuple(options.get("baseline_prefixes", ()))))
            return feature_catalog(units)
        return fake

    def make_feature(self, feature, relative, kind, name, lines):
        """A feature file as `feature capture` writes it, so the claim is read from disk."""
        path = self.root / "features" / feature / "APP" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_migration.step_sql("APP", kind, name, lines), encoding="utf-8", newline="\n")
        return path

    def claim_path(self, feature, relative):
        return (Path("features") / feature / "APP" / relative).as_posix()

    def snapshot(self):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*"))

    def run_main(self, argv, environ=ENV, capture=None):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(baseline, "_capture", capture or self.fake_capture()), \
                patch.object(new_migration, "_capture", capture or self.fake_capture()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = new_migration.main(argv, environ=environ, repo_root=self.root)
        return code, out.getvalue(), err.getvalue()

    def test_feature_project_without_options_refuses_per_unit_and_creates_nothing(self):
        self.make_feature("billing", "procedures/proc1.prc.sql", "PROCEDURE", "PROC1", PROC)
        before = self.snapshot()

        def no_read(*args, **kwargs):
            raise AssertionError("DEV must not be read for a routing refusal")

        code, out, err = self.run_main(
            ["change", "--source", "PROCEDURE:PROC1", "--source", "PACKAGE:PKG", "--env", "dev"],
            capture=no_read,
        )
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.captures, [])
        self.assertIn(
            f"procedure PROC1: owned by feature billing: edit {self.claim_path('billing', 'procedures/proc1.prc.sql')}",
            err,
        )
        self.assertIn("package PKG: scripts/team.sh feature capture --package PKG --schema APP --env dev <feature>", err)
        self.assertIn(
            "pass --feature <slug> to capture into a feature now, or --dated-folder to create a dated migration folder as before",
            err,
        )

    def test_trigger_capture_command_uses_the_trigger_flag(self):
        self.make_feature("billing", "procedures/proc1.prc.sql", "PROCEDURE", "PROC1", PROC)
        units = {("TRIGGER", "TRG"): ["trigger trg\n", "end;\n"]}
        code, _out, err = self.run_main(["change", "--source", "TRIGGER:TRG", "--env", "dev"],
                                        capture=self.fake_capture(units))
        self.assertEqual(code, 2)
        self.assertIn("trigger TRG: scripts/team.sh feature capture --trigger TRG --schema APP --env dev <feature>", err)
        self.assertEqual(self.captures, [])

    def test_feature_flag_captures_unowned_units_and_skips_units_already_in_the_feature(self):
        self.make_feature("billing", "procedures/proc1.prc.sql", "PROCEDURE", "PROC1", PROC)
        code, out, err = self.run_main(
            ["change", "--feature", "billing", "--source", "PROCEDURE:PROC1", "--source", "PACKAGE:PKG",
             "--source", "PACKAGE BODY:PKG", "--env", "dev"],
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(self.captures, [("APP", ("PKG",))])  # one capture for the package spec and body
        self.assertIn(f"already in billing: edit {self.claim_path('billing', 'procedures/proc1.prc.sql')}", out)
        self.assertIn("Captured features/billing/APP/packages/pkg.pks.sql", out)
        self.assertIn("Captured features/billing/APP/packages/pkg.pkb.sql", out)
        self.assertTrue((self.root / "features" / "billing" / "APP" / "packages" / "pkg.pks.sql").is_file())
        self.assertTrue((self.root / "features" / "billing" / "APP" / "packages" / "pkg.pkb.sql").is_file())
        self.assertEqual(sorted(p.name for p in (self.root / "features" / "billing" / "APP" / "procedures").iterdir()),
                         ["proc1.prc.sql"])
        self.assertEqual(list((self.root / "migrations").iterdir()), [])

    def test_feature_flag_refuses_a_unit_owned_by_another_feature_before_any_capture(self):
        self.make_feature("other", "procedures/proc1.prc.sql", "PROCEDURE", "PROC1", PROC)
        code, _out, err = self.run_main(
            ["change", "--feature", "billing", "--source", "PACKAGE:PKG", "--source", "PROCEDURE:PROC1", "--env", "dev"],
        )
        self.assertEqual(code, 2)
        self.assertEqual(self.captures, [])
        self.assertIn("owned by feature other", err)
        self.assertIn("nothing was captured", err)
        self.assertFalse((self.root / "features" / "billing").exists())

    def test_feature_flag_rejects_an_invalid_slug_before_reading_dev(self):
        code, _out, err = self.run_main(["change", "--feature", "Bad_Slug", "--source", "PACKAGE:PKG", "--env", "dev"])
        self.assertEqual(code, 2)
        self.assertIn("feature must be a lowercase hyphenated slug", err)
        self.assertEqual(self.captures, [])

    def test_dated_folder_in_a_feature_project_matches_a_project_without_features(self):
        plain_dir = tempfile.TemporaryDirectory()
        self.addCleanup(plain_dir.cleanup)
        plain = Path(plain_dir.name).resolve()
        (plain / "migrations").mkdir()
        self.make_feature("billing", "procedures/proc1.prc.sql", "PROCEDURE", "PROC1", PROC)
        argv = ["change-pkg", "--source", "PACKAGE:PKG", "--env", "dev", "--dated-folder"]
        code, out, err = self.run_main(argv)
        self.assertEqual(code, 0, err)
        plain_out, plain_err = io.StringIO(), io.StringIO()
        with patch.object(new_migration, "_capture", self.fake_capture()), \
                contextlib.redirect_stdout(plain_out), contextlib.redirect_stderr(plain_err):
            plain_code = new_migration.main(argv, environ=ENV, repo_root=plain)
        self.assertEqual(plain_code, 0)
        self.assertEqual(out, plain_out.getvalue())
        self.assertEqual(err, plain_err.getvalue())
        folder = next((self.root / "migrations" / "APP").iterdir())
        plain_folder = next((plain / "migrations" / "APP").iterdir())
        self.assertEqual(folder.name, plain_folder.name)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), sorted(p.name for p in plain_folder.iterdir()))
        for step in folder.iterdir():
            self.assertEqual(step.read_bytes(), (plain_folder / step.name).read_bytes())
        self.assertEqual(self.captures, [("APP", ("PKG",))] * 2)  # one DEV read per project

    def test_a_project_without_features_keeps_the_dated_folder_default(self):
        code, out, err = self.run_main(["change-pkg", "--source", "PROCEDURE:PROC1", "--env", "dev"])
        self.assertEqual(code, 0, err)
        self.assertIn("Created migration: migrations/APP/", out)
        self.assertFalse((self.root / "features").exists())

    def test_feature_flag_in_a_project_without_features_creates_the_feature(self):
        code, out, err = self.run_main(["change", "--feature", "billing", "--source", "PROCEDURE:PROC1", "--env", "dev"])
        self.assertEqual(code, 0, err)
        self.assertIn(f"Captured {self.claim_path('billing', 'procedures/proc1.prc.sql')}", out)
        self.assertEqual(list((self.root / "migrations").iterdir()), [])

    def test_a_non_unit_source_in_a_feature_project_keeps_the_existing_error(self):
        self.make_feature("billing", "procedures/proc1.prc.sql", "PROCEDURE", "PROC1", PROC)
        before = self.snapshot()
        code, _out, err = self.run_main(["change", "--source", "VIEW:V", "--env", "dev"])
        self.assertEqual(code, 2)
        self.assertIn("new-migration error: --source must be TYPE:NAME", err)
        self.assertNotIn("feature", err)
        self.assertEqual(self.snapshot(), before)

    def test_feature_and_dated_folder_are_mutually_exclusive(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                new_migration.main(["x", "--source", "PACKAGE:PKG", "--env", "dev", "--feature", "billing", "--dated-folder"],
                                   environ=ENV, repo_root=self.root)
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("not allowed with argument", err.getvalue())


if __name__ == "__main__":
    unittest.main()
