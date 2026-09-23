from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from types import SimpleNamespace
import tempfile
import unittest

from teamlib.app_identity import (
    AppIdentity,
    AppIdentityError,
    observe_app_identity,
    require_apex_version,
    require_app_identity,
)
from teamlib.config import Target
from teamlib.sqlcl import SqlclError


class AppIdentityVersionTests(unittest.TestCase):
    def test_apex_versions_below_26_1_are_refused(self):
        for version in ("24.2", "24.2.0", "26.0", "26.0.1"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(AppIdentityError, "26.1"):
                    require_apex_version(version)

    def test_apex_versions_at_or_above_26_1_are_accepted(self):
        for version in ("26.1", "26.1.0", "26.1.4", "26.2", "27.1"):
            with self.subTest(version=version):
                require_apex_version(version)

    def test_malformed_and_unknown_versions_are_refused(self):
        for version in ("", None, "unknown", "invalid.version"):
            with self.subTest(version=version):
                with self.assertRaises(AppIdentityError):
                    require_apex_version(version)


class AppIdentityGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = Target(
            project="team", role="integration", environment="staging", connection="fake",
            instance_id="FREE", db_name="FREEPDB1", service="freep1", session_user="DEMO",
            current_schema="DEMO", alias="checkout", workspace_id=10, app_id=100,
            parsing_schema="APP_SCHEMA", ownership_mode="shared", binding_digest="a" * 64,
        )

    def test_present_matching_identity_is_accepted(self):
        observed = AppIdentity(
            status="PRESENT",
            app_id=100,
            workspace_id=10,
            parsing_schema="APP_SCHEMA",
            workspace_schemas=frozenset({"APP_SCHEMA", "OTHER_SCHEMA"}),
            apex_version="26.1.4",
        )
        require_app_identity(self.target, observed)

    def test_wrong_app_id_is_refused(self):
        observed = AppIdentity(
            status="PRESENT",
            app_id=999,
            workspace_id=10,
            parsing_schema="APP_SCHEMA",
            workspace_schemas=frozenset({"APP_SCHEMA"}),
            apex_version="26.1.4",
        )
        with self.assertRaisesRegex(AppIdentityError, "app ID"):
            require_app_identity(self.target, observed)

    def test_wrong_workspace_id_is_refused(self):
        observed = AppIdentity(
            status="PRESENT",
            app_id=100,
            workspace_id=99,
            parsing_schema="APP_SCHEMA",
            workspace_schemas=frozenset({"APP_SCHEMA"}),
            apex_version="26.1.4",
        )
        with self.assertRaisesRegex(AppIdentityError, "workspace ID"):
            require_app_identity(self.target, observed)

    def test_wrong_parsing_schema_is_refused(self):
        observed = AppIdentity(
            status="PRESENT",
            app_id=100,
            workspace_id=10,
            parsing_schema="WRONG_SCHEMA",
            workspace_schemas=frozenset({"WRONG_SCHEMA", "APP_SCHEMA"}),
            apex_version="26.1.4",
        )
        with self.assertRaisesRegex(AppIdentityError, "parsing schema"):
            require_app_identity(self.target, observed)

    def test_parsing_schema_not_assigned_to_workspace_is_refused(self):
        observed = AppIdentity(
            status="PRESENT",
            app_id=100,
            workspace_id=10,
            parsing_schema="APP_SCHEMA",
            workspace_schemas=frozenset({"OTHER_SCHEMA"}),
            apex_version="26.1.4",
        )
        with self.assertRaisesRegex(AppIdentityError, "not assigned to workspace"):
            require_app_identity(self.target, observed)

    def test_unknown_status_is_refused(self):
        observed = AppIdentity(
            status="UNKNOWN",
            app_id=None,
            workspace_id=None,
            parsing_schema=None,
            workspace_schemas=frozenset(),
            apex_version="26.1.4",
        )
        with self.assertRaisesRegex(AppIdentityError, "UNKNOWN"):
            require_app_identity(self.target, observed)

    def test_absent_app_refused_when_not_allowed(self):
        observed = AppIdentity(
            status="ABSENT",
            app_id=None,
            workspace_id=10,
            parsing_schema=None,
            workspace_schemas=frozenset({"APP_SCHEMA"}),
            apex_version="26.1.4",
        )
        with self.assertRaisesRegex(AppIdentityError, "absent"):
            require_app_identity(self.target, observed, allow_absent=False)

    def test_absent_app_allowed_for_first_deploy_with_verified_schema_assignment(self):
        observed = AppIdentity(
            status="ABSENT",
            app_id=None,
            workspace_id=10,
            parsing_schema=None,
            workspace_schemas=frozenset({"APP_SCHEMA"}),
            apex_version="26.1.4",
        )
        require_app_identity(self.target, observed, allow_absent=True)

    def test_absent_app_with_unassigned_schema_is_refused_even_when_allow_absent(self):
        observed = AppIdentity(
            status="ABSENT",
            app_id=None,
            workspace_id=10,
            parsing_schema=None,
            workspace_schemas=frozenset({"OTHER_SCHEMA"}),
            apex_version="26.1.4",
        )
        with self.assertRaisesRegex(AppIdentityError, "not assigned to workspace"):
            require_app_identity(self.target, observed, allow_absent=True)


class ObserveAppIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = Target(
            project="team", role="integration", environment="staging", connection="fake",
            instance_id="FREE", db_name="FREEPDB1", service="freep1", session_user="DEMO",
            current_schema="DEMO", alias="checkout", workspace_id=10, app_id=100,
            parsing_schema="APP_SCHEMA", ownership_mode="shared", binding_digest="a" * 64,
        )

    def test_observe_present_app(self):
        stdout = """\
TEAM_RESULT_BEGIN
TEAM_APP_ID_APEX_VERSION|26.1.4
TEAM_APP_ID_WS_SCHEMA|10|APP_SCHEMA
TEAM_APP_ID_APP|10|100|APP_SCHEMA
TEAM_RESULT_END
"""
        def runner(target, operation, driver, work, **kwargs):
            return SimpleNamespace(
                stdout=stdout, stderr="", exit_code=0,
                identity={"SESSION_USER": "DEMO", "CURRENT_SCHEMA": "DEMO", "DB_NAME": "FREEPDB1", "SERVICE": "freep1", "INSTANCE_ID": "FREE"},
                completion={"operation": operation}, result_manifest={"status": "success"},
            )

        observed = observe_app_identity(self.target, runner=runner)
        self.assertEqual(observed.status, "PRESENT")
        self.assertEqual(observed.app_id, 100)
        self.assertEqual(observed.workspace_id, 10)
        self.assertEqual(observed.parsing_schema, "APP_SCHEMA")
        self.assertEqual(observed.workspace_schemas, frozenset({"APP_SCHEMA"}))
        self.assertEqual(observed.apex_version, "26.1.4")

    def test_observe_absent_app_with_verified_workspace_schemas(self):
        stdout = """\
TEAM_RESULT_BEGIN
TEAM_APP_ID_APEX_VERSION|26.1.4
TEAM_APP_ID_WS_SCHEMA|10|APP_SCHEMA
TEAM_RESULT_END
"""
        def runner(target, operation, driver, work, **kwargs):
            return SimpleNamespace(
                stdout=stdout, stderr="", exit_code=0,
                identity={"SESSION_USER": "DEMO", "CURRENT_SCHEMA": "DEMO", "DB_NAME": "FREEPDB1", "SERVICE": "freep1", "INSTANCE_ID": "FREE"},
                completion={"operation": operation}, result_manifest={"status": "success"},
            )

        observed = observe_app_identity(self.target, runner=runner)
        self.assertEqual(observed.status, "ABSENT")
        self.assertEqual(observed.workspace_id, 10)
        self.assertEqual(observed.workspace_schemas, frozenset({"APP_SCHEMA"}))
        self.assertEqual(observed.apex_version, "26.1.4")

    def test_observe_no_row_and_unknown_assignment_is_unknown(self):
        stdout = """\
TEAM_RESULT_BEGIN
TEAM_APP_ID_APEX_VERSION|26.1.4
TEAM_RESULT_END
"""
        def runner(target, operation, driver, work, **kwargs):
            return SimpleNamespace(
                stdout=stdout, stderr="", exit_code=0,
                identity={"SESSION_USER": "DEMO", "CURRENT_SCHEMA": "DEMO", "DB_NAME": "FREEPDB1", "SERVICE": "freep1", "INSTANCE_ID": "FREE"},
                completion={"operation": operation}, result_manifest={"status": "success"},
            )

        observed = observe_app_identity(self.target, runner=runner)
        self.assertEqual(observed.status, "UNKNOWN")

    def test_observe_duplicate_rows_is_unknown(self):
        stdout = """\
TEAM_RESULT_BEGIN
TEAM_APP_ID_APEX_VERSION|26.1.4
TEAM_APP_ID_WS_SCHEMA|10|APP_SCHEMA
TEAM_APP_ID_APP|10|100|APP_SCHEMA
TEAM_APP_ID_APP|10|100|OTHER_SCHEMA
TEAM_RESULT_END
"""
        def runner(target, operation, driver, work, **kwargs):
            return SimpleNamespace(
                stdout=stdout, stderr="", exit_code=0,
                identity={"SESSION_USER": "DEMO", "CURRENT_SCHEMA": "DEMO", "DB_NAME": "FREEPDB1", "SERVICE": "freep1", "INSTANCE_ID": "FREE"},
                completion={"operation": operation}, result_manifest={"status": "success"},
            )

        observed = observe_app_identity(self.target, runner=runner)
        self.assertEqual(observed.status, "UNKNOWN")

    def test_observe_runner_error_is_unknown(self):
        def failing_runner(target, operation, driver, work, **kwargs):
            raise SqlclError("connection timed out")

        observed = observe_app_identity(self.target, runner=failing_runner)
        self.assertEqual(observed.status, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
