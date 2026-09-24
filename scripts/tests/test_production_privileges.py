from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import team
from teamlib.config import load_config, profile_target
from teamlib.production_privileges import (
    ProductionPrivilegeError,
    audit_production_profile,
    parse_production_privileges,
)


class ProductionPrivilegeParserTests(unittest.TestCase):
    def test_read_only_system_and_object_grants_are_accepted(self):
        report = parse_production_privileges(
            "TEAM_PRIV|SYSTEM|CREATE SESSION\n"
            "TEAM_PRIV|SYSTEM|READ ANY TABLE\n"
            "TEAM_PRIV|ROLE|REPORTING_READ\n"
            "TEAM_PRIV|OBJECT|SELECT|VIEW\n"
            "TEAM_PRIV|COLUMN|READ\n"
        )
        self.assertEqual(report.system_privileges, ("CREATE SESSION", "READ ANY TABLE"))
        self.assertEqual(report.roles, ("REPORTING_READ",))
        self.assertEqual(report.object_privileges, ("READ", "SELECT"))
        self.assertEqual(report.owned_objects, ())

    def test_granted_roles_must_all_be_enabled_to_be_audited(self):
        base = "TEAM_PRIV|SYSTEM|CREATE SESSION\nTEAM_PRIV|ROLE|REPORTING_READ\n"
        report = parse_production_privileges(base + "TEAM_PRIV|GRANTED|REPORTING_READ\n")
        self.assertEqual(report.roles, ("REPORTING_READ",))
        # A non-default role is missing from SESSION_ROLES; SET ROLE could enable it later.
        with self.assertRaisesRegex(ProductionPrivilegeError, "not enabled in this session.*DATA_WRITER"):
            parse_production_privileges(base + "TEAM_PRIV|GRANTED|REPORTING_READ\nTEAM_PRIV|GRANTED|DATA_WRITER\n")

    def test_oracle_public_grants_on_oracle_objects_are_the_baseline(self):
        # Every real account receives these from PUBLIC; schema inventory reads
        # need EXECUTE on DBMS_METADATA, DBMS_LOB and UTL_ENCODE.
        report = parse_production_privileges(
            "TEAM_PRIV|SYSTEM|CREATE SESSION\n"
            "TEAM_PRIV|OBJECT|EXECUTE|PACKAGE|PUBLIC|Y|DBMS_METADATA|N\n"
            "TEAM_PRIV|OBJECT|EXECUTE|PACKAGE|PUBLIC|Y|DBMS_LOB|N\n"
            "TEAM_PRIV|OBJECT|EXECUTE|PACKAGE|PUBLIC|Y|UTL_ENCODE|N\n"
            "TEAM_PRIV|OBJECT|SELECT|VIEW|PUBLIC|Y|ALL_OBJECTS|N\n"
            "TEAM_PRIV|OBJECT|INSERT|TABLE|PUBLIC|Y|PLAN_TABLE$|Y\n"
            "TEAM_PRIV|OBJECT|SELECT|VIEW|REPORTER|N|SALES_V|N\n"
        )
        self.assertEqual(report.public_oracle_grants, 5)
        self.assertEqual(report.object_privileges, ("SELECT",))

    def test_public_side_effect_grants_on_oracle_objects_are_refused(self):
        for row in (
            "TEAM_PRIV|OBJECT|EXECUTE|PACKAGE|PUBLIC|Y|DBMS_JOB|N",
            "TEAM_PRIV|OBJECT|EXECUTE|PACKAGE|PUBLIC|Y|UTL_HTTP|N",
            "TEAM_PRIV|OBJECT|EXECUTE|PACKAGE|PUBLIC|Y|DBMS_PIPE|N",
            "TEAM_PRIV|OBJECT|INSERT|TABLE|PUBLIC|Y|AUD$|N",
            "TEAM_PRIV|OBJECT|ALTER|TABLE|PUBLIC|Y|PLAN_TABLE$|Y",
            "TEAM_PRIV|OBJECT|SELECT|SEQUENCE|PUBLIC|Y|SOME_SEQ|N",
        ):
            with self.subTest(row=row), self.assertRaisesRegex(ProductionPrivilegeError, "PUBLIC"):
                parse_production_privileges(f"TEAM_PRIV|SYSTEM|CREATE SESSION\n{row}\n")

    def test_public_grants_on_application_objects_are_judged(self):
        for row in (
            "TEAM_PRIV|OBJECT|UPDATE|TABLE|PUBLIC|N|ORDERS|N",
            "TEAM_PRIV|OBJECT|EXECUTE|PACKAGE|PUBLIC|N|APP_API|N",
            "TEAM_PRIV|COLUMN|INSERT",
        ):
            with self.subTest(row=row), self.assertRaises(ProductionPrivilegeError):
                parse_production_privileges(f"TEAM_PRIV|SYSTEM|CREATE SESSION\n{row}\n")

    def test_direct_or_role_grants_on_oracle_objects_are_still_judged(self):
        # Only PUBLIC's Oracle baseline is exempt; a grant made to this account
        # or one of its roles is a deliberate choice and must be read-only.
        with self.assertRaisesRegex(ProductionPrivilegeError, "EXECUTE"):
            parse_production_privileges(
                "TEAM_PRIV|SYSTEM|CREATE SESSION\n"
                "TEAM_PRIV|OBJECT|EXECUTE|PACKAGE|EXECUTE_CATALOG_ROLE|Y|DBMS_METADATA|N\n"
            )

    def test_malformed_grantee_columns_fail_closed(self):
        for row in (
            "TEAM_PRIV|OBJECT|SELECT|VIEW|PUBLIC|maybe|ALL_OBJECTS|N",
            "TEAM_PRIV|OBJECT|SELECT|VIEW||Y|ALL_OBJECTS|N",
            "TEAM_PRIV|OBJECT|SELECT|VIEW|PUBLIC|Y||N",
            "TEAM_PRIV|OBJECT|SELECT|VIEW|PUBLIC|Y|ALL_OBJECTS",
            "TEAM_PRIV|OBJECT|SELECT|VIEW|PUBLIC|Y",
        ):
            with self.subTest(row=row), self.assertRaises(ProductionPrivilegeError):
                parse_production_privileges(f"TEAM_PRIV|SYSTEM|CREATE SESSION\n{row}\n")

    def test_write_system_privilege_is_refused(self):
        with self.assertRaisesRegex(ProductionPrivilegeError, "CREATE ANY TABLE"):
            parse_production_privileges(
                "TEAM_PRIV|SYSTEM|CREATE SESSION\nTEAM_PRIV|SYSTEM|CREATE ANY TABLE\n"
            )

    def test_write_object_privilege_is_refused(self):
        with self.assertRaisesRegex(ProductionPrivilegeError, "UPDATE"):
            parse_production_privileges(
                "TEAM_PRIV|SYSTEM|CREATE SESSION\nTEAM_PRIV|OBJECT|UPDATE|TABLE\n"
            )

    def test_execute_object_privilege_is_refused(self):
        with self.assertRaisesRegex(ProductionPrivilegeError, "EXECUTE"):
            parse_production_privileges(
                "TEAM_PRIV|SYSTEM|CREATE SESSION\nTEAM_PRIV|OBJECT|EXECUTE|PROCEDURE\n"
            )

    def test_sequence_privilege_is_refused_even_when_named_select(self):
        with self.assertRaisesRegex(ProductionPrivilegeError, "SEQUENCE"):
            parse_production_privileges(
                "TEAM_PRIV|SYSTEM|CREATE SESSION\nTEAM_PRIV|OBJECT|SELECT|SEQUENCE\n"
            )

    def test_select_any_sequence_is_refused_as_effectful(self):
        with self.assertRaisesRegex(ProductionPrivilegeError, "SELECT ANY SEQUENCE"):
            parse_production_privileges(
                "TEAM_PRIV|SYSTEM|CREATE SESSION\nTEAM_PRIV|SYSTEM|SELECT ANY SEQUENCE\n"
            )

    def test_account_owned_objects_are_refused(self):
        with self.assertRaisesRegex(ProductionPrivilegeError, "owned object"):
            parse_production_privileges(
                "TEAM_PRIV|SYSTEM|CREATE SESSION\nTEAM_PRIV|OWNED|TABLE\n"
            )

    def test_missing_create_session_or_missing_query_output_fails_closed(self):
        for output in (
            "TEAM_PRIV|SYSTEM|READ ANY TABLE\n",
            "TEAM_PRIV|UNRECOGNIZED|value\n",
            "ordinary SQLcl output only\n",
        ):
            with self.subTest(output=output), self.assertRaises(ProductionPrivilegeError):
                parse_production_privileges(output)

    def test_audit_only_accepts_production_targets_and_uses_read_sql(self):
        config = self._production_config()
        target = profile_target(config, "METADATA")
        calls = []

        def runner(got_target, operation, driver, work):
            calls.append((got_target, operation, Path(driver).read_text(encoding="utf-8")))
            return type("Result", (), {"stdout": "TEAM_PRIV|SYSTEM|CREATE SESSION\n"})()

        report = audit_production_profile(
            target,
            repo=Path(__file__).resolve().parents[2],
            runner=runner,
        )
        self.assertEqual(report.system_privileges, ("CREATE SESSION",))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "read")
        self.assertIn("SESSION_PRIVS", calls[0][2])
        self.assertIn("SESSION_ROLES", calls[0][2])

        with self.assertRaisesRegex(ProductionPrivilegeError, "production target"):
            audit_production_profile(
                profile_target(self._production_config(environment="test"), "METADATA"),
                repo=Path(__file__).resolve().parents[2],
                runner=runner,
            )

    def _production_config(self, environment="production"):
        lines = [
            "PROJECT_NAME=team-template",
            f"TARGET_ROLE={'production' if environment == 'production' else 'test'}",
            f"DB_ENVIRONMENT={environment}",
            "APEX_APPS=checkout:101:APP_CODE",
            "TABLES_SCHEMA=APP_DATA",
            "CODE_SCHEMA=APP_CODE",
            "METADATA_SCHEMA=APP_META",
            "APP_OWNERSHIP_MODE=shared",
            "APEX_WORKSPACE_ID=5402650006222933",
        ]
        for profile in ("TABLES", "CODE", "APEX", "METADATA"):
            lines.extend(
                (
                    f"{profile}_SQLCL_CONNECTION={profile.lower()}-profile",
                    f"{profile}_EXPECTED_USER=APP_{profile}",
                    f"{profile}_EXPECTED_CURRENT_SCHEMA=APP_{profile}",
                    f"{profile}_EXPECTED_DB_NAME={'PRODPDB1' if environment == 'production' else 'TESTPDB1'}",
                    f"{profile}_EXPECTED_SERVICE={'prod1' if environment == 'production' else 'test1'}",
                    f"{profile}_EXPECTED_INSTANCE_ID={'PRODPDB1' if environment == 'production' else 'TESTPDB1'}",
                )
            )
        with tempfile.TemporaryDirectory(prefix="team-prod-priv-config-") as directory:
            path = Path(directory) / ".env"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            # load_config consumes the path immediately; the returned Config is
            # self-contained, so the temporary file can be removed here.
            config = load_config(path)
        return config


