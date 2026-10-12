"""Filesystem path helpers keep Windows short-name aliases inside their root."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.path_utils import relative_to_root


class RelativeToRootTests(unittest.TestCase):
    def test_accepts_a_physical_root_alias_and_returns_only_the_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "long-root-name"
            root.mkdir()
            alias = Path(temporary) / "short-root"
            candidate = alias / "scratch" / "report.json"

            def samefile(left: str | Path, right: str | Path) -> bool:
                return Path(left) == alias and Path(right) == root

            with patch("scripts.path_utils.os.path.samefile", side_effect=samefile):
                self.assertEqual(Path("scratch/report.json"), relative_to_root(candidate, root))

    def test_does_not_treat_a_parent_alias_outside_the_repository_as_source_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "real-parent"
            root = parent / "repo"
            root.mkdir(parents=True)
            alias_parent = Path(temporary) / "parent-alias"
            alias_parent.symlink_to(parent, target_is_directory=True)
            candidate = alias_parent / "repo" / "scratch" / "report.json"

            self.assertEqual(Path("scratch/report.json"), relative_to_root(candidate, root))

    def test_rejects_an_unrelated_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            outside = Path(temporary) / "outside" / "file.json"
            root.mkdir()
            with self.assertRaises(ValueError):
                relative_to_root(outside, root)


if __name__ == "__main__":
    unittest.main()
