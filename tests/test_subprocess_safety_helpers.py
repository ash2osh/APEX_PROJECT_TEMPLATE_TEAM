"""Tests for interrupt_process_group in _test_subprocess_safety: the settle delay and the POSIX-only guard."""

import os
import signal
import subprocess
import sys
import unittest
from unittest.mock import patch

import _test_subprocess_safety as safety
from _test_subprocess_safety import interrupt_process_group


class _FakeProcess:
    pid = 4242


class InterruptProcessGroupTests(unittest.TestCase):
    def _recorded_calls(self, process, **options):
        calls = []
        with patch.object(safety.time, "sleep", side_effect=lambda seconds: calls.append(("sleep", seconds))), \
                patch.object(safety.os, "killpg", side_effect=lambda pid, number: calls.append(("killpg", pid, number))):
            interrupt_process_group(process, **options)
        return calls

    @unittest.skipIf(os.name == "nt", "process groups and SIGINT to a group are POSIX only")
    def test_the_settle_delay_comes_before_the_group_interrupt(self):
        self.assertEqual(
            [("sleep", safety.INTERRUPT_SETTLE_SECONDS), ("killpg", 4242, signal.SIGINT)],
            self._recorded_calls(_FakeProcess()),
        )
        self.assertEqual(0.1, safety.INTERRUPT_SETTLE_SECONDS)

    @unittest.skipIf(os.name == "nt", "process groups and SIGINT to a group are POSIX only")
    def test_the_settle_can_be_chosen_by_the_caller(self):
        self.assertEqual(
            [("sleep", 0.25), ("killpg", 4242, signal.SIGINT)],
            self._recorded_calls(_FakeProcess(), settle=0.25),
        )

    @unittest.skipIf(os.name == "nt", "process groups and SIGINT to a group are POSIX only")
    def test_a_negative_settle_is_refused_before_anything_is_sent(self):
        with self.assertRaises(ValueError):
            self._recorded_calls(_FakeProcess(), settle=-1)

    def test_windows_is_refused_without_sending_anything(self):
        calls = []
        with patch.object(safety.os, "name", "nt"), \
                patch.object(safety.time, "sleep", side_effect=lambda seconds: calls.append(("sleep", seconds))), \
                patch.object(safety.os, "killpg", create=True,  # Windows has no os.killpg; this test also runs there
                             side_effect=lambda pid, number: calls.append(("killpg", pid, number))):
            with self.assertRaisesRegex(RuntimeError, "POSIX only"):
                interrupt_process_group(_FakeProcess())
        self.assertEqual([], calls)

    @unittest.skipIf(os.name == "nt", "process groups and SIGINT to a group are POSIX only")
    def test_a_real_child_group_is_stopped_by_the_interrupt(self):
        # The child restores the default SIGINT action, as a non-interactive background job's shell would expect,
        # so the interrupt ends it by signal and its exit status is -SIGINT.
        code = (
            "import signal, time\n"
            "signal.signal(signal.SIGINT, signal.SIG_DFL)\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n"
        )
        with subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        ) as process:
            try:
                self.assertEqual("ready\n", process.stdout.readline())
                interrupt_process_group(process)
                process.wait(timeout=30)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        self.assertEqual(-signal.SIGINT, process.returncode)


if __name__ == "__main__":
    unittest.main()
