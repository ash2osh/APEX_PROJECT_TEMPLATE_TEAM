"""Path containment handles equivalent Windows short-name aliases."""

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

            self.assertIsNotNone(relative_to_root, "the short-path-aware containment helper is missing")
            assert relative_to_root is not None
            with patch("scripts.path_utils.os.path.samefile", side_effect=samefile):
                self.assertEqual(Path("scratch/report.json"), relative_to_root(candidate, root))

    def test_does_not_treat_a_parent_alias_outside_the_repository_as_source_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "real-parent"
            root = parent / "repo"
            root.mkdir(parents=True)
            alias_parent = Path(temporary) / "parent-alias"
            try:
                alias_parent.symlink_to(parent, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            candidate = alias_parent / "repo" / "scratch" / "report.json"

            self.assertEqual(Path("scratch/report.json"), relative_to_root(candidate, root))

    def test_rejects_a_path_outside_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            outside = Path(temporary) / "outside" / "file.json"
            root.mkdir()
            with self.assertRaises(ValueError):
                relative_to_root(outside, root)

    def test_rejects_intermediate_symlink_under_root_even_on_lexical_match(self) -> None:
        from scripts.path_utils import PathReparseError

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            root.mkdir()
            outside = Path(temporary) / "outside"
            outside.mkdir()
            (outside / "secret.sql").write_text("SELECT 1 FROM DUAL;\n", encoding="utf-8")
            link_dir = root / "linked"
            try:
                link_dir.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            with self.assertRaises(PathReparseError):
                relative_to_root(link_dir / "secret.sql", root)


if __name__ == "__main__":
    unittest.main()
