#!/usr/bin/env python3
"""Regression tests for setup_graphify_apx.py using scratch-only fixtures."""

from __future__ import annotations

import builtins
from contextlib import redirect_stdout
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "setup_graphify_apx.py"
sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("setup_graphify_apx", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GraphifyPatchTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = REPO_ROOT / "scratch"
        scratch.mkdir(exist_ok=True)
        self.project = Path(tempfile.mkdtemp(prefix="graphify-test.", dir=scratch))
        self.root = self.project / '.venv-graphify' / 'lib' / 'python3.12' / 'site-packages' / 'graphify'
        self.root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree,self.project)

    def tearDown(self) -> None:
        shutil.rmtree(self.root)

    def write_package_at(self, base: Path) -> None:
        (base / "extractors").mkdir(parents=True, exist_ok=True)
        (base / "detect.py").write_text(
            "CODE_EXTENSIONS = {'.sql',}\n",
            encoding="utf-8",
        )
        (base / "extract.py").write_text(
            "from graphify.extractors.sql import extract_sql  # noqa: F401\n"
            '_DISPATCH = {\n    ".sql": extract_sql,\n}\n'
            '_EXTRA_FOR_EXTENSION = {\n    ".sql": "sql",\n}\n',
            encoding="utf-8",
        )

    def write_package(self) -> None:
        self.write_package_at(self.root)

    def snapshot_tree(self, root: Path) -> dict[str, tuple[str, bytes | str]]:
        snapshot: dict[str, tuple[str, bytes | str]] = {}
        for path in sorted(root.rglob("*")):
            relative = str(path.relative_to(root))
            if path.is_symlink():
                snapshot[relative] = ("symlink", os.readlink(path))
            elif path.is_dir():
                snapshot[relative] = ("directory", b"")
            else:
                snapshot[relative] = ("file", path.read_bytes())
        return snapshot

    def test_missing_required_module_fails(self) -> None:
        (self.root / "detect.py").write_text("EXTENSIONS = {'.sql',}\n", encoding="utf-8")
        self.assertFalse(MODULE.patch_graphify_dir(self.root,repo_root=self.project))

    def test_installs_canonical_extractor_and_routes_apx_to_it(self) -> None:
        self.write_package()

        self.assertTrue(MODULE.patch_graphify_dir(self.root,repo_root=self.project))

        installed = self.root / "extractors" / "apexlang.py"
        canonical = REPO_ROOT / "scripts" / "graphify_apexlang_extractor.py"
        self.assertEqual(installed.read_bytes(), canonical.read_bytes())
        extract = (self.root / "extract.py").read_text(encoding="utf-8")
        detect = (self.root / "detect.py").read_text(encoding="utf-8")
        self.assertIn("from graphify.extractors.apexlang import extract_apexlang", extract)
        self.assertIn('".apx": extract_apexlang,', extract)
        self.assertNotIn('".apx": extract_sql,', extract)
        self.assertIn("'.apx'", detect)
        # Foreign-key stubs only resolve when .sql goes through the wrapper.
        self.assertIn("from graphify.extractors.apexlang import extract_sql_linked", extract)
        self.assertIn('".sql": extract_sql_linked,', extract)
        self.assertNotIn('".sql": extract_sql,', extract)
        self.assertIn('".sql": "sql",', extract)

    def test_repeated_install_is_byte_for_byte_idempotent(self) -> None:
        self.write_package()
        self.assertTrue(MODULE.patch_graphify_dir(self.root,repo_root=self.project))
        paths = [
            self.root / "detect.py",
            self.root / "extract.py",
            self.root / "extractors" / "apexlang.py",
        ]
        first = {path: path.read_bytes() for path in paths}

        self.assertTrue(MODULE.patch_graphify_dir(self.root,repo_root=self.project))
        self.assertEqual(first, {path: path.read_bytes() for path in paths})

    def test_verify_mode_reports_an_unpatched_installation(self):
        self.write_package()
        self.assertFalse(MODULE.verify_installation(self.root,run_smoke=False)[0])

    def test_verify_mode_accepts_a_patched_installation(self):
        self.write_package()
        self.assertTrue(MODULE.patch_graphify_dir(self.root,repo_root=self.project))
        self.assertTrue(MODULE.verify_installation(self.root,run_smoke=False)[0])

    def test_verify_mode_does_not_mutate_package_or_scratch(self):
        self.write_package()
        self.assertTrue(MODULE.patch_graphify_dir(self.root,repo_root=self.project))
        before=self.snapshot_tree(self.project)
        self.assertTrue(MODULE.verify_installation(self.root,run_smoke=False)[0])
        self.assertEqual(before,self.snapshot_tree(self.project))

    def test_public_verify_ignores_shared_fallback_discovery(self):
        with mock.patch.object(MODULE,'REPO_ROOT',self.project), mock.patch.object(MODULE,'find_graphify_dirs',side_effect=AssertionError('shared discovery forbidden')):
            # Existing incomplete project environment must be attention, not shared fallback.
            self.assertEqual(MODULE.main(['--verify']),1)

    def test_legacy_verify_absent_optional_environment_is_informational(self):
        absent=self.project/'absent'; absent.mkdir()
        with mock.patch.object(MODULE,'REPO_ROOT',absent),mock.patch.object(MODULE,'find_graphify_dirs',side_effect=AssertionError('shared discovery forbidden')):
            self.assertEqual(MODULE.main(['--verify']),0)

    def test_legacy_setup_uses_pinned_project_setup(self):
        with mock.patch('scripts.graphify_project.setup_graphify',return_value=1) as setup, mock.patch.object(MODULE,'graphify_console_interpreter',side_effect=AssertionError('shared discovery forbidden')):
            self.assertFalse(MODULE.setup_graphify_apx(self.project))
        setup.assert_called_once_with(self.project)

    def test_repairs_outdated_installed_extractor(self) -> None:
        self.write_package()
        self.assertTrue(MODULE.patch_graphify_dir(self.root,repo_root=self.project))
        installed = self.root / "extractors" / "apexlang.py"
        installed.write_text("# stale extractor\n", encoding="utf-8")

        self.assertTrue(MODULE.patch_graphify_dir(self.root,repo_root=self.project))
        canonical = REPO_ROOT / "scripts" / "graphify_apexlang_extractor.py"
        self.assertEqual(installed.read_bytes(), canonical.read_bytes())

    def test_incompatible_dispatch_fails_without_partial_install(self) -> None:
        (self.root / "extractors").mkdir()
        (self.root / "detect.py").write_text("CODE_EXTENSIONS = {'.sql',}\n", encoding="utf-8")
        (self.root / "extract.py").write_text("DISPATCH_CHANGED = {}\n", encoding="utf-8")
        before = (self.root / "detect.py").read_bytes()

        self.assertFalse(MODULE.patch_graphify_dir(self.root,repo_root=self.project))
        self.assertEqual((self.root / "detect.py").read_bytes(), before)
        self.assertFalse((self.root / "extractors" / "apexlang.py").exists())

    def test_replaces_legacy_apx_sql_route(self) -> None:
        self.write_package()
        extract_path = self.root / "extract.py"
        extract_path.write_text(
            extract_path.read_text(encoding="utf-8").replace(
                '".sql": extract_sql,',
                '".sql": extract_sql,\n    ".apx": extract_sql,',
            ),
            encoding="utf-8",
        )

        self.assertTrue(MODULE.patch_graphify_dir(self.root,repo_root=self.project))
        extract = extract_path.read_text(encoding="utf-8")
        self.assertIn('".apx": extract_apexlang,', extract)
        self.assertNotIn('".apx": extract_sql,', extract)

    def test_sql_mention_before_the_extension_set_is_not_patched(self) -> None:
        self.write_package()
        detect_path = self.root / "detect.py"
        detect_path.write_text(
            "# handles '.sql', files among others\n"
            "CODE_EXTENSIONS = {'.py', '.sql', '.sh'}\n",
            encoding="utf-8",
        )

        self.assertTrue(MODULE.patch_graphify_dir(self.root,repo_root=self.project))

        detect = detect_path.read_text(encoding="utf-8")
        self.assertIn("# handles '.sql', files among others\n", detect)
        self.assertIn("CODE_EXTENSIONS = {'.py', '.sql', '.apx', '.sh'}", detect)
        self.assertTrue(MODULE.verify_installation(self.root)[0])

    def test_marker_outside_the_extension_set_does_not_verify(self) -> None:
        self.write_package()
        self.assertTrue(MODULE.patch_graphify_dir(self.root,repo_root=self.project))
        detect_path = self.root / "detect.py"
        detect_path.write_text("# '.sql', '.apx',\nCODE_EXTENSIONS = {'.py', '.sql', '.sh'}\n", encoding="utf-8")

        ok, reason = MODULE.verify_installation(self.root)

        self.assertFalse(ok)
        self.assertIn("not registered", reason)

    def test_unrecognized_existing_apx_handling_fails_closed(self) -> None:
        """A future Graphify that mentions .apx its own way must not look patched."""
        self.write_package()
        detect_path = self.root / "detect.py"
        detect_path.write_text(
            "CODE_EXTENSIONS = {'.sql',}\n"
            "MARKUP_EXTENSIONS = {'.apx',}\n",
            encoding="utf-8",
        )
        before = detect_path.read_bytes()

        self.assertFalse(
            MODULE.patch_graphify_dir(self.root,repo_root=self.project),
            "an unrecognized .apx mention must fail closed, not report success",
        )
        self.assertEqual(detect_path.read_bytes(), before)
        self.assertFalse((self.root / "extractors" / "apexlang.py").exists())

    def test_exposes_apx_cache_invalidation(self) -> None:
        self.assertTrue(
            hasattr(MODULE, "invalidate_apx_cache"),
            "invalidate_apx_cache(cache_root) is missing",
        )

    def test_invalidates_cached_apx_and_sql_extractions_only(self) -> None:
        # Both kinds depend on the database mirror: an .apx result names mirrored
        # tables and a .sql result names its foreign-key parents, and the cache
        # is keyed by file content alone.
        cache_root = self.root / "cache" / "ast" / "v0.9.35"
        cache_root.mkdir(parents=True)
        apx_cache = cache_root / "apx.json"
        sql_cache = cache_root / "sql.json"
        other_cache = cache_root / "other.json"
        apx_cache.write_text(
            '{"nodes":[{"source_file":"apps/DEMO/102/pages/p00004-home.apx"}],"edges":[]}',
            encoding="utf-8",
        )
        sql_cache.write_text(
            '{"nodes":[{"source_file":"database/DEMO/tables/ORDERS.sql"}],"edges":[]}',
            encoding="utf-8",
        )
        other_cache.write_text(
            '{"nodes":[{"source_file":"tools/helper.py"}],"edges":[]}',
            encoding="utf-8",
        )

        removed = MODULE.invalidate_apx_cache(self.root / "cache" / "ast")

        self.assertEqual(removed, 2)
        self.assertFalse(apx_cache.exists())
        self.assertFalse(sql_cache.exists())
        self.assertTrue(other_cache.exists())

    def test_explicit_setup_failure_does_not_mutate_other_installations(self):
        self.write_package(); before=self.snapshot_tree(self.project)
        with mock.patch('scripts.graphify_project.setup_graphify',return_value=1):
            self.assertFalse(MODULE.setup_graphify_apx(self.project))
        self.assertEqual(before,self.snapshot_tree(self.project))

    def test_prefers_the_interpreter_behind_the_graphify_console_script(self) -> None:
        console_script = self.root / "bin" / "graphify"
        console_script.parent.mkdir()
        console_script.write_text(f"#!{sys.executable}\n", encoding="utf-8")
        active_package = self.root / "active" / "site-packages" / "graphify"
        active_package.mkdir(parents=True)
        located = subprocess.CompletedProcess(
            args=[sys.executable, "-c", ""],
            returncode=0,
            stdout=f"{active_package}\n",
            stderr="",
        )
        with (
            mock.patch.object(MODULE.shutil, "which", return_value=str(console_script)),
            mock.patch.object(MODULE.subprocess, "run", return_value=located) as run,
            mock.patch.object(
                MODULE.glob,
                "glob",
                side_effect=AssertionError("resolved PATH installation must skip fallback scanning"),
            ),
        ):
            self.assertEqual(MODULE.find_graphify_dirs(), [str(active_package)])

        run.assert_called_once_with(
            [
                sys.executable,
                "-B",
                "-c",
                "import graphify, os; print(os.path.dirname(graphify.__file__))",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_fallback_candidates_are_unique_sorted_and_warning_counts_unique_paths(self) -> None:
        console_script = self.root / "bin" / "graphify"
        console_script.parent.mkdir()
        console_script.write_text("not a shebang\n", encoding="utf-8")
        first = self.root / "z-fallback"
        second = self.root / "a-fallback"
        original_import = builtins.__import__

        def import_without_graphify(name, *args, **kwargs):
            if name == "graphify":
                raise ImportError("graphify unavailable in test interpreter")
            return original_import(name, *args, **kwargs)

        output = io.StringIO()
        with (
            mock.patch.object(MODULE.shutil, "which", return_value=str(console_script)),
            mock.patch.object(
                builtins, "__import__", side_effect=import_without_graphify
            ),
            mock.patch.object(
                MODULE.glob,
                "glob",
                return_value=[str(first), str(second), str(first)],
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(
                MODULE.find_graphify_dirs(), sorted({str(first), str(second)})
            )

        self.assertIn(
            "Warning: could not resolve the active Graphify from PATH; "
            "patching 2 candidate installation(s)",
            output.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
