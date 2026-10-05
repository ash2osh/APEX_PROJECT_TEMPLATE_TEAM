"""Regression tests for exported identifiers and fail-closed graph coverage."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import subprocess
import types
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    if not path.is_file():
        raise AssertionError(f"missing coverage script: {path.name}")
    spec = importlib.util.spec_from_file_location(f"repair_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    with mock.patch.object(sys, "path", [str(ROOT / "scripts"), *sys.path]):
        spec.loader.exec_module(module)
    return module


class ExportedIdentifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.extractor = load_script("graphify_apexlang_extractor")

    def test_exported_identifiers_preserve_following_region_and_sql(self):
        cases = (
            ("column", "APEX$ROW_ACTION"),
            ("column", "APEX$ROW_SELECTOR"),
            ("column", ":APP_SESSION"),
            ("region", "تقرير-المديونية-والتحصيل"),
            ("region", "ٌroom-errors"),
            ("pageItem", "P19_نوع_الرعايه"),
            ("dynamicAction", "refresh-الباقات"),
            ("textMessage", "DIV_STYLEDISPLAYBLOCK_WIDTH110PXبدل_طبيعة_عملDIV"),
            ("column", "'#A01#'"),
            ("region", '"Arabic name الباقات"'),
        )
        path = Path("apps/DEMO/102/pages/p00004-home.apx")
        for kind, identifier in cases:
            with self.subTest(identifier=identifier):
                source = (
                    "page 4 (\n"
                    f"    {kind} {identifier} (\n"
                    "    )\n"
                    "    region after (\n"
                    "        sqlQuery: select id from orders\n"
                    "    )\n"
                    ")\n"
                )
                result = self.extractor.parse_apexlang(source, path)
                after = next(n for n in result["nodes"] if n["label"] == "Region: after")
                reads = {(e["source"], e["target"]) for e in result["edges"] if e["relation"] == "reads_from"}
                self.assertIn((after["id"], "orders"), reads)
                self.assertEqual(after["metadata"]["application_id"], "102")

    def test_literal_parentheses_inside_comments_and_payload_are_not_components(self):
        source = (
            "page 4 (\n"
            "    // region الباقات (\n"
            "    region report (\n"
            "        sqlQuery: ```sql\n"
            "            select '(' as label from orders\n"
            "        ```\n"
            "    )\n"
            ")\n"
        )
        result = self.extractor.parse_apexlang(source, Path("apps/DEMO/102/pages/page.apx"))
        self.assertEqual(1, sum(e["relation"] == "reads_from" for e in result["edges"]))

    def test_unclosed_components_and_unmatched_closes_still_fail(self):
        for source in ("page 4 (\n", "page 4 (\n)\n)\n"):
            with self.subTest(source=source), self.assertRaises(self.extractor.ApexlangParseError):
                self.extractor.parse_apexlang(source, Path("apps/DEMO/102/pages/page.apx"))


class CoverageGateTests(unittest.TestCase):
    def setUp(self):
        self.checker = load_script("check_graphify_apx")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "apps/DEMO/102/pages/page.apx"
        self.path.parent.mkdir(parents=True)
        self.path.write_text("page 4 (\n)\n", encoding="utf-8")
        self.files = [self.path]

    def run_check(self, *, graph=None):
        output = io.StringIO()
        args = ["--root", str(self.root)]
        if graph is not None:
            args.extend(["--graph", str(graph)])
        with mock.patch.object(self.checker, "scan_files", return_value=self.files), redirect_stdout(output):
            status = self.checker.main(args)
        return status, output.getvalue()

    def test_invalid_file_fails_even_when_other_files_parse(self):
        self.path.write_text("page 4 (\n", encoding="utf-8")
        valid = self.path.with_name("valid.apx")
        valid.write_text("page 5 (\n)\n", encoding="utf-8")
        self.files.append(valid)
        status, output = self.run_check()
        self.assertEqual(1, status)
        self.assertIn("unclosed component", output)
        self.assertIn("apps/DEMO/102/pages/page.apx", output)

    def test_missing_file_in_graph_fails(self):
        graph = self.root / "graph.json"
        graph.write_text(json.dumps({"nodes": [], "links": []}), encoding="utf-8")
        status, output = self.run_check(graph=graph)
        self.assertEqual(1, status)
        self.assertIn("missing from graph", output)

    def test_complete_graph_passes_without_changing_files(self):
        graph = self.root / "graph.json"
        graph.write_text(json.dumps({"nodes": [{"source_file": "apps/DEMO/102/pages/page.apx"}], "links": []}), encoding="utf-8")
        before = {p: p.read_bytes() for p in (self.path, graph)}
        status, output = self.run_check(graph=graph)
        self.assertEqual(0, status)
        self.assertIn("1 APEXlang files", output)
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_missing_or_corrupt_graph_fails(self):
        graph = self.root / "graph.json"
        self.assertEqual(1, self.run_check(graph=graph)[0])
        graph.write_text("invalid json", encoding="utf-8")
        self.assertEqual(1, self.run_check(graph=graph)[0])

    def test_unavailable_scan_fails_without_claiming_coverage(self):
        output = io.StringIO()
        with mock.patch.object(self.checker, "scan_files", side_effect=RuntimeError("scan unavailable")), redirect_stdout(output):
            status = self.checker.main(["--root", str(self.root)])
        self.assertEqual(1, status)
        self.assertIn("scan unavailable", output.getvalue())
        self.assertNotIn("OK", output.getvalue())

    def test_scan_selects_only_apx_from_graphify_code_corpus(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({"files": [str(self.path), str(self.path.with_suffix('.sql'))], "walk_errors": []}), "")
        with mock.patch.object(self.checker, "graphify_console_interpreter", return_value=sys.executable), mock.patch.object(self.checker.subprocess, "run", return_value=result) as run:
            self.assertEqual([self.path], self.checker.scan_files(self.root))
        self.assertIn("cache_root=Path(cache)", run.call_args.args[0][3])

    def test_scan_errors_or_invalid_output_fail_closed(self):
        payloads = (
            {"files": [str(self.path)], "walk_errors": ["permission denied"]},
            {"files": [str(self.path)]},
            {"files": [None], "walk_errors": []},
            {"files": [str(self.path)], "walk_errors": None},
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                result = subprocess.CompletedProcess([], 0, json.dumps(payload), "")
                with mock.patch.object(self.checker, "graphify_console_interpreter", return_value=sys.executable), mock.patch.object(self.checker.subprocess, "run", return_value=result), self.assertRaises((RuntimeError, ValueError)):
                    self.checker.scan_files(self.root)

    def test_file_outside_root_is_rejected_before_reading(self):
        outside = self.root.parent / "outside.apx"
        with mock.patch.object(self.checker, "scan_files", return_value=[outside]), redirect_stdout(io.StringIO()):
            self.assertEqual(1, self.checker.main(["--root", str(self.root)]))

    def test_invalid_graph_schema_fails(self):
        graph = self.root / "graph.json"
        for payload in ({}, {"nodes": [None]}, {"nodes": [{"source_file": None}]}):
            with self.subTest(payload=payload):
                graph.write_text(json.dumps(payload), encoding="utf-8")
                self.assertEqual(1, self.run_check(graph=graph)[0])

    def execute_scan(self, result, *, excludes=None, gitignore=True):
        package = types.ModuleType("graphify")
        package.__path__ = []
        detector = types.ModuleType("graphify.detect")
        detector.detect = mock.Mock(return_value=result)
        watch = types.ModuleType("graphify.watch")
        watch._read_build_excludes = mock.Mock(return_value=excludes or [])
        watch._read_build_gitignore = mock.Mock(return_value=gitignore)
        with mock.patch.dict(sys.modules, {"graphify": package, "graphify.detect": detector, "graphify.watch": watch}), mock.patch.object(sys, "argv", ["scan", str(self.root)]), redirect_stdout(io.StringIO()):
            exec(self.checker.SCAN, {})
        return detector.detect, watch

    def test_embedded_scan_rejects_missing_walk_error_metadata(self):
        with self.assertRaises(KeyError):
            self.execute_scan({"files": {"code": []}})

    def test_embedded_scan_honors_persisted_exclusions_and_gitignore(self):
        detect, watch = self.execute_scan({"files": {"code": []}, "walk_errors": []}, excludes=["apps/DEMO/102/pages/ignored.apx"], gitignore=False)
        kwargs = detect.call_args.kwargs
        self.assertEqual(["apps/DEMO/102/pages/ignored.apx"], kwargs.get("extra_excludes"))
        self.assertIs(False, kwargs.get("gitignore"))
        watch._read_build_excludes.assert_called_once_with(self.root / "graphify-out")
        watch._read_build_gitignore.assert_called_once_with(self.root / "graphify-out")

    def test_documented_invocation_does_not_create_local_bytecode(self):
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("check_graphify_apx", "setup_graphify_apx", "graphify_apexlang_extractor"):
            (scripts / f"{name}.py").write_bytes((ROOT / "scripts" / f"{name}.py").read_bytes())
        env = dict(os.environ)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.pop("PYTHONPYCACHEPREFIX", None)
        result = subprocess.run([sys.executable, str(scripts / "check_graphify_apx.py"), "--help"], env=env, capture_output=True, timeout=30)
        self.assertEqual(0, result.returncode)
        self.assertFalse((scripts / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
