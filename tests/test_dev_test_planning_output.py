"""Preserve JSON bytes across DBMS_OUTPUT chunks in DEV planning replies."""

from __future__ import annotations

import json
import unittest

from scripts.dev_test_target import _parse_planning_output


class PlanningOutputChunkTests(unittest.TestCase):
    def test_large_catalog_reassembles_chunks_without_inserting_characters(self):
        expected = {
            "existingObjects": [
                {"owner": "APP", "name": f"T_{index}", "type": "TABLE"}
                for index in range(250)
            ]
        }
        payload = json.dumps(expected)
        self.assertGreater(len(payload), 8000)
        chunks = [payload[index:index + 8000] for index in range(0, len(payload), 8000)]
        output = (
            "PLAN_PAYLOAD_BEGIN:plan\n" + "\n".join(chunks)
            + "\nPLAN_PAYLOAD_END:plan\nPLAN_VERIFIED:plan\n"
        )
        self.assertEqual(_parse_planning_output(output), expected)

    def test_windows_chunks_preserve_unicode_and_escaped_line_breaks(self):
        expected = {"name": "اسم_الجدول", "value": "first\nsecond\u2028third\u0085fourth", "count": 123456789}
        payload = json.dumps(expected, ensure_ascii=False)
        # Cut inside both strings and the numeric token, as DBMS_LOB.SUBSTR may.
        chunks = [payload[index:index + 7] for index in range(0, len(payload), 7)]
        output = (
            "PLAN_PAYLOAD_BEGIN:plan\r\n" + "\r\n".join(chunks)
            + "\r\nPLAN_PAYLOAD_END:plan\r\nPLAN_VERIFIED:plan\r\n"
        )
        self.assertEqual(_parse_planning_output(output), expected)


if __name__ == "__main__":
    unittest.main()
