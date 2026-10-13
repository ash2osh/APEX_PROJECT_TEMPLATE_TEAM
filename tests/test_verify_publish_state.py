import json
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path
import shutil
from datetime import datetime, timedelta


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

    def run_verifier(self, source: Path, exported: Path, before: Path, after: Path, *, strict_bytes: bool = False):
        stable_export = exported.parent / "stable-export"
        if stable_export.exists():
            shutil.rmtree(stable_export)
        shutil.copytree(exported, stable_export)
        stable_before = exported.parent / "stable-before.txt"
        stable_after = exported.parent / "stable-after.txt"
        live, observed, version = after.read_text(encoding="utf-8").strip().split("|", 2)
        second_observed = datetime.fromisoformat(observed) + timedelta(seconds=2)
        stable_before.write_text(f"{live}|{second_observed.isoformat()}|{version}\n", encoding="utf-8")
        stable_after.write_text(f"{live}|{(second_observed + timedelta(seconds=1)).isoformat()}|{version}\n", encoding="utf-8")
        command = [
            sys.executable, str(VERIFIER), "100", str(source), str(exported), str(before), str(after),
            "--repo-root", str(source.parent), "--record-baseline",
            "--stability-export-dir", str(stable_export),
            "--stability-before-file", str(stable_before), "--stability-after-file", str(stable_after),
        ]
        if strict_bytes:
            command.append("--strict-bytes")
        return subprocess.run(
            command,
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

    def test_normalized_cosmetic_diff_passes_and_lists_all_cosmetic_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, _ = self.make_fixture(Path(temporary))
            path = Path("shared-components/messages.apx")
            source_file, exported_file = source / path, exported / path
            source_file.parent.mkdir(parents=True)
            exported_file.parent.mkdir(parents=True)
            source_file.write_text(
                "// local comment\n"
                "textMessage Z (\n    message {\n        text: `z`\n        language: en\n    }\n)\n"
                "textMessage A (\n    message {\n        text: `a`\n        language: en\n    }\n)\n\n",
                encoding="utf-8",
            )
            exported_file.write_text(
                "textMessage A (\n    message {\n        text: `a`\n        language: en\n    }\n)\n"
                "textMessage Z (\n    message {\n        text: `z`\n        language: en\n    }\n)\n",
                encoding="utf-8",
            )

            result = self.run_verifier(source, exported, before, after)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("cosmetic-only differences: shared-components/messages.apx", result.stdout)

    def test_real_difference_after_an_earlier_cosmetic_file_is_reported_with_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            cosmetic = Path("shared-components/messages.apx")
            real = Path("shared-components/report.apx")
            real_second = Path("shared-components/z-table.apx")
            for path in (cosmetic, real, real_second):
                (source / path).parent.mkdir(parents=True, exist_ok=True)
                (exported / path).parent.mkdir(parents=True, exist_ok=True)
            (source / cosmetic).write_bytes(b"// comment\ntextMessage Z (\n    language: en\n)\n")
            (exported / cosmetic).write_bytes(b"textMessage Z (\n    language: en\n)\n")
            (source / real).write_bytes(b"page 1 (\n    name: Home\n    title: `Local`\n)\n")
            (exported / real).write_bytes(b"page 1 (\n    name: Home\n    title: `Live`\n)\n")
            (source / real_second).write_bytes(b"region R (\n    title: `Local table`\n)\n")
            (exported / real_second).write_bytes(b"region R (\n    title: `Live table`\n)\n")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("APEXlang source bytes do not match the post-import re-export", result.stderr)
            self.assertIn("shared-components/report.apx (line 3)", result.stderr)
            self.assertIn("shared-components/z-table.apx (line 2)", result.stderr)
            self.assertNotIn("Local", result.stderr)
            self.assertNotIn("Live", result.stderr)
            self.assertIn("cosmetic-only differences: shared-components/messages.apx", result.stdout)
            self.assertEqual(marker_path.read_bytes(), original_marker)

    def test_strict_bytes_rejects_a_cosmetic_only_difference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            (source / "application.apx").write_bytes(b"application {}\n// source comment\n")
            (exported / "application.apx").write_bytes(b"application {}\n")

            result = self.run_verifier(source, exported, before, after, strict_bytes=True)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("APEXlang source bytes do not match the post-import re-export", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

    def test_baseline_requires_second_export_to_have_a_stable_normalized_form(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            unstable = exported.parent / "stable-export"
            shutil.copytree(exported, unstable)
            (unstable / "application.apx").write_bytes(b"application { changed }\n")
            stable_before = exported.parent / "stable-before.txt"
            stable_after = exported.parent / "stable-after.txt"
            stable_before.write_text("2026-09-26T09:30:00|2026-09-26T09:30:06|Release 1.0\n", encoding="utf-8")
            stable_after.write_text("2026-09-26T09:30:00|2026-09-26T09:30:07|Release 1.0\n", encoding="utf-8")
            command = [
                sys.executable, str(VERIFIER), "100", str(source), str(exported), str(before), str(after),
                "--repo-root", str(source.parent), "--record-baseline",
                "--stability-export-dir", str(unstable),
                "--stability-before-file", str(stable_before), "--stability-after-file", str(stable_after),
            ]

            result = subprocess.run(command, text=True, capture_output=True, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("second post-import re-export", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

    def test_verified_summary_keeps_target_release_and_rejects_a_release_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            stable_export = exported.parent / "stable-export"
            shutil.copytree(exported, stable_export)
            stable_before = exported.parent / "stable-before.txt"
            stable_after = exported.parent / "stable-after.txt"
            stable_before.write_text("2026-09-26T09:30:00|2026-09-26T09:30:06|Release 1.0\n", encoding="utf-8")
            stable_after.write_text("2026-09-26T09:30:00|2026-09-26T09:30:07|Release 1.0\n", encoding="utf-8")
            release_files = {
                "before": exported.parent / "release-before.txt",
                "after": exported.parent / "release-after.txt",
                "stable_before": exported.parent / "stable-release-before.txt",
                "stable_after": exported.parent / "stable-release-after.txt",
            }
            release_files["before"].write_text("26.2.0|2026-09-26T09:30:00Z\n", encoding="utf-8")
            release_files["after"].write_text("26.2.0|2026-09-26T09:30:01Z\n", encoding="utf-8")
            release_files["stable_before"].write_text("26.2.0|2026-09-26T09:30:06Z\n", encoding="utf-8")
            release_files["stable_after"].write_text("26.2.0|2026-09-26T09:30:07Z\n", encoding="utf-8")
            summary_path = exported.parent / "summary.json"
            command = [
                sys.executable, str(VERIFIER), "100", str(source), str(exported), str(before), str(after),
                "--repo-root", str(source.parent), "--record-baseline",
                "--stability-export-dir", str(stable_export),
                "--stability-before-file", str(stable_before), "--stability-after-file", str(stable_after),
                "--target-apex-release-before", str(release_files["before"]),
                "--target-apex-release-after", str(release_files["after"]),
                "--stability-target-apex-release-before", str(release_files["stable_before"]),
                "--stability-target-apex-release-after", str(release_files["stable_after"]),
                "--summary-path", str(summary_path),
            ]

            result = subprocess.run(command, text=True, capture_output=True, check=False)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertIs(summary["sourceVerified"], True)
            self.assertEqual(summary["sourceProvenanceStatus"], "unknown")
            self.assertIsNone(summary["sourceApexRelease"])
            self.assertEqual(summary["targetApexRelease"], "26.2.0")

            marker = marker_path.read_bytes()
            release_files["stable_before"].write_text("26.2.1|2026-09-26T09:30:06Z\n", encoding="utf-8")
            release_files["stable_after"].write_text("26.2.1|2026-09-26T09:30:07Z\n", encoding="utf-8")
            result = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("target APEX release changed", result.stderr)
            self.assertEqual(marker_path.read_bytes(), marker)


if __name__ == "__main__":
    unittest.main()
