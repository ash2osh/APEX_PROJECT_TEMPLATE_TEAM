"""Contract tests for scripts/dev_test_manifest.py (Task 1).

These tests are written first, before the implementation, and are expected to
fail with ImportError/AttributeError until scripts/dev_test_manifest.py exists.

Conventions follow the existing test suite: _no_real_sqlcl is imported to
prevent any accidental real SQLcl execution, and tests use tempfile.
TemporaryDirectory for isolation.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _no_real_sqlcl  # noqa: F401  keep tests away from a real SQLcl

try:
    from scripts import dev_test_manifest as dtm
except ImportError:
    dtm = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write(root: Path, rel: str, text: str = "-- sql\n") -> Path:
    """Write a file relative to root, creating parent dirs."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")
    return p


def _suite_json(root: Path, data: object) -> Path:
    """Write suite JSON to root/suite.json and return the path."""
    p = root / "suite.json"
    p.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    return p


# ---------------------------------------------------------------------------
# Minimal spec example from section 6
# ---------------------------------------------------------------------------

SPEC_EXAMPLE = {
    "schemaVersion": 1,
    "id": "example-dev-fixture",
    "scenarios": [
        {
            "id": "fixture-insert",
            "description": "Create a small run-owned table and retain two synthetic rows.",
            "dependsOn": [],
            "requirements": {"objects": [], "checks": []},
            "effects": {
                "writes": True,
                "transaction": "mixed",
                "external": "none",
                "affectedObjects": ["@fixtures"],
                "backgroundConsumers": [],
            },
            "fixtures": {
                "objects": [
                    {"binding": "items", "suffix": "ITEMS", "type": "TABLE"}
                ],
                "selectors": [],
            },
            "steps": [
                {
                    "id": "create-and-seed",
                    "sqlFile": "sql/create-and-seed.sql",
                    "expected": {"kind": "success"},
                }
            ],
            "checks": [
                {
                    "id": "two-retained-rows",
                    "sql": "SELECT COUNT(*) FROM ${object.items} WHERE RUN_TAG = :DEVTEST_RUN_ID",
                    "expected": 2,
                }
            ],
            "observations": [],
            "cleanup": None,
        }
    ],
}


