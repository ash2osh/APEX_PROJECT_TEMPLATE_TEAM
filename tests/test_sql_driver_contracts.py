import re
import sys
import unittest
from pathlib import Path
import tempfile

from scripts.db_targets import Target
from scripts.schema_catalog import _catalog_driver


ROOT = Path(__file__).resolve().parents[1]
SQL_DRIVERS = (
    "backup_db.sql",
    "check_builder_drift.sql",
    "doctor.sql",
    "export_apps.sql",
    "lookup_app_schema.sql",
    "publish_app.sql",
    "schema_catalog.sql",
)


class SqlDriverContractTests(unittest.TestCase):
    def test_sql_errors_use_stable_failure_status_and_rollback(self) -> None:
        for name in SQL_DRIVERS:
            with self.subTest(driver=name):
                contents = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertIn("WHENEVER SQLERROR EXIT FAILURE ROLLBACK", contents)
                self.assertIn("WHENEVER OSERROR EXIT FAILURE ROLLBACK", contents)
                self.assertNotIn("SQL.SQLCODE", contents)

    def test_read_only_drivers_exit_success_with_rollback(self) -> None:
        for name in ("check_builder_drift.sql", "doctor.sql", "export_apps.sql", "lookup_app_schema.sql", "backup_db.sql"):
            with self.subTest(driver=name):
                contents = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertRegex(contents, r"(?im)^EXIT SUCCESS ROLLBACK\s*$")

    def test_feature_trigger_catalog_captures_database_object_status(self) -> None:
        from scripts import compare_schema

        contents = (ROOT / "scripts/compare_env_catalog.sql").read_text(encoding="utf-8")
        start = contents.index("PROCEDURE add_triggers")
        end = contents.index("PROCEDURE add_stored_code", start)
        trigger_reader = contents[start:end]
        self.assertIn("all_objects o", trigger_reader)
        self.assertIn("o.object_type = 'TRIGGER'", trigger_reader)
        self.assertIn("l_page.put('object_status', trigger_row.object_status)", trigger_reader)
        self.assertIn("object_status", compare_schema.COMPARE_ENV_FIELDS["triggers"])

    def test_generated_catalog_driver_exits_success_with_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            driver = _catalog_driver(Path(temporary), "inventory", Target("dev", "dev-profile", "APP_DEV", "APP_DEV", "development"))
            contents = driver.read_text(encoding="utf-8")
        self.assertRegex(contents, r"(?im)^EXIT SUCCESS ROLLBACK\s*$")
        self.assertNotIn("COMMIT", contents)
        self.assertIn("ALTER SESSION SET CURRENT_SCHEMA = APP_DEV", contents)

    def test_generated_catalog_driver_terminates_alter_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            driver = _catalog_driver(Path(temporary), "inventory", Target("dev", "dev-profile", "APP_DEV", "APP_DEV", "development"))
            lines = driver.read_text(encoding="utf-8").splitlines()

        alter_session = next(line for line in lines if line.startswith("ALTER SESSION SET CURRENT_SCHEMA ="))
        self.assertTrue(alter_session.endswith(";"))

    def test_generated_catalog_driver_silences_sqlcl_before_first_include(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            driver = _catalog_driver(Path(temporary), "inventory", Target("dev", "dev-profile", "APP_DEV", "APP_DEV", "development"))
            lines = driver.read_text(encoding="utf-8").splitlines()

        first_include = next(index for index, line in enumerate(lines) if line.startswith("@@schema_catalog.sql"))
        self.assertIn("SET VERIFY OFF", lines[:first_include])
        self.assertIn("SET FEEDBACK OFF", lines[:first_include])

    def test_generated_catalog_driver_disables_ddl_insert_before_first_include(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            driver = _catalog_driver(Path(temporary), "inventory", Target("dev", "dev-profile", "APP_DEV", "APP_DEV", "development"))
            lines = driver.read_text(encoding="utf-8").splitlines()

        first_include = next(index for index, line in enumerate(lines) if line.startswith("@@schema_catalog.sql"))
        self.assertIn("SET DDL INSERT OFF", lines[:first_include])

    def test_character_chunk_buffers_hold_the_bytes_of_the_characters_they_receive(self) -> None:
        # DBMS_LOB.SUBSTR counts characters; a VARCHAR2 variable counts bytes
        # here. A 4000-character chunk of UTF-8 text with accented letters is more
        # than 4000 bytes, so ORA-06502 ended a catalog read after the migration
        # had already committed an object whose name or DDL held one. The catalog
        # driver now emits gzip/base64 from a BLOB and has a separate byte-path test.
        checked = 0
        # The drivers are SQL files, and Python builds one more (the migration checks).
        for path in sorted((ROOT / "scripts").glob("*.sql")) + sorted((ROOT / "scripts").glob("*.py")):
            contents = path.read_text(encoding="utf-8")
            for buffer in re.finditer(r"(?i)\b(\w+)\s+VARCHAR2\((\d+)\)\s*;", contents):
                name, size = buffer.group(1), int(buffer.group(2))
                for fill in re.finditer(rf"(?i)\b{name}\s*:=\s*DBMS_LOB\.SUBSTR\(\s*\w+\s*,\s*(\d+)\s*,", contents):
                    checked += 1
                    with self.subTest(driver=path.name, buffer=name):
                        self.assertGreaterEqual(size, 4 * int(fill.group(1)), f"{name} VARCHAR2({size}) cannot hold {fill.group(1)} four-byte characters")
                        self.assertLessEqual(size, 32767)
        self.assertGreaterEqual(checked, 2, "the character chunking loops were not found; update this test")

    def test_catalog_driver_transcodes_utf8_clob_to_binary_before_base64_chunking(self) -> None:
        contents = (ROOT / "scripts" / "schema_catalog.sql").read_text(encoding="utf-8")
        self.assertIn("blob_csid    => NLS_CHARSET_ID('AL32UTF8')", contents)
        self.assertIn("l_gzip := UTL_COMPRESS.LZ_COMPRESS(l_blob)", contents)
        self.assertRegex(contents, r"l_piece\s+RAW\(12000\)")
        self.assertRegex(contents, r"l_chunk\s+VARCHAR2\(32767\)")
        self.assertIn("l_piece := DBMS_LOB.SUBSTR(l_gzip, 12000, l_pos)", contents)
        self.assertIn("l_pos := l_pos + UTL_RAW.LENGTH(l_piece)", contents)
        self.assertIn("CATALOG_ENCODING:gzip-base64-v1", contents)
        self.assertGreaterEqual(32767, 4 * 12000 // 3)

    def test_catalog_driver_cleans_temporary_lobs_on_success_and_exception(self) -> None:
        contents = (ROOT / "scripts" / "schema_catalog.sql").read_text(encoding="utf-8")
        self.assertIn("DBMS_LOB.ISTEMPORARY", contents)
        self.assertIn("DBMS_LOB.FREETEMPORARY", contents)
        self.assertGreaterEqual(len(re.findall(r"(?i)cleanup_temp_blob\s*\(\s*l_gzip\s*\)", contents)), 2)
        self.assertGreaterEqual(len(re.findall(r"(?i)cleanup_temp_blob\s*\(\s*l_blob\s*\)", contents)), 2)
        self.assertGreaterEqual(len(re.findall(r"(?i)cleanup_temp_lob\s*\(\s*l_raw\s*\)", contents)), 2)
        self.assertRegex(
            contents,
            r"(?i)cleanup_temp_blob\s*\(\s*l_gzip\s*\);\s*"
            r"cleanup_temp_blob\s*\(\s*l_blob\s*\);\s*"
            r"cleanup_temp_lob\s*\(\s*l_raw\s*\);\s*"
            r"EXCEPTION\s+WHEN\s+OTHERS\s+THEN\s*"
            r"cleanup_temp_blob\s*\(\s*l_gzip\s*\);\s*"
            r"cleanup_temp_blob\s*\(\s*l_blob\s*\);\s*"
            r"cleanup_temp_lob\s*\(\s*l_raw\s*\);\s*"
            r"ROLLBACK;\s*RAISE;",
        )


    def test_the_catalog_driver_lifts_the_dbms_output_cap_before_it_prints_the_payload(self) -> None:
        # SQLcl's SERVEROUTPUT SIZE UNLIMITED still leaves a 1,000,000-byte buffer
        # (ORU-10027), so a selected definition over 1 MB (a large package body)
        # could not be captured. DBMS_OUTPUT.ENABLE(NULL) inside the block lifts it.
        contents = (ROOT / "scripts" / "schema_catalog.sql").read_text(encoding="utf-8")
        enable = re.search(r"(?i)DBMS_OUTPUT\.ENABLE\(\s*NULL\s*\)\s*;", contents)
        first_put = re.search(r"(?i)DBMS_OUTPUT\.PUT_LINE\(", contents)
        self.assertIsNotNone(enable, "schema_catalog.sql must call DBMS_OUTPUT.ENABLE(NULL)")
        self.assertIsNotNone(first_put)
        self.assertLess(enable.start(), first_put.start())

    def test_identity_and_drift_drivers_emit_verification_sentinels(self) -> None:
        self.assertIn("APEX_DOCTOR_VERIFIED:&&expected_user", (ROOT / "scripts/doctor.sql").read_text(encoding="utf-8"))
        self.assertIn("APEX_DRIFT_QUERY_VERIFIED", (ROOT / "scripts/check_builder_drift.sql").read_text(encoding="utf-8"))

    def test_publish_driver_imports_an_explicit_source_and_descriptor(self) -> None:
        contents = (ROOT / "scripts/publish_app.sql").read_text(encoding="utf-8")
        self.assertIn('apex import -input "&&application_source" -deployment "&&deployment_file"', contents)
        self.assertNotIn("apex import -input .", contents)

    def test_publish_driver_rechecks_the_approved_live_state_before_import(self) -> None:
        contents = (ROOT / "scripts/publish_app.sql").read_text(encoding="utf-8")
        self.assertIn("DEFINE expected_live_state = '&7'", contents)
        recheck = contents.index("-20016")
        self.assertLess(recheck, contents.index("apex import -input"))
        self.assertIn("REGEXP_REPLACE(v_version, '[' || c_python_whitespace || ']+$')", contents)
        # Built in PL/SQL (32767-byte strings), not in SQL, so a long
        # multibyte version still fits once hex-encoded.
        block = contents[contents.index("c_python_whitespace CONSTANT") - 400 : recheck]
        self.assertIn("v_observed VARCHAR2(32767);", block)
        self.assertNotIn("RAWTOHEX(UTL_I18N.STRING_TO_RAW(REGEXP_REPLACE(MAX(", contents)
        # The SQL trims exactly the characters Python's str.rstrip() trims.
        listed = set(re.findall(r"\\([0-9A-F]{4})", contents[contents.index("c_python_whitespace CONSTANT"):contents.index("BEGIN", contents.index("c_python_whitespace CONSTANT"))]))
        python = {f"{code:04X}" for code in range(sys.maxunicode + 1) if chr(code).isspace()}
        self.assertEqual(listed, python)

    def test_revision_queries_distinguish_imported_app_from_absent_app(self) -> None:
        # APEX leaves last_updated_on NULL on import, so NVL(MAX(...)) alone
        # would report an installed app as NOT_FOUND.
        for name, queries in (("check_builder_drift.sql", 1), ("export_apps.sql", 2)):
            with self.subTest(driver=name):
                contents = (ROOT / "scripts" / name).read_text(encoding="utf-8")
                self.assertEqual(contents.count("WHEN COUNT(*) = 0 THEN 'NOT_FOUND'"), queries)
                self.assertEqual(contents.count("WHEN MAX(last_updated_on) IS NULL THEN 'NO_TIMESTAMP'"), queries)
                self.assertEqual(contents.count("|| '|' || MAX(version)"), queries)
                self.assertIn("SET LINESIZE 32767", contents)

    def test_release_apply_driver_enables_sqlblanklines_and_never_sets_plsql_code_type(self) -> None:
        from scripts import release

        target = Target("dev", "dev-db", "OPS", "APP", "development")
        with tempfile.TemporaryDirectory() as temporary:
            sql_file = Path(temporary) / "001-update.sql"
            sql_file.write_text("UPDATE review_table SET flag = 1\n\nWHERE id = 1;\n", encoding="utf-8", newline="\n")
            step = release.BundleStep("ddl", "APP", "01-ddl/001-update.sql", sql_file)
            contents = release._render_apply_driver(target, [step])
        self.assertIn("SET SQLBLANKLINES ON", contents)
        self.assertIn("ALTER SESSION SET CURRENT_SCHEMA = APP;", contents)
        self.assertIn("WHENEVER SQLERROR EXIT FAILURE ROLLBACK", contents)
        self.assertNotIn("SQL.SQLCODE", contents)
        self.assertNotIn("PLSQL_CODE_TYPE", contents)
        self.assertLess(contents.index("SET SQLBLANKLINES ON"), contents.index("RELEASE_STEP_START:01-ddl/001-update.sql"))


BACKUP_DB_SQL = ROOT / "scripts" / "backup_db.sql"


def generated_driver_selects(source: str) -> list[str]:
    """The SELECT statements that build the spool driver: each starts with a SPOOL path string."""
    return re.findall(r"(?s)SELECT 'SPOOL database/&&spool_schema/.*?ORDER BY [^;]*;", source)


def string_literals(text: str) -> list[str]:
    return re.findall(r"'((?:[^']|'')*)'", text)


class BackupDbTriggerSequenceSqlTests(unittest.TestCase):
    """backup_db.sql: the tables scope owns table triggers and sequences; the code scope keeps the rest."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.source = BACKUP_DB_SQL.read_text(encoding="utf-8")

    def test_seventh_argument_is_the_tables_prefixes_after_the_spool_directory(self) -> None:
        self.assertIn("DEFINE spool_schema = '&6'\nDEFINE tables_prefixes = '&7'", self.source)

    def test_table_owned_trigger_predicate_is_used_by_tables_export_code_exclusion_and_both_manifest_counts(self) -> None:
        # The predicate is written out in each place; each copy must test the same five conditions.
        predicate = "AND t.table_owner = t.owner\n"
        self.assertGreaterEqual(self.source.count(predicate), 5, "every trigger query needs the table-owned predicate")
        self.assertIn("'&&tables_prefixes' <> '-'", self.source)
        triggers = [s for s in generated_driver_selects(self.source) if "triggers/" in s]
        self.assertEqual(len(triggers), 2, "the tables scope and the code scope both spool triggers/")
        table_trigger_export = next(s for s in triggers if "FROM all_triggers t\nWHERE LOWER('&&object_scope') = 'tables'" in s)
        self.assertIn("WHERE LOWER('&&object_scope') = 'tables'", table_trigger_export)
        self.assertIn("t.base_object_type = 'TABLE'", table_trigger_export)
        self.assertIn("INSTR(t.trigger_name, configured_prefixes.object_prefix) = 1", table_trigger_export)
        # The code scope's trigger export excludes exactly the table-owned triggers.
        code_trigger_export = next(s for s in triggers if "FROM all_objects objects_to_export" in s)
        self.assertIn("AND NOT EXISTS (\n    SELECT 1\n    FROM all_triggers t\n    WHERE t.owner = objects_to_export.owner", code_trigger_export)
        self.assertIn("AND t.trigger_name = objects_to_export.object_name", code_trigger_export)
        self.assertIn("'&&tables_prefixes' <> '-'", code_trigger_export)

    def test_manifest_counts_use_the_same_predicates_per_type(self) -> None:
        manifest = self.source[self.source.index("SPOOL database/&&spool_schema/&&manifest_file"):]
        for object_type, scope in (
            ("SEQUENCE", "tables"), ("TABLE", "tables"), ("TRIGGER", "tables"),
            ("FUNCTION", "code"), ("PACKAGE", "code"), ("PACKAGE BODY", "code"), ("PROCEDURE", "code"),
            ("SYNONYM", "code"), ("TRIGGER", "code"), ("VIEW", "code"),
        ):
            with self.subTest(type=object_type, scope=scope):
                self.assertRegex(manifest, rf"SELECT '{re.escape(object_type)}'[^,\n]*,\s*'{scope}'[^,\n]*,\s*COUNT\(\*\)")
        self.assertIn("WHERE object_scope = LOWER('&&object_scope')\nORDER BY object_type;", manifest)
        self.assertIn("SELECT object_type || '=' || object_count", manifest)
        self.assertIn("AND NOT EXISTS (\n      SELECT 1\n      FROM all_triggers t\n      WHERE t.owner = o.owner", manifest)

    def test_identity_and_system_sequences_are_excluded_everywhere_sequences_are_counted_or_exported(self) -> None:
        self.assertGreaterEqual(self.source.count("s.sequence_name NOT LIKE 'ISEQ$$%'"), 3)
        self.assertGreaterEqual(self.source.count("FROM all_tab_identity_cols i"), 3)
        sequences = [s for s in generated_driver_selects(self.source) if "sequences/" in s]
        self.assertEqual(len(sequences), 1)
        self.assertIn("WHERE LOWER('&&object_scope') = 'tables'", sequences[0])

    def test_sequence_ddl_drops_the_live_start_with_position(self) -> None:
        sequences = next(s for s in generated_driver_selects(self.source) if "sequences/" in s)
        self.assertIn("REGEXP_REPLACE(DBMS_METADATA.GET_DDL(''SEQUENCE''", sequences)
        self.assertIn("''[[:space:]]+START WITH -?[0-9]+''", sequences)
        self.assertIn("''[[:space:]]+START WITH -?[0-9]+'', '''') FROM DUAL'", sequences)
        self.assertIn("|| CHR(59)", sequences)

    def test_generated_driver_strings_hold_no_literal_semicolon(self) -> None:
        selects = generated_driver_selects(self.source)
        self.assertGreaterEqual(len(selects), 8, "the generated-driver SELECTs were not all found")
        for select in selects:
            with self.subTest(select=select[:60]):
                for literal in string_literals(select):
                    self.assertNotIn(";", literal)
                self.assertIn("|| CHR(59)", select)

    def test_unsafe_name_check_covers_table_owned_triggers_and_sequences_for_the_tables_scope(self) -> None:
        check = self.source[self.source.index("v_unsafe VARCHAR2(4000);"):self.source.index("IF v_unsafe IS NOT NULL")]
        self.assertIn("FROM all_triggers t", check)
        self.assertIn("SELECT s.sequence_name", check)
        self.assertIn("LOWER('&&object_scope') = 'tables'", check)
        self.assertIn("NOT REGEXP_LIKE(object_name, '^[A-Za-z0-9_$#]+$')", check)


if __name__ == "__main__":
    unittest.main()
