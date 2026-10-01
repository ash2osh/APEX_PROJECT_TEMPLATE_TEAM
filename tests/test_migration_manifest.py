import hashlib
import json
import tempfile
import unittest
from pathlib import Path

try:
    from scripts import migration_manifest as manifest
except ImportError:
    manifest = None


class MigrationManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "migrations").mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def require_manifest(self):
        self.assertIsNotNone(manifest, "dated migration manifest handling is not implemented")
        return manifest

    def add_folder(self, name: str, sql_files: dict[str, str] | None = None, checks: dict | None = None) -> Path:
        folder = self.root / "migrations" / name
        folder.mkdir(parents=True)
        for filename, source in (sql_files or {"001-create-table.sql": "CREATE TABLE T (ID NUMBER);\n"}).items():
            path = folder / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8", newline="\n")
        if checks is None:
            checks = {
                "schemaVersion": 1,
                "preconditions": [],
                "postconditions": [{"id": "table-exists", "sql": "SELECT CASE WHEN COUNT(*) = 1 THEN 1 ELSE 0 END FROM user_tables WHERE table_name = 'T'", "expected": 1}],
            }
        (folder / "checks.json").write_text(json.dumps(checks) + "\n", encoding="utf-8")
        return folder

    def test_iso_dates_sort_newest_first_without_reordering_batch(self) -> None:
        api = self.require_manifest()
        older = self.add_folder("2026-09-27_create-customers-r001")
        newer = self.add_folder("2026-09-28_add-customer-status-r001")
        same_day = self.add_folder("2026-09-27_add-orders-r001")

        listed = api.list_migration_folders(self.root)
        batch = api.load_batch(self.root, ["migrations/2026-09-27_create-customers-r001", "migrations/2026-09-28_add-customer-status-r001"])

        self.assertEqual(listed[0], newer)
        self.assertEqual(listed[1:], tuple(sorted((older, same_day), key=lambda path: path.name, reverse=True)))
        self.assertEqual(tuple(migration.folder for migration in batch), (older, newer))

    def test_sequences_are_contiguous_and_named(self) -> None:
        api = self.require_manifest()
        folder = self.add_folder(
            "2026-09-27_create-customers-r001",
            {
                "001-create-table.sql": "CREATE TABLE T (ID NUMBER);\n",
                "002-create-indexes.sql": "CREATE INDEX T_I ON T(ID);\n",
                "003-create-view.sql": "CREATE VIEW T_V AS SELECT ID FROM T;\n",
            },
        )

        migration = api.load_migration(self.root, str(folder.relative_to(self.root)))

        self.assertEqual(tuple(file.sequence for file in migration.files), (1, 2, 3))
        self.assertEqual(tuple(file.name for file in migration.files), ("001-create-table.sql", "002-create-indexes.sql", "003-create-view.sql"))

    def test_invalid_folder_names_and_sequences_are_rejected(self) -> None:
        api = self.require_manifest()
        invalid = (
            "2026-02-30_create-customers-r001",
            "2026-09-27_create-customers-r000",
            "2026-09-27_Create-customers-r001",
            "2026-09-27_create_customers-r001",
        )
        for name in invalid:
            with self.subTest(name=name):
                self.add_folder(name)
                with self.assertRaises(api.MigrationManifestError):
                    api.load_migration(self.root, f"migrations/{name}")

        for files in (
            {"000-create.sql": "SELECT 1 FROM dual;\n"},
            {"001-create.sql": "SELECT 1 FROM dual;\n", "003-alter.sql": "SELECT 1 FROM dual;\n"},
            {"001-first.sql": "SELECT 1 FROM dual;\n", "001-second.sql": "SELECT 1 FROM dual;\n"},
            {"create.sql": "SELECT 1 FROM dual;\n"},
        ):
            with self.subTest(files=files), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "migrations").mkdir()
                folder = root / "migrations" / "2026-09-27_create-customers-r001"
                folder.mkdir()
                for filename, source in files.items():
                    (folder / filename).write_text(source, encoding="utf-8")
                (folder / "checks.json").write_text(json.dumps({"schemaVersion": 1, "preconditions": [], "postconditions": [{"id": "ok", "sql": "SELECT 1 FROM dual", "expected": 1}]}))
                with self.assertRaises(api.MigrationManifestError):
                    api.load_migration(root, "migrations/2026-09-27_create-customers-r001")

    def test_revision_gaps_and_duplicate_family_revision_are_rejected(self) -> None:
        api = self.require_manifest()
        self.add_folder("2026-09-27_create-customers-r001")
        self.add_folder("2026-09-28_create-customers-r003")
        with self.assertRaises(api.MigrationManifestError):
            api.load_batch(self.root, ["migrations/2026-09-27_create-customers-r001", "migrations/2026-09-28_create-customers-r003"])

        self.add_folder("2026-09-29_create-customers-r001")
        with self.assertRaises(api.MigrationManifestError):
            api.load_batch(self.root, ["migrations/2026-09-27_create-customers-r001", "migrations/2026-09-29_create-customers-r001"])

    def test_folder_paths_and_file_content_fail_closed(self) -> None:
        api = self.require_manifest()
        folder = self.add_folder("2026-09-27_create-customers-r001")
        outside = self.root / "outside.sql"
        outside.write_text("SELECT 1 FROM dual;\n", encoding="utf-8")
        (folder / "002-escape.sql").symlink_to(outside)
        with self.assertRaises(api.MigrationManifestError):
            api.load_migration(self.root, "migrations/2026-09-27_create-customers-r001")

        for payload in (b"SELECT 1 FROM dual;\r\n", b"\xff\xfe"):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                target = root / "migrations" / "2026-09-27_create-customers-r001"
                target.mkdir(parents=True)
                (target / "001-create.sql").write_bytes(payload)
                (target / "checks.json").write_text(json.dumps({"schemaVersion": 1, "preconditions": [], "postconditions": [{"id": "ok", "sql": "SELECT 1 FROM dual", "expected": 1}]}))
                with self.assertRaises(api.MigrationManifestError):
                    api.load_migration(root, "migrations/2026-09-27_create-customers-r001")

        with self.assertRaises(api.MigrationManifestError):
            api.load_migration(self.root, "migrations/../outside.sql")

    def test_checks_require_valid_read_only_assertions(self) -> None:
        api = self.require_manifest()
        allowed = (
            "SELECT CASE WHEN COUNT(*) = 1 THEN 1 ELSE 0 END FROM user_tables WHERE table_name = 'T'",
            "WITH expected AS (SELECT 1 AS value FROM dual) SELECT MAX(value) FROM expected",
            "SELECT CASE WHEN SYS_CONTEXT('USERENV', 'SESSION_USER') IS NOT NULL THEN 1 ELSE 0 END FROM dual",
            "SELECT CASE WHEN COUNT(*) = 1 THEN 1 ELSE 0 END FROM all_tables WHERE owner = :target_schema",
        )
        rejected = (
            "PROMPT hello",
            "SELECT 1 FROM dual; SELECT 2 FROM dual",
            "SELECT id FROM t FOR UPDATE",
            "SELECT seq.NEXTVAL FROM dual",
            "SELECT seq.CURRVAL FROM dual",
            "BEGIN NULL; END;",
            "DELETE FROM t",
            "SELECT dangerous_function() FROM dual",
            "SELECT 1 FROM remote_table@other_db",
            "SELECT COUNT(*) FROM all_tables WHERE owner = :some_owner",
        )
        for query in allowed:
            with self.subTest(query=query):
                api.validate_check_query(query)
        for query in rejected:
            with self.subTest(query=query):
                with self.assertRaises(api.MigrationManifestError):
                    api.validate_check_query(query)

        for checks in (
            {"schemaVersion": 1, "preconditions": [], "postconditions": []},
            {"schemaVersion": 2, "preconditions": [], "postconditions": [{"id": "ok", "sql": "SELECT 1 FROM dual", "expected": 1}]},
            {"schemaVersion": 1, "preconditions": [], "postconditions": [{"id": "same", "sql": "SELECT 1 FROM dual", "expected": 0}, {"id": "same", "sql": "SELECT 1 FROM dual", "expected": 1}]},
        ):
            with self.subTest(checks=checks), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                folder = root / "migrations" / "2026-09-27_create-customers-r001"
                folder.mkdir(parents=True)
                (folder / "001-create.sql").write_text("SELECT 1 FROM dual;\n")
                (folder / "checks.json").write_text(json.dumps(checks))
                with self.assertRaises(api.MigrationManifestError):
                    api.load_migration(root, "migrations/2026-09-27_create-customers-r001")

    def test_independent_same_name_folders_have_content_bound_digests(self) -> None:
        api = self.require_manifest()
        with tempfile.TemporaryDirectory() as other_temp:
            first = self.add_folder("2026-09-27_create-customers-r001", {"001-create-table.sql": "CREATE TABLE T (ID NUMBER);\n"})
            second_root = Path(other_temp)
            (second_root / "migrations").mkdir()
            second = second_root / "migrations" / first.name
            second.mkdir()
            (second / "001-create-table.sql").write_text("CREATE TABLE T (ID NUMBER, NAME VARCHAR2(20));\n")
            (second / "checks.json").write_text((first / "checks.json").read_text())

            first_migration = api.load_migration(self.root, str(first.relative_to(self.root)))
            second_migration = api.load_migration(second_root, str(second.relative_to(second_root)))

            self.assertEqual(first.name, second.name)
            self.assertNotEqual(first_migration.payload_digest, second_migration.payload_digest)
            expected_file_hash = hashlib.sha256((first / "001-create-table.sql").read_bytes()).hexdigest()
            self.assertEqual(first_migration.files[0].sha256, expected_file_hash)

    def test_sql_keywords_before_a_parenthesis_are_not_function_calls(self) -> None:
        # A word followed by '(' was treated as a function call, so ordinary
        # read-only SQL such as AND (...) or FROM (subquery) was refused as a
        # call to a "user-defined function" named AND or FROM.
        api = self.require_manifest()
        allowed = (
            "SELECT CASE WHEN (SELECT COUNT(*) FROM user_tables WHERE table_name = 'T') = 1 THEN 1 ELSE 0 END FROM dual",
            "SELECT COUNT(*) FROM user_tables WHERE table_name = 'T' AND (num_rows = 1 OR num_rows IS NULL)",
            "SELECT COUNT(*) FROM user_tables WHERE table_name = 'T' OR (table_name = 'U' AND num_rows = 1)",
            "SELECT COUNT(*) FROM user_tables WHERE (table_name = 'T')",
            "SELECT COUNT(*) FROM user_tables WHERE NOT (table_name = 'T')",
            "SELECT CASE WHEN COUNT(*) > 0 THEN (1) ELSE (0) END FROM user_tables",
            "SELECT (COUNT(*)) FROM user_tables",
            "SELECT COUNT(*) FROM (SELECT table_name FROM user_tables)",
            "SELECT COUNT(*) FROM user_tables t JOIN (SELECT table_name FROM user_tables) u ON (t.table_name = u.table_name)",
            "SELECT COUNT(*) FROM user_tables t JOIN user_tab_columns c USING (table_name)",
            "SELECT COUNT(*) FROM user_tables WHERE num_rows > ALL (SELECT num_rows FROM user_tables WHERE table_name = 'X')",
            "SELECT COUNT(*) FROM user_tab_columns GROUP BY (table_name, column_name)",
            "SELECT COUNT(*) FROM user_tab_columns GROUP BY table_name HAVING (COUNT(*) > 1)",
            "SELECT table_name FROM user_tables ORDER BY (table_name)",
            "SELECT 1 FROM dual UNION (SELECT 1 FROM dual)",
            "SELECT COUNT(*) FROM user_tables WHERE num_rows BETWEEN (1) AND (5)",
            "SELECT COUNT(*) FROM user_tables WHERE table_name LIKE ('A' || '%')",
            "SELECT (1 + 2) * (3 + 4) FROM dual",
        )
        for query in allowed:
            with self.subTest(query=query):
                api.validate_check_query(query)

        # Keywords do not open a way to call a function: a user-defined function
        # (including one named after a non-reserved keyword) is still refused.
        rejected = (
            "SELECT my_func(1) FROM dual",
            "SELECT when(1) FROM dual",
            "SELECT join(1) FROM dual",
            "SELECT using(1) FROM dual",
            "SELECT some(1) FROM dual",
            "SELECT lateral(1) FROM dual",
            "SELECT pkg.when(1) FROM dual",
            "SELECT 1 FROM dual WHERE x = my_func (1)",
            "SELECT CASE WHEN my_func(1) = 1 THEN 1 ELSE 0 END FROM dual",
            "SELECT 1 FROM dual WHERE a AND dangerous(1)",
        )
        for query in rejected:
            with self.subTest(query=query):
                with self.assertRaises(api.MigrationManifestError):
                    api.validate_check_query(query)

    def test_a_quoted_name_before_a_parenthesis_is_a_function_call(self) -> None:
        # "MY_FUNC"(1) calls MY_FUNC just as MY_FUNC(1) does, but only unquoted
        # names were checked, so a quoted one was accepted.
        api = self.require_manifest()
        for query in (
            'SELECT CASE WHEN "MY_FUNC"(1) = 1 THEN 1 ELSE 0 END FROM dual',
            'SELECT CASE WHEN "my_func" (1) = 1 THEN 1 ELSE 0 END FROM dual',
            'SELECT 1 FROM dual WHERE "COUNT"(1) = 1',
            'SELECT 1 FROM dual WHERE x = "Dangerous"(1, 2)',
        ):
            with self.subTest(query=query):
                with self.assertRaises(api.MigrationManifestError) as caught:
                    api.validate_check_query(query)
                self.assertIn("function", str(caught.exception))
        for query in (
            'SELECT COUNT(*) FROM "USER_TABLES" WHERE "TABLE_NAME" IN (\'T\', \'U\')',
            'SELECT COUNT(*) AS "TOTAL" FROM user_tables WHERE ("TABLE_NAME" = \'T\')',
            'SELECT COUNT(*) FROM user_tables t WHERE t."TABLE_NAME" = \'T\' AND ("NUM_ROWS" > 0 OR "NUM_ROWS" IS NULL)',
        ):
            with self.subTest(query=query):
                api.validate_check_query(query)

    def test_non_ascii_text_outside_a_quoted_value_is_refused_in_a_check(self) -> None:
        # Oracle accepts accented letters in an unquoted name, so é(1) or
        # my_func_é(1) calls a function, but the tokenizer saw only ASCII words
        # and let the call through.
        api = self.require_manifest()
        for query in (
            "SELECT \u00e9(1) FROM dual",
            "SELECT CASE WHEN my_func_\u00e9(1) = 1 THEN 1 ELSE 0 END FROM dual",
            "SELECT CASE WHEN pkg.func_\u00e9(1) = 1 THEN 1 ELSE 0 END FROM dual",
            "SELECT CASE WHEN \uff46unc(1) = 1 THEN 1 ELSE 0 END FROM dual",
            "SELECT 1 FROM caf\u00e9",
        ):
            with self.subTest(query=query):
                with self.assertRaises(api.MigrationManifestError) as caught:
                    api.validate_check_query(query)
                self.assertIn("non-ASCII", str(caught.exception))
        for query in (
            "SELECT COUNT(*) FROM user_tables WHERE table_name = 'CAF\u00c9'",
            'SELECT COUNT(*) FROM user_tables WHERE table_name = "CAF\u00c9"',
            "SELECT 1 FROM dual -- caf\u00e9",
            "SELECT 1 /* caf\u00e9 */ FROM dual",
        ):
            with self.subTest(query=query):
                api.validate_check_query(query)

    def test_a_quoted_sequence_pseudo_column_is_refused_in_a_check(self) -> None:
        # Oracle reads my_seq."NEXTVAL" as the pseudo-column (it accepts
        # dual."ROWID"), and NEXTVAL advances the sequence, so a check must not
        # use it quoted any more than unquoted.
        api = self.require_manifest()
        for query in (
            'SELECT CASE WHEN my_seq."NEXTVAL" > 0 THEN 1 ELSE 0 END FROM dual',
            'SELECT CASE WHEN app.my_seq."CURRVAL" > 0 THEN 1 ELSE 0 END FROM dual',
            'SELECT 1 FROM dual WHERE s."NEXTVAL" IS NOT NULL',
        ):
            with self.subTest(query=query):
                with self.assertRaises(api.MigrationManifestError) as caught:
                    api.validate_check_query(query)
                self.assertIn("forbidden", str(caught.exception))
        for query in (
            'SELECT COUNT(*) FROM user_tab_columns WHERE column_name = \'NEXTVAL\'',
            'SELECT COUNT(*) FROM dual WHERE dual."DUMMY" = \'X\'',
            'SELECT 1 FROM dual WHERE "nextval_count" = 1 OR "Nextval" = 2',
        ):
            with self.subTest(query=query):
                api.validate_check_query(query)

    def test_division_is_allowed_in_a_check_but_a_sqlcl_slash_terminator_is_not(self) -> None:
        # A check reaches Oracle as hex through DBMS_SQL, never as a SQLcl line, so
        # '/' as the division operator is harmless; only the habitual trailing
        # '/' terminator is a mistake worth refusing.
        api = self.require_manifest()
        for query in (
            "SELECT 1/1 FROM dual",
            "SELECT 1 / 2 FROM dual",
            "SELECT CASE WHEN COUNT(*) / 2 = 1 THEN 1 ELSE 0 END FROM user_tables",
            "SELECT 4\n  / 2 FROM dual",
            "SELECT AVG(num_rows) / COALESCE(MAX(num_rows), 1) FROM user_tables",
            "SELECT (10 / 5) * (6 / 3) FROM dual",
        ):
            with self.subTest(query=query):
                api.validate_check_query(query)
        for query in (
            "SELECT 1 FROM dual\n/",
            "SELECT 1 FROM dual /",
            "SELECT 1 FROM dual;\n/",
            "/ SELECT 1 FROM dual",
            "SELECT 1 // 2 FROM dual",
            "SELECT 1 FROM dual\\",
        ):
            with self.subTest(query=query):
                with self.assertRaises(api.MigrationManifestError):
                    api.validate_check_query(query)

    def test_file_rename_and_checks_content_change_the_payload_digest(self) -> None:
        api = self.require_manifest()
        folder = self.add_folder("2026-09-27_create-customers-r001")
        initial = api.load_migration(self.root, str(folder.relative_to(self.root)))
        (folder / "001-create-table.sql").rename(folder / "001-create-customer-table.sql")
        renamed = api.load_migration(self.root, str(folder.relative_to(self.root)))
        self.assertNotEqual(initial.payload_digest, renamed.payload_digest)
        (folder / "checks.json").write_text((folder / "checks.json").read_text().replace("'T'", "'CUSTOMERS'"))
        checks_changed = api.load_migration(self.root, str(folder.relative_to(self.root)))
        self.assertNotEqual(renamed.payload_digest, checks_changed.payload_digest)

    def test_receipts_bind_target_and_payload_and_install_without_overwrite(self) -> None:
        api = self.require_manifest()
        folder = self.add_folder("2026-09-27_create-customers-r001")
        migration = api.load_migration(self.root, str(folder.relative_to(self.root)))
        target = {"environment": "dev", "schema": "APP", "session_user": "MIGRATOR", "db_unique_name": "DEVDB"}
        receipt = {
            "schemaVersion": 1,
            "state": "verified",
            "environment": "dev",
            "migration": folder.name,
            "createdDate": migration.date,
            "family": migration.family,
            "revision": migration.revision,
            "files": [{"name": item.name, "sequence": item.sequence, "sha256": item.sha256} for item in migration.files],
            "checksSha256": migration.checks_sha256,
            "payloadDigest": migration.payload_digest,
            "target": target,
            "applyStartedAt": "2026-09-27T10:00:00Z",
            "applyCompletedAt": "2026-09-27T10:01:00Z",
            "verifiedAt": "2026-09-27T10:02:00Z",
            "checks": [{"id": "table-exists", "passed": True, "rows": 1, "value": 1}],
            "verifier": "template-migration-v1",
            "normalization": "logical-v1",
        }
        path = folder / "status.dev.json"
        api.install_receipt(path, receipt)

        loaded = api.validate_receipt(path, migration, target)
        self.assertEqual(loaded, receipt)
        with self.assertRaises(api.MigrationManifestError):
            api.install_receipt(path, {**receipt, "payloadDigest": "different"})
        with self.assertRaises(api.MigrationManifestError):
            api.validate_receipt(path, migration, {**target, "schema": "OTHER"})

    def test_status_directory_is_environment_specific_and_no_status_is_invented(self) -> None:
        api = self.require_manifest()
        folder = self.add_folder("2026-09-27_create-customers-r001")
        self.assertFalse((folder / "status.dev.json").exists())
        migration = api.load_migration(self.root, str(folder.relative_to(self.root)))
        self.assertEqual(migration.family, "create-customers")
        self.assertEqual(migration.revision, 1)


if __name__ == "__main__":
    unittest.main()
