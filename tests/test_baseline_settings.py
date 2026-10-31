"""Execute generated compiler-setting checks against catalog-shaped rows."""

import sqlite3
import unittest

from scripts import baseline


class BaselineSettingsChecks(unittest.TestCase):
    def test_generated_settings_query_accepts_exact_row_and_rejects_every_mismatch(self):
        settings = {
            "plsql_optimize_level": "2", "plsql_code_type": "INTERPRETED",
            "plsql_debug": "FALSE", "plsql_warnings": "ENABLE:ALL",
            "nls_length_semantics": "BYTE", "plscope_settings": "IDENTIFIERS:NONE",
            "plsql_ccflags": None,
        }
        columns = ["OWNER", "NAME", "TYPE", *(key.upper() for key in settings)]
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE TABLE DUAL (DUMMY TEXT)")
            connection.execute("INSERT INTO DUAL VALUES ('X')")
            connection.execute("CREATE TABLE ALL_PLSQL_OBJECT_SETTINGS (" + ", ".join(columns) + ")")
            values = ["APP_STAGE", "PKG_ITEMS", "PACKAGE", *settings.values()]
            connection.execute(
                "INSERT INTO ALL_PLSQL_OBJECT_SETTINGS VALUES (" + ", ".join("?" for _ in values) + ")", values,
            )
            for flags in (None, "REVIEW:TRUE"):
                with self.subTest(flags=flags):
                    settings["plsql_ccflags"] = flags
                    connection.execute("UPDATE ALL_PLSQL_OBJECT_SETTINGS SET PLSQL_CCFLAGS = ?", (flags,))
                    query = baseline._exact_settings_check(settings, "PKG_ITEMS", "PACKAGE", "APP_STAGE")["sql"]
                    self.assertEqual(connection.execute(query).fetchone()[0], 1)
                    for column in columns:
                        with self.subTest(column=column):
                            original = connection.execute(f"SELECT {column} FROM ALL_PLSQL_OBJECT_SETTINGS").fetchone()[0]
                            connection.execute(f"UPDATE ALL_PLSQL_OBJECT_SETTINGS SET {column} = ?", ("MISMATCH",))
                            self.assertEqual(connection.execute(query).fetchone()[0], 0)
                            connection.execute(f"UPDATE ALL_PLSQL_OBJECT_SETTINGS SET {column} = ?", (original,))
                    connection.execute("DELETE FROM ALL_PLSQL_OBJECT_SETTINGS")
                    self.assertEqual(connection.execute(query).fetchone()[0], 0)
                    connection.execute(
                        "INSERT INTO ALL_PLSQL_OBJECT_SETTINGS VALUES (" + ", ".join("?" for _ in values) + ")", values,
                    )

    def test_empty_ccflags_matches_the_null_flags_oracle_stores_for_it(self):
        # ALTER SESSION SET PLSQL_CCFLAGS = '' leaves ALL_PLSQL_OBJECT_SETTINGS.PLSQL_CCFLAGS
        # NULL, and '' = NULL is never true, so an empty expectation must test IS NULL.
        settings = {
            "plsql_optimize_level": "2", "plsql_code_type": "INTERPRETED",
            "plsql_debug": "FALSE", "plsql_warnings": "DISABLE:ALL",
            "nls_length_semantics": "BYTE", "plscope_settings": "IDENTIFIERS:NONE",
            "plsql_ccflags": "",
        }
        columns = ["OWNER", "NAME", "TYPE", *(key.upper() for key in settings)]
        stored = [None if key == "plsql_ccflags" else value for key, value in settings.items()]
        values = ["APP_STAGE", "PKG_ITEMS", "PACKAGE", *stored]
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE TABLE DUAL (DUMMY TEXT)")
            connection.execute("INSERT INTO DUAL VALUES ('X')")
            connection.execute("CREATE TABLE ALL_PLSQL_OBJECT_SETTINGS (" + ", ".join(columns) + ")")
            connection.execute(
                "INSERT INTO ALL_PLSQL_OBJECT_SETTINGS VALUES (" + ", ".join("?" for _ in values) + ")", values,
            )
            query = baseline._exact_settings_check(settings, "PKG_ITEMS", "PACKAGE", "APP_STAGE")["sql"]
            self.assertEqual(connection.execute(query).fetchone()[0], 1)
            connection.execute("UPDATE ALL_PLSQL_OBJECT_SETTINGS SET PLSQL_CCFLAGS = ?", ("REVIEW:TRUE",))
            self.assertEqual(connection.execute(query).fetchone()[0], 0)
