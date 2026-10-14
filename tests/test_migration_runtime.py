from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fake_sqlcl import environment as fake_environment, install
from scripts.migration_runtime import MigrationRuntimeError, require_migration_sqlcl_version
from scripts.sqlcl_session import bash_command


ROOT = Path(__file__).resolve().parents[1]


class MigrationRuntimeTests(unittest.TestCase):
    def test_version_bridge_uses_posix_repo_path_for_git_bash(self) -> None:
        calls = []

        def runner(arguments, **_kwargs):
            calls.append(arguments)
            return SimpleNamespace(returncode=0, stdout="SQLcl: Release 26.3 Production\n", stderr="")

        with patch("scripts.migration_runtime._REPO_ROOT", PureWindowsPath(r"C:\team\repo")):
            require_migration_sqlcl_version(runner=runner)

        self.assertEqual("C:/team/repo", calls[0][4])
        self.assertEqual(Path(calls[0][5]).as_posix(), calls[0][5])

    def test_runtime_floor_accepts_minimum_and_newer_sqlcl(self) -> None:
        for output in (
            "SQLcl: Release 26.3 Production\nBuild: 26.3.0.260.1620\n",
            "SQLcl: Release 26.3.0.1 Production\n",
            "SQLcl: Release 27.1 Production\n",
        ):
            with self.subTest(output=output):
                calls = []

                def runner(arguments, _calls=calls, _output=output, **kwargs):
                    _calls.append((arguments, kwargs, Path(arguments[5], ".sqlcl-stdin").is_file()))
                    return SimpleNamespace(returncode=0, stdout=_output, stderr="")

                require_migration_sqlcl_version(runner=runner)

                self.assertEqual(1, len(calls))
                arguments = calls[0][0]
                self.assertEqual(bash_command(), arguments[0])
                self.assertEqual("-c", arguments[1])
                self.assertIn('source "$1/scripts/sqlcl_safe.sh"', arguments[2])
                self.assertIn('invoke_sqlcl_safe "$2" -V', arguments[2])
                self.assertEqual("migration-runtime", arguments[3])
                self.assertEqual(ROOT.as_posix(), arguments[4])
                self.assertTrue(calls[0][2])
                self.assertNotIn("-name", arguments)
                self.assertNotIn("docker-demo", arguments)
                self.assertTrue(calls[0][1]["capture_output"])
                self.assertTrue(calls[0][1]["text"])

    def test_launcher_uses_extensionless_fake_through_safe_bash_bridge(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            args_file = directory / "sql-args.txt"
            fake_bin = install(
                directory / "bin",
                'printf "%s\\n" "$@" > "$MIGRATION_TEST_SQL_ARGS"\n'
                'printf "SQLcl: Release 26.3 Production\\n"\n',
            )
            environment = fake_environment(fake_bin, MIGRATION_TEST_SQL_ARGS=str(args_file))
            with patch.dict(os.environ, environment):
                require_migration_sqlcl_version()

            self.assertEqual(["-V"], args_file.read_text(encoding="utf-8").splitlines())

    def test_runtime_floor_refuses_old_unreadable_or_failed_sqlcl(self) -> None:
        cases = (
            (SimpleNamespace(returncode=0, stdout="SQLcl: Release 26.2.2.0 Production\n", stderr=""),),
            (SimpleNamespace(returncode=0, stdout="SQLcl unavailable\n", stderr=""),),
            (SimpleNamespace(returncode=1, stdout="SQLcl: Release 26.3 Production\n", stderr="failed"),),
        )
        for (result,) in cases:
            with self.subTest(result=result), self.assertRaises(MigrationRuntimeError):
                require_migration_sqlcl_version(runner=lambda *_args, _result=result, **_kwargs: _result)

    def test_runtime_floor_sanitizes_launch_failures(self) -> None:
        def runner(*_args, **_kwargs):
            raise FileNotFoundError("sql executable missing at /private/location/sql")

        with self.assertRaises(MigrationRuntimeError) as caught:
            require_migration_sqlcl_version(runner=runner)

        self.assertIn("could not run", str(caught.exception).casefold())
        self.assertNotIn("/private/location", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
