#!/usr/bin/env python3
"""Tests for project-isolated optional Graphify and shared-inode protection."""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest import mock

import _no_real_sqlcl  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.dont_write_bytecode = True

from scripts.graphify_project import (
    project_graphify_environment,
    inspect_graphify,
    main as graphify_project_main,
)
from scripts.setup_graphify_apx import verify_installation


class GraphifyProjectTests(unittest.TestCase):
    def test_patch_refuses_named_graphify_directories_outside_canonical_environment(self):
        from scripts.setup_graphify_apx import patch_graphify_dir
        package=self.test_root/'graphify-shared'/'graphify'
        self.write_mock_package_at(package)
        before={p.relative_to(package):p.read_bytes() for p in package.rglob('*') if p.is_file()}
        self.assertFalse(patch_graphify_dir(package))
        self.assertEqual(before,{p.relative_to(package):p.read_bytes() for p in package.rglob('*') if p.is_file()})

    def test_patch_refuses_symlinked_environment_and_parent(self):
        from scripts.setup_graphify_apx import patch_graphify_dir
        for parent in ('environment','lib'):
            with self.subTest(parent=parent):
                project=self.test_root/parent; project.mkdir()
                shared=self.test_root/(parent+'-shared'); self.write_mock_package_at(shared/'graphify')
                env=project/'.venv-graphify'
                if parent=='environment': env.symlink_to(shared,target_is_directory=True); package=env/'graphify'
                else:
                    env.mkdir(); (env/'lib').symlink_to(shared,target_is_directory=True); package=env/'lib'/'graphify'
                self.assertFalse(patch_graphify_dir(package,repo_root=project))

    def test_setup_rejects_noncanonical_target_before_installing(self):
        from scripts.graphify_project import setup_graphify
        with mock.patch('scripts.graphify_project.subprocess.run') as run:
            self.assertEqual(setup_graphify(self.proj_a,self.proj_a/'scratch'/'other-env'),1)
        run.assert_not_called()

    def test_setup_refuses_external_environment_directory_links_before_installing(self):
        from scripts.graphify_project import setup_graphify
        env=project_graphify_environment(self.proj_a); env.mkdir()
        (env/'lib').symlink_to(self.global_dir,target_is_directory=True)
        with mock.patch('scripts.graphify_project.subprocess.run') as run:
            self.assertEqual(setup_graphify(self.proj_a),1)
        run.assert_not_called()

    def test_existing_graph_without_local_environment_needs_attention(self):
        graph=self.proj_a/'graphify-out';graph.mkdir()
        (graph/'graph.json').write_text('{"nodes":[]}')
        self.assertEqual(inspect_graphify(self.proj_a)['status'],'stale')

    def test_extract_requires_output_and_resolves_relative_output_from_project_root(self):
        from scripts.graphify_project import extract_graphify
        pkg=self.write_mock_env(project_graphify_environment(self.proj_a))
        from scripts.setup_graphify_apx import patch_graphify_dir
        self.assertTrue(patch_graphify_dir(pkg,repo_root=self.proj_a))
        with mock.patch('scripts.graphify_project.subprocess.run',return_value=mock.Mock(returncode=0)) as run:
            with redirect_stderr(io.StringIO()):
                self.assertEqual(extract_graphify(self.proj_a,['--out','graphify-out']),1)
            run.assert_called_once()
            self.assertNotIn('--force',run.call_args.args[0])
    def setUp(self) -> None:
        scratch = REPO_ROOT / "scratch"
        scratch.mkdir(exist_ok=True)
        self.test_root = Path(tempfile.mkdtemp(prefix="graphify-project-test.", dir=scratch))
        self.proj_a = self.test_root / "proj_a"
        self.proj_b = self.test_root / "proj_b"
        self.global_dir = self.test_root / "global_graphify"
        self.proj_a.mkdir()
        self.proj_b.mkdir()
        self.global_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.test_root, ignore_errors=True)

    def write_mock_package_at(self, base: Path) -> None:
        (base / "extractors").mkdir(parents=True, exist_ok=True)
        (base / "__init__.py").write_text("# mock graphify\n", encoding="utf-8")
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

    def write_mock_env(
        self,
        env_path: Path,
        python_version: str = "3.12.3",
        graphify_version: str = "0.9.75",
        tree_sitter_sql_version: str = "0.3.11",
        include_py: bool = True,
        include_graphify_bin: bool = True,
        include_graphify_pkg: bool = True,
        include_dist_info: bool = True,
    ) -> Path:
        bin_dir = env_path / ("Scripts" if os.name == "nt" else "bin")
        bin_dir.mkdir(parents=True, exist_ok=True)
        if include_py:
            py_exe = bin_dir / ("python.exe" if os.name == "nt" else "python")
            py_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            py_exe.chmod(0o755)
        if include_graphify_bin:
            g_exe = bin_dir / ("graphify.exe" if os.name == "nt" else "graphify")
            g_exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            g_exe.chmod(0o755)

        (env_path / "pyvenv.cfg").write_text(
            f"version = {python_version}\nversion_info = {python_version[:4]}\n",
            encoding="utf-8",
        )

        sp_rel = "Lib/site-packages" if os.name == "nt" else "lib/python3.12/site-packages"
        sp_dir = env_path / sp_rel
        sp_dir.mkdir(parents=True, exist_ok=True)

        if include_dist_info:
            if graphify_version:
                g_dist = sp_dir / f"graphifyy-{graphify_version}.dist-info"
                g_dist.mkdir(parents=True, exist_ok=True)
                (g_dist / "METADATA").write_text(
                    f"Metadata-Version: 2.1\nName: graphifyy\nVersion: {graphify_version}\n",
                    encoding="utf-8",
                )
            if tree_sitter_sql_version:
                ts_dist = sp_dir / f"tree_sitter_sql-{tree_sitter_sql_version}.dist-info"
                ts_dist.mkdir(parents=True, exist_ok=True)
                (ts_dist / "METADATA").write_text(
                    f"Metadata-Version: 2.1\nName: tree_sitter_sql\nVersion: {tree_sitter_sql_version}\n",
                    encoding="utf-8",
                )

        lib_dir = sp_dir / "graphify"
        if include_graphify_pkg:
            lib_dir.mkdir(parents=True, exist_ok=True)
            self.write_mock_package_at(lib_dir)
        return lib_dir

    def test_project_graphify_environment_returns_dot_venv_graphify(self) -> None:
        expected = self.proj_a / ".venv-graphify"
        self.assertEqual(project_graphify_environment(self.proj_a), expected)

    def test_inspect_graphify_absent_when_environment_missing(self) -> None:
        result = inspect_graphify(self.proj_a)
        self.assertEqual(result["status"], "absent")
        self.assertIn("reasons", result)
        self.assertTrue(any("not configured" in r or "absent" in r for r in result["reasons"]))
        self.assertFalse(result.get("is_configured", False))

    def test_inspect_graphify_incompatible_on_missing_required_files(self) -> None:
        env_path = project_graphify_environment(self.proj_a)
        pkg_dir = self.write_mock_env(env_path)
        # Remove detect.py to corrupt package
        (pkg_dir / "detect.py").unlink()

        result = inspect_graphify(self.proj_a)
        self.assertEqual(result["status"], "incompatible")
        self.assertTrue(any("detect.py" in r or "missing" in r for r in result["reasons"]))

    def test_inspect_graphify_stale_when_unpatched_or_extractor_differs(self) -> None:
        env_path = project_graphify_environment(self.proj_a)
        self.write_mock_env(env_path)

        result = inspect_graphify(self.proj_a)
        self.assertEqual(result["status"], "stale")
        self.assertTrue(any("not registered" in r or "extractor" in r or "stale" in r for r in result["reasons"]))

    def test_inspect_graphify_stale_when_ast_cache_is_stale(self) -> None:
        env_path = project_graphify_environment(self.proj_a)
        pkg_dir = self.write_mock_env(env_path)

        # Patch package properly
        from scripts.setup_graphify_apx import patch_graphify_dir
        self.assertTrue(patch_graphify_dir(pkg_dir,repo_root=self.proj_a))

        # Create stale AST cache
        cache_dir = self.proj_a / "graphify-out" / "cache" / "ast" / "v1"
        cache_dir.mkdir(parents=True)
        stale_file = cache_dir / "old.json"
        stale_file.write_text(
            json.dumps({"nodes": [{"source_file": "apps/APP/101/p1.apx"}]}),
            encoding="utf-8",
        )
        os.utime(stale_file, (1000, 1000))

        result = inspect_graphify(self.proj_a)
        self.assertEqual(result["status"], "stale")
        self.assertTrue(any("stale" in r.lower() or "cache" in r.lower() for r in result["reasons"]))

    def test_inspect_graphify_current_when_patched_and_clean(self) -> None:
        env_path = project_graphify_environment(self.proj_a)
        pkg_dir = self.write_mock_env(env_path)

        from scripts.setup_graphify_apx import patch_graphify_dir
        self.assertTrue(patch_graphify_dir(pkg_dir,repo_root=self.proj_a))

        result = inspect_graphify(self.proj_a)
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["reasons"], [])

    def test_two_project_isolation_and_shared_inode_regression(self) -> None:
        """CRITICAL: Test that patching Project A NEVER mutates Project B or global install,
        even when package files are hardlinked (sharing the same inode) across environments!
        """
        # Create mock global package
        global_pkg = self.global_dir / "graphify"
        self.write_mock_package_at(global_pkg)

        # Create proj_a and proj_b envs
        env_a = project_graphify_environment(self.proj_a)
        env_b = project_graphify_environment(self.proj_b)
        pkg_a = env_a / "lib/python3.12/site-packages/graphify"
        pkg_b = env_b / "lib/python3.12/site-packages/graphify"
        (pkg_a / "extractors").mkdir(parents=True)
        (pkg_b / "extractors").mkdir(parents=True)

        for filename in ("detect.py", "extract.py", "__init__.py"):
            src = global_pkg / filename
            target_a = pkg_a / filename
            target_b = pkg_b / filename
            os.link(src, target_a)
            os.link(src, target_b)

        # Assert files share inode
        self.assertEqual(os.stat(global_pkg / "detect.py").st_ino, os.stat(pkg_a / "detect.py").st_ino)
        self.assertEqual(os.stat(pkg_a / "detect.py").st_ino, os.stat(pkg_b / "detect.py").st_ino)
        self.assertEqual(os.stat(pkg_a / "detect.py").st_nlink, 3)

        global_detect_before = (global_pkg / "detect.py").read_bytes()
        global_extract_before = (global_pkg / "extract.py").read_bytes()
        b_detect_before = (pkg_b / "detect.py").read_bytes()
        b_extract_before = (pkg_b / "extract.py").read_bytes()

        # Patch Project A
        from scripts.setup_graphify_apx import patch_graphify_dir
        self.assertTrue(patch_graphify_dir(pkg_a,repo_root=self.proj_a))

        # Project A should be patched and verified
        self.assertTrue(verify_installation(pkg_a)[0])

        # Project B and Global MUST be 100% byte-identical to before!
        self.assertEqual((global_pkg / "detect.py").read_bytes(), global_detect_before)
        self.assertEqual((global_pkg / "extract.py").read_bytes(), global_extract_before)
        self.assertEqual((pkg_b / "detect.py").read_bytes(), b_detect_before)
        self.assertEqual((pkg_b / "extract.py").read_bytes(), b_extract_before)

        # Inode of Project A must have decoupled (atomic replacement broke the link)
        self.assertNotEqual(os.stat(pkg_a / "detect.py").st_ino, os.stat(pkg_b / "detect.py").st_ino)
        self.assertEqual(os.stat(pkg_b / "detect.py").st_ino, os.stat(global_pkg / "detect.py").st_ino)

        # Verify on B should NOT change anything
        b_verified, b_reason = verify_installation(pkg_b)
        self.assertFalse(b_verified)
        self.assertEqual((pkg_b / "detect.py").read_bytes(), b_detect_before)
        self.assertEqual((pkg_b / "extract.py").read_bytes(), b_extract_before)

    def test_refuses_symlink_escape_for_environment(self) -> None:
        """Reject local environment path or symlink pointing elsewhere."""
        env_a = project_graphify_environment(self.proj_a)
        outside = self.test_root / "outside_env"
        self.write_mock_env(outside)
        env_a.symlink_to(outside, target_is_directory=True)

        result = inspect_graphify(self.proj_a)
        self.assertEqual(result["status"], "incompatible")
        self.assertTrue(any("symbolic link" in r or "outside" in r for r in result["reasons"]))

        # CLI setup must refuse
        with redirect_stderr(io.StringIO()):
            rc = graphify_project_main(["--root", str(self.proj_a), "setup"])
        self.assertNotEqual(rc, 0)

    def test_cli_verify_absent_is_informational_exit_0(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = graphify_project_main(["--root", str(self.proj_a), "verify"])
        self.assertEqual(rc, 0)
        self.assertIn("INFO", out.getvalue())
        self.assertIn("absent", out.getvalue().lower())

    def test_cli_verify_stale_is_attention_exit_1(self) -> None:
        env_path = project_graphify_environment(self.proj_a)
        self.write_mock_env(env_path)

        out = io.StringIO()
        with redirect_stdout(out):
            rc = graphify_project_main(["--root", str(self.proj_a), "verify"])
        self.assertEqual(rc, 1)
        self.assertIn("ATTENTION", out.getvalue())
        self.assertIn("stale", out.getvalue().lower())

    def test_cli_verify_current_is_ok_exit_0(self) -> None:
        env_path = project_graphify_environment(self.proj_a)
        pkg_dir = self.write_mock_env(env_path)
        from scripts.setup_graphify_apx import patch_graphify_dir
        self.assertTrue(patch_graphify_dir(pkg_dir,repo_root=self.proj_a))

        out = io.StringIO()
        with redirect_stdout(out):
            rc = graphify_project_main(["--root", str(self.proj_a), "verify"])
        self.assertEqual(rc, 0)
        self.assertIn("OK", out.getvalue())

    def test_cli_extract_refuses_when_absent_or_stale(self) -> None:
        # Absent
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = graphify_project_main(["--root", str(self.proj_a), "extract"])
        self.assertEqual(rc, 1)
        self.assertIn("setup", (out.getvalue() + err.getvalue()).lower())

        # Stale
        env_path = project_graphify_environment(self.proj_a)
        # Unpatched environment is not package-ready, so extract refuses
        self.write_mock_env(env_path)
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = graphify_project_main(["--root", str(self.proj_a), "extract"])
        self.assertEqual(rc, 1)
        self.assertIn("setup", (out.getvalue() + err.getvalue()).lower())

    def test_inspect_graphify_incompatible_when_env_exists_but_package_missing(self) -> None:
        env_path = project_graphify_environment(self.proj_a)
        self.write_mock_env(env_path, include_graphify_pkg=False)
        result = inspect_graphify(self.proj_a)
        self.assertEqual(result["status"], "incompatible")
        self.assertTrue(any("not installed" in r.lower() or "missing" in r.lower() for r in result["reasons"]))

    def test_inspect_graphify_incompatible_when_launchers_missing(self) -> None:
        # Python launcher missing
        env_path = project_graphify_environment(self.proj_a)
        self.write_mock_env(env_path, include_py=False)
        result = inspect_graphify(self.proj_a)
        self.assertEqual(result["status"], "incompatible")
        self.assertTrue(any("python" in r.lower() or "interpreter" in r.lower() or "launcher" in r.lower() for r in result["reasons"]))

        # Graphify CLI launcher missing
        env_path2 = project_graphify_environment(self.proj_b)
        self.write_mock_env(env_path2, include_graphify_bin=False)
        result2 = inspect_graphify(self.proj_b)
        self.assertEqual(result2["status"], "incompatible")
        self.assertTrue(any("graphify" in r.lower() or "launcher" in r.lower() for r in result2["reasons"]))

    def test_inspect_graphify_incompatible_on_python_version_prerequisite(self) -> None:
        env_path = project_graphify_environment(self.proj_a)
        self.write_mock_env(env_path, python_version="3.11.8")
        result = inspect_graphify(self.proj_a)
        self.assertEqual(result["status"], "incompatible")
        self.assertTrue(any("3.12" in r for r in result["reasons"]))

    def test_inspect_graphify_incompatible_on_pinned_version_mismatch(self) -> None:
        env_path = project_graphify_environment(self.proj_a)
        self.write_mock_env(env_path, graphify_version="0.9.74")
        result = inspect_graphify(self.proj_a)
        self.assertEqual(result["status"], "incompatible")
        self.assertTrue(any("version" in r.lower() or "0.9.75" in r for r in result["reasons"]))

    def test_inspect_graphify_incompatible_on_missing_sql_dependency(self) -> None:
        env_path = project_graphify_environment(self.proj_a)
        self.write_mock_env(env_path, tree_sitter_sql_version="")
        result = inspect_graphify(self.proj_a)
        self.assertEqual(result["status"], "incompatible")
        self.assertTrue(any("tree-sitter-sql" in r.lower() or "dependency" in r.lower() for r in result["reasons"]))

    def test_inspect_graphify_stale_when_domain_files_missing_from_graph(self) -> None:
        env_path = project_graphify_environment(self.proj_a)
        pkg_dir = self.write_mock_env(env_path)
        from scripts.setup_graphify_apx import patch_graphify_dir
        self.assertTrue(patch_graphify_dir(pkg_dir,repo_root=self.proj_a))

        (self.proj_a / "apps" / "DEMO" / "101" / "pages").mkdir(parents=True)
        (self.proj_a / "apps" / "DEMO" / "101" / "pages" / "p1.apx").write_text("page 1 ()", encoding="utf-8")
        (self.proj_a / "database").mkdir(parents=True)
        (self.proj_a / "database" / "orders.sql").write_text("create table t(id int);", encoding="utf-8")
        (self.proj_a / "app_context").mkdir(parents=True)
        (self.proj_a / "app_context" / "domain.md").write_text("# Domain", encoding="utf-8")

        graph_dir = self.proj_a / "graphify-out"
        graph_dir.mkdir(parents=True)
        graph_file = graph_dir / "graph.json"
        graph_file.write_text(
            json.dumps({
                "nodes": [
                    {"id": "apps_demo_101_pages_p1", "source_file": "apps/DEMO/101/pages/p1.apx"},
                ],
                "edges": []
            }),
            encoding="utf-8"
        )

        result = inspect_graphify(self.proj_a)
        self.assertEqual(result["status"], "stale")
        self.assertTrue(result.get("is_package_ready", False))
        self.assertFalse(result.get("is_graph_current", True))
        self.assertTrue(any("missing" in r.lower() for r in result["reasons"]))

        # Now include all domain files in graph
        graph_file.write_text(
            json.dumps({
                "nodes": [
                    {"id": "apps_demo_101_pages_p1", "source_file": "apps/DEMO/101/pages/p1.apx"},
                    {"id": "database_orders", "source_file": "database/orders.sql"},
                    {"id": "app_context_domain", "source_file": "app_context/domain.md"},
                ],
                "edges": []
            }),
            encoding="utf-8"
        )
        result2 = inspect_graphify(self.proj_a)
        self.assertEqual(result2["status"], "current")
        self.assertTrue(result2.get("is_graph_current", False))

    def test_inspect_graphify_stale_when_graph_schema_malformed(self) -> None:
        env_path = project_graphify_environment(self.proj_a)
        pkg_dir = self.write_mock_env(env_path)
        from scripts.setup_graphify_apx import patch_graphify_dir
        self.assertTrue(patch_graphify_dir(pkg_dir,repo_root=self.proj_a))

        graph_dir = self.proj_a / "graphify-out"
        graph_dir.mkdir(parents=True)
        graph_file = graph_dir / "graph.json"
        graph_file.write_text(json.dumps({"nodes": [{"missing_id_and_source": 1}]}), encoding="utf-8")

        result = inspect_graphify(self.proj_a)
        self.assertEqual(result["status"], "stale")
        self.assertTrue(any("schema" in r.lower() or "node" in r.lower() for r in result["reasons"]))

    def test_inspect_graphify_stale_when_ast_cache_malformed_fails_closed(self) -> None:
        env_path = project_graphify_environment(self.proj_a)
        pkg_dir = self.write_mock_env(env_path)
        from scripts.setup_graphify_apx import patch_graphify_dir
        self.assertTrue(patch_graphify_dir(pkg_dir,repo_root=self.proj_a))

        cache_dir = self.proj_a / "graphify-out" / "cache" / "ast" / "v1"
        cache_dir.mkdir(parents=True)
        corrupt_file = cache_dir / "corrupt.json"
        corrupt_file.write_text("{malformed json", encoding="utf-8")

        result = inspect_graphify(self.proj_a)
        self.assertEqual(result["status"], "stale")
        self.assertTrue(any("malformed" in r.lower() or "cache" in r.lower() for r in result["reasons"]))

    def test_extract_allowed_when_graph_stale_if_package_ready(self) -> None:
        env_path = project_graphify_environment(self.proj_a)
        pkg_dir = self.write_mock_env(env_path)
        from scripts.setup_graphify_apx import patch_graphify_dir
        self.assertTrue(patch_graphify_dir(pkg_dir,repo_root=self.proj_a))

        # Create eligible domain file so graph is required
        (self.proj_a / "apps" / "DEMO" / "101" / "pages").mkdir(parents=True)
        (self.proj_a / "apps" / "DEMO" / "101" / "pages" / "p1.apx").write_text("page 1 ()", encoding="utf-8")

        # Stale graph (no graph yet), but package is ready
        inspection = inspect_graphify(self.proj_a)
        self.assertEqual(inspection["status"], "stale")
        self.assertTrue(inspection["is_package_ready"])

        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            from scripts.graphify_project import extract_graphify
            rc = extract_graphify(self.proj_a, [])
            self.assertEqual(rc, 1)  # Successful process exit alone does not prove a graph was produced.
            mock_run.assert_called()
            self.assertNotIn("--force",mock_run.call_args.args[0])

    def test_extract_refuses_target_and_escaping_output_args(self) -> None:
        with redirect_stderr(io.StringIO()):
            rc = graphify_project_main(["--root", str(self.proj_a), "--target", "/some/env", "extract"])
        self.assertEqual(rc, 1)

        with redirect_stderr(io.StringIO()):
            rc2 = graphify_project_main(["--root", str(self.proj_a), "extract", "--", "--output", "/tmp/escape"])
        self.assertEqual(rc2, 1)

    def test_hardlink_regression_with_preexisting_apexlang_and_rollback(self) -> None:
        """Shared-inode test with pre-existing apexlang.py AND failed verification rollback."""
        global_pkg = self.global_dir / "graphify"
        self.write_mock_package_at(global_pkg)
        (global_pkg / "extractors" / "apexlang.py").write_text("# preexisting apexlang\n", encoding="utf-8")

        env_a = project_graphify_environment(self.proj_a)
        env_b = project_graphify_environment(self.proj_b)
        pkg_a = env_a / "lib/python3.12/site-packages/graphify"
        pkg_b = env_b / "lib/python3.12/site-packages/graphify"
        (pkg_a / "extractors").mkdir(parents=True)
        (pkg_b / "extractors").mkdir(parents=True)

        for filename in ("detect.py", "extract.py", "__init__.py"):
            os.link(global_pkg / filename, pkg_a / filename)
            os.link(global_pkg / filename, pkg_b / filename)
        os.link(global_pkg / "extractors" / "apexlang.py", pkg_a / "extractors" / "apexlang.py")
        os.link(global_pkg / "extractors" / "apexlang.py", pkg_b / "extractors" / "apexlang.py")

        b_before = {
            "detect": (pkg_b / "detect.py").read_bytes(),
            "extract": (pkg_b / "extract.py").read_bytes(),
            "apexlang": (pkg_b / "extractors" / "apexlang.py").read_bytes(),
        }
        global_before = {
            "detect": (global_pkg / "detect.py").read_bytes(),
            "extract": (global_pkg / "extract.py").read_bytes(),
            "apexlang": (global_pkg / "extractors" / "apexlang.py").read_bytes(),
        }

        # Failed verification triggers rollback in Project A
        from scripts.setup_graphify_apx import patch_graphify_dir
        with mock.patch("scripts.setup_graphify_apx.verify_installation", return_value=(False, "simulated verification failure")):
            res = patch_graphify_dir(pkg_a,repo_root=self.proj_a)
            self.assertFalse(res)

        # After rollback, Project B and global must remain 100% byte-identical
        self.assertEqual((pkg_b / "detect.py").read_bytes(), b_before["detect"])
        self.assertEqual((pkg_b / "extract.py").read_bytes(), b_before["extract"])
        self.assertEqual((pkg_b / "extractors" / "apexlang.py").read_bytes(), b_before["apexlang"])
        self.assertEqual((global_pkg / "detect.py").read_bytes(), global_before["detect"])
        self.assertEqual((global_pkg / "extract.py").read_bytes(), global_before["extract"])
        self.assertEqual((global_pkg / "extractors" / "apexlang.py").read_bytes(), global_before["apexlang"])


if __name__ == "__main__":
    unittest.main()
