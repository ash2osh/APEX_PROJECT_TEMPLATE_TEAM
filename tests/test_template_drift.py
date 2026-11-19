import json
import subprocess
import _test_subprocess_safety  # noqa: F401
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.template_drift import inspect_template_drift
from scripts.upgrade_template import normalized_sha256


class TemplateDriftTests(unittest.TestCase):
    def make_downstream(self, root: Path):
        root.mkdir(parents=True)
        (root / "AGENTS.md").write_bytes(b"template guidance\n")
        manifest = {
            "schemaVersion": 2,
            "apexRelease": "26.2",
            "minimumSqlclVersion": "26.3.0.0",
            "upstream": "https://github.com/example/template.git",
            "templateOwned": ["AGENTS.md"],
            "projectOwned": [],
            "templateOnly": [],
        }
        (root / "template-manifest.json").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        lock = {
            "schemaVersion": 1,
            "apexRelease": "26.2",
            "upstream": manifest["upstream"],
            "files": {"AGENTS.md": normalized_sha256(b"template guidance\n")},
        }
        (root / ".template-lock.json").write_text(json.dumps(lock) + "\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin", "https://github.com/example/downstream.git"], check=True)
        return lock

    def test_local_drift_is_read_only_and_never_fetches_by_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            self.make_downstream(root)
            (root / "AGENTS.md").write_bytes(b"template guidance\r\n")
            (root / "AGENTS.md.template-new").write_text("candidate\n", encoding="utf-8")
            before = (root / "AGENTS.md").read_bytes()

            with patch("scripts.template_drift.fetch_template", side_effect=AssertionError("unexpected fetch")):
                report = inspect_template_drift(root)

            self.assertEqual(report["status"], "attention")
            self.assertEqual(report["localFiles"][0]["status"], "matches-lock")
            self.assertEqual(report["conflicts"][0]["status"], "pending")
            self.assertEqual(report["upstreamComparison"]["status"], "not-checked")
            self.assertEqual((root / "AGENTS.md").read_bytes(), before)
            self.assertEqual((root / "AGENTS.md.template-new").read_text(encoding="utf-8"), "candidate\n")

    def test_customized_and_missing_template_files_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            self.make_downstream(root)
            (root / "AGENTS.md").write_text("developer edit\n", encoding="utf-8")
            report = inspect_template_drift(root)
            self.assertEqual(report["localFiles"][0]["status"], "customized")

            (root / "AGENTS.md").unlink()
            report = inspect_template_drift(root)
            self.assertEqual(report["localFiles"][0]["status"], "missing")

    def add_locked_file(self, root: Path, relative: str, content: bytes, present: bool) -> None:
        manifest_path = root / "template-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["templateOwned"] = sorted(set(manifest["templateOwned"]) | {relative})
        manifest_path.write_text(json.dumps(manifest) + "\n", encoding="utf-8")
        lock_path = root / ".template-lock.json"
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        lock["files"][relative] = normalized_sha256(content)
        lock_path.write_text(json.dumps(lock) + "\n", encoding="utf-8")
        if present:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def test_missing_cleanup_files_are_removed_by_cleanup_not_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            self.make_downstream(root)
            self.add_locked_file(root, "tests/test_example.py", b"print('x')\n", present=False)
            self.add_locked_file(root, ".github/workflows/ci.yml", b"name: ci\n", present=False)

            report = inspect_template_drift(root)

            statuses = {row["path"]: row["status"] for row in report["localFiles"]}
            self.assertEqual(statuses["tests/test_example.py"], "removed-by-cleanup")
            self.assertEqual(statuses[".github/workflows/ci.yml"], "removed-by-cleanup")
            self.assertEqual(statuses["AGENTS.md"], "matches-lock")
            self.assertEqual(report["status"], "current")

    def test_missing_locked_file_outside_cleanup_prefixes_stays_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            self.make_downstream(root)
            self.add_locked_file(root, "scripts/tool.py", b"print('x')\n", present=False)

            report = inspect_template_drift(root)

            statuses = {row["path"]: row["status"] for row in report["localFiles"]}
            self.assertEqual(statuses["scripts/tool.py"], "missing")
            self.assertEqual(report["status"], "attention")

    def test_customized_test_file_is_still_customized(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            self.make_downstream(root)
            self.add_locked_file(root, "tests/test_example.py", b"print('x')\n", present=True)
            (root / "tests" / "test_example.py").write_text("print('edited')\n", encoding="utf-8")

            report = inspect_template_drift(root)

            statuses = {row["path"]: row["status"] for row in report["localFiles"]}
            self.assertEqual(statuses["tests/test_example.py"], "customized")
            self.assertEqual(report["status"], "attention")

    def test_untrusted_upstream_ref_is_rejected_without_invoking_git(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            self.make_downstream(root)
            with patch("scripts.template_drift.subprocess.run", side_effect=AssertionError("unexpected git command")):
                report = inspect_template_drift(root, upstream=True, ref="--upload-pack=evil")

            self.assertEqual(report["upstreamComparison"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
