from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
import _test_subprocess_safety  # noqa: F401
import io
import tempfile
import unittest
import urllib.request
import uuid
from contextlib import ExitStack
from contextlib import redirect_stderr, redirect_stdout
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
        # Production resolves the report root before checking every component;
        # keep the fixture on the same canonical path on Windows, where the
        # temporary-directory environment may use an 8.3 alias.
        self.root = Path(self.temporary.name).resolve()
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

    def test_live_renderers_show_observed_target_and_preflight_statuses(self) -> None:
        self.add_folder("migrations/2026-10-13_render-live-r001")
        api = self.require_plan()
        report = api.build_migration_plan(self.root)
        report.update({"mode": "live", "environment": "dev", "target": {
            "session_user": "APP", "current_schema": "APP", "db_name": "DEVDB",
            "db_unique_name": "DEVDB1", "service_name": "dev", "container_id": "3",
            "container_name": "APP_PDB", "edition": "ORA$BASE", "database_version": "23.26.3.0.0",
        }})
        report["preflight"] = {
            "exit_code": 1,
            "conflicts": [{"code": "LIVE_NAMESPACE_OCCUPIED", "name": "CUSTOMERS", "path": "migrations/2026-10-13_render-live-r001/001-step-1.sql"}],
            "errors": [],
            "coverage": {"deferredPreconditionFolders": ["2026-10-14_render-live-r002"]},
            "limitation": "The catalog observation is point-in-time.",
        }
        report["exitCode"] = 1

        text = api.render_migration_plan(report, "text")
        markdown = api.render_migration_plan(report, "markdown")

        for rendered in (text, markdown):
            with self.subTest(rendered=rendered[:40]):
                self.assertIn("Mode: live", rendered)
                self.assertIn("Target:", rendered)
                self.assertIn("APP", rendered)
                self.assertIn("Preflight exit: 1", rendered)
                self.assertIn("LIVE_NAMESPACE_OCCUPIED", rendered)
                self.assertIn("render-live-r001/001-step-1.sql", rendered)
                self.assertIn("render-live-r002", rendered)

        incomplete = dict(report)
        incomplete["preflight"] = {
            "exit_code": 2,
            "conflicts": [],
            "errors": [{"code": "INCOMPLETE_VISIBILITY", "message": "safe status"}],
            "coverage": {"deferredPreconditionFolders": ["2026-10-14_render-live-r002"]},
            "limitation": "The catalog observation is point-in-time.",
        }
        incomplete["exitCode"] = 2
        for rendered in (
            api.render_migration_plan(incomplete, "text"),
            api.render_migration_plan(incomplete, "markdown"),
        ):
            with self.subTest(incomplete=rendered[:40]):
                self.assertIn("Preflight exit: 2", rendered)
                self.assertIn("INCOMPLETE_VISIBILITY", rendered)
                self.assertIn("1 error", rendered)

    def test_preflight_raw_messages_are_sanitized_in_all_renderers(self) -> None:
        self.add_folder("migrations/2026-10-13_sanitize-live-r001")
        api = self.require_plan()
        report = api.build_migration_plan(self.root)
        report.update({"mode": "live", "environment": "dev"})
        dangerous = "ORA-20001: SELECT private_value FROM secret_table; password=SECRET_MARKER_9K"
        report["preflight"] = {
            "exit_code": 2,
            "conflicts": [],
            "errors": [{
                "code": "CHECK_FAILED", "check": "precheck-orders",
                "path": "migrations/2026-10-13_sanitize-live-r001/001-step-1.sql",
                "errorCode": "ORA-20001", "message": dangerous,
                "observed": {"error": dangerous, "sql": dangerous},
            }],
            "coverage": {
                "deferredPreconditionFolders": ["2026-10-14_sanitize-live-r002"],
                "manual_review_required": [{
                    "migration": "2026-10-13_sanitize-live-r001", "file": "001-step-1.sql",
                    "reason": dangerous,
                }],
                "operations": [{"file": "001-step-1.sql", "sql": dangerous}],
            },
            "limitation": dangerous,
            "results": [{"sql": dangerous, "error": dangerous}],
        }
        report["exitCode"] = 2

        for output_format in ("json", "text", "markdown"):
            rendered = api.render_migration_plan(report, output_format)
            with self.subTest(output_format=output_format):
                self.assertIn("CHECK_FAILED", rendered)
                self.assertIn("precheck-orders", rendered)
                self.assertIn("ORA-20001", rendered)
                self.assertIn("sanitize-live-r001/001-step-1.sql", rendered)
                self.assertIn("manual review", rendered.casefold())
                self.assertNotIn("SELECT private_value", rendered)
                self.assertNotIn("secret_table", rendered)
                self.assertNotIn("SECRET_MARKER_9K", rendered)

    def test_main_offline_schema_report_never_reads_environment_or_sqlcl(self) -> None:
        self.add_folder("migrations/ALPHA/2026-10-13_cli-first-r001")
        self.add_folder("migrations/BRAVO/2026-10-13_cli-second-r001")
        api = self.require_plan()
        output = io.StringIO()
        errors = io.StringIO()
        with patch("scripts.local_config.read_project_env", side_effect=AssertionError("offline command read .env")), patch.object(
            api, "require_migration_sqlcl_version", side_effect=AssertionError("offline command started SQLcl")
        ), redirect_stdout(output), redirect_stderr(errors):
            status = api.main(["migrations/ALPHA/2026-10-13_cli-first-r001", "--schema", "ALPHA", "--format", "json"], repo_root=self.root)

        report = json.loads(output.getvalue())
        self.assertEqual(0, status)
        self.assertEqual("offline", report["mode"])
        self.assertEqual(["migrations/ALPHA/2026-10-13_cli-first-r001"], [row["path"] for row in report["migrations"]])
        self.assertEqual("ALPHA", report["migrations"][0]["schema"])
        self.assertEqual("", errors.getvalue())

    def test_main_rejects_ambiguous_live_flags_before_environment_or_sqlcl(self) -> None:
        api = self.require_plan()
        for arguments in (
            ["--env", "dev", "--format", "json"],
            ["--live", "--format", "json"],
            ["--live", "--env", "dev", "--format", "json"],
        ):
            output = io.StringIO()
            errors = io.StringIO()
            with self.subTest(arguments=arguments), patch(
                "scripts.local_config.read_project_env", side_effect=AssertionError("invalid request read .env")
            ) as read_env, patch.object(
                api, "require_migration_sqlcl_version", side_effect=AssertionError("invalid request started SQLcl")
            ) as sqlcl_guard, redirect_stdout(output), redirect_stderr(errors):
                status = api.main(arguments, repo_root=self.root)
            report = json.loads(output.getvalue())
            self.assertEqual(2, status)
            self.assertTrue(report["diagnostics"])
            self.assertIsNone(report["target"])
            read_env.assert_not_called()
            sqlcl_guard.assert_not_called()
            self.assertEqual("", errors.getvalue())

    def test_main_invalid_live_environment_is_canonical_and_secret_free(self) -> None:
        self.add_folder("migrations/2026-10-13_invalid-env-r001")
        api = self.require_plan()
        marker = "staging/SELECT password FROM private_table; password=SECRET_ENV_MARKER_4X/"

        for output_format in ("json", "text", "markdown"):
            output = io.StringIO()
            errors = io.StringIO()
            with self.subTest(output_format=output_format), patch(
                "scripts.local_config.read_project_env", side_effect=AssertionError("invalid env read .env")
            ) as read_env, patch.object(
                api, "require_migration_sqlcl_version", side_effect=AssertionError("invalid env started SQLcl")
            ) as sqlcl_guard, redirect_stdout(output), redirect_stderr(errors):
                status = api.main(
                    ["migrations/2026-10-13_invalid-env-r001", "--live", "--env", marker, "--format", output_format],
                    repo_root=self.root,
                )

            self.assertEqual(2, status)
            self.assertNotIn(marker, output.getvalue())
            self.assertNotIn("SECRET_ENV_MARKER_4X", output.getvalue())
            self.assertNotIn("SELECT password FROM private_table", output.getvalue())
            self.assertNotIn("password=", output.getvalue())
            if output_format == "json":
                self.assertTrue(output.getvalue().lstrip().startswith("{"), output.getvalue())
                self.assertIsNone(json.loads(output.getvalue())["environment"])
            read_env.assert_not_called()
            sqlcl_guard.assert_not_called()
            self.assertEqual("", errors.getvalue())

    def test_main_syntax_error_keeps_final_json_format_and_sanitizes_environment(self) -> None:
        self.add_folder("migrations/2026-10-13_syntax-env-r001")
        api = self.require_plan()
        marker = "SECRET_ENV_MARKER_8M/SELECT password FROM secrets"
        output = io.StringIO()
        errors = io.StringIO()
        with patch("scripts.local_config.read_project_env", side_effect=AssertionError("syntax error read .env")) as read_env, patch.object(
            api, "require_migration_sqlcl_version", side_effect=AssertionError("syntax error started SQLcl")
        ) as sqlcl_guard, redirect_stdout(output), redirect_stderr(errors):
            status = api.main(
                [
                    "migrations/2026-10-13_syntax-env-r001",
                    "--live",
                    "--env",
                    marker,
                    "--unknown-option",
                    "--format=json",
                ],
                repo_root=self.root,
            )

        self.assertEqual(2, status)
        self.assertTrue(output.getvalue().lstrip().startswith("{"), output.getvalue())
        report = json.loads(output.getvalue())
        self.assertEqual(1, report["schemaVersion"])
        self.assertIsNone(report["environment"])
        self.assertNotIn(marker, output.getvalue())
        self.assertNotIn("SECRET_ENV_MARKER_8M", output.getvalue())
        self.assertNotIn("SELECT password FROM secrets", output.getvalue())
        read_env.assert_not_called()
        sqlcl_guard.assert_not_called()
        self.assertEqual("", errors.getvalue())

    def test_main_rejects_abbreviated_live_option_before_environment_or_sqlcl(self) -> None:
        self.add_folder("migrations/2026-10-13_abbreviated-option-r001")
        api = self.require_plan()
        output = io.StringIO()
        errors = io.StringIO()
        with patch("scripts.local_config.read_project_env", side_effect=AssertionError("abbreviated option read .env")) as read_env, patch.object(
            api, "require_migration_sqlcl_version", side_effect=AssertionError("abbreviated option started SQLcl")
        ) as sqlcl_guard, redirect_stdout(output), redirect_stderr(errors):
            status = api.main(
                ["migrations/2026-10-13_abbreviated-option-r001", "--live", "--en", "dev", "--format=json"],
                repo_root=self.root,
            )

        self.assertEqual(2, status)
        report = json.loads(output.getvalue())
        self.assertIsNone(report["environment"])
        self.assertIn("invalid-arguments", [item["code"] for item in report["diagnostics"]])
        read_env.assert_not_called()
        sqlcl_guard.assert_not_called()
        self.assertEqual("", errors.getvalue())

    def test_main_missing_option_value_does_not_hide_trailing_json_format(self) -> None:
        self.add_folder("migrations/2026-10-13_missing-option-value-r001")
        api = self.require_plan()
        output = io.StringIO()
        errors = io.StringIO()
        with patch("scripts.local_config.read_project_env", side_effect=AssertionError("syntax error read .env")) as read_env, patch.object(
            api, "require_migration_sqlcl_version", side_effect=AssertionError("syntax error started SQLcl")
        ) as sqlcl_guard, redirect_stdout(output), redirect_stderr(errors):
            status = api.main(
                ["migrations/2026-10-13_missing-option-value-r001", "--live", "--env", "--format=json"],
                repo_root=self.root,
            )

        self.assertEqual(2, status)
        self.assertTrue(output.getvalue().lstrip().startswith("{"), output.getvalue())
        report = json.loads(output.getvalue())
        self.assertIsNone(report["environment"])
        self.assertIn("invalid-arguments", [item["code"] for item in report["diagnostics"]])
        read_env.assert_not_called()
        sqlcl_guard.assert_not_called()
        self.assertEqual("", errors.getvalue())

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

    def test_live_requires_explicit_one_owner_selection(self) -> None:
        from scripts import migration_checks

        alpha = self.add_folder("migrations/ALPHA/2026-10-13_first-owner-r001")
        bravo = self.add_folder("migrations/BRAVO/2026-10-13_second-owner-r001")
        incomplete_folder = self.add_folder("migrations/2026-10-14_missing-profile-r001")
        api = self.require_plan()
        values = {
            "DB_ENVIRONMENT": "development",
            "CODE_SCHEMA": "ALPHA,BRAVO",
            "CODE_SQLCL_CONNECTION": "dev-alpha,dev-bravo",
            "CODE_EXPECTED_USER": "ALPHA,BRAVO",
        }
        preflight_patch = patch.object(migration_checks, "preflight_selected", create=True)

        with patch.object(api, "require_migration_sqlcl_version") as runtime_guard, preflight_patch as selected:
            discovered = api.build_live_migration_plan(
                self.root, (), environment="dev", schema=None, values=values
            )
            no_environment = api.build_live_migration_plan(
                self.root, [alpha.relative_to(self.root).as_posix()], environment=None, schema=None, values=values
            )
            mixed = api.build_live_migration_plan(
                self.root,
                [alpha.relative_to(self.root).as_posix(), bravo.relative_to(self.root).as_posix()],
                environment="dev",
                schema=None,
                values=values,
            )
            incomplete = api.build_live_migration_plan(
                self.root,
                [incomplete_folder.relative_to(self.root).as_posix()],
                environment="dev",
                schema=None,
                values={"DB_ENVIRONMENT": "development"},
            )

        for report in (discovered, no_environment, mixed, incomplete):
            self.assertEqual(2, report["exitCode"], report)
            self.assertIsNone(report["target"])
        self.assertEqual("discovered", discovered["selection"])
        self.assertTrue(discovered["diagnostics"])
        self.assertFalse(runtime_guard.called, "invalid live selections must refuse before probing SQLcl")
        self.assertFalse(selected.called, "invalid live selections must refuse before database preflight")

    def test_runtime_floor_refuses_before_capture(self) -> None:
        from types import SimpleNamespace

        from scripts import migration_checks, migration_runtime
        from scripts.sqlcl_session import bash_command

        runtime_folder = self.add_folder("migrations/2026-10-13_runtime-floor-r001")
        (runtime_folder / "001-step-1.sql").write_text(
            "CREATE TABLE zz_new (id NUMBER);\n", encoding="utf-8", newline="\n"
        )
        api = self.require_plan()
        values = {
            "DB_ENVIRONMENT": "development",
            "CODE_SCHEMA": "APP",
            "CODE_SQLCL_CONNECTION": "dev-profile",
            "CODE_EXPECTED_USER": "APP",
        }
        preflight_patch = patch.object(migration_checks, "preflight_selected", create=True)
        cases = (
            ("old", "SQLcl: Release 26.2.2.0 Production\n"),
            ("unreadable", "SQLcl is unavailable\n"),
        )

        for label, output in cases:
            with self.subTest(version=label):
                calls = []

                def runner(arguments, _calls=calls, _output=output, **kwargs):
                    _calls.append((arguments, kwargs))
                    return SimpleNamespace(returncode=0, stdout=_output, stderr="")

                def refuse_for_this_version():
                    return migration_runtime.require_migration_sqlcl_version(runner=runner)

                with patch.object(api, "require_migration_sqlcl_version", side_effect=refuse_for_this_version), preflight_patch as selected:
                    report = api.build_live_migration_plan(
                        self.root,
                        ["migrations/2026-10-13_runtime-floor-r001"],
                        environment="dev",
                        schema=None,
                        values=values,
                    )

                self.assertEqual(2, report["exitCode"], report)
                self.assertIsNone(report["target"])
                self.assertIsNone(report["preflight"])
                self.assertTrue(report["diagnostics"])
                self.assertEqual(1, len(calls))
                arguments = calls[0][0]
                self.assertEqual(bash_command(), arguments[0])
                self.assertEqual("-c", arguments[1])
                self.assertIn('source "$1/scripts/sqlcl_safe.sh"', arguments[2])
                self.assertIn('invoke_sqlcl_safe "$2" -V', arguments[2])
                self.assertEqual("migration-runtime", arguments[3])
                self.assertNotIn("-name", arguments)
                self.assertFalse(selected.called, "version refusal must happen before database preflight")

    def test_live_hashes_remain_bound_if_selected_files_change_during_capture(self) -> None:
        import json as json_module

        from scripts import migration_plan, schema_catalog
        from scripts.migration_checks import CheckReport
        from scripts.schema_catalog import SchemaSnapshot

        values = {
            "DB_ENVIRONMENT": "development",
            "CODE_SCHEMA": "APP",
            "CODE_SQLCL_CONNECTION": "dev-profile",
            "CODE_EXPECTED_USER": "APP",
        }
        identity = {
            "session_user": "APP",
            "current_schema": "APP",
            "db_name": "DEVDB",
            "db_unique_name": "DEVDB1",
            "service_name": "dev",
            "container_id": "3",
            "container_name": "APP_PDB",
            "edition": "ORA$BASE",
            "database_version": "23.26.3.0.0",
        }
        snapshot = SchemaSnapshot(
            identity, {}, {}, {"ownerComplete": True, "catalogs": ["ALL_OBJECTS"]},
            "2026-10-13T10:00:00Z", "2026-10-13T10:00:01Z",
        )
        checks = CheckReport(True, True, (), (), {"complete": True})
        api = self.require_plan()

        for changed_file in ("sql", "checks"):
            with self.subTest(changed_file=changed_file):
                folder = self.add_folder(f"migrations/2026-10-13_hash-binding-{changed_file}-r001")
                sql_path = folder / "001-step-1.sql"
                checks_path = folder / "checks.json"
                sql_path.write_text("CREATE TABLE zz_original (id NUMBER);\n", encoding="utf-8", newline="\n")

                def capture(
                    target,
                    _keys,
                    _run_dir,
                    _changed_file=changed_file,
                    _sql_path=sql_path,
                    _checks_path=checks_path,
                    **_kwargs,
                ):
                    schema_catalog._target_identity(snapshot.identity, target)
                    if _changed_file == "sql":
                        _sql_path.write_text("CREATE TABLE zz_changed (id NUMBER);\n", encoding="utf-8", newline="\n")
                    else:
                        payload = json_module.loads(_checks_path.read_text(encoding="utf-8"))
                        _checks_path.write_text(
                            json_module.dumps(payload, separators=(",", ":")) + "\n",
                            encoding="utf-8",
                            newline="\n",
                        )
                    return None, snapshot

                with patch.object(migration_plan, "require_migration_sqlcl_version"), patch(
                    "scripts.schema_catalog.capture_inventory_snapshot_with_retries", side_effect=capture
                ), patch("scripts.migration_checks.run_checks", return_value=checks):
                    report = api.build_live_migration_plan(
                        self.root,
                        [folder.relative_to(self.root).as_posix()],
                        environment="dev",
                        schema=None,
                        values=values,
                    )

                self.assertEqual(2, report["exitCode"], report)
                changed_path = (
                    f"{folder.relative_to(self.root).as_posix()}/"
                    f"{'001-step-1.sql' if changed_file == 'sql' else 'checks.json'}"
                )
                self.assertTrue(
                    any(
                        item["code"] == "selected-input-changed" and item["path"] == changed_path
                        for item in report["diagnostics"]
                    ),
                    report,
                )
                self.assertNotIn("CREATE TABLE zz_changed", json_module.dumps(report))
                self.assertNotIn('"expected":1', json_module.dumps(report))

    def test_live_hashes_detect_deleted_last_step_during_capture(self) -> None:
        import json as json_module

        from scripts import schema_catalog
        from scripts.migration_checks import CheckReport
        from scripts.schema_catalog import SchemaSnapshot

        values = {
            "DB_ENVIRONMENT": "development",
            "CODE_SCHEMA": "APP",
            "CODE_SQLCL_CONNECTION": "dev-profile",
            "CODE_EXPECTED_USER": "APP",
        }
        identity = {
            "session_user": "APP",
            "current_schema": "APP",
            "db_name": "DEVDB",
            "db_unique_name": "DEVDB1",
            "service_name": "dev",
            "container_id": "3",
            "container_name": "APP_PDB",
            "edition": "ORA$BASE",
            "database_version": "23.26.3.0.0",
        }
        snapshot = SchemaSnapshot(
            identity, {}, {}, {"ownerComplete": True, "catalogs": ["ALL_OBJECTS"]},
            "2026-10-13T10:00:00Z", "2026-10-13T10:00:01Z",
        )
        checks = CheckReport(True, True, (), (), {"complete": True})
        api = self.require_plan()
        folder = self.add_folder("migrations/2026-10-13_hash-deletion-r001", sql_files=2)
        first_step = folder / "001-step-1.sql"
        last_step = folder / "002-step-2.sql"
        first_step.write_text("CREATE TABLE zz_kept (id NUMBER);\n", encoding="utf-8", newline="\n")
        last_step.write_text("CREATE TABLE zz_deleted_private (id NUMBER);\n", encoding="utf-8", newline="\n")
        selected_path = folder.relative_to(self.root).as_posix()
        original_record = api.build_migration_plan(self.root, [selected_path])["migrations"][0]

        def capture(target, _keys, _run_dir, **_kwargs):
            schema_catalog._target_identity(snapshot.identity, target)
            last_step.unlink()
            return None, snapshot

        with patch.object(api, "require_migration_sqlcl_version"), patch(
            "scripts.schema_catalog.capture_inventory_snapshot_with_retries", side_effect=capture
        ), patch("scripts.migration_checks.run_checks", return_value=checks):
            report = api.build_live_migration_plan(
                self.root,
                [selected_path],
                environment="dev",
                schema=None,
                values=values,
            )

        missing_path = f"{selected_path}/002-step-2.sql"
        self.assertEqual(2, report["exitCode"], report)
        self.assertFalse(report["preflight"]["coverage"]["selectedInputHashesVerified"])
        self.assertTrue(
            any(
                error.get("code") == "SELECTED_INPUT_CHANGED" and error.get("path") == missing_path
                for error in report["preflight"]["errors"]
            ),
            report["preflight"],
        )
        self.assertTrue(
            any(
                item["code"] == "selected-input-changed" and item["path"] == missing_path
                for item in report["diagnostics"]
            ),
            report["diagnostics"],
        )
        current_record = report["migrations"][0]
        self.assertEqual(original_record["files"], current_record["files"])
        self.assertEqual(original_record["payloadDigest"], current_record["payloadDigest"])
        self.assertEqual(original_record["checksSha256"], current_record["checksSha256"])
        self.assertEqual(2, report["summary"]["sqlFileCount"])
        serialized = json_module.dumps(report)
        self.assertNotIn("CREATE TABLE zz_kept", serialized)
        self.assertNotIn("CREATE TABLE zz_deleted_private", serialized)

    def test_live_retains_guarded_preflight_diagnostics(self) -> None:
        from scripts import migration_checks, migration_plan, schema_catalog
        from scripts.migration_checks import CheckReport
        from scripts.schema_catalog import CatalogError, ObjectKey, SchemaSnapshot

        values = {
            "DB_ENVIRONMENT": "development",
            "CODE_SCHEMA": "APP",
            "CODE_SQLCL_CONNECTION": "dev-profile",
            "CODE_EXPECTED_USER": "APP",
        }
        identity = {
            "session_user": "APP",
            "current_schema": "APP",
            "db_name": "DEVDB",
            "db_unique_name": "DEVDB1",
            "service_name": "dev",
            "container_id": "3",
            "container_name": "APP_PDB",
            "edition": "ORA$BASE",
            "database_version": "23.26.3.0.0",
        }
        coverage = {"ownerComplete": True, "catalogs": ["ALL_OBJECTS"]}
        api = self.require_plan()

        def add_live_folder(name: str, sql: str) -> Path:
            folder = self.add_folder(name, sql_files=1)
            (folder / "001-step-1.sql").write_text(sql, encoding="utf-8", newline="\n")
            return folder

        def snapshot_for(*, objects=(), observed_identity=identity):
            inventory = {
                ObjectKey("APP", name, object_type): {"owner": "APP", "name": name, "type": object_type, "status": "VALID"}
                for name, object_type in objects
            }
            return SchemaSnapshot(
                dict(observed_identity), inventory, {}, dict(coverage),
                "2026-10-13T10:00:00Z", "2026-10-13T10:00:01Z",
            )

        def capture(snapshot):
            def selected(target, _keys, _run_dir, **_kwargs):
                schema_catalog._target_identity(snapshot.identity, target)
                return None, snapshot
            return selected

        checks = CheckReport(True, True, (), (), {"complete": True})
        success_folder = add_live_folder(
            "migrations/2026-10-13_live-success-r001", "CREATE TABLE zz_fresh (id NUMBER);\n"
        )
        with patch.object(migration_plan, "require_migration_sqlcl_version"), patch(
            "scripts.schema_catalog.capture_inventory_snapshot_with_retries",
            side_effect=capture(snapshot_for()),
        ), patch("scripts.migration_checks.run_checks", return_value=checks):
            success = api.build_live_migration_plan(
                self.root, [success_folder.relative_to(self.root).as_posix()],
                environment="dev", schema=None, values=values,
            )
        self.assertEqual(0, success["exitCode"], success)
        self.assertEqual(identity, success["target"])
        self.assertEqual(identity, success["preflight"]["coverage"]["observedIdentity"])
        self.assertEqual([], success["preflight"]["coverage"]["deferredPreconditionFolders"])
        self.assertIn(migration_checks.LIMITATION, success["preflight"]["limitation"])

        production_folder = add_live_folder(
            "migrations/2026-10-13_live-production-identity-r001", "CREATE TABLE zz_new (id NUMBER);\n"
        )
        production_identity = {**identity, "db_unique_name": "PRODDB1"}
        with patch.object(migration_plan, "require_migration_sqlcl_version"), patch(
            "scripts.schema_catalog.capture_inventory_snapshot_with_retries",
            side_effect=capture(snapshot_for(observed_identity=production_identity)),
        ) as production_capture, patch("scripts.migration_checks.run_checks", return_value=checks) as run_checks:
            production = api.build_live_migration_plan(
                self.root, [production_folder.relative_to(self.root).as_posix()],
                environment="dev", schema=None, values=values,
            )
        self.assertEqual(2, production["exitCode"], production)
        self.assertIsNone(production["target"])
        self.assertTrue(production["preflight"]["errors"])
        self.assertFalse(run_checks.called)
        self.assertTrue(production_capture.called)

        incomplete_folder = add_live_folder(
            "migrations/2026-10-13_live-incomplete-r001", "CREATE TABLE zz_new (id NUMBER);\n"
        )
        with patch.object(migration_plan, "require_migration_sqlcl_version"), patch(
            "scripts.schema_catalog.capture_inventory_snapshot_with_retries",
            side_effect=CatalogError("catalog capture is incomplete"),
        ):
            incomplete = api.build_live_migration_plan(
                self.root, [incomplete_folder.relative_to(self.root).as_posix()],
                environment="dev", schema=None, values=values,
            )
        self.assertEqual(2, incomplete["exitCode"], incomplete)
        self.assertIsNone(incomplete["target"])
        self.assertTrue(incomplete["preflight"]["errors"])
        incomplete_error_codes = {error.get("code") for error in incomplete["preflight"]["errors"]}
        incomplete_diagnostic_codes = {item["code"] for item in incomplete["diagnostics"]}
        self.assertIn("LIVE_PREFLIGHT_UNAVAILABLE", incomplete_error_codes)
        self.assertNotIn("OBSERVED_IDENTITY_INCOMPLETE", incomplete_error_codes)
        self.assertNotIn("live-target-identity-incomplete", incomplete_diagnostic_codes)

        conflict_folder = add_live_folder(
            "migrations/2026-10-13_live-conflict-r001", "CREATE TABLE zz_occupied (id NUMBER);\n"
        )
        with patch.object(migration_plan, "require_migration_sqlcl_version"), patch(
            "scripts.schema_catalog.capture_inventory_snapshot_with_retries",
            side_effect=capture(snapshot_for(objects=(("ZZ_OCCUPIED", "TABLE"),))),
        ), patch("scripts.migration_checks.run_checks", return_value=checks):
            conflict = api.build_live_migration_plan(
                self.root, [conflict_folder.relative_to(self.root).as_posix()],
                environment="dev", schema=None, values=values,
            )
        self.assertEqual(1, conflict["exitCode"], conflict)
        self.assertEqual(identity, conflict["target"])
        self.assertTrue(conflict["preflight"]["conflicts"])

        locked_folder = add_live_folder(
            "migrations/2026-10-13_live-locked-r001", "CREATE TABLE zz_locked (id NUMBER);\n"
        )
        self.add_receipt(locked_folder)
        with patch.object(migration_plan, "require_migration_sqlcl_version"), patch(
            "scripts.schema_catalog.capture_inventory_snapshot_with_retries",
            side_effect=capture(snapshot_for()),
        ), patch("scripts.migration_checks.run_checks", return_value=checks):
            locked = api.build_live_migration_plan(
                self.root, [locked_folder.relative_to(self.root).as_posix()],
                environment="dev", schema=None, values=values,
            )
        self.assertEqual(1, locked["exitCode"], locked)
        self.assertEqual("locked", locked["migrations"][0]["localEvidence"]["status"])
        self.assertNotIn("ready", json.dumps(locked["migrations"][0]["localEvidence"]).casefold())

        unknown_folder = add_live_folder(
            "migrations/2026-10-13_live-unknown-r001", "CREATE TABLE zz_unknown (id NUMBER);\n"
        )
        (unknown_folder / "status.dev.json").write_text("not json\n", encoding="utf-8")
        with patch.object(migration_plan, "require_migration_sqlcl_version") as runtime_guard, patch.object(
            migration_checks, "preflight_selected", create=True
        ) as selected:
            unknown = api.build_live_migration_plan(
                self.root, [unknown_folder.relative_to(self.root).as_posix()],
                environment="dev", schema=None, values=values,
            )
        self.assertEqual(2, unknown["exitCode"], unknown)
        self.assertEqual("unknown", unknown["migrations"][0]["localEvidence"]["status"])
        self.assertIsNone(unknown["target"])
        self.assertFalse(runtime_guard.called)
        self.assertFalse(selected.called)

    def test_live_report_fails_closed_on_partial_or_missing_external_identity(self) -> None:
        from scripts import migration_checks, migration_plan
        from scripts.migration_checks import PreflightReport

        values = {
            "DB_ENVIRONMENT": "development",
            "CODE_SCHEMA": "APP",
            "CODE_SQLCL_CONNECTION": "dev-profile",
            "CODE_EXPECTED_USER": "APP",
        }
        folder = self.add_folder("migrations/2026-10-14_external-identity-r001")
        selected_path = folder.relative_to(self.root).as_posix()
        cases = (
            ("missing", {"complete": True}, (), ()),
            ("partial", {"complete": True, "observedIdentity": {"current_schema": "APP", "db_name": "DB_SECRET_MARKER"}}, (), ()),
            (
                "partial-with-existing-diagnostics",
                {"complete": True, "observedIdentity": {"current_schema": "APP", "db_name": "DB_SECRET_MARKER"}},
                ({"code": "LIVE_NAMESPACE_OCCUPIED", "name": "ZZ_EXISTING"},),
                ({"code": "EXISTING_ERROR", "message": "Existing safe diagnostic"},),
            ),
        )

        for label, coverage, conflicts, errors in cases:
            with self.subTest(identity=label):
                preflight = PreflightReport(
                    0, conflicts, errors,
                    coverage,
                    migration_checks.LIMITATION,
                )
                with patch.object(migration_plan, "require_migration_sqlcl_version"), patch.object(
                    migration_checks, "preflight_selected", return_value=preflight
                ):
                    report = self.require_plan().build_live_migration_plan(
                        self.root, [selected_path], environment="dev", schema=None, values=values,
                    )

                self.assertEqual(2, report["exitCode"], report)
                self.assertIsNone(report["target"])
                self.assertFalse(report["preflight"]["coverage"]["complete"])
                self.assertEqual(2, report["preflight"]["exit_code"])
                self.assertNotIn("observedIdentity", report["preflight"]["coverage"])
                self.assertTrue(any(error.get("code") == "OBSERVED_IDENTITY_INCOMPLETE" for error in report["preflight"]["errors"]))
                if errors:
                    self.assertTrue(any(error.get("code") == "EXISTING_ERROR" for error in report["preflight"]["errors"]))
                if conflicts:
                    self.assertTrue(any(item.get("code") == "LIVE_NAMESPACE_OCCUPIED" for item in report["preflight"]["conflicts"]))
                self.assertTrue(any(item["code"] == "live-target-identity-incomplete" for item in report["diagnostics"]))
                self.assertNotIn("DB_SECRET_MARKER", json.dumps(report))


if __name__ == "__main__":
    unittest.main()
