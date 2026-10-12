import json
import tempfile
import unittest
from pathlib import Path

from scripts.source_provenance import (
    canonical_source_digest,
    make_source_provenance,
    read_source_provenance,
)


class SourceProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.source = Path(self.temporary.name) / "app"
        (self.source / ".apex").mkdir(parents=True)
        (self.source / "application.apx").write_bytes(b"app\n")
        (self.source / ".apex/apexlang.json").write_bytes(b"{}\n")

    def tearDown(self):
        self.temporary.cleanup()

    def write_marker(self, provenance):
        (self.source / "apex-team-export.json").write_text(
            json.dumps({"schemaVersion": 2, "sourceProvenance": provenance}) + "\n",
            encoding="utf-8",
        )

    def test_export_marker_verifies_then_detects_added_edited_and_deleted_source(self):
        marker = make_source_provenance(
            self.source, "26.2.0", "2026-10-12T04:00:00Z"
        )
        self.write_marker(marker)
        self.assertEqual(read_source_provenance(self.source).status, "verified")

        (self.source / "application.apx").write_bytes(b"changed\n")
        self.assertEqual(read_source_provenance(self.source).status, "stale")

        (self.source / "application.apx").write_bytes(b"app\n")
        (self.source / "page.apx").write_bytes(b"new\n")
        self.assertEqual(read_source_provenance(self.source).status, "stale")

        (self.source / "page.apx").unlink()
        (self.source / ".apex/apexlang.json").unlink()
        self.assertEqual(read_source_provenance(self.source).status, "stale")

    def test_operational_marker_and_deployment_descriptor_are_outside_canonical_digest(self):
        before = canonical_source_digest(self.source)
        (self.source / "apex-team-export.json").write_text("{}\n", encoding="utf-8")
        (self.source / "deployments").mkdir()
        (self.source / "deployments/dev.json").write_text("changed\n", encoding="utf-8")

        self.assertEqual(canonical_source_digest(self.source), before)

    def test_legacy_or_malformed_marker_is_unknown_and_release_is_validated(self):
        self.assertEqual(read_source_provenance(self.source).status, "unknown")
        self.write_marker({"schemaVersion": 1, "sourceRelease": "26.2.0"})
        self.assertEqual(read_source_provenance(self.source).status, "unknown")
        with self.assertRaises(ValueError):
            make_source_provenance(self.source, "26.1.0", "2026-10-12T04:00:00Z")
        with self.assertRaises(ValueError):
            make_source_provenance(self.source, "26.2.0", "2026-10-12T04:00:00+02:00")


if __name__ == "__main__":
    unittest.main()
