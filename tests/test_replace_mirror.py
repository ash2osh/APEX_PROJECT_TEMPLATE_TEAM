import os
import shutil
import subprocess
import tempfile
import unittest
import sys
from fake_sqlcl import BASH
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


class MirrorSafetyTests(unittest.TestCase):
    """Refusals shared by the Bash and PowerShell mirror replacements."""

    def make_repo(self, root: Path) -> tuple[Path, Path]:
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        for name in ("replace_mirror.sh", "replace_mirror.ps1"):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        (root / ".gitignore").write_text("scratch/\n*.local\napps/**/apex-team-export.json\n", encoding="utf-8")
        mirror = root / "database" / "DEMO"
        (mirror / "tables").mkdir(parents=True)
        (mirror / "tables" / "t.sql").write_text("old\n", encoding="utf-8")
        app = root / "apps" / "DEMO" / "100"
        app.mkdir(parents=True)
        (app / "application.apx").write_text("old\n", encoding="utf-8")
        (app / "apex-team-export.json").write_text("{}\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.test", "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "seed"], check=True)
        staged = root / "scratch" / "stage"
        (staged / "tables").mkdir(parents=True)
        (staged / "tables" / "t.sql").write_text("new\n", encoding="utf-8")
        return staged, mirror

    def commands(self, root: Path, staged: Path, destination: str, environment=None):
        yield "bash", subprocess.run(
            [BASH, str(root / "scripts" / "replace_mirror.sh"), str(staged), destination],
            cwd=root, env=environment, text=True, capture_output=True, check=False,
        )
        if PWSH:
            yield "pwsh", subprocess.run(
                [PWSH, "-NoProfile", "-File", str(root / "scripts" / "replace_mirror.ps1"), str(staged), destination],
                cwd=root, env=environment, text=True, capture_output=True, check=False,
            )

    def test_ignored_file_in_mirror_is_not_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged, mirror = self.make_repo(root)
            sidecar = mirror / "notes.local"
            sidecar.write_text("keep me\n", encoding="utf-8")
            for shell, result in self.commands(root, staged, "database/DEMO"):
                with self.subTest(shell=shell):
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("ignored local files", result.stdout + result.stderr)
                    self.assertEqual(sidecar.read_text(encoding="utf-8"), "keep me\n")
                    self.assertEqual((mirror / "tables" / "t.sql").read_text(encoding="utf-8"), "old\n")

    def test_regenerated_export_marker_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.make_repo(root)
            staged = root / "scratch" / "app-stage"
            staged.mkdir(parents=True)
            (staged / "application.apx").write_text("new\n", encoding="utf-8")
            result = subprocess.run(
                [BASH, str(root / "scripts" / "replace_mirror.sh"), str(staged), "apps/DEMO/100"],
                cwd=root, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((root / "apps/DEMO/100/application.apx").read_text(encoding="utf-8"), "new\n")

    def test_short_lock_window_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged, mirror = self.make_repo(root)
            environment = {**os.environ, "MIRROR_LOCK_STALE_SECONDS": "0"}
            for shell, result in self.commands(root, staged, "database/DEMO", environment):
                with self.subTest(shell=shell):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("at least 60", result.stdout + result.stderr)
                    self.assertEqual((mirror / "tables" / "t.sql").read_text(encoding="utf-8"), "old\n")


    def test_failure_on_the_second_mirror_rolls_back_the_first(self) -> None:
        if os.name == "nt":
            self.skipTest("a read-only directory does not stop writes on Windows, and there is no geteuid")
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        for shell in ("bash", "pwsh"):
            if shell == "pwsh" and not PWSH:
                continue
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                staged_database, mirror = self.make_repo(root)
                app = root / "apps" / "DEMO" / "100"
                staged_app = root / "scratch" / "app-stage"
                staged_app.mkdir()
                (staged_app / "application.apx").write_text("new\n", encoding="utf-8")
                # The app installs first; database/ is read-only, so the second
                # pair cannot move its old mirror aside and the first must roll back.
                database = root / "database"
                database.chmod(0o500)
                try:
                    pairs = [str(staged_app), "apps/DEMO/100", str(staged_database), "database/DEMO"]
                    if shell == "bash":
                        command = [BASH, str(root / "scripts" / "replace_mirror.sh"), *pairs]
                    else:
                        command = [PWSH, "-NoProfile", "-File", str(root / "scripts" / "replace_mirror.ps1"), *pairs]
                    result = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)
                finally:
                    database.chmod(0o700)
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertEqual((app / "application.apx").read_text(encoding="utf-8"), "old\n", output)
                self.assertEqual((staged_app / "application.apx").read_text(encoding="utf-8"), "new\n", output)
                self.assertEqual((mirror / "tables" / "t.sql").read_text(encoding="utf-8"), "old\n", output)
                self.assertNotIn("INCOMPLETE", output)


class PreserveDeploymentsTests(unittest.TestCase):
    def test_symlinked_deployments_directory_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            (outside / "dev.json").write_text("{}\n", encoding="utf-8")
            existing = root / "existing"
            existing.mkdir()
            try:
                os.symlink(outside, existing / "deployments", target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")
            staged = root / "staged"
            staged.mkdir()
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "preserve_deployments.py"), str(existing), str(staged)],
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not be a symbolic link", result.stderr)
            self.assertFalse((staged / "deployments").exists())
