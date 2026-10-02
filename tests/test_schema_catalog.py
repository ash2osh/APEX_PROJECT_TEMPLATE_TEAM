import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.db_targets import Target
from scripts.schema_catalog import (
    CatalogError,
    ObjectKey,
    capture_inventory,
    capture_snapshot,
    parse_inventory,
    parse_snapshot,
)
from scripts.sqlcl_session import SqlclError, SqlclResult, run_sqlcl
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "schema_catalog"


def fixture_payload(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def framed(payload: dict, phase: str | None = None) -> str:
    name = phase or payload["phase"]
    return (
        f"CATALOG_PAYLOAD_BEGIN:{name}\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + f"\nCATALOG_PAYLOAD_END:{name}\nCATALOG_VERIFIED:{name}\n"
    )


def target() -> Target:
    return Target("dev", "dev-profile", "APP_DEV", "APP_DEV", "development")


class SchemaCatalogTests(unittest.TestCase):
    def test_owner_inventory_fixture_has_full_identity_and_objects(self) -> None:
        inventory = parse_inventory(framed(fixture_payload("owner-inventory.json")), target())

        self.assertEqual(inventory.identity["db_unique_name"], "DEVDB1")
        self.assertEqual(inventory.identity["container_id"], "3")
        self.assertEqual(inventory.identity["edition"], "ORA$BASE")
        self.assertIn(ObjectKey("APP_DEV", "CUSTOMERS", "TABLE"), inventory.objects)
        self.assertTrue(inventory.coverage["ownerComplete"])

    def test_inventory_marks_identity_sequences_for_table_mapping(self) -> None:
        payload = fixture_payload("owner-inventory.json")
        payload["objects"].append({"owner": "APP_DEV", "name": "ISEQ$$_42", "type": "SEQUENCE", "status": "VALID", "last_ddl_time": "2026-09-27T12:00:00Z", "identity_sequence": True})

        inventory = parse_inventory(framed(payload), target())

        self.assertTrue(inventory.objects[ObjectKey("APP_DEV", "ISEQ$$_42", "SEQUENCE")]["identity_sequence"])

    def test_snapshot_preserves_raw_ddl_unicode_and_embedded_newlines(self) -> None:
        payload = fixture_payload("owner-snapshot.json")
        payload["definitions"][0]["raw_ddl"] += "\n-- café Ω"
        snapshot = parse_snapshot(
            framed(payload), target(), (("CUSTOMERS", "TABLE"),)
        )

        definition = snapshot.objects[ObjectKey("APP_DEV", "CUSTOMERS", "TABLE")]
        self.assertIn("punctuation ; , ( )", definition.raw_ddl)
        self.assertIn("\n-- café Ω", definition.raw_ddl)
        self.assertEqual(len(definition.dependents), 3)

    def test_service_aliases_do_not_change_database_scope_identity(self) -> None:
        first = fixture_payload("owner-inventory.json")["identity"]
        second = fixture_payload("owner-snapshot.json")["identity"]
        self.assertNotEqual(first["service_name"], second["service_name"])
        self.assertTrue(SchemaCatalogIdentity.same_database_scope(first, second))

    def test_partial_catalog_visibility_never_becomes_empty_success(self) -> None:
        payload = fixture_payload("owner-inventory.json")
        payload["objects"] = []
        payload["coverage"]["ownerComplete"] = False
        payload["coverage"]["path"] = "PARTIAL_ALL_OBJECTS"

        with self.assertRaises(CatalogError):
            parse_inventory(framed(payload), target())

    def test_wrong_session_user_or_current_schema_is_rejected(self) -> None:
        for attribute, value in (("session_user", "OTHER_USER"), ("current_schema", "OTHER_SCHEMA")):
            with self.subTest(attribute=attribute):
                payload = fixture_payload("owner-inventory.json")
                payload["identity"][attribute] = value
                with self.assertRaises(CatalogError):
                    parse_inventory(framed(payload), target())

    def test_wrong_database_identity_is_incomplete(self) -> None:
        payload = fixture_payload("owner-inventory.json")
        payload["identity"]["db_unique_name"] = ""

        with self.assertRaises(CatalogError):
            parse_inventory(framed(payload), target())

    def test_capture_window_must_be_ordered_and_timezone_aware(self) -> None:
        payload = fixture_payload("owner-inventory.json")
        payload["started_at"] = "2026-09-28T10:00:03"
        payload["completed_at"] = "2026-09-28T10:00:02Z"

        with self.assertRaises(CatalogError):
            parse_inventory(framed(payload), target())

    def test_authorized_enabled_metadata_reader_path_is_accepted(self) -> None:
        payload = fixture_payload("owner-inventory.json")
        payload["identity"]["session_user"] = "CATALOG_READER"
        payload["coverage"].update({"path": "METADATA_PRIVILEGE", "privileges": ["SELECT_CATALOG_ROLE"], "metadataReadable": True})

        metadata_target = Target("dev", "reader-profile", "CATALOG_READER", "APP_DEV", "development")
        inventory = parse_inventory(framed(payload), metadata_target)

        self.assertTrue(inventory.coverage["ownerComplete"])

    def test_missing_end_marker_and_truncated_clob_are_errors(self) -> None:
        payload = fixture_payload("owner-snapshot.json")
        output = framed(payload).replace("CATALOG_PAYLOAD_END:snapshot\n", "")
        with self.assertRaises(CatalogError):
            parse_snapshot(output, target(), (("CUSTOMERS", "TABLE"),))

        truncated = framed(payload).split("CATALOG_PAYLOAD_END:snapshot", 1)[0]
        with self.assertRaises(CatalogError):
            parse_snapshot(truncated, target(), (("CUSTOMERS", "TABLE"),))

    def test_concurrent_ddl_between_inventory_boundaries_is_rejected(self) -> None:
        payload = fixture_payload("owner-snapshot.json")
        payload["before"][0]["object_id"] = 41
        payload["after"][0]["object_id"] = 42

        with self.assertRaises(CatalogError):
            parse_snapshot(framed(payload), target(), (("CUSTOMERS", "TABLE"),))

    def test_snapshot_rejects_capability_gap_and_database_switch(self) -> None:
        payload = fixture_payload("owner-snapshot.json")
        payload["coverage"]["unsupported"] = ["sequence option SCALE_FLAG is not captured"]
        with self.assertRaises(CatalogError):
            parse_snapshot(framed(payload), target(), (("CUSTOMERS", "TABLE"),))

        payload = fixture_payload("owner-snapshot.json")
        payload["identity"]["db_unique_name"] = "OTHERDB"
        with self.assertRaises(CatalogError):
            parse_snapshot(framed(payload), target(), (("CUSTOMERS", "TABLE"),), inventory_from_fixture())

    def test_capture_inventory_keeps_partial_rows_with_visibility_error(self) -> None:
        payload = fixture_payload("owner-inventory.json")
        payload["complete"] = False
        payload["coverage"]["ownerComplete"] = False
        payload["coverage"]["path"] = "PARTIAL_ALL_OBJECTS"
        with self.assertRaises(CatalogError) as caught:
            parse_inventory(framed(payload), target())
        self.assertIsNotNone(caught.exception.partial)
        self.assertIn(ObjectKey("APP_DEV", "CUSTOMERS", "TABLE"), caught.exception.partial.objects)

    def test_selected_metadata_error_preserves_partial_inventory(self) -> None:
        payload = fixture_payload("owner-snapshot.json")
        payload["metadataErrors"] = [{"owner": "APP_DEV", "name": "CUSTOMERS", "type": "TABLE", "error": "ORA-31603"}]
        output = framed(payload)

        with self.assertRaises(CatalogError) as caught:
            parse_snapshot(output, target(), (("CUSTOMERS", "TABLE"),))

        self.assertIsNotNone(caught.exception.partial)
        self.assertIn(ObjectKey("APP_DEV", "CUSTOMERS", "TABLE"), caught.exception.partial.inventory)

    def test_unselected_object_does_not_need_definition_extraction(self) -> None:
        payload = fixture_payload("owner-snapshot.json")
        payload["before"].append({"owner": "APP_DEV", "name": "FUTURE_TYPE", "type": "JAVA CLASS", "status": "INVALID", "last_ddl_time": "2026-09-01T00:00:00Z"})
        payload["after"] = list(payload["before"])

        snapshot = parse_snapshot(framed(payload), target(), (("CUSTOMERS", "TABLE"),))

        self.assertEqual(len(snapshot.objects), 4)
        self.assertNotIn(ObjectKey("APP_DEV", "FUTURE_TYPE", "JAVA CLASS"), snapshot.objects)

    def test_identity_generated_sequence_is_represented_by_selected_table(self) -> None:
        payload = fixture_payload("owner-snapshot.json")
        sequence = {"owner": "APP_DEV", "name": "ISEQ$$_42", "type": "SEQUENCE", "status": "VALID", "last_ddl_time": "2026-09-27T12:00:00Z"}
        payload["before"].append(sequence)
        payload["after"].append(sequence)
        columns = payload["definitions"][0]["attributes"]["columns"]
        columns.append({"name": "ID", "data_type": "NUMBER", "nullable": "N", "identity": "YES", "column_id": 2})
        payload["definitions"][0]["attributes"]["identity_columns"] = [{"column_name": "ID", "sequence_name": "ISEQ$$_42", "generation_type": "ALWAYS"}]

        snapshot = parse_snapshot(framed(payload), target(), (("CUSTOMERS", "TABLE"),))

        table = snapshot.objects[ObjectKey("APP_DEV", "CUSTOMERS", "TABLE")]
        self.assertEqual(table.attributes["identity_columns"][0]["sequence_name"], "ISEQ$$_42")
        self.assertNotIn(ObjectKey("APP_DEV", "ISEQ$$_42", "SEQUENCE"), snapshot.objects)

    def test_snapshot_requires_each_requested_definition_and_relationship(self) -> None:
        payload = fixture_payload("owner-snapshot.json")
        payload["definitions"] = []

        with self.assertRaises(CatalogError):
            parse_snapshot(framed(payload), target(), (("CUSTOMERS", "TABLE"),))

    def test_package_body_is_a_separate_catalog_object(self) -> None:
        payload = fixture_payload("owner-snapshot.json")
        payload["before"] += [
            {"owner": "APP_DEV", "name": "CUSTOMER_API", "type": "PACKAGE", "status": "VALID", "last_ddl_time": "2026-09-27T12:00:00Z"},
            {"owner": "APP_DEV", "name": "CUSTOMER_API", "type": "PACKAGE BODY", "status": "VALID", "last_ddl_time": "2026-09-27T12:00:00Z"},
        ]
        payload["after"] = list(payload["before"])
        for object_type in ("PACKAGE", "PACKAGE BODY"):
            payload["definitions"].append({"owner": "APP_DEV", "name": "CUSTOMER_API", "type": object_type, "valid": True, "attributes": {}, "raw_ddl": f"CREATE {object_type} APP_DEV.CUSTOMER_API AS BEGIN NULL; END;", "dependents": []})

        snapshot = parse_snapshot(framed(payload), target(), (("CUSTOMER_API", "PACKAGE"),))

        self.assertIn(ObjectKey("APP_DEV", "CUSTOMER_API", "PACKAGE"), snapshot.objects)
        self.assertIn(ObjectKey("APP_DEV", "CUSTOMER_API", "PACKAGE BODY"), snapshot.objects)

    def test_capture_uses_static_read_only_driver_and_selected_keys(self) -> None:
        class Capture:
            def __init__(self) -> None:
                self.drivers: list[str] = []

            def __call__(self, selected_target: Target, driver_path: Path, run_dir: Path):
                self.drivers.append(driver_path.read_text(encoding="utf-8"))
                return SqlclResult(0, framed(fixture_payload("owner-inventory.json")), run_dir)

        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            capture_inventory(target(), run_dir, _runner=Capture())
            self.assertIn("ALL_OBJECTS", (ROOT / "scripts/schema_catalog.sql").read_text(encoding="utf-8"))

    def test_foreign_key_constraints_use_ref_constraint_metadata_type(self) -> None:
        source = " ".join((ROOT / "scripts/schema_catalog.sql").read_text(encoding="utf-8").split())

        self.assertTrue(
            "CASE WHEN MAX(constraint_type) = 'R' THEN 'REF_CONSTRAINT' "
            "ELSE 'CONSTRAINT' END" in source,
            "foreign-key metadata type must be REF_CONSTRAINT; other constraints stay CONSTRAINT",
        )

    def test_selected_key_input_is_encoded_without_sql_interpolation(self) -> None:
        class Capture:
            def __init__(self) -> None:
                self.driver = ""

            def __call__(self, selected_target: Target, driver_path: Path, run_dir: Path):
                self.driver = driver_path.read_text(encoding="utf-8")
                return SqlclResult(0, framed(fixture_payload("owner-snapshot.json")), run_dir)

        with tempfile.TemporaryDirectory() as temporary:
            runner = Capture()
            with self.assertRaises(CatalogError):
                capture_snapshot(target(), inventory_from_fixture(), (("X' OR '1'='1", "TABLE"),), Path(temporary), _runner=runner)
            self.assertNotIn("X' OR '1'='1", runner.driver)
            self.assertIn(b"X' OR '1'='1".hex().upper(), runner.driver)


class SchemaCatalogIdentity:
    @staticmethod
    def same_database_scope(left: dict, right: dict) -> bool:
        from scripts.schema_catalog import same_database_scope
        return same_database_scope(left, right)


def inventory_from_fixture():
    return parse_inventory(framed(fixture_payload("owner-inventory.json")), target())


class SqlclTransportTests(unittest.TestCase):
    def fake_sql(self, directory: Path, behavior: str = "success") -> Path:
        executable = directory / "sql"
        executable.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys, time\n"
            "sqlpath = pathlib.Path(os.getenv('SQLPATH', ''))\n"
            "pathlib.Path(os.environ['FAKE_RECORD']).write_text(json.dumps({'args':sys.argv[1:],'cwd':os.getcwd(),'sqlpath':os.getenv('SQLPATH'),'oracle_path':os.getenv('ORACLE_PATH'),'login_loaded':(sqlpath / 'login.sql').exists(),'stdin':sys.stdin.read()}))\n"
            + ("time.sleep(3)\n" if behavior == "sleep" else "")
            + ("print('ORA-20000: fake failure')\n" if behavior == "ora" else "print('CATALOG_VERIFIED:inventory')\n")
            + ("print('Error starting at line : 4 File @ x.sql')\nprint('In command -')\nprint('WHERE id = 1')\nprint('Error report -')\nprint('Unknown Command')\n" if behavior == "client" else "")
            + ("sys.exit(3)\n" if behavior == "nonzero" else "")
            ,
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable

    def test_safe_transport_isolates_working_directory_paths_and_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.fake_sql(fake_bin)
            caller_sqlpath = root / "caller-sqlpath"
            caller_sqlpath.mkdir()
            (caller_sqlpath / "login.sql").write_text("PROMPT CALLER_LOGIN_RAN\n", encoding="utf-8")
            driver = root / "run" / "driver.sql"
            driver.parent.mkdir()
            driver.write_text("EXIT SUCCESS ROLLBACK\n", encoding="utf-8")
            record = root / "record.json"
            environment = os.environ.copy()
            environment.update({"PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}", "FAKE_RECORD": str(record), "SQLPATH": str(caller_sqlpath), "ORACLE_PATH": str(caller_sqlpath)})

            result = run_sqlcl(target(), driver, driver.parent, environment=environment)
            observation = json.loads(record.read_text(encoding="utf-8"))

            self.assertEqual(Path(observation["cwd"]), driver.parent)
            self.assertEqual(observation["sqlpath"], str(driver.parent / ".sqlcl-path"))
            self.assertEqual(observation["oracle_path"], str(driver.parent / ".sqlcl-path"))
            self.assertFalse(observation["login_loaded"])
            self.assertEqual(observation["stdin"], "")
            self.assertEqual(result.returncode, 0)

    def test_transport_rejects_nonzero_or_oracle_error_with_saved_diagnostics(self) -> None:
        for behavior, expected_error in (("ora", "database or client error"), ("client", "database or client error"), ("nonzero", "exited with status 3")):
            with self.subTest(behavior=behavior), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fake_bin = root / "bin"
                fake_bin.mkdir()
                self.fake_sql(fake_bin, behavior)
                driver = root / "run" / "driver.sql"
                driver.parent.mkdir()
                driver.write_text("EXIT SUCCESS ROLLBACK\n", encoding="utf-8")
                environment = os.environ.copy()
                record = root / "record.json"
                environment.update({"PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}", "FAKE_RECORD": str(record)})

                with self.assertRaises(SqlclError) as caught:
                    run_sqlcl(target(), driver, driver.parent, environment=environment)

                self.assertTrue(record.exists())
                self.assertTrue((driver.parent / "sqlcl-output.log").exists())
                self.assertIn(expected_error, str(caught.exception))
                self.assertIn("sqlcl-output.log", str(caught.exception))

    def test_timeout_retains_output_and_returns_stable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.fake_sql(fake_bin, "sleep")
            driver = root / "run" / "driver.sql"
            driver.parent.mkdir()
            driver.write_text("EXIT SUCCESS ROLLBACK\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.update({"PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}", "FAKE_RECORD": str(root / "record.json")})

            with self.assertRaisesRegex(SqlclError, "timed out"):
                run_sqlcl(target(), driver, driver.parent, environment=environment, timeout_seconds=0.5)

            self.assertTrue((driver.parent / "sqlcl-output.log").exists())

    def test_driver_must_be_confined_to_private_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "run"
            run.mkdir()
            outside = root / "driver.sql"
            outside.write_text("SELECT 1 FROM dual;\n", encoding="utf-8")
            with self.assertRaises(SqlclError):
                run_sqlcl(target(), outside, run)

    def test_argv_transport_does_not_interpret_metacharacters_in_driver_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="catalog path ") as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.fake_sql(fake_bin)
            run_dir = root / "run;touch-INJECTED"
            run_dir.mkdir()
            driver = run_dir / "driver 'quoted'.sql"
            driver.write_text("EXIT SUCCESS ROLLBACK\n", encoding="utf-8")
            record = root / "record.json"
            environment = os.environ.copy()
            environment.update({"PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}", "FAKE_RECORD": str(record)})

            result = run_sqlcl(target(), driver, run_dir, environment=environment)

            observation = json.loads(record.read_text(encoding="utf-8"))
            self.assertIn("@" + str(driver), observation["args"])
            self.assertFalse((root / "INJECTED").exists())
            self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
