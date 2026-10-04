import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
import sys
from unittest.mock import patch
from pathlib import Path

import _windows_lf  # noqa: F401  (Path.write_text writes LF on Windows too)
from scripts import upgrade_template as upgrade_engine


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "scripts" / "upgrade_template.py"
MANIFEST = {
    "schemaVersion": 1,
    "upstream": "unused",
    "templateOwned": ["AGENTS.md", "scripts/**", "docs/migration-rules.md", "template-manifest.json"],
    "projectOwned": ["AGENTS.project.md"],
    "templateOnly": ["docs/plan.md"],
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
        template_manifest = {**MANIFEST, "upstream": str(self.template)}
        write(
            self.template,
            {
                "template-manifest.json": json.dumps(template_manifest),
                "AGENTS.md": "rules v1\n",
                "docs/migration-rules.md": "migration rules v1\n",
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
                "template-manifest.json": json.dumps(template_manifest),
                "AGENTS.md": "rules v1\n",
                "docs/migration-rules.md": "migration rules v1\n",
                "AGENTS.project.md": "our project rules\n",
                "scripts/tool.sh": "echo v1\n",
                "scripts/old.sh": "echo old\n",
                "apps/DEMO/100/application.apx": "app X ()\n",
                "migrations/2026-09-27_create-customers-r001/001-create-table.sql": "CREATE TABLE CUSTOMERS (ID NUMBER);\n",
                "migrations/2026-09-27_create-customers-r001/checks.json": "{\"schemaVersion\":1}\n",
                "migrations/2026-09-27_create-customers-r001/status.dev.json": "{\"state\":\"verified\"}\n",
            },
        )
        commit_all(self.project, "created from template")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def upgrade(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ENGINE), "--project-root", str(self.project), "--source", str(self.template), *extra],
            text=True,
            capture_output=True,
            check=False,
        )

    def upgrade_without_source(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ENGINE), "--project-root", str(self.project), *extra],
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

    def add_environment_files(self) -> None:
        manifest = {**MANIFEST, "upstream": str(self.template)}
        manifest["templateOwned"] = [*MANIFEST["templateOwned"], ".env.example"]
        common = {
            "template-manifest.json": json.dumps(manifest),
            ".env.example": "DEVELOPER_NAME=EXAMPLE\nTABLES_SQLCL_CONNECTION=demo-dev\n",
        }
        write(self.template, common)
        commit_all(self.template, "add environment example")
        write(self.project, {
            **common,
            ".env": "DEVELOPER_NAME=ALICE\nTABLES_SQLCL_CONNECTION=alice-dev\n",
            ".env.local": "PROJECT_NAME=local settings\n",
            ".env.example.backup": "PROJECT_NAME=example backup\n",
        })
        commit_all(self.project, "configure credential-free environment")

    def test_first_upgrade_env_example_conflict_preserves_tracked_environment(self) -> None:
        self.add_environment_files()
        self.assertIn(".env", git(self.project, "ls-files").splitlines())
        self.assertFalse((self.project / ".template-lock.json").exists())
        self.release_v2({".env.example": "DEVELOPER_NAME=EXAMPLE\nTABLES_SQLCL_CONNECTION=new-demo-dev\n"})

        result = self.upgrade()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("CONFLICT .env.example", result.stdout)
        self.assertEqual(self.read(".env.example.template-new"),
                         "DEVELOPER_NAME=EXAMPLE\nTABLES_SQLCL_CONNECTION=new-demo-dev\n")
        self.assertEqual(self.read(".env.example"), "DEVELOPER_NAME=EXAMPLE\nTABLES_SQLCL_CONNECTION=demo-dev\n")
        self.assertEqual(self.read(".env"), "DEVELOPER_NAME=ALICE\nTABLES_SQLCL_CONNECTION=alice-dev\n")
        self.assertEqual(self.read(".env.local"), "PROJECT_NAME=local settings\n")
        self.assertEqual(self.read(".env.example.backup"), "PROJECT_NAME=example backup\n")
        self.assertNotIn(".env", self.lock()["files"])
        self.assertNotIn(".env.example.template-new", self.lock()["files"])

    def test_existing_lock_env_example_conflict_preserves_customized_example(self) -> None:
        self.add_environment_files()
        self.adopt()
        write(self.project, {".env.example": "DEVELOPER_NAME=OUR_EXAMPLE\n"})
        commit_all(self.project, "customize environment example")
        self.release_v2({".env.example": "DEVELOPER_NAME=NEW_EXAMPLE\n"})

        result = self.upgrade()

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(self.read(".env.example"), "DEVELOPER_NAME=OUR_EXAMPLE\n")
        self.assertEqual(self.read(".env.example.template-new"), "DEVELOPER_NAME=NEW_EXAMPLE\n")
        self.assertEqual(self.read(".env"), "DEVELOPER_NAME=ALICE\nTABLES_SQLCL_CONNECTION=alice-dev\n")

    def test_manifest_cannot_manage_environment_or_generated_candidate(self) -> None:
        self.add_environment_files()
        for path in (".env", ".env.local", ".env.example.template-new", ".template-lock.json"):
            with self.subTest(path=path):
                manifest = {**MANIFEST, "upstream": str(self.template)}
                manifest["templateOwned"] = [*MANIFEST["templateOwned"], path]
                write(self.template, {
                    "template-manifest.json": json.dumps(manifest),
                    path: "PROJECT_NAME=untrusted template settings\n",
                })
                commit_all(self.template, "unsafe environment ownership")

                result = self.upgrade()

                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(git(self.project, "status", "--porcelain"), "")
                self.assertEqual(self.read(".env"), "DEVELOPER_NAME=ALICE\nTABLES_SQLCL_CONNECTION=alice-dev\n")
                self.assertFalse((self.project / ".env.example.template-new").exists())
                self.assertFalse((self.project / ".template-lock.json").exists())

    def test_lock_cannot_manage_environment_or_generated_candidate(self) -> None:
        self.add_environment_files()
        self.adopt()
        original_lock = self.lock()
        for path in (".env", ".env.local", ".env.example.template-new", ".template-lock.json"):
            with self.subTest(path=path):
                lock = {**original_lock, "files": {**original_lock["files"], path: "a" * 64}}
                write(self.project, {".template-lock.json": json.dumps(lock)})
                commit_all(self.project, "unsafe environment lock entry")

                result = self.upgrade()

                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("protected project data", result.stderr)
                self.assertEqual(git(self.project, "status", "--porcelain"), "")
                self.assertEqual(self.read(".env"), "DEVELOPER_NAME=ALICE\nTABLES_SQLCL_CONNECTION=alice-dev\n")
                self.assertFalse((self.project / ".env.example.template-new").exists())

    def test_ignored_environment_conflict_candidate_blocks_next_upgrade(self) -> None:
        self.add_environment_files()
        write(self.project, {".gitignore": ".env.*\n!/.env.example\n"})
        commit_all(self.project, "ignore local environment variants")
        self.release_v2({".env.example": "DEVELOPER_NAME=NEW_EXAMPLE\n"})
        first = self.upgrade()
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        commit_all(self.project, "record upgrade without resolving ignored candidate")
        self.assertNotIn(".env.example.template-new", git(self.project, "ls-files").splitlines())
        before = self.read(".template-lock.json")

        result = self.upgrade()

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("resolve and delete", result.stderr)
        self.assertEqual(self.read(".env.example.template-new"), "DEVELOPER_NAME=NEW_EXAMPLE\n")
        self.assertEqual(self.read(".env.example"), "DEVELOPER_NAME=EXAMPLE\nTABLES_SQLCL_CONNECTION=demo-dev\n")
        self.assertEqual(self.read(".template-lock.json"), before)
        self.assertEqual(git(self.project, "status", "--porcelain"), "")

    def test_environment_candidate_is_not_a_general_write_destination(self) -> None:
        self.add_environment_files()
        write(self.template, {".env.example.template-new": "PROJECT_NAME=not a conflict\n"})

        for kind in ("CREATE", "DELETE", "CONFLICT"):
            with self.subTest(kind=kind):
                with self.assertRaisesRegex(upgrade_engine.UpgradeError, "protected project data"):
                    upgrade_engine.apply_actions(
                        self.project, self.template,
                        [upgrade_engine.Action("CONFLICT", ".env.example"),
                         upgrade_engine.Action(kind, ".env.example.template-new", None)],
                        "unused", "0" * 40, {},
                    )

                self.assertEqual(git(self.project, "status", "--porcelain"), "")
                self.assertFalse((self.project / ".env.example.template-new").exists())
                self.assertFalse((self.project / ".template-lock.json").exists())

    def test_first_upgrade_of_identical_copy_writes_lock_without_changes(self) -> None:
        result = self.upgrade()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UNCHANGED AGENTS.md", result.stdout)
        self.assertEqual(self.lock()["commit"], git(self.template, "rev-parse", "HEAD"))
        self.assertIn("scripts/tool.sh", self.lock()["files"])
        self.assertNotIn("AGENTS.project.md", self.lock()["files"])

    def test_fresh_template_clone_uses_manifest_upstream_without_source_or_lock(self) -> None:
        result = self.upgrade_without_source("--dry-run")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UNCHANGED AGENTS.md", result.stdout)
        self.assertFalse((self.project / ".template-lock.json").exists())

    def test_unmodified_template_files_are_updated_and_new_files_created(self) -> None:
        self.adopt()
        self.release_v2({"scripts/tool.sh": "echo v2\n", "scripts/new.sh": "echo new\n"})

        result = self.upgrade()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UPDATE scripts/tool.sh", result.stdout)
        self.assertIn("CREATE scripts/new.sh", result.stdout)
        self.assertEqual(self.read("scripts/tool.sh"), "echo v2\n")
        self.assertEqual(self.read("scripts/new.sh"), "echo new\n")

    def test_edit_saved_after_planning_is_not_overwritten(self) -> None:
        self.adopt()
        original_lock = self.read(".template-lock.json")
        self.release_v2({"AGENTS.md": "rules v2\n", "scripts/tool.sh": "echo v2\n"})
        manifest = upgrade_engine.load_manifest(self.template)
        template_owned, placeholders = upgrade_engine.classify(self.template, manifest)
        lock = upgrade_engine.read_lock(self.project)
        actions, new_lock = upgrade_engine.plan_upgrade(self.project, self.template, template_owned, placeholders, lock)
        self.assertIn(upgrade_engine.Action("UPDATE", "scripts/tool.sh", upgrade_engine.sha256(self.project / "scripts/tool.sh")), actions)
        (self.project / "scripts/tool.sh").write_text("echo saved after planning\n", encoding="utf-8")

        with self.assertRaisesRegex(upgrade_engine.UpgradeError, "changed after the upgrade was planned"):
            upgrade_engine.apply_actions(
                self.project, self.template, actions, str(self.template),
                git(self.template, "rev-parse", "HEAD"), new_lock,
            )

        self.assertEqual(self.read("scripts/tool.sh"), "echo saved after planning\n")
        self.assertEqual(self.read("AGENTS.md"), "rules v1\n")
        self.assertEqual(self.read(".template-lock.json"), original_lock)

    def test_file_recreated_while_moved_aside_is_kept(self) -> None:
        self.adopt()
        self.release_v2({"scripts/tool.sh": "echo v2\n"})
        manifest = upgrade_engine.load_manifest(self.template)
        template_owned, placeholders = upgrade_engine.classify(self.template, manifest)
        lock, lock_hash = upgrade_engine.read_lock_with_hash(self.project)
        actions, new_lock = upgrade_engine.plan_upgrade(self.project, self.template, template_owned, placeholders, lock)
        real_replace = os.replace
        tool = self.project / "scripts" / "tool.sh"

        def save_after_move_aside(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
            real_replace(source, target)
            if Path(source) == tool:
                tool.write_text("echo saved meanwhile\n", encoding="utf-8")

        with patch("scripts.upgrade_template.os.replace", side_effect=save_after_move_aside):
            with self.assertRaisesRegex(upgrade_engine.UpgradeError, "recreated"):
                upgrade_engine.apply_actions(
                    self.project, self.template, actions, str(self.template),
                    git(self.template, "rev-parse", "HEAD"), new_lock, lock_hash=lock_hash,
                )

        self.assertEqual(self.read("scripts/tool.sh"), "echo saved meanwhile\n")

    def test_lock_changed_after_it_was_read_is_not_overwritten(self) -> None:
        self.adopt()
        self.release_v2({"scripts/tool.sh": "echo v2\n"})
        lock_hash = upgrade_engine.sha256(self.project / ".template-lock.json")
        manifest = upgrade_engine.load_manifest(self.template)
        template_owned, placeholders = upgrade_engine.classify(self.template, manifest)
        actions, new_lock = upgrade_engine.plan_upgrade(
            self.project, self.template, template_owned, placeholders, upgrade_engine.read_lock(self.project),
        )
        changed_lock = self.read(".template-lock.json").replace('"schemaVersion": 1', '"schemaVersion": 1 ')
        (self.project / ".template-lock.json").write_text(changed_lock, encoding="utf-8")

        with self.assertRaisesRegex(upgrade_engine.UpgradeError, "changed after the upgrade was planned"):
            upgrade_engine.apply_actions(
                self.project, self.template, actions, str(self.template),
                git(self.template, "rev-parse", "HEAD"), new_lock, lock_hash=lock_hash,
            )

        self.assertEqual(self.read(".template-lock.json"), changed_lock)
        self.assertEqual(self.read("scripts/tool.sh"), "echo v1\n")

    def test_filesystem_failure_rolls_back_prior_updates_and_lock(self) -> None:
        self.adopt()
        original_lock = self.read(".template-lock.json")
        self.release_v2({"AGENTS.md": "rules v2\n", "scripts/tool.sh": "echo v2\n"})
        actions = [
            upgrade_engine.Action("UPDATE", "AGENTS.md"),
            upgrade_engine.Action("UPDATE", "scripts/tool.sh"),
        ]
        real_install = upgrade_engine._install_no_replace

        def fail_installing_tool(source: Path, target: Path) -> None:
            # scripts/tool.sh has already moved aside when its install fails.
            if target.name == "tool.sh":
                raise OSError("injected filesystem failure")
            real_install(source, target)

        with patch("scripts.upgrade_template._install_no_replace", side_effect=fail_installing_tool):
            with self.assertRaisesRegex(upgrade_engine.UpgradeError, "filesystem update failed and was rolled back"):
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

    def test_interrupt_rolls_back_prior_updates_and_lock_like_a_failure(self) -> None:
        # Ctrl-C is a KeyboardInterrupt, not an OSError. It used to skip the
        # rollback and the cleanup then deleted the backups, leaving a half
        # applied upgrade whose lock already listed the new hashes.
        self.adopt()
        original_lock = self.read(".template-lock.json")
        self.release_v2({"AGENTS.md": "rules v2\n", "scripts/tool.sh": "echo v2\n"})
        actions = [
            upgrade_engine.Action("UPDATE", "AGENTS.md"),
            upgrade_engine.Action("UPDATE", "scripts/tool.sh"),
        ]
        real_install = upgrade_engine._install_no_replace

        def interrupt_installing_tool(source: Path, target: Path) -> None:
            if target.name == "tool.sh":
                raise KeyboardInterrupt
            real_install(source, target)

        with patch("scripts.upgrade_template._install_no_replace", side_effect=interrupt_installing_tool):
            with self.assertRaises(KeyboardInterrupt):
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
        self.assertEqual(list(self.project.glob(".apex-template-upgrade-*")), [])

    def test_interrupt_right_after_a_file_is_moved_aside_still_restores_it(self) -> None:
        # The signal can land after the original moved to its backup but before
        # the bookkeeping flag is set; rollback must look at the files, not the flag.
        self.adopt()
        original_lock = self.read(".template-lock.json")
        self.release_v2({"AGENTS.md": "rules v2\n"})
        actions = [upgrade_engine.Action("UPDATE", "AGENTS.md")]
        real_replace = upgrade_engine.os.replace
        interrupted = []

        def replace_then_interrupt(source, destination, *args, **keywords):
            real_replace(source, destination, *args, **keywords)
            if Path(destination).name.startswith("backup-") and not interrupted:
                interrupted.append(destination)
                raise KeyboardInterrupt

        with patch("scripts.upgrade_template.os.replace", side_effect=replace_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                upgrade_engine.apply_actions(
                    self.project,
                    self.template,
                    actions,
                    str(self.template),
                    git(self.template, "rev-parse", "HEAD"),
                    {"AGENTS.md": "a" * 64},
                )

        self.assertTrue(interrupted, "the injected interrupt never fired")
        self.assertEqual(self.read("AGENTS.md"), "rules v1\n")
        self.assertEqual(self.read(".template-lock.json"), original_lock)
        self.assertEqual(git(self.project, "status", "--porcelain"), "")

    def interrupt_right_after_installing(self, name: str, actions: list) -> None:
        # The signal can land after a file is installed but before the bookkeeping
        # flag says so; rollback must still remove it (new file) or restore the old one.
        real_install = upgrade_engine._install_no_replace
        interrupted = []

        def install_then_interrupt(source: Path, target: Path) -> None:
            real_install(source, target)
            if target.name == name and not interrupted:
                interrupted.append(target)
                raise KeyboardInterrupt

        with patch("scripts.upgrade_template._install_no_replace", side_effect=install_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                upgrade_engine.apply_actions(
                    self.project, self.template, actions, str(self.template),
                    git(self.template, "rev-parse", "HEAD"), {action.path: "a" * 64 for action in actions},
                )
        self.assertTrue(interrupted, "the injected interrupt never fired")

    def test_interrupt_right_after_a_new_file_is_installed_removes_it(self) -> None:
        self.adopt()
        original_lock = self.read(".template-lock.json")
        self.release_v2({"AGENTS.md": "rules v2\n", "scripts/new_tool.sh": "echo new\n"})

        self.interrupt_right_after_installing(
            "new_tool.sh",
            [upgrade_engine.Action("UPDATE", "AGENTS.md"), upgrade_engine.Action("CREATE", "scripts/new_tool.sh")],
        )

        self.assertFalse((self.project / "scripts" / "new_tool.sh").exists())
        self.assertEqual(self.read("AGENTS.md"), "rules v1\n")
        self.assertEqual(self.read(".template-lock.json"), original_lock)
        self.assertEqual(git(self.project, "status", "--porcelain"), "")
        self.assertEqual(list(self.project.glob(".apex-template-upgrade-*")), [])

    def test_interrupt_right_after_a_file_is_replaced_restores_the_old_one(self) -> None:
        self.adopt()
        original_lock = self.read(".template-lock.json")
        self.release_v2({"AGENTS.md": "rules v2\n"})

        self.interrupt_right_after_installing("AGENTS.md", [upgrade_engine.Action("UPDATE", "AGENTS.md")])

        self.assertEqual(self.read("AGENTS.md"), "rules v1\n")
        self.assertEqual(self.read(".template-lock.json"), original_lock)
        self.assertEqual(git(self.project, "status", "--porcelain"), "")
        self.assertEqual(list(self.project.glob(".apex-template-upgrade-*")), [])

    def test_interrupt_message_says_the_project_was_restored(self) -> None:
        self.adopt()
        self.release_v2({"AGENTS.md": "rules v2\n"})
        with patch("scripts.upgrade_template.fetch_template", side_effect=KeyboardInterrupt):
            with patch("sys.stderr") as stderr:
                status = upgrade_engine.main(["--project-root", str(self.project), "--source", str(self.template)])
        self.assertEqual(status, 130)
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("interrupted", written)

    @unittest.skipUnless(hasattr(signal, "SIGTERM") and os.name == "posix", "needs POSIX signals")
    @unittest.skipIf(os.name == "nt", "os.kill(os.getpid(), SIGTERM) terminates the process on Windows instead of raising the handler")
    def test_sigterm_is_handled_like_ctrl_c(self) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        try:
            upgrade_engine._interrupt_on_sigterm()
            with self.assertRaises(KeyboardInterrupt):
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(1)
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_failed_rollback_retains_backup_and_does_not_claim_success(self) -> None:
        self.adopt()
        self.release_v2({"AGENTS.md": "rules v2\n", "scripts/tool.sh": "echo v2\n"})
        actions = [
            upgrade_engine.Action("UPDATE", "AGENTS.md"),
            upgrade_engine.Action("UPDATE", "scripts/tool.sh"),
        ]
        real_install = upgrade_engine._install_no_replace
        real_replace = os.replace

        def fail_installing_tool(source: Path, target: Path) -> None:
            if target.name == "tool.sh":
                raise OSError("injected install failure")
            real_install(source, target)

        def fail_restoring_tool(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
            # Moving scripts/tool.sh aside works; restoring it fails.
            if Path(target).name == "tool.sh":
                raise OSError("injected restore failure")
            real_replace(source, target)

        with patch("scripts.upgrade_template._install_no_replace", side_effect=fail_installing_tool), \
                patch("scripts.upgrade_template.os.replace", side_effect=fail_restoring_tool):
            with self.assertRaises(upgrade_engine.UpgradeError) as caught:
                upgrade_engine.apply_actions(
                    self.project,
                    self.template,
                    actions,
                    str(self.template),
                    git(self.template, "rev-parse", "HEAD"),
                    {"AGENTS.md": "a" * 64, "scripts/tool.sh": "b" * 64},
                )

        message = str(caught.exception)
        self.assertIn("rollback incomplete", message)
        self.assertIn("recovery backups retained at", message)
        self.assertNotIn("was rolled back", message)
        recovery = Path(message.split("recovery backups retained at ", 1)[1])
        self.assertTrue(recovery.is_dir())
        self.assertIn(b"echo v1\n", [path.read_bytes() for path in recovery.iterdir() if path.is_file()])
        self.assertEqual(self.read("AGENTS.md"), "rules v1\n")

    def test_rollback_keeps_an_edit_saved_after_install(self) -> None:
        self.adopt()
        self.release_v2({"AGENTS.md": "rules v2\n", "scripts/tool.sh": "echo v2\n"})
        actions = [
            upgrade_engine.Action("UPDATE", "AGENTS.md"),
            upgrade_engine.Action("UPDATE", "scripts/tool.sh"),
        ]
        real_install = upgrade_engine._install_no_replace

        def edit_agents_then_fail(source: Path, target: Path) -> None:
            if target.name == "tool.sh":
                (self.project / "AGENTS.md").write_text("rules edited during upgrade\n", encoding="utf-8")
                raise OSError("injected install failure")
            real_install(source, target)

        with patch("scripts.upgrade_template._install_no_replace", side_effect=edit_agents_then_fail):
            with self.assertRaisesRegex(upgrade_engine.UpgradeError, "changed after the upgrade installed it"):
                upgrade_engine.apply_actions(
                    self.project, self.template, actions, str(self.template),
                    git(self.template, "rev-parse", "HEAD"),
                    {"AGENTS.md": "a" * 64, "scripts/tool.sh": "b" * 64},
                )

        self.assertEqual(self.read("AGENTS.md"), "rules edited during upgrade\n")
        self.assertEqual(self.read("scripts/tool.sh"), "echo v1\n")

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
        self.assertEqual(self.read("docs/migration-rules.md"), "migration rules v1\n")
        self.assertFalse((self.project / "docs/plan.md").exists())
        self.assertEqual(
            self.read("migrations/2026-09-27_create-customers-r001/001-create-table.sql"),
            "CREATE TABLE CUSTOMERS (ID NUMBER);\n",
        )
        self.assertEqual(
            self.read("migrations/2026-09-27_create-customers-r001/checks.json"),
            "{\"schemaVersion\":1}\n",
        )
        self.assertEqual(
            self.read("migrations/2026-09-27_create-customers-r001/status.dev.json"),
            "{\"state\":\"verified\"}\n",
        )

    def test_migration_guide_is_updated_but_local_migration_files_and_receipts_are_untouched(self) -> None:
        self.adopt()
        self.release_v2({"docs/migration-rules.md": "migration rules v2\n"})

        result = self.upgrade()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UPDATE docs/migration-rules.md", result.stdout)
        self.assertEqual(self.read("docs/migration-rules.md"), "migration rules v2\n")
        self.assertEqual(
            self.read("migrations/2026-09-27_create-customers-r001/status.dev.json"),
            "{\"state\":\"verified\"}\n",
        )

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

    def test_a_working_copy_that_only_differs_by_line_endings_is_not_a_dirty_tree(self) -> None:
        # With core.autocrlf=true and eol=lf, `git status` calls a file modified after an editor
        # saved it with CRLF although `git diff` has nothing to show. The guard is about content,
        # so it asks `git diff`.
        git(self.project, "config", "core.autocrlf", "true")
        # Bytes, not text: write_text would turn these LF files into CRLF ones on Windows.
        (self.project / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
        (self.project / "docs").mkdir(exist_ok=True)
        (self.project / "docs" / "note.md").write_bytes(b"line one\nline two\n")
        commit_all(self.project, "attributes and a note")
        (self.project / "docs" / "note.md").write_bytes(b"line one\r\nline two\r\n")
        if not git(self.project, "status", "--porcelain"):
            self.skipTest("this Git does not report a line-ending-only change as modified")

        result = self.upgrade("--dry-run")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("uncommitted changes", result.stderr)

    def test_a_real_edit_of_a_tracked_file_is_still_a_dirty_tree(self) -> None:
        write(self.project, {"scripts/tool.sh": "echo edited\n"})

        result = self.upgrade("--dry-run")

        self.assertEqual(result.returncode, 2)
        self.assertIn("uncommitted changes", result.stderr)

    def test_a_staged_change_is_still_a_dirty_tree(self) -> None:
        write(self.project, {"scripts/tool.sh": "echo edited\n"})
        git(self.project, "add", "scripts/tool.sh")

        result = self.upgrade("--dry-run")

        self.assertEqual(result.returncode, 2)
        self.assertIn("uncommitted changes", result.stderr)

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


class EnvironmentGitPolicyTests(unittest.TestCase):
    def test_root_configuration_is_trackable_but_nested_and_variant_env_files_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            init_repo(project)
            (project / ".gitignore").write_bytes((ROOT / ".gitignore").read_bytes())
            paths = {
                ".env": False,
                ".env.example": False,
                ".env.local": True,
                ".env.example.template-new": True,
                "nested/.env": True,
                "nested/.env.local": True,
                "nested/.env.example": True,
            }
            for path, ignored in paths.items():
                with self.subTest(path=path):
                    write(project, {path: "PROJECT_NAME=credential-free fixture\n"})
                    result = subprocess.run(
                        ["git", "-C", str(project), "check-ignore", "-q", path], check=False,
                    )
                    self.assertEqual(result.returncode, 0 if ignored else 1)
            add = subprocess.run(
                ["git", "-C", str(project), "add", ".env", ".env.example"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(add.returncode, 0, add.stderr)
            self.assertEqual(git(project, "ls-files").splitlines(), [".env", ".env.example"])


class LineEndingPlanTests(unittest.TestCase):
    """plan_upgrade must not treat a CRLF/LF difference as a customization.

    Git for Windows checks text out as CRLF by default, so one template file has
    different bytes on different machines.
    """

    def plan(self, template_bytes: bytes, project_bytes: bytes | None, lock_files: dict[str, str]):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            template, project = base / "template", base / "project"
            template.mkdir()
            project.mkdir()
            (template / "AGENTS.md").write_bytes(template_bytes)
            if project_bytes is not None:
                (project / "AGENTS.md").write_bytes(project_bytes)
            actions, new_lock = upgrade_engine.plan_upgrade(
                project, template, ["AGENTS.md"], [], {"files": lock_files}
            )
        return {action.path: action.kind for action in actions}, new_lock

    def test_crlf_template_and_lf_project_are_unchanged_not_a_conflict(self) -> None:
        kinds, _ = self.plan(b"rules v1\r\nmore\r\n", b"rules v1\nmore\n", {})

        self.assertEqual(kinds["AGENTS.md"], "UNCHANGED")

    def test_lf_template_and_crlf_project_are_unchanged_not_a_conflict(self) -> None:
        kinds, _ = self.plan(b"rules v1\nmore\n", b"rules v1\r\nmore\r\n", {})

        self.assertEqual(kinds["AGENTS.md"], "UNCHANGED")

    def test_a_real_edit_is_still_a_conflict(self) -> None:
        kinds, _ = self.plan(b"rules v2\r\n", b"our edit\n", {"AGENTS.md": "0" * 64})

        self.assertEqual(kinds["AGENTS.md"], "CONFLICT")

    def test_lock_written_before_normalization_still_recognizes_an_untouched_file(self) -> None:
        # Older locks hold the hash of the exact bytes, here a CRLF checkout.
        raw_lock_hash = hashlib.sha256(b"rules v1\r\n").hexdigest()

        kinds, new_lock = self.plan(b"rules v2\n", b"rules v1\r\n", {"AGENTS.md": raw_lock_hash})

        self.assertEqual(kinds["AGENTS.md"], "UPDATE")
        self.assertEqual(new_lock["AGENTS.md"], hashlib.sha256(b"rules v2\n").hexdigest())

    def test_lock_hash_is_the_same_for_lf_and_crlf_template_bytes(self) -> None:
        _, lf_lock = self.plan(b"a\nb\n", None, {})
        _, crlf_lock = self.plan(b"a\r\nb\r\n", None, {})

        self.assertEqual(lf_lock, crlf_lock)
        self.assertEqual(lf_lock["AGENTS.md"], hashlib.sha256(b"a\nb\n").hexdigest())

    def test_binary_content_is_compared_byte_for_byte(self) -> None:
        kinds, _ = self.plan(b"\x00a\r\nb", b"\x00a\nb", {})

        self.assertEqual(kinds["AGENTS.md"], "CONFLICT")

    def project_and_template(self, base: Path, template_bytes: bytes, project_bytes: bytes) -> tuple[Path, Path]:
        template, project = base / "template", base / "project"
        template.mkdir()
        project.mkdir()
        (template / "AGENTS.md").write_bytes(template_bytes)
        (project / "AGENTS.md").write_bytes(project_bytes)
        return project, template

    def test_an_older_lock_upgrades_an_untouched_crlf_file_and_is_rewritten_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, template = self.project_and_template(Path(temporary), b"rules v2\n", b"rules v1\r\n")
            raw_lock = {"AGENTS.md": hashlib.sha256(b"rules v1\r\n").hexdigest()}
            actions, new_lock = upgrade_engine.plan_upgrade(project, template, ["AGENTS.md"], [], {"files": raw_lock})

            upgrade_engine.apply_actions(project, template, actions, "unused", "0" * 40, new_lock)

            self.assertEqual((project / "AGENTS.md").read_bytes(), b"rules v2\n")
            written = json.loads((project / upgrade_engine.LOCK_NAME).read_text(encoding="utf-8"))
            self.assertEqual(written["files"], {"AGENTS.md": hashlib.sha256(b"rules v2\n").hexdigest()})

    def test_a_line_ending_only_save_after_planning_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project, template = self.project_and_template(Path(temporary), b"rules v2\n", b"rules v1\r\n")
            raw_lock = {"AGENTS.md": hashlib.sha256(b"rules v1\r\n").hexdigest()}
            actions, new_lock = upgrade_engine.plan_upgrade(project, template, ["AGENTS.md"], [], {"files": raw_lock})
            self.assertEqual({action.path: action.kind for action in actions}, {"AGENTS.md": "UPDATE"})
            # The planning decision ignores line endings, the race guard must not.
            (project / "AGENTS.md").write_bytes(b"rules v1\n")

            with self.assertRaisesRegex(upgrade_engine.UpgradeError, "changed after the upgrade was planned"):
                upgrade_engine.apply_actions(project, template, actions, "unused", "0" * 40, new_lock)

            self.assertEqual((project / "AGENTS.md").read_bytes(), b"rules v1\n")
            self.assertFalse((project / upgrade_engine.LOCK_NAME).exists())


if __name__ == "__main__":
    unittest.main()
