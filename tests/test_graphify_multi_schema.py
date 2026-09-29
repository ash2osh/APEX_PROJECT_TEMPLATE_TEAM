#!/usr/bin/env python3
"""DatabaseMirror across several schemas and synonyms."""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "scripts" / "graphify_apexlang_extractor.py"


@unittest.skipUnless(MODULE_PATH.is_file(), "canonical extractor is missing")
class MultiSchemaMirrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location("graphify_apexlang_extractor_multi", MODULE_PATH)
        assert spec and spec.loader
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)

    def setUp(self) -> None:
        self.module.DatabaseMirror.clear_cache()
        self.root = Path(tempfile.mkdtemp(prefix="graphify-multi."))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def mirror(self, schema: str):
        return self.module.DatabaseMirror(self.root, schema)

    def node_id(self, relative: str, schema: str, name: str) -> str:
        make_id = self.module.make_id
        return make_id(make_id(relative.removesuffix(".sql")), schema, name)

    def test_qualified_table_resolves_in_the_schema_it_names(self) -> None:
        self.write("database/OTHER/tables/WIDGETS.sql", 'CREATE TABLE "OTHER"."WIDGETS" ("ID" NUMBER);\n')
        self.write("database/DEMO/tables/USERS.sql", 'CREATE TABLE "DEMO"."USERS" ("ID" NUMBER);\n')
        mirror = self.mirror("DEMO")
        self.assertEqual(self.node_id("database/OTHER/tables/WIDGETS.sql", "OTHER", "WIDGETS"), mirror.table("OTHER.WIDGETS"))
        self.assertEqual(self.node_id("database/DEMO/tables/USERS.sql", "DEMO", "USERS"), mirror.table("USERS"))
        self.assertEqual(mirror.table("USERS"), mirror.table("DEMO.USERS"))

    def test_an_unqualified_name_does_not_leak_into_another_schema(self) -> None:
        self.write("database/OTHER/tables/WIDGETS.sql", 'CREATE TABLE "OTHER"."WIDGETS" ("ID" NUMBER);\n')
        self.write("database/DEMO/tables/USERS.sql", 'CREATE TABLE "DEMO"."USERS" ("ID" NUMBER);\n')
        self.assertIsNone(self.mirror("DEMO").table("WIDGETS"))

    def test_unmirrored_schema_or_object_stays_unresolved(self) -> None:
        self.write("database/DEMO/tables/USERS.sql", 'CREATE TABLE "DEMO"."USERS" ("ID" NUMBER);\n')
        mirror = self.mirror("DEMO")
        self.assertIsNone(mirror.table("NOPE.THING"))
        self.assertIsNone(mirror.table("DEMO.MISSING"))
        self.assertIsNone(mirror.table("A.B.C"))

    def test_a_synonym_resolves_to_the_target_schemas_table_in_one_hop(self) -> None:
        self.write("database/OTHER/tables/WIDGETS.sql", 'CREATE TABLE "OTHER"."WIDGETS" ("ID" NUMBER);\n')
        self.write("database/DEMO/synonyms/WIDGET_SYN.sql", '\n  CREATE OR REPLACE EDITIONABLE SYNONYM "DEMO"."WIDGET_SYN" FOR "OTHER"."WIDGETS";\n')
        mirror = self.mirror("DEMO")
        expected = self.node_id("database/OTHER/tables/WIDGETS.sql", "OTHER", "WIDGETS")
        self.assertEqual(expected, mirror.table("WIDGET_SYN"))
        self.assertEqual(expected, mirror.table("DEMO.WIDGET_SYN"))

    def test_an_unqualified_synonym_target_stays_in_the_synonyms_schema(self) -> None:
        self.write("database/DEMO/tables/USERS.sql", 'CREATE TABLE "DEMO"."USERS" ("ID" NUMBER);\n')
        self.write("database/DEMO/synonyms/PEOPLE.sql", 'CREATE OR REPLACE SYNONYM "DEMO"."PEOPLE" FOR "USERS";\n')
        self.assertEqual(self.mirror("DEMO").table("USERS"), self.mirror("DEMO").table("PEOPLE"))

    def test_a_database_link_synonym_resolves_to_nothing(self) -> None:
        self.write("database/OTHER/tables/WIDGETS.sql", 'CREATE TABLE "OTHER"."WIDGETS" ("ID" NUMBER);\n')
        self.write("database/DEMO/synonyms/REMOTE_W.sql", 'CREATE OR REPLACE SYNONYM "DEMO"."REMOTE_W" FOR "OTHER"."WIDGETS"@"REMOTE_LINK";\n')
        self.assertIsNone(self.mirror("DEMO").table("REMOTE_W"))

    def test_a_synonym_is_followed_only_one_hop(self) -> None:
        self.write("database/OTHER/tables/WIDGETS.sql", 'CREATE TABLE "OTHER"."WIDGETS" ("ID" NUMBER);\n')
        self.write("database/OTHER/synonyms/W1.sql", 'CREATE OR REPLACE SYNONYM "OTHER"."W1" FOR "OTHER"."WIDGETS";\n')
        self.write("database/DEMO/synonyms/W2.sql", 'CREATE OR REPLACE SYNONYM "DEMO"."W2" FOR "OTHER"."W1";\n')
        self.assertIsNone(self.mirror("DEMO").table("W2"))

    def test_packages_resolve_by_schema_and_through_synonyms(self) -> None:
        self.write("database/OTHER/packages/OTHER_PKG_SPEC.sql", 'CREATE OR REPLACE PACKAGE "OTHER"."OTHER_PKG" AS\nEND;\n')
        self.write("database/DEMO/synonyms/PKG_SYN.sql", 'CREATE OR REPLACE SYNONYM "DEMO"."PKG_SYN" FOR "OTHER"."OTHER_PKG";\n')
        mirror = self.mirror("DEMO")
        expected = self.module.make_id("database/OTHER/packages/OTHER_PKG_SPEC")
        self.assertEqual(expected, mirror.package("OTHER.OTHER_PKG.DO_IT"))
        self.assertEqual(expected, mirror.package("PKG_SYN.DO_IT"))
        self.assertIsNone(mirror.package("OTHER_PKG.DO_IT"))

    def test_the_index_is_built_once_per_root(self) -> None:
        self.write("database/DEMO/tables/USERS.sql", 'CREATE TABLE "DEMO"."USERS" ("ID" NUMBER);\n')
        scans = []
        original = self.module.DatabaseMirror._scan_root
        self.module.DatabaseMirror._scan_root = staticmethod(lambda root: scans.append(root) or original(root))
        self.addCleanup(setattr, self.module.DatabaseMirror, "_scan_root", original)
        self.mirror("DEMO").table("USERS")
        self.mirror("DEMO").table("USERS")
        self.module.DatabaseMirror(self.root, "OTHER").table("X")
        self.assertEqual(1, len(scans))

    def test_a_mirror_without_a_root_resolves_nothing(self) -> None:
        mirror = self.module.DatabaseMirror()
        self.assertIsNone(mirror.table("USERS"))
        self.assertIsNone(mirror.package("PKG.FN"))

    def test_the_schema_of_an_application_file_is_still_taken_from_its_path(self) -> None:
        page = self.write("apps/DEMO/102/pages/p00001-home.apx", "page 1 (\n    name: Home\n)\n")
        mirror = self.module.DatabaseMirror.for_application_file(page)
        self.assertEqual("DEMO", mirror.schema)


if __name__ == "__main__":
    unittest.main()
