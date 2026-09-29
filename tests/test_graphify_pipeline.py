#!/usr/bin/env python3
"""Run the repository extractors through the real Graphify pipeline.

The extractor unit tests cannot see what Graphify does with their output:
same-id nodes from different files are salted apart, and a stub is only
rewired onto a real node when their labels match. These checks build a small
application plus database mirror and inspect the finished graph.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EXTRACTOR = REPO_ROOT / "scripts" / "graphify_apexlang_extractor.py"

FILES = {
    "database/DEMO/tables/USERS.sql": (
        'CREATE TABLE "DEMO"."USERS"\n'
        '   ("ID" NUMBER NOT NULL ENABLE) ;\n'
        'ALTER TABLE "DEMO"."USERS" ADD CONSTRAINT "PK_USERS" PRIMARY KEY ("ID");\n'
    ),
    "database/DEMO/tables/ORDERS.sql": (
        'CREATE TABLE "DEMO"."ORDERS"\n'
        '   ("ID" NUMBER NOT NULL ENABLE, "USER_ID" NUMBER) ;\n'
        'ALTER TABLE "DEMO"."ORDERS" ADD CONSTRAINT "FK_ORDERS_USERS" '
        'FOREIGN KEY ("USER_ID") REFERENCES "DEMO"."USERS" ("ID") ENABLE;\n'
    ),
    # A second child makes the parent's foreign-key stubs collide across files,
    # which is what defeats Graphify's own resolution on a real schema.
    "database/DEMO/tables/INVOICES.sql": (
        'CREATE TABLE "DEMO"."INVOICES"\n'
        '   ("ID" NUMBER NOT NULL ENABLE, "USER_ID" NUMBER) ;\n'
        'ALTER TABLE "DEMO"."INVOICES" ADD CONSTRAINT "FK_INVOICES_USERS" '
        'FOREIGN KEY ("USER_ID") REFERENCES "DEMO"."USERS" ("ID") ENABLE;\n'
    ),
    "database/DEMO/packages/AUTH_PKG_SPEC.sql": (
        'CREATE OR REPLACE PACKAGE "DEMO"."AUTH_PKG" AS\n'
        "  FUNCTION is_admin(p_user VARCHAR2) RETURN BOOLEAN;\n"
        "END auth_pkg;\n"
    ),
    "database/OTHER/tables/WIDGETS.sql": (
        'CREATE TABLE "OTHER"."WIDGETS"\n'
        '   ("ID" NUMBER NOT NULL ENABLE) ;\n'
    ),
    "database/DEMO/synonyms/WIDGET_SYN.sql": (
        '  CREATE OR REPLACE EDITIONABLE SYNONYM "DEMO"."WIDGET_SYN" FOR "OTHER"."WIDGETS";\n'
    ),
    "apps/DEMO/102/shared-components/authorizations.apx": (
        "authorization is-admin (\n"
        "    name: IS_ADMIN\n"
        "    type: plSqlFunctionBody\n"
        "    settings {\n"
        "        plsqlFunctionBody: return auth_pkg.is_admin(:APP_USER);\n"
        "    }\n"
        ")\n"
    ),
    "apps/DEMO/102/pages/p00004-home.apx": (
        "page 4 (\n"
        "    name: Home\n"
        "    region orders (\n"
        "        name: Orders\n"
        "        source {\n"
        "            sqlQuery: select o.id from orders o join users u on u.id = o.user_id\n"
        "        }\n"
        "        security {\n"
        "            authorizationScheme: @is-admin\n"
        "        }\n"
        "    )\n"
        ")\n"
    ),
    "apps/DEMO/102/pages/p00005-other.apx": (
        "page 5 (\n"
        "    name: Other\n"
        "    region users (\n"
        "        name: Users\n"
        "        security {\n"
        "            authorizationScheme: @is-admin\n"
        "        }\n"
        "    )\n"
        ")\n"
    ),
    "apps/DEMO/102/pages/p00006-widgets.apx": (
        "page 6 (\n"
        "    name: Widgets\n"
        "    region direct (\n"
        "        name: Direct\n"
        "        source {\n"
        "            sqlQuery: select w.id from other.widgets w\n"
        "        }\n"
        "    )\n"
        "    region via-synonym (\n"
        "        name: Via synonym\n"
        "        source {\n"
        "            sqlQuery: select s.id from widget_syn s\n"
        "        }\n"
        "    )\n"
        ")\n"
    ),
}

# Runs the real `graphify update` with the repository extractors routed in, so
# the id salting and stub rewiring that only the CLI path performs are exercised.
DRIVER = r'''
import importlib.util
import sys
from pathlib import Path

corpus, extractor = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("apexlang_under_test", extractor)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

import graphify.extract as graphify_extract

dispatch = getattr(graphify_extract, "_DISPATCH", None)
if dispatch is None:
    print("SKIP graphify has no _DISPATCH table")
    raise SystemExit(0)
dispatch[".apx"] = module.extract_apexlang
dispatch[".sql"] = module.extract_sql_linked

from graphify.__main__ import main

sys.argv = ["graphify", "update", corpus]
main()
'''


def graphify_python() -> Path | None:
    executable = shutil.which("graphify")
    if not executable:
        return None
    try:
        first_line = Path(executable).resolve().read_text(encoding="utf-8").splitlines()[0]
    except (OSError, UnicodeError, IndexError):
        return None
    if not first_line.startswith("#!"):
        return None
    interpreter = Path(first_line[2:].strip())
    return interpreter if interpreter.is_file() else None


class GraphifyPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        interpreter = graphify_python()
        if interpreter is None:
            raise unittest.SkipTest("Graphify Python launcher is unavailable")
        # Outside the repository: a matcher honouring ignore rules would
        # otherwise see every path under the gitignored scratch/ as ignored.
        corpus = Path(tempfile.mkdtemp(prefix="graphify-pipeline."))
        cls.addClassCleanup(shutil.rmtree, corpus, ignore_errors=True)
        for relative, text in FILES.items():
            target = corpus / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        (corpus / ".graphifyignore").write_text("", encoding="utf-8")
        completed = subprocess.run(
            [str(interpreter), "-B", "-c", DRIVER, str(corpus), str(EXTRACTOR)],
            text=True,
            capture_output=True,
            cwd=corpus,
            timeout=300,
        )
        if "SKIP" in completed.stdout:
            raise unittest.SkipTest(completed.stdout.strip())
        graph_path = corpus / "graphify-out" / "graph.json"
        if completed.returncode != 0 or not graph_path.is_file():
            raise unittest.SkipTest(f"Graphify pipeline unavailable: {completed.stderr[-300:]}")
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        nodes = {node["id"]: node for node in graph["nodes"]}
        cls.result = {
            "stubs": sorted(node["label"] for node in graph["nodes"] if not node.get("source_file")),
            "edges": [
                {
                    "source": link["source"],
                    "target": link["target"],
                    "relation": link.get("relation"),
                    "target_file": nodes[link["target"]].get("source_file", ""),
                    "source_label": nodes[link["source"]].get("label", ""),
                    "target_label": nodes[link["target"]].get("label", ""),
                }
                for link in graph["links"]
            ],
            "authorizations": [
                {"id": node["id"], "label": node.get("label"), "file": node.get("source_file")}
                for node in graph["nodes"]
                if (node.get("metadata") or {}).get("component_type") == "authorization"
            ],
        }

    def edges(self, relation: str) -> list[dict]:
        return [edge for edge in self.result["edges"] if edge["relation"] == relation]

    def test_page_reads_land_on_the_mirrored_table_nodes(self) -> None:
        reads = self.edges("reads_from")
        self.assertTrue(reads, "no reads_from edges were built")
        for edge in reads:
            self.assertTrue(
                edge["target_file"].startswith(("database/DEMO/tables/", "database/OTHER/tables/")),
                f"{edge['target_label']} is a stub, not a mirrored table",
            )
        self.assertEqual(
            {'"DEMO"."ORDERS"', '"DEMO"."USERS"', '"OTHER"."WIDGETS"'}, {edge["target_label"] for edge in reads}
        )

    def test_cross_schema_and_synonym_reads_land_on_the_other_schemas_table(self) -> None:
        widget_edges = [edge for edge in self.edges("reads_from") if "WIDGETS" in edge["target_label"]]
        self.assertTrue(widget_edges, "the page never linked to OTHER.WIDGETS")
        for edge in widget_edges:
            self.assertEqual("database/OTHER/tables/WIDGETS.sql", edge["target_file"], edge)
        self.assertNotIn("WIDGET_SYN", self.result["stubs"])

    def test_foreign_key_lands_on_the_mirrored_parent_table(self) -> None:
        references = self.edges("references")
        self.assertEqual(2, len(references), references)
        for edge in references:
            self.assertEqual("database/DEMO/tables/USERS.sql", edge["target_file"], edge)
        self.assertEqual(1, len({edge["target"] for edge in references}))

    def test_package_call_lands_on_the_mirrored_package(self) -> None:
        calls = self.edges("calls")
        self.assertEqual(1, len(calls), calls)
        self.assertEqual("database/DEMO/packages/AUTH_PKG_SPEC.sql", calls[0]["target_file"])

    def test_a_reference_and_its_declaration_are_one_node(self) -> None:
        authorizations = self.result["authorizations"]
        self.assertEqual(1, len(authorizations), authorizations)
        self.assertTrue(authorizations[0]["file"].endswith("authorizations.apx"))
        # The graph is undirected, so an edge may be stored in either direction.
        secured = self.edges("secured_by")
        self.assertEqual(2, len(secured), "both pages should be secured by the one scheme")
        schemes = {
            edge[end]
            for edge in secured
            for end in ("source", "target")
            if edge[f"{end}_label"].startswith("Authorization:")
        }
        self.assertEqual(1, len(schemes), schemes)


if __name__ == "__main__":
    unittest.main()
