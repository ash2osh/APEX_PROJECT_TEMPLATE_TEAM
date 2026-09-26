import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from scripts import upgrade_template as upgrade_engine


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "scripts" / "upgrade_template.py"
MANIFEST = {
    "schemaVersion": 1,
    "upstream": "unused",
    "templateOwned": ["AGENTS.md", "scripts/**", "template-manifest.json"],
    "projectOwned": ["AGENTS.project.md"],
    "templateOnly": ["docs/**"],
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def init_repo(path: Path) -> None:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")


def write(root: Path, files: dict[str, str]) -> None:
    for relative, text in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def commit_all(root: Path, message: str) -> None:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)


class UpgradeTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.template = base / "template"
        self.project = base / "project"
        init_repo(self.template)
        write(
            self.template,
            {
                "template-manifest.json": json.dumps(MANIFEST),
                "AGENTS.md": "rules v1\n",
                "AGENTS.project.md": "<!-- placeholder -->\n",
                "scripts/tool.sh": "echo v1\n",
                "scripts/old.sh": "echo old\n",
                "docs/plan.md": "template only\n",
            },
        )
        commit_all(self.template, "v1")
        # A project created from v1 by copying files (no shared history).
        init_repo(self.project)
        write(
            self.project,
            {
                "AGENTS.md": "rules v1\n",
                "AGENTS.project.md": "our project rules\n",
                "scripts/tool.sh": "echo v1\n",
                "scripts/old.sh": "echo old\n",
                "apps/DEMO/100/application.apx": "app X ()\n",
            },
        )
        commit_all(self.project, "created from template")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def upgrade(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(ENGINE), "--project-root", str(self.project), "--source", str(self.template), *extra],
            text=True,
            capture_output=True,
            check=False,
        )

    def release_v2(self, files: dict[str, str], removed: tuple[str, ...] = ()) -> None:
        write(self.template, files)
        for relative in removed:
            (self.template / relative).unlink()
        commit_all(self.template, "v2")

    def read(self, relative: str) -> str:
        return (self.project / relative).read_text(encoding="utf-8")

    def lock(self) -> dict:
        return json.loads(self.read(".template-lock.json"))

    def adopt(self) -> None:
        # First upgrade of an identical copy: everything UNCHANGED, lock written.
        result = self.upgrade()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commit_all(self.project, "adopt template lock")

    def test_first_upgrade_of_identical_copy_writes_lock_without_changes(self) -> None:
        result = self.upgrade()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UNCHANGED AGENTS.md", result.stdout)
        self.assertEqual(self.lock()["commit"], git(self.template, "rev-parse", "HEAD"))
        self.assertIn("scripts/tool.sh", self.lock()["files"])
        self.assertNotIn("AGENTS.project.md", self.lock()["files"])

    def test_unmodified_template_files_are_updated_and_new_files_created(self) -> None:
        self.adopt()
        self.release_v2({"scripts/tool.sh": "echo v2\n", "scripts/new.sh": "echo new\n"})

        result = self.upgrade()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UPDATE scripts/tool.sh", result.stdout)
        self.assertIn("CREATE scripts/new.sh", result.stdout)
        self.assertEqual(self.read("scripts/tool.sh"), "echo v2\n")
        self.assertEqual(self.read("scripts/new.sh"), "echo new\n")

    def test_filesystem_failure_rolls_back_prior_updates_and_lock(self) -> None:
        self.adopt()
        original_lock = self.read(".template-lock.json")
        self.release_v2({"AGENTS.md": "rules v2\n", "scripts/tool.sh": "echo v2\n"})
        actions = [
            upgrade_engine.Action("UPDATE", "AGENTS.md"),
            upgrade_engine.Action("UPDATE", "scripts/tool.sh"),
        ]
        real_replace = os.replace
        calls = 0

        def fail_during_second_file(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
            nonlocal calls
            calls += 1
            if calls == 4:
                raise OSError("injected filesystem failure")
            real_replace(source, target)

        with patch("scripts.upgrade_template.os.replace", side_effect=fail_during_second_file):
            with self.assertRaisesRegex(upgrade_engine.UpgradeError, "filesystem update failed"):
                upgrade_engine.apply_actions(
                    self.project,
                    self.template,
                    actions,
                    str(self.template),
                    git(self.template, "rev-parse", "HEAD"),
                    {"AGENTS.md": "a" * 64, "scripts/tool.sh": "b" * 64},
                )

        self.assertEqual(self.read("AGENTS.md"), "rules v1\n")
        self.assertEqual(self.read("scripts/tool.sh"), "echo v1\n")
        self.assertEqual(self.read(".template-lock.json"), original_lock)
        self.assertEqual(git(self.project, "status", "--porcelain"), "")

    def test_locally_modified_file_changed_upstream_is_a_conflict(self) -> None:
        self.adopt()
        write(self.project, {"AGENTS.md": "rules v1 plus ours\n"})
        commit_all(self.project, "customize")
        self.release_v2({"AGENTS.md": "rules v2\n"})

        result = self.upgrade()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("CONFLICT AGENTS.md", result.stdout)
        self.assertEqual(self.read("AGENTS.md"), "rules v1 plus ours\n")
        self.assertEqual(self.read("AGENTS.md.template-new"), "rules v2\n")

    def test_resolved_conflict_is_not_reported_again(self) -> None:
        self.adopt()
        write(self.project, {"AGENTS.md": "rules v1 plus ours\n"})
        commit_all(self.project, "customize")
        self.release_v2({"AGENTS.md": "rules v2\n"})
        self.upgrade()
        write(self.project, {"AGENTS.md": "rules v2 plus ours\n"})
        (self.project / "AGENTS.md.template-new").unlink()
        commit_all(self.project, "merge template v2")

        result = self.upgrade()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("KEEP-LOCAL AGENTS.md", result.stdout)
        self.assertEqual(self.read("AGENTS.md"), "rules v2 plus ours\n")

    def test_customized_file_unchanged_upstream_is_kept(self) -> None:
        self.adopt()
        write(self.project, {"scripts/tool.sh": "echo ours\n"})
        commit_all(self.project, "customize")
        self.release_v2({"AGENTS.md": "rules v2\n"})

        result = self.upgrade()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("KEEP-LOCAL scripts/tool.sh", result.stdout)
        self.assertEqual(self.read("scripts/tool.sh"), "echo ours\n")

    def test_project_placeholders_are_never_overwritten_but_are_created_when_missing(self) -> None:
        self.release_v2({"AGENTS.project.md": "<!-- placeholder v2 -->\n"})
        result = self.upgrade()
        self.assertIn("KEEP-PLACEHOLDER AGENTS.project.md", result.stdout)
        self.assertEqual(self.read("AGENTS.project.md"), "our project rules\n")

        (self.project / "AGENTS.project.md").unlink()
        commit_all(self.project, "remove placeholder")
        result = self.upgrade()
        self.assertIn("PLACEHOLDER AGENTS.project.md", result.stdout)
        self.assertEqual(self.read("AGENTS.project.md"), "<!-- placeholder v2 -->\n")

    def test_files_removed_upstream_are_deleted_only_when_unmodified(self) -> None:
        self.adopt()
        write(self.project, {"scripts/tool.sh": "echo ours\n"})
        commit_all(self.project, "customize")
        self.release_v2({}, removed=("scripts/old.sh", "scripts/tool.sh"))

        result = self.upgrade()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("DELETE scripts/old.sh", result.stdout)
        self.assertIn("KEEP-REMOVED scripts/tool.sh", result.stdout)
        self.assertFalse((self.project / "scripts/old.sh").exists())
        self.assertEqual(self.read("scripts/tool.sh"), "echo ours\n")

    def test_project_data_and_template_only_files_are_untouched(self) -> None:
        self.release_v2({"docs/plan.md": "template only v2\n"})

        result = self.upgrade()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.read("apps/DEMO/100/application.apx"), "app X ()\n")
        self.assertFalse((self.project / "docs").exists())

    def test_first_upgrade_never_overwrites_a_differing_file(self) -> None:
        write(self.project, {"AGENTS.md": "rules edited before any lock\n"})
        commit_all(self.project, "edit")

        result = self.upgrade()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("CONFLICT AGENTS.md", result.stdout)
        self.assertEqual(self.read("AGENTS.md"), "rules edited before any lock\n")

    def test_dirty_tree_or_pending_conflict_file_is_refused(self) -> None:
        write(self.project, {"scratch.txt": "uncommitted\n"})
        result = self.upgrade()
        self.assertEqual(result.returncode, 2)
        self.assertIn("uncommitted changes", result.stderr)
        (self.project / "scratch.txt").unlink()

        write(self.project, {"AGENTS.md.template-new": "left over\n"})
        git(self.project, "add", "-A")
        git(self.project, "commit", "-q", "-m", "oops")
        result = self.upgrade()
        self.assertEqual(result.returncode, 2)
        self.assertIn(".template-new", result.stderr)

    def test_dry_run_changes_nothing(self) -> None:
        self.adopt()
        self.release_v2({"scripts/tool.sh": "echo v2\n"})

        result = self.upgrade("--dry-run")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UPDATE scripts/tool.sh", result.stdout)
        self.assertEqual(self.read("scripts/tool.sh"), "echo v1\n")
        self.assertEqual(git(self.project, "status", "--porcelain"), "")

    def test_ref_selects_the_template_version(self) -> None:
        v1 = git(self.template, "rev-parse", "HEAD")
        self.release_v2({"scripts/tool.sh": "echo v2\n"})

        result = self.upgrade("--ref", v1)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.read("scripts/tool.sh"), "echo v1\n")
        self.assertEqual(self.lock()["commit"], v1)

    def test_invalid_manifest_paths_are_refused_without_changes(self) -> None:
        bad_manifest = dict(MANIFEST)
        bad_manifest["templateOwned"] = ["../outside/**"]
        write(self.template, {"template-manifest.json": json.dumps(bad_manifest)})
        commit_all(self.template, "invalid manifest")
        before = git(self.project, "status", "--porcelain")

        result = self.upgrade()

        self.assertEqual(result.returncode, 2)
        self.assertIn("unsafe path pattern", result.stderr)
        self.assertEqual(git(self.project, "status", "--porcelain"), before)
        self.assertFalse((self.project.parent / "outside").exists())

    def test_lock_entries_cannot_escape_the_project_root(self) -> None:
        self.adopt()
        outside = self.project.parent / "outside.txt"
        outside.write_text("keep me\n", encoding="utf-8")
        data = self.lock()
        data["files"]["../outside.txt"] = "a" * 64
        write(self.project, {".template-lock.json": json.dumps(data)})
        commit_all(self.project, "corrupt lock path")

        result = self.upgrade()

        self.assertEqual(result.returncode, 2)
        self.assertIn("unsafe path", result.stderr)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep me\n")

    @unittest.skipIf(os.name == "nt", "symbolic links need privileges on Windows")
    def test_symlinked_project_parent_is_refused(self) -> None:
        self.adopt()
        outside = self.project.parent / "outside"
        outside.mkdir()
        (outside / "tool.sh").write_text("echo external\n", encoding="utf-8")
        shutil.rmtree(self.project / "scripts")
        (self.project / "scripts").symlink_to(outside, target_is_directory=True)
        commit_all(self.project, "replace managed dir with symlink")
        self.release_v2({"scripts/tool.sh": "echo v2\n"})

        result = self.upgrade()

        self.assertEqual(result.returncode, 2)
        self.assertIn("symbolic link", result.stderr)
        self.assertEqual((outside / "tool.sh").read_text(encoding="utf-8"), "echo external\n")

    def test_lock_cannot_direct_deletes_outside_current_manifest(self) -> None:
        self.adopt()
        outside = self.project / "private.txt"
        outside.write_text("keep me\n", encoding="utf-8")
        data = self.lock()
        data["files"]["private.txt"] = "a" * 64
        write(self.project, {".template-lock.json": json.dumps(data)})
        commit_all(self.project, "corrupt lock ownership")

        result = self.upgrade()

        self.assertEqual(result.returncode, 2)
        self.assertIn("not template-owned", result.stderr)
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep me\n")

    @unittest.skipIf(os.name == "nt", "symbolic links need privileges on Windows")
    def test_symbolic_link_in_template_is_refused(self) -> None:
        (self.template / "scripts/link.sh").symlink_to("tool.sh")
        commit_all(self.template, "link")

        result = self.upgrade()

        self.assertEqual(result.returncode, 2)
        self.assertIn("symbolic link", result.stderr)
        self.assertFalse((self.project / "scripts/link.sh").exists())


if __name__ == "__main__":
    unittest.main()
