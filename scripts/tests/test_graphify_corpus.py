from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
import tempfile
import unittest

from teamlib.graphify_corpus import collect_corpus


class GraphifyCorpusTests(unittest.TestCase):
    def test_alias_keyed_corpus_excludes_runtime_and_database_history(self):
        with tempfile.TemporaryDirectory(prefix="team-graphify-") as directory:
            root = Path(directory)
            (root / "apps" / "checkout").mkdir(parents=True)
            (root / "apps" / "checkout" / "page.apx").write_text("page 1 (\n name: Home\n)\n", encoding="utf-8")
            (root / "apps" / "checkout" / "deployments").mkdir()
            (root / "apps" / "checkout" / "deployments" / "default.json").write_text("{}", encoding="utf-8")
            (root / "apps" / "checkout" / ".apex").mkdir()
            (root / "apps" / "checkout" / ".apex" / "apexlang.json").write_text("{}", encoding="utf-8")
            (root / "migrations").mkdir()
            (root / "migrations" / "bad.apx").write_text("page: bad", encoding="utf-8")
            (root / "app_context" / "checkout").mkdir(parents=True)
            (root / "app_context" / "checkout" / "purpose.md").write_text("purpose", encoding="utf-8")
            corpus = collect_corpus(root)
            self.assertEqual(len(corpus), 3)
            self.assertEqual({node["alias"] for node in corpus}, {"checkout"})
            self.assertTrue(any(node.get("relative_path") == "apps/checkout/page.apx" for node in corpus))
            self.assertNotIn("deployments", str(corpus))
            self.assertNotIn("migrations", str(corpus))

    def test_outside_or_numeric_application_path_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="team-graphify-") as directory:
            root = Path(directory)
            (root / "apps" / "123").mkdir(parents=True)
            (root / "apps" / "123" / "page.apx").write_text("page: home", encoding="utf-8")
            with self.assertRaises(ValueError):
                collect_corpus(root)


if __name__ == "__main__":
    unittest.main()
