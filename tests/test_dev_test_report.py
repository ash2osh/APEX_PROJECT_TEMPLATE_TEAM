"""Behavior contract tests for dev_test_report.py (Task 2).

Tests cover: validate_report, aggregate_exit, render_report, save_report,
and new_report builder. Written before the implementation (RED phase).

Run:
    python3 -m unittest discover -s tests -p 'test_dev_test_report.py' -v
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# Module import
# ---------------------------------------------------------------------------

import _no_real_sqlcl  # noqa: F401
from scripts.dev_test_report import new_report, validate_report, aggregate_exit, render_report, save_report
from scripts.dev_test_evidence import create_run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _observed():
    return {
        "session_user": "DEMO", "current_schema": "DEMO", "db_name": "DEV",
        "db_unique_name": "DEV", "service_name": "dev", "container_id": "3",
        "container_name": "DEV", "edition": "ORA$BASE", "database_version": "23.26",
    }


def _selected_coverage(report):
    report["coverage"].update(complete=True, eligible=True)
    if not report["selection"]["scenarioIds"]:
        report["selection"]["scenarioIds"] = [item["id"] for item in report["scenarios"]]


def _minimal_report(*, action: str = "run", scenario_ids: list[str] | None = None) -> dict:
    """Return a well-formed minimal report dict (no RunEvidence needed)."""
    return new_report(
        run=None,
        action=action,
        suite_id="example-suite",
        scenario_ids=scenario_ids or [],
        origin="builtin",
    )


def _scenario_record(
    id: str = "sc-1",
    status: str = "PASSED",
    eligible: bool = True,
) -> dict:
    return {
        "id": id,
        "status": status,
        "eligible": eligible,
        "steps": [],
        "checks": [],
        "observations": [],
        "effects": {},
    }


# ---------------------------------------------------------------------------
# 1. new_report — builder creates well-formed schema records
# ---------------------------------------------------------------------------

class TestNewReport(unittest.TestCase):
    """new_report produces consistent schema records."""


    def test_new_report_has_all_required_root_fields(self) -> None:
        """new_report includes all required root fields."""
        r = _minimal_report()
        required = {
            "schemaVersion", "runId", "action", "startedAt", "finishedAt",
            "source", "selection", "target", "prerequisites", "scenarios",
            "inventory", "cleanup", "diagnostics", "limitations", "coverage",
        }
        for field in required:
            self.assertIn(field, r, f"Missing root field: {field}")

    def test_schema_version_is_integer_one(self) -> None:
        r = _minimal_report()
        self.assertEqual(r["schemaVersion"], 1)
        self.assertIsInstance(r["schemaVersion"], int)
        self.assertNotIsInstance(r["schemaVersion"], bool)

    def test_action_values(self) -> None:
        """action field reflects the supplied action string."""
        self.assertEqual(_minimal_report(action="plan")["action"], "plan")
        self.assertEqual(_minimal_report(action="run")["action"], "run")

    def test_cleanup_initial_status_is_not_requested(self) -> None:
        """cleanup.status is NOT_REQUESTED initially."""
        r = _minimal_report()
        self.assertEqual(r["cleanup"]["status"], "NOT_REQUESTED")

    def test_selection_fields(self) -> None:
        """selection carries suiteId, scenarioIds, origin."""
        r = _minimal_report(scenario_ids=["s1", "s2"])
        sel = r["selection"]
        self.assertEqual(sel["suiteId"], "example-suite")
        self.assertIn("s1", sel["scenarioIds"])
        self.assertIn("s2", sel["scenarioIds"])
        self.assertEqual(sel["origin"], "builtin")

    def test_source_has_required_subfields(self) -> None:
        """source carries gitSha, dirty, inputHashes."""
        r = _minimal_report()
        src = r["source"]
        self.assertIn("gitSha", src)
        self.assertIn("dirty", src)
        self.assertIn("inputHashes", src)

    def test_target_has_required_subfields(self) -> None:
        """target carries configured, observed, versions."""
        r = _minimal_report()
        tgt = r["target"]
        self.assertIn("configured", tgt)
        self.assertIn("observed", tgt)
        self.assertIn("versions", tgt)

    def test_coverage_has_required_subfields(self) -> None:
        """coverage carries complete and eligible."""
        r = _minimal_report()
        cov = r["coverage"]
        self.assertIn("complete", cov)
        self.assertIn("eligible", cov)

    def test_inventory_has_objects_and_rows(self) -> None:
        """inventory carries objects and rows."""
        r = _minimal_report()
        inv = r["inventory"]
        self.assertIn("objects", inv)
        self.assertIn("rows", inv)

    def test_report_is_json_serializable(self) -> None:
        """new_report output is JSON-serializable."""
        r = _minimal_report()
        serialized = json.dumps(r)
        self.assertIsInstance(serialized, str)

    def test_report_roundtrips_through_json(self) -> None:
        """new_report dict survives JSON serialization/deserialization."""
        r = _minimal_report()
        restored = json.loads(json.dumps(r))
        self.assertEqual(restored["schemaVersion"], r["schemaVersion"])
        self.assertEqual(restored["action"], r["action"])


# ---------------------------------------------------------------------------
# 2. validate_report — accepts complete records, rejects incomplete/corrupt
# ---------------------------------------------------------------------------

class TestValidateReport(unittest.TestCase):
    """validate_report: accepts well-formed; rejects incomplete/corrupt."""


    def test_validates_well_formed_plan_report(self) -> None:
        """A complete plan report with no scenarios passes validation."""
        r = _minimal_report(action="plan")
        result = validate_report(r)
        self.assertIsInstance(result, dict)

    def test_validates_run_report_with_passed_scenarios(self) -> None:
        """A run report with all PASSED scenarios validates successfully."""
        r = _minimal_report(action="run")
        r["scenarios"] = [_scenario_record("sc-1", "PASSED")]
        result = validate_report(r)
        self.assertIsInstance(result, dict)

    def test_rejects_missing_schema_version(self) -> None:
        """validate_report raises if schemaVersion is absent."""
        r = _minimal_report()
        del r["schemaVersion"]
        with self.assertRaises((ValueError, KeyError, TypeError)):
            validate_report(r)

    def test_rejects_wrong_schema_version(self) -> None:
        """validate_report raises if schemaVersion != 1."""
        r = _minimal_report()
        r["schemaVersion"] = 2
        with self.assertRaises(ValueError):
            validate_report(r)

    def test_rejects_missing_run_id(self) -> None:
        r = _minimal_report()
        del r["runId"]
        with self.assertRaises((ValueError, KeyError)):
            validate_report(r)

    def test_rejects_missing_action(self) -> None:
        r = _minimal_report()
        del r["action"]
        with self.assertRaises((ValueError, KeyError)):
            validate_report(r)

    def test_rejects_invalid_scenario_status(self) -> None:
        """validate_report raises for unknown scenario status values."""
        r = _minimal_report()
        r["scenarios"] = [_scenario_record("s1", "WINNING")]  # not a valid state
        with self.assertRaises(ValueError):
            validate_report(r)

    def test_rejects_invalid_cleanup_status(self) -> None:
        """validate_report raises for unknown cleanup status values."""
        r = _minimal_report()
        r["cleanup"]["status"] = "MAYBE"
        with self.assertRaises(ValueError):
            validate_report(r)

    def test_rejects_truncated_json_loaded_as_partial_dict(self) -> None:
        """A dict missing required fields is rejected even if parseable."""
        partial = {"schemaVersion": 1}  # missing required fields
        with self.assertRaises((ValueError, KeyError)):
            validate_report(partial)

    def test_rejects_non_dict_input(self) -> None:
        with self.assertRaises((ValueError, TypeError, AttributeError)):
            validate_report("not a dict")  # type: ignore[arg-type]

    def test_rejects_none_input(self) -> None:
        with self.assertRaises((ValueError, TypeError, AttributeError)):
            validate_report(None)  # type: ignore[arg-type]

    def test_scenario_missing_required_fields_is_rejected(self) -> None:
        """A scenario record without 'id' is invalid."""
        r = _minimal_report()
        r["scenarios"] = [{"status": "PASSED"}]  # no id, no steps, etc.
        with self.assertRaises((ValueError, KeyError)):
            validate_report(r)


# ---------------------------------------------------------------------------
# 3. aggregate_exit — exact exit code contract
# ---------------------------------------------------------------------------

class TestAggregateExit(unittest.TestCase):
    """aggregate_exit returns the correct exit code per spec section 10."""


    def _report_with_scenarios(self, statuses: list[str], action: str = "run") -> dict:
        r = _minimal_report(action=action)
        r["finishedAt"] = "2026-10-16T12:00:00Z"
        r["target"]["observed"] = _observed()
        r["scenarios"] = [_scenario_record(f"sc-{i}", s) for i, s in enumerate(statuses)]
        return r

    def test_all_passed_returns_0(self) -> None:
        """All scenarios PASSED => exit 0."""
        r = self._report_with_scenarios(["PASSED", "PASSED"])
        _selected_coverage(r)
        self.assertEqual(aggregate_exit(r), 0)

    def test_at_least_one_failed_returns_1(self) -> None:
        """One FAILED scenario (no higher-priority conditions) => exit 1."""
        r = self._report_with_scenarios(["PASSED", "FAILED"])
        _selected_coverage(r)
        self.assertEqual(aggregate_exit(r), 1)

    def test_blocked_returns_2(self) -> None:
        """Any BLOCKED scenario => exit 2 (incomplete coverage)."""
        r = self._report_with_scenarios(["PASSED", "BLOCKED"])
        self.assertEqual(aggregate_exit(r), 2)

    def test_unknown_returns_2(self) -> None:
        """Any UNKNOWN scenario => exit 2."""
        r = self._report_with_scenarios(["PASSED", "UNKNOWN"])
        self.assertEqual(aggregate_exit(r), 2)

    def test_not_run_returns_2(self) -> None:
        """Any NOT_RUN scenario (when eligible) => exit 2."""
        r = _minimal_report(action="run")
        r["finishedAt"] = "2026-10-16T12:00:00Z"
        r["target"]["observed"] = _observed()
        r["scenarios"] = [
            _scenario_record("sc-1", "PASSED", eligible=True),
            _scenario_record("sc-2", "NOT_RUN", eligible=True),
        ]
        self.assertEqual(aggregate_exit(r), 2)

    def test_exit_2_takes_precedence_over_exit_1(self) -> None:
        """Exit 2 (BLOCKED) takes precedence over exit 1 (FAILED)."""
        r = self._report_with_scenarios(["FAILED", "BLOCKED"])
        self.assertEqual(aggregate_exit(r), 2)

    def test_plan_with_all_eligible_returns_0(self) -> None:
        """A plan report with all scenarios eligible and observed target returns exit 0."""
        r = _minimal_report(action="plan")
        r["finishedAt"] = "2026-10-16T12:00:00Z"
        r["target"]["observed"] = _observed()
        r["coverage"]["complete"] = True
        r["coverage"]["eligible"] = True
        _selected_coverage(r)
        self.assertEqual(aggregate_exit(r), 0)

    def test_empty_scenarios_run_returns_2(self) -> None:
        """A run with zero scenarios is incomplete coverage => exit 2."""
        r = _minimal_report(action="run")
        r["scenarios"] = []
        self.assertEqual(aggregate_exit(r), 2)

    def test_intent_without_outcome_is_unknown_exit_2(self) -> None:
        """test_intent_survives_missing_outcome: scenario with no result is UNKNOWN, exit 2."""
        r = _minimal_report(action="run")
        # Pre-write interrupt: scenario has an intent event but no outcome recorded
        r["scenarios"] = [{
            "id": "sc-1",
            "status": "UNKNOWN",
            "eligible": True,
            "steps": [],
            "checks": [],
            "observations": [],
            "effects": {},
        }]
        self.assertEqual(aggregate_exit(r), 2)

    def test_prewrite_interrupt_exit_130(self) -> None:
        """Interruption before any write may have started => exit 130."""
        r = _minimal_report(action="run")
        r["diagnostics"] = {"interrupted": True, "writeStarted": False}
        self.assertEqual(aggregate_exit(r), 130)

    def test_postwrite_interrupt_exit_2(self) -> None:
        """Interruption after a write may have started => exit 2 (UNKNOWN)."""
        r = _minimal_report(action="run")
        r["diagnostics"] = {"interrupted": True, "writeStarted": True}
        r["scenarios"] = [_scenario_record("sc-1", "UNKNOWN")]
        self.assertEqual(aggregate_exit(r), 2)

    def test_six_passes_not_requested_cleanup_returns_0(self) -> None:
        """test_report_retention_is_not_failure: 6 PASSED + NOT_REQUESTED cleanup => exit 0."""
        r = _minimal_report(action="run")
        r["finishedAt"] = "2026-10-16T12:00:00Z"
        r["target"]["observed"] = _observed()
        r["scenarios"] = [_scenario_record(f"sc-{i}", "PASSED") for i in range(6)]
        r["cleanup"]["status"] = "NOT_REQUESTED"
        _selected_coverage(r)
        self.assertEqual(aggregate_exit(r), 0)

    def test_cleanup_incomplete_returns_2(self) -> None:
        """INCOMPLETE cleanup => exit 2 (unsupported/incomplete cleanup)."""
        r = _minimal_report(action="run")
        r["scenarios"] = [_scenario_record("sc-1", "PASSED")]
        r["cleanup"]["status"] = "INCOMPLETE"
        self.assertEqual(aggregate_exit(r), 2)

    def test_cleanup_refused_returns_2(self) -> None:
        r = _minimal_report(action="run")
        r["scenarios"] = [_scenario_record("sc-1", "PASSED")]
        r["cleanup"]["status"] = "REFUSED"
        self.assertEqual(aggregate_exit(r), 2)

    def test_cleanup_does_not_rewrite_execution_results(self) -> None:
        """Successful cleanup (COMPLETE) with a FAILED scenario still returns 1."""
        r = _minimal_report(action="run")
        r["finishedAt"] = "2026-10-16T12:00:00Z"
        r["target"]["observed"] = _observed()
        r["scenarios"] = [_scenario_record("sc-1", "FAILED")]
        r["cleanup"]["status"] = "COMPLETE"
        # exit 1 for failure; cleanup does not promote to 0
        _selected_coverage(r)
        self.assertEqual(aggregate_exit(r), 1)

    def test_missing_intent_outcome_is_never_inferred_as_success(self) -> None:
        """A scenario with status UNKNOWN is never treated as PASSED."""
        r = _minimal_report(action="run")
        r["scenarios"] = [_scenario_record("sc-1", "UNKNOWN")]
        exit_code = aggregate_exit(r)
        self.assertNotEqual(exit_code, 0)
        self.assertNotEqual(exit_code, 1)
        self.assertEqual(exit_code, 2)


# ---------------------------------------------------------------------------
# 4. render_report — format parameter produces strings, not sensitive data
# ---------------------------------------------------------------------------

class TestRenderReport(unittest.TestCase):


    def test_render_text_returns_string(self) -> None:
        r = _minimal_report()
        result = render_report(r, "text")
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_render_json_returns_valid_json(self) -> None:
        r = _minimal_report()
        result = render_report(r, "json")
        parsed = json.loads(result)
        self.assertIsInstance(parsed, dict)

    def test_render_markdown_returns_string_with_markdown(self) -> None:
        r = _minimal_report()
        result = render_report(r, "markdown")
        self.assertIsInstance(result, str)
        # Must have some markdown structure (headers, etc.)
        self.assertTrue("#" in result or "**" in result or "|" in result)

    def test_render_unknown_format_raises(self) -> None:
        r = _minimal_report()
        with self.assertRaises((ValueError, KeyError)):
            render_report(r, "pdf")

    def test_render_text_does_not_include_sensitive_bindings(self) -> None:
        """Console/summary reports do not echo sensitive data."""
        r = _minimal_report()
        # Inject a fake "sensitive" field to ensure it doesn't leak
        r["source"]["inputHashes"] = {"secret.sql": "abc123"}
        result = render_report(r, "text")
        # The hash value should not appear verbatim in console output
        # (this is a basic sanity check; full redaction is documented as a limitation)
        self.assertNotIn("secret.sql", result)


# ---------------------------------------------------------------------------
# 5. save_report — writes to run directory
# ---------------------------------------------------------------------------

class TestSaveReport(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_report_creates_json_and_markdown(self) -> None:
        """save_report writes both report.json and report.md in the run directory."""
        run = create_run(Path(self.tmp))
        r = _minimal_report()
        r["runId"] = run.run_id
        save_report(run, r)
        run_dir = Path(self.tmp) / "scratch" / "dev-test" / run.run_id
        self.assertTrue((run_dir / "report.json").exists())
        self.assertTrue((run_dir / "report.md").exists())

    def test_saved_json_is_valid(self) -> None:
        """report.json is valid JSON."""
        run = create_run(Path(self.tmp))
        r = _minimal_report()
        r["runId"] = run.run_id
        save_report(run, r)
        run_dir = Path(self.tmp) / "scratch" / "dev-test" / run.run_id
        data = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(data["schemaVersion"], 1)

    def test_saved_markdown_is_nonempty(self) -> None:
        run = create_run(Path(self.tmp))
        r = _minimal_report()
        r["runId"] = run.run_id
        save_report(run, r)
        run_dir = Path(self.tmp) / "scratch" / "dev-test" / run.run_id
        md = (run_dir / "report.md").read_text(encoding="utf-8")
        self.assertGreater(len(md.strip()), 0)

    def test_save_report_is_atomic(self) -> None:
        """Saving a second report replaces the first atomically."""
        run = create_run(Path(self.tmp))
        r1 = _minimal_report(action="plan")
        r1["runId"] = run.run_id
        save_report(run, r1)
        r2 = _minimal_report(action="run")
        r2["runId"] = run.run_id
        save_report(run, r2)
        run_dir = Path(self.tmp) / "scratch" / "dev-test" / run.run_id
        data = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(data["runId"], run.run_id)
        self.assertEqual(data["action"], "run")


# ---------------------------------------------------------------------------
# 6. Deliberate retained INVALID fixture — valid report, not conflated
# ---------------------------------------------------------------------------

class TestDeliberateInvalidFixture(unittest.TestCase):
    """
    A report that intentionally records an INVALID application object
    (e.g. invalid-view scenario) must:
      - pass validate_report (it is a complete record)
      - NOT be conflated with an unexplained invalid application object
    """


    def test_invalid_view_scenario_passed_validates_ok(self) -> None:
        """PASSED scenario that detected its expected INVALID view validates ok."""
        r = _minimal_report(action="run")
        r["finishedAt"] = "2026-10-16T12:00:00Z"
        r["target"]["observed"] = _observed()
        # The invalid-view scenario passes by detecting the expected invalid view.
        r["scenarios"] = [{
            "id": "invalid-view",
            "status": "PASSED",  # scenario passed: it found the expected invalid state
            "eligible": True,
            "steps": [],
            "checks": [
                {
                    "id": "view-invalid",
                    "expected": 0,
                    "actual": 0,
                    "passed": True,
                    "sql": "SELECT COUNT(*) FROM ALL_OBJECTS WHERE STATUS='VALID' AND OBJECT_NAME=:N",
                }
            ],
            "observations": [
                {
                    "id": "invalid-status",
                    "rows": [{"OBJECT_NAME": "DT_XYZW_VW", "STATUS": "INVALID"}],
                    "truncated": False,
                }
            ],
            "effects": [
                {
                    "type": "deliberate-invalid-fixture",
                    "objectName": "DT_XYZW_VW",
                    "note": "intentionally retained for inspection",
                }
            ],
        }]
        # Must validate without error
        result = validate_report(r)
        self.assertIsInstance(result, dict)
        # Exit code should be 0 (all eligible scenarios passed)
        _selected_coverage(r)
        self.assertEqual(aggregate_exit(r), 0)


# ---------------------------------------------------------------------------
# 7. Completed report evidence enforcement and sanitization
# ---------------------------------------------------------------------------

class TestCompletedReportValidationAndExit(unittest.TestCase):
    """Missing executed scenarios, unobserved identity or missing finish evidence must fail."""


    def test_aggregate_exit_rejects_run_success_without_finished_at(self) -> None:
        """aggregate_exit returns 2 if finishedAt is None, even if coverage flags say complete."""
        r = _minimal_report(action="run")
        r["target"]["observed"] = _observed()
        r["finishedAt"] = None  # execution not finished
        r["scenarios"] = [_scenario_record("s1", "PASSED")]
        r["coverage"]["complete"] = True
        r["coverage"]["eligible"] = True
        self.assertEqual(aggregate_exit(r), 2)

    def test_aggregate_exit_rejects_run_success_without_observed_target(self) -> None:
        """aggregate_exit returns 2 if target.observed is None, even if all scenarios PASSED."""
        r = _minimal_report(action="run")
        r["finishedAt"] = "2026-10-16T12:00:00Z"
        r["target"]["observed"] = None  # target identity not verified
        r["scenarios"] = [_scenario_record("s1", "PASSED")]
        r["coverage"]["complete"] = True
        r["coverage"]["eligible"] = True
        self.assertEqual(aggregate_exit(r), 2)

    def test_aggregate_exit_rejects_plan_success_without_observed_target(self) -> None:
        """Plan action returns 2 if target.observed is None."""
        r = _minimal_report(action="plan")
        r["finishedAt"] = "2026-10-16T12:00:00Z"
        r["target"]["observed"] = None
        r["coverage"]["complete"] = True
        r["coverage"]["eligible"] = True
        self.assertEqual(aggregate_exit(r), 2)

    def test_aggregate_exit_rejects_plan_success_without_finished_at(self) -> None:
        """Plan action returns 2 if finishedAt is None."""
        r = _minimal_report(action="plan")
        r["finishedAt"] = None
        r["target"]["observed"] = _observed()
        r["coverage"]["complete"] = True
        r["coverage"]["eligible"] = True
        self.assertEqual(aggregate_exit(r), 2)

    def test_aggregate_exit_rejects_run_when_selected_scenarios_missing(self) -> None:
        """aggregate_exit returns 2 if selection has scenarioIds not present in executed scenarios."""
        r = _minimal_report(action="run", scenario_ids=["s1", "s2"])
        r["finishedAt"] = "2026-10-16T12:00:00Z"
        r["target"]["observed"] = _observed()
        r["scenarios"] = [_scenario_record("s1", "PASSED")]  # s2 is missing!
        r["coverage"]["complete"] = True
        r["coverage"]["eligible"] = True
        self.assertEqual(aggregate_exit(r), 2)

    def test_validate_completed_report_helper(self) -> None:
        """validate_completed_report rejects incomplete evidence."""
        from scripts import dev_test_report
        validate_fn = getattr(dev_test_report, "validate_completed_report", None)
        if validate_fn is None:
            self.fail("validate_completed_report helper not found in dev_test_report")

        r = _minimal_report(action="run")
        # Incomplete report missing finishedAt and observed target must raise
        with self.assertRaises(ValueError):
            validate_fn(r)

        # Once finishedAt and observed target are set, it passes
        r["finishedAt"] = "2026-10-16T12:00:00Z"
        r["target"]["observed"] = _observed()
        r["scenarios"] = [_scenario_record("s1", "PASSED")]
        _selected_coverage(r)
        self.assertIsInstance(validate_fn(r), dict)

    def test_render_report_scrubs_sensitive_business_data_and_credentials(self) -> None:
        """render_report text and markdown output never leaks passwords, tokens or sample rows."""
        r = _minimal_report(action="run")
        r["finishedAt"] = "2026-10-16T12:00:00Z"
        r["target"]["observed"] = _observed()
        r["inventory"]["rows"] = [{"CUSTOMER_SSN": "123-45-6789", "SALARY": 150000}]
        r["diagnostics"] = {"password": "topsecretpassword", "bearer_token": "token-xyz-123"}
        r["source"]["inputHashes"] = {"/home/user/secret.sql": "aabbccddeeff"}

        text_out = render_report(r, "text")
        md_out = render_report(r, "markdown")

        for secret in ("123-45-6789", "150000", "topsecretpassword", "token-xyz-123", "/home/user/secret.sql"):
            self.assertNotIn(secret, text_out, f"Secret leaked in text report: {secret}")
            self.assertNotIn(secret, md_out, f"Secret leaked in markdown report: {secret}")


if __name__ == "__main__":
    unittest.main()
