"""The three .env loaders must accept the same keys.

scripts/local_config.py (Python), scripts/load_env.sh (Bash) and scripts/load_env.ps1 (PowerShell) each keep their own
list of allowed keys. A key added to one list only is accepted by verify-local and refused by every team.sh command
(or the other way round), which no other test notices: STAGING_NEUTRALIZE_PRINCIPALS did exactly that.
"""
from __future__ import annotations

import _test_subprocess_safety  # noqa: F401
import re
import unittest
from pathlib import Path

from scripts import local_config

ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = re.compile(r"[A-Z][A-Z0-9_]*")


def python_keys() -> set[str]:
    return set(local_config._BASE_ALLOWED_KEYS) | set(local_config.UC_APX_KEYS)


def bash_keys(text: str) -> set[str]:
    """The key patterns of the `case "$project_env_key" in` block, up to its `*)` refusal."""
    start = text.index('case "$project_env_key" in')
    block = text[start:text.index("    *)", start)]
    keys: set[str] = set()
    for line in block.splitlines()[1:]:
        pattern = line.strip()
        if re.fullmatch(r"[A-Z0-9_|]+(\|\\|\)(\s*;;)?)", pattern):
            keys.update(IDENTIFIER.findall(pattern))
    return keys


def powershell_keys(text: str) -> set[str]:
    """The $projectEnvAllowed list, the 26.1 uc-apx additions and the generated MIGRATION_* keys."""
    start = text.index("$projectEnvAllowed = @(")
    listed = text[start:text.index(")", start)]
    keys = set(re.findall(r'"([A-Z][A-Z0-9_]*)"', listed))
    added = re.search(r'\$projectEnvAllowed \+= @\(([^)]*)\)', text)
    if added:
        keys.update(re.findall(r'"([A-Z][A-Z0-9_]*)"', added.group(1)))
    loop = re.search(
        r'foreach \(\$projectEnvMigrationPrefix in @\(([^)]*)\)\) \{\s*foreach \(\$projectEnvSuffix in @\(([^)]*)\)\)', text)
    if loop:
        prefixes = re.findall(r'"([A-Z_]*)"', loop.group(1))
        suffixes = re.findall(r'"([A-Z_]+)"', loop.group(2))
        keys.update(f"{prefix}MIGRATION_{suffix}" for prefix in prefixes for suffix in suffixes)
    return keys


class LoaderKeyParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.python = python_keys()
        self.bash = bash_keys((ROOT / "scripts" / "load_env.sh").read_text(encoding="utf-8"))
        self.powershell = powershell_keys((ROOT / "scripts" / "load_env.ps1").read_text(encoding="utf-8"))

    def test_the_parsers_found_the_lists(self) -> None:
        for name, keys in (("load_env.sh", self.bash), ("load_env.ps1", self.powershell)):
            with self.subTest(loader=name):
                self.assertIn("TABLES_SCHEMA", keys)
                self.assertIn("STAGING_MIGRATION_EXPECTED_USER", keys)
                self.assertGreater(len(keys), 30)

    def test_bash_accepts_exactly_the_python_keys(self) -> None:
        self.assertEqual(sorted(self.python - self.bash), [], "keys local_config.py accepts but load_env.sh refuses")
        self.assertEqual(sorted(self.bash - self.python), [], "keys load_env.sh accepts but local_config.py refuses")

    def test_powershell_accepts_exactly_the_python_keys(self) -> None:
        self.assertEqual(sorted(self.python - self.powershell), [], "keys local_config.py accepts but load_env.ps1 refuses")
        self.assertEqual(sorted(self.powershell - self.python), [], "keys load_env.ps1 accepts but local_config.py refuses")


if __name__ == "__main__":
    unittest.main()
