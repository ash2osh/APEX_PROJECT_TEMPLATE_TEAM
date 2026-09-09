from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
import tempfile
import unittest

from teamlib.conflict_assistant import ConflictAssistantError, explain_conflict, write_candidate


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

    def test_candidate_output_rejects_parent_traversal_in_scratch_root(self):
        with tempfile.TemporaryDirectory(prefix="team-conflict-output-") as directory:
            root = Path(directory)
            recovery = root / "recovery" / "r3"
            recovery.mkdir(parents=True)
            (recovery / "capture.json").write_text(
                '{"version":1,"diagnostics":{"conflicts":["pages/p1.apx"]},"base":{"pages/p1.apx":"title: old"},"source_base":{"pages/p1.apx":"title: old"},"head":{"pages/p1.apx":"title: head"},"mine":{"pages/p1.apx":"title: shared"}}',
                encoding="utf-8",
            )
            briefing = explain_conflict("r3", root=root)
            with self.assertRaises(ConflictAssistantError):
                write_candidate(briefing, {"pages/p1.apx": "title: resolved"}, out_root=root / "scratch" / ".." / "outside")


class SingleAdapterTests(unittest.TestCase):
    def test_explain_conflict_has_exactly_one_cli_adapter(self):
        root = Path(__file__).resolve().parents[2]
        self.assertFalse(
            (root / "scripts/teamlib/conflict_assistant_cli.py").exists(),
            "conflict_assistant_cli.py duplicates conflict_assistant.main",
        )
        from teamlib import conflict_assistant
        self.assertTrue(callable(getattr(conflict_assistant, "main", None)))

    def test_b64_sql_has_no_silently_truncating_clob_branch(self):
        import inspect
        from teamlib.migration_store import _b64_sql
        self.assertNotIn("clob", inspect.signature(_b64_sql).parameters)
        self.assertNotIn("900", inspect.getsource(_b64_sql))


if __name__ == "__main__":
    unittest.main()
