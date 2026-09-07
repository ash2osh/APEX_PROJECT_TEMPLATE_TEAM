from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from teamlib.release import ReleasePlan
from teamlib.release_adapter import ReleaseAdapterError, apply_verified_release


ROOT = Path(__file__).resolve().parents[2]


class ReleaseAdapterTests(unittest.TestCase):
    def test_production_contract_refuses_before_loading_environment_or_archive(self):
        plan = ReleasePlan("a" * 64, "b" * 64, (), "c" * 64, {"environment": "production"}, "d" * 64)
        with tempfile.TemporaryDirectory(prefix="team-release-adapter-") as directory:
            with self.assertRaisesRegex(ReleaseAdapterError, "production"):
                apply_verified_release(
                    Path(directory) / "missing.tar",
                    ROOT / "targets" / "production.json",
                    Path(directory) / "missing.env",
                    plan,
                    {},
                )


if __name__ == "__main__":
    unittest.main()
