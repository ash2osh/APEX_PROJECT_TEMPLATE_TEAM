"""Regression coverage for exported identifiers and upload MIME patterns."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "graphify_parser_regressions", ROOT / "scripts/graphify_apexlang_extractor.py"
)
assert SPEC and SPEC.loader
EXTRACTOR = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = EXTRACTOR
SPEC.loader.exec_module(EXTRACTOR)


class GraphifyParserRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(prefix="graphify-parser-regression.")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)

    def extract(self, text: str, relative: str = "apps/DEMO/102/pages/p00004-home.apx") -> dict:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return EXTRACTOR.extract_apexlang(path)

    def test_unquoted_arabic_columns_do_not_hide_later_process_writes(self) -> None:
        result = self.extract(
            "page 4 (\n"
            "    region التقرير (\n"
            "        column القسم (\n"
            "            heading: القسم\n"
            "        )\n"
            "    )\n"
            "    process سجل (\n"
            "        plsqlCode: insert into audit_log(id) values(1);\n"
            "    )\n"
            ")\n"
        )
        self.assertNotIn("error", result)
        self.assertTrue(any(n["label"] == "Region: التقرير" for n in result["nodes"]))
        self.assertTrue(any(e["relation"] == "writes_to" for e in result["edges"]))

    def test_apex_dollar_columns_do_not_unbalance_the_page(self) -> None:
        result = self.extract(
            "page 4 (\n"
            "    region grid (\n"
            "        column APEX$ROW_ACTION (\n"
            "        )\n"
            "        column DERIVED$01 (\n"
            "        )\n"
            "    )\n"
            "    process after-grid (\n"
            "        plsqlCode: delete from audit_log;\n"
            "    )\n"
            ")\n"
        )
        self.assertNotIn("error", result)
        self.assertTrue(any(e["relation"] == "writes_to" for e in result["edges"]))

    def test_different_arabic_shared_components_keep_distinct_reference_targets(self) -> None:
        declarations = self.extract(
            'lov "القسم" (\n)\nlov "الموظف" (\n)\n',
            "apps/DEMO/102/shared-components/lovs.apx",
        )
        components = {n["label"]: n["id"] for n in declarations["nodes"] if n.get("metadata", {}).get("component_type") == "lov"}
        self.assertEqual(2, len(components))
        self.assertNotEqual(components["LOV: القسم"], components["LOV: الموظف"])
        references = self.extract(
            "page 4 (\n"
            "    region grid (\n"
            "        namedLov: @القسم\n"
            "        namedLov: @الموظف\n"
            "    )\n"
            ")\n"
        )
        targets = {e["target"] for e in references["edges"] if e["relation"] == "references_component"}
        self.assertEqual(set(components.values()), targets)

    def test_unicode_normalization_preserves_shared_component_link(self) -> None:
        for declaration, reference in (("café", "cafe\u0301"), ("İ", "i\u0307"), ("Straße", "STRASSE"), ("ᾴ", "α\u0345\u0301")):
            with self.subTest(declaration=declaration, reference=reference):
                declarations = self.extract(f'lov "{declaration}" (\n)\n', "apps/DEMO/102/shared-components/lovs.apx")
                references = self.extract(f'page 4 (\n    namedLov: @{reference}\n)\n')
                component = next(n for n in declarations["nodes"] if n.get("metadata", {}).get("component_type") == "lov")
                target = next(e["target"] for e in references["edges"] if e["relation"] == "references_component")
                self.assertEqual(component["id"], target)

    def test_upload_mime_wildcards_do_not_hide_following_process(self) -> None:
        for value in ("image/*", "application/*", "*/*", "image/png,image/*", "image/* ,application/pdf", "image/* , application/pdf"):
            with self.subTest(value=value):
                result = self.extract(
                    "page 4 (\n"
                    "    pageItem P4_FILE (\n"
                    "        storage {\n"
                    f"            fileTypes: {value}\n"
                    "        }\n"
                    "    )\n"
                    "    process save-file (\n"
                    "        plsqlCode: insert into attachments(id) values(1);\n"
                    "    )\n"
                    ")\n"
                )
                self.assertNotIn("error", result)
                self.assertTrue(any(e["relation"] == "writes_to" for e in result["edges"]))

    def test_real_comments_still_work_after_mime_wildcards(self) -> None:
        result = self.extract(
            "page 4 (\n"
            "    pageItem P4_FILE (\n"
            "        fileTypes: image/* /* real comment starts\n"
            "        region fake (\n"
            "        comment ends */\n"
            "    )\n"
            "    /* region another-fake ( */\n"
            "    region real (\n"
            "        sqlQuery: select id from orders /* sql comment */\n"
            "    )\n"
            ")\n"
        )
        self.assertNotIn("error", result)
        self.assertFalse(any("fake" in n["label"] for n in result["nodes"]))
        self.assertTrue(any(e["relation"] == "reads_from" for e in result["edges"]))

    def test_mime_pattern_in_exported_comments_does_not_hide_following_process(self) -> None:
        result = self.extract(
            "page 4 (\n"
            "    pageItem P4_FILE (\n"
            "        comments {\n"
            "            comments: image/*,application/pdf\n"
            "        }\n"
            "    )\n"
            "    process save-file (\n"
            "        plsqlCode: insert into attachments(id) values(1);\n"
            "    )\n"
            ")\n"
        )
        self.assertNotIn("error", result)
        self.assertTrue(any(e["relation"] == "writes_to" for e in result["edges"]))

    def test_exported_column_expression_keeps_the_region_frame(self) -> None:
        result = self.extract(
            "page 4 (\n"
            "    region departments (\n"
            "        column D.DEPARTMENT||'-'||D.PROJETS (\n"
            "        )\n"
            "    )\n"
            "    process after-report (\n"
            "        plsqlCode: delete from departments_stg;\n"
            "    )\n"
            ")\n"
        )
        self.assertNotIn("error", result)
        self.assertTrue(any(e["relation"] == "writes_to" for e in result["edges"]))

    def test_unterminated_quoted_identifier_is_still_reported(self) -> None:
        result = self.extract('page 4 (\n    region "broken (\n    )\n)\n')
        self.assertIn("error", result)

    def test_adjacent_block_comments_do_not_emit_fake_regions(self) -> None:
        for value in ("fileTypes: image/png/* comment starts", "comments: note/* comment starts"):
            with self.subTest(value=value):
                result = self.extract(
                    "page 4 (\n"
                    f"    {value}\n"
                    "    region fake (\n"
                    "    )\n"
                    "    comment ends */\n"
                    "    region real (\n"
                    "        sqlQuery: select id from orders\n"
                    "    )\n"
                    ")\n"
                )
                self.assertNotIn("error", result)
                regions = [n["label"] for n in result["nodes"] if n.get("metadata", {}).get("component_type") == "region"]
                self.assertEqual(["Region: real"], regions)


if __name__ == "__main__":
    unittest.main()
