"""Exercise pack setup with real local Git repositories and real filesystem changes."""
import errno
import json
import os
import shutil
import subprocess
import stat
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch
from pathlib import Path

from scripts import workflow_skills


ROOT = Path(__file__).resolve().parents[1]


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@contextmanager
def windows_readonly_deletions():
    """Model Windows denying unlink of read-only Git objects, using the real filesystem."""
    unlink = os.unlink

    def remove(path, *args, **kwargs):
        mode = os.stat(path, dir_fd=kwargs.get("dir_fd"), follow_symlinks=False).st_mode
        if not mode & stat.S_IWRITE:
            raise PermissionError(errno.EACCES, "Windows read-only file cannot be unlinked", str(path))
        return unlink(path, *args, **kwargs)

    with patch.object(workflow_skills.os, "unlink", side_effect=remove):
        yield


class WorkflowSkillsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project [with spaces]"
        self.root.mkdir()
        git(self.root, "init", "-q")
        self.catalog = {}
        for pack, name in (("superpowers", "brainstorming"), ("mattpocock", "tdd")):
            source = self.base / pack
            skill = source / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\nTest skill\n", encoding="utf-8")
            (skill / "helper.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            (skill / "helper.sh").chmod(0o755)
            (source / "LICENSE").write_text("MIT license fixture", encoding="utf-8")
            git(source, "init", "-q")
            git(source, "add", ".")
            git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture")
            self.catalog[pack] = {"repository": str(source), "commit": git(source, "rev-parse", "HEAD"),
                                  "skills": {name: f"skills/{name}"}}

    def setup(self, selection, **kwargs):
        return workflow_skills.setup(self.root, selection, catalog=self.catalog, **kwargs)

    def test_default_status_is_none_without_writes(self):
        before = set(self.root.iterdir())
        result = workflow_skills.status(self.root)
        self.assertEqual(result["selection"], "none")
        self.assertEqual(result["primary"], "none")
        self.assertEqual(set(self.root.iterdir()), before)

    def test_setup_cleans_readonly_git_objects_and_releases_the_setup_lock(self):
        with windows_readonly_deletions():
            try:
                result = self.setup("superpowers")
            except PermissionError as exc:
                self.fail(f"setup must remove its read-only Git objects: {exc}")
        self.assertEqual(result["selection"], "superpowers")
        self.assertEqual(result["issues"], [])
        self.assertEqual(list((self.root / ".agent-skills").iterdir()), [])

    def test_readonly_cleanup_does_not_replace_the_original_setup_refusal(self):
        with windows_readonly_deletions():
            try:
                self.setup("superpowers")
                self.catalog["mattpocock"]["commit"] = "f" * 40
                with self.assertRaisesRegex(workflow_skills.SkillsError, "Git operation failed"):
                    self.setup("both")
            except PermissionError as exc:
                self.fail(f"cleanup must preserve the original refusal: {exc}")
        self.assertEqual(workflow_skills.status(self.root)["selection"], "superpowers")
        self.assertEqual(list((self.root / ".agent-skills").iterdir()), [])

    def test_cleanup_propagates_other_permission_errors_and_retains_recovery_lock(self):
        unlink = os.unlink

        def deny_writable(path, *args, **kwargs):
            mode = os.stat(path, dir_fd=kwargs.get("dir_fd"), follow_symlinks=False).st_mode
            if mode & stat.S_IWRITE:
                raise PermissionError(errno.EACCES, "ACL denies deletion", str(path))
            return unlink(path, *args, **kwargs)

        with patch.object(workflow_skills.os, "unlink", side_effect=deny_writable):
            with self.assertRaisesRegex(PermissionError, "ACL denies deletion"):
                self.setup("superpowers")
        self.assertTrue((self.root / ".agent-skills/setup-lock").is_dir())
        self.assertTrue(workflow_skills.status(self.root)["issues"])

    def test_each_selection_installs_only_requested_packs_and_records_exact_commit(self):
        for selection, names in (("none", []), ("superpowers", ["brainstorming"]),
                                 ("mattpocock", ["tdd"]), ("both", ["brainstorming", "tdd"])):
            with self.subTest(selection=selection):
                self.setup(selection)
                state = workflow_skills.status(self.root)
                self.assertEqual(state["selection"], selection)
                self.assertEqual(state["issues"], [])
                for agent in (".agents", ".claude"):
                    for name in ("brainstorming", "tdd"):
                        self.assertEqual((self.root / agent / "skills" / name / "SKILL.md").exists(), name in names)
                if names:
                    lock = json.loads((self.root / ".agent-skills.lock.json").read_text())
                    for pack, installed in lock["packs"].items():
                        self.assertEqual(installed["commit"], self.catalog[pack]["commit"])
                self.setup("none")

    def test_both_records_primary_and_switches_without_losing_other_pack(self):
        self.setup("both", primary="mattpocock")
        state = workflow_skills.status(self.root)
        self.assertEqual(state["primary"], "mattpocock")
        self.assertTrue((self.root / ".agents/skills/brainstorming/SKILL.md").exists())
        self.setup("both", primary="superpowers")
        self.assertEqual(workflow_skills.status(self.root)["primary"], "superpowers")

    def test_none_removes_managed_files_and_preserves_custom_skills(self):
        custom = self.root / ".agents/skills/custom/SKILL.md"
        custom.parent.mkdir(parents=True)
        custom.write_text("my skill")
        self.setup("both")
        self.setup("none")
        self.assertEqual(custom.read_text(), "my skill")
        self.assertFalse((self.root / ".agents/skills/tdd").exists())

    def test_edited_or_added_files_refuse_removal_without_changing_selection(self):
        self.setup("superpowers")
        folder = self.root / ".agents/skills/brainstorming"
        for filename in ("SKILL.md", "new.md"):
            path = folder / filename
            original = path.read_bytes() if path.exists() else None
            path.write_text("local edit")
            with self.subTest(filename=filename), self.assertRaisesRegex(workflow_skills.SkillsError, "modified"):
                self.setup("none")
            self.assertEqual(json.loads((self.root / ".agent-skills.json").read_text())["selection"], "superpowers")
            if original is None:
                path.unlink()
            else:
                path.write_bytes(original)

    def test_existing_unmanaged_or_tracked_skill_refuses_overwrite(self):
        folder = self.root / ".claude/skills/tdd"
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("user owned")
        with self.assertRaisesRegex(workflow_skills.SkillsError, "unmanaged"):
            self.setup("both")
        self.assertFalse((self.root / ".agents/skills/brainstorming").exists())
        shutil.rmtree(folder)
        self.setup("both")
        git(self.root, "add", ".agents/skills/tdd")
        with self.assertRaisesRegex(workflow_skills.SkillsError, "tracked"):
            self.setup("none")
        self.assertTrue((folder / "SKILL.md").exists())

    def test_unavailable_commit_keeps_old_install_and_config(self):
        self.setup("superpowers")
        config = (self.root / ".agent-skills.json").read_bytes()
        self.catalog["mattpocock"]["commit"] = "f" * 40
        with self.assertRaises(workflow_skills.SkillsError):
            self.setup("both")
        self.assertEqual((self.root / ".agent-skills.json").read_bytes(), config)
        self.assertTrue((self.root / ".agents/skills/brainstorming/SKILL.md").exists())

    def test_license_and_executable_helpers_are_preserved(self):
        self.setup("superpowers")
        skill = self.root / ".agents/skills/brainstorming"
        self.assertEqual((skill / "UPSTREAM-LICENSE").read_text(), "MIT license fixture")
        if os.name != "nt":
            self.assertTrue((skill / "helper.sh").stat().st_mode & 0o111)

    def test_metadata_write_failure_rolls_back_both_agent_directories_and_config(self):
        self.setup("superpowers")
        config = (self.root / ".agent-skills.json").read_bytes()
        lock = (self.root / ".agent-skills.lock.json").read_bytes()
        replace = os.replace

        def fail_lock(source, target):
            if Path(target).name == ".agent-skills.lock.json":
                raise OSError("simulated disk failure")
            return replace(source, target)

        with patch.object(workflow_skills.os, "replace", side_effect=fail_lock), self.assertRaises(OSError):
            self.setup("both")
        self.assertEqual((self.root / ".agent-skills.json").read_bytes(), config)
        self.assertEqual((self.root / ".agent-skills.lock.json").read_bytes(), lock)
        self.assertEqual(workflow_skills.status(self.root)["issues"], [])
        self.assertFalse((self.root / ".claude/skills/tdd").exists())

    def test_interrupt_after_skill_rename_restores_the_backup(self):
        self.setup("superpowers")
        original = (self.root / ".agents/skills/brainstorming/SKILL.md").read_bytes()
        rename = Path.rename
        source = self.root / ".agents/skills/brainstorming"

        def interrupt_after_rename(path, target):
            result = rename(path, target)
            # Compare the tail: on Windows the code renames the resolved path, whose temp
            # directory is the long form of the short (RUNNER~1) name in `source`.
            if Path(path).parts[-3:] == source.parts[-3:]:
                raise KeyboardInterrupt
            return result

        with patch.object(Path, "rename", interrupt_after_rename), self.assertRaises(KeyboardInterrupt):
            self.setup("none")
        self.assertEqual(original, (self.root / ".agents/skills/brainstorming/SKILL.md").read_bytes())
        self.assertFalse((self.root / ".agent-skills/setup-lock").exists())
        self.assertEqual(workflow_skills.status(self.root)["issues"], [])

    def test_source_symlinks_are_refused_without_installing_any_pack(self):
        source = self.base / "mattpocock"
        try:
            (source / "skills/tdd/link").symlink_to("SKILL.md")
        except OSError:
            self.skipTest("symlinks unavailable")
        git(source, "add", ".")
        git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "link")
        self.catalog["mattpocock"]["commit"] = git(source, "rev-parse", "HEAD")
        with self.assertRaisesRegex(workflow_skills.SkillsError, "link"):
            self.setup("both")
        self.assertFalse((self.root / ".agents/skills/brainstorming").exists())

    def test_invalid_primary_or_config_refuses_before_install(self):
        with self.assertRaises(workflow_skills.SkillsError):
            self.setup("superpowers", primary="mattpocock")
        (self.root / ".agent-skills.json").write_text('{"schemaVersion": 99}')
        with self.assertRaises(workflow_skills.SkillsError):
            self.setup("none")

    def test_symlink_parent_is_refused_without_touching_destination(self):
        external = self.base / "external"
        external.mkdir()
        try:
            (self.root / ".agents").symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        with self.assertRaises(workflow_skills.SkillsError):
            self.setup("superpowers")
        self.assertEqual(list(external.iterdir()), [])

    def test_status_reports_modified_and_missing_install_without_network(self):
        self.setup("both")
        (self.root / ".claude/skills/tdd/SKILL.md").unlink()
        state = workflow_skills.status(self.root)
        self.assertTrue(state["issues"])
        (self.root / ".agent-skills.lock.json").unlink()
        self.assertTrue(workflow_skills.status(self.root)["issues"])

    def test_tampered_lock_cannot_remove_other_project_files(self):
        self.setup("superpowers")
        path = self.root / ".agent-skills.lock.json"
        lock = json.loads(path.read_text())
        lock["directories"]["../../outside"] = {}
        path.write_text(json.dumps(lock))
        with self.assertRaises(workflow_skills.SkillsError):
            self.setup("none")

    def test_both_wrappers_status_work_without_env_or_sqlcl(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("team.sh", "team.ps1", "workflow_skills.py", "upgrade_template.py", "resolve_python.ps1"):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        for entry in ("team.sh", "team.ps1"):
            if entry.endswith("ps1"):
                if not shutil.which("pwsh"):
                    continue
                launcher = [shutil.which("pwsh"), "-NoProfile", "-File"]
            else:
                from fake_sqlcl import BASH
                launcher = [BASH]
            result = subprocess.run([*launcher, str(scripts / entry), "skills", "status", "--format", "json"],
                                    env=dict(os.environ, PROJECT_ENV_FILE="/missing/.env"), capture_output=True, text=True)
            with self.subTest(entry=entry):
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["selection"], "none")


if __name__ == "__main__":
    unittest.main()
