"""Recorded-catalog and fake-SQLcl coverage for compare-env."""

from __future__ import annotations

import base64
import gzip
import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import _no_real_sqlcl  # noqa: F401
from tests import fake_sqlcl
from scripts.compare_schema import (
    COMPARE_ENV_SECTIONS,
    compare_environment_catalogs,
    emit_dba_script,
    parse_environment_catalog_output,
    render_environment_report,
)
from scripts.schema_catalog import CatalogError


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "compare_env"


def catalog(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def framed(payload: dict) -> str:
    encoded = base64.b64encode(gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))).decode("ascii")
    chunks = [encoded[index:index + 120] for index in range(0, len(encoded), 120)]
    return "\n".join((
        "CATALOG_PAYLOAD_BEGIN:compare-env",
        "CATALOG_ENCODING:gzip-base64-v1",
        *chunks,
        "CATALOG_PAYLOAD_END:compare-env",
        "CATALOG_VERIFIED:compare-env",
    ))


class EnvironmentComparisonTests(unittest.TestCase):
    def test_catalog_fixture_covers_every_documented_section(self) -> None:
        self.assertEqual(
            COMPARE_ENV_SECTIONS,
            (
                "tables", "columns", "constraints", "indexes", "triggers", "sequences", "synonyms",
                "views", "stored-code", "invalid-objects", "identity-columns", "object-grants",
                "system-privileges", "roles", "network-aces", "ords", "java-mle",
                "installed-options", "versions",
            ),
        )

    def test_records_are_matched_by_name_and_report_all_difference_classes(self) -> None:
        report = compare_environment_catalogs(
            catalog("dev"), catalog("staging"),
            sections=("tables", "constraints", "triggers"),
        )
        observed = {(row["section"], row["classification"], row["name"]) for row in report["differences"]}
        self.assertIn(("tables", "missing on target", "T_FROM_ONLY"), observed)
        self.assertIn(("tables", "only on target", "T_TARGET_ONLY"), observed)
        self.assertIn(("constraints", "different", "CK_STATE"), observed)
        self.assertIn(("triggers", "different", "TRG_AUDIT"), observed)
        check_difference = next(row for row in report["differences"] if row["name"] == "CK_STATE")
        trigger_difference = next(row for row in report["differences"] if row["name"] == "TRG_AUDIT")
        self.assertIn("condition", check_difference["fields"])
        self.assertIn("source_hash", trigger_difference["fields"])
        self.assertTrue(any(row["section"] == "tables" and row["name"] == "T_SHARED" for row in report["identical"]))
        self.assertTrue(any(row["section"] == "constraints" and row["name"] == "SYS_C100 / SYS_C900" for row in report["identical"]))

    def test_system_generated_constraint_names_do_not_create_a_difference(self) -> None:
        report = compare_environment_catalogs(
            catalog("dev"), catalog("staging"), sections=("constraints",),
        )
        self.assertFalse(any(row["name"] in {"SYS_C100", "SYS_C900"} for row in report["differences"]))
        self.assertTrue(any(row["classification"] == "identical" and row["source"]["name"] == "SYS_C100" for row in report["identical"]))

    def test_trigger_source_hash_detects_logic_change_while_ignoring_first_line(self) -> None:
        source = catalog("dev")
        target = catalog("staging")
        old_headers = (source["sections"]["triggers"][0]["source_header"], target["sections"]["triggers"][0]["source_header"])
        self.assertNotEqual(*old_headers)
        report = compare_environment_catalogs(source, target, sections=("triggers",))
        difference = report["differences"][0]
        self.assertEqual(difference["name"], "TRG_AUDIT")
        self.assertNotEqual(difference["source"]["source_hash"], difference["target"]["source_hash"])

    def test_trigger_header_formatting_alone_is_not_a_logic_difference(self) -> None:
        source = catalog("dev")
        target = catalog("staging")
        target["sections"]["triggers"][0]["source_hash"] = source["sections"]["triggers"][0]["source_hash"]
        report = compare_environment_catalogs(source, target, sections=("triggers",))
        self.assertEqual(report["differences"], [])
        self.assertEqual(report["identical"][0]["name"], "TRG_AUDIT")

    def test_unselected_prefix_grants_are_not_reported(self) -> None:
        source = catalog("dev")
        target = catalog("staging")
        source["sections"]["object-grants"].append({
            "owner": "APP_DEV", "object_name": "ERP_LOAD", "grantee": "ETL_ROLE",
            "privilege": "SELECT", "grantable": "NO",
        })
        report = compare_environment_catalogs(
            source, target, sections=("object-grants",), prefixes=("HR_",),
        )
        self.assertEqual(report["differences"], [])

    def test_different_reference_and_application_rows_do_not_change_structure_readiness(self) -> None:
        source = catalog("dev")
        target = json.loads(json.dumps(source))
        target["environment"] = "staging"
        target["schema"] = "APP_STAGE"
        target["reference_rows"] = [{"id": 900, "label": "Changed"}]
        source["reference_rows"] = [{"id": 1, "label": "Original"}]
        target["erp_camp_rows"] = [{"id": 100, "value": "different"}]
        source["erp_camp_rows"] = [{"id": 2, "value": "source"}]
        report = compare_environment_catalogs(source, target, sections=("tables",))
        self.assertEqual(report["exit_code"], 0)
        self.assertEqual(len(report["identical"]), len(source["sections"]["tables"]))
        self.assertFalse(any(row["section"] in {"reference-data", "application-data"} for row in report["differences"]))

    def test_star_prefix_includes_all_object_grants(self) -> None:
        report = compare_environment_catalogs(
            catalog("dev"), catalog("staging"), sections=("object-grants",), prefixes=("*",),
        )
        self.assertEqual(len(report["differences"]), 1)
        self.assertEqual(report["differences"][0]["classification"], "missing on target")

    def test_missing_dba_connection_is_an_explicit_blocker(self) -> None:
        report = compare_environment_catalogs(
            catalog("dev"), catalog("staging"), sections=("system-privileges",),
            dba_connections={"dev": False, "staging": False},
        )
        self.assertEqual(report["exit_code"], 2)
        self.assertEqual(report["blockers"][0]["message"], "not compared (no DBA connection)")

    def test_invalid_objects_block_readiness_even_when_both_sides_match(self) -> None:
        source = catalog("dev")
        target = catalog("staging")
        for payload in (source, target):
            payload["sections"]["invalid-objects"] = [{"name": "BROKEN_VIEW", "type": "VIEW", "status": "INVALID"}]
            payload["coverage"]["sections"]["invalid-objects"] = {"complete": True, "pages": [1]}
        report = compare_environment_catalogs(source, target, sections=("invalid-objects",))
        self.assertEqual(report["exit_code"], 2)
        self.assertTrue(any(row["name"] == "BROKEN_VIEW" for row in report["blockers"]))

    def test_paged_capture_with_more_than_one_page_is_complete(self) -> None:
        payload = catalog("dev")
        rows = [{"name": f"TABLE_{index:04d}", "temporary": "N", "partitioned": "N"} for index in range(501)]
        payload["sections"]["tables"] = rows
        payload["coverage"]["sections"]["tables"] = {"complete": True, "pages": [500, 1]}
        parsed = parse_environment_catalog_output(framed(payload), sections=("tables",))
        self.assertEqual(len(parsed["sections"]["tables"]), 501)
        report = compare_environment_catalogs(parsed, parsed, sections=("tables",))
        self.assertEqual(len(report["identical"]), 501)
        self.assertEqual(report["exit_code"], 0)

    def test_baseline_data_capture_preserves_oracle_number_precision(self) -> None:
        payload = catalog("dev")
        exact_number = "12345678901234567890.12345678901234567890"
        payload["sections"]["baseline-data"] = [{
            "name": "APP_NUMBERS", "complete": True, "rowCount": 1, "pages": [1],
            "identity": None,
            "columns": [{"name": "AMOUNT", "data_type": "NUMBER"}],
            "rows": [{"AMOUNT": "DECIMAL_MARKER"}],
        }]
        payload["coverage"]["sections"]["baseline-data"] = {"complete": True, "pages": [1]}
        encoded_json = json.dumps(payload, separators=(",", ":")).replace(
            '"AMOUNT":"DECIMAL_MARKER"', f'"AMOUNT":{exact_number}',
        )
        encoded = base64.b64encode(gzip.compress(encoded_json.encode("utf-8"))).decode("ascii")
        output = "\n".join((
            "CATALOG_PAYLOAD_BEGIN:compare-env",
            "CATALOG_ENCODING:gzip-base64-v1",
            *[encoded[index:index + 120] for index in range(0, len(encoded), 120)],
            "CATALOG_PAYLOAD_END:compare-env",
            "CATALOG_VERIFIED:compare-env",
        ))

        parsed = parse_environment_catalog_output(
            output, sections=("baseline-data",), baseline_capture=True,
        )

        self.assertEqual(
            parsed["sections"]["baseline-data"][0]["rows"][0]["AMOUNT"],
            Decimal(exact_number),
        )

    def test_full_last_page_without_terminal_short_page_is_rejected(self) -> None:
        payload = catalog("dev")
        payload["sections"]["tables"] = [
            {"name": f"TABLE_{index:04d}", "temporary": "N", "partitioned": "N"}
            for index in range(500)
        ]
        payload["coverage"]["sections"]["tables"] = {"complete": True, "pages": [500]}
        with self.assertRaisesRegex(CatalogError, "final page"):
            parse_environment_catalog_output(framed(payload), sections=("tables",))

    def test_capture_that_hits_a_row_cap_is_rejected(self) -> None:
        payload = catalog("dev")
        payload["coverage"]["sections"]["tables"] = {
            "complete": False, "capHit": True, "pages": [500, 500],
        }
        with self.assertRaisesRegex(CatalogError, "cap"):
            parse_environment_catalog_output(framed(payload), sections=("tables",))

    def test_truncated_constraint_condition_blocks_readiness(self) -> None:
        source = catalog("dev")
        target = catalog("staging")
        source["sections"]["constraints"][0]["condition_truncated"] = "Y"
        report = compare_environment_catalogs(source, target, sections=("constraints",))
        self.assertEqual(report["exit_code"], 2)
        self.assertTrue(any("may be truncated" in item["message"] for item in report["blockers"]))

    def test_dba_capture_must_match_the_environment_database_scope(self) -> None:
        from scripts.compare_schema import _merge_dba_capture

        base = catalog("dev")
        privileged = catalog("dev")
        privileged["identity"]["db_unique_name"] = "WRONG_DATABASE"
        with self.assertRaisesRegex(CatalogError, "same database"):
            _merge_dba_capture(base, privileged)

    def test_catalog_sql_hashes_fetched_lines_after_line_one(self) -> None:
        sql = (ROOT / "scripts" / "compare_env_catalog.sql").read_text(encoding="utf-8")
        self.assertIn("IF source_line > 1 THEN", sql)
        self.assertIn("SELECT ORA_HASH(source_text) INTO line_hash FROM dual", sql)
        self.assertNotIn("ORA_HASH(s.text)", sql)

    def test_report_renderers_group_readiness_items_and_list_exclusions(self) -> None:
        report = compare_environment_catalogs(catalog("dev"), catalog("staging"), sections=("tables",))
        text = render_environment_report(report, "text")
        markdown = render_environment_report(report, "markdown")
        encoded = json.loads(render_environment_report(report, "json"))
        self.assertIn("Blockers", text)
        self.assertIn("Differences", markdown)
        self.assertIn("not compared", markdown)
        self.assertTrue(any("ERP and camp data" in item for item in encoded["notCompared"]))
        self.assertTrue(any(item.startswith("Sections not selected:") for item in encoded["notCompared"]))

    def test_dba_script_contains_only_additive_reviewable_statements(self) -> None:
        source = catalog("dev")
        target = catalog("staging")
        source["sections"]["system-privileges"] = [
            {"grantee": "APP_DEV", "privilege": "CREATE SESSION", "admin_option": "NO"},
        ]
        source["sections"]["roles"] = [
            {"grantee": "APP_DEV", "role": "REPORT_ROLE", "admin_option": "YES", "default_role": "YES"},
        ]
        target["sections"]["system-privileges"] = []
        target["sections"]["roles"] = []
        for payload, sys_pages, role_pages in ((source, [1], [1]), (target, [0], [0])):
            payload["coverage"]["sections"]["system-privileges"] = {"complete": True, "pages": sys_pages}
            payload["coverage"]["sections"]["roles"] = {"complete": True, "pages": role_pages}
        report = compare_environment_catalogs(
            source, target,
            sections=("object-grants", "system-privileges", "roles", "network-aces", "ords"),
        )
        script = emit_dba_script(report)
        self.assertIn('GRANT SELECT ON "APP_STAGE"."T_SHARED" TO "REPORT_ROLE"', script)
        self.assertIn('GRANT CREATE SESSION TO "APP_STAGE"', script)
        self.assertIn('GRANT "REPORT_ROLE" TO "APP_STAGE" WITH ADMIN OPTION', script)
        self.assertIn("DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE", script)
        self.assertIn("ORDS.ENABLE_SCHEMA", script)
        self.assertNotIn("REVOKE", script)
        self.assertNotIn("DROP ", script)


