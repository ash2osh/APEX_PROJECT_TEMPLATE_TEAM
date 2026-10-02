import json
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFIER = ROOT / "scripts" / "verify_publish_state.py"


class VerifyPublishStateTests(unittest.TestCase):
    def make_fixture(self, root: Path) -> tuple[Path, Path, Path, Path, Path]:
        source = root / "source"
        exported = root / "exported"
        (source / ".apex").mkdir(parents=True)
        (source / "deployments").mkdir()
        (exported / ".apex").mkdir(parents=True)
        (source / "application.apx").write_bytes(b"application {}\n")
        (source / ".apex" / "apexlang.json").write_bytes(b'{"version":1}\n')
        (source / "deployments" / "dev.json").write_text("{}\n", encoding="utf-8")
        (exported / "application.apx").write_bytes(b"application {}\n")
        (exported / ".apex" / "apexlang.json").write_bytes(b'{"version":1}\n')
        marker = source / "apex-team-export.json"
        marker.write_text(
            json.dumps(
                {
                    "applicationId": 100,
                    "applicationPresent": True,
                    "builderLastUpdatedOn": "2026-09-26T08:00:00",
                    "version": "Release 1.0",
                }
            ) + "\n",
            encoding="utf-8",
        )
        before = root / "before.txt"
        after = root / "after.txt"
        before.write_text("2026-09-26T09:30:00|2026-09-26T09:30:03|Release 1.0\n", encoding="utf-8")
        after.write_text("2026-09-26T09:30:00|2026-09-26T09:30:04|Release 1.0\n", encoding="utf-8")
        return source, exported, before, after, marker

    def run_verifier(self, source: Path, exported: Path, before: Path, after: Path):
        return subprocess.run(
            [
                sys.executable, str(VERIFIER), "100", str(source), str(exported), str(before), str(after),
                "--repo-root", str(source.parent), "--record-baseline",
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_exact_reexport_advances_marker_to_verified_live_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))

            result = self.run_verifier(source, exported, before, after)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(marker["applicationId"], 100)
            self.assertEqual(marker["builderLastUpdatedOn"], "2026-09-26T09:30:00")
            self.assertIs(marker["applicationPresent"], True)
            self.assertEqual(marker["version"], "Release 1.0")

    # An APEXlang import leaves last_updated_on NULL (APEX skips its audit
    # columns while importing), so this is the normal post-publish state.
    def test_imported_app_without_builder_timestamp_advances_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            before.write_text("NO_TIMESTAMP|2026-09-26T09:30:03|Release 1.0\n", encoding="utf-8")
            after.write_text("NO_TIMESTAMP|2026-09-26T09:30:04|Release 1.0\n", encoding="utf-8")

            result = self.run_verifier(source, exported, before, after)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            self.assertEqual(
                marker,
                {
                    "applicationId": 100,
                    "applicationPresent": True,
                    "builderLastUpdatedOn": None,
                    "version": "Release 1.0",
                },
            )

    def test_absent_app_after_import_does_not_advance_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            before.write_text("NOT_FOUND|2026-09-26T09:30:03|\n", encoding="utf-8")
            after.write_text("NOT_FOUND|2026-09-26T09:30:04|\n", encoding="utf-8")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not visible", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

    def test_intervening_import_does_not_advance_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            before.write_text("NO_TIMESTAMP|2026-09-26T09:30:03|V2 [ASHARIF-2026-09-26r001]\n", encoding="utf-8")
            after.write_text("NO_TIMESTAMP|2026-09-26T09:30:04|V2 [BOB-2026-09-26r001]\n", encoding="utf-8")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("changed while", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

    def test_source_mismatch_does_not_advance_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            (exported / "application.apx").write_bytes(b"application { changed }\n")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("do not match", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

    def test_a_different_live_publish_tag_says_a_teammates_import_replaced_this_one(self) -> None:
        # Two developers who publish within one import's duration both pass the
        # drift check; the later import wins and the earlier one fails here.
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            (source / "application.apx").write_text('app X (\n    version: "Release 1.0 [ALICE-2026-10-01r007]"\n)\n', encoding="utf-8")
            (exported / "application.apx").write_text('app X (\n    version: "Release 1.0 [BOB-2026-10-01r007]"\n)\n', encoding="utf-8")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("do not match", result.stderr)
            self.assertIn("[BOB-2026-10-01r007]", result.stderr)
            self.assertIn("[ALICE-2026-10-01r007]", result.stderr)
            self.assertIn("a teammate's import replaced yours", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

    def test_a_mismatch_with_the_same_publish_tag_does_not_blame_a_teammate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, _ = self.make_fixture(Path(temporary))
            same = 'app X (\n    version: "Release 1.0 [ALICE-2026-10-01r007]"\n)\n'
            (source / "application.apx").write_text(same, encoding="utf-8")
            (exported / "application.apx").write_text(same.replace("app X", "app Y"), encoding="utf-8")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("do not match", result.stderr)
            self.assertNotIn("teammate", result.stderr)

    def test_intervening_builder_revision_does_not_advance_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            after.write_text("2026-09-26T09:31:00|2026-09-26T09:31:02|Release 1.0\n", encoding="utf-8")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("changed while", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

    def test_ambiguous_one_second_revision_does_not_advance_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            before.write_text("2026-09-26T09:30:03|2026-09-26T09:30:03|Release 1.0\n", encoding="utf-8")
            after.write_text("2026-09-26T09:30:03|2026-09-26T09:30:04|Release 1.0\n", encoding="utf-8")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("same database second", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

    def test_future_revision_does_not_advance_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            before.write_text("2026-09-26T09:30:08|2026-09-26T09:30:06|Release 1.0\n", encoding="utf-8")
            after.write_text("2026-09-26T09:30:08|2026-09-26T09:30:07|Release 1.0\n", encoding="utf-8")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("later than the database-time observation", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

    # SQLcl's starter application (apex generate) ships supporting-objects/deinstall-script.sql
    # with no content, and APEX leaves an empty script out of an export.
    DEINSTALL = Path("supporting-objects") / "deinstall-script.sql"

    def test_an_empty_deinstall_script_that_the_export_leaves_out_does_not_stop_the_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            (source / self.DEINSTALL).parent.mkdir()
            (source / self.DEINSTALL).write_bytes(b"")

            result = self.run_verifier(source, exported, before, after)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(marker_path.read_text(encoding="utf-8"))["builderLastUpdatedOn"], "2026-09-26T09:30:00")

    def test_a_deinstall_script_with_content_must_come_back_in_the_re_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            (source / self.DEINSTALL).parent.mkdir()
            (source / self.DEINSTALL).write_bytes(b"DROP TABLE T;\n")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing from re-export: supporting-objects/deinstall-script.sql", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

    def test_any_other_file_missing_from_the_re_export_still_stops_the_baseline_even_when_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            (source / "pages").mkdir()
            (source / "pages" / "p00002-empty.apx").write_bytes(b"")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing from re-export: pages/p00002-empty.apx", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

    def test_a_deinstall_script_only_in_the_re_export_still_stops_the_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            (exported / self.DEINSTALL).parent.mkdir()
            (exported / self.DEINSTALL).write_bytes(b"")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unexpected in re-export: supporting-objects/deinstall-script.sql", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)


if __name__ == "__main__":
    unittest.main()