class ProductionDoctorPrivilegeTests(unittest.TestCase):
    def test_production_doctor_audits_each_configured_profile(self):
        env = [
            "PROJECT_NAME=team-template",
            "TARGET_ROLE=production",
            "DB_ENVIRONMENT=production",
            "APEX_APPS=checkout:101:APP_CODE",
            "TABLES_SCHEMA=APP_DATA",
            "CODE_SCHEMA=APP_CODE",
            "METADATA_SCHEMA=APP_META",
            "APP_OWNERSHIP_MODE=shared",
            "APEX_WORKSPACE_ID=5402650006222933",
        ]
        for profile in ("TABLES", "CODE", "APEX", "METADATA"):
            env.extend(
                (
                    f"{profile}_SQLCL_CONNECTION={profile.lower()}-profile",
                    f"{profile}_EXPECTED_USER=APP_{profile}",
                    f"{profile}_EXPECTED_CURRENT_SCHEMA=APP_{profile}",
                    f"{profile}_EXPECTED_DB_NAME=PRODPDB1",
                    f"{profile}_EXPECTED_SERVICE=prod1",
                    f"{profile}_EXPECTED_INSTANCE_ID=PRODPDB1",
                )
            )
        with tempfile.TemporaryDirectory(prefix="team-prod-doctor-") as directory:
            root = Path(directory)
            env_path = root / ".env"
            env_path.write_text("\n".join(env) + "\n", encoding="utf-8")
            audits = []

            def audit(target, *, repo):
                audits.append((target.connection, target.session_user))
                return type(
                    "Report",
                    (),
                    {
                        "system_privileges": ("CREATE SESSION",),
                        "roles": (),
                        "object_privileges": ("READ", "SELECT"),
                        "owned_objects": (),
                    },
                )()

            with patch("team.audit_production_profile", audit), patch("team._repo_root", return_value=root):
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = team.main(["--env", str(env_path), "doctor"])
        self.assertEqual(code, 0)
        self.assertEqual(len(audits), 4)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["production_privilege_audit"], "PASS")


if __name__ == "__main__":
    unittest.main()
