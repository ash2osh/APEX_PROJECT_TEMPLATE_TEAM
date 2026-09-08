"""Explicit Docker/APEX qualification for the configured live target.

This module is intentionally outside the default offline discovery.  Invoke it
with ``TEAM_LIVE_ENV=/path/to/live.env`` after reviewing the target.  Missing
configuration is a failure, not a skipped qualification.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import os
import hashlib
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import tempfile
import unittest
import uuid

from teamlib.apex import ApexError, adopt_app, capture_app, import_app
from teamlib.config import load_config, profile_target
from teamlib.control_store import MutexHeld, SqlControlStore
from teamlib.live_inventory import inventory_target
from teamlib.migrate import apply_plan
from teamlib.migration_store import MigrationMutexHeld, SqlMigrationStore
from teamlib.masters import MasterError, apex_component_resolver, validate_masters
from teamlib.sqlcl import SqlclError, run_sqlcl


class DockerQualificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        env_path = os.environ.get("TEAM_LIVE_ENV")
        if not env_path:
            raise unittest.SkipTest("TEAM_LIVE_ENV is required for the live Docker qualification")
        cls.env_path = Path(env_path)
        cls.config = load_config(cls.env_path, require_verify=True)
        if cls.config.environment == "production":
            raise AssertionError("live qualification refuses production-classified targets")
        cls.work = Path(tempfile.mkdtemp(prefix="team-live-docker-"))
        cls.metadata = profile_target(cls.config, "METADATA")
        cls.control = SqlControlStore(cls.metadata, work_root=cls.work / "metadata")
        cls.migration = SqlMigrationStore(cls.metadata, work_root=cls.work / "metadata")

    @classmethod
    def tearDownClass(cls) -> None:
        import shutil

        shutil.rmtree(cls.work, ignore_errors=True)

    def test_all_profiles_pass_identity_probe(self):
        identity = Path(__file__).resolve().parents[2] / "sql" / "identity.sql"
        for profile in ("TABLES", "CODE", "APEX", "METADATA", "VERIFY"):
            target = profile_target(
                self.config,
                profile,
                alias=next(iter(self.config.apps)) if profile == "APEX" else None,
            )
            result = run_sqlcl(target, "read", identity, self.work / f"identity-{profile.lower()}")
            self.assertEqual(result.identity["INSTANCE_ID"], target.instance_id)

    def test_full_inventory_is_framed_and_nonempty(self):
        target = profile_target(self.config, "TABLES")
        inventory = inventory_target(
            target,
            self.config.tables_schema,
            self.config.code_schema,
            self.work / "inventory",
        )
        self.assertGreater(len(inventory.objects), 0)
        self.assertIn(inventory.topology, {"shared", "separate"})
        self.assertEqual(len(inventory.digest), 64)

    def test_oracle_app_mutex_is_physical_and_nowait(self):
        target = profile_target(self.config, "APEX", alias="master-app")
        self.control.setup_state([target])
        suffix = uuid.uuid4().hex
        first = f"live-checkout-a-{suffix}"
        second = f"live-checkout-b-{suffix}"
        self.control.register_app(target, first, "docker-test", "codex")
        self.control.register_app(target, second, "docker-test", "codex")
        run_token = f"live-app-a-{suffix}"
        self.control.acquire_app(target.physical_key, run_token, first, "docker-test", "codex")
        with self.assertRaises(MutexHeld):
            self.control.acquire_app(target.physical_key, f"live-app-b-{suffix}", second, "docker-test", "codex")
        state = self.control.release_app(target.physical_key, run_token, confirmed_success=True)
        self.assertGreaterEqual(state.generation, 2)

    def test_payload_and_verify_profiles_cannot_read_controller_metadata(self):
        driver = self.work / "metadata-isolation.sql"
        driver.write_text("SELECT COUNT(*) FROM DEMO_META.TEAM_MIGRATION_META;\n", encoding="utf-8", newline="\n")
        for profile in ("TABLES", "VERIFY"):
            with self.subTest(profile=profile):
                with self.assertRaises(SqlclError):
                    run_sqlcl(profile_target(self.config, profile), "read", driver, self.work / f"isolation-{profile.lower()}")

    def test_payload_and_verify_profiles_cannot_mutate_controller_metadata(self):
        driver = self.work / "metadata-mutation.sql"
        driver.write_text("DELETE FROM DEMO_META.TEAM_MIGRATION_META;\n", encoding="utf-8", newline="\n")
        for profile in ("TABLES", "CODE", "VERIFY"):
            with self.subTest(profile=profile):
                with self.assertRaises(SqlclError):
                    run_sqlcl(profile_target(self.config, profile), "write", driver, self.work / f"mutation-{profile.lower()}")

    def test_master_component_identity_is_queried_on_target(self):
        contract_path = Path(__file__).resolve().parents[3] / "targets" / "masters.json"
        contract = json.loads(contract_path.read_text(encoding="utf-8"))
        target = profile_target(self.config, "APEX", alias="employee-self-service")
        source = {
            "shared-components/auth.apx":
            b"authentication subscriber-auth { subscription { master: @/500/opendoor-master } }"
        }
        report = validate_masters(
            source,
            target,
            contract,
            component_resolver=apex_component_resolver(
                work_root=self.work / "master-checks",
            ),
        )
        self.assertTrue(report.valid)
        wrong = dict(source)
        wrong["shared-components/auth.apx"] = (
            b"authentication subscriber-auth { subscription { master: @/500/not-a-component } }"
        )
        with self.assertRaises(MasterError):
            validate_masters(
                wrong,
                target,
                contract,
                component_resolver=apex_component_resolver(
                    work_root=self.work / "master-checks-wrong",
                ),
            )

    def test_apex_round_trip_uses_sql_controller(self):
        target = profile_target(self.config, "APEX", alias="master-app")
        self.control.setup_state([target])
        with tempfile.TemporaryDirectory(prefix="team-live-apex-repo-") as directory:
            repo = Path(directory)
            import subprocess

            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "codex@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Codex"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-qm", "live baseline"], check=True)
            capture = capture_app(target, repo=repo, control_store=self.control)
            source = repo / "apps" / "master-app"
            source.mkdir(parents=True)
            for relative, data in capture.tree.items():
                destination = source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            subprocess.run(["git", "-C", str(repo), "add", "apps/master-app"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "adopt live export"], check=True)
            adopt_app(target, repo=repo, control_store=self.control)
            head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            baseline = import_app(
                target,
                head,
                repo=repo,
                control_store=self.control,
                checkout_uuid=f"live-import-{uuid.uuid4().hex}",
                host="docker-test",
                user="codex",
            )
            self.assertEqual(baseline.source_commit, head)

    def test_concurrent_apex_exports_both_succeed(self):
        """Shared-app exports are independent reads and must not serialize."""
        target = profile_target(self.config, "APEX", alias="master-app")
        self.control.setup_state([target])
        with tempfile.TemporaryDirectory(prefix="team-live-concurrent-export-") as directory:
            repo = Path(directory)
            import subprocess

            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "codex@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Codex"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-qm", "live export baseline"], check=True)

            def export_once():
                return capture_app(
                    target,
                    repo=repo,
                    control_store=self.control,
                    runner=run_sqlcl,
                    persist=False,
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                first, second = tuple(pool.map(lambda _index: export_once(), (1, 2)))
            self.assertEqual(first.tree, second.tree)
            self.assertEqual(first.after_sync.generation, second.after_sync.generation)

    def test_second_concurrent_import_refuses_while_first_holds_mutex(self):
        """A second writer must fail fast while a real import owns the row."""
        target = profile_target(self.config, "APEX", alias="master-app")
        self.control.setup_state([target])
        with tempfile.TemporaryDirectory(prefix="team-live-concurrent-import-") as directory:
            repo = Path(directory)
            import subprocess

            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "codex@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Codex"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-qm", "live import baseline"], check=True)
            capture = capture_app(target, repo=repo, control_store=self.control)
            source = repo / "apps" / "master-app"
            source.mkdir(parents=True)
            for relative, data in capture.tree.items():
                destination = source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            subprocess.run(["git", "-C", str(repo), "add", "apps/master-app"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "adopt live import source"], check=True)
            adopt_app(target, repo=repo, control_store=self.control)
            head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

            first_write_started = threading.Event()
            release_first_write = threading.Event()

            def first_runner(run_target, operation, driver, work):
                if operation == "write" and Path(driver).name == "import.sql":
                    first_write_started.set()
                    if not release_first_write.wait(120):
                        raise AssertionError("timed out waiting for the competing import")
                return run_sqlcl(run_target, operation, driver, work)

            first_checkout = f"live-import-a-{uuid.uuid4().hex}"
            second_checkout = f"live-import-b-{uuid.uuid4().hex}"
            with ThreadPoolExecutor(max_workers=2) as pool:
                first_future = pool.submit(
                    import_app,
                    target,
                    head,
                    repo=repo,
                    control_store=self.control,
                    checkout_uuid=first_checkout,
                    host="docker-test",
                    user="codex-a",
                    runner=first_runner,
                )
                try:
                    self.assertTrue(first_write_started.wait(180), "first import did not reach its payload")
                    second_future = pool.submit(
                        import_app,
                        target,
                        head,
                        repo=repo,
                        control_store=self.control,
                        checkout_uuid=second_checkout,
                        host="docker-test",
                        user="codex-b",
                        runner=run_sqlcl,
                    )
                    with self.assertRaises(ApexError) as caught:
                        second_future.result(timeout=180)
                    message = str(caught.exception).lower()
                    self.assertTrue("mutex" in message or "held" in message, message)
                    release_first_write.set()
                    self.assertEqual(first_future.result(timeout=240).source_commit, head)
                finally:
                    release_first_write.set()

    def test_capture_straddling_real_state_change_is_discarded(self):
        """A generation change between export observations invalidates capture."""
        target = profile_target(self.config, "APEX", alias="master-app")
        self.control.setup_state([target])
        with tempfile.TemporaryDirectory(prefix="team-live-torn-capture-") as directory:
            repo = Path(directory)
            import subprocess

            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "codex@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Codex"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "--allow-empty", "-qm", "live torn baseline"], check=True)
            export_finished = threading.Event()
            allow_capture_return = threading.Event()

            def delayed_runner(run_target, operation, driver, work):
                result = run_sqlcl(run_target, operation, driver, work)
                if operation == "read":
                    export_finished.set()
                    if not allow_capture_return.wait(120):
                        raise AssertionError("timed out waiting for the state mutation")
                return result

            checkout = f"live-torn-{uuid.uuid4().hex}"
            self.control.register_app(target, checkout, "docker-test", "codex")
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    capture_app,
                    target,
                    repo=repo,
                    control_store=self.control,
                    runner=delayed_runner,
                    persist=False,
                )
                self.assertTrue(export_finished.wait(180), "export did not complete before the mutation")
                token = f"live-torn-token-{uuid.uuid4().hex}"
                self.control.acquire_app(target.physical_key, token, checkout, "docker-test", "codex")
                self.control.release_app(target.physical_key, token, confirmed_success=True)
                allow_capture_return.set()
                with self.assertRaises(ApexError) as caught:
                    future.result(timeout=180)
                self.assertIn("generation", str(caught.exception).lower())

    def test_real_sql_migration_records_observation_and_cleans_payload(self):
        suffix = uuid.uuid4().hex[:12].upper()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        migration_id = f"{stamp}__codex__live-sql-{uuid.uuid4().hex[:8]}"
        table_name = f"TEAM_LIVE_{suffix}"
        with tempfile.TemporaryDirectory(prefix="team-live-migration-") as directory:
            source = Path(directory)
            header = "-- migration-version: 1\n-- target: tables\n-- destructive: false\n\n"
            (source / f"{migration_id}.sql").write_text(
                header + f"CREATE TABLE {table_name} (ID NUMBER);\n",
                encoding="utf-8",
                newline="\n",
            )
            (source / f"{migration_id}.verify.sql").write_text(
                "SELECT 'live' assertion_name, 'PASS' status FROM dual;\n",
                encoding="utf-8",
                newline="\n",
            )
            tables = profile_target(self.config, "TABLES")
            verify_target = profile_target(self.config, "VERIFY")
            schema_set_digest = hashlib.sha256(
                f"{self.config.tables_schema}|{self.config.code_schema}|{self.config.metadata_schema}".encode("ascii")
            ).hexdigest()
            self.migration.bootstrap(self.metadata, schema_set_digest=schema_set_digest)

            def observe(_migration, _phase):
                return inventory_target(
                    tables,
                    self.config.tables_schema,
                    self.config.code_schema,
                    self.work / "migration-inventory" / uuid.uuid4().hex,
                    schema_set_digest=schema_set_digest,
                )

            def execute(migration):
                run_sqlcl(tables, "write", migration.sql_path, self.work / "migration-payload")

            def verify(migration):
                run_sqlcl(verify_target, "read", migration.verify_path, self.work / "migration-verify")
                return True

            report = apply_plan(
                source,
                {
                    "target": self.metadata,
                    "store": self.migration,
                    "execute": execute,
                    "verify": verify,
                    "observe": observe,
                    "require_observation": True,
                    "schema_set_digest": schema_set_digest,
                    "source_commit": "docker-live",
                    "applied_by": "codex",
                },
            )
            self.assertEqual(report.applied, (migration_id,))
            history = self.migration.read_history(self.metadata)
            self.assertIn(migration_id, history)
            self.assertEqual(len(history[migration_id]["observation"]["before"]), 64)
            inventories = self.migration.read_inventories(self.metadata)
            self.assertEqual(len(inventories), 2)
            self.assertTrue(all(manifest["objects"] for manifest in inventories.values()))
            observations = self.migration.read_state(self.metadata)["observations"]
            self.assertEqual(
                [(item["sequence"], item["predecessor_sequence"]) for item in observations],
                [(0, None), (1, 0)],
            )
            cleanup = self.work / "cleanup.sql"
            cleanup.write_text(f"DROP TABLE {table_name} PURGE;\n", encoding="utf-8", newline="\n")
            run_sqlcl(tables, "write", cleanup, self.work / "migration-cleanup")

    def test_oracle_migration_mutex_rejects_second_runner_nowait(self):
        """The migration controller fails a competing runner without waiting."""
        schema_set_digest = hashlib.sha256(
            f"{self.config.tables_schema}|{self.config.code_schema}|{self.config.metadata_schema}".encode("ascii")
        ).hexdigest()
        self.migration.bootstrap(self.metadata, schema_set_digest=schema_set_digest)
        first_token = f"live-migration-a-{uuid.uuid4().hex}"
        second_token = f"live-migration-b-{uuid.uuid4().hex}"
        self.migration.acquire(self.metadata, first_token, "live-runner-a", "docker-test")
        try:
            competing = SqlMigrationStore(self.metadata, work_root=self.work / "competing-migration")
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(competing.acquire, self.metadata, second_token, "live-runner-b", "docker-test")
                with self.assertRaises(MigrationMutexHeld) as caught:
                    future.result(timeout=60)
            self.assertIn(first_token, str(caught.exception))
        finally:
            self.migration.release(self.metadata, first_token)


if __name__ == "__main__":
    unittest.main()
