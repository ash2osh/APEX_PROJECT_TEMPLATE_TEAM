"""Project suite protection against hostile upstream ownership/deletion claims."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.upgrade_template import (
    Action,
    UpgradeError,
    apply_actions,
    classify,
    load_manifest,
    validate_lock_ownership,
)


class DevTestUpgradeReviewTests(unittest.TestCase):
    def test_manifest_cannot_claim_project_suite_tree(self):
        for pattern in ('dev-tests/**', 'dev-tests/suites/**', 'dev-tests/suites/orders.json'):
            with self.subTest(pattern=pattern), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                value = {'schemaVersion': 1, 'upstream': 'https://example.invalid/template.git',
                         'templateOwned': [pattern], 'projectOwned': [], 'templateOnly': []}
                (root / 'template-manifest.json').write_text(json.dumps(value), encoding='utf-8')
                fixture = root / 'dev-tests/suites/orders.json'
                fixture.parent.mkdir(parents=True)
                fixture.write_text('{}', encoding='utf-8')
                with patch('scripts.upgrade_template.tracked_template_files',
                           return_value={'dev-tests/suites/orders.json': '100644'}):
                    with self.assertRaises(UpgradeError):
                        classify(root, load_manifest(root))

    def test_old_lock_cannot_authorize_project_suite_deletion(self):
        import re
        manifest = {'_templateOwnedPatterns': [re.compile('.*')]}
        for name in ('dev-tests/suites/orders.json', 'dev-tests/suites/sql/create.sql'):
            with self.subTest(path=name), self.assertRaises(UpgradeError):
                validate_lock_ownership({'files': {name: 'a' * 64}}, manifest)

    def test_apply_actions_cannot_update_or_delete_project_suite_files(self):
        for kind in ("UPDATE", "DELETE"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                template = root / "template"
                project = root / "project"
                template.mkdir()
                project.mkdir()
                suite_file = project / "dev-tests/suites/orders.json"
                suite_file.parent.mkdir(parents=True)
                suite_file.write_text('{"project":"keep"}\n', encoding="utf-8")
                before = suite_file.read_bytes()
                with self.assertRaises(UpgradeError):
                    apply_actions(
                        project,
                        template,
                        [Action(kind, "dev-tests/suites/orders.json")],
                        "unused",
                        "0" * 40,
                        {},
                    )
                self.assertEqual(before, suite_file.read_bytes())
