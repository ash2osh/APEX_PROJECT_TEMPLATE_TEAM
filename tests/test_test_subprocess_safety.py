"""Tests for the TEMPLATE_TEST_POWERSHELL switch in _test_subprocess_safety: which PowerShell working_pwsh() reports."""

import os
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import _test_subprocess_safety as safety


ENGINE_VARIABLE = "TEMPLATE_TEST_POWERSHELL"
PWSH = "/opt/microsoft/powershell/pwsh"
WINDOWS_POWERSHELL = "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
BOTH_ON_PATH = {"pwsh": PWSH, "powershell": WINDOWS_POWERSHELL}


def _bad_value_message(value):
    return f'TEMPLATE_TEST_POWERSHELL must be "pwsh" or "powershell", not {value!r}'


@contextmanager
def _engine_setting(value):
    """Patch os.environ so TEMPLATE_TEST_POWERSHELL is value, or absent when value is None."""
    values = {} if value is None else {ENGINE_VARIABLE: value}
    with patch.dict(os.environ, values, clear=True):
        yield


class PowerShellEngineSwitchTests(unittest.TestCase):
    def setUp(self):
        self._clear_caches()
        self.addCleanup(self._clear_caches)

    @staticmethod
    def _clear_caches():
        safety.working_pwsh.cache_clear()
        safety.working_windows_powershell.cache_clear()

    def _resolve(self, value, on_path, launcher_ok=True):
        """Run working_pwsh() under the setting value, with only the names in on_path found on PATH.

        Returns the result, the names looked up on PATH, and the executables given to the launcher check.
        """
        self._clear_caches()
        with _engine_setting(value), \
                patch.object(safety.shutil, "which", side_effect=lambda name: on_path.get(name)) as which, \
                patch.object(safety, "_launcher_works", return_value=launcher_ok) as launcher:
            result = safety.working_pwsh()
        looked_up = [call.args[0] for call in which.call_args_list]
        launched = [call.args[0] for call in launcher.call_args_list]
        return result, looked_up, launched

    def test_unset_selects_pwsh(self):
        result, looked_up, launched = self._resolve(None, BOTH_ON_PATH)
        self.assertEqual(PWSH, result)
        self.assertEqual(["pwsh"], looked_up)
        self.assertEqual([PWSH], launched)

    def test_empty_and_pwsh_spellings_select_pwsh(self):
        for value in ("", "   ", "pwsh", "PWSH ", " Pwsh\t"):
            with self.subTest(value=value):
                result, looked_up, launched = self._resolve(value, BOTH_ON_PATH)
                self.assertEqual(PWSH, result)
                self.assertEqual(["pwsh"], looked_up)
                self.assertEqual([PWSH], launched)

    def test_pwsh_is_none_when_its_launcher_does_not_start(self):
        result, _, launched = self._resolve(None, {"pwsh": PWSH}, launcher_ok=False)
        self.assertIsNone(result)
        self.assertEqual([PWSH], launched)

    def test_powershell_selects_windows_powershell_and_never_looks_for_pwsh(self):
        for value in ("powershell", " PowerShell ", "POWERSHELL"):
            with self.subTest(value=value):
                result, looked_up, launched = self._resolve(value, BOTH_ON_PATH)
                self.assertEqual(WINDOWS_POWERSHELL, result)
                self.assertNotIn("pwsh", looked_up)
                self.assertEqual([WINDOWS_POWERSHELL], launched)

    def test_powershell_does_not_fall_back_to_pwsh(self):
        result, looked_up, launched = self._resolve("powershell", {"pwsh": PWSH})
        self.assertIsNone(result)
        self.assertEqual(["powershell"], looked_up)
        self.assertEqual([], launched)

    def test_powershell_is_none_when_windows_powershell_does_not_start(self):
        result, _, launched = self._resolve("powershell", BOTH_ON_PATH, launcher_ok=False)
        self.assertIsNone(result)
        self.assertEqual([WINDOWS_POWERSHELL], launched)

    def test_other_values_raise_naming_the_value_before_anything_starts(self):
        self._clear_caches()
        with _engine_setting("bash"), \
                patch.object(safety.shutil, "which") as which, \
                patch.object(safety, "_launcher_works") as launcher:
            with self.assertRaises(ValueError) as raised:
                safety.working_pwsh()
        self.assertEqual(_bad_value_message("bash"), str(raised.exception))
        which.assert_not_called()
        launcher.assert_not_called()

    def test_selected_powershell_engine_normalises_the_setting(self):
        cases = {
            None: "pwsh",
            "": "pwsh",
            "  ": "pwsh",
            "PWSH": "pwsh",
            "powershell": "powershell",
            " PowerShell\n": "powershell",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                with _engine_setting(value):
                    self.assertEqual(expected, safety.selected_powershell_engine())

    def test_selected_powershell_engine_rejects_other_values(self):
        for value in ("bash", "powershell.exe", "pwsh7"):
            with self.subTest(value=value):
                with _engine_setting(value):
                    with self.assertRaises(ValueError) as raised:
                        safety.selected_powershell_engine()
                self.assertEqual(_bad_value_message(value), str(raised.exception))


if __name__ == "__main__":
    unittest.main()
