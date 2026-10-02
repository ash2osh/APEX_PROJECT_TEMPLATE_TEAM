import os
import shutil
import subprocess
import unittest

import _no_real_sqlcl  # noqa: F401  (installs the guard)


class NoRealSqlclTests(unittest.TestCase):
    def test_a_test_cannot_reach_a_real_sqlcl(self) -> None:
        found = shutil.which("sql")
        if os.name == "nt":
            # Windows: every directory that holds a real SQLcl is off PATH.
            self.assertIsNone(found)
            return
        # POSIX: the first `sql` on PATH is the guard, which refuses loudly.
        self.assertIsNotNone(found)
        result = subprocess.run([found], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 99)
        self.assertIn("reached a real SQLcl", result.stderr)

    def test_a_fake_placed_in_front_still_wins(self) -> None:
        import tempfile
        from pathlib import Path

        if os.name == "nt":
            self.skipTest("the tests' fake sql is a POSIX shell script")
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "sql"
            fake.write_text("#!/bin/sh\necho fake-ran\n", encoding="utf-8")
            fake.chmod(0o755)
            environment = dict(os.environ, PATH=f"{temporary}{os.pathsep}{os.environ['PATH']}")
            result = subprocess.run(["sql"], capture_output=True, text=True, env=environment, check=False)
        self.assertEqual(result.stdout.strip(), "fake-ran")


if __name__ == "__main__":
    unittest.main()