class TestLoadSpecExample(unittest.TestCase):
    """Test that the exact spec section-6 example loads correctly."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _write(self.root, "sql/create-and-seed.sql")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def require(self):
        self.assertIsNotNone(dtm, "scripts.dev_test_manifest is not implemented yet")
        return dtm

    def test_loads_spec_example(self) -> None:
        api = self.require()
        p = _suite_json(self.root, SPEC_EXAMPLE)
        suite = api.load_suite(p)
        # schemaVersion == 1
        self.assertEqual(suite.schemaVersion, 1)
        # exactly one scenario
        self.assertEqual(len(suite.scenarios), 1)
        sc = suite.scenarios[0]
        # cleanup is None
        self.assertIsNone(sc.cleanup)
        # check expected is integer 2
        self.assertEqual(sc.checks[0]["expected"], 2)
        self.assertIsInstance(sc.checks[0]["expected"], int)
        self.assertNotIsInstance(sc.checks[0]["expected"], bool)
        # suite root recorded correctly
        self.assertEqual(suite.root, self.root)
        # suite id
        self.assertEqual(suite.id, "example-dev-fixture")

    def test_suite_and_scenario_are_frozen(self) -> None:
        api = self.require()
        p = _suite_json(self.root, SPEC_EXAMPLE)
        suite = api.load_suite(p)
        with self.assertRaises((AttributeError, TypeError)):
            suite.id = "mutated"  # type: ignore[misc]
        with self.assertRaises((AttributeError, TypeError)):
            suite.scenarios[0].id = "mutated"  # type: ignore[misc]

    def test_step_carries_path_and_sha256(self) -> None:
        api = self.require()
        p = _suite_json(self.root, SPEC_EXAMPLE)
        suite = api.load_suite(p)
        step = suite.scenarios[0].steps[0]
        # path must be an absolute path within suite root
        step_path = step["path"]
        self.assertIsInstance(step_path, Path)
        self.assertTrue(step_path.is_absolute())
        # sha256 is a 64-char hex string
        self.assertRegex(step["sha256"], r"^[0-9a-f]{64}$")

    def test_error_class_is_valueerror_subclass(self) -> None:
        api = self.require()
        self.assertTrue(issubclass(api.DevTestManifestError, ValueError))

    def test_error_has_code(self) -> None:
        api = self.require()
        err = api.DevTestManifestError("TEST_CODE", "some message")
        self.assertEqual(err.code, "TEST_CODE")


# ---------------------------------------------------------------------------
# Rejection table cases
# ---------------------------------------------------------------------------

class TestRejectionCases(unittest.TestCase):
    """All spec-required rejection cases."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _write(self.root, "sql/create-and-seed.sql")
        _write(self.root, "sql/cleanup.sql")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def require(self):
        self.assertIsNotNone(dtm, "scripts.dev_test_manifest is not implemented yet")
        return dtm

    def load(self, data: object):
        api = self.require()
        p = _suite_json(self.root, data)
        return api.load_suite(p)

    def assertRefuses(self, data: object, *, msg: str = "") -> dtm.DevTestManifestError:
        api = self.require()
        with self.assertRaises(api.DevTestManifestError, msg=msg or str(data)) as ctx:
            self.load(data)
        return ctx.exception

    def _minimal_scenario(self, **overrides) -> dict:
        sc = {
            "id": "fixture-insert",
            "description": "A scenario.",
            "dependsOn": [],
            "requirements": {"objects": [], "checks": []},
            "effects": {
                "writes": False,
                "transaction": "read-only",
                "external": "none",
                "affectedObjects": [],
                "backgroundConsumers": [],
            },
            "fixtures": {"objects": [], "selectors": []},
            "steps": [
                {
                    "id": "step-one",
                    "sqlFile": "sql/create-and-seed.sql",
                    "expected": {"kind": "success"},
                }
            ],
            "checks": [{"id": "row-count", "sql": "SELECT 1 FROM DUAL", "expected": 1}],
            "observations": [],
            "cleanup": None,
        }
        sc.update(overrides)
        return sc

    def _minimal_suite(self, scenarios=None, **overrides) -> dict:
        d = {
            "schemaVersion": 1,
            "id": "test-suite",
            "scenarios": scenarios if scenarios is not None else [self._minimal_scenario()],
        }
        d.update(overrides)
        return d

    # -- schemaVersion --

    def test_rejects_schema_version_boolean_true(self) -> None:
        self.assertRefuses(self._minimal_suite(schemaVersion=True),
                           msg="boolean True schemaVersion must be refused")

    def test_rejects_schema_version_boolean_false(self) -> None:
        self.assertRefuses(self._minimal_suite(schemaVersion=False),
                           msg="boolean False schemaVersion must be refused")

    def test_rejects_schema_version_string(self) -> None:
        self.assertRefuses(self._minimal_suite(schemaVersion="1"),
                           msg="string schemaVersion must be refused")

    def test_rejects_schema_version_wrong_integer(self) -> None:
        self.assertRefuses(self._minimal_suite(schemaVersion=2),
                           msg="schemaVersion != 1 must be refused")

    def test_rejects_schema_version_null(self) -> None:
        self.assertRefuses(self._minimal_suite(schemaVersion=None))

    # -- root-level unknown fields --

    def test_rejects_unknown_root_field(self) -> None:
        d = self._minimal_suite()
        d["extra"] = "unexpected"
        self.assertRefuses(d, msg="unknown root field must be refused")

    # -- suite id --

    def test_rejects_suite_id_uppercase(self) -> None:
        self.assertRefuses(self._minimal_suite(id="BadID"))

    def test_rejects_suite_id_starting_with_digit(self) -> None:
        self.assertRefuses(self._minimal_suite(id="1bad"))

    def test_rejects_suite_id_too_long(self) -> None:
        # max 64 chars
        self.assertRefuses(self._minimal_suite(id="a" * 65))

    def test_rejects_suite_id_with_underscore(self) -> None:
        self.assertRefuses(self._minimal_suite(id="bad_id"))

    # -- scenarios: duplicate IDs --

    def test_rejects_duplicate_scenario_ids(self) -> None:
        s1 = self._minimal_scenario(id="scenario-a")
        s2 = self._minimal_scenario(id="scenario-a")
        self.assertRefuses(self._minimal_suite(scenarios=[s1, s2]),
                           msg="duplicate scenario IDs must be refused")

    def test_rejects_empty_scenarios(self) -> None:
        self.assertRefuses(self._minimal_suite(scenarios=[]),
                           msg="empty scenarios list must be refused")

    # -- scenario unknown fields --

    def test_rejects_unknown_scenario_field(self) -> None:
        sc = self._minimal_scenario()
        sc["extra"] = "unexpected"
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    # -- checks: missing (empty list) --

    def test_rejects_empty_checks(self) -> None:
        sc = self._minimal_scenario(checks=[])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]),
                           msg="empty checks list must be refused")

    # -- checks: boolean expected --

    def test_rejects_check_expected_boolean_true(self) -> None:
        sc = self._minimal_scenario(checks=[{"id": "c1", "sql": "SELECT 1 FROM DUAL", "expected": True}])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_check_expected_boolean_false(self) -> None:
        sc = self._minimal_scenario(checks=[{"id": "c1", "sql": "SELECT 1 FROM DUAL", "expected": False}])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_check_expected_string(self) -> None:
        sc = self._minimal_scenario(checks=[{"id": "c1", "sql": "SELECT 1 FROM DUAL", "expected": "1"}])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    # -- duplicate check IDs --

    def test_rejects_duplicate_check_ids(self) -> None:
        sc = self._minimal_scenario(checks=[
            {"id": "c1", "sql": "SELECT 1 FROM DUAL", "expected": 1},
            {"id": "c1", "sql": "SELECT 2 FROM DUAL", "expected": 2},
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    # -- duplicate step IDs --

    def test_rejects_duplicate_step_ids(self) -> None:
        sc = self._minimal_scenario(steps=[
            {"id": "step-one", "sqlFile": "sql/create-and-seed.sql", "expected": {"kind": "success"}},
            {"id": "step-one", "sqlFile": "sql/create-and-seed.sql", "expected": {"kind": "success"}},
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    # -- observations: maxRows limits --

    def test_rejects_observation_max_rows_zero(self) -> None:
        sc = self._minimal_scenario(observations=[
            {"id": "obs1", "sql": "SELECT 1 FROM DUAL", "maxRows": 0}
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_observation_max_rows_101(self) -> None:
        sc = self._minimal_scenario(observations=[
            {"id": "obs1", "sql": "SELECT 1 FROM DUAL", "maxRows": 101}
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_observation_max_rows_boolean(self) -> None:
        sc = self._minimal_scenario(observations=[
            {"id": "obs1", "sql": "SELECT 1 FROM DUAL", "maxRows": True}
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_accepts_observation_max_rows_1(self) -> None:
        sc = self._minimal_scenario(observations=[
            {"id": "obs1", "sql": "SELECT 1 FROM DUAL", "maxRows": 1}
        ])
        suite = self.load(self._minimal_suite(scenarios=[sc]))
        self.assertEqual(suite.scenarios[0].observations[0]["maxRows"], 1)

    def test_accepts_observation_max_rows_100(self) -> None:
        sc = self._minimal_scenario(observations=[
            {"id": "obs1", "sql": "SELECT 1 FROM DUAL", "maxRows": 100}
        ])
        suite = self.load(self._minimal_suite(scenarios=[sc]))
        self.assertEqual(suite.scenarios[0].observations[0]["maxRows"], 100)

    # -- effects: unsupported enums --

    def test_rejects_unsupported_transaction_enum(self) -> None:
        effects = {
            "writes": False,
            "transaction": "autocommit",  # not in allowed set
            "external": "none",
            "affectedObjects": [],
            "backgroundConsumers": [],
        }
        sc = self._minimal_scenario(effects=effects)
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_unsupported_external_enum(self) -> None:
        effects = {
            "writes": False,
            "transaction": "read-only",
            "external": "maybe",  # not in allowed set
            "affectedObjects": [],
            "backgroundConsumers": [],
        }
        sc = self._minimal_scenario(effects=effects)
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_unknown_effects_field(self) -> None:
        effects = {
            "writes": False,
            "transaction": "read-only",
            "external": "none",
            "affectedObjects": [],
            "backgroundConsumers": [],
            "extra": "unexpected",
        }
        sc = self._minimal_scenario(effects=effects)
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    # -- effects: writes must be bool --

    def test_rejects_effects_writes_not_bool(self) -> None:
        effects = {
            "writes": 1,  # integer, not bool
            "transaction": "read-only",
            "external": "none",
            "affectedObjects": [],
            "backgroundConsumers": [],
        }
        sc = self._minimal_scenario(effects=effects)
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    # -- requirements: unknown fields --

    def test_rejects_unknown_requirements_field(self) -> None:
        reqs = {"objects": [], "checks": [], "extra": "unexpected"}
        sc = self._minimal_scenario(requirements=reqs)
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    # -- requirements: checks boolean expected --

    def test_rejects_requirement_check_expected_boolean(self) -> None:
        reqs = {
            "objects": [],
            "checks": [{"id": "c1", "sql": "SELECT 1 FROM DUAL", "expected": True}],
        }
        sc = self._minimal_scenario(requirements=reqs)
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    # -- fixtures: unsupported object types --

    def test_rejects_unsupported_fixture_object_type(self) -> None:
        fixtures = {
            "objects": [{"binding": "items", "suffix": "ITEMS", "type": "TRIGGER"}],
            "selectors": [],
        }
        sc = self._minimal_scenario(fixtures=fixtures)
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_unknown_fixtures_field(self) -> None:
        fixtures = {
            "objects": [],
            "selectors": [],
            "extra": "unexpected",
        }
        sc = self._minimal_scenario(fixtures=fixtures)
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    # -- fixtures: duplicate binding names --

    def test_rejects_duplicate_fixture_binding_names(self) -> None:
        fixtures = {
            "objects": [
                {"binding": "items", "suffix": "ITEMS", "type": "TABLE"},
                {"binding": "items", "suffix": "ITEMS2", "type": "TABLE"},
            ],
            "selectors": [],
        }
        sc = self._minimal_scenario(fixtures=fixtures)
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_duplicate_selector_ids(self) -> None:
        fixtures = {
            "objects": [],
            "selectors": [
                {"id": "sel1", "sql": "SELECT 1 ID FROM DUAL", "columns": [{"name": "ID", "binding": "DEVTEST_ID", "type": "integer"}]},
                {"id": "sel1", "sql": "SELECT 2 ID FROM DUAL", "columns": [{"name": "ID", "binding": "DEVTEST_ID2", "type": "integer"}]},
            ],
        }
        sc = self._minimal_scenario(fixtures=fixtures)
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_duplicate_selector_column_binding(self) -> None:
        fixtures = {
            "objects": [],
            "selectors": [
                {"id": "sel1", "sql": "SELECT 1 A FROM DUAL", "columns": [
                    {"name": "A", "binding": "DEVTEST_X", "type": "integer"},
                ]},
                {"id": "sel2", "sql": "SELECT 2 B FROM DUAL", "columns": [
                    {"name": "B", "binding": "DEVTEST_X", "type": "integer"},  # duplicate binding
                ]},
            ],
        }
        sc = self._minimal_scenario(fixtures=fixtures)
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_selector_column_type_not_integer_or_string(self) -> None:
        fixtures = {
            "objects": [],
            "selectors": [
                {"id": "sel1", "sql": "SELECT 1 ID FROM DUAL", "columns": [
                    {"name": "ID", "binding": "DEVTEST_ID", "type": "float"}
                ]},
            ],
        }
        sc = self._minimal_scenario(fixtures=fixtures)
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    # -- steps: expected kind --

    def test_accepts_expected_oracle_error(self) -> None:
        sc = self._minimal_scenario(steps=[
            {
                "id": "failing-step",
                "sqlFile": "sql/create-and-seed.sql",
                "expected": {"kind": "oracle-error", "code": "ORA-00942"},
            }
        ])
        suite = self.load(self._minimal_suite(scenarios=[sc]))
        self.assertEqual(suite.scenarios[0].steps[0]["expected"]["kind"], "oracle-error")

    def test_rejects_oracle_error_missing_code(self) -> None:
        sc = self._minimal_scenario(steps=[
            {
                "id": "failing-step",
                "sqlFile": "sql/create-and-seed.sql",
                "expected": {"kind": "oracle-error"},
            }
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_oracle_error_malformed_code(self) -> None:
        sc = self._minimal_scenario(steps=[
            {
                "id": "failing-step",
                "sqlFile": "sql/create-and-seed.sql",
                "expected": {"kind": "oracle-error", "code": "12345"},
            }
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_unknown_step_expected_kind(self) -> None:
        sc = self._minimal_scenario(steps=[
            {
                "id": "step-one",
                "sqlFile": "sql/create-and-seed.sql",
                "expected": {"kind": "timeout"},
            }
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_unknown_step_field(self) -> None:
        sc = self._minimal_scenario(steps=[
            {
                "id": "step-one",
                "sqlFile": "sql/create-and-seed.sql",
                "expected": {"kind": "success"},
                "extra": "unexpected",
            }
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    # -- missing SQL files --

    def test_rejects_missing_sql_file(self) -> None:
        sc = self._minimal_scenario(steps=[
            {
                "id": "step-one",
                "sqlFile": "sql/does-not-exist.sql",
                "expected": {"kind": "success"},
            }
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    # -- absolute / drive / UNC / traversal paths --

    def test_rejects_absolute_unix_path(self) -> None:
        sc = self._minimal_scenario(steps=[
            {"id": "step-one", "sqlFile": "/etc/passwd", "expected": {"kind": "success"}}
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_drive_letter_path_backslash(self) -> None:
        # Windows-style drive path — tested on Linux too per spec
        sc = self._minimal_scenario(steps=[
            {"id": "step-one", "sqlFile": "C:\\test.sql", "expected": {"kind": "success"}}
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_drive_letter_path_forward_slash(self) -> None:
        sc = self._minimal_scenario(steps=[
            {"id": "step-one", "sqlFile": "C:/test.sql", "expected": {"kind": "success"}}
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_drive_letter_relative(self) -> None:
        # C:test.sql — Windows relative-on-drive
        sc = self._minimal_scenario(steps=[
            {"id": "step-one", "sqlFile": "C:test.sql", "expected": {"kind": "success"}}
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_unc_path_backslash(self) -> None:
        sc = self._minimal_scenario(steps=[
            {"id": "step-one", "sqlFile": "\\\\server\\share\\test.sql", "expected": {"kind": "success"}}
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_unc_path_forward_slash(self) -> None:
        sc = self._minimal_scenario(steps=[
            {"id": "step-one", "sqlFile": "//server/share/test.sql", "expected": {"kind": "success"}}
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_traversal_dotdot(self) -> None:
        sc = self._minimal_scenario(steps=[
            {"id": "step-one", "sqlFile": "../test.sql", "expected": {"kind": "success"}}
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_traversal_nested(self) -> None:
        sc = self._minimal_scenario(steps=[
            {"id": "step-one", "sqlFile": "sub/../../test.sql", "expected": {"kind": "success"}}
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_symlink_escape(self) -> None:
        # Create a symlink inside root that points outside root
        outside = Path(self.tmp.name).parent / "outside.sql"
        outside.write_text("-- outside\n", encoding="utf-8")
        link = self.root / "sql" / "escape.sql"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks not supported on this platform")
        sc = self._minimal_scenario(steps=[
            {"id": "step-one", "sqlFile": "sql/escape.sql", "expected": {"kind": "success"}}
        ])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    # -- dependencies --

    def test_rejects_forward_dependency(self) -> None:
        # scenario A depends on scenario B which appears AFTER it
        sa = self._minimal_scenario(id="scenario-a", dependsOn=["scenario-b"])
        sb = self._minimal_scenario(id="scenario-b", steps=[
            {"id": "step-one", "sqlFile": "sql/create-and-seed.sql", "expected": {"kind": "success"}}
        ])
        # Reload sa with a unique step to avoid step-id collision
        sa["steps"] = [{"id": "step-a", "sqlFile": "sql/create-and-seed.sql", "expected": {"kind": "success"}}]
        self.assertRefuses(self._minimal_suite(scenarios=[sa, sb]),
                           msg="forward dependencies must be refused")

    def test_rejects_self_dependency(self) -> None:
        sc = self._minimal_scenario(id="scenario-a", dependsOn=["scenario-a"])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]),
                           msg="self-dependency must be refused")

    def test_rejects_nonexistent_dependency(self) -> None:
        sc = self._minimal_scenario(id="scenario-a", dependsOn=["no-such-scenario"])
        self.assertRefuses(self._minimal_suite(scenarios=[sc]),
                           msg="dependency on nonexistent scenario must be refused")

    def test_rejects_cyclic_dependency(self) -> None:
        # A → B and B → A
        sa = {**self._minimal_scenario(id="scenario-a"), "dependsOn": ["scenario-b"]}
        sb = {**self._minimal_scenario(id="scenario-b"), "dependsOn": ["scenario-a"]}
        sa["steps"] = [{"id": "step-a", "sqlFile": "sql/create-and-seed.sql", "expected": {"kind": "success"}}]
        sb["steps"] = [{"id": "step-b", "sqlFile": "sql/create-and-seed.sql", "expected": {"kind": "success"}}]
        self.assertRefuses(self._minimal_suite(scenarios=[sa, sb]),
                           msg="cyclic dependency must be refused")

    # -- duplicate JSON keys --

    def test_rejects_duplicate_json_keys_in_file(self) -> None:
        api = self.require()
        # Manually write a JSON file with duplicate keys
        raw = '{"schemaVersion": 1, "id": "test-suite", "id": "other", "scenarios": []}'
        p = self.root / "suite.json"
        p.write_text(raw, encoding="utf-8", newline="\n")
        with self.assertRaises(api.DevTestManifestError):
            api.load_suite(p)

    # -- cleanup validation --

    def test_accepts_cleanup_null(self) -> None:
        sc = self._minimal_scenario(cleanup=None)
        suite = self.load(self._minimal_suite(scenarios=[sc]))
        self.assertIsNone(suite.scenarios[0].cleanup)

    def test_rejects_cleanup_empty_sql_files(self) -> None:
        sc = self._minimal_scenario(cleanup={
            "sqlFiles": [],
            "checks": [{"id": "c1", "sql": "SELECT 1 FROM DUAL", "expected": 0}],
        })
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_cleanup_empty_checks(self) -> None:
        sc = self._minimal_scenario(cleanup={
            "sqlFiles": ["sql/cleanup.sql"],
            "checks": [],
        })
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_rejects_cleanup_missing_sql_file(self) -> None:
        sc = self._minimal_scenario(cleanup={
            "sqlFiles": ["sql/does-not-exist-cleanup.sql"],
            "checks": [{"id": "c1", "sql": "SELECT 0 FROM DUAL", "expected": 0}],
        })
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_accepts_valid_cleanup(self) -> None:
        sc = self._minimal_scenario(cleanup={
            "sqlFiles": ["sql/cleanup.sql"],
            "checks": [{"id": "c1", "sql": "SELECT 1 FROM DUAL", "expected": 0}],
        })
        suite = self.load(self._minimal_suite(scenarios=[sc]))
        self.assertIsNotNone(suite.scenarios[0].cleanup)
        self.assertIn("sqlFiles", suite.scenarios[0].cleanup)

    # -- requirements object entries --

    def test_rejects_requirements_object_invalid_status(self) -> None:
        reqs = {
            "objects": [
                {"owner": "@selected", "name": "MY_TABLE", "type": "TABLE",
                 "requiredStatus": "INVALID", "member": None}
            ],
            "checks": [],
        }
        sc = self._minimal_scenario(requirements=reqs)
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))

    def test_accepts_requirements_object_valid_status(self) -> None:
        for status in ("VALID", "ANY"):
            with self.subTest(status=status):
                reqs = {
                    "objects": [
                        {"owner": "@selected", "name": "MY_TABLE", "type": "TABLE",
                         "requiredStatus": status, "member": None}
                    ],
                    "checks": [],
                }
                sc = self._minimal_scenario(requirements=reqs)
                suite = self.load(self._minimal_suite(scenarios=[sc]))
                self.assertEqual(
                    suite.scenarios[0].requirements["objects"][0]["requiredStatus"], status
                )

    def test_rejects_requirements_object_unknown_field(self) -> None:
        reqs = {
            "objects": [
                {"owner": "@selected", "name": "MY_TABLE", "type": "TABLE",
                 "requiredStatus": "VALID", "member": None, "extra": "unexpected"}
            ],
            "checks": [],
        }
        sc = self._minimal_scenario(requirements=reqs)
        self.assertRefuses(self._minimal_suite(scenarios=[sc]))


# ---------------------------------------------------------------------------
# Selection tests
# ---------------------------------------------------------------------------

class TestSelectScenarios(unittest.TestCase):
    """Test select_scenarios refuses missing dependencies and preserves order."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        _write(self.root, "sql/step-a.sql")
        _write(self.root, "sql/step-b.sql")
        _write(self.root, "sql/step-c.sql")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def require(self):
        self.assertIsNotNone(dtm, "scripts.dev_test_manifest is not implemented yet")
        return dtm

    def _make_suite(self) -> dtm.Suite:
        api = self.require()
        data = {
            "schemaVersion": 1,
            "id": "order-test",
            "scenarios": [
                {
                    "id": "scenario-a",
                    "description": "A",
                    "dependsOn": [],
                    "requirements": {"objects": [], "checks": []},
                    "effects": {
                        "writes": False,
                        "transaction": "read-only",
                        "external": "none",
                        "affectedObjects": [],
                        "backgroundConsumers": [],
                    },
                    "fixtures": {"objects": [], "selectors": []},
                    "steps": [
                        {"id": "step-a", "sqlFile": "sql/step-a.sql", "expected": {"kind": "success"}}
                    ],
                    "checks": [{"id": "check-a", "sql": "SELECT 1 FROM DUAL", "expected": 1}],
                    "observations": [],
                    "cleanup": None,
                },
                {
                    "id": "scenario-b",
                    "description": "B depends on A",
                    "dependsOn": ["scenario-a"],
                    "requirements": {"objects": [], "checks": []},
                    "effects": {
                        "writes": False,
                        "transaction": "read-only",
                        "external": "none",
                        "affectedObjects": [],
                        "backgroundConsumers": [],
                    },
                    "fixtures": {"objects": [], "selectors": []},
                    "steps": [
                        {"id": "step-b", "sqlFile": "sql/step-b.sql", "expected": {"kind": "success"}}
                    ],
                    "checks": [{"id": "check-b", "sql": "SELECT 1 FROM DUAL", "expected": 1}],
                    "observations": [],
                    "cleanup": None,
                },
                {
                    "id": "scenario-c",
                    "description": "C independent",
                    "dependsOn": [],
                    "requirements": {"objects": [], "checks": []},
                    "effects": {
                        "writes": False,
                        "transaction": "read-only",
                        "external": "none",
                        "affectedObjects": [],
                        "backgroundConsumers": [],
                    },
                    "fixtures": {"objects": [], "selectors": []},
                    "steps": [
                        {"id": "step-c", "sqlFile": "sql/step-c.sql", "expected": {"kind": "success"}}
                    ],
                    "checks": [{"id": "check-c", "sql": "SELECT 1 FROM DUAL", "expected": 1}],
                    "observations": [],
                    "cleanup": None,
                },
            ],
        }
        p = self.root / "suite.json"
        p.write_text(json.dumps(data), encoding="utf-8", newline="\n")
        return api.load_suite(p)

    def test_selection_returns_all_when_no_ids_given(self) -> None:
        api = self.require()
        suite = self._make_suite()
        result = api.select_scenarios(suite, [])
        self.assertEqual(len(result), 3)
        self.assertEqual(tuple(s.id for s in result), ("scenario-a", "scenario-b", "scenario-c"))

    def test_selection_preserves_manifest_order(self) -> None:
        api = self.require()
        suite = self._make_suite()
        # Request B and A in reversed order — should return manifest order (A, B)
        result = api.select_scenarios(suite, ["scenario-b", "scenario-a"])
        self.assertEqual(tuple(s.id for s in result), ("scenario-a", "scenario-b"))

    def test_selection_single_independent(self) -> None:
        api = self.require()
        suite = self._make_suite()
        result = api.select_scenarios(suite, ["scenario-c"])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "scenario-c")

    def test_selection_never_adds_project_writes(self) -> None:
        """Selecting B without its required dependency A must be refused."""
        api = self.require()
        suite = self._make_suite()
        with self.assertRaises(api.DevTestManifestError) as ctx:
            api.select_scenarios(suite, ["scenario-b"])
        # must not silently widen: A must not be added automatically
        self.assertIn("MISSING_DEPENDENCY", ctx.exception.code)

    def test_selection_rejects_unknown_id(self) -> None:
        api = self.require()
        suite = self._make_suite()
        with self.assertRaises(api.DevTestManifestError):
            api.select_scenarios(suite, ["no-such-scenario"])

    def test_selection_with_explicit_dep_included(self) -> None:
        api = self.require()
        suite = self._make_suite()
        # B depends on A; selecting both is valid
        result = api.select_scenarios(suite, ["scenario-a", "scenario-b"])
        self.assertEqual(tuple(s.id for s in result), ("scenario-a", "scenario-b"))


if __name__ == "__main__":
    unittest.main()
