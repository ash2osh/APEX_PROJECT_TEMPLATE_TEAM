import io
import contextlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import migrate
from scripts.migration_diff import main, mirror_path

OLD_BODY = (
    '  CREATE OR REPLACE EDITIONABLE PACKAGE BODY "APP"."PKG" AS\n'
    "  FUNCTION f RETURN NUMBER IS BEGIN RETURN 1; END f;\n"
    "END PKG;\n/\n"
)
NEW_BODY = OLD_BODY.replace("END PKG;", "  FUNCTION g RETURN NUMBER IS BEGIN RETURN 2; END g;\nEND PKG;")


class MigrationDiffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        mirror = self.root / "database" / "APP" / "packages"
        mirror.mkdir(parents=True)
        (mirror / "PKG_BODY.sql").write_text(OLD_BODY, encoding="utf-8")
        self.folder = self.root / "migrations" / "APP" / "2026-01-31_add-g-r001"
        self.folder.mkdir(parents=True)
        (self.folder / "001-pkg-body.sql").write_text(NEW_BODY, encoding="utf-8")
        (self.folder / "002-add-column.sql").write_text("ALTER TABLE T ADD (C NUMBER);\n", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = main(list(args), root=self.root)
        return code, out.getvalue()

    def test_mirror_path_maps_spec_body_and_dollar_names(self):
        self.assertEqual(mirror_path('CREATE OR REPLACE EDITIONABLE PACKAGE "A"."P" AS'), Path("database/A/packages/P_SPEC.sql"))
        self.assertEqual(mirror_path('CREATE OR REPLACE PACKAGE BODY a.p$x AS'), Path("database/A/packages/P-S-X_BODY.sql"))
        self.assertIsNone(mirror_path("ALTER TABLE T ADD (C NUMBER);"))

    def test_writes_only_the_changed_lines_into_the_dated_folder(self):
        code, output = self.run_main("migrations/APP/2026-01-31_add-g-r001", "--date", "2026-01-31")
        self.assertEqual(code, 0, output)
        diff = (self.root / "ai_generate" / "2026-01-31" / "2026-01-31_add-g-r001.diff").read_text(encoding="utf-8")
        self.assertIn("# 001-pkg-body.sql: +1 -0 (vs database/APP/packages/PKG_BODY.sql)", diff)
        self.assertIn("+  FUNCTION g RETURN NUMBER IS BEGIN RETURN 2; END g;", diff)
        self.assertNotIn("+  FUNCTION f RETURN NUMBER", diff)  # unchanged lines are context, not additions

    def test_step_without_a_mirror_counterpart_is_shown_in_full(self):
        self.run_main("migrations/APP/2026-01-31_add-g-r001", "--date", "2026-01-31")
        diff = (self.root / "ai_generate" / "2026-01-31" / "2026-01-31_add-g-r001.diff").read_text(encoding="utf-8")
        self.assertIn("# 002-add-column.sql: +1 -0 (no mirror counterpart, shown in full)", diff)

    def test_read_only_for_the_migration_and_the_mirror(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.run_main("migrations/APP/2026-01-31_add-g-r001", "--date", "2026-01-31")
        after = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file() and "ai_generate" not in p.parts}
        self.assertEqual(before, after)

    def test_refuses_a_missing_folder_and_a_bad_date(self):
        self.assertEqual(self.run_main("migrations/APP/nope")[0], 2)
        self.assertEqual(self.run_main("migrations/APP/2026-01-31_add-g-r001", "--date", "today")[0], 2)


class DailyDiffAfterMigrateTests(unittest.TestCase):
    """`migrate --env dev` records the readable diff after a verified apply."""

    setUp = MigrationDiffTests.setUp
    tearDown = MigrationDiffTests.tearDown

    def record(self, day="2026-01-31"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            migrate.record_daily_diffs(self.root, [SimpleNamespace(folder=self.folder)], day)
        return out.getvalue()

    def diff_file(self):
        return self.root / "ai_generate" / "2026-01-31" / "2026-01-31_add-g-r001.diff"

    def test_writes_the_diff_when_the_project_has_ai_generate(self):
        (self.root / "ai_generate").mkdir()
        output = self.record()
        self.assertIn("daily change record: wrote ai_generate/2026-01-31/2026-01-31_add-g-r001.diff", output)
        self.assertIn("+  FUNCTION g", self.diff_file().read_text(encoding="utf-8"))

    def test_keeps_an_existing_diff_written_before_the_mirror_refresh(self):
        self.diff_file().parent.mkdir(parents=True)
        self.diff_file().write_text("first diff\n", encoding="utf-8")
        output = self.record()
        self.assertIn("daily change record: kept", output)
        self.assertEqual(self.diff_file().read_text(encoding="utf-8"), "first diff\n")

    def test_does_nothing_without_ai_generate(self):
        self.assertEqual(self.record(), "")
        self.assertFalse((self.root / "ai_generate").exists())

    def test_a_failure_is_reported_and_not_raised(self):
        (self.root / "ai_generate").mkdir()
        for step in self.folder.glob("*.sql"):
            step.unlink()
        output = self.record()
        self.assertIn("daily change record: not written for 2026-01-31_add-g-r001", output)

    def test_main_records_only_after_a_successful_dev_apply(self):
        cases = (("dev", 0, 1), ("dev", 1, 0), ("staging", 0, 0), ("prod", 0, 0))
        for environment, status, calls in cases:
            with self.subTest(environment=environment, status=status):
                target = SimpleNamespace(environment=environment, schema="APP")
                with patch.object(migrate, "load_batch", return_value=[SimpleNamespace(folder=self.folder, schema="APP")]), \
                        patch.object(migrate, "batch_schema", return_value="APP"), \
                        patch.object(migrate, "resolve_target", return_value=target), \
                        patch.object(migrate, "assert_single_layout"), \
                        patch.object(migrate, "flat_migrations_apply", return_value=False), \
                        patch.object(migrate, "apply_batch", return_value=status), \
                        patch.object(migrate, "record_daily_diffs") as record:
                    result = migrate.main(["migrations/APP/2026-01-31_add-g-r001", "--env", environment],
                                          environ={}, repo_root=self.root)
                self.assertEqual(result, status)
                self.assertEqual(record.call_count, calls)

    def test_rehearse_never_records(self):
        target = SimpleNamespace(environment="dev", schema="APP")
        with patch.object(migrate, "load_batch", return_value=[SimpleNamespace(folder=self.folder, schema="APP")]), \
                patch.object(migrate, "batch_schema", return_value="APP"), \
                patch.object(migrate, "resolve_target", return_value=target), \
                patch.object(migrate, "assert_single_layout"), \
                patch.object(migrate, "flat_migrations_apply", return_value=False), \
                patch.object(migrate, "rehearse_batch", return_value=0), \
                patch.object(migrate, "record_daily_diffs") as record:
            migrate.main(["migrations/APP/2026-01-31_add-g-r001", "--env", "dev", "--rehearse"], environ={}, repo_root=self.root)
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
