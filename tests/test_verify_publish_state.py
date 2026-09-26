import json
import subprocess
import tempfile
import unittest
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
            json.dumps({"applicationId": 100, "builderLastUpdatedOn": "2026-09-26T08:00:00"}) + "\n",
            encoding="utf-8",
        )
        before = root / "before.txt"
        after = root / "after.txt"
        before.write_text("2026-09-26T09:30:00|2026-09-26T09:30:03\n", encoding="utf-8")
        after.write_text("2026-09-26T09:30:00|2026-09-26T09:30:04\n", encoding="utf-8")
        return source, exported, before, after, marker

    def run_verifier(self, source: Path, exported: Path, before: Path, after: Path):
        return subprocess.run(
            [
                "python3", str(VERIFIER), "100", str(source), str(exported), str(before), str(after),
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

    def test_source_mismatch_does_not_advance_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            (exported / "application.apx").write_bytes(b"application { changed }\n")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("do not match", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

    def test_intervening_builder_revision_does_not_advance_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            after.write_text("2026-09-26T09:31:00|2026-09-26T09:31:02\n", encoding="utf-8")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("changed while", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

    def test_ambiguous_one_second_revision_does_not_advance_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            before.write_text("2026-09-26T09:30:03|2026-09-26T09:30:03\n", encoding="utf-8")
            after.write_text("2026-09-26T09:30:03|2026-09-26T09:30:04\n", encoding="utf-8")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("same database second", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

    def test_future_revision_does_not_advance_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, exported, before, after, marker_path = self.make_fixture(Path(temporary))
            original_marker = marker_path.read_bytes()
            before.write_text("2026-09-26T09:30:08|2026-09-26T09:30:06\n", encoding="utf-8")
            after.write_text("2026-09-26T09:30:08|2026-09-26T09:30:07\n", encoding="utf-8")

            result = self.run_verifier(source, exported, before, after)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("later than the database-time observation", result.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)


if __name__ == "__main__":
    unittest.main()
