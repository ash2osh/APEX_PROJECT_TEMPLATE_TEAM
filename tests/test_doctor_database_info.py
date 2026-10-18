"""Exercise doctor transcript verification through the real Bash/PowerShell wrappers."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import fake_sqlcl

ROOT = Path(__file__).resolve().parents[1]


class DoctorDatabaseInfoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "doctor project [space]"
        shutil.copytree(ROOT / "scripts", self.root / "scripts")
        self.config = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.env_file = self.root / ".env"
        self.env_file.write_text(self.config, encoding="utf-8")
        bin_path = Path(self.temporary.name) / "fake SQLcl"
        fake_sqlcl.install(bin_path, r'''if [ "${1:-}" = -V ]; then
  printf 'SQLcl: Release 26.3 Production\n'; exit 0
fi
printf 'SQLcl target: session_user=%s, current_schema=%s, db_name=DEV\n' "$8" "$6"
printf 'APEX_RELEASE_VERIFIED:26.2\nAPEX_WORKSPACE_USERS_VERIFIED\n'
if [ "$FAKE_DOCTOR_IDENTITY" = yes ]; then printf 'APEX_DOCTOR_VERIFIED:%s\n' "$8"; fi
if [ "$8" = REST_API ]; then
  printf 'DATABASE_VERSION_VERIFIED:19.0.0.0.0:19.0.0\n'
else
  printf '%s\n' "$FAKE_DATABASE_INFO"
fi
exit "$FAKE_DOCTOR_EXIT"
''')
        self.environment = fake_sqlcl.environment(
            bin_path, PROJECT_ENV_FILE=str(self.env_file),
            FAKE_DATABASE_INFO="DATABASE_VERSION_VERIFIED:23.26.0.0.0:23.0.0",
            FAKE_DOCTOR_IDENTITY="yes", FAKE_DOCTOR_EXIT="0",
        )

    def wrappers(self):
        yield [fake_sqlcl.BASH, str(self.root / "scripts/team.sh")]
        if shutil.which("pwsh"):
            yield [shutil.which("pwsh"), "-NoProfile", "-File", str(self.root / "scripts/team.ps1")]

    def run_doctor(self, wrapper):
        return subprocess.run(
            [*wrapper, "doctor"], cwd=self.root, env=self.environment,
            text=True, capture_output=True, check=False, timeout=60,
        )

    def test_detected_version_is_reported_per_target_without_configuration(self):
        # Reusing the first profile's version for a separate ORDS target is a bug.
        self.env_file.write_text(
            self.config + "\nORDS_SCHEMA=REST_API\nORDS_SQLCL_CONNECTION=rest-demo\nORDS_EXPECTED_USER=REST_API\n",
            encoding="utf-8",
        )
        for wrapper in self.wrappers():
            with self.subTest(wrapper=wrapper[0]):
                result = self.run_doctor(wrapper)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("DATABASE_VERSION_VERIFIED:23.26.0.0.0:23.0.0", result.stdout)
                self.assertIn("DATABASE_VERSION_VERIFIED:19.0.0.0.0:19.0.0", result.stdout)

    def test_missing_malformed_or_duplicate_detection_cannot_pass_doctor(self):
        # A successful SQLcl exit/identity marker alone must not certify detection.
        for value in ("", "DATABASE_VERSION_VERIFIED:23:unknown",
                      "DATABASE_VERSION_VERIFIED:23.0:",
                      "DATABASE_VERSION_VERIFIED:23.0:23.0\nDATABASE_VERSION_VERIFIED:19.0:19.0"):
            self.environment["FAKE_DATABASE_INFO"] = value
            for wrapper in self.wrappers():
                with self.subTest(value=value, wrapper=wrapper[0]):
                    result = self.run_doctor(wrapper)
                    self.assertNotEqual(0, result.returncode, result.stdout)
                    self.assertIn("database version/compatibility detection is unknown", result.stdout + result.stderr)

    def test_version_observation_does_not_replace_identity_verification(self):
        self.environment["FAKE_DOCTOR_IDENTITY"] = "no"
        for wrapper in self.wrappers():
            with self.subTest(wrapper=wrapper[0]):
                result = self.run_doctor(wrapper)
                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertIn("did not verify the doctor script", result.stdout + result.stderr)

    def test_sqlcl_failure_cannot_pass_with_valid_detection(self):
        self.environment["FAKE_DOCTOR_EXIT"] = "1"
        for wrapper in self.wrappers():
            with self.subTest(wrapper=wrapper[0]):
                result = self.run_doctor(wrapper)
                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertIn("SQLcl doctor check failed", result.stdout + result.stderr)
