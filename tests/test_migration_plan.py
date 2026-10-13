from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
import tempfile
import unittest
import urllib.request
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import _no_real_sqlcl  # noqa: F401 (keeps tests away from a real SQLcl)

from scripts import migration_manifest as manifest
from scripts.migration_attempts import write_attempt_event

try:
    from scripts import migration_plan
except ImportError:
    migration_plan = None


class MigrationPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def require_plan(self):
        self.assertIsNotNone(migration_plan, "offline migration planning is not implemented")
        return migration_plan

    def add_folder(
        self,
        relative: str,
        *,
        sql_files: int = 1,
        preconditions: int = 0,
        postconditions: int = 1,
    ) -> Path:
        return self.add_folder_to(
            self.root,
            relative,
            sql_files=sql_files,
            preconditions=preconditions,
            postconditions=postconditions,
        )

    def add_folder_to(
        self,
        root: Path,
        relative: str,
        *,
        sql_files: int = 1,
        preconditions: int = 0,
        postconditions: int = 1,
    ) -> Path:
        folder = root / relative
        folder.mkdir(parents=True)
        for sequence in range(1, sql_files + 1):
            (folder / f"{sequence:03d}-step-{sequence}.sql").write_text(
                f"SELECT {sequence} FROM dual;\n", encoding="utf-8", newline="\n"
            )
        checks = {
            "schemaVersion": 1,
            "preconditions": [
                {"id": f"before-{index}", "sql": "SELECT 1 FROM dual", "expected": 1}
                for index in range(1, preconditions + 1)
            ],
            "postconditions": [
                {"id": f"after-{index}", "sql": "SELECT 1 FROM dual", "expected": 1}
                for index in range(1, postconditions + 1)
            ],
        }
        (folder / "checks.json").write_text(json.dumps(checks) + "\n", encoding="utf-8", newline="\n")
        return folder

    def add_receipt(self, folder: Path, *, change_hash: bool = False) -> None:
        relative = folder.relative_to(self.root).as_posix()
        migration = manifest.load_migration(self.root, relative)
        target = {"environment": "dev", "connection": "local-fixture", "expected_user": "APP", "current_schema": "APP"}
        receipt = {
            "schemaVersion": 1,
            "state": "verified",
            "environment": "dev",
            "migration": folder.name,
            "createdDate": migration.date,
            "family": migration.family,
            "revision": migration.revision,
            "files": [
                {"name": item.name, "sequence": item.sequence, "sha256": item.sha256}
                for item in migration.files
            ],
            "checksSha256": migration.checks_sha256,
            "payloadDigest": migration.payload_digest,
            "target": target,
            "applyStartedAt": "2026-10-12T10:00:00Z",
            "applyCompletedAt": "2026-10-12T10:01:00Z",
            "verifiedAt": "2026-10-12T10:02:00Z",
            "checks": [{"id": check.id, "passed": True} for check in migration.postconditions],
            "verifier": "fixture-verifier-v1",
            "normalization": "fixture-normalization-v1",
        }
        if change_hash:
            receipt["files"][0]["sha256"] = "0" * 64
        manifest.install_receipt(folder / "status.dev.json", receipt)

    def add_missing_transcript_event(self, folder: Path) -> None:
        relative = folder.relative_to(self.root).as_posix()
        migration = manifest.load_migration(self.root, relative)
        event_id = str(uuid.uuid4())
        event = {
            "schemaVersion": 1,
            "eventId": event_id,
            "attemptId": str(uuid.uuid4()),
            "runId": "migration-attempt-report-test",
            "predecessorEventId": None,
            "recordedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "migrationPath": relative,
            "environment": "dev",
            "target": {
                "environment": "dev",
                "connection": "fixture-profile",
                "expected_user": "APP",
                "current_schema": "APP",
            },
            "identity": None,
            "files": [
                {"name": item.name, "sequence": item.sequence, "sha256": item.sha256}
                for item in migration.files
            ],
            "checksSha256": migration.checks_sha256,
            "payloadDigest": migration.payload_digest,
            "state": "apply-not-started",
            "writeAttempted": False,
            "evidence": [{
                "kind": "no-write-sqlcl-output",
                "path": "scratch/migration-attempt-report-test/output.log",
                "sha256": hashlib.sha256(b"output\n").hexdigest(),
                "classification": "connection-failed-before-payload",
            }],
        }
        write_attempt_event(folder, event)

    def tree_digest(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.root.rglob("*"), key=lambda item: item.relative_to(self.root).as_posix()):
            relative = path.relative_to(self.root).as_posix()
            digest.update(relative.encode())
            if path.is_symlink():
                digest.update(b"L" + os.readlink(path).encode())
            elif path.is_file():
                digest.update(b"F" + path.read_bytes())
            elif path.is_dir():
                digest.update(b"D")
        return digest.hexdigest()

    def test_discovery_is_inventory_without_execution_order(self) -> None:
        self.add_folder("migrations/ALPHA/2026-09-01_add-column-r001")
        self.add_folder("migrations/ALPHA/2026-09-02_add-column-r002")
        self.add_folder("migrations/BRAVO/2026-09-01_add-column-r001")
        self.add_folder("migrations/BRAVO/2026-09-02_add-column-r002")

        api = self.require_plan()
        discovered = api.build_migration_plan(self.root)
        self.assertEqual(0, discovered["exitCode"])
        self.assertEqual("discovered", discovered["selection"])
        self.assertEqual("sorted-display-only", discovered["orderStatus"])
        self.assertEqual(4, discovered["summary"]["folderCount"])
        self.assertEqual(
            {"migrations/ALPHA/2026-09-01_add-column-r001", "migrations/BRAVO/2026-09-01_add-column-r001"},
            {row["path"] for row in discovered["migrations"] if row["revision"] == 1},
        )

        supplied = [
            "migrations/BRAVO/2026-09-01_add-column-r001",
            "migrations/ALPHA/2026-09-01_add-column-r001",
            "migrations/BRAVO/2026-09-02_add-column-r002",
            "migrations/ALPHA/2026-09-02_add-column-r002",
        ]
        explicit = api.build_migration_plan(self.root, supplied)
        self.assertEqual("caller-supplied", explicit["orderStatus"])
        self.assertEqual(supplied, [row["path"] for row in explicit["migrations"]])

        reversed_family = api.build_migration_plan(
            self.root,
            [supplied[2], supplied[0], supplied[3], supplied[1]],
        )
        self.assertEqual(2, reversed_family["exitCode"])
        self.assertTrue(reversed_family["diagnostics"])

    def test_explicit_selection_rejects_reparse_points_in_every_path_component(self) -> None:
        folder = self.add_folder("migrations/ALPHA/2026-09-03_reparse-r001")
        relative = folder.relative_to(self.root).as_posix()
        components = (
            self.root / "migrations",
            self.root / "migrations" / "ALPHA",
            folder,
        )
        api = self.require_plan()

        for reparse_component in components:
            with self.subTest(component=reparse_component.relative_to(self.root)):
                with patch.object(
                    migration_plan,
                    "_is_link_or_reparse",
                    side_effect=lambda path, target=reparse_component: Path(path) == target,
                ):
                    with patch.object(manifest, "load_batch", wraps=manifest.load_batch) as loader:
                        report = api.build_migration_plan(self.root, [relative])

                self.assertEqual(2, report["exitCode"])
                self.assertFalse(loader.called, "unsafe explicit paths must be rejected before migration loading")
                self.assertTrue(any(item["code"] == "unsafe-path" for item in report["diagnostics"]))
                self.assertEqual("unknown", report["migrations"][0]["localEvidence"]["status"])

    @unittest.skipUnless(os.name == "nt", "requires a Windows directory junction")
    def test_explicit_selection_rejects_a_real_windows_junction(self) -> None:
        target = self.add_folder("migrations/ALPHA/2026-09-04_junction-target-r001")
        junction = target.parent / "2026-09-05_junction-input-r001"
        command = f'mklink /J "{junction}" "{target}"'
        created = subprocess.run(
            ["cmd.exe", "/c", command],
            capture_output=True,
            check=False,
            text=True,
        )
        if created.returncode != 0:
            self.skipTest("Windows did not permit creation of a temporary directory junction")

        try:
            self.assertFalse(junction.is_symlink(), "test input must exercise a junction, not a symbolic link")
            self.assertTrue(
                junction.lstat().st_file_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
            relative = junction.relative_to(self.root).as_posix()
            report = self.require_plan().build_migration_plan(self.root, [relative])
            self.assertEqual(2, report["exitCode"])
            self.assertTrue(any(item["code"] == "unsafe-path" for item in report["diagnostics"]))
        finally:
            junction.rmdir()

    def test_report_counts_are_informational(self) -> None:
        locked = self.add_folder("migrations/2026-09-10_add-index-r001", sql_files=1, preconditions=1, postconditions=2)
        (locked / "001-step-1.sql").write_text("SELECT 'PRIVATE_SQL_BODY' FROM dual;\n", encoding="utf-8")
        self.add_receipt(locked)
        self.add_folder("migrations/2026-09-11_add-view-r001", sql_files=2, preconditions=1, postconditions=1)

        report = self.require_plan().build_migration_plan(self.root)

        self.assertEqual(0, report["exitCode"], report["diagnostics"])
        self.assertEqual(
            {
                "folderCount": 2,
                "sqlFileCount": 3,
                "preconditionCount": 2,
                "postconditionCount": 3,
                "singleSqlFileFolderCount": 1,
                "immutableFolderCount": 1,
                "unknownEvidenceFolderCount": 0,
            },
            report["summary"],
        )
        record = next(item for item in report["migrations"] if item["path"].endswith("add-index-r001"))
        self.assertEqual("locked", record["localEvidence"]["status"])
        self.assertNotIn("ready", json.dumps(record).casefold())
        self.assertNotIn("PRIVATE_SQL_BODY", json.dumps(report))

    def test_offline_report_never_opens_external_or_write_paths(self) -> None:
        self.add_folder("migrations/2026-09-12_read-only-r001")
        (self.root / ".env").write_text("PASSWORD=must-not-be-read\n", encoding="utf-8")
        before = self.tree_digest()
        original_open = Path.open

        def guard_open(path: Path, *args, **kwargs):
            if path.name == ".env":
                raise AssertionError("offline reporting must not read .env")
            mode = args[0] if args else kwargs.get("mode", "r")
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                raise AssertionError("offline reporting must not open a file for writing")
            return original_open(path, *args, **kwargs)

        def forbidden(*_args, **_kwargs):
            raise AssertionError("offline reporting attempted an external or write operation")

        with ExitStack() as stack:
            stack.enter_context(patch.object(Path, "open", guard_open))
            for name in ("write_bytes", "write_text", "mkdir", "touch", "unlink", "rename", "replace"):
                stack.enter_context(patch.object(Path, name, forbidden))
            stack.enter_context(patch("scripts.local_config.read_project_env", forbidden))
            stack.enter_context(patch("scripts.sqlcl_session.run_sqlcl", forbidden))
            stack.enter_context(patch.object(subprocess, "run", forbidden))
            stack.enter_context(patch.object(subprocess, "Popen", forbidden))
            stack.enter_context(patch.object(socket, "create_connection", forbidden))
            stack.enter_context(patch.object(urllib.request, "urlopen", forbidden))
            report = self.require_plan().build_migration_plan(self.root)

        self.assertEqual(0, report["exitCode"])
        self.assertEqual(before, self.tree_digest())
        self.assertNotIn("must-not-be-read", json.dumps(report))

    def test_bad_evidence_and_paths_are_diagnosed(self) -> None:
        changed = self.add_folder("migrations/2026-09-13_changed-receipt-r001")
        self.add_receipt(changed, change_hash=True)
        self.add_folder("migrations/2026-09-14_readable-r001")
        unreadable = self.add_folder("migrations/2026-09-15_bad-checks-r001")
        (unreadable / "checks.json").write_text("{bad json\n", encoding="utf-8")

        api = self.require_plan()
        report = api.build_migration_plan(self.root)
        self.assertEqual(2, report["exitCode"])
        self.assertIn("migrations/2026-09-14_readable-r001", {item["path"] for item in report["migrations"]})
        changed_record = next(item for item in report["migrations"] if item["path"] == "migrations/2026-09-13_changed-receipt-r001")
        self.assertEqual("unknown", changed_record["localEvidence"]["status"])
        self.assertTrue(any(item["path"] == "migrations/2026-09-15_bad-checks-r001" for item in report["diagnostics"]))

        malformed = self.add_folder("migrations/2026-09-16_malformed-receipt-r001")
        (malformed / "status.dev.json").write_text("{bad json\n", encoding="utf-8")
        missing_transcript = self.add_folder("migrations/2026-09-17_missing-transcript-r001")
        self.add_missing_transcript_event(missing_transcript)
        evidence_report = api.build_migration_plan(self.root)
        self.assertEqual(2, evidence_report["exitCode"])
        statuses = {item["path"]: item["localEvidence"]["status"] for item in evidence_report["migrations"]}
        self.assertEqual("unknown", statuses["migrations/2026-09-16_malformed-receipt-r001"])
        self.assertEqual("unknown", statuses["migrations/2026-09-17_missing-transcript-r001"])

        missing_root = self.root / "without-migrations"
        missing_root.mkdir()
        missing = api.build_migration_plan(missing_root)
        self.assertEqual(2, missing["exitCode"])
        self.assertTrue(missing["diagnostics"])

        empty_root = self.root / "empty"
        (empty_root / "migrations").mkdir(parents=True)
        empty = api.build_migration_plan(empty_root)
        self.assertEqual(0, empty["exitCode"])
        self.assertEqual(0, empty["summary"]["folderCount"])

        gap_root = self.root / "gap"
        (gap_root / "migrations").mkdir(parents=True)
        self.add_folder_to(gap_root, "migrations/2026-09-18_revision-gap-r001")
        self.add_folder_to(gap_root, "migrations/2026-09-20_revision-gap-r003")
        gap = api.build_migration_plan(gap_root)
        self.assertEqual(2, gap["exitCode"])
        self.assertTrue(gap["diagnostics"])

        for folders in (
            ("migrations/../outside",),
            ("migrations/2026-09-14_readable-r001", "migrations/2026-09-14_readable-r001/"),
        ):
            with self.subTest(folders=folders):
                unsafe = api.build_migration_plan(self.root, folders)
                self.assertEqual(2, unsafe["exitCode"])
                self.assertTrue(unsafe["diagnostics"])

        symlink_root = self.root / "symlink-root"
        (symlink_root / "migrations").mkdir(parents=True)
        target = self.root / "symlink-target"
        target.mkdir()
        try:
            (symlink_root / "migrations" / "2026-09-21_linked-r001").symlink_to(target, target_is_directory=True)
        except OSError:
            pass
        else:
            linked = api.build_migration_plan(symlink_root)
            self.assertEqual(2, linked["exitCode"])
            self.assertTrue(linked["diagnostics"])

    def test_schema_filter_marks_flat_owner_unverified(self) -> None:
        self.add_folder("migrations/ALPHA/2026-09-22_schema-one-r001")
        self.add_folder("migrations/BRAVO/2026-09-23_schema-two-r001")
        self.add_folder("migrations/2026-09-24_flat-owner-r001")
        api = self.require_plan()

        filtered = api.build_migration_plan(self.root, schema="ALPHA")
        self.assertEqual(0, filtered["exitCode"], filtered["diagnostics"])
        self.assertEqual(
            {"migrations/ALPHA/2026-09-22_schema-one-r001", "migrations/2026-09-24_flat-owner-r001"},
            {item["path"] for item in filtered["migrations"]},
        )
        flat = next(item for item in filtered["migrations"] if item["schemaSource"] == "caller-asserted")
        self.assertEqual("ALPHA", flat["schema"])

        unresolved = api.build_migration_plan(self.root)
        flat_unresolved = next(item for item in unresolved["migrations"] if item["path"].endswith("flat-owner-r001"))
        self.assertIsNone(flat_unresolved["schema"])
        self.assertEqual("unresolved", flat_unresolved["schemaSource"])

        mismatch = api.build_migration_plan(self.root, ["migrations/BRAVO/2026-09-23_schema-two-r001"], schema="ALPHA")
        self.assertEqual(2, mismatch["exitCode"])
        self.assertTrue(any("schema" in item["message"].casefold() for item in mismatch["diagnostics"]))

    def test_renderers_expose_meaningful_fields_and_json_hides_exit_code(self) -> None:
        self.add_folder("migrations/2026-09-25_render-r001")
        api = self.require_plan()
        report = api.build_migration_plan(self.root)

        self.assertEqual(
            {
                "schemaVersion", "mode", "selection", "orderStatus", "environment", "target", "summary",
                "migrations", "preflight", "diagnostics", "limitations", "exitCode",
            },
            set(report),
        )
        self.assertEqual(
            {
                "path", "schema", "schemaSource", "family", "revision", "payloadDigest", "checksSha256",
                "files", "preconditionCount", "postconditionCount", "localEvidence",
            },
            set(report["migrations"][0]),
        )
        self.assertEqual({"status", "reasons"}, set(report["migrations"][0]["localEvidence"]))
        self.assertEqual({"name", "sequence", "sha256"}, set(report["migrations"][0]["files"][0]))

        rendered_json = json.loads(api.render_migration_plan(report, "json"))
        self.assertEqual(1, rendered_json["schemaVersion"])
        self.assertNotIn("exitCode", rendered_json)
        self.assertIn("folderCount", rendered_json["summary"])
        self.assertIn("Folders: 1", api.render_migration_plan(report, "text"))
        markdown = api.render_migration_plan(report, "markdown")
        self.assertIn("# Migration plan", markdown)
        self.assertIn("render-r001", markdown)
        self.assertIn("no dependency or execution order is inferred", markdown.casefold())

    def test_markdown_diagnostics_cannot_create_links(self) -> None:
        self.add_folder("migrations/2026-09-26_markdown-link-r001")
        api = self.require_plan()
        report = api.build_migration_plan(self.root)
        report["diagnostics"].append(
            {
                "code": "invalid-migration-directory",
                "path": "migrations/[open](https://example.invalid)",
                "message": "Invalid directory name.",
            }
        )

        markdown = api.render_migration_plan(report, "markdown")

        self.assertIn(r"\[open\]\(https://example.invalid\)", markdown)
        self.assertNotIn("[open](https://example.invalid)", markdown)
        report["diagnostics"] = [{"code": "unsafe|path", "path": "a|b", "message": "line one\nline two"}]
        escaped = api.render_migration_plan(report, "markdown")
        self.assertIn("unsafe\\|path", escaped)
        self.assertIn("a\\|b", escaped)
        self.assertNotIn("\nline two", escaped)
        with self.assertRaises(ValueError):
            api.render_migration_plan(report, "html")


if __name__ == "__main__":
    unittest.main()