class CompareEnvironmentSqlclTests(unittest.TestCase):
    def test_command_uses_fake_sqlcl_and_recorded_catalogs(self) -> None:
        from scripts.compare_schema import main

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_program = root / "fake_sql.py"
            fake_program.write_text(
                "import os, pathlib, sys\n"
                "import base64, gzip, json\n"
                "args = sys.argv[1:]\n"
                "driver = pathlib.Path(next(arg[1:] for arg in args if arg.startswith('@')))\n"
                "environment = next(line.split(':', 1)[1].strip() for line in driver.read_text().splitlines() if line.startswith('-- COMPARE_ENVIRONMENT:'))\n"
                "payload = json.loads(pathlib.Path(os.environ['COMPARE_ENV_FIXTURE_DIR'], environment + '.json').read_text())\n"
                "encoded = base64.b64encode(gzip.compress(json.dumps(payload, separators=(',', ':')).encode())).decode()\n"
                "print('CATALOG_PAYLOAD_BEGIN:compare-env')\nprint('CATALOG_ENCODING:gzip-base64-v1')\n"
                "[print(encoded[index:index + 120]) for index in range(0, len(encoded), 120)]\n"
                "print('CATALOG_PAYLOAD_END:compare-env')\nprint('CATALOG_VERIFIED:compare-env')\n",
                encoding="utf-8",
            )
            fake_sqlcl.install(fake_bin, f'#!/usr/bin/env bash\nexec "{Path(os.sys.executable).as_posix()}" "{fake_program.as_posix()}" "$@"\n')
            values = {
                "DB_ENVIRONMENT": "development",
                "CODE_SCHEMA": "APP_DEV", "CODE_SQLCL_CONNECTION": "DEV", "CODE_EXPECTED_USER": "APP_DEV",
                "STAGING_SCHEMA": "APP_STAGE", "STAGING_SQLCL_CONNECTION": "STAGE", "STAGING_EXPECTED_USER": "APP_STAGE",
                "TABLES_PREFIXES": "HR_", "CODE_PREFIXES": "HR_",
            }
            scratch = root / "scratch"
            with patch.dict(os.environ, {
                "PATH": fake_sqlcl.environment(fake_bin)["PATH"],
                "COMPARE_ENV_FIXTURE_DIR": str(FIXTURES),
            }, clear=False):
                result = main(
                    ["compare-env", "--from", "dev", "--to", "staging", "--section", "constraints", "--format", "json"],
                    environ=values,
                    run_dir=scratch,
                )
            self.assertEqual(result, 1)
            evidence = list(scratch.glob("compare-env-*/report.json"))
            self.assertEqual(evidence, [])

    def test_dba_aliases_capture_read_only_sections_and_emit_script(self) -> None:
        from scripts.compare_schema import main

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_program = root / "fake_sql.py"
            fake_program.write_text(
                "import os, pathlib, sys\n"
                "import base64, gzip, json\n"
                "args = sys.argv[1:]\n"
                "driver = pathlib.Path(next(arg[1:] for arg in args if arg.startswith('@')))\n"
                "environment = next(line.split(':', 1)[1].strip() for line in driver.read_text().splitlines() if line.startswith('-- COMPARE_ENVIRONMENT:'))\n"
                "payload = json.loads(pathlib.Path(os.environ['COMPARE_ENV_FIXTURE_DIR'], environment + '.json').read_text())\n"
                "encoded = base64.b64encode(gzip.compress(json.dumps(payload, separators=(',', ':')).encode())).decode()\n"
                "print('CATALOG_PAYLOAD_BEGIN:compare-env')\nprint('CATALOG_ENCODING:gzip-base64-v1')\n"
                "[print(encoded[index:index + 120]) for index in range(0, len(encoded), 120)]\n"
                "print('CATALOG_PAYLOAD_END:compare-env')\nprint('CATALOG_VERIFIED:compare-env')\n",
                encoding="utf-8",
            )
            fake_sqlcl.install(fake_bin, f'#!/usr/bin/env bash\nexec "{Path(os.sys.executable).as_posix()}" "{fake_program.as_posix()}" "$@"\n')
            values = {
                "DB_ENVIRONMENT": "development",
                "CODE_SCHEMA": "APP_DEV", "CODE_SQLCL_CONNECTION": "DEV", "CODE_EXPECTED_USER": "APP_DEV",
                "STAGING_SCHEMA": "APP_STAGE", "STAGING_SQLCL_CONNECTION": "STAGE", "STAGING_EXPECTED_USER": "APP_STAGE",
                "TABLES_PREFIXES": "T_", "CODE_PREFIXES": "T_",
                "DEV_DBA_SQLCL_CONNECTION": "DEV_DBA", "STAGING_DBA_SQLCL_CONNECTION": "STAGE_DBA",
            }
            output_file = root / "reviewed-dba.sql"
            with patch.dict(os.environ, {
                "PATH": fake_sqlcl.environment(fake_bin)["PATH"],
                "COMPARE_ENV_FIXTURE_DIR": str(FIXTURES),
            }, clear=False):
                result = main(
                    ["compare-env", "--from", "dev", "--to", "staging",
                     "--section", "object-grants", "--section", "network-aces", "--section", "ords",
                     "--format", "json", "--emit-dba-script", str(output_file)],
                    environ=values,
                    run_dir=root / "scratch",
                )
            self.assertEqual(result, 1)
            script = output_file.read_text(encoding="utf-8")
            self.assertIn('GRANT SELECT ON "APP_STAGE"."T_SHARED" TO "REPORT_ROLE"', script)
            self.assertIn("DBMS_NETWORK_ACL_ADMIN.APPEND_HOST_ACE", script)
            self.assertIn("ORDS.ENABLE_SCHEMA", script)


if __name__ == "__main__":
    unittest.main()
