from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from unittest.mock import patch
import subprocess
import unittest

from teamlib.apex_validate import ValidationReport, validate_apexlang_tree


class ApexValidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.valid_tree = {
            "application/set_environment.sql": b"prompt --application/set_environment\n",
            "application/create_application.sql": b"prompt --application/create_application\n",
        }

    @patch("subprocess.run")
    def test_validation_successful(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["sql", "-L", "/nolog"],
            returncode=0,
            stdout="Validating file: application/set_environment.sql\nValidation successful.\n",
            stderr="",
        )
        report = validate_apexlang_tree(self.valid_tree)
        self.assertIsInstance(report, ValidationReport)
        self.assertTrue(report.success)
        self.assertEqual(report.status, "SUCCESS")
        self.assertIn("Validation successful", report.log)
        self.assertEqual(report.errors, ())

    @patch("subprocess.run")
    def test_compiler_errors_detected_and_refused(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["sql", "-L", "/nolog"],
            returncode=1,
            stdout="Validating file: application/set_environment.sql\nError at line 12: syntax error\nValidation failed.\n",
            stderr="",
        )
        report = validate_apexlang_tree(self.valid_tree)
        self.assertFalse(report.success)
        self.assertEqual(report.status, "FAILED")
        self.assertTrue(len(report.errors) > 0)
        self.assertIn("syntax error", " ".join(report.errors).lower())

    @patch("subprocess.run")
    def test_warnings_without_success_marker_refuses(self, mock_run):
        # Even with exit code 0, missing 'Validation successful' must refuse
        mock_run.return_value = subprocess.CompletedProcess(
            args=["sql", "-L", "/nolog"],
            returncode=0,
            stdout="Validating file: application/set_environment.sql\nWarning: deprecated syntax\nDone.\n",
            stderr="",
        )
        report = validate_apexlang_tree(self.valid_tree)
        self.assertFalse(report.success)
        self.assertEqual(report.status, "FAILED")
        self.assertIn("validation successful", " ".join(report.errors).lower())

    @patch("subprocess.run")
    def test_exit_zero_failure_when_no_apexlang_files(self, mock_run):
        # SQLcl returns exit code 0 when input dir doesn't contain APEXlang files
        mock_run.return_value = subprocess.CompletedProcess(
            args=["sql", "-L", "/nolog"],
            returncode=0,
            stdout="Directory input does not contain APEXlang files.\n",
            stderr="",
        )
        report = validate_apexlang_tree(self.valid_tree)
        self.assertFalse(report.success)
        self.assertEqual(report.status, "FAILED")
        self.assertTrue(len(report.errors) > 0)

    @patch("subprocess.run")
    def test_temp_directory_cleaned_up(self, mock_run):
        observed_dirs = []

        def fake_run(args, **kwargs):
            input_text = kwargs.get("input", "")
            # extract path from "apex validate -input <dir>"
            for line in input_text.splitlines():
                if "-input" in line:
                    parts = line.split("-input")
                    if len(parts) > 1:
                        target_dir = Path(parts[1].strip())
                        observed_dirs.append(target_dir)
                        # Check that files were written inside this dir
                        self.assertTrue(target_dir.is_dir())
                        self.assertTrue((target_dir / "application" / "set_environment.sql").is_file())
            return subprocess.CompletedProcess(
                args=args, returncode=0,
                stdout="Validation successful.\n", stderr="",
            )

        mock_run.side_effect = fake_run
        report = validate_apexlang_tree(self.valid_tree)
        self.assertTrue(report.success)
        self.assertEqual(len(observed_dirs), 1)
        # Verify the temp directory was deleted after the with block
        self.assertFalse(observed_dirs[0].exists())

    def test_empty_tree_refuses_without_running_sqlcl(self):
        report = validate_apexlang_tree({})
        self.assertFalse(report.success)
        self.assertEqual(report.status, "FAILED")
        self.assertIn("empty", " ".join(report.errors).lower())


if __name__ == "__main__":
    unittest.main()
