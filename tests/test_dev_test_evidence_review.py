"""Controller regressions for durable DEV run evidence and truthful reports."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.dev_test_evidence import append_event, assert_inputs_unchanged, create_run, freeze_inputs
from scripts.dev_test_report import aggregate_exit, new_report, render_report, save_report, validate_report


def completed_report(run=None):
    report = new_report(run, "run", "suite", ["only"], "project")
    report["finishedAt"] = "2026-10-16T10:00:00Z"
    report["target"]["observed"] = {
        "session_user": "APP", "current_schema": "APP", "db_name": "DEV",
        "db_unique_name": "DEV", "service_name": "dev", "container_id": "3",
        "container_name": "DEV", "edition": "ORA$BASE", "database_version": "23.26",
    }
    report["coverage"] = {"complete": True, "eligible": True}
    report["scenarios"] = [{
        "id": "only", "status": "PASSED", "eligible": True,
        "steps": [], "checks": [{"id": "check", "value": 1, "expected": 1, "passed": True}],
        "observations": [], "effects": {},
    }]
    return report


class EvidenceReviewTests(unittest.TestCase):
    def test_report_rejects_invalid_nested_field_types(self):
        cases = [
            ("coverage", "complete", 1), ("source", "dirty", "false"),
            ("selection", "origin", "unknown"), ("inventory", "rows", "not rows"),
        ]
        for section, key, value in cases:
            with self.subTest(section=section, key=key):
                report = completed_report()
                report[section][key] = value
                with self.assertRaises(ValueError):
                    validate_report(report)
        report = completed_report()
        report["scenarios"][0]["eligible"] = "yes"
        with self.assertRaises(ValueError):
            validate_report(report)

    def test_incomplete_coverage_or_identity_is_not_success(self):
        for mutation in ("coverage", "identity", "duplicate"):
            with self.subTest(mutation=mutation):
                report = completed_report()
                if mutation == "coverage":
                    report["coverage"]["complete"] = False
                elif mutation == "identity":
                    report["target"]["observed"] = {}
                else:
                    report["scenarios"].append(dict(report["scenarios"][0]))
                self.assertEqual(aggregate_exit(report), 2)

    def test_structured_diagnostics_render_and_preserve_interrupt(self):
        report = completed_report()
        report["diagnostics"] = [{"code": "NOTE", "message": "Fixtures retained"}]
        self.assertIn("Fixtures retained", render_report(report, "text"))
        self.assertIn("Fixtures retained", render_report(report, "markdown"))
        report["finishedAt"] = None
        report["diagnostics"] = [{"code": "INTERRUPTED", "writeStarted": False}]
        self.assertEqual(aggregate_exit(report), 130)

    def test_requested_unsupported_cleanup_is_incomplete(self):
        report = completed_report()
        report["cleanup"] = {"status": "UNSUPPORTED", "requested": True}
        self.assertEqual(aggregate_exit(report), 2)

    def test_report_cannot_be_saved_under_another_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            first = create_run(Path(temporary))
            second = create_run(Path(temporary))
            with self.assertRaises(ValueError):
                save_report(second, completed_report(first))
            self.assertFalse((second.run_dir / "report.json").exists())

    def test_package_import_and_report_save_work_without_scripts_path_injection(self):
        root = Path(__file__).resolve().parents[1]
        code = (
            "import tempfile; from pathlib import Path; "
            "from scripts.dev_test_evidence import create_run; "
            "from scripts.dev_test_report import new_report,save_report; "
            "t=tempfile.TemporaryDirectory(); r=create_run(Path(t.name)); "
            "save_report(r,new_report(r,'plan','suite',['only'],'project')); t.cleanup()"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=root,
                                env={**os.environ, "PYTHONPATH": str(root)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_or_malformed_frozen_snapshot_is_refused(self):
        for mutation in ("snapshot", "manifest"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "input.sql"
                source.write_text("SELECT 1 FROM DUAL;\n")
                run = create_run(root)
                freeze_inputs(run, [source])
                if mutation == "snapshot":
                    for snapshot in run.snapshots_dir.iterdir():
                        snapshot.unlink()
                else:
                    run._manifest_path.write_text(json.dumps({}))
                with self.assertRaises(ValueError):
                    assert_inputs_unchanged(run)

    def test_corrupt_intent_cannot_allow_refreeze(self):
        for corrupted in ("{", "null", "{}"):
            with self.subTest(value=corrupted), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "input.sql"
                source.write_text("SELECT 1 FROM DUAL;\n")
                run = create_run(root)
                freeze_inputs(run, [source])
                append_event(run, {"type": "intent"}).write_text(corrupted)
                with self.assertRaises(ValueError):
                    freeze_inputs(run, [source])

    def test_event_writer_refuses_redirected_directory(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as outside:
            run = create_run(Path(temporary))
            run.events_dir.rmdir()
            try:
                run.events_dir.symlink_to(Path(outside), target_is_directory=True)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaises(ValueError):
                append_event(run, {"type": "intent"})
            self.assertEqual(list(Path(outside).iterdir()), [])
