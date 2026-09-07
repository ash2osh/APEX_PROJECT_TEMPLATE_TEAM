from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from teamlib.conflict_assistant import explain_conflict


class ConflictAssistantTests(unittest.TestCase):
    def test_explanation_names_shared_capture_and_questions_without_writing_source(self):
        with tempfile.TemporaryDirectory(prefix="team-conflict-") as directory:
            root = Path(directory)
            recovery = root / "recovery" / "r1"
            recovery.mkdir(parents=True)
            # The assistant accepts a compact fixture manifest so it can also
            # inspect retained production bundles without a target connection.
            (recovery / "capture.json").write_text(
                '{"version":1,"type":"capture","recovery_id":"r1","head_commit":"abc","diagnostics":{"conflicts":["pages/p1.apx"]},"base":{"pages/p1.apx":"title: old"},"source_base":{"pages/p1.apx":"title: source"},"head":{"pages/p1.apx":"title: HEAD title"},"mine":{"pages/p1.apx":"title: shared title"}}',
                encoding="utf-8",
            )
            briefing = explain_conflict("r1", root=root)
            self.assertIn("shared application", briefing.text.lower())
            self.assertTrue(briefing.questions)
            self.assertFalse(list(root.glob("apps/**")))

    def test_different_properties_are_explained_as_independent_without_selecting_values(self):
        with tempfile.TemporaryDirectory(prefix="team-conflict-independent-") as directory:
            root = Path(directory)
            recovery = root / "recovery" / "r2"
            recovery.mkdir(parents=True)
            (recovery / "capture.json").write_text(
                '{"version":1,"type":"capture","recovery_id":"r2","head_commit":"abc","diagnostics":{"conflicts":["pages/p1.apx"]},"base":{"pages/p1.apx":"title: old\\nlabel: old"},"source_base":{"pages/p1.apx":"title: old\\nlabel: old"},"head":{"pages/p1.apx":"title: committed\\nlabel: old"},"mine":{"pages/p1.apx":"title: old\\nlabel: shared"}}',
                encoding="utf-8",
            )
            briefing = explain_conflict("r2", root=root)
            self.assertFalse(briefing.questions)
            self.assertIn("independent", briefing.text.lower())


if __name__ == "__main__":
    unittest.main()
