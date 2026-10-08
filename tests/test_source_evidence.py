import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.source_evidence import content_evidence, read_content_baseline, tree_hashes, read_page_lock_evidence


class SourceEvidenceTests(unittest.TestCase):
    def test_page_lock_evidence_requires_stable_exact_native_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before, after = root / 'before.json', root / 'after.json'
            self.assertIsNone(read_page_lock_evidence(before, after, 100))
            rows = [{'applicationId': 100, 'workspace': 'DEMO', 'pageId': 1, 'lockId': 3356265751219301, 'owner': 'DEMO', 'comment': 'Owned before export', 'lockedOn': '2026-10-08T12:30:00'}]
            before.write_text(json.dumps(rows)); after.write_text(json.dumps(rows))
            self.assertEqual(read_page_lock_evidence(before, after, 100), rows)
            rows[0]['lockId'] += 1; after.write_text(json.dumps(rows))
            with self.assertRaisesRegex(ValueError, 'page lock'):
                read_page_lock_evidence(before, after, 100)
            before.unlink()
            with self.assertRaises(ValueError): read_page_lock_evidence(before, after, 100)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFO unavailable')
    def test_non_regular_nodes_are_refused_before_reading_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            os.mkfifo(root / 'blocked.apx')
            with self.assertRaisesRegex(ValueError, 'regular'):
                tree_hashes(root)

    def fixture(self,root):
        (root/'.apex').mkdir()
        (root/'.apex/apexlang.json').write_text('{"mmdVersion":"26.2.0+3479"}')
        (root/'application.apx').write_text('app SAMPLE ()\n')
        (root/'pages').mkdir();(root/'pages/p1.apx').write_text('page 1 ()\n')
        (root/'static-files').mkdir();(root/'static-files/pic.png').write_bytes(b'\x89PNG\x00\xff')
        (root/'deployments').mkdir();(root/'deployments/dev.json').write_text('{}\n')

    def test_hashes_include_raw_binary_and_protected_descriptors_but_not_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);self.fixture(root)
            (root/'apex-team-export.json').write_text('{}')
            hashes=tree_hashes(root)
            self.assertEqual(hashes['static-files/pic.png'],hashlib.sha256(b'\x89PNG\x00\xff').hexdigest())
            self.assertIn('deployments/dev.json',hashes);self.assertNotIn('apex-team-export.json',hashes)

    def test_legacy_markers_never_qualify_partial_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);self.fixture(root)
            marker={'applicationId':100,'applicationPresent':True,'builderLastUpdatedOn':None,'version':'Release 1.0'}
            (root/'apex-team-export.json').write_text(json.dumps(marker))
            with self.assertRaises(ValueError):read_content_baseline(root,100)
            marker.update(content_evidence(root));(root/'apex-team-export.json').write_text(json.dumps(marker))
            self.assertEqual(read_content_baseline(root,100),tree_hashes(root))
            with self.assertRaises(ValueError):read_content_baseline(root,101)

    def test_real_format_and_safe_paths_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);self.fixture(root)
            (root/'.apex/apexlang.json').write_text('{"version":1}')
            with self.assertRaises(ValueError):content_evidence(root)
            (root/'.apex/apexlang.json').write_text('{"mmdVersion":"26.1.0+3102"}')
            with self.assertRaises(ValueError):content_evidence(root)

    def test_symlinks_fail_without_following_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);self.fixture(root)
            (root/'escape').symlink_to(root.parent)
            with self.assertRaises(ValueError):tree_hashes(root)
