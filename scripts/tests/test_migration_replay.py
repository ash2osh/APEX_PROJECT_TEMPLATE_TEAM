from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from teamlib.replay import ReplayError, ReplayReport, adopt_baseline, replay


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-replay-")
        self.root = Path(self.temp.name) / "migrations"
        self.root.mkdir()
        self.sql = self.root / "20260907T100000__alice__one.sql"
        self.sql.write_text("-- migration-version: 1\n-- target: tables\n-- destructive: false\n\nCREATE TABLE T(ID NUMBER);\n", encoding="utf-8")
        (self.root / "20260907T100000__alice__one.verify.sql").write_text("", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_replay_is_deterministic_and_reports_order(self):
        report = replay(self.root, {"status": "empty"})
        self.assertIsInstance(report, ReplayReport)
        self.assertEqual(report.order, ("20260907T100000__alice__one",))

    def test_adoption_requires_exact_candidate_and_live_evidence(self):
        evidence = Path(self.temp.name) / "evidence.json"
        evidence.write_text('{"version":1,"migration_id":"m1","candidate_digest":"x","live_digest":"x","verified":true}\n', encoding="utf-8")
        self.assertTrue(adopt_baseline("m1", evidence, {"target_role": "developer"}))
        evidence.write_text('{"version":1,"migration_id":"m1","candidate_digest":"x","live_digest":"y","verified":true}\n', encoding="utf-8")
        with self.assertRaises(ReplayError):
            adopt_baseline("m1", evidence, {"target_role": "developer"})


if __name__ == "__main__":
    unittest.main()
