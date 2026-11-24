import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

import _test_subprocess_safety  # noqa: F401
from scripts import terminal_confirm


class FakeStream:
    def __init__(self, terminal):
        self.terminal = terminal

    def isatty(self):
        return self.terminal


class BrokenStream:
    def isatty(self):
        raise ValueError("closed file")


class TerminalConfirmTests(unittest.TestCase):
    def test_only_a_terminal_counts_as_an_answer_source(self):
        self.assertTrue(terminal_confirm.stdin_is_terminal(FakeStream(True)))
        self.assertFalse(terminal_confirm.stdin_is_terminal(FakeStream(False)))
        self.assertFalse(terminal_confirm.stdin_is_terminal(BrokenStream()))
        # None means standard input itself. Replace it: on Windows the NUL device a test runner hands
        # to its children reports itself as a terminal.
        with patch.object(sys, "stdin", None):
            self.assertFalse(terminal_confirm.stdin_is_terminal(None))
        with patch.object(sys, "stdin", FakeStream(False)):
            self.assertFalse(terminal_confirm.stdin_is_terminal(None))
        self.assertFalse(terminal_confirm.stdin_is_terminal(object()))

    def test_nul_device_is_not_a_terminal(self):
        # On Windows isatty() is True for the NUL device; only a console handle may answer a prompt.
        with open(os.devnull) as nul:
            self.assertFalse(terminal_confirm.stdin_is_terminal(nul))

    def test_windows_file_backed_stream_needs_a_console_handle(self):
        class FileBacked(FakeStream):
            def fileno(self):
                return 7

        with patch.object(terminal_confirm, "_IS_WINDOWS", True), \
                patch.object(terminal_confirm, "_console_mode_answers", return_value=False) as console:
            self.assertFalse(terminal_confirm.stdin_is_terminal(FileBacked(True)))
            console.assert_called_once_with(7)
        with patch.object(terminal_confirm, "_IS_WINDOWS", True), \
                patch.object(terminal_confirm, "_console_mode_answers", return_value=True):
            self.assertTrue(terminal_confirm.stdin_is_terminal(FileBacked(True)))
        # A stream with no descriptor keeps the isatty() answer, and a non-terminal never asks the descriptor.
        with patch.object(terminal_confirm, "_IS_WINDOWS", True):
            self.assertTrue(terminal_confirm.stdin_is_terminal(FakeStream(True)))
            self.assertFalse(terminal_confirm.stdin_is_terminal(FakeStream(False)))
        with patch.object(terminal_confirm, "_IS_WINDOWS", True), \
                patch.object(terminal_confirm, "_console_mode_answers", side_effect=AssertionError("descriptor asked")):
            self.assertFalse(terminal_confirm.stdin_is_terminal(FileBacked(False)))

    def test_console_mode_is_false_for_an_unknown_descriptor(self):
        self.assertFalse(terminal_confirm._console_mode_answers(-1))

    def test_yes_gate_refuses_with_the_one_message_that_names_yes(self):
        stderr = io.StringIO()
        with patch.object(terminal_confirm, "stdin_is_terminal", return_value=False), contextlib.redirect_stderr(stderr):
            self.assertFalse(terminal_confirm.may_read_yes_no())
        self.assertEqual(stderr.getvalue().strip(), terminal_confirm.YES_REFUSAL)
        self.assertIn("--yes", terminal_confirm.YES_REFUSAL)

    def test_redirected_input_refused_for_acknowledgment_even_with_retired_environment_variable(self):
        # TEAM_ACK_FROM_STDIN is retired: no code reads it, so setting it changes nothing.
        stderr = io.StringIO()
        with patch.object(terminal_confirm, "stdin_is_terminal", return_value=False), \
                patch.dict(os.environ, {"TEAM_ACK_FROM_STDIN": "1"}), contextlib.redirect_stderr(stderr):
            self.assertFalse(terminal_confirm.may_read_acknowledgment())
        self.assertIn(terminal_confirm.ACKNOWLEDGMENT_REFUSAL, stderr.getvalue())

        with patch.object(terminal_confirm, "stdin_is_terminal", return_value=True):
            self.assertTrue(terminal_confirm.may_read_acknowledgment())

    def test_recorded_and_printed_confirmation_forms(self):
        self.assertEqual(terminal_confirm.confirmed_by(False, None), "terminal")
        self.assertEqual(terminal_confirm.confirmed_by(True, None), "--yes")
        self.assertEqual(terminal_confirm.confirmed_by(False, "PRODDB_UNIQUE"), "--confirm-target")
        self.assertEqual(terminal_confirm.confirmed_by(True, "PRODDB_UNIQUE"), "--yes --confirm-target")
        self.assertEqual(terminal_confirm.confirmed_line(True, None), "Confirmed by --yes; the interactive prompt was skipped.")
        self.assertEqual(terminal_confirm.confirmed_line(False, "X"), "Confirmed by --confirm-target X; the interactive prompt was skipped.")
        self.assertEqual(terminal_confirm.confirmed_line(True, "X"), "Confirmed by --yes and --confirm-target X; the interactive prompt was skipped.")


if __name__ == "__main__":
    unittest.main()
