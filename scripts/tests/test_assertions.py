from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from types import SimpleNamespace
import tempfile
import unittest

from teamlib.assertions import (
    AssertionVerificationError,
    parse_team_assertions,
    run_verification_member,
)


class AssertionTests(unittest.TestCase):
    def test_all_unique_pass_rows_succeed(self):
        self.assertEqual(
            parse_team_assertions(
                "TEAM_ASSERT|table_exists|PASS\n"
                "TEAM_ASSERT|column_exists|PASS\n"
            ),
            ("table_exists", "column_exists"),
        )

    def test_fail_missing_malformed_and_duplicate_rows_refuse(self):
        cases = (
            ("TEAM_ASSERT|table_exists|FAIL\n", "failed assertions"),
            ("ordinary SQL output\n", "no TEAM_ASSERT rows"),
            ("TEAM_ASSERT|broken\n", "malformed TEAM_ASSERT row"),
            (
                "TEAM_ASSERT|same|PASS\nTEAM_ASSERT|same|PASS\n",
                "duplicate assertion",
            ),
        )
        for stdout, message in cases:
            with self.subTest(stdout=stdout):
                with self.assertRaisesRegex(AssertionVerificationError, message):
                    parse_team_assertions(stdout)

    def test_empty_member_is_deliberate_success_without_sqlcl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            member = root / "empty.verify.sql"
            member.write_bytes(b"")
            called = []
            result = run_verification_member(
                object(),
                member,
                root / "work",
                runner=lambda *args, **kwargs: called.append(args),
            )
        self.assertEqual(result, ())
        self.assertEqual(called, [])

    def test_nonempty_member_requires_framed_pass_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            member = root / "check.verify.sql"
            member.write_text(
                "SELECT 'TEAM_ASSERT|exists|FAIL' FROM dual;\n",
                encoding="utf-8",
            )
            runner = lambda *args, **kwargs: SimpleNamespace(
                stdout="TEAM_ASSERT|exists|FAIL\n"
            )
            with self.assertRaisesRegex(AssertionVerificationError, "exists"):
                run_verification_member(
                    object(), member, root / "work", runner=runner
                )


if __name__ == "__main__":
    unittest.main()
