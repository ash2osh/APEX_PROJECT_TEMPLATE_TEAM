"""Controller regressions for strict DEV suite identifiers and I/O refusals."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.dev_test_manifest import DevTestManifestError, load_suite
from test_dev_test_manifest import SPEC_EXAMPLE


class ManifestReviewTests(unittest.TestCase):
    def test_rejects_unsafe_or_colliding_fixture_names(self):
        for mutation in ("suffix", "binding", "collision"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "sql").mkdir()
                (root / "sql/create-and-seed.sql").write_text("SELECT 1 FROM DUAL;\n")
                data = copy.deepcopy(SPEC_EXAMPLE)
                objects = data["scenarios"][0]["fixtures"]["objects"]
                if mutation == "collision":
                    objects.append({"binding": "other", "suffix": "ITEMS", "type": "TABLE"})
                else:
                    objects[0][mutation] = 'INVALID"; DROP TABLE X'
                path = root / "suite.json"
                path.write_text(json.dumps(data))
                with self.assertRaises(DevTestManifestError):
                    load_suite(path)

    def test_rejects_unsafe_requirement_and_selector_names(self):
        for mutation in ("owner", "name", "type", "member", "selector"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "sql").mkdir()
                (root / "sql/create-and-seed.sql").write_text("SELECT 1 FROM DUAL;\n")
                data = copy.deepcopy(SPEC_EXAMPLE)
                scenario = data["scenarios"][0]
                requirement = {"owner": "APP", "name": "T", "type": "TABLE", "requiredStatus": "VALID", "member": None}
                if mutation == "selector":
                    scenario["fixtures"]["selectors"] = [{"id": "row", "sql": "SELECT 1 FROM DUAL", "columns": [
                        {"name": "ID", "binding": "OTHER_NAMESPACE", "type": "integer"},
                    ]}]
                else:
                    requirement[mutation] = 'INVALID"; DROP TABLE X'
                    scenario["requirements"]["objects"] = [requirement]
                path = root / "suite.json"
                path.write_text(json.dumps(data))
                with self.assertRaises(DevTestManifestError):
                    load_suite(path)

    def test_oracle_error_code_has_exactly_five_digits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sql").mkdir()
            (root / "sql/create-and-seed.sql").write_text("SELECT 1 FROM DUAL;\n")
            data = copy.deepcopy(SPEC_EXAMPLE)
            data["scenarios"][0]["steps"][0]["expected"] = {"kind": "oracle-error", "code": "ORA-1"}
            path = root / "suite.json"
            path.write_text(json.dumps(data))
            with self.assertRaises(DevTestManifestError):
                load_suite(path)

    def test_invalid_utf8_is_a_manifest_refusal(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "suite.json"
            path.write_bytes(b"\xff\xfe")
            with self.assertRaises(DevTestManifestError):
                load_suite(path)

    def test_unusable_sql_paths_are_structured_refusals(self):
        for bad_path in ("sql/\x00.sql", "sql/loop.sql"):
            with self.subTest(path=repr(bad_path)), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "sql").mkdir()
                if bad_path.endswith("loop.sql"):
                    try:
                        (root / bad_path).symlink_to("loop.sql")
                    except OSError:
                        continue
                data = copy.deepcopy(SPEC_EXAMPLE)
                data["scenarios"][0]["steps"][0]["sqlFile"] = bad_path
                path = root / "suite.json"
                path.write_text(json.dumps(data))
                with self.assertRaises(DevTestManifestError):
                    load_suite(path)
