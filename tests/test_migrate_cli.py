import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fake_sqlcl import environment as fake_environment, install
from scripts.db_targets import Target
from scripts.migrate import MigrationApplyError, _assert_check_identity, apply_batch, apply_folder, main
from scripts.migration_checks import CheckReport, analyze_batch, run_checks
from scripts.migration_manifest import load_batch, validate_receipt
from scripts.migration_revision import inspect_migration_lock
from scripts.schema_catalog import ObjectDefinition, ObjectKey, SchemaInventory, SchemaSnapshot
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)


ROOT = Path(__file__).resolve().parents[1]


def now_text():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class FakeDatabase:
    def __init__(self, target):
        self.target = target
        self.objects = {}
        self.calls = []
        self.fail_file = None
        self.mutate_source_during_apply = None
        self.source_file_to_mutate = None
        self.change_identity_after_apply = False
        self.check_identity_override = {}
        self.apply_expected_identities = []
        # check id -> (object name, object type) that must exist for it to pass
        self.check_requires_object = {}

    def identity(self):
        environment = self.target.environment
        return {
            "session_user": self.target.expected_user,
            "current_schema": self.target.schema,
            "db_name": f"{environment.upper()}DB",
            "db_unique_name": f"{environment.upper()}DB_UNIQUE",
            "service_name": f"{environment}.service",
            "container_id": "3",
            "container_name": "APP_PDB",
            "edition": "ORA$BASE",
            "database_version": "19.0",
        }

    def _inventory(self):
        identity = self.identity()
        rows = {}
        for (name, object_type), details in self.objects.items():
            rows[ObjectKey(self.target.schema, name, object_type)] = {
                "owner": self.target.schema,
                "name": name,
                "type": object_type,
                "status": details.get("status", "VALID"),
                "last_ddl_time": "2026-09-28T10:00:00",
            }
        return SchemaInventory(
            identity,
            rows,
            {"ownerComplete": True, "path": "OWNER_SESSION", "catalogs": ["ALL_OBJECTS", "ALL_TABLES", "ALL_TAB_COLUMNS", "ALL_VIEWS", "ALL_SEQUENCES", "ALL_CONSTRAINTS", "ALL_CONS_COLUMNS", "ALL_INDEXES", "ALL_IND_COLUMNS", "ALL_TRIGGERS"]},
            now_text(),
            now_text(),
        )

    def capture_inventory(self, target, _run_dir):
        self.calls.append(("inventory", target.environment))
        return self._inventory()

    def capture_snapshot(self, target, inventory, keys, _run_dir):
        self.calls.append(("snapshot", target.environment, tuple(keys)))
        definitions = {}
        for name, object_type in keys:
            details = self.objects.get((name, object_type))
            if details is None:
                continue
            key = ObjectKey(target.schema, name, object_type)
            attrs = {}
            if object_type == "TABLE":
                attrs["columns"] = [{"name": column, "data_type": "NUMBER", "nullable": "Y", "column_id": index} for index, column in enumerate(sorted(details.get("columns", set())), start=1)]
            definitions[key] = ObjectDefinition(key, attrs, details.get("ddl", f"CREATE {object_type} {target.schema}.{name}"), (), details.get("status", "VALID") == "VALID")
        return SchemaSnapshot(inventory.identity, inventory.objects, definitions, inventory.coverage, inventory.started_at, inventory.completed_at)

    def run_checks(self, target, checks, run_dir, *, phase):
        self.calls.append(("checks", target.environment, phase, tuple(check.id for check in checks)))
        missing = [check.id for check in checks if check.id in self.check_requires_object and self.check_requires_object[check.id] not in self.objects]
        results = tuple(
            {"id": check.id, "row_count": 1, "column_count": 1, "numeric": True, "value": 0 if check.id in missing else 1, "passed": check.id not in missing}
            for check in checks
        )
        errors = tuple({"code": "CHECK_FAILED", "check": check_id, "message": "ORA-00942: table or view does not exist"} for check_id in missing)
        identity = self.identity()
        if self.check_identity_override and phase in self.check_identity_override:
            identity = {**identity, **self.check_identity_override[phase]}
        return CheckReport(not missing, True, results, errors, {"phase": phase, "complete": True, "identity": identity})

    def apply_folder(self, migration, target, run_dir, *, expected_identity=None):
        self.calls.append(("apply", migration.folder.name, tuple(file.name for file in migration.files)))
        self.apply_expected_identities.append(expected_identity)
        operations = analyze_batch((migration,), target.schema)
        operations_by_file = {}
        for operation in operations:
            operations_by_file.setdefault(operation["file"], []).append(operation)
        executed = []
        for file in migration.files:
            self.calls.append(("sql-file", file.name))
            executed.append(file.name)
            for operation in operations_by_file.get(file.name, ()):
                kind = operation["kind"]
                name = operation.get("name")
                if kind in {"CREATE_TABLE", "CREATE_VIEW", "CREATE_SEQUENCE", "CREATE_INDEX"}:
                    self.objects[(name, operation["object_type"])] = {
                        "columns": set(operation.get("columns", ())),
                        "status": "VALID",
                        "ddl": file.source.decode("utf-8").strip(),
                    }
                elif kind == "ALTER_ADD_COLUMN":
                    self.objects[(operation["table"], "TABLE")]["columns"].update(operation["columns"])
            if file.name == self.fail_file:
                raise MigrationApplyError(f"simulated apply failure after {file.name}")
            if self.mutate_source_during_apply == file.name:
                self.source_file_to_mutate.write_text("CREATE TABLE CHANGED_AFTER_FREEZE (ID NUMBER);\n", encoding="utf-8")
        identity = self.identity()
        if self.change_identity_after_apply:
            identity["db_unique_name"] = "OTHERDB_UNIQUE"
        return {
            "committed": True,
            "applyStartedAt": now_text(),
            "applyCompletedAt": now_text(),
            "identity": identity,
            "payloadDigest": migration.payload_digest,
            "executed_files": executed,
        }


class MigrateCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_the_exit_status_is_also_written_where_migrate_sh_reads_it(self):
        # On Windows Git Bash can report 130 for Python's status; migrate.sh reads this file.
        status_file = self.root / "status"
        result = subprocess.run(
            [sys.executable, "-m", "scripts.migrate"], cwd=ROOT, capture_output=True, text=True, check=False,
            env={**os.environ, "MIGRATE_STATUS_FILE": str(status_file)},
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(status_file.read_text(encoding="ascii"), "2\n")

    def add_folder(self, name, files, *, preconditions=(), postconditions=None):
        folder = self.root / "migrations" / name
        folder.mkdir()
        for filename, content in files.items():
            (folder / filename).write_text(content, encoding="utf-8", newline="\n")
        checks = {
            "schemaVersion": 1,
            "preconditions": list(preconditions),
            "postconditions": list(postconditions or [{"id": "after-change", "sql": "SELECT 1 FROM dual", "expected": 1}]),
        }
        (folder / "checks.json").write_text(json.dumps(checks) + "\n", encoding="utf-8")
        return folder

    def load(self, *names):
        return load_batch(self.root, [f"migrations/{name}" for name in names])

    def target(self, environment="dev"):
        return Target(environment, f"{environment}-profile", f"LOGIN_{environment.upper()}", f"APP_{environment.upper()}", {"dev": "development", "staging": "staging", "prod": "production"}[environment])

    def apply(self, migrations, target=None, *, confirm=None, fake=None, **overrides):
        target = target or self.target()
        fake = fake or FakeDatabase(target)
        capture_inventory_fn = overrides.pop("capture_inventory_fn", fake.capture_inventory)
        capture_snapshot_fn = overrides.pop("capture_snapshot_fn", fake.capture_snapshot)
        run_checks_fn = overrides.pop("run_checks_fn", fake.run_checks)
        apply_folder_fn = overrides.pop("apply_folder_fn", fake.apply_folder)
        result = apply_batch(
            self.root,
            migrations,
            target,
            confirm or (lambda _prompt: True),
            capture_inventory_fn=capture_inventory_fn,
            capture_snapshot_fn=capture_snapshot_fn,
            run_checks_fn=run_checks_fn,
            apply_folder_fn=apply_folder_fn,
            **overrides,
        )
        return result, fake

    def test_apply_freezes_order_verifies_and_writes_receipt(self):
        folder = self.add_folder("2026-09-28_create-orders-r001", {
            "001-create-table.sql": "CREATE TABLE ORDERS (ID NUMBER);\n",
            "002-add-status.sql": "ALTER TABLE ORDERS ADD STATUS VARCHAR2(20);\n",
        })
        migration = self.load(folder.name)

        result, fake = self.apply(migration)

        self.assertEqual(result, 0)
        self.assertEqual([call[1] for call in fake.calls if call[0] == "sql-file"], ["001-create-table.sql", "002-add-status.sql"])
        receipt = validate_receipt(folder / "status.dev.json", migration[0], {
            "environment": "dev", "connection": "dev-profile", "expected_user": "LOGIN_DEV", **fake.identity(),
        })
        self.assertEqual(receipt["state"], "verified")
        self.assertEqual([item["kind"] for item in receipt["checks"] if item.get("kind")], ["catalog", "catalog"])
        self.assertNotIn("developer", json.dumps(receipt).casefold())
        self.assertNotIn("password", json.dumps(receipt).casefold())

    def test_stage_and_prod_require_exact_confirmation_and_decline_performs_no_apply(self):
        for environment in ("staging", "prod"):
            with self.subTest(environment=environment):
                folder = self.add_folder(f"2026-09-28_create-{environment}-r001", {"001-create-table.sql": "CREATE TABLE T (ID NUMBER);\n"})
                migration = self.load(folder.name)
                target = self.target(environment)
                prompts = []
                result, fake = self.apply(migration, target, confirm=lambda prompt, prompts=prompts: prompts.append(prompt) or False)
                self.assertEqual(result, 1)
                self.assertEqual(prompts, [f"Migrating to {environment.upper()}. Proceed? [y/N]"])
                self.assertFalse(any(call[0] == "apply" for call in fake.calls))
                self.assertFalse((folder / f"status.{environment}.json").exists())

    def test_second_file_failure_stops_third_and_later_folder_without_receipt(self):
        first = self.add_folder("2026-09-28_create-first-r001", {
            "001-create-a.sql": "CREATE TABLE A (ID NUMBER);\n",
            "002-fail.sql": "CREATE TABLE B (ID NUMBER);\n",
            "003-create-c.sql": "CREATE TABLE C (ID NUMBER);\n",
        })
        later = self.add_folder("2026-09-28_create-later-r001", {"001-create-d.sql": "CREATE TABLE D (ID NUMBER);\n"})
        migrations = self.load(first.name, later.name)
        fake = FakeDatabase(self.target())
        fake.fail_file = "002-fail.sql"

        result, fake = self.apply(migrations, fake=fake)

        self.assertEqual(result, 2)
        executed = [call[1] for call in fake.calls if call[0] == "sql-file"]
        self.assertEqual(executed, ["001-create-a.sql", "002-fail.sql"])
        self.assertFalse((first / "status.dev.json").exists())
        self.assertFalse((later / "status.dev.json").exists())
        manifests = list((self.root / "scratch").glob("migration-attempt-*/run-manifest.json"))
        self.assertEqual(len(manifests), 1)
        run = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertTrue(run["migrations"][0]["writeAttempted"])

    def test_failed_attempt_without_receipt_blocks_automatic_replay(self):
        folder = self.add_folder("2026-09-28_create-replay-r001", {"001-create-a.sql": "CREATE TABLE A (ID NUMBER);\n"})
        migrations = self.load(folder.name)
        fake = FakeDatabase(self.target())
        fake.fail_file = "001-create-a.sql"
        failed, _ = self.apply(migrations, fake=fake)
        self.assertEqual(failed, 2)
        fake.fail_file = None
        apply_count = len([call for call in fake.calls if call[0] == "apply"])

        retry, fake = self.apply(migrations, fake=fake)

        self.assertEqual(retry, 2)
        self.assertEqual(len([call for call in fake.calls if call[0] == "apply"]), apply_count)
        self.assertFalse((folder / "status.dev.json").exists())

    def test_identity_guard_refusal_is_recorded_as_not_started_and_allows_retry(self):
        folder = self.add_folder(
            "2026-09-28_identity-refusal-r001",
            {"001-insert-row.sql": "INSERT INTO T (ID) VALUES (1);\n"},
            preconditions=[{"id": "before-row", "sql": "SELECT 1 FROM dual", "expected": 1}],
        )
        migrations = self.load(folder.name)
        fake = FakeDatabase(self.target())
        identity = json.dumps(fake.identity(), separators=(",", ":"))
        refusal = (
            "MIGRATION_IDENTITY_BEGIN\n" + identity + "\nMIGRATION_IDENTITY_END\n"
            "MIGRATION_IDENTITY_VERIFIED\nORA-20987: MIGRATION_IDENTITY_GUARD_REFUSED: target differs\n"
        )
        binary = install(self.root / "fake-bin", "cat <<'FAKE_SQLCL_OUTPUT'\n" + refusal + "FAKE_SQLCL_OUTPUT\nexit 1\n")
        stderr = io.StringIO()

        with patch.dict("os.environ", fake_environment(binary)), contextlib.redirect_stderr(stderr):
            failed, _ = self.apply(migrations, fake=fake, apply_folder_fn=apply_folder)

        self.assertEqual(failed, 2)
        self.assertIn("no payload statement started", stderr.getvalue())
        manifest = next((self.root / "scratch").glob("migration-attempt-*/run-manifest.json"))
        recorded = json.loads(manifest.read_text(encoding="utf-8"))["migrations"][0]
        self.assertEqual(recorded["state"], "apply-not-started")
        self.assertFalse(recorded["writeAttempted"])
        self.assertEqual(inspect_migration_lock(self.root, f"migrations/{folder.name}").status, "unlocked")

        success = (
            "MIGRATION_IDENTITY_BEGIN\n" + identity + "\nMIGRATION_IDENTITY_END\n"
            "MIGRATION_IDENTITY_VERIFIED\nMIGRATION_PAYLOAD_STARTED:001-insert-row.sql\n"
            "1 row inserted.\nMIGRATION_APPLY_COMPLETED\n"
        )
        install(self.root / "fake-bin", "cat <<'FAKE_SQLCL_OUTPUT'\n" + success + "FAKE_SQLCL_OUTPUT\n")
        with patch.dict("os.environ", fake_environment(binary)):
            retried, _ = self.apply(migrations, fake=fake, apply_folder_fn=apply_folder)

        self.assertEqual(retried, 0)
        self.assertTrue((folder / "status.dev.json").is_file())

    def test_connection_refusal_before_payload_is_recorded_as_not_started(self):
        folder = self.add_folder(
            "2026-09-28_connection-refusal-r001",
            {"001-insert-row.sql": "INSERT INTO T (ID) VALUES (1);\n"},
            preconditions=[{"id": "before-row", "sql": "SELECT 1 FROM dual", "expected": 1}],
        )
        migrations = self.load(folder.name)
        fake = FakeDatabase(self.target())
        binary = install(
            self.root / "fake-bin",
            "printf '%s\\n' 'ORA-12154: TNS could not resolve the connect identifier'\nexit 1\n",
        )

        with patch.dict("os.environ", fake_environment(binary)), contextlib.redirect_stderr(io.StringIO()):
            failed, _ = self.apply(migrations, fake=fake, apply_folder_fn=apply_folder)

        self.assertEqual(failed, 2)
        attempt = next((self.root / "scratch").glob("migration-attempt-*/run-manifest.json"))
        recorded = json.loads(attempt.read_text(encoding="utf-8"))["migrations"][0]
        self.assertEqual(recorded["state"], "apply-not-started")
        self.assertEqual(recorded["applyStartClassification"], "connection-failed-before-payload")
        self.assertFalse(recorded["writeAttempted"])

    def test_ora_error_after_payload_started_remains_locked(self):
        folder = self.add_folder(
            "2026-09-28_payload-error-r001",
            {"001-insert-row.sql": "INSERT INTO T (ID) VALUES (1);\n"},
            preconditions=[{"id": "before-row", "sql": "SELECT 1 FROM dual", "expected": 1}],
        )
        migrations = self.load(folder.name)
        fake = FakeDatabase(self.target())
        identity = json.dumps(fake.identity(), separators=(",", ":"))
        output = (
            "MIGRATION_IDENTITY_BEGIN\n" + identity + "\nMIGRATION_IDENTITY_END\n"
            "MIGRATION_IDENTITY_VERIFIED\nMIGRATION_PAYLOAD_STARTED:001-insert-row.sql\n"
            "ORA-00942: table or view does not exist\n"
        )
        binary = install(self.root / "fake-bin", "cat <<'FAKE_SQLCL_OUTPUT'\n" + output + "FAKE_SQLCL_OUTPUT\nexit 1\n")

        with patch.dict("os.environ", fake_environment(binary)), contextlib.redirect_stderr(io.StringIO()):
            failed, _ = self.apply(migrations, fake=fake, apply_folder_fn=apply_folder)

        self.assertEqual(failed, 2)
        attempt = next((self.root / "scratch").glob("migration-attempt-*/run-manifest.json"))
        recorded = json.loads(attempt.read_text(encoding="utf-8"))["migrations"][0]
        self.assertEqual(recorded["state"], "apply-failed-or-unknown")
        self.assertTrue(recorded["writeAttempted"])
        self.assertEqual(inspect_migration_lock(self.root, f"migrations/{folder.name}").status, "locked")

    def test_timeout_after_payload_started_without_ora_stays_locked(self):
        folder = self.add_folder(
            "2026-09-28_payload-timeout-r001",
            {"001-large.sql": "INSERT INTO T (ID) VALUES (1);\n"},
            preconditions=[{"id": "before-row", "sql": "SELECT 1 FROM dual", "expected": 1}],
        )
        migrations = self.load(folder.name)
        fake = FakeDatabase(self.target())
        identity = json.dumps(fake.identity(), separators=(",", ":"))
        output = (
            "MIGRATION_IDENTITY_BEGIN\n" + identity + "\nMIGRATION_IDENTITY_END\n"
            "MIGRATION_IDENTITY_VERIFIED\nMIGRATION_PAYLOAD_STARTED:001-large.sql\n"
        )
        binary = install(
            self.root / "fake-bin",
            "cat <<'FAKE_SQLCL_OUTPUT'\n" + output + "FAKE_SQLCL_OUTPUT\nsleep 2\n",
        )

        with patch.dict("os.environ", fake_environment(binary, MIGRATION_APPLY_TIMEOUT_SECONDS="0.05")), contextlib.redirect_stderr(io.StringIO()):
            failed, _ = self.apply(migrations, fake=fake, apply_folder_fn=apply_folder)

        self.assertEqual(failed, 2)
        attempt = next((self.root / "scratch").glob("migration-attempt-*/run-manifest.json"))
        recorded = json.loads(attempt.read_text(encoding="utf-8"))["migrations"][0]
        self.assertEqual(recorded["state"], "apply-failed-or-unknown")
        self.assertTrue(recorded["writeAttempted"])

    def test_a_retained_attempt_with_a_duplicate_key_still_blocks_replay(self):
        # json.loads keeps the last of two equal keys, so "writeAttempted": true
        # followed by "writeAttempted": false read as false and let the DDL run
        # again. Receipts reject duplicate keys; the attempt record must too.
        folder = self.add_folder("2026-09-28_create-dup-r001", {"001-create-a.sql": "CREATE TABLE A (ID NUMBER);\n"})
        migrations = self.load(folder.name)
        fake = FakeDatabase(self.target())
        fake.fail_file = "001-create-a.sql"
        failed, _ = self.apply(migrations, fake=fake)
        self.assertEqual(failed, 2)
        manifest = next((self.root / "scratch").glob("migration-attempt-*/run-manifest.json"))
        original = manifest.read_text(encoding="utf-8")
        self.assertIn('"writeAttempted": true', original)
        manifest.write_text(original.replace('"writeAttempted": true', '"writeAttempted": true, "writeAttempted": false'), encoding="utf-8")
        fake.fail_file = None
        apply_count = len([call for call in fake.calls if call[0] == "apply"])

        retry, fake = self.apply(migrations, fake=fake)

        self.assertEqual(retry, 2)
        self.assertEqual(len([call for call in fake.calls if call[0] == "apply"]), apply_count)
        self.assertFalse((folder / "status.dev.json").exists())

    def test_matching_receipt_blocks_replay(self):
        folder = self.add_folder("2026-09-28_create-once-r001", {"001-create-once.sql": "CREATE TABLE ONCE_T (ID NUMBER);\n"})
        migrations = self.load(folder.name)
        first, fake = self.apply(migrations)
        self.assertEqual(first, 0)
        apply_count = len([call for call in fake.calls if call[0] == "apply"])

        second, fake = self.apply(migrations, fake=fake)

        self.assertEqual(second, 2)
        self.assertEqual(len([call for call in fake.calls if call[0] == "apply"]), apply_count)

    def test_source_mutation_after_review_blocks_first_write(self):
        folder = self.add_folder("2026-09-28_create-frozen-r001", {"001-create-t.sql": "CREATE TABLE T (ID NUMBER);\n"})
        migration = self.load(folder.name)
        target = self.target("staging")

        def change_source(_prompt):
            (folder / "001-create-t.sql").write_text("CREATE TABLE OTHER_T (ID NUMBER);\n", encoding="utf-8")
            return True

        result, fake = self.apply(migration, target, confirm=change_source)

        self.assertEqual(result, 2)
        self.assertFalse(any(call[0] == "apply" for call in fake.calls))
        self.assertFalse((folder / "status.staging.json").exists())

    def test_source_mutation_during_apply_does_not_change_frozen_payload_or_receipt_digest(self):
        folder = self.add_folder("2026-09-28_create-frozen-r001", {"001-create-t.sql": "CREATE TABLE T (ID NUMBER);\n"})
        migration = self.load(folder.name)
        fake = FakeDatabase(self.target())
        fake.mutate_source_during_apply = "001-create-t.sql"
        fake.source_file_to_mutate = folder / "001-create-t.sql"

        result, fake = self.apply(migration, fake=fake)

        self.assertEqual(result, 2)
        self.assertEqual((folder / "001-create-t.sql").read_text(encoding="utf-8"), "CREATE TABLE CHANGED_AFTER_FREEZE (ID NUMBER);\n")
        self.assertFalse((folder / "status.dev.json").exists())
        manifests = list((self.root / "scratch").glob("migration-attempt-*/run-manifest.json"))
        run = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(run["migrations"][0]["payloadDigest"], migration[0].payload_digest)

    def test_changed_observed_target_after_commit_prevents_receipt(self):
        folder = self.add_folder("2026-09-28_create-target-r001", {"001-create-t.sql": "CREATE TABLE T (ID NUMBER);\n"})
        migration = self.load(folder.name)
        fake = FakeDatabase(self.target())
        fake.change_identity_after_apply = True

        result, _ = self.apply(migration, fake=fake)

        self.assertEqual(result, 2)
        self.assertFalse((folder / "status.dev.json").exists())

    def test_receipt_install_failure_retains_attempt_and_blocks_retry(self):
        folder = self.add_folder("2026-09-28_create-receipt-r001", {"001-create-t.sql": "CREATE TABLE T (ID NUMBER);\n"})
        migration = self.load(folder.name)
        fake = FakeDatabase(self.target())

        def fail_receipt(_path, _receipt):
            raise OSError("simulated disk failure")

        result, fake = self.apply(migration, fake=fake, receipt_installer=fail_receipt)
        self.assertEqual(result, 2)
        self.assertFalse((folder / "status.dev.json").exists())
        apply_count = len([call for call in fake.calls if call[0] == "apply"])
        attempt_dirs = list((self.root / "scratch").glob("migration-attempt-*"))
        self.assertEqual(len(attempt_dirs), 1)
        attempt_dirs[0].rename(attempt_dirs[0].with_name("migration-attempt-with_underscore"))

        retry, fake = self.apply(migration, fake=fake)
        self.assertEqual(retry, 2)
        self.assertEqual(len([call for call in fake.calls if call[0] == "apply"]), apply_count)

    def test_migrate_cli_requires_one_environment_and_rejects_missing_schema_without_fallback(self):
        folder = self.add_folder("2026-09-28_create-cli-r001", {"001-create-t.sql": "CREATE TABLE T (ID NUMBER);\n"})
        values = {
            "DB_ENVIRONMENT": "development",
            "CODE_SQLCL_CONNECTION": "dev-profile", "CODE_EXPECTED_USER": "APP_DEV", "CODE_SCHEMA": "APP_DEV",
            "STAGING_SQLCL_CONNECTION": "stage-profile", "STAGING_EXPECTED_USER": "LOGIN_STAGE",
        }
        code = main([f"migrations/{folder.name}", "--env", "staging"], environ=values, repo_root=self.root, confirm=lambda _prompt: False)
        self.assertEqual(code, 2)

    def test_sql_apply_driver_uses_migration_guard_define_off_and_commit(self):
        folder = self.add_folder("2026-09-28_create-driver-r001", {
            "001-create-t.sql": "CREATE TABLE T (ID NUMBER);\n",
            "002-create-pkg.sql": "CREATE OR REPLACE PACKAGE zz_pkg AS\n  PROCEDURE p;\nEND;\n/\nCREATE OR REPLACE PACKAGE BODY zz_pkg AS\n  PROCEDURE p IS BEGIN NULL; END;\nEND;\n/\n",
        }, preconditions=[{"id": "before-change", "sql": "SELECT 1 FROM dual", "expected": 1}])
        migration = self.load(folder.name)[0]
        staged_dir = self.root / "scratch" / "manual-driver"
        staged_dir.mkdir(parents=True)
        payload = staged_dir / "payload" / migration.folder.name
        payload.mkdir(parents=True)
        for file in migration.files:
            path = payload / file.name
            path.write_bytes(file.source)
            path.chmod(0o600)
        from dataclasses import replace
        staged_files = tuple(replace(file, path=payload / file.name) for file in migration.files)
        staged = replace(migration, folder=payload, files=staged_files)
        run_dir = staged_dir / "apply"

        # Inspect the generated driver without opening SQLcl by replacing its transport.
        from unittest.mock import patch
        from scripts.sqlcl_session import SqlclResult
        fake_identity = json.dumps({
            "session_user": "LOGIN_DEV", "current_schema": "APP_DEV", "db_name": "DEVDB",
            "db_unique_name": "DEVDB_UNIQUE", "service_name": "dev.service", "container_id": "3",
            "container_name": "APP_PDB", "edition": "ORA$BASE", "database_version": "19.0",
        })
        def fake_sqlcl(_target, driver, working, **kwargs):
            self.assertEqual(kwargs["phase"], "apply")
            content = driver.read_text(encoding="utf-8")
            self.assertIn("@@verify_migration_access.sql", (working / "migrate.sql").read_text(encoding="utf-8"))
            self.assertIn("SET DEFINE OFF", content)
            self.assertIn("@@../payload/2026-09-28_create-driver-r001/001-create-t.sql", content)
            self.assertIn("PROMPT MIGRATION_PAYLOAD_STARTED:001-create-t.sql", content)
            self.assertIn("EXIT SUCCESS COMMIT", content)
            payload_at = content.index("@@../payload/")
            # SQLcl ends a plain SQL statement at a blank line unless told
            # otherwise: an UPDATE split before its WHERE would run on every row.
            self.assertIn("SET SQLBLANKLINES ON", content)
            self.assertLess(content.index("SET SQLBLANKLINES ON"), payload_at)
            # The identity guard runs before any payload and pins every field.
            guard_at = content.index("-20987")
            self.assertLess(guard_at, payload_at)
            self.assertIn("!= 'DEVDB_UNIQUE'", content)
            self.assertIn("!= 'dev.service'", content)
            # The compile guard runs after the payload and before the commit,
            # and checks only the units this migration compiles.
            compile_at = content.index("-20986")
            self.assertLess(payload_at, compile_at)
            self.assertLess(compile_at, content.index("MIGRATION_APPLY_COMPLETED"))
            self.assertIn("FROM all_errors", content)
            self.assertIn("FROM all_objects", content)
            self.assertIn("check_unit('APP_DEV', 'PACKAGE', 'ZZ_PKG', TRUE);", content)
            self.assertIn("check_unit('APP_DEV', 'PACKAGE BODY', 'ZZ_PKG', TRUE);", content)
            self.assertNotIn("check_unit('APP_DEV', 'TABLE'", content)
            # A payload's WHENEVER SQLERROR CONTINUE must not let the guard's
            # error fall through to the commit.
            last_payload_at = content.rindex("@@../payload/")
            whenever_at = content.index("WHENEVER SQLERROR EXIT FAILURE ROLLBACK", last_payload_at)
            self.assertLess(whenever_at, compile_at)
            output = f"MIGRATION_IDENTITY_BEGIN\n{fake_identity}\nMIGRATION_IDENTITY_END\nMIGRATION_IDENTITY_VERIFIED\nMIGRATION_APPLY_COMPLETED\n"
            return SqlclResult(0, output, working)
        expected_identity = json.loads(fake_identity)
        with patch("scripts.migrate.run_sqlcl", side_effect=fake_sqlcl):
            evidence = apply_folder(staged, self.target(), run_dir, expected_identity=expected_identity)
        self.assertTrue(evidence["committed"])

        # An identity value that cannot be embedded safely refuses before SQLcl runs.
        with patch("scripts.migrate.run_sqlcl", side_effect=AssertionError("SQLcl must not run")):
            with self.assertRaisesRegex(MigrationApplyError, "cannot be enforced"):
                apply_folder(staged, self.target(), staged_dir / "apply-quote", expected_identity={**expected_identity, "service_name": "x'y"})

    def schema_folder(self, schema, name, sql):
        folder = self.root / "migrations" / schema / name
        folder.mkdir(parents=True)
        (folder / "001-create.sql").write_text(sql, encoding="utf-8", newline="\n")
        checks = {"schemaVersion": 1, "preconditions": [], "postconditions": [{"id": "after-change", "sql": "SELECT 1 FROM dual", "expected": 1}]}
        (folder / "checks.json").write_text(json.dumps(checks) + "\n", encoding="utf-8")
        return folder

    def test_an_attempt_in_one_schema_does_not_block_the_same_folder_name_in_another(self):
        # migrations/<SCHEMA>/ may reuse a family and revision per schema; the
        # retained attempt evidence was matched by folder name alone, so a write
        # in DEMO refused the never-written DEMO2 folder as "different bytes".
        name = "2026-10-01_multi-r001"
        first = self.schema_folder("DEMO", name, "CREATE TABLE T_ONE (ID NUMBER);\n")
        second = self.schema_folder("DEMO2", name, "CREATE TABLE T_TWO (ID NUMBER);\n")
        demo = Target("dev", "dev-profile", "LOGIN_DEV", "DEMO", "development")
        demo2 = Target("dev", "dev-profile", "LOGIN_DEV", "DEMO2", "development")

        applied_first, _ = self.apply(load_batch(self.root, [f"migrations/DEMO/{name}"]), target=demo)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            applied_second, _ = self.apply(load_batch(self.root, [f"migrations/DEMO2/{name}"]), target=demo2)

        self.assertEqual(applied_first, 0)
        self.assertEqual(applied_second, 0, stderr.getvalue())
        self.assertTrue((first / "status.dev.json").exists())
        self.assertTrue((second / "status.dev.json").exists())

    def test_an_attempt_in_the_same_schema_still_blocks_a_changed_folder_of_that_name(self):
        name = "2026-10-01_multi-r001"
        demo = Target("dev", "dev-profile", "LOGIN_DEV", "DEMO", "development")
        folder = self.schema_folder("DEMO", name, "CREATE TABLE T_ONE (ID NUMBER);\n")
        fake = FakeDatabase(demo)
        fake.fail_file = "001-create.sql"
        with contextlib.redirect_stderr(io.StringIO()):
            failed, _ = self.apply(load_batch(self.root, [f"migrations/DEMO/{name}"]), target=demo, fake=fake)
        self.assertEqual(failed, 2)
        (folder / "001-create.sql").write_text("CREATE TABLE T_CHANGED (ID NUMBER);\n", encoding="utf-8", newline="\n")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            again, _ = self.apply(load_batch(self.root, [f"migrations/DEMO/{name}"]), target=demo)
        self.assertEqual(again, 2)
        self.assertIn("prior write attempt", stderr.getvalue())

    def test_interrupt_during_apply_says_the_folder_may_be_partially_applied(self):
        # Ctrl-C used to end in a Python traceback. The write attempt is already
        # recorded, so the next run refuses; the message must say why.
        folder = self.add_folder("2026-09-28_interrupt-r001", {"001-create.sql": "CREATE TABLE T (ID NUMBER);\n"})
        fake = FakeDatabase(self.target())

        def interrupted(*_arguments, **_keywords):
            raise KeyboardInterrupt

        fake.apply_folder = interrupted
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result, _ = self.apply(self.load(folder.name), fake=fake)

        self.assertEqual(result, 2)
        message = stderr.getvalue()
        self.assertIn("interrupted", message)
        self.assertIn("may be partially applied", message)
        self.assertIn("Retained attempt evidence", message)
        self.assertFalse((folder / "status.dev.json").exists())
        retained = list((self.root / "scratch").glob("migration-attempt-*"))
        self.assertEqual(len(retained), 1)
        manifest = json.loads((retained[0] / "run-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["state"], "apply-failed-or-unknown")

        # The documented consequence: another attempt is refused until reconciled.
        with contextlib.redirect_stderr(io.StringIO()) as again:
            repeated, _ = self.apply(self.load(folder.name))
        self.assertEqual(repeated, 2)
        self.assertIn("write attempt", again.getvalue())

    def test_interrupt_before_any_write_leaves_nothing_behind(self):
        folder = self.add_folder("2026-09-28_interrupt-early-r001", {"001-create.sql": "CREATE TABLE T (ID NUMBER);\n"})
        fake = FakeDatabase(self.target())

        def interrupted(*_arguments, **_keywords):
            raise KeyboardInterrupt

        fake.capture_inventory = interrupted
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result, _ = self.apply(self.load(folder.name), fake=fake)

        self.assertEqual(result, 130)
        self.assertIn("interrupted", stderr.getvalue())
        self.assertIn("no writes were attempted", stderr.getvalue())
        self.assertEqual(list((self.root / "scratch").glob("migration-attempt-*")), [])

    def test_a_later_folder_precondition_may_depend_on_an_earlier_folder(self):
        # r002's precondition queries the table r001 creates; it runs at r002's
        # own boundary, after r001, not against the state before the batch.
        first = self.add_folder("2026-09-28_orders-r001", {"001-create.sql": "CREATE TABLE ORDERS (ID NUMBER);\n"})
        second = self.add_folder(
            "2026-09-28_orders-r002", {"001-add.sql": "ALTER TABLE ORDERS ADD STATUS VARCHAR2(20);\n"},
            preconditions=[{"id": "orders-exists", "sql": "SELECT COUNT(*) FROM orders WHERE ROWNUM = 1", "expected": 1}],
        )
        fake = FakeDatabase(self.target())
        fake.check_requires_object = {"orders-exists": ("ORDERS", "TABLE")}

        result, fake = self.apply(self.load(first.name, second.name), fake=fake)

        self.assertEqual(result, 0)
        self.assertTrue((first / "status.dev.json").exists())
        self.assertTrue((second / "status.dev.json").exists())
        initial = [call for call in fake.calls if call[0] == "checks" and call[2] == "preconditions"]
        self.assertEqual(initial[0][3], ())
        self.assertIn(("orders-exists",), [call[3] for call in initial[1:]])

    def test_a_first_folder_precondition_still_blocks_the_batch_before_any_write(self):
        first = self.add_folder(
            "2026-09-28_needs-r001", {"001-add.sql": "ALTER TABLE MISSING_T ADD STATUS VARCHAR2(20);\n"},
            preconditions=[{"id": "missing-exists", "sql": "SELECT COUNT(*) FROM missing_t WHERE ROWNUM = 1", "expected": 1}],
        )
        fake = FakeDatabase(self.target())
        fake.check_requires_object = {"missing-exists": ("MISSING_T", "TABLE")}
        result, fake = self.apply(self.load(first.name), fake=fake)
        self.assertNotEqual(result, 0)
        self.assertFalse(any(call[0] == "apply" for call in fake.calls))

    def test_apply_receives_the_preflight_identity(self):
        migration = self.load(self.add_folder("2026-09-28_pin-identity-r001", {"001-create-t.sql": "CREATE TABLE T (ID NUMBER);\n"}).name)
        result, fake = self.apply(migration)
        self.assertEqual(result, 0)
        self.assertEqual(len(fake.apply_expected_identities), 1)
        pinned = fake.apply_expected_identities[0]
        for field, value in fake.identity().items():
            self.assertEqual(pinned[field], value)

    def test_postcondition_session_on_another_database_writes_no_receipt(self):
        folder = self.add_folder("2026-09-28_other-db-r001", {"001-create-t.sql": "CREATE TABLE T (ID NUMBER);\n"})
        migration = self.load(folder.name)
        fake = FakeDatabase(self.target())
        fake.check_identity_override = {"postconditions": {"db_unique_name": "OTHERDB"}}
        result, fake = self.apply(migration, fake=fake)
        self.assertEqual(result, 2)
        self.assertFalse((folder / "status.dev.json").exists())

    def test_precondition_session_on_another_database_blocks_the_write(self):
        migration = self.load(self.add_folder(
            "2026-09-28_other-pre-r001", {"001-create-t.sql": "CREATE TABLE T (ID NUMBER);\n"},
            preconditions=[{"id": "before-change", "sql": "SELECT 1 FROM dual", "expected": 1}],
        ).name)
        fake = FakeDatabase(self.target())
        fake.check_identity_override = {"preconditions": {"db_name": "OTHERDB"}}
        result, fake = self.apply(migration, fake=fake)
        self.assertEqual(result, 2)
        self.assertFalse(any(call[0] == "apply" for call in fake.calls))

    def test_failed_postconditions_name_check_ids_store_them_and_verbose_shows_all(self):
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                suffix = "verbose" if verbose else "brief"
                folder = self.add_folder(
                    f"2026-10-03_{suffix}-r001",
                    {"001-create-t.sql": "CREATE TABLE T (ID NUMBER);\n"},
                    postconditions=[
                        {"id": f"check-{index:02}", "sql": "SELECT 1 FROM dual", "expected": 1}
                        for index in range(22)
                    ],
                )
                migrations = self.load(folder.name)
                fake = FakeDatabase(self.target())

                def fail_postconditions(target, checks, run_dir, *, phase):
                    if phase != "postconditions":
                        return fake.run_checks(target, checks, run_dir, phase=phase)
                    ids = [check.id for check in checks]
                    results = tuple({
                        "id": check.id, "row_count": 1, "column_count": 1,
                        "numeric": True, "value": 0, "passed": False,
                    } for check in checks)
                    errors = tuple({
                        "code": "CHECK_FAILED", "check": check.id, "expected": 1,
                        "observedValue": 0, "message": "assertion returned zero",
                    } for check in checks)
                    return CheckReport(False, True, results, errors, {
                        "phase": phase, "complete": True, "identity": fake.identity(),
                        "failedCheckIds": ids,
                    })

                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    result, _ = self.apply(
                        migrations, fake=fake, run_checks_fn=fail_postconditions, verbose=verbose,
                    )

                self.assertEqual(result, 2)
                message = stderr.getvalue()
                self.assertIn("check-00", message)
                self.assertIn("expected=1", message)
                self.assertIn("observed=0", message)
                if verbose:
                    self.assertIn("check-21", message)
                    self.assertNotIn("use --verbose", message)
                else:
                    self.assertNotIn("check-21", message)
                    self.assertIn("2 more", message)
                manifest_path = next(
                    path for path in (self.root / "scratch").glob("migration-attempt-*/run-manifest.json")
                    if json.loads(path.read_text(encoding="utf-8"))["migrations"][0]["folder"] == folder.name
                )
                record = json.loads(manifest_path.read_text(encoding="utf-8"))["migrations"][0]
                self.assertEqual(record["failedCheckIds"], [f"check-{index:02}" for index in range(22)])

    def test_postcondition_with_no_check_output_says_why_and_keeps_evidence(self):
        folder = self.add_folder("2026-10-03_no-output-r001", {"001-create-t.sql": "CREATE TABLE T (ID NUMBER);\n"})
        migrations = self.load(folder.name)
        fake = FakeDatabase(self.target())
        binary = install(self.root / "fake-bin", 'printf "FAKE_SQLCL_NO_CHECK_PAYLOAD\\n"\n')

        def only_postconditions_use_sqlcl(target, checks, run_dir, *, phase):
            if phase == "postconditions":
                return run_checks(target, checks, run_dir, phase=phase)
            return fake.run_checks(target, checks, run_dir, phase=phase)

        stderr = io.StringIO()
        with patch.dict("os.environ", fake_environment(binary)), contextlib.redirect_stderr(stderr):
            result, _ = self.apply(migrations, fake=fake, run_checks_fn=only_postconditions_use_sqlcl)

        self.assertEqual(result, 2)
        self.assertIn("verification produced no output: timeout, size limit or SQLcl crash", stderr.getvalue())
        self.assertIn("sqlcl-output.log", stderr.getvalue())
        self.assertFalse((folder / "status.dev.json").exists())

    def test_apply_timeout_says_it_was_cut_off_and_may_be_partially_applied(self):
        folder = self.add_folder("2026-10-03_apply-timeout-r001", {"001-create-t.sql": "CREATE TABLE T (ID NUMBER);\n"})
        migrations = self.load(folder.name)
        binary = install(self.root / "fake-bin", 'printf "FAKE_SQLCL_APPLY_STARTED\\n"\nsleep 5\n')
        stderr = io.StringIO()

        with patch.dict("os.environ", fake_environment(binary, MIGRATION_APPLY_TIMEOUT_SECONDS="0.05")), contextlib.redirect_stderr(stderr):
            result, _ = self.apply(migrations, apply_folder_fn=apply_folder)

        self.assertEqual(result, 2)
        self.assertIn("cut off after 0.05 s", stderr.getvalue())
        self.assertIn("may be partially applied", stderr.getvalue())
        self.assertFalse((folder / "status.dev.json").exists())

    def test_migrate_cli_accepts_and_forwards_verbose(self):
        folder = self.add_folder("2026-10-03_verbose-cli-r001", {"001-create-t.sql": "CREATE TABLE T (ID NUMBER);\n"})
        values = {
            "DB_ENVIRONMENT": "development",
            "CODE_SCHEMA": "APP", "CODE_SQLCL_CONNECTION": "dev-profile", "CODE_EXPECTED_USER": "MIGRATOR",
        }

        with patch("scripts.migrate.apply_batch", return_value=0) as apply:
            result = main([f"migrations/{folder.name}", "--env", "dev", "--verbose"],
                          environ=values, repo_root=self.root)

        self.assertEqual(result, 0)
        self.assertIs(apply.call_args.kwargs["verbose"], True)

    def test_each_batched_check_session_must_match_the_preflight_identity(self):
        identity = FakeDatabase(self.target()).identity()
        report = CheckReport(True, True, ({"id": "verified", "passed": True},), (), {
            "phase": "postconditions",
            "identity": identity,
            "sessionIdentities": [identity, {**identity, "db_unique_name": "OTHERDB_UNIQUE"}],
        })

        with self.assertRaisesRegex(MigrationApplyError, "different database/schema identity"):
            _assert_check_identity(identity, report)

    def test_migrate_cli_help_exposes_verbose_verification_reporting(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as caught:
            main(["--help"])

        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--verbose", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
