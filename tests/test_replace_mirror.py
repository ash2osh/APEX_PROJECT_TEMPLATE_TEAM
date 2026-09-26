import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")


@unittest.skipUnless(PWSH, "PowerShell Core is not installed")
class ReplaceMirrorTests(unittest.TestCase):
    def make_checkout(self, root: Path) -> tuple[Path, Path, Path]:
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        replace_script = scripts / "replace_mirror.ps1"
        shutil.copy2(ROOT / "scripts" / "replace_mirror.ps1", replace_script)
        scratch = root / "scratch"
        scratch.mkdir()
        staged = scratch / "stage"
        staged.mkdir()
        (staged / "application.apx").write_text("replacement\n", encoding="utf-8")
        return replace_script, scratch, staged

    def test_destination_symlink_ancestor_cannot_replace_external_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            outside = Path(temporary) / "outside"
            external_app = outside / "100"
            external_app.mkdir(parents=True)
            original = external_app / "original.apx"
            original.write_text("keep me\n", encoding="utf-8")
            apps = root / "apps"
            apps.mkdir()
            try:
                os.symlink(outside, apps / "DEMO", target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")

            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "Mirror Test"], check=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "mirror@example.test"], check=True)
            subprocess.run(["git", "-C", str(root), "add", "apps/DEMO"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-qm", "seed symlink"], check=True)
            replace_script, _, staged = self.make_checkout(root)

            result = subprocess.run(
                [PWSH, "-NoProfile", "-File", str(replace_script), str(staged), "apps/DEMO/100"],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(original.exists(), "the external original must not be moved or deleted")
            self.assertEqual(original.read_text(encoding="utf-8"), "keep me\n")
            self.assertTrue((staged / "application.apx").exists())

    def test_staged_root_symlink_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            scripts = root / "scripts"
            scripts.mkdir()
            replace_script = scripts / "replace_mirror.ps1"
            shutil.copy2(ROOT / "scripts" / "replace_mirror.ps1", replace_script)
            scratch = root / "scratch"
            scratch.mkdir()
            outside = Path(temporary) / "outside-stage"
            outside.mkdir()
            payload = outside / "application.apx"
            payload.write_text("keep staging\n", encoding="utf-8")
            try:
                os.symlink(outside, scratch / "staged-link", target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "README.md").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Mirror Test", "-c",
                            "user.email=mirror@example.test", "commit", "-qm", "seed"], check=True)

            result = subprocess.run(
                [PWSH, "-NoProfile", "-File", str(replace_script), str(scratch / "staged-link"), "apps/DEMO/100"],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(payload.read_text(encoding="utf-8"), "keep staging\n")
            self.assertTrue((scratch / "staged-link").is_symlink())


if __name__ == "__main__":
    unittest.main()
