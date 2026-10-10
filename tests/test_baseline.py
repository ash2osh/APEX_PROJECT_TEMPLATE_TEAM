"""Command contract regressions for baseline tooling."""

from __future__ import annotations

import os
import io
import json
import re
import subprocess
import tempfile
import unittest
import contextlib
from decimal import Decimal
from pathlib import Path
from contextlib import redirect_stderr
from unittest.mock import patch

from fake_sqlcl import BASH
from tests import fake_sqlcl
from scripts import baseline
from scripts.migration_manifest import load_migration
from scripts.schema_catalog import ObjectDefinition, ObjectKey, SchemaSnapshot

ROOT = Path(__file__).resolve().parents[1]


class TeamBaselineCommandTests(unittest.TestCase):
    def run_team(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ, PROJECT_ENV_FILE="/no/such/baseline-env")
        return subprocess.run(
            [BASH, str(ROOT / "scripts" / "team.sh"), "baseline", *arguments],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_team_help_lists_baseline_exports_and_build(self) -> None:
        result = subprocess.run(
            [BASH, str(ROOT / "scripts" / "team.sh"), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("baseline export-source", result.stdout)
        self.assertIn("baseline export-grants", result.stdout)
        self.assertIn("baseline export-data", result.stdout)
        self.assertIn("baseline build", result.stdout)
        self.assertIn("baseline filter-ords", result.stdout)
        self.assertIn("--data", result.stdout)

    def test_baseline_without_subcommand_reports_usage_before_loading_env(self) -> None:
        result = self.run_team()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("usage: scripts/team.sh baseline", result.stderr)
        self.assertNotIn("configuration file", result.stderr)

    def test_baseline_help_does_not_require_database_configuration(self) -> None:
        result = self.run_team("--help")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("export-source", result.stdout)
        self.assertIn("export-grants", result.stdout)
        self.assertIn("build", result.stdout)

    def test_filter_ords_runs_locally_without_loading_env(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "filtered.sql"
            environment = dict(os.environ, PROJECT_ENV_FILE="/no/such/baseline-env")
            result = subprocess.run(
                [BASH, str(ROOT / "scripts" / "team.sh"), "baseline", "filter-ords",
                 "--exclude-module", "legacy.api", "--input", str(ROOT / "tests" / "fixtures" / "baseline" / "ords_modules.sql"),
                 "--output", str(output)],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("legacy.api", output.read_text(encoding="utf-8"))

    def test_documented_baseline_configuration_passes_the_loader(self) -> None:
        config = baseline.load_config(ROOT / "docs" / "baseline.example.json")

        self.assertEqual(config["schemas"], ["APP"])
        self.assertEqual(config["sequenceMappings"][0]["sequence"], "APP_ITEMS_SEQ")


class BaselineExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "baseline.json").write_text(
            '{"schemaVersion":1,"schemas":["APP_DEV"],"prefixes":["PKG_","V_","APP_"],'
            '"excludedObjects":[],"referenceData":{"tables":[{"name":"APP_REF_ITEMS",'
            '"excludeColumns":["API_TOKEN"],"keyColumns":["ITEM_CODE"],"labelColumns":["ITEM_LABEL"],'
            '"identity":null,"rowLimit":10}]},'
            '"grants":{"skipGrantees":["SYS","SYSTEM","PUBLIC"],"includeGrantees":[],"keepGrantOptions":true}}\n',
            encoding="utf-8",
        )
        self.fixture_dir = ROOT / "tests" / "fixtures" / "baseline"
        self.fake_bin = self.root / "fake-bin"
        self.fake_bin.mkdir()
        self.record = self.root / "fake-record.json"
        fake_program = self.root / "fake-sqlcl.py"
        fake_program.write_text(
            "import base64, gzip, json, os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "driver = pathlib.Path(next(arg[1:] for arg in args if arg.startswith('@')))\n"
            "text = driver.read_text(encoding='utf-8')\n"
            "environment = next(line.split(':', 1)[1].strip() for line in text.splitlines() if line.startswith('-- COMPARE_ENVIRONMENT:'))\n"
            "dba = next(line.split(':', 1)[1].strip() for line in text.splitlines() if line.startswith('-- COMPARE_ENV_DBA:')) == 'true'\n"
            "call = next(line for line in text.splitlines() if line.startswith('@@compare_env_catalog.sql ')).split()\n"
            "sections = json.loads(bytes.fromhex(call[4] if dba else call[5]).decode('utf-8'))\n"
            "kind = 'data' if 'baseline-data' in sections else ('source' if 'baseline-source' in sections else ('system' if dba else 'grants'))\n"
            "payload = json.loads(pathlib.Path(os.environ['BASELINE_FIXTURE_DIR'], f'{kind}_{environment}.json').read_text(encoding='utf-8'))\n"
            "filter_values = call[-3:-1] if len(call) > 9 else call[-2:]\n"
            "filters = [json.loads(bytes.fromhex(value).decode('utf-8')) for value in filter_values]\n"
            "data_tables = json.loads(bytes.fromhex(call[9]).decode('utf-8')) if len(call) > 9 else []\n"
            "pathlib.Path(os.environ['BASELINE_FAKE_RECORD']).write_text(json.dumps({'driver':text,'kind':kind,'filters':filters,'dataTables':data_tables}), encoding='utf-8')\n"
            "encoded = base64.b64encode(gzip.compress(json.dumps(payload, separators=(',', ':')).encode('utf-8'))).decode('ascii')\n"
            "print('CATALOG_PAYLOAD_BEGIN:compare-env')\nprint('CATALOG_ENCODING:gzip-base64-v1')\n"
            "[print(encoded[index:index + 120]) for index in range(0, len(encoded), 120)]\n"
            "print('CATALOG_PAYLOAD_END:compare-env')\nprint('CATALOG_VERIFIED:compare-env')\n",
            encoding="utf-8",
        )
        fake_sqlcl.install(
            self.fake_bin,
            f'#!/usr/bin/env bash\nexec "{Path(os.sys.executable).as_posix()}" "{fake_program.as_posix()}" "$@"\n',
        )
        self.environ = {
            "DB_ENVIRONMENT": "development",
            "CODE_SCHEMA": "APP_DEV",
            "CODE_SQLCL_CONNECTION": "DEV",
            "CODE_EXPECTED_USER": "APP_DEV",
            "DEV_DBA_SQLCL_CONNECTION": "DEV_DBA",
        }

    def call_baseline(self, *arguments: str, include_dba: bool = True) -> tuple[int, str, str]:
        from scripts import baseline

        values = dict(self.environ)
        if not include_dba:
            values.pop("DEV_DBA_SQLCL_CONNECTION", None)
        environment = fake_sqlcl.environment(
            self.fake_bin,
            BASELINE_FIXTURE_DIR=str(self.fixture_dir),
            BASELINE_FAKE_RECORD=str(self.record),
        )
        error_output = io.StringIO()
        with patch.dict(os.environ, environment, clear=False):
            with patch.object(baseline, "ROOT", self.root), patch.dict(os.environ, values, clear=False):
                with redirect_stderr(error_output):
                    result = baseline.main(arguments)
        record = self.record.read_text(encoding="utf-8") if self.record.is_file() else ""
        return result, record, error_output.getvalue()

    def test_export_source_preserves_source_lines_settings_and_views_in_json(self) -> None:
        result, record, _error = self.call_baseline("export-source", "--from", "dev")

        self.assertEqual(result, 0)
        recorded = json.loads(record)
        self.assertEqual(recorded["kind"], "source")
        self.assertEqual(recorded["filters"], [["PKG_", "V_", "APP_"], []])
        output = self.root / "scratch" / "baseline" / "dev" / "APP_DEV" / "stored-source"
        unit = json.loads((output / "PKG_ALPHA__PACKAGE.json").read_text(encoding="utf-8"))
        self.assertEqual(unit["lines"], ["PACKAGE PKG_ALPHA AS\n", "  value   \n", "\n"])
        settings = json.loads((output / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings[0]["plsql_warnings"], "ENABLE:ALL")
        view = json.loads((output / "views" / "V_ALPHA.json").read_text(encoding="utf-8"))
        self.assertEqual(view["text"], "SELECT 'view text' AS VALUE FROM DUAL\n")
        self.assertEqual(view["textSource"], "ALL_VIEWS.TEXT")
        self.assertIn('CREATE OR REPLACE FORCE EDITIONABLE VIEW "APP_DEV"."V_ALPHA"', view["ddl"])

    def test_export_grants_keeps_grantable_and_system_privilege_fields(self) -> None:
        result, record, _error = self.call_baseline("export-grants", "--from", "dev")

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(record)["kind"], "system")
        output = self.root / "scratch" / "baseline" / "dev" / "APP_DEV" / "grants"
        object_grants = json.loads((output / "object-grants.json").read_text(encoding="utf-8"))
        system_privileges = json.loads((output / "system-privileges.json").read_text(encoding="utf-8"))
        self.assertEqual(object_grants[0]["grantable"], "YES")
        self.assertEqual(system_privileges[0]["admin_option"], "NO")

    def test_export_grants_refuses_missing_dba_profile_instead_of_silent_empty_export(self) -> None:
        result, _record, error = self.call_baseline("export-grants", "--from", "dev", include_dba=False)

        self.assertEqual(result, 2)
        self.assertIn("DEV_DBA_SQLCL_CONNECTION", error)

    def test_export_data_writes_only_allowlisted_columns_to_schema_scoped_json(self) -> None:
        result, record, error = self.call_baseline("export-data", "--from", "dev")

        self.assertEqual(result, 0, error)
        recorded = json.loads(record)
        self.assertEqual(recorded["kind"], "data")
        self.assertEqual(recorded["dataTables"][0]["name"], "APP_REF_ITEMS")
        self.assertEqual(recorded["dataTables"][0]["excludeColumns"], ["API_TOKEN"])
        self.assertIn("SET TRANSACTION READ ONLY;", recorded["driver"])
        output = self.root / "scratch" / "baseline" / "dev" / "data" / "APP_DEV" / "APP_REF_ITEMS.json"
        exported = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(exported["rowCount"], 2)
        self.assertEqual(exported["rows"][0]["ITEM_CODE"], "A-1")
        self.assertNotIn("API_TOKEN", output.read_text(encoding="utf-8"))

    def test_export_data_refuses_a_per_table_row_limit_before_writing(self) -> None:
        config = baseline.load_config(self.root / "baseline.json")
        fixture = json.loads((self.fixture_dir / "data_dev.json").read_text(encoding="utf-8"))
        table = fixture["sections"]["baseline-data"][0]
        table["rowCount"] = 11

        with patch.object(baseline, "_capture", return_value=fixture):
            with self.assertRaisesRegex(baseline.BaselineError, "exceeded its rowLimit 10"):
                baseline._export_data(config, self.environ, "dev", self.root / "scratch", ["APP_DEV"])

        self.assertFalse((self.root / "scratch" / "dev" / "data" / "APP_DEV").exists())

    def test_source_export_refuses_incomplete_paged_capture_before_writing(self) -> None:
        config = baseline.load_config(self.root / "baseline.json")
        fixture = json.loads((self.fixture_dir / "source_dev.json").read_text(encoding="utf-8"))
        fixture["coverage"]["sections"]["baseline-source"]["complete"] = False
        output = self.root / "scratch" / "baseline"

        with patch.object(baseline, "_capture", return_value=fixture):
            with self.assertRaisesRegex(baseline.BaselineError, "unavailable or incomplete"):
                baseline._export_source(config, self.environ, "dev", output)

        self.assertFalse((output / "dev" / "APP_DEV").exists())


class BaselineBuilderUnitTests(unittest.TestCase):
    settings = {
        "plsql_optimize_level": "2",
        "plsql_code_type": "INTERPRETED",
        "plsql_debug": "FALSE",
        "plsql_warnings": "ENABLE:ALL",
        "plsql_ccflags": None,
        "nls_length_semantics": "BYTE",
        "plscope_settings": "IDENTIFIERS:NONE",
    }

    def test_whitespace_and_trailing_hyphen_source_use_capped_clob_chunks(self) -> None:
        unit = {
            "name": "PKG_ALPHA",
            "type": "PACKAGE",
            "lines": ["PACKAGE PKG_ALPHA AS\n", "   \n", "x-\n", "x" * 67 + "\n"],
        }
        sql = baseline._exact_source_step(unit, self.settings)

        self.assertIn("l_source CLOB", sql)
        self.assertIn("CHR(10)", sql)
        self.assertIn("x-", sql)
        self.assertLessEqual(max(map(len, re.findall(r"l_source := l_source \|\| q'~(.*?)~'", sql))), 32)
        baseline.validate_sql_only(sql)

    def test_source_checks_use_plain_text_equality_and_final_newline_tolerance(self) -> None:
        unit = {"name": "PKG_ALPHA", "type": "PACKAGE", "lines": ["PACKAGE PKG_ALPHA AS\n", "END;\n"]}
        checks = baseline._source_check_queries(unit, "APP_STAGE")
        sql = checks[0]["sql"].upper()

        self.assertIn("S.TEXT =", sql)
        self.assertIn("S.TEXT IN (", sql)
        self.assertNotIn("S.TEXT IS NULL", sql)
        self.assertNotIn("NVL(S.TEXT", sql)
        self.assertIn("ALL_SOURCE", sql)
        self.assertIn("S.OWNER = 'APP_STAGE'", sql)

    def test_q_quote_delimiter_collision_selects_a_safe_delimiter(self) -> None:
        literal = baseline._q_literal("value~'quoted")

        self.assertTrue(literal.startswith("q'!"))
        self.assertTrue(literal.endswith("!'"))
        self.assertIn("~'quoted", literal)

    def test_json_export_preserves_exact_decimal_number_text(self) -> None:
        exact = Decimal("12345678901234567890.12345678901234567890")
        encoded = baseline._json_bytes({"VALUE": exact})

        self.assertIn(b'"VALUE": 12345678901234567890.12345678901234567890', encoded)
        self.assertEqual(json.loads(encoded, parse_float=Decimal)["VALUE"], exact)
        self.assertEqual(
            baseline.format_sql_value(exact, {"name": "AMOUNT", "data_type": "NUMBER"}),
            "12345678901234567890.12345678901234567890",
        )

    def test_reference_text_literals_are_chunked_and_preserve_newlines(self) -> None:
        expression = baseline._text_sql_literal("x" * 70 + "\nsecond ~' line")
        chunks = re.findall(r"q'~(.*?)~'", expression)

        self.assertEqual([len(chunk) for chunk in chunks[:3]], [32, 32, 6])
        self.assertIn("CHR(10)", expression)
        self.assertIn("q'!", expression)
        self.assertIn("CHR(13) || CHR(10)", baseline._text_sql_literal("first\r\nsecond"))
        self.assertEqual(baseline._text_sql_literal(""), "NULL")

    def test_reference_dates_are_typed_and_invalid_date_text_stays_text(self) -> None:
        text_sql = baseline.format_sql_value("2026-02-30", {"name": "DATE_TEXT", "data_type": "VARCHAR2"})
        date_sql = baseline.format_sql_value("2026-02-28T00:00:00", {"name": "EFFECTIVE_ON", "data_type": "DATE"})

        self.assertIn("2026-02-30", text_sql)
        self.assertNotIn("TO_DATE", text_sql)
        self.assertIn("TO_DATE", date_sql)
        with self.assertRaisesRegex(baseline.BaselineError, "invalid ISO date"):
            baseline.format_sql_value("2026-02-30", {"name": "EFFECTIVE_ON", "data_type": "DATE"})

    def test_not_null_guard_handles_ora_01442_without_source_text_null_checks(self) -> None:
        sql = baseline._wrap_not_null("APP_STAGE", "ITEMS", "ITEM_ID")

        self.assertIn("SQLCODE = -1442", sql)
        self.assertIn("SEARCH_CONDITION_VC", sql)
        self.assertIn('"ITEM_ID" IS NOT NULL', sql)

    def test_compile_all_without_prefixes_covers_every_invalid_configured_unit(self) -> None:
        sql = baseline._compile_all_step("APP_STAGE", [])

        self.assertIn("OWNER = 'APP_STAGE' AND STATUS = 'INVALID' AND (1 = 1)", sql)
        self.assertIn("configured stored units remain invalid after compile passes", sql)

    def test_identity_and_sequence_steps_move_generators_without_system_value_override(self) -> None:
        identity = baseline._sync_identity_sql("APP_STAGE", {
            "table_name": "ITEMS", "column_name": "ITEM_ID", "generation_type": "BY DEFAULT",
        })
        sequence = baseline._sync_sequence_sql("APP_STAGE", {"name": "ITEM_SEQ", "increment_by": 1}, {
            "sequence": "ITEM_SEQ", "table": "ITEMS", "column": "ITEM_ID",
        })

        self.assertIn("START WITH LIMIT VALUE", identity)
        self.assertIn("NEXTVAL", sequence)
        self.assertIn("MAX(\"ITEM_ID\")", sequence)
        self.assertNotIn("OVERRIDING SYSTEM VALUE", identity + sequence)

    def test_generated_object_name_helpers_keep_schema_and_column_ddl_quoted(self) -> None:
        ddl = baseline._replace_create_header(
            'CREATE TABLE "APP_DEV"."T_ITEMS" ("ID" NUMBER)', "TABLE", "APP_DEV", "APP_STAGE",
        )

        self.assertEqual(ddl, 'CREATE TABLE "APP_STAGE"."T_ITEMS" ("ID" NUMBER)')
        self.assertEqual(
            baseline._column_sql({"name": "ITEM_NAME", "data_type": "VARCHAR2", "data_length": 80, "char_length": 80, "char_used": "C"}),
            '"ITEM_NAME" VARCHAR2(80 CHAR)',
        )
        self.assertEqual(
            baseline._replace_create_header(
                'CREATE OR REPLACE FORCE EDITIONABLE VIEW "APP_DEV"."V_ITEMS" AS SELECT 1 X FROM DUAL',
                "VIEW", "APP_DEV", "APP_STAGE",
            ),
            'CREATE OR REPLACE FORCE EDITIONABLE VIEW "APP_STAGE"."V_ITEMS" AS SELECT 1 X FROM DUAL',
        )

    def test_index_with_same_column_list_is_skipped(self) -> None:
        config = {"schemas": ["APP_DEV"], "prefixes": ["APP_"], "excludedObjects": [], "sequenceMappings": []}
        source = {"sections": {
            "tables": [{"name": "APP_ITEMS"}], "columns": [], "constraints": [], "sequences": [],
            "indexes": [{"name": "APP_ITEMS_IX_NEW", "table_name": "APP_ITEMS", "columns": [{"name": "ITEM_ID", "position": 1, "descend": "ASC"}]}],
            "identity-columns": [],
        }}
        target = {"sections": {
            "tables": [{"name": "APP_ITEMS"}], "columns": [], "constraints": [], "sequences": [],
            "indexes": [{"name": "APP_ITEMS_IX_OLD", "table_name": "APP_ITEMS", "columns": [{"name": "ITEM_ID", "position": 1, "descend": "ASC"}]}],
            "identity-columns": [],
        }}
        snapshot = SchemaSnapshot({}, {}, {ObjectKey("APP_DEV", "APP_ITEMS_IX_NEW", "INDEX"): ObjectDefinition(
            ObjectKey("APP_DEV", "APP_ITEMS_IX_NEW", "INDEX"), {}, 'CREATE INDEX "APP_DEV"."APP_ITEMS_IX_NEW" ON "APP_DEV"."APP_ITEMS" ("ITEM_ID")', (), True,
        )}, {}, "", "")

        steps, _checks = baseline._structure_bundle(config, "APP_DEV", "APP_STAGE", source, target, snapshot)

        self.assertEqual(steps, {})

    def test_named_check_constraint_with_changed_condition_is_replaced(self) -> None:
        config = {"schemas": ["APP_DEV"], "prefixes": ["APP_"], "excludedObjects": [], "sequenceMappings": []}
        source = {"sections": {
            "tables": [{"name": "APP_ITEMS"}], "columns": [], "indexes": [], "sequences": [], "identity-columns": [],
            "constraints": [{"name": "APP_ITEMS_CK", "table_name": "APP_ITEMS", "constraint_type": "C", "columns": ["STATUS"], "condition": "STATUS IN ('A', 'B')", "condition_truncated": "N", "delete_rule": None}],
        }}
        target = {"sections": {
            "tables": [{"name": "APP_ITEMS"}], "columns": [], "indexes": [], "sequences": [], "identity-columns": [],
            "constraints": [{"name": "APP_ITEMS_CK", "table_name": "APP_ITEMS", "constraint_type": "C", "columns": ["STATUS"], "condition": "STATUS = 'A'", "condition_truncated": "N", "delete_rule": None}],
        }}
        key = ObjectKey("APP_DEV", "APP_ITEMS_CK", "CONSTRAINT")
        snapshot = SchemaSnapshot({}, {}, {key: ObjectDefinition(key, {}, "ALTER TABLE APP_DEV.APP_ITEMS ADD CONSTRAINT APP_ITEMS_CK", (), True)}, {}, "", "")

        steps, checks = baseline._structure_bundle(config, "APP_DEV", "APP_STAGE", source, target, snapshot)
        sql = steps["001-structure-delta.sql"]

        self.assertIn("DROP CONSTRAINT", sql)
        self.assertIn("STATUS IN ('A', 'B')", sql)
        self.assertIn("FUNCTION normalized", sql)
        self.assertTrue(checks["postconditions"])

    def test_existing_non_identity_column_requires_a_reviewed_conversion(self) -> None:
        config = {"schemas": ["APP_DEV"], "prefixes": ["APP_"], "excludedObjects": [], "sequenceMappings": []}
        source = {"sections": {
            "tables": [{"name": "APP_ITEMS"}], "columns": [], "constraints": [], "indexes": [], "sequences": [],
            "identity-columns": [{"table_name": "APP_ITEMS", "column_name": "ITEM_ID", "generation_type": "BY DEFAULT"}],
        }}
        target = {"sections": {
            "tables": [{"name": "APP_ITEMS"}], "columns": [{"table_name": "APP_ITEMS", "name": "ITEM_ID"}],
            "constraints": [], "indexes": [], "sequences": [], "identity-columns": [],
        }}

        with self.assertRaisesRegex(baseline.BaselineError, "reviewed conversion migration"):
            baseline._structure_bundle(config, "APP_DEV", "APP_STAGE", source, target, SchemaSnapshot({}, {}, {}, {}, "", ""))

    def test_object_grants_are_batched_before_the_step_limit(self) -> None:
        config = {
            "schemas": ["APP_DEV"], "prefixes": ["APP_"], "excludedObjects": [],
            "grants": {"skipGrantees": [], "includeGrantees": [], "keepGrantOptions": True},
        }
        rows = [
            {"owner": "APP_DEV", "object_name": f"APP_T{index:03d}", "grantee": "REPORT_ROLE", "privilege": "SELECT", "grantable": "NO"}
            for index in range(101)
        ]
        source = {"sections": {"object-grants": rows}}
        target = {"sections": {"object-grants": []}}

        steps, checks = baseline._grant_bundle(config, "APP_DEV", "APP_STAGE", source, target)

        self.assertEqual(len(steps), 2)
        self.assertEqual(len(steps["001-object-grants.sql"].splitlines()), 100)
        self.assertEqual(len(steps["002-object-grants.sql"].splitlines()), 1)
        self.assertEqual(len(checks["postconditions"]), 101)


class BaselineReferenceDataTests(unittest.TestCase):
    fixtures = ROOT / "tests" / "fixtures" / "baseline"

    def setUp(self) -> None:
        self.config = {
            "referenceData": {"tables": [
                {
                    "name": "APP_CANCEL_REASON", "excludeColumns": ["API_TOKEN"],
                    "keyColumns": ["REASON_CODE"], "labelColumns": ["REASON_LABEL"],
                    "identity": {"column": "REASON_ID", "generationType": "ALWAYS"}, "rowLimit": 100,
                },
                {
                    "name": "APP_CANCEL_POLICY", "excludeColumns": ["API_TOKEN", "REFRESH_TOKEN"],
                    "keyColumns": ["POLICY_CODE"], "labelColumns": ["POLICY_NAME"],
                    "identity": {"column": "POLICY_ID", "generationType": "BY DEFAULT"}, "rowLimit": 100,
                },
                {
                    "name": "APP_ALWAYS_LOOKUP", "excludeColumns": [],
                    "keyColumns": ["LOOKUP_CODE"], "labelColumns": ["LOOKUP_LABEL"],
                    "identity": {"column": "LOOKUP_ID", "generationType": "ALWAYS"}, "rowLimit": 100,
                },
            ]},
        }
        self.specifications = {item["name"]: item for item in self.config["referenceData"]["tables"]}
        self.source_raw = json.loads((self.fixtures / "reference_data_source.json").read_text(encoding="utf-8"))
        self.target_raw = json.loads((self.fixtures / "reference_data_target.json").read_text(encoding="utf-8"))
        self.source_data = {
            name: baseline._validate_reference_capture(row, self.specifications[name], "dev", "APP_DEV")
            for name, row in self.source_raw.items()
        }
        self.target_catalog = {"sections": {"baseline-data": list(self.target_raw.values())}}
        self.constraints = json.loads((self.fixtures / "reference_data_constraints.json").read_text(encoding="utf-8"))
        self.source_catalog = {"sections": {"constraints": self.constraints}}
        self.target_data = baseline._reference_data_tables(
            self.target_catalog, self.config["referenceData"]["tables"], "staging", "APP_STAGE",
        )

    def make_bundle(self, target_rows=None):
        if target_rows is not None:
            target_catalog = {"sections": {"baseline-data": target_rows}}
            self.target_catalog = target_catalog
            self.target_data = baseline._reference_data_tables(
                target_catalog, self.config["referenceData"]["tables"], "staging", "APP_STAGE",
            )
        return baseline._reference_data_bundle(
            self.config, "APP_DEV", "APP_STAGE", self.source_catalog, self.target_catalog,
            self.source_data, self.target_data,
        )

    def test_dev26_cancel_reason_foreign_key_is_resolved_by_target_label(self) -> None:
        steps, checks, differences = self.make_bundle()
        sql = "\n".join(steps.values())

        self.assertIn('SELECT P."REASON_ID" FROM "APP_STAGE"."APP_CANCEL_REASON" P', sql)
        self.assertIn('P."REASON_LABEL" = q\'~User cancelled~\'', sql)
        self.assertNotIn('"REASON_ID" = 5', sql)
        self.assertNotIn("API_TOKEN", sql)
        self.assertNotIn("REFRESH_TOKEN", sql)
        self.assertIn('ALTER TABLE "APP_STAGE"."APP_CANCEL_POLICY" MODIFY ("POLICY_ID" GENERATED BY DEFAULT AS IDENTITY (START WITH LIMIT VALUE))', sql)
        self.assertIn('INSERT INTO "APP_STAGE"."APP_ALWAYS_LOOKUP" ("LOOKUP_CODE", "LOOKUP_LABEL")', sql)
        self.assertNotIn('INSERT INTO "APP_STAGE"."APP_ALWAYS_LOOKUP" ("LOOKUP_ID"', sql)
        self.assertIn("TO_DATE('2026-02-28T00:00:00'", sql)
        self.assertIn("2026-02-30", sql)
        self.assertNotIn("TO_DATE('2026-02-30'", sql)
        self.assertIn("CHR(10)", sql)
        self.assertIn("q'!", sql)
        self.assertNotIn("OVERRIDING SYSTEM VALUE", sql)
        self.assertEqual(differences, [])
        self.assertGreaterEqual(len(checks["preconditions"]), 4)
        self.assertTrue(any(check["id"].startswith("reference-parent-label-") for check in checks["preconditions"]))

    def test_null_optional_foreign_key_is_preserved_without_an_id_lookup(self) -> None:
        source_data = json.loads(json.dumps(self.source_data))
        source_data["APP_CANCEL_POLICY"]["rows"][0]["REASON_ID"] = None

        steps, checks, _differences = baseline._reference_data_bundle(
            self.config, "APP_DEV", "APP_STAGE", self.source_catalog, self.target_catalog,
            source_data, self.target_data,
        )
        sql = "\n".join(steps.values())

        self.assertIn('"REASON_ID"', sql)
        self.assertNotIn('SELECT P."REASON_ID"', sql)
        self.assertFalse(any(check["id"].startswith("reference-parent-label-") for check in checks["preconditions"]))

    def test_reference_data_dml_steps_respect_statement_and_byte_limits(self) -> None:
        source_data = json.loads(json.dumps(self.source_data))
        source_data["APP_ALWAYS_LOOKUP"]["rows"] = [
            {"LOOKUP_ID": index, "LOOKUP_CODE": f"LOOKUP-{index}", "LOOKUP_LABEL": f"Lookup {index}"}
            for index in range(1, 102)
        ]
        source_data["APP_ALWAYS_LOOKUP"]["rowCount"] = 101
        source_data["APP_ALWAYS_LOOKUP"]["pages"] = [101]

        steps, _checks, _differences = baseline._reference_data_bundle(
            self.config, "APP_DEV", "APP_STAGE", self.source_catalog, self.target_catalog,
            source_data, self.target_data,
        )
        dml_steps = [value.encode("utf-8") for name, value in steps.items() if "reference-data.sql" in name]

        self.assertGreaterEqual(len(dml_steps), 2)
        self.assertTrue(all(step.count(b"INSERT INTO") <= baseline.DATA_STEP_STATEMENT_LIMIT for step in dml_steps))
        self.assertTrue(all(len(step) <= baseline.DATA_STEP_BYTE_LIMIT for step in dml_steps))

    def test_missing_or_ambiguous_target_parent_label_refuses_build(self) -> None:
        reason_rows = self.target_raw["APP_CANCEL_REASON"]
        missing = json.loads(json.dumps(self.target_raw["APP_CANCEL_REASON"]))
        missing["rows"] = []
        missing["rowCount"] = 0
        missing["pages"] = [0]
        with self.assertRaisesRegex(baseline.BaselineError, "missing on target table APP_CANCEL_REASON"):
            self.make_bundle([missing, self.target_raw["APP_CANCEL_POLICY"], self.target_raw["APP_ALWAYS_LOOKUP"]])

        ambiguous = json.loads(json.dumps(reason_rows))
        ambiguous["rows"].append({"REASON_ID": 2, "REASON_CODE": "OTHER", "REASON_LABEL": "User cancelled"})
        ambiguous["rowCount"] = 2
        ambiguous["pages"] = [2]
        with self.assertRaisesRegex(baseline.BaselineError, "ambiguous or duplicated on target table APP_CANCEL_REASON"):
            self.make_bundle([ambiguous, self.target_raw["APP_CANCEL_POLICY"], self.target_raw["APP_ALWAYS_LOOKUP"]])

    def test_existing_same_key_row_with_different_values_is_reported_without_update(self) -> None:
        target_policy = json.loads(json.dumps(self.target_raw["APP_CANCEL_POLICY"]))
        target_policy["rows"] = [{
            "POLICY_ID": 901, "POLICY_CODE": "CXL-01", "POLICY_NAME": "User cancellation policy",
            "REASON_ID": 1, "POLICY_NOTE": "target note", "DATE_TEXT": "2026-02-30",
            "EFFECTIVE_ON": "2026-02-28T00:00:00",
        }]
        target_policy["rowCount"] = 1
        target_policy["pages"] = [1]

        steps, checks, differences = self.make_bundle([self.target_raw["APP_CANCEL_REASON"], target_policy, self.target_raw["APP_ALWAYS_LOOKUP"]])
        sql = "\n".join(steps.values())

        self.assertTrue(any("POLICY_NOTE" in difference for difference in differences))
        self.assertTrue(any(check["id"].startswith("reference-existing-row-") for check in checks["preconditions"]))
        self.assertNotIn("UPDATE ", sql.upper())

    def test_label_match_with_a_different_natural_key_creates_blocking_precondition(self) -> None:
        target_policy = json.loads(json.dumps(self.target_raw["APP_CANCEL_POLICY"]))
        target_policy["rows"] = [{
            "POLICY_ID": 901, "POLICY_CODE": "OTHER-CODE", "POLICY_NAME": "User cancellation policy",
            "REASON_ID": 1, "POLICY_NOTE": "target note", "DATE_TEXT": "2026-02-30",
            "EFFECTIVE_ON": "2026-02-28T00:00:00",
        }]
        target_policy["rowCount"] = 1
        target_policy["pages"] = [1]

        _steps, checks, differences = self.make_bundle([self.target_raw["APP_CANCEL_REASON"], target_policy, self.target_raw["APP_ALWAYS_LOOKUP"]])

        self.assertTrue(any("natural key differs" in difference for difference in differences))
        label_conflict = next(check for check in checks["preconditions"] if check["id"].startswith("reference-label-conflict-"))
        self.assertEqual(label_conflict["expected"], 1)
        self.assertIn("= 0 THEN 1 ELSE 0", label_conflict["sql"])

    def test_repeated_data_migration_build_is_byte_identical_and_receipt_locked(self) -> None:
        steps, checks, differences = self.make_bundle()
        readme = "# Reference data\n\n" + "\n".join(differences)
        files = baseline._migration_bytes(steps, checks, readme)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "migrations").mkdir()
            first_output = io.StringIO()
            with contextlib.redirect_stdout(first_output):
                folder = baseline._build_migration_folder(root, "APP_STAGE", "baseline-data", files)
            self.assertIsNotNone(folder)
            first_bytes = {path.name: path.read_bytes() for path in folder.iterdir()}
            second_output = io.StringIO()
            with contextlib.redirect_stdout(second_output):
                unchanged = baseline._build_migration_folder(root, "APP_STAGE", "baseline-data", files)
            self.assertIsNone(unchanged)
            self.assertIn("Unchanged migration; left byte-identical", second_output.getvalue())
            self.assertEqual(first_bytes, {path.name: path.read_bytes() for path in folder.iterdir()})
            (folder / "status.dev.json").write_text('{"schemaVersion":1,"state":"verified","environment":"dev"}\n', encoding="utf-8")
            locked_output = io.StringIO()
            with contextlib.redirect_stdout(locked_output):
                locked = baseline._build_migration_folder(root, "APP_STAGE", "baseline-data", files)
            self.assertIsNone(locked)
            self.assertIn("Skipped receipted or attempted migration without changing it", locked_output.getvalue())


class BaselineOrdsFilterTests(unittest.TestCase):
    fixtures = ROOT / "tests" / "fixtures" / "baseline"

    def test_filter_removes_complete_module_and_rejects_missing_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.fixtures / "ords_modules.sql"
            output = root / "filtered.sql"
            stdout, stderr = io.StringIO(), io.StringIO()
            with patch.object(baseline, "ROOT", root), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                status = baseline.main([
                    "filter-ords", "--exclude-module", "legacy.api",
                    "--input", str(source), "--output", str(output),
                ])
            self.assertEqual(status, 0, stderr.getvalue())
            filtered = output.read_text(encoding="utf-8")
            self.assertNotIn("legacy.api", filtered)
            self.assertNotIn("p_pattern => 'old'", filtered)
            self.assertIn("current.api", filtered)

            missing_output = root / "missing-filter.sql"
            with patch.object(baseline, "ROOT", root), contextlib.redirect_stderr(stderr):
                status = baseline.main([
                    "filter-ords", "--exclude-module", "missing.api",
                    "--input", str(source), "--output", str(missing_output),
                ])
            self.assertEqual(status, 2)
            self.assertFalse(missing_output.exists())
            self.assertIn("does not match an exported module", stderr.getvalue())


class BaselineBuildIntegrationTests(unittest.TestCase):
    fixtures = ROOT / "tests" / "fixtures" / "baseline"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "baseline.json").write_text(
            json.dumps({
                "schemaVersion": 1,
                "schemas": ["APP_DEV"],
                "prefixes": ["APP_", "PKG_", "V_"],
                "excludedObjects": [],
                "referenceData": {"tables": []},
                "sequenceMappings": [],
                "grants": {"skipGrantees": [], "includeGrantees": [], "keepGrantOptions": True},
            }) + "\n",
            encoding="utf-8",
        )
        (self.root / "migrations").mkdir()
        (self.root / "migrations" / ".gitkeep").write_text("", encoding="utf-8")
        self.source = json.loads((self.fixtures / "build_dev.json").read_text(encoding="utf-8"))
        self.target = json.loads((self.fixtures / "build_staging.json").read_text(encoding="utf-8"))
        snapshot_data = json.loads((self.fixtures / "build_snapshot.json").read_text(encoding="utf-8"))
        definitions = {}
        for row in snapshot_data["definitions"]:
            key = ObjectKey(row["owner"], row["name"], row["type"])
            dependents = tuple(ObjectKey(item["owner"], item["name"], item["type"]) for item in row.get("dependents", []))
            definitions[key] = ObjectDefinition(key, row.get("attributes", {}), row["raw_ddl"], dependents, True)
        self.snapshot = SchemaSnapshot(self.source["identity"], {}, definitions, {}, "", "")
        self.values = {
            "DB_ENVIRONMENT": "development",
            "CODE_SCHEMA": "APP_DEV", "CODE_SQLCL_CONNECTION": "DEV", "CODE_EXPECTED_USER": "APP_DEV",
            "STAGING_SCHEMA": "APP_STAGE", "STAGING_SQLCL_CONNECTION": "STAGE", "STAGING_EXPECTED_USER": "APP_STAGE",
        }

    def run_build(self) -> tuple[int, str, str]:
        def capture(_target, environment, _sections, _run_dir, **_kwargs):
            catalog = self.source if environment == "dev" else self.target
            return json.loads(json.dumps(catalog))

        def snapshot(_target, _keys, _run_dir):
            return None, self.snapshot

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(baseline, "ROOT", self.root),
            patch.object(baseline, "capture_environment_catalog", side_effect=capture),
            patch.object(baseline, "capture_inventory_snapshot_with_retries", side_effect=snapshot),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = baseline.main(["build", "--to", "staging"], environ=self.values)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_build_generates_valid_structure_grants_and_exact_source_folders_idempotently(self) -> None:
        code, output, error = self.run_build()
        self.assertEqual(code, 0, output + error)
        folders = sorted((self.root / "migrations" / "APP_STAGE").iterdir())
        self.assertEqual(len(folders), 3, output)
        generated = {path.name.split("_", 1)[1].split("-r", 1)[0]: path for path in folders}
        structure, grants, code_folder = (generated[name] for name in ("baseline-structure", "baseline-grants", "baseline-code"))
        structure_sql = (structure / "001-structure-delta.sql").read_text(encoding="utf-8")
        grants_sql = (grants / "001-object-grants.sql").read_text(encoding="utf-8")
        code_sql = "\n".join(path.read_text(encoding="utf-8") for path in code_folder.glob("*.sql"))
        code_checks = json.loads((code_folder / "checks.json").read_text(encoding="utf-8"))

        self.assertIn("ALL_TABLES", structure_sql)
        self.assertIn("DROP CONSTRAINT", structure_sql)
        self.assertIn("SQLCODE = -1442", structure_sql)
        self.assertNotIn("APP_ITEMS_IX_NEW\" ON", structure_sql)
        self.assertIn("WITH GRANT OPTION", grants_sql)
        self.assertIn("PLSQL_OPTIMIZE_LEVEL = 2", code_sql)
        self.assertIn("l_source CLOB", code_sql)
        self.assertIn("COMPILE BODY", code_sql)
        self.assertTrue(any(check["id"].startswith("valid-object-") for check in code_checks["postconditions"]))
        self.assertTrue(any("STATUS = 'VALID'" in check["sql"] for check in code_checks["postconditions"]))
        self.assertIn("l_ddl CLOB", code_sql)
        self.assertIn("urce view", code_sql)
        self.assertNotIn("OVERRIDING SYSTEM VALUE", structure_sql + grants_sql + code_sql)
        for folder in folders:
            migration = load_migration(self.root, folder.relative_to(self.root).as_posix())
            self.assertTrue(migration.files)

        before = {folder: {child.name: child.read_bytes() for child in folder.iterdir()} for folder in folders}
        second_code, second_output, second_error = self.run_build()
        self.assertEqual(second_code, 0, second_output + second_error)
        self.assertIn("Unchanged migration; left byte-identical", second_output)
        self.assertEqual(folders, sorted((self.root / "migrations" / "APP_STAGE").iterdir()))
        self.assertEqual(before, {folder: {child.name: child.read_bytes() for child in folder.iterdir()} for folder in folders})

    def test_receipted_folder_is_reported_and_not_rewritten(self) -> None:
        code, output, error = self.run_build()
        self.assertEqual(code, 0, output + error)
        code_folder = next((self.root / "migrations" / "APP_STAGE").glob("*_baseline-code-r001"))
        receipt = {"schemaVersion": 1, "state": "verified", "environment": "dev"}
        (code_folder / "status.dev.json").write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        before = {path.name: path.read_bytes() for path in code_folder.iterdir()}

        second_code, second_output, second_error = self.run_build()

        self.assertEqual(second_code, 0, second_output + second_error)
        self.assertIn("Skipped receipted or attempted migration without changing it", second_output)
        self.assertEqual(before, {path.name: path.read_bytes() for path in code_folder.iterdir()})

    def test_build_data_reads_export_and_creates_label_mapped_migration(self) -> None:
        config = {
            "schemaVersion": 1,
            "schemas": ["APP_DEV"],
            "prefixes": ["APP_", "PKG_", "V_"],
            "excludedObjects": [],
            "referenceData": {"tables": [
                {"name": "APP_CANCEL_REASON", "excludeColumns": ["API_TOKEN"], "keyColumns": ["REASON_CODE"], "labelColumns": ["REASON_LABEL"], "identity": {"column": "REASON_ID", "generationType": "ALWAYS"}, "rowLimit": 100},
                {"name": "APP_CANCEL_POLICY", "excludeColumns": ["API_TOKEN", "REFRESH_TOKEN"], "keyColumns": ["POLICY_CODE"], "labelColumns": ["POLICY_NAME"], "identity": {"column": "POLICY_ID", "generationType": "BY DEFAULT"}, "rowLimit": 100},
                {"name": "APP_ALWAYS_LOOKUP", "excludeColumns": [], "keyColumns": ["LOOKUP_CODE"], "labelColumns": ["LOOKUP_LABEL"], "identity": {"column": "LOOKUP_ID", "generationType": "ALWAYS"}, "rowLimit": 100},
            ]},
            "sequenceMappings": [],
            "grants": {"skipGrantees": [], "includeGrantees": [], "keepGrantOptions": True},
        }
        (self.root / "baseline.json").write_text(json.dumps(config) + "\n", encoding="utf-8")
        source_data = json.loads((self.fixtures / "reference_data_source.json").read_text(encoding="utf-8"))
        data_root = self.root / "scratch" / "baseline" / "dev" / "data" / "APP_DEV"
        for name, table in source_data.items():
            (data_root / f"{name}.json").parent.mkdir(parents=True, exist_ok=True)
            (data_root / f"{name}.json").write_text(json.dumps(table) + "\n", encoding="utf-8")
        target_data = json.loads((self.fixtures / "reference_data_target.json").read_text(encoding="utf-8"))
        constraints = json.loads((self.fixtures / "reference_data_constraints.json").read_text(encoding="utf-8"))
        source_catalog = json.loads(json.dumps(self.source))
        target_catalog = json.loads(json.dumps(self.target))
        for catalog in (source_catalog, target_catalog):
            catalog["sections"]["tables"].extend({"name": name} for name in target_data)
            catalog["sections"]["constraints"].extend(constraints)
        target_catalog["sections"]["baseline-data"] = list(target_data.values())
        target_catalog["coverage"]["sections"]["baseline-data"] = {"complete": True, "pages": [3]}

        def capture(_target, environment, sections, _run_dir, **_kwargs):
            selected = source_catalog if environment == "dev" else target_catalog
            return json.loads(json.dumps(selected))

        def snapshot(_target, _keys, _run_dir):
            return None, self.snapshot

        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch.object(baseline, "ROOT", self.root),
            patch.object(baseline, "capture_environment_catalog", side_effect=capture),
            patch.object(baseline, "capture_inventory_snapshot_with_retries", side_effect=snapshot),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = baseline.main(["build", "--from", "dev", "--to", "staging", "--data"], environ=self.values)

        self.assertEqual(code, 0, stdout.getvalue() + stderr.getvalue())
        folder = next((self.root / "migrations" / "APP_STAGE").glob("*_baseline-data-r001"))
        sql = "\n".join(path.read_text(encoding="utf-8") for path in folder.glob("*.sql"))
        self.assertIn('SELECT P."REASON_ID" FROM "APP_STAGE"."APP_CANCEL_REASON" P', sql)
        self.assertNotIn('"REASON_ID" = 5', sql)
        self.assertNotIn("API_TOKEN", sql)


if __name__ == "__main__":
    unittest.main()
