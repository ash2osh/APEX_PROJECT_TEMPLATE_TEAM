import errno
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from scripts import directory_install as installer
except ImportError:
    installer = None


class DirectoryInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.destination_parent = self.root / "migrations" / "DEMO"
        self.destination_parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def require_installer(self):
        self.assertIsNotNone(installer, "exclusive staged directory installation is not implemented")
        self.assertTrue(hasattr(installer, "install_directory_exclusive"), "exclusive installer API is missing")
        self.assertTrue(hasattr(installer, "DirectoryInstallError"), "exclusive installer error type is missing")
        return installer

    def make_stage(self, name: str = "stage") -> Path:
        stage = self.scratch / name
        stage.mkdir()
        (stage / "001-create-table.sql").write_bytes(b"CREATE TABLE T (ID NUMBER);\n")
        (stage / "checks.json").write_bytes(b'{"schemaVersion":1}\n')
        return stage

    def destination(self, name: str = "2026-10-13_customer-status-r001") -> Path:
        return self.destination_parent / name

    @staticmethod
    def snapshot_tree(folder: Path) -> dict[str, bytes | None]:
        return {
            path.relative_to(folder).as_posix(): path.read_bytes() if path.is_file() else None
            for path in sorted(folder.rglob("*"))
        }

    def test_install_moves_a_new_stage_directory(self) -> None:
        api = self.require_installer()
        stage = self.make_stage()
        destination = self.destination()
        expected = self.snapshot_tree(stage)

        api.install_directory_exclusive(stage, destination)

        self.assertFalse(stage.exists())
        self.assertEqual(self.snapshot_tree(destination), expected)

    def test_empty_destination_is_preserved(self) -> None:
        api = self.require_installer()
        stage = self.make_stage()
        before_stage = self.snapshot_tree(stage)
        destination = self.destination()
        destination.mkdir()

        with self.assertRaises(api.DirectoryInstallError):
            api.install_directory_exclusive(stage, destination)

        self.assertTrue(destination.is_dir())
        self.assertEqual(self.snapshot_tree(destination), {})
        self.assertTrue(stage.is_dir())
        self.assertEqual(self.snapshot_tree(stage), before_stage)

    def test_nonempty_destination_is_byte_identical_after_refusal(self) -> None:
        api = self.require_installer()
        stage = self.make_stage()
        destination = self.destination()
        destination.mkdir()
        (destination / "existing.sql").write_bytes(b"preserve these exact bytes\x00\xff")
        nested = destination / "nested"
        nested.mkdir()
        (nested / "note.txt").write_bytes(b"existing\r\nbytes")
        before_destination = self.snapshot_tree(destination)
        before_stage = self.snapshot_tree(stage)

        with self.assertRaises(api.DirectoryInstallError):
            api.install_directory_exclusive(stage, destination)

        self.assertEqual(self.snapshot_tree(destination), before_destination)
        self.assertEqual(self.snapshot_tree(stage), before_stage)

    def test_refuses_different_filesystems_before_rename(self) -> None:
        api = self.require_installer()
        stage = self.make_stage()
        destination = self.destination()
        before_stage = self.snapshot_tree(stage)

        with patch.object(api, "_filesystem_id", side_effect=(101, 202)):
            with self.assertRaises(api.DirectoryInstallError):
                api.install_directory_exclusive(stage, destination)

        self.assertTrue(stage.is_dir())
        self.assertFalse(destination.exists())
        self.assertEqual(self.snapshot_tree(stage), before_stage)

    def test_refuses_when_platform_has_no_supported_primitive(self) -> None:
        api = self.require_installer()
        stage = self.make_stage()
        destination = self.destination()
        before_stage = self.snapshot_tree(stage)

        with patch.object(api.sys, "platform", "freebsd"):
            with self.assertRaisesRegex(api.DirectoryInstallError, "unavailable|supported"):
                api.install_directory_exclusive(stage, destination)

        self.assertTrue(stage.is_dir())
        self.assertFalse(destination.exists())
        self.assertEqual(self.snapshot_tree(stage), before_stage)

    def test_windows_platform_dispatch_uses_its_exclusive_rename(self) -> None:
        api = self.require_installer()
        stage = self.make_stage()
        destination = self.destination()
        with patch.object(api.sys, "platform", "win32"), patch.object(
            api, "_rename_windows_noreplace", wraps=api._rename_windows_noreplace
        ) as rename:
            api.install_directory_exclusive(stage, destination)

        rename.assert_called_once_with(stage.resolve(), destination.resolve())
        self.assertTrue(destination.is_dir())

    @unittest.skipUnless(sys.platform == "linux", "Linux renameat2 refusal injection")
    def test_refuses_when_linux_no_replace_primitive_is_unavailable(self) -> None:
        api = self.require_installer()
        stage = self.make_stage()
        destination = self.destination()
        before_stage = self.snapshot_tree(stage)

        with patch.object(api, "_rename_linux_noreplace", side_effect=OSError(errno.ENOSYS, "unsupported")):
            with self.assertRaises(api.DirectoryInstallError):
                api.install_directory_exclusive(stage, destination)

        self.assertTrue(stage.is_dir())
        self.assertFalse(destination.exists())
        self.assertEqual(self.snapshot_tree(stage), before_stage)

    @unittest.skipUnless(sys.platform == "linux", "native Linux renameat2 no-overwrite qualification")
    def test_linux_native_rename_does_not_replace_a_destination_created_after_precheck(self) -> None:
        api = self.require_installer()
        stage = self.make_stage()
        destination = self.destination()
        before_stage = self.snapshot_tree(stage)

        def create_destination_during_preflight(path: Path) -> int:
            if path == destination.parent and not destination.exists():
                destination.mkdir()
                (destination / "keep.txt").write_bytes(b"native-race-proof")
            return path.stat().st_dev

        with patch.object(api, "_filesystem_id", side_effect=create_destination_during_preflight):
            with self.assertRaisesRegex(api.DirectoryInstallError, "occupied"):
                api.install_directory_exclusive(stage, destination)

        self.assertEqual((destination / "keep.txt").read_bytes(), b"native-race-proof")
        self.assertTrue(stage.is_dir())
        self.assertEqual(self.snapshot_tree(stage), before_stage)

    def test_refuses_symlink_stage_destination_and_parent(self) -> None:
        api = self.require_installer()

        actual_stage = self.make_stage("actual-stage")
        stage_link = self.scratch / "stage-link"
        actual_destination = self.destination("actual-destination")
        actual_destination.mkdir()
        (actual_destination / "keep.txt").write_bytes(b"keep")
        destination_link = self.destination("destination-link")
        parent_link = self.root / "linked-parent"
        try:
            stage_link.symlink_to(actual_stage, target_is_directory=True)
            destination_link.symlink_to(actual_destination, target_is_directory=True)
            parent_link.symlink_to(self.destination_parent, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlink creation is unavailable: {error}")

        linked_parent_destination = parent_link / "via-link"
        for stage, destination in (
            (stage_link, self.destination("stage-link-target")),
            (self.make_stage("destination-link-stage"), destination_link),
            (self.make_stage("parent-link-stage"), linked_parent_destination),
        ):
            with self.subTest(stage=stage, destination=destination), self.assertRaises(api.DirectoryInstallError):
                api.install_directory_exclusive(stage, destination)

        self.assertEqual((actual_destination / "keep.txt").read_bytes(), b"keep")

    def test_windows_reparse_attribute_is_treated_as_an_unsafe_path(self) -> None:
        api = self.require_installer()

        class ReparseDirectory:
            st_mode = stat.S_IFDIR
            st_file_attributes = 0x400

        self.assertTrue(api._is_symlink_or_reparse(ReparseDirectory()))

    def test_refuses_a_missing_or_nondirectory_destination_parent(self) -> None:
        api = self.require_installer()
        stage = self.make_stage()

        for destination in (
            self.root / "missing" / "destination",
            self.root / "not-a-directory" / "destination",
        ):
            if destination.parent.name == "not-a-directory":
                destination.parent.write_text("file", encoding="utf-8")
            with self.subTest(destination=destination), self.assertRaises(api.DirectoryInstallError):
                api.install_directory_exclusive(stage, destination)

        self.assertTrue(stage.is_dir())

    @unittest.skipUnless(sys.platform == "win32", "native Windows no-overwrite qualification")
    def test_windows_native_rename_does_not_replace_a_destination_created_after_precheck(self) -> None:
        api = self.require_installer()
        stage = self.make_stage()
        destination = self.destination()
        before_stage = self.snapshot_tree(stage)

        def create_destination_during_preflight(path: Path) -> int:
            if path == destination.parent and not destination.exists():
                destination.mkdir()
                (destination / "keep.txt").write_bytes(b"native-race-proof")
            return path.stat().st_dev

        with patch.object(api, "_filesystem_id", side_effect=create_destination_during_preflight):
            with self.assertRaises(api.DirectoryInstallError):
                api.install_directory_exclusive(stage, destination)

        self.assertTrue(destination.is_dir())
        self.assertEqual((destination / "keep.txt").read_bytes(), b"native-race-proof")
        self.assertTrue(stage.is_dir())
        self.assertEqual(self.snapshot_tree(stage), before_stage)


if __name__ == "__main__":
    unittest.main()
