from __future__ import annotations

from pathlib import Path
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import teamlib.release as release_module
from teamlib.release import ReleaseError, apply_release, build_release, plan_release, release_app_order, release_app_trees, release_migration_files, validate_release_identity, verify_release


class ReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="team-release-")
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "config", "user.name", "Release Test"], check=True)
        (self.repo / "apps" / "checkout" / ".apex").mkdir(parents=True)
        (self.repo / "apps" / "checkout" / "application.apx").write_bytes(b"app\n")
        (self.repo / "apps" / "checkout" / ".apex" / "apexlang.json").write_bytes(b'{"format":"APEXLANG"}\n')
        (self.repo / "migrations").mkdir()
        mid = "20260907T100000__alice__one"
        (self.repo / "migrations" / f"{mid}.sql").write_text("-- migration-version: 1\n-- target: tables\n-- destructive: false\n\nCREATE TABLE T(ID NUMBER);\n", encoding="utf-8")
        (self.repo / "migrations" / f"{mid}.verify.sql").write_text("", encoding="utf-8")
        (self.repo / "targets").mkdir()
        (self.repo / "targets" / "masters.json").write_text('{"version":1,"masters":[]}\n', encoding="utf-8")
        (self.repo / "ci" / "app-checks").mkdir(parents=True)
        (self.repo / "ci" / "app-checks" / "checkout.json").write_text('{"version":1,"alias":"checkout"}\n', encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "release seed"], check=True)
        self.commit = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_deterministic_build_and_complete_verify(self):
        first = build_release(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "out1")
        second = build_release(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "out2")
        self.assertEqual(first.archive_digest, second.archive_digest)
        verified = verify_release(first.archive_path)
        self.assertEqual(verified.source_commit, self.commit)
        self.assertEqual(release_app_trees(first.archive_path)["checkout"]["application.apx"], b"app\n")
        self.assertIn("release/contracts/masters.json", first.payload_paths)
        self.assertIn("release/checks/apps/checkout.json", first.payload_paths)
        self.assertTrue(first.toolchain)
        self.assertEqual(verified.app_tree_digests, first.app_tree_digests)
        self.assertEqual(tuple(release_migration_files(first.archive_path)), (
            "20260907T100000__alice__one.sql",
            "20260907T100000__alice__one.verify.sql",
        ))

    def test_dirty_and_untracked_files_do_not_enter_artifact(self):
        (self.repo / "apps" / "checkout" / "evil.apx").write_text("untracked", encoding="utf-8")
        manifest = build_release(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "out")
        self.assertNotIn("evil.apx", manifest.payload_paths)

    def test_unsafe_symlink_in_commit_refuses(self):
        (self.repo / "apps" / "checkout" / "link.apx").symlink_to("/etc/passwd")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "bad"], check=True)
        bad = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        with self.assertRaises(ReleaseError):
            build_release(self.repo, bad, "1.2.3", Path(self.temp.name) / "bad-out")

    def test_tag_identity_and_release_record_are_immutable(self):
        with self.assertRaises(ReleaseError):
            validate_release_identity("v1.2.4", "1.2.3", self.commit, {})
        record = {"1.2.3": {"source_commit": self.commit, "archive_digest": "a" * 64}}
        validate_release_identity("v1.2.3", "1.2.3", self.commit, record)
        with self.assertRaises(ReleaseError):
            validate_release_identity("v1.2.3", "1.2.3", "b" * 40, record)

    def test_unrepresentable_ustar_path_refuses_before_output(self):
        long_name = "x" * 101 + ".apx"
        path = self.repo / "apps" / "checkout" / long_name
        path.write_text("too long", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", str(path)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "long path"], check=True)
        commit = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        out = Path(self.temp.name) / "long-out"
        with self.assertRaisesRegex(ReleaseError, "cannot be represented"):
            build_release(self.repo, commit, "1.2.3", out)
        self.assertFalse(out.exists())

    def test_plan_release_uses_artifact_history(self):
        manifest = build_release(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "out")
        plan = plan_release(manifest.archive_path, {}, {"role": "test", "environment": "test", "instance_id": "TEST"})
        self.assertEqual(plan.pending, ("20260907T100000__alice__one",))

    def test_apply_release_executes_only_through_explicit_nonproduction_adapters(self):
        manifest = build_release(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "apply-out")
        target = {"role": "test", "environment": "test", "instance_id": "TEST"}
        plan = plan_release(manifest.archive_path, {}, target)
        events = []
        report = apply_release(
            manifest.archive_path,
            target,
            plan,
            history={},
            apply_migrations=lambda pending, reviewed: events.append(("migrate", tuple(pending), reviewed.pending)),
            deploy_application=lambda alias, tree, reviewed: events.append(("deploy", alias, tree["application.apx"], reviewed.archive_digest)),
        )
        self.assertEqual(report.status, "applied")
        self.assertEqual(events[0][0], "migrate")
        self.assertEqual(events[1][0], "deploy")

    def test_malformed_manifest_payload_is_rejected(self):
        archive = Path(self.temp.name) / "malformed.tar"
        manifest = {"format_version": 1, "version": "1.0.0", "source_commit": self.commit, "source_tree": "x", "payload": "not-a-list"}
        with tarfile.open(archive, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            data = json.dumps(manifest).encode("utf-8")
            info = tarfile.TarInfo("release/MANIFEST.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with self.assertRaisesRegex(ReleaseError, "payload"):
            verify_release(archive)

    def test_release_app_order_uses_one_stable_archive_read(self):
        manifest = build_release(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "order-out")
        original = release_module._read_archive_bytes
        with patch.object(release_module, "_read_archive_bytes", wraps=original) as reader:
            self.assertEqual(release_app_order(manifest.archive_path), ("checkout",))
            self.assertEqual(reader.call_count, 1)

    def test_manifest_contract_and_app_check_digests_are_verified(self):
        manifest = build_release(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "digest-out")
        tampered = Path(self.temp.name) / "digest-tampered.tar"
        with tarfile.open(manifest.archive_path, mode="r:") as source, tarfile.open(tampered, mode="w", format=tarfile.USTAR_FORMAT) as destination:
            for member in source.getmembers():
                data = source.extractfile(member).read() if member.isfile() else b""
                if member.name == "release/MANIFEST.json":
                    value = json.loads(data.decode("utf-8"))
                    value["app_checks_digest"] = "0" * 64
                    data = json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n"
                    member.size = len(data)
                destination.addfile(member, io.BytesIO(data) if member.isfile() else None)
        with self.assertRaisesRegex(ReleaseError, "app-check"):
            verify_release(tampered)


if __name__ == "__main__":
    unittest.main()
