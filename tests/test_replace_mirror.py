import os
import re
import shutil
import subprocess
import tempfile
import unittest
import sys
from fake_sqlcl import BASH
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")


def plain(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return re.sub(r"\s*\n\s*\|?\s*", " ", text)


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
                    if shell == "bash":
                        # A refusal is 2, as team.ps1 reports the PowerShell helper's throw (README).
                        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
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


class OrdsMirrorTests(unittest.TestCase):
    """database/<SCHEMA>/ords is a mirror of its own: replaced alone, locked with its schema, never nested."""

    def make_repo(self, root: Path) -> tuple[Path, Path]:
        staged, mirror = MirrorSafetyTests.make_repo(self, root)
        ords = mirror / "ords"
        ords.mkdir()
        (ords / "schema.sql").write_text("old ords\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.test", "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "ords"], check=True)
        staged_ords = root / "scratch" / "ords-stage"
        staged_ords.mkdir()
        (staged_ords / "schema.sql").write_text("new ords\n", encoding="utf-8")
        return staged_ords, mirror

    def run_shells(self, root: Path, pairs: list[str], environment=None):
        yield "bash", subprocess.run([BASH, str(root / "scripts" / "replace_mirror.sh"), *pairs], cwd=root, env=environment, text=True, capture_output=True, check=False)
        if PWSH:
            yield "pwsh", subprocess.run([PWSH, "-NoProfile", "-File", str(root / "scripts" / "replace_mirror.ps1"), *pairs], cwd=root, env=environment, text=True, capture_output=True, check=False)

    def test_the_ords_folder_is_replaced_and_its_siblings_are_not_touched(self) -> None:
        for shell in ("bash", "pwsh"):
            if shell == "pwsh" and not PWSH:
                continue
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                staged, mirror = self.make_repo(root)
                pairs = [str(staged), "database/DEMO/ords"]
                if shell == "bash":
                    result = subprocess.run([BASH, str(root / "scripts" / "replace_mirror.sh"), *pairs], cwd=root, text=True, capture_output=True, check=False)
                else:
                    result = subprocess.run([PWSH, "-NoProfile", "-File", str(root / "scripts" / "replace_mirror.ps1"), *pairs], cwd=root, text=True, capture_output=True, check=False)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual("new ords\n", (mirror / "ords" / "schema.sql").read_text(encoding="utf-8"))
                self.assertEqual("old\n", (mirror / "tables" / "t.sql").read_text(encoding="utf-8"))
                self.assertEqual([], [path.name for path in (root / "scratch").glob(".mirror-backup*")])
                self.assertEqual([], [path.name for path in (root / "scratch" / ".mirror-locks").glob("*.lock")], "every lock is released")

    def test_a_first_ords_export_creates_the_folder_beside_the_existing_mirrors(self) -> None:
        for shell in ("bash", "pwsh"):
            if shell == "pwsh" and not PWSH:
                continue
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                staged, mirror = MirrorSafetyTests.make_repo(self, root)
                staged_ords = root / "scratch" / "ords-stage"
                staged_ords.mkdir()
                (staged_ords / "schema.sql").write_text("first ords\n", encoding="utf-8")
                pairs = [str(staged_ords), "database/DEMO/ords"]
                if shell == "bash":
                    result = subprocess.run([BASH, str(root / "scripts" / "replace_mirror.sh"), *pairs], cwd=root, text=True, capture_output=True, check=False)
                else:
                    result = subprocess.run([PWSH, "-NoProfile", "-File", str(root / "scripts" / "replace_mirror.ps1"), *pairs], cwd=root, text=True, capture_output=True, check=False)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual("first ords\n", (mirror / "ords" / "schema.sql").read_text(encoding="utf-8"))
                self.assertEqual("old\n", (mirror / "tables" / "t.sql").read_text(encoding="utf-8"))

    def test_a_dirty_ords_folder_is_refused_and_a_dirty_sibling_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged, mirror = self.make_repo(root)
            (mirror / "tables" / "t.sql").write_text("being edited\n", encoding="utf-8")
            for shell, result in self.run_shells(root, [str(staged), "database/DEMO/ords"]):
                with self.subTest(shell=shell, case="dirty sibling"):
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    (mirror / "ords" / "schema.sql").write_text("old ords\n", encoding="utf-8")
                    staged.mkdir(exist_ok=True)
                    (staged / "schema.sql").write_text("new ords\n", encoding="utf-8")
            (mirror / "ords" / "schema.sql").write_text("hand edit\n", encoding="utf-8")
            for shell, result in self.run_shells(root, [str(staged), "database/DEMO/ords"]):
                with self.subTest(shell=shell, case="dirty ords"):
                    self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertIn("dirty mirror", plain(result.stdout + result.stderr))
                    self.assertEqual("hand edit\n", (mirror / "ords" / "schema.sql").read_text(encoding="utf-8"))

    def test_other_three_part_database_destinations_stay_unapproved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged, mirror = self.make_repo(root)
            for destination in ("database/DEMO/tables", "database/DEMO/ORDS", "database/DEMO/ords/deeper", "database"):
                for shell, result in self.run_shells(root, [str(staged), destination]):
                    with self.subTest(shell=shell, destination=destination):
                        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                        self.assertIn("not an approved generated mirror", plain(result.stdout + result.stderr))
                        self.assertEqual("old\n", (mirror / "tables" / "t.sql").read_text(encoding="utf-8"))

    def test_a_mirror_and_a_folder_inside_it_cannot_be_replaced_in_one_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged_ords, mirror = self.make_repo(root)
            whole = root / "scratch" / "whole-stage"
            (whole / "tables").mkdir(parents=True)
            (whole / "tables" / "t.sql").write_text("whole\n", encoding="utf-8")
            pairs = [str(whole), "database/DEMO", str(staged_ords), "database/DEMO/ords"]
            for shell, result in self.run_shells(root, pairs):
                with self.subTest(shell=shell):
                    self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertIn("mirror destinations overlap", plain(result.stdout + result.stderr))
                    self.assertEqual("old\n", (mirror / "tables" / "t.sql").read_text(encoding="utf-8"))
                    self.assertEqual("old ords\n", (mirror / "ords" / "schema.sql").read_text(encoding="utf-8"))

    def test_a_held_schema_lock_blocks_an_ords_only_replacement(self) -> None:
        import hashlib
        import time

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staged, mirror = self.make_repo(root)
            locks = root / "scratch" / ".mirror-locks"
            locks.mkdir(parents=True)
            digest = hashlib.sha256(b"database/DEMO").hexdigest()[:16]
            # A fresh lock of the whole schema mirror, held by a live process of the PowerShell kind
            # (the Bash side cannot check its liveness and waits out the staleness window).
            (locks / f"{digest}.lock").write_text(f"version=1\nimpl=ps1\npid={os.getpid()}\nepoch={int(time.time())}\n", encoding="utf-8")
            for shell, result in self.run_shells(root, [str(staged), "database/DEMO/ords"]):
                with self.subTest(shell=shell):
                    self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertIn("another mirror replacement is already running for database/DEMO", plain(result.stdout + result.stderr))
                    self.assertEqual("old ords\n", (mirror / "ords" / "schema.sql").read_text(encoding="utf-8"))
                    self.assertTrue((locks / f"{digest}.lock").exists(), "a lock this call did not take must stay")


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
