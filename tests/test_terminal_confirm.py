import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

import _test_subprocess_safety  # noqa: F401
from scripts import migrate, terminal_confirm


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

    def test_yes_gate_refuses_with_the_one_message_that_names_yes(self):
        stderr = io.StringIO()
        with patch.object(terminal_confirm, "stdin_is_terminal", return_value=False), contextlib.redirect_stderr(stderr):
            self.assertFalse(terminal_confirm.may_read_yes_no())
        self.assertEqual(stderr.getvalue().strip(), terminal_confirm.YES_REFUSAL)
        self.assertIn("--yes", terminal_confirm.YES_REFUSAL)

    def test_the_acknowledgment_switch_never_opens_a_yes_no_prompt(self):
        stderr = io.StringIO()
        with patch.dict(os.environ, {terminal_confirm.ACK_FROM_STDIN_ENV: "1"}), patch.object(terminal_confirm, "stdin_is_terminal", return_value=False), \
                patch("builtins.input", side_effect=AssertionError("a piped answer must not be read")), contextlib.redirect_stderr(stderr):
            self.assertFalse(migrate._confirm_from_terminal("Migrating to PROD. Proceed? [y/N]"))
        self.assertIn("refused", stderr.getvalue())

    def test_the_acknowledgment_switch_allows_only_a_redirected_acknowledgment_and_says_so(self):
        stderr = io.StringIO()
        with patch.object(terminal_confirm, "stdin_is_terminal", return_value=False), contextlib.redirect_stderr(stderr):
            self.assertFalse(terminal_confirm.may_read_acknowledgment({}))
            self.assertTrue(terminal_confirm.may_read_acknowledgment({terminal_confirm.ACK_FROM_STDIN_ENV: "1"}))
            self.assertFalse(terminal_confirm.may_read_acknowledgment({terminal_confirm.ACK_FROM_STDIN_ENV: "yes"}))
        self.assertEqual(stderr.getvalue().count(terminal_confirm.ACK_FROM_STDIN_NOTICE), 1)

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
