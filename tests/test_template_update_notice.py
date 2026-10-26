"""Check update notices against real local Git refs without network access."""
import json
import os
from pathlib import Path
import subprocess
import _test_subprocess_safety  # noqa: F401
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TemplateUpdateNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "template source"
        self.source.mkdir()
        self.project = self.root / "downstream project"
        self.project.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        self.first = self.commit("first")
        self.git("branch", "release-26.1")
        self.second = self.commit("second")
        self.lock = {"schemaVersion": 1, "upstream": str(self.source),
                     "templateRef": "release-26.1", "commit": self.first, "files": {}}

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.source), *args],
                                capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def commit(self, text):
        (self.source / "file.txt").write_text(text, encoding="utf-8")
        self.git("add", "file.txt")
        self.git("commit", "-qm", text)
        return self.git("rev-parse", "HEAD")

    def run_check(self, lock=None, environment=None):
        path = self.project / ".template-lock.json"
        path.write_text(json.dumps(self.lock if lock is None else lock), encoding="utf-8")
        before = path.read_bytes()
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/check_template_update.py"), "--repo-root", str(self.project)],
            env=environment, capture_output=True, text=True, check=False, timeout=20,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        self.assertEqual(before, path.read_bytes())
        self.assertEqual([".template-lock.json"], sorted(p.name for p in self.project.iterdir()))
        return result.stdout

    def test_newer_default_branch_does_not_notify_a_current_pinned_branch(self):
        self.assertEqual("", self.run_check())

    def test_notice_uses_the_recorded_branch_and_changes_no_project_files(self):
        self.git("checkout", "-q", "release-26.1")
        changed = self.commit("release update")
        for ref in ("release-26.1", "refs/heads/release-26.1"):
            with self.subTest(ref=ref):
                result = self.run_check({**self.lock, "templateRef": ref})
                self.assertEqual(1, len(result.splitlines()))
                self.assertIn(self.first[:7] + " -> " + changed[:7], result)
                self.assertIn("upgrade-template --dry-run", result)

    def test_annotated_tags_compare_the_peeled_commit(self):
        self.git("tag", "-a", "stable", self.first, "-m", "stable")
        for ref in ("stable", "refs/tags/stable"):
            with self.subTest(ref=ref):
                self.assertEqual("", self.run_check({**self.lock, "templateRef": ref}))
        self.git("tag", "-f", "-a", "stable", self.second, "-m", "new stable")
        result = self.run_check({**self.lock, "templateRef": "stable"})
        self.assertIn(self.first[:7] + " -> " + self.second[:7], result)

    def test_ambiguous_branch_and_tag_or_unknown_ref_stay_silent(self):
        self.git("tag", "release-26.1", self.second)
        self.assertEqual("", self.run_check())
        self.assertEqual("", self.run_check({**self.lock, "templateRef": "missing-ref"}))

    def test_commit_pin_and_legacy_lock_without_ref_do_not_check_head(self):
        self.assertEqual("", self.run_check({**self.lock, "templateRef": self.first}))
        legacy = dict(self.lock)
        del legacy["templateRef"]
        self.assertEqual("", self.run_check(legacy))

    def test_missing_or_invalid_lock_is_quiet(self):
        for payload in (None, "broken json", "[]"):
            path = self.project / ".template-lock.json"
            if payload is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(payload, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/check_template_update.py"), "--repo-root", str(self.project)],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual((0, "", ""), (result.returncode, result.stdout, result.stderr))

    def test_unavailable_source_or_git_is_quiet(self):
        self.assertEqual("", self.run_check({**self.lock, "upstream": str(self.root / "absent")}))
        environment = dict(os.environ, PATH=str(self.root / "no-bin"))
        self.assertEqual("", self.run_check(environment=environment))

    def test_timeout_returns_unknown_with_a_bounded_noninteractive_git_call(self):
        from unittest.mock import patch
        from scripts.check_template_update import update_notice
        (self.project / ".template-lock.json").write_text(json.dumps(self.lock), encoding="utf-8")
        with patch("scripts.check_template_update.subprocess.run", side_effect=[
            subprocess.CompletedProcess([], 0, "", ""), subprocess.TimeoutExpired("git ls-remote", 10),
        ]) as run:
            self.assertIsNone(update_notice(self.project))
        self.assertEqual(10, run.call_args.kwargs["timeout"])
        self.assertEqual("0", run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"])

    def test_credentials_and_option_like_refs_never_start_git(self):
        from unittest.mock import patch
        from scripts.check_template_update import update_notice
        for changes in ({"upstream": "https://user@example.invalid/template"},
                        {"templateRef": "--upload-pack=anything"}):
            with self.subTest(changes=changes):
                (self.project / ".template-lock.json").write_text(json.dumps({**self.lock, **changes}), encoding="utf-8")
                with patch("scripts.check_template_update.subprocess.run") as run:
                    self.assertIsNone(update_notice(self.project))
                run.assert_not_called()
