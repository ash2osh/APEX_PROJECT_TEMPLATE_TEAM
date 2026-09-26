import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check_builder_drift.py"


class BuilderDriftTests(unittest.TestCase):
    def run_guard(self, *, exported_at: str | None, sql_output: str, sql_exit: str = "0"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app"
            app.mkdir()
            (app / "application.apx").write_text("app SAMPLE ()\n", encoding="utf-8")
            if exported_at is not None:
                (app / "apex-team-export.json").write_text(
                    json.dumps({"applicationId": 100, "exportedAt": exported_at}),
                    encoding="utf-8",
                )

            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_sql = fake_bin / "sql"
            fake_sql.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$FAKE_SQL_OUTPUT\"\n"
                "exit \"$FAKE_SQL_EXIT\"\n",
                encoding="utf-8",
            )
            fake_sql.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["FAKE_SQL_OUTPUT"] = sql_output
            environment["FAKE_SQL_EXIT"] = sql_exit
            return subprocess.run(
                [
                    "python3",
                    str(GUARD),
                    "100",
                    "docker-demo",
                    str(app),
                    "--expected-user",
                    "DEMO",
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_live_builder_update_after_export_is_refused(self) -> None:
        result = self.run_guard(
            exported_at="2026-09-26T08:00:00",
            sql_output="2026-09-26T09:00:00",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT DETECTED] Live APEX App 100", result.stdout)
        self.assertIn("2026-09-26T09:00:00", result.stdout)
        self.assertIn("scripts/team.sh export 100", result.stdout)

    def test_live_state_equal_to_or_older_than_export_is_clean(self) -> None:
        result = self.run_guard(
            exported_at="2026-09-26T09:00:00",
            sql_output="2026-09-26T08:00:00",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[DRIFT OK] No uncaptured Builder edits detected.", result.stdout)

    def test_missing_export_timestamp_fails_closed(self) -> None:
        result = self.run_guard(exported_at=None, sql_output="2026-09-26T09:00:00")
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT UNKNOWN]", result.stderr)
        self.assertIn("scripts/team.sh export 100", result.stderr)

    def test_unavailable_sqlcl_query_fails_closed(self) -> None:
        result = self.run_guard(
            exported_at="2026-09-26T08:00:00",
            sql_output="connection failed",
            sql_exit="1",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("[DRIFT UNKNOWN]", result.stderr)

    def test_application_not_yet_installed_is_not_builder_drift(self) -> None:
        result = self.run_guard(
            exported_at="2026-09-26T08:00:00",
            sql_output="NOT_FOUND",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("[DRIFT OK]", result.stdout)


if __name__ == "__main__":
    unittest.main()
