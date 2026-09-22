from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

_SCRIPTS_DIR = str(
    Path(__file__).resolve().parents[
        1 if Path(__file__).resolve().parent.name == "tests" else 2
    ]
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from teamlib.source_snapshot import SourceSnapshotError, load_integration_source


MIGRATION_ID = "20260922T120000__alice__snapshot"
UNTRACKED_ID = "20260922T120100__alice__untracked"


def inventory_bytes(marker: str = "committed") -> bytes:
    document = {
        "version": 1,
        "topology": "separate",
        "normalizer_version": "inventory-v1",
        "coverage_version": "coverage-v1",
        "schema_set_digest": "",
        "objects": {
            "APP|TABLE|EMPLOYEE": hashlib.sha256(marker.encode("utf-8")).hexdigest()
        },
        "invalid": [],
    }
    document["inventory_digest"] = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return json.dumps(document, sort_keys=True).encode("utf-8") + b"\n"


def declaration_bytes() -> bytes:
    return json.dumps(
        {
            "version": 1,
            "alias": "employee",
            "page_ids": [1],
            "checks": [
                {
                    "id": "objects",
                    "page_id": 1,
                    "kind": "select",
                    "verify_sql": "employee/objects.verify.sql",
                    "expected_objects": ["APP.EMPLOYEE"],
                },
                {
                    "id": "home",
                    "page_id": 1,
                    "kind": "flow",
                    "flow": "employee/home.flow.json",
                    "steps": [
                        {
                            "action": "navigate",
                            "path": "/ords/r/app/employee/home",
                            "expected_visible_text": "Employee",
                        }
                    ],
                },
            ],
        },
        sort_keys=True,
    ).encode("utf-8") + b"\n"


class SourceSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-source-snapshot-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Snapshot Test"],
            check=True,
        )
        self.committed_sql = (
            b"-- migration-version: 1\n-- target: tables\n-- destructive: false\n\n"
            b"CREATE TABLE EMPLOYEE(ID NUMBER);\n"
        )
        self.committed_verify = (
            b"SELECT 'employee_exists' assertion_name, 'PASS' status FROM dual;\n"
        )
        self.committed_inventory = inventory_bytes()
        self.committed_app = b"prompt --application/set_environment\n"
        self.committed_check_sql = (
            b"SELECT 'TEAM_ASSERT|' || assertion_name || '|' || status AS status "
            b"FROM (SELECT 'employee_exists' assertion_name, 'PASS' status FROM dual);\n"
        )
        self._write_seed()
        self._commit("seed")
        self.commit = self._head()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, relative: str, data: bytes) -> None:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def _write_seed(self) -> None:
        self._write(f"migrations/{MIGRATION_ID}.sql", self.committed_sql)
        self._write(f"migrations/{MIGRATION_ID}.verify.sql", self.committed_verify)
        self._write("database/schema-inventory.json", self.committed_inventory)
        self._write("apps/employee/application.apx", self.committed_app)
        self._write("ci/app-checks/employee.json", declaration_bytes())
        self._write("ci/app-checks/employee/objects.verify.sql", self.committed_check_sql)
        self._write("ci/app-checks/employee/home.flow.json", b"{}\n")
        self._write("unowned/secret.txt", b"must not enter snapshot\n")

    def _commit(self, message: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "commit", "-qm", message], check=True
        )

    def _head(self) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()

    def test_snapshot_uses_only_exact_committed_bytes_after_worktree_changes(self):
        self._write(f"migrations/{MIGRATION_ID}.sql", b"dirty migration\n")
        self._write("database/schema-inventory.json", inventory_bytes("dirty"))
        self._write("apps/employee/application.apx", b"dirty app\n")
        self._write("ci/app-checks/employee.json", declaration_bytes().replace(b"home", b"dirty"))
        self._write("ci/app-checks/employee/objects.verify.sql", b"dirty check\n")
        self._write(
            f"migrations/{UNTRACKED_ID}.sql",
            b"-- migration-version: 1\n-- target: tables\n-- destructive: false\n\nSELECT 1 FROM dual;\n",
        )

        source = load_integration_source(self.repo, self.commit, ("employee",))

        self.assertEqual(source.commit, self.commit)
        self.assertEqual(source.migrations[f"{MIGRATION_ID}.sql"], self.committed_sql)
        self.assertEqual(source.canonical_inventory.as_dict(), json.loads(self.committed_inventory))
        self.assertEqual(source.app_trees["employee"]["application.apx"], self.committed_app)
        self.assertEqual(
            source.check_bundle.member_bytes("employee/objects.verify.sql"),
            self.committed_check_sql,
        )
        self.assertNotIn(f"{UNTRACKED_ID}.sql", source.migrations)
        self.assertNotIn("secret.txt", source.migrations)

    def test_snapshot_ignores_repository_placeholder_files(self):
        self._write("migrations/.gitkeep", b"")
        self._commit("add migration placeholder")

        source = load_integration_source(self.repo, self._head(), ("employee",))

        self.assertNotIn(".gitkeep", source.migrations)

    def test_snapshot_requires_an_exact_commit_and_stays_stable_after_head_moves(self):
        with self.assertRaisesRegex(SourceSnapshotError, "exact commit"):
            load_integration_source(self.repo, "HEAD", ("employee",))
        with self.assertRaisesRegex(SourceSnapshotError, "exact commit"):
            load_integration_source(self.repo, "a" * 39, ("employee",))

        source = load_integration_source(self.repo, self.commit, ("employee",))
        self._write("apps/employee/application.apx", b"second commit\n")
        self._commit("move head")
        self.assertNotEqual(self._head(), self.commit)
        self.assertEqual(source.commit, self.commit)
        self.assertEqual(source.app_trees["employee"]["application.apx"], self.committed_app)

    def test_snapshot_refuses_missing_required_owned_inputs(self):
        cases = (
            ("database/schema-inventory.json", "canonical inventory"),
            ("apps/employee/application.apx", "application employee"),
            (f"migrations/{MIGRATION_ID}.verify.sql", "verification"),
        )
        for relative, message in cases:
            with self.subTest(relative=relative):
                subprocess.run(
                    ["git", "-C", str(self.repo), "rm", "-q", relative], check=True
                )
                self._commit(f"remove {relative}")
                with self.assertRaisesRegex(SourceSnapshotError, message):
                    load_integration_source(self.repo, self._head(), ("employee",))
                subprocess.run(
                    ["git", "-C", str(self.repo), "checkout", self.commit, "--", relative],
                    check=True,
                )
                self._commit(f"restore {relative}")

    def test_snapshot_refuses_symlinks_under_owned_paths(self):
        link = self.repo / "apps" / "employee" / "link.apx"
        link.symlink_to("application.apx")
        self._commit("add symlink")
        with self.assertRaisesRegex(SourceSnapshotError, "unsupported Git mode"):
            load_integration_source(self.repo, self._head(), ("employee",))

    def test_materialized_migrations_are_bounded_and_removed(self):
        source = load_integration_source(self.repo, self.commit, ("employee",))
        scratch = Path(self.temp.name) / "scratch"
        sibling = scratch / "keep.txt"
        scratch.mkdir()
        sibling.write_text("keep", encoding="utf-8")
        with source.materialize_migrations(scratch) as root:
            self.assertEqual(root.parent, scratch)
            self.assertEqual((root / f"{MIGRATION_ID}.sql").read_bytes(), self.committed_sql)
            materialized = root
        self.assertFalse(materialized.exists())
        self.assertEqual(sibling.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
