"""Subprocess safety rules used by the test harness."""

import os
import subprocess
import _test_subprocess_safety  # noqa: F401
import sys
import tempfile
import unittest
from pathlib import Path


class SubprocessDefaultsTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "an open inherited stdin pipe is tested on POSIX")
    def test_stdin_reader_finishes_when_test_harness_stdin_is_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            wrapper = Path(temporary) / "read_stdin.py"
            wrapper.write_text(
                "import sys\nsys.stdin.read()\nprint('finished')\n",
                encoding="utf-8",
            )
            pipe_read, pipe_write = os.pipe()
            saved_stdin = os.dup(0)
            try:
                os.dup2(pipe_read, 0)
                result = subprocess.run(
                    [sys.executable, str(wrapper)],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=3,
                )
            finally:
                os.dup2(saved_stdin, 0)
                os.close(saved_stdin)
                os.close(pipe_read)
                os.close(pipe_write)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "finished\n")
