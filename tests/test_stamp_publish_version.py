import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STAMPER = ROOT / "scripts" / "stamp_publish_version.py"
HEADER = "app SAMPLE (\n    name: Sample\n"
BODY = "    logo {\n        type: text\n    }\n)\n"


class StampPublishVersionTests(unittest.TestCase):
    def stamp(self, source: str, developer: str = "ASHARIF", publish_date: str = "2026-09-26"):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "application.apx"
            path.write_text(source, encoding="utf-8")
            result = subprocess.run(
                ["python3", str(STAMPER), str(path), developer, "--date", publish_date],
                text=True,
                capture_output=True,
                check=False,
            )
            return result, path.read_text(encoding="utf-8")

    def test_default_version_is_written_with_first_tag_after_name(self) -> None:
        result, source = self.stamp(HEADER + BODY)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "Release 1.0 [ASHARIF-2026-09-26r001]")
        self.assertEqual(
            source,
            HEADER + '    version: "Release 1.0 [ASHARIF-2026-09-26r001]"\n' + BODY,
        )

    def test_same_developer_and_date_increments_counter(self) -> None:
        result, source = self.stamp(
            HEADER + '    version: "V2 Powered By xxx [ASHARIF-2026-09-26r009]"\n' + BODY
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('    version: "V2 Powered By xxx [ASHARIF-2026-09-26r010]"\n', source)

    def test_another_developer_continues_the_counter_for_the_date(self) -> None:
        # Restarting per developer could repeat an earlier tag (A-r001, B-r001,
        # A-r001), which would hide the imports in between from the guard.
        result, source = self.stamp(HEADER + '    version: "V2 [ASHARIF-2026-09-26r004]"\n' + BODY, "BOB")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('    version: "V2 [BOB-2026-09-26r005]"\n', source)

    def test_later_date_restarts_counter(self) -> None:
        result, source = self.stamp(
            HEADER + '    version: "V2 [ASHARIF-2026-09-26r004]"\n' + BODY, "BOB", "2026-09-27"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('    version: "V2 [BOB-2026-09-27r001]"\n', source)

    def test_earlier_workstation_date_never_moves_the_tag_date_back(self) -> None:
        # A teammate behind in time zone must not restart a date already used.
        result, source = self.stamp(
            HEADER + '    version: "V2 [ASHARIF-2026-09-27r002]"\n' + BODY, "BOB", "2026-09-26"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('    version: "V2 [BOB-2026-09-27r003]"\n', source)

    def test_bare_human_version_keeps_its_text(self) -> None:
        result, source = self.stamp(HEADER + "    version: V2 Powered By xxx\n" + BODY)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('    version: "V2 Powered By xxx [ASHARIF-2026-09-26r001]"\n', source)

    def test_quotes_and_backslashes_use_apexlang_escapes(self) -> None:
        result, source = self.stamp(HEADER + '    version: "V2 \\"beta\\" C:\\\\x [T]"\n' + BODY)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'V2 "beta" C:\\x [T] [ASHARIF-2026-09-26r001]')
        self.assertIn('    version: "V2 \\"beta\\" C:\\\\x [T] [ASHARIF-2026-09-26r001]"\n', source)

    def test_nested_version_attributes_are_not_the_application_version(self) -> None:
        result, source = self.stamp(HEADER + "    pwa {\n        version: 3\n    }\n" + BODY)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("        version: 3\n", source)
        self.assertIn('    version: "Release 1.0 [ASHARIF-2026-09-26r001]"\n', source)

    def test_invalid_input_fails_without_changing_the_file(self) -> None:
        for source, developer in (
            (HEADER + "    version: A\n    version: B\n" + BODY, "ASHARIF"),
            ("app SAMPLE (\n" + BODY, "ASHARIF"),
            (HEADER + '    version: "bad \\n escape"\n' + BODY, "ASHARIF"),
            (HEADER + "    version: " + "x" * 250 + "\n" + BODY, "ASHARIF"),
            (HEADER + BODY, "a-sharif"),
        ):
            with self.subTest(source=source[:40], developer=developer):
                result, after = self.stamp(source, developer)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(after, source)


if __name__ == "__main__":
    unittest.main()
