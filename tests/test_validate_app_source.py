import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_app_source.py"


class ValidateAppSourceTests(unittest.TestCase):
    def run_validator(self, repo_root: Path, source: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(VALIDATOR), str(repo_root), str(source)],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_regular_application_tree_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "apps" / "DEMO" / "100"
            (app / ".apex").mkdir(parents=True)
            (app / ".apex" / "apexlang.json").write_text("{}\n", encoding="utf-8")

            result = self.run_validator(root, app)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_symlinked_application_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "checkout"
            root.mkdir()
            outside = Path(temporary) / "external-app"
            outside.mkdir()
            (outside / "application.apx").write_text("application {}\n", encoding="utf-8")
            app = root / "apps" / "DEMO" / "100"
            app.parent.mkdir(parents=True)
            try:
                app.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            result = self.run_validator(root, app)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symbolic links or reparse points", result.stderr)

    def test_nested_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "checkout"
            app = root / "apps" / "DEMO" / "100"
            (app / ".apex").mkdir(parents=True)
            outside = Path(temporary) / "external.apx"
            outside.write_text("application {}\n", encoding="utf-8")
            try:
                (app / "application.apx").symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            result = self.run_validator(root, app)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symbolic links or reparse points", result.stderr)

    def test_path_outside_checkout_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "checkout"
            root.mkdir()
            outside = Path(temporary) / "external-app"
            outside.mkdir()

            result = self.run_validator(root, outside)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("outside the repository", result.stderr)


if __name__ == "__main__":
    unittest.main()
