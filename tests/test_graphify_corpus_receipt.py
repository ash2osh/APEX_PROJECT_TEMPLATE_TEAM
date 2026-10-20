"""Exact-detector corpus receipts keep offline inspection independent of ignore parsing."""
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import _no_real_sqlcl  # noqa: F401

from scripts import graphify_project as graphify
from scripts.setup_graphify_apx import patch_graphify_dir
import test_graphify_project as project_fixtures


class CorpusReceiptTests(unittest.TestCase):
    def setUp(self):
        self.helper = project_fixtures.GraphifyProjectTests()
        self.helper.setUp()
        self.addCleanup(self.helper.tearDown)
        self.root = self.helper.proj_a
        package = self.helper.write_mock_env(self.root / ".venv-graphify")
        with redirect_stdout(io.StringIO()):
            self.assertTrue(patch_graphify_dir(package, repo_root=self.root))
        (self.root / "app_context").mkdir()
        (self.root / "app_context/visible.md").write_text("# Visible\n", encoding="utf-8")
        (self.root / "app_context/private.md").write_text("# Excluded\n", encoding="utf-8")
        (self.root / ".graphifyignore").write_text("/app_context/private.md\n", encoding="utf-8")
        self.output = self.root / "graphify-out"
        self.output.mkdir()
        self.graph = self.output / "graph.json"
        self.graph.write_text(json.dumps({"nodes": [{"id": "visible", "source_file": "app_context/visible.md"}],
                                          "edges": []}), encoding="utf-8")

    def refresh(self, selected=None):
        selected = selected if selected is not None else [str(self.root / "app_context/visible.md")]
        result = Mock(returncode=0, stdout=json.dumps({"files": selected, "walk_errors": []}), stderr="")
        with patch.object(graphify.subprocess, "run", return_value=result) as run:
            with redirect_stdout(io.StringIO()):
                code = graphify.main(["--root", str(self.root), "verify", "--refresh-corpus"])
        return code, run

    def inspect_offline(self):
        with patch.object(graphify.subprocess, "run", side_effect=AssertionError("inspection must stay offline")):
            return graphify.inspect_graphify(self.root)

    def test_custom_exclusion_passes_after_exact_detector_refresh(self):
        code, run = self.refresh()
        self.assertEqual(code, 0)
        self.assertEqual(self.inspect_offline()["status"], "current")
        command = run.call_args.args[0]
        self.assertEqual(command[0], str(graphify.environment_python(self.root / ".venv-graphify")))
        self.assertIn("_read_build_excludes", command[3])
        self.assertIn("_read_build_gitignore", command[3])

    def test_missing_receipt_reports_coverage_unavailable_without_guessing(self):
        result = self.inspect_offline()
        self.assertEqual(result["status"], "stale")
        self.assertTrue(result["is_package_ready"])
        self.assertIn("coverage unavailable", " ".join(result["reasons"]).lower())
        self.assertNotIn("private.md", " ".join(result["reasons"]))

    def test_new_deleted_and_changed_candidates_invalidate_the_receipt(self):
        visible = self.root / "app_context/visible.md"
        for action in (lambda: (self.root / "app_context/new.md").write_text("new"),
                       lambda: visible.write_text("changed"), lambda: visible.unlink()):
            with self.subTest(action=action):
                self.assertEqual(self.refresh()[0], 0)
                action()
                self.assertEqual(self.inspect_offline()["status"], "stale")
                visible.write_text("# Visible\n", encoding="utf-8")
                (self.root / "app_context/new.md").unlink(missing_ok=True)

    def test_default_verify_does_not_spawn_or_write_a_receipt(self):
        with patch.object(graphify.subprocess, "run", side_effect=AssertionError("default verify must stay offline")):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(graphify.main(["--root", str(self.root), "verify"]), 1)
        self.assertFalse((self.output / graphify.CORPUS_RECEIPT).exists())

    def test_global_target_cannot_be_erased_before_refresh_validation(self):
        with patch.object(graphify.subprocess, "run", side_effect=AssertionError("external targets cannot refresh")):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(graphify.main(["--root", str(self.root), "--target", str(self.helper.global_dir),
                                                "verify", "--refresh-corpus"]), 1)
        self.assertFalse((self.output / graphify.CORPUS_RECEIPT).exists())

    def test_excluded_source_link_is_fingerprinted_without_reading_its_target(self):
        target = self.helper.test_root / "outside.md"
        target.write_text("private external content", encoding="utf-8")
        link = self.root / "app_context/private-link.md"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        with (self.root / ".graphifyignore").open("a", encoding="utf-8") as handle:
            handle.write("/app_context/private-link.md\n")
        read_bytes = Path.read_bytes
        def guarded_read(path):
            if path == target:
                self.fail("excluded external target was read")
            return read_bytes(path)
        with patch.object(Path, "read_bytes", guarded_read):
            self.assertEqual(self.refresh()[0], 0)
            self.assertEqual(self.inspect_offline()["status"], "current")
            target.write_text("changed excluded target", encoding="utf-8")
            self.assertEqual(self.inspect_offline()["status"], "current")
            self.assertNotEqual(self.refresh([str(link)])[0], 0)

    def test_included_local_link_tracks_its_in_project_target_content(self):
        target = self.root / "notes.txt"
        target.write_text("local source", encoding="utf-8")
        link = self.root / "app_context/alias.md"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("symlinks unavailable")
        self.graph.write_text(json.dumps({"nodes": [{"id": "alias", "source_file": "app_context/alias.md"}]}),
                              encoding="utf-8")
        self.assertEqual(self.refresh([str(link)])[0], 0)
        self.assertEqual(self.inspect_offline()["status"], "current")
        target.write_text("changed local source", encoding="utf-8")
        self.assertEqual(self.inspect_offline()["status"], "stale")

    def test_domain_root_and_ignore_links_refuse_without_following_external_trees(self):
        external = self.helper.test_root / "external"
        external.mkdir()
        (external / "outside.apx").write_text("unread", encoding="utf-8")
        try:
            (self.root / "apps").symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("symlinks unavailable")
        rglob = Path.rglob
        def guarded_walk(path, pattern):
            if path == self.root / "apps":
                self.fail("external domain root was traversed")
            return rglob(path, pattern)
        with patch.object(Path, "rglob", guarded_walk):
            self.assertNotEqual(self.refresh()[0], 0)
            self.assertEqual(self.inspect_offline()["status"], "stale")
        (self.root / "apps").unlink()
        (self.root / ".graphifyignore").unlink()
        (self.root / ".graphifyignore").symlink_to(external / "outside.apx")
        self.assertNotEqual(self.refresh()[0], 0)

    def test_extract_refreshes_the_receipt_after_the_parser_check(self):
        scan = Mock(returncode=0, stdout=json.dumps({"files": [str(self.root / "app_context/visible.md")],
                                                   "walk_errors": []}), stderr="")
        with patch.object(graphify.subprocess, "run", side_effect=[Mock(returncode=0), Mock(returncode=0), scan]) as run:
            with redirect_stdout(io.StringIO()):
                self.assertEqual(graphify.extract_graphify(self.root, []), 0)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(self.inspect_offline()["status"], "current")

    def test_changed_inputs_during_the_scan_refuse_a_receipt(self):
        def scan(*args, **kwargs):
            (self.root / "app_context/visible.md").write_text("changed during scan", encoding="utf-8")
            return Mock(returncode=0, stdout=json.dumps({"files": [str(self.root / "app_context/visible.md")],
                                                       "walk_errors": []}), stderr="")
        with patch.object(graphify.subprocess, "run", side_effect=scan), redirect_stdout(io.StringIO()):
            self.assertEqual(graphify.main(["--root", str(self.root), "verify", "--refresh-corpus"]), 1)
        self.assertFalse((self.output / graphify.CORPUS_RECEIPT).exists())

    def test_scan_error_and_incomplete_scan_do_not_install_a_receipt(self):
        for result in (Mock(returncode=1, stdout="", stderr="unavailable"),
                       Mock(returncode=0, stdout='{"files":[],"walk_errors":["denied"]}', stderr="")):
            with self.subTest(result=result):
                with patch.object(graphify.subprocess, "run", return_value=result), redirect_stdout(io.StringIO()):
                    self.assertEqual(graphify.main(["--root", str(self.root), "verify", "--refresh-corpus"]), 1)
                self.assertFalse((self.output / graphify.CORPUS_RECEIPT).exists())

    def test_nested_ignore_graph_and_saved_settings_changes_invalidate_receipt(self):
        for relative, contents in (("app_context/.graphifyignore", "*.md\n"),
                                   ("app_context/.gitignore", "private.md\n"),
                                   ("graphify-out/.graphify_build.json", '{"excludes":["private.md"]}'),
                                   ("graphify-out/graph.json", '{"nodes":[],"edges":[]}')):
            with self.subTest(relative=relative):
                self.assertEqual(self.refresh()[0], 0)
                path = self.root / relative
                before = path.read_bytes() if path.exists() else None
                path.write_text(contents, encoding="utf-8")
                self.assertEqual(self.inspect_offline()["status"], "stale")
                if before is None:
                    path.unlink()
                else:
                    path.write_bytes(before)

    def test_non_domain_noise_marker_removal_invalidates_eligibility(self):
        coverage = self.root / "app_context/coverage"
        coverage.mkdir()
        (coverage / "guide.md").write_text("# Guide\n", encoding="utf-8")
        marker = coverage / "index.html"
        marker.write_text("report", encoding="utf-8")
        # Pinned detect._has_coverage_artifacts prunes this directory while
        # index.html exists, so the exact scan legitimately returns visible only.
        self.assertEqual(self.refresh()[0], 0)
        before = graphify.corpus_inputs(self.root)
        marker.unlink()
        self.assertNotEqual(graphify.corpus_inputs(self.root), before)
        self.assertEqual(self.inspect_offline()["status"], "stale")

    def test_included_source_missing_from_graph_refuses_a_receipt(self):
        code, _ = self.refresh([str(self.root / "app_context/visible.md"), str(self.root / "app_context/private.md")])
        self.assertNotEqual(code, 0)
        self.assertEqual(self.inspect_offline()["status"], "stale")

    def test_linked_worktree_index_and_common_exclude_invalidate_receipt(self):
        metadata = self.helper.test_root / "git-common"
        worktree = metadata / "worktrees/fixture"
        worktree.mkdir(parents=True)
        (metadata / "info").mkdir()
        (worktree / "commondir").write_text("../..\n", encoding="utf-8")
        (self.root / ".git").write_text(f"gitdir: {worktree}\n", encoding="utf-8")
        for path in (worktree / "index", metadata / "info/exclude"):
            with self.subTest(path=path):
                self.assertEqual(self.refresh()[0], 0)
                path.write_text("changed\n", encoding="utf-8")
                self.assertEqual(self.inspect_offline()["status"], "stale")


if __name__ == "__main__":
    unittest.main()
