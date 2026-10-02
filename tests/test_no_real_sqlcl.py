import os
import shutil
import subprocess
import unittest

import _no_real_sqlcl  # noqa: F401  (installs the guard)


class NoRealSqlclTests(unittest.TestCase):
    def test_a_test_cannot_reach_a_real_sqlcl(self) -> None:
        found = shutil.which("sql")
        self.assertIsNotNone(found)
        # The first `sql` on PATH is the guard, in a folder this module created.
        self.assertIn("no-real-sqlcl-", found)
        if os.name == "nt":
            # CreateProcess starts sql.exe: a stand-in that rejects SQLcl's arguments.
            result = subprocess.run(["sql", "-S", "-noupdates", "-name", "x"], capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("SQLcl", result.stdout + result.stderr)
            return
        result = subprocess.run([found], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 99)
        self.assertIn("reached a real SQLcl", result.stderr)

    def test_the_directory_of_a_real_sqlcl_stays_on_path_on_windows(self) -> None:
        if os.name != "nt":
            self.skipTest("Windows only: other tools may share that directory")
        real = [entry for entry in os.environ["PATH"].split(os.pathsep) if os.path.isfile(os.path.join(entry, "sql.exe"))
                and "no-real-sqlcl-" not in entry]
        if not real:
            self.skipTest("no real SQLcl on this machine's PATH")
        # Still there (shadowed, not removed) and behind the guard.
        parts = os.environ["PATH"].split(os.pathsep)
        self.assertLess(next(i for i, entry in enumerate(parts) if "no-real-sqlcl-" in entry), parts.index(real[0]))

    def test_a_fake_placed_in_front_still_wins(self) -> None:
        import tempfile
        from pathlib import Path

        import fake_sqlcl

        with tempfile.TemporaryDirectory() as temporary:
            fake_sqlcl.install(Path(temporary), "#!/bin/sh\necho fake-ran\n")
            environment = fake_sqlcl.environment(Path(temporary))
            # CreateProcess resolves a bare name against the parent's PATH on Windows.
            program = shutil.which("sql", path=environment["PATH"])
            result = subprocess.run([program], capture_output=True, text=True, env=environment, stdin=subprocess.DEVNULL, check=False)
        self.assertEqual(result.stdout.strip(), "fake-ran")


if __name__ == "__main__":
    unittest.main()
