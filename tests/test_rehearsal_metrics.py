from __future__ import annotations

import json
import unittest

from scripts.db_targets import Target
from scripts.migration_manifest import RehearsalMetric
from scripts.rehearsal_metrics import IDENTITY_FIELDS, parse_metric_frame, render_metric_driver


class RehearsalMetricFrameTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = Target("dev", "docker-demo2", "DEMO2", "DEMO2", "development")
        self.identity = {
            "session_user": "DEMO2",
            "current_schema": "DEMO2",
            "db_name": "FREE",
            "db_unique_name": "FREEPDB1",
            "service_name": "freepdb1",
            "container_id": "3",
            "container_name": "FREEPDB1",
            "edition": "ORA$BASE",
            "database_version": "23.0",
        }
        self.metrics = (
            RehearsalMetric(
                "tagged-total",
                "SELECT COALESCE(SUM(AMOUNT), 0) FROM QUALIFIED_ROWS WHERE ID = 1",
                0,
                25,
                0,
            ),
        )

    def frame(self, phase: str, value: object = 25, *, identity: dict | None = None, metric_id: str = "tagged-total", result_overrides: dict | None = None) -> str:
        result = {
            "id": metric_id,
            "row_count": 1,
            "column_count": 1,
            "numeric": True,
            "value": value,
        }
        result.update(result_overrides or {})
        payload = {
            "schemaVersion": 1,
            "phase": phase,
            "complete": True,
            "identity": identity or self.identity,
            "results": [result],
        }
        return "\n".join((
            f"CHECK_PAYLOAD_BEGIN:{phase}",
            json.dumps(payload, separators=(",", ":")),
            f"CHECK_PAYLOAD_END:{phase}",
            f"CHECK_VERIFIED:{phase}",
        ))

    def test_render_metric_driver_uses_guarded_scalar_frame_without_printing_query_text(self):
        driver = render_metric_driver(self.target, self.metrics, "metric-before")

        self.assertIn("CHECK_PAYLOAD_BEGIN:metric-before", driver)
        self.assertIn("CHECK_VERIFIED:metric-before", driver)
        self.assertIn("session_user", driver)
        self.assertIn("current_schema", driver)
        self.assertNotIn(self.metrics[0].sql, driver)

    def test_parser_accepts_one_complete_integer_metric_with_exact_identity(self):
        capture = parse_metric_frame(
            self.frame("metric-after", 25),
            self.metrics,
            "metric-after",
            self.identity,
        )

        self.assertTrue(capture.complete)
        self.assertEqual(capture.identity, self.identity)
        self.assertEqual(capture.values, {"tagged-total": 25})
        self.assertEqual(capture.errors, ())

    def test_parser_normalizes_finite_integral_numeric_output(self):
        capture = parse_metric_frame(
            self.frame("metric-after", 25.0),
            self.metrics,
            "metric-after",
            self.identity,
        )

        self.assertTrue(capture.complete)
        self.assertEqual(capture.values, {"tagged-total": 25})

    def test_parser_rejects_missing_duplicate_truncated_or_out_of_order_frames(self):
        good = self.frame("metric-after")
        broken = (
            "",
            good.replace("CHECK_VERIFIED:metric-after", ""),
            good + "\n" + good,
            good.replace("CHECK_PAYLOAD_END:metric-after", "CHECK_PAYLOAD_BEGIN:metric-after"),
        )
        for output in broken:
            with self.subTest(output=output[:80]):
                capture = parse_metric_frame(output, self.metrics, "metric-after", self.identity)
                self.assertFalse(capture.complete)
                self.assertEqual(capture.values, {})
                self.assertTrue(capture.errors)

    def test_parser_rejects_wrong_identity_and_never_exposes_values(self):
        wrong_identity = {**self.identity, "db_unique_name": "OTHERDB"}
        capture = parse_metric_frame(
            self.frame("metric-after", identity=wrong_identity),
            self.metrics,
            "metric-after",
            self.identity,
        )

        self.assertFalse(capture.complete)
        self.assertEqual(capture.values, {})
        self.assertIn("differs from the selected target", " ".join(capture.errors))

    def test_parser_rejects_wrong_metric_order_and_non_scalar_results(self):
        wrong_id = parse_metric_frame(
            self.frame("metric-after", metric_id="other-total"),
            self.metrics,
            "metric-after",
            self.identity,
        )
        non_scalar = parse_metric_frame(
            self.frame("metric-after", 25, result_overrides={"row_count": 2}),
            self.metrics,
            "metric-after",
            self.identity,
        )

        for capture in (wrong_id, non_scalar):
            self.assertFalse(capture.complete)
            self.assertEqual(capture.values, {})
            self.assertTrue(capture.errors)

    def test_identity_contract_covers_database_service_container_schema_and_release(self):
        self.assertEqual(set(self.identity), set(IDENTITY_FIELDS))
        missing_field = dict(self.identity)
        del missing_field["container_id"]
        capture = parse_metric_frame(
            self.frame("metric-after", identity=missing_field),
            self.metrics,
            "metric-after",
            self.identity,
        )

        self.assertFalse(capture.complete)
        self.assertEqual(capture.values, {})


if __name__ == "__main__":
    unittest.main()
