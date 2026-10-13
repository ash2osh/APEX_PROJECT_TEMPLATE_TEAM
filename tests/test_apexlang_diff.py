import tempfile
import unittest
from pathlib import Path

from scripts.apexlang_diff import collect_source_mismatches


class ApexlangDiffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.exported = self.root / "exported"
        self.source.mkdir()
        self.exported.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def write(root: Path, relative: str, content: bytes):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def seed_required(self):
        for root in (self.source, self.exported):
            self.write(root, "application.apx", b"app\n")
            self.write(root, ".apex/apexlang.json", b"{}\n")

    def test_reports_every_byte_mismatch_without_source_text(self):
        self.seed_required()
        self.write(self.source, "application.apx", b"secret-source-line\n")
        self.write(self.exported, "application.apx", b"different-source-line\n")
        self.write(self.source, "pages/page-1.apx", b"first-secret\n")
        self.write(self.exported, "pages/page-1.apx", b"second-secret\n")

        mismatches = collect_source_mismatches(self.source, self.exported)

        self.assertEqual(
            [(item.path, item.kind, item.first_differing_line) for item in mismatches],
            [("application.apx", "bytes", 1), ("pages/page-1.apx", "bytes", 1)],
        )
        self.assertNotIn("secret", repr(mismatches))

    def test_reports_required_missing_and_unexpected_files(self):
        self.write(self.source, "application.apx", b"app\n")
        self.write(self.exported, ".apex/apexlang.json", b"{}\n")
        self.write(self.exported, "extra.apx", b"extra\n")

        mismatches = collect_source_mismatches(self.source, self.exported)

        self.assertEqual(
            [(item.path, item.kind) for item in mismatches],
            [
                (".apex/apexlang.json", "required-missing"),
                (".apex/apexlang.json", "unexpected"),
                ("application.apx", "missing"),
                ("extra.apx", "unexpected"),
            ],
        )

    def test_ignores_operational_metadata_and_empty_deinstall_script(self):
        self.seed_required()
        self.write(self.source, "apex-team-export.json", b"source marker\n")
        self.write(self.exported, "apex-team-export.json", b"export marker\n")
        self.write(self.source, "deployments/dev.json", b"source descriptor\n")
        self.write(self.exported, "deployments/dev.json", b"export descriptor\n")
        self.write(self.source, "supporting-objects/deinstall-script.sql", b"")

        self.assertEqual(collect_source_mismatches(self.source, self.exported), ())


if __name__ == "__main__":
    unittest.main()
