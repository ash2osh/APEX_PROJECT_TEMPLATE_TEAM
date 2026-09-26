import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from graphify_apexlang_extractor import parse_apexlang  # noqa: E402


class GraphifyApexlangExtractorTests(unittest.TestCase):
    def test_comment_backticks_do_not_hide_following_multiline_sql(self) -> None:
        source = """// SQL examples use ```sql in this note
App 100 (
  Page 1 (
    SQLQuery: ```sql
      SELECT * FROM ORDERS
    ```
  )
)
"""

        extracted = parse_apexlang(source, Path("comment-fence.apx"))

        self.assertEqual({"nodes", "edges"}, set(extracted))
        reads = [edge for edge in extracted["edges"] if edge["relation"] == "reads_from"]
        self.assertEqual(1, len(reads))
        target_id = reads[0]["target"]
        target = next(node for node in extracted["nodes"] if node["id"] == target_id)
        self.assertEqual("ORDERS", target["label"])


if __name__ == "__main__":
    unittest.main()
