from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from copy import copy
from unittest.mock import patch

import teamlib.release as release_module
from teamlib.app_checks import build_app_check_bundle
from teamlib.release import ReleaseError, apply_release, plan_release, release_app_check_bundle, release_app_order, release_app_trees, release_migration_files, verify_release
from scripts.tests.release_fixtures import _build_git_release_fixture


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
        (self.repo / "apps" / "hr" / ".apex").mkdir(parents=True)
        (self.repo / "apps" / "hr" / "application.apx").write_bytes(b"hr app\n")
        (self.repo / "apps" / "hr" / ".apex" / "apexlang.json").write_bytes(b'{"format":"APEXLANG"}\n')
        (self.repo / "apps" / "payroll" / ".apex").mkdir(parents=True)
        (self.repo / "apps" / "payroll" / "application.apx").write_bytes(b"payroll app\n")
        (self.repo / "apps" / "payroll" / ".apex" / "apexlang.json").write_bytes(b'{"format":"APEXLANG"}\n')
        (self.repo / "apps" / ".gitkeep").write_bytes(b"")
        (self.repo / "migrations").mkdir()
        mid = "20260907T100000__alice__one"
        (self.repo / "migrations" / f"{mid}.sql").write_text("-- migration-version: 1\n-- target: tables\n-- destructive: false\n\nCREATE TABLE T(ID NUMBER);\n", encoding="utf-8")
        (self.repo / "migrations" / f"{mid}.verify.sql").write_text("", encoding="utf-8")
        (self.repo / "targets").mkdir()
        (self.repo / "targets" / "masters.json").write_text('{"version":1,"masters":[]}\n', encoding="utf-8")
        (self.repo / "app_context" / "checkout").mkdir(parents=True)
        (self.repo / "app_context" / "checkout" / "release.json").write_text(
            '{"version": 1, "requires": ["20260907T100000__alice__one"]}\n', encoding="utf-8"
        )
        (self.repo / "app_context" / "hr").mkdir(parents=True)
        (self.repo / "app_context" / "hr" / "release.json").write_text(
            '{"version": 1, "requires": ["20260907T100000__alice__one"]}\n', encoding="utf-8"
        )
        (self.repo / "app_context" / "payroll").mkdir(parents=True)
        (self.repo / "app_context" / "payroll" / "release.json").write_text(
            '{"version": 1, "requires": []}\n', encoding="utf-8"
        )
        (self.repo / "ci" / "app-checks").mkdir(parents=True)
        declaration = {
            "version": 1,
            "alias": "checkout",
            "page_ids": [1],
            "checks": [
                {
                    "id": "objects",
                    "page_id": 1,
                    "kind": "select",
                    "verify_sql": "checkout/objects.verify.sql",
                    "expected_objects": ["APP.CHECKOUT"],
                },
                {
                    "id": "login",
                    "page_id": 1,
                    "kind": "flow",
                    "flow": "checkout/login.flow.json",
                    "steps": [
                        {
                            "action": "navigate",
                            "path": "/ords/r/app/checkout/home",
                            "expected_visible_text": "Checkout",
                        }
                    ],
                },
            ],
        }
        (self.repo / "ci" / "app-checks" / "checkout.json").write_text(
            json.dumps(declaration, sort_keys=True) + "\n", encoding="utf-8"
        )
        (self.repo / "ci" / "app-checks" / "checkout").mkdir()
        (self.repo / "ci" / "app-checks" / "checkout" / "objects.verify.sql").write_text(
            "SELECT 'TEAM_ASSERT|' || assertion_name || '|' || status AS status "
            "FROM (SELECT 'checkout_table_exists' assertion_name, 'PASS' status FROM dual);\n",
            encoding="utf-8",
        )
        (self.repo / "ci" / "app-checks" / "checkout" / "login.flow.json").write_text(
            "{}\n", encoding="utf-8"
        )
        (self.repo / "ci" / "app-checks" / "hr").mkdir()
        (self.repo / "ci" / "app-checks" / "hr" / "objects.verify.sql").write_text(
            "SELECT 'TEAM_ASSERT|hr|PASS' AS status FROM dual;\n",
            encoding="utf-8",
        )
        hr_decl = {
            "version": 1,
            "alias": "hr",
            "page_ids": [1],
            "checks": [
                {
                    "id": "hr_objects",
                    "page_id": 1,
                    "kind": "select",
                    "verify_sql": "hr/objects.verify.sql",
                    "expected_objects": ["HR"],
                }
            ],
        }
        (self.repo / "ci" / "app-checks" / "hr.json").write_text(
            json.dumps(hr_decl, sort_keys=True) + "\n", encoding="utf-8"
        )
        (self.repo / "ci" / "app-checks" / "payroll").mkdir()
        (self.repo / "ci" / "app-checks" / "payroll" / "objects.verify.sql").write_text(
            "SELECT 'TEAM_ASSERT|payroll|PASS' AS status FROM dual;\n",
            encoding="utf-8",
        )
        payroll_decl = {
            "version": 1,
            "alias": "payroll",
            "page_ids": [1],
            "checks": [
                {
                    "id": "payroll_objects",
                    "page_id": 1,
                    "kind": "select",
                    "verify_sql": "payroll/objects.verify.sql",
                    "expected_objects": ["PAYROLL"],
                }
            ],
        }
        (self.repo / "ci" / "app-checks" / "payroll.json").write_text(
            json.dumps(payroll_decl, sort_keys=True) + "\n", encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "release seed"], check=True)
        self.commit = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def rewrite_manifest(self, archive: Path, mutate, label: str) -> Path:
        destination = Path(self.temp.name) / f"tampered-{label}.tar"
        with tarfile.open(archive, mode="r:") as source, tarfile.open(
            destination, mode="w", format=tarfile.USTAR_FORMAT
        ) as output:
            for original in source.getmembers():
                member = copy(original)
                data = source.extractfile(original).read() if original.isfile() else b""
                if member.name == "release/MANIFEST.json":
                    manifest = json.loads(data.decode("utf-8"))
                    mutate(manifest)
                    data = json.dumps(
                        manifest, sort_keys=True, indent=2, ensure_ascii=False
                    ).encode("utf-8") + b"\n"
                    member.size = len(data)
                output.addfile(member, io.BytesIO(data) if member.isfile() else None)
        return destination

    def test_deterministic_build_and_complete_verify(self):
        first = _build_git_release_fixture(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "out1", kind="app", alias="checkout")
        second = _build_git_release_fixture(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "out2", kind="app", alias="checkout")
        self.assertEqual(first.archive_digest, second.archive_digest)
        verified = verify_release(first.archive_path)
        self.assertEqual(verified.source_commit, self.commit)
        self.assertEqual(release_app_trees(first.archive_path)["checkout"]["application.apx"], b"app\n")
        self.assertIn("release/contracts/masters.json", first.payload_paths)
        self.assertIn("release/checks/apps/checkout.json", first.payload_paths)
        self.assertTrue(first.toolchain)
        self.assertEqual(verified.app_tree_digests, first.app_tree_digests)
        self.assertEqual(tuple(release_migration_files(first.archive_path)), ())

    def test_release_exposes_the_verified_application_check_bundle(self):
        manifest = _build_git_release_fixture(
            self.repo, self.commit, "1.2.3", Path(self.temp.name) / "check-bundle-out", kind="app", alias="checkout"
        )
        bundle = release_app_check_bundle(manifest.archive_path)
        source_members = {
            path.relative_to(self.repo / "ci" / "app-checks").as_posix(): path.read_bytes()
            for path in (self.repo / "ci" / "app-checks").rglob("*")
            if path.is_file() and (path.name.startswith("checkout") or "checkout/" in path.as_posix())
        }

        self.assertEqual(
            bundle.checks_digest,
            build_app_check_bundle(source_members, ("checkout",)).checks_digest,
        )
        self.assertEqual(
            manifest.app_checks_digest,
            verify_release(manifest.archive_path).app_checks_digest,
        )

    def test_build_self_verifies_the_emitted_archive(self):
        with patch.object(release_module, "verify_release", wraps=release_module.verify_release) as verifier:
            _build_git_release_fixture(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "self-verify-out", kind="app", alias="checkout")
        self.assertEqual(verifier.call_count, 1)

    def test_release_preserves_authored_down_members_and_detects_tampering(self):
        migration_id = "20260907T100001__alice__reversible"
        migration_root = self.repo / "migrations"
        (migration_root / f"{migration_id}.sql").write_text(
            "-- migration-version: 1\n-- target: tables\n-- destructive: false\n\nCREATE TABLE REVERSIBLE(ID NUMBER);\n",
            encoding="utf-8",
        )
        (migration_root / f"{migration_id}.verify.sql").write_text("", encoding="utf-8")
        (migration_root / f"{migration_id}.down.sql").write_text(
            "-- migration-version: 1\n-- destructive: true\n\nDROP TABLE REVERSIBLE;\n",
            encoding="utf-8",
        )
        (migration_root / f"{migration_id}.down.verify.sql").write_text("", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "reversible migration"], check=True)
        commit = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        manifest = _build_git_release_fixture(self.repo, commit, "1.2.4", Path(self.temp.name) / "reversible-out", kind="schema")
        self.assertEqual(tuple(path for path in release_migration_files(manifest.archive_path) if path.startswith(migration_id)), (
            f"{migration_id}.sql", f"{migration_id}.verify.sql", f"{migration_id}.down.sql", f"{migration_id}.down.verify.sql",
        ))
        tampered = Path(self.temp.name) / "reversible-tampered.tar"
        with tarfile.open(manifest.archive_path, mode="r:") as source, tarfile.open(tampered, mode="w", format=tarfile.USTAR_FORMAT) as destination:
            for member in source.getmembers():
                data = source.extractfile(member).read() if member.isfile() else b""
                if member.name.endswith(f"{migration_id}.down.sql"):
                    data = data.replace(b"DROP TABLE", b"DROP VIEW")
                    member.size = len(data)
                destination.addfile(member, io.BytesIO(data) if member.isfile() else None)
        with self.assertRaisesRegex(ReleaseError, "hash mismatch"):
            verify_release(tampered)

    def test_dirty_and_untracked_files_do_not_enter_artifact(self):
        (self.repo / "apps" / "checkout" / "evil.apx").write_text("untracked", encoding="utf-8")
        manifest = _build_git_release_fixture(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "out", kind="app", alias="checkout")
        self.assertNotIn("evil.apx", manifest.payload_paths)

    def test_apps_placeholder_is_not_packaged_as_an_application(self):
        manifest = _build_git_release_fixture(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "placeholder-out", kind="app", alias="checkout")
        self.assertNotIn("release/apps/.gitkeep", manifest.payload_paths)
        self.assertEqual(tuple(release_app_trees(manifest.archive_path)), ("checkout",))

    def test_unsafe_symlink_in_commit_refuses(self):
        (self.repo / "apps" / "checkout" / "link.apx").symlink_to("/etc/passwd")
        subprocess.run(["git", "-C", str(self.repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "bad"], check=True)
        bad = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        with self.assertRaises(ReleaseError):
            _build_git_release_fixture(self.repo, bad, "1.2.3", Path(self.temp.name) / "bad-out", kind="app", alias="checkout")

    def test_unrepresentable_ustar_path_refuses_before_output(self):
        long_name = "x" * 101 + ".apx"
        path = self.repo / "apps" / "checkout" / long_name
        path.write_text("too long", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", str(path)], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "long path"], check=True)
        commit = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        out = Path(self.temp.name) / "long-out"
        with self.assertRaisesRegex(ReleaseError, "cannot be represented"):
            _build_git_release_fixture(self.repo, commit, "1.2.3", out, kind="app", alias="checkout")
        self.assertFalse(out.exists())

    def test_plan_release_uses_artifact_history(self):
        manifest = _build_git_release_fixture(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "out", kind="schema")
        plan = plan_release(manifest.archive_path, {}, {"role": "test", "environment": "test", "instance_id": "TEST"})
        self.assertEqual(plan.pending, ("20260907T100000__alice__one",))

    def test_plan_release_excludes_reverted_artifacts(self):
        manifest = _build_git_release_fixture(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "reverted-out", kind="schema")
        migration_id = manifest.migrations[0]["id"]
        history = {migration_id: {"status": "REVERTED", "checksum": manifest.migrations[0]["checksum"], "sequence": 2}}
        plan = plan_release(manifest.archive_path, history, {"role": "test", "environment": "test", "instance_id": "TEST"})
        self.assertEqual(plan.pending, ())

    def test_plan_release_refuses_unresolved_and_foreign_history(self):
        manifest = _build_git_release_fixture(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "history-out", kind="schema")
        target = {"role": "test", "environment": "test", "instance_id": "TEST"}
        unresolved = {"20260907T100000__alice__one": {"status": "UNKNOWN"}}
        with self.assertRaisesRegex(ReleaseError, "unresolved"):
            plan_release(manifest.archive_path, unresolved, target)
        foreign = {"20260907T090000__bob__old": {"status": "APPLIED", "checksum": "a" * 64}}
        with self.assertRaisesRegex(ReleaseError, "foreign"):
            plan_release(manifest.archive_path, foreign, target)

    def test_apply_release_executes_only_through_explicit_nonproduction_adapters(self):
        manifest = _build_git_release_fixture(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "apply-out", kind="schema")
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
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "migrate")

    def test_apply_release_for_app_executes_only_deploy_after_requirements(self):
        manifest = _build_git_release_fixture(self.repo, self.commit, "1.2.4", Path(self.temp.name) / "apply-app-out", kind="app", alias="hr")
        target = {"role": "test", "environment": "test", "instance_id": "TEST"}
        req = manifest.required_migrations[0]
        history = {req["id"]: {"status": "APPLIED", "checksum": req["checksum"]}}
        plan = plan_release(manifest.archive_path, history, target)
        events = []
        report = apply_release(
            manifest.archive_path,
            target,
            plan,
            history=history,
            apply_migrations=lambda pending, reviewed: events.append(("migrate", tuple(pending), reviewed.pending)),
            deploy_application=lambda alias, tree, reviewed: events.append(("deploy", alias, tree["application.apx"], reviewed.archive_digest)),
        )
        self.assertEqual(report.status, "applied")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "deploy")
        self.assertEqual(events[0][1], "hr")

        # Reverted requirement refuses before any writer
        bad_history = {req["id"]: {"status": "REVERTED", "checksum": req["checksum"]}}
        bad_plan = plan_release(manifest.archive_path, bad_history, target)
        events.clear()
        with self.assertRaisesRegex(ReleaseError, "required migration unavailable"):
            apply_release(
                manifest.archive_path,
                target,
                bad_plan,
                history=bad_history,
                apply_migrations=lambda *a: events.append("unexpected"),
                deploy_application=lambda *a: events.append("unexpected"),
            )
        self.assertEqual(events, [])

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
        manifest = _build_git_release_fixture(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "order-out", kind="app", alias="checkout")
        original = release_module._read_archive_bytes
        with patch.object(release_module, "_read_archive_bytes", wraps=original) as reader:
            self.assertEqual(release_app_order(manifest.archive_path), ("checkout",))
            self.assertEqual(reader.call_count, 1)

    def test_manifest_contract_and_app_check_digests_are_verified(self):
        manifest = _build_git_release_fixture(self.repo, self.commit, "1.2.3", Path(self.temp.name) / "digest-out", kind="app", alias="checkout")
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

    def test_verify_rejects_migration_manifest_not_derived_from_payload(self):
        manifest = _build_git_release_fixture(
            self.repo, self.commit, "1.2.3", Path(self.temp.name) / "metadata-out", kind="schema"
        )
        mutations = {
            "checksum": "0" * 64,
            "target": "code",
            "dependencies": [["foreign", "1" * 64]],
            "destructive": True,
            "reversible": True,
            "down_destructive": True,
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                tampered = self.rewrite_manifest(
                    manifest.archive_path,
                    lambda data, field=field, value=value: data["migrations"][0].__setitem__(field, value),
                    field,
                )
                with self.assertRaisesRegex(ReleaseError, "migration metadata"):
                    verify_release(tampered)

    def test_verify_rejects_false_source_tree_and_application_tree_digest(self):
        manifest = _build_git_release_fixture(
            self.repo, self.commit, "1.2.3", Path(self.temp.name) / "digest-out", kind="app", alias="checkout"
        )
        tampered_source = self.rewrite_manifest(
            manifest.archive_path,
            lambda data: data.__setitem__("source_tree", "0" * 64),
            "source-tree",
        )
        with self.assertRaisesRegex(ReleaseError, "source tree"):
            verify_release(tampered_source)
        tampered_app = self.rewrite_manifest(
            manifest.archive_path,
            lambda data: data["app_tree_digests"].__setitem__("checkout", "0" * 64),
            "app-tree",
        )
        with self.assertRaisesRegex(ReleaseError, "application tree"):
            verify_release(tampered_app)

    def test_verify_requires_a_closed_manifest_shape_and_valid_source_commit(self):
        manifest = _build_git_release_fixture(
            self.repo, self.commit, "1.2.3", Path(self.temp.name) / "shape-out", kind="app", alias="checkout"
        )
        unknown = self.rewrite_manifest(
            manifest.archive_path,
            lambda data: data.__setitem__("unexpected", True),
            "unknown-field",
        )
        with self.assertRaisesRegex(ReleaseError, "manifest keys"):
            verify_release(unknown)
        missing = self.rewrite_manifest(
            manifest.archive_path,
            lambda data: data.pop("toolchain"),
            "missing-field",
        )
        with self.assertRaisesRegex(ReleaseError, "manifest keys"):
            verify_release(missing)
        bad_commit = self.rewrite_manifest(
            manifest.archive_path,
            lambda data: data.__setitem__("source_commit", "not-a-commit"),
            "source-commit",
        )
        with self.assertRaisesRegex(ReleaseError, "source commit"):
            verify_release(bad_commit)


    def test_schema_build_and_deterministic_verify(self):
        first = _build_git_release_fixture(self.repo, self.commit, "1.0.0", Path(self.temp.name) / "schema1", kind="schema")
        second = _build_git_release_fixture(self.repo, self.commit, "1.0.0", Path(self.temp.name) / "schema2", kind="schema")
        self.assertEqual(first.archive_digest, second.archive_digest)
        self.assertEqual(first.kind, "schema")
        self.assertIsNone(first.alias)
        self.assertEqual(first.required_migrations, ())
        self.assertEqual(first.app_tree_digests, {})
        self.assertTrue(any(p.startswith("release/migrations/") for p in first.payload_paths))
        self.assertFalse(any(p.startswith("release/apps/") for p in first.payload_paths))
        self.assertFalse(any(p.startswith("release/checks/apps/") for p in first.payload_paths))
        self.assertFalse(any(p.startswith("release/app_context/") for p in first.payload_paths))
        verified = verify_release(first.archive_path)
        self.assertEqual(verified.kind, "schema")
        self.assertIsNone(verified.alias)

    def test_app_build_and_deterministic_verify(self):
        first = _build_git_release_fixture(self.repo, self.commit, "1.0.0", Path(self.temp.name) / "hr1", kind="app", alias="hr")
        second = _build_git_release_fixture(self.repo, self.commit, "1.0.0", Path(self.temp.name) / "hr2", kind="app", alias="hr")
        self.assertEqual(first.archive_digest, second.archive_digest)
        self.assertEqual(first.kind, "app")
        self.assertEqual(first.alias, "hr")
        self.assertEqual(set(first.app_tree_digests.keys()), {"hr"})
        self.assertFalse(any(p.startswith("release/migrations/") for p in first.payload_paths))
        self.assertFalse(any("payroll" in p for p in first.payload_paths))
        self.assertFalse(any("checkout" in p for p in first.payload_paths))
        self.assertTrue(any(p.startswith("release/apps/hr/") for p in first.payload_paths))
        self.assertIn("release/checks/apps/hr.json", first.payload_paths)
        self.assertIn("release/app_context/hr/release.json", first.payload_paths)
        self.assertEqual(len(first.required_migrations), 1)
        self.assertEqual(first.required_migrations[0]["id"], "20260907T100000__alice__one")
        self.assertEqual(len(first.required_migrations[0]["checksum"]), 64)
        verified = verify_release(first.archive_path)
        self.assertEqual(verified.kind, "app")
        self.assertEqual(verified.alias, "hr")
        self.assertEqual(verified.required_migrations, first.required_migrations)

    def test_empty_schema_release_refuses(self):
        empty_repo = Path(self.temp.name) / "empty-repo"
        empty_repo.mkdir()
        subprocess.run(["git", "init", "-q", str(empty_repo)], check=True)
        subprocess.run(["git", "-C", str(empty_repo), "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(empty_repo), "config", "user.name", "Test"], check=True)
        (empty_repo / "apps" / "hr").mkdir(parents=True)
        (empty_repo / "apps" / "hr" / "application.apx").write_bytes(b"hr")
        (empty_repo / "apps" / "hr" / ".apex").mkdir()
        (empty_repo / "apps" / "hr" / ".apex" / "apexlang.json").write_bytes(b"{}")
        subprocess.run(["git", "-C", str(empty_repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(empty_repo), "commit", "-qm", "no migrations"], check=True)
        commit = subprocess.check_output(["git", "-C", str(empty_repo), "rev-parse", "HEAD"], text=True).strip()
        with self.assertRaisesRegex(ReleaseError, "no migrations"):
            _build_git_release_fixture(empty_repo, commit, "1.0.0", Path(self.temp.name) / "empty-out", kind="schema")

    def test_app_build_fails_for_missing_release_json(self):
        (self.repo / "apps" / "other" / ".apex").mkdir(parents=True)
        (self.repo / "apps" / "other" / "application.apx").write_bytes(b"other")
        (self.repo / "apps" / "other" / ".apex" / "apexlang.json").write_bytes(b"{}")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "app without release.json"], check=True)
        commit = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        with self.assertRaisesRegex(ReleaseError, "release.json"):
            _build_git_release_fixture(self.repo, commit, "1.0.0", Path(self.temp.name) / "other-out", kind="app", alias="other")

    def test_app_build_fails_for_absent_requirement_id(self):
        (self.repo / "app_context" / "hr" / "release.json").write_text(
            '{"version": 1, "requires": ["20260907T100000__alice__absent"]}\n', encoding="utf-8"
        )
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "bad req"], check=True)
        commit = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        with self.assertRaisesRegex(ReleaseError, "required migration not found"):
            _build_git_release_fixture(self.repo, commit, "1.0.0", Path(self.temp.name) / "bad-req-out", kind="app", alias="hr")

    def test_app_build_fails_for_duplicate_requirement_id(self):
        (self.repo / "app_context" / "hr" / "release.json").write_text(
            '{"version": 1, "requires": ["20260907T100000__alice__one", "20260907T100000__alice__one"]}\n',
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "dup req"], check=True)
        commit = subprocess.check_output(["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        with self.assertRaisesRegex(ReleaseError, "duplicate requirement"):
            _build_git_release_fixture(self.repo, commit, "1.0.0", Path(self.temp.name) / "dup-req-out", kind="app", alias="hr")

    def test_app_build_succeeds_with_empty_requires(self):
        manifest = _build_git_release_fixture(
            self.repo, self.commit, "1.0.0", Path(self.temp.name) / "payroll-out", kind="app", alias="payroll"
        )
        self.assertEqual(manifest.required_migrations, ())
        self.assertEqual(manifest.alias, "payroll")

    def test__build_git_release_fixture_without_kind_refuses(self):
        with self.assertRaisesRegex(ReleaseError, "release kind must be specified"):
            _build_git_release_fixture(self.repo, self.commit, "1.0.0", Path(self.temp.name) / "no-kind-out")

    def test_verify_release_rejects_cross_kind_members(self):
        schema_manifest = _build_git_release_fixture(
            self.repo, self.commit, "1.0.0", Path(self.temp.name) / "cross-schema-out", kind="schema"
        )
        tampered_schema = Path(self.temp.name) / "tampered-schema.tar"
        with tarfile.open(schema_manifest.archive_path, mode="r:") as source, tarfile.open(
            tampered_schema, mode="w", format=tarfile.USTAR_FORMAT
        ) as dest:
            for member in source.getmembers():
                dest.addfile(member, source.extractfile(member) if member.isfile() else None)
            info = tarfile.TarInfo("release/apps/hr/application.apx")
            info.size = 2
            dest.addfile(info, io.BytesIO(b"hr"))
        with self.assertRaisesRegex(ReleaseError, "cross-kind|payload"):
            verify_release(tampered_schema)

        app_manifest = _build_git_release_fixture(
            self.repo, self.commit, "1.0.0", Path(self.temp.name) / "cross-app-out", kind="app", alias="hr"
        )
        tampered_app = Path(self.temp.name) / "tampered-app.tar"
        with tarfile.open(app_manifest.archive_path, mode="r:") as source, tarfile.open(
            tampered_app, mode="w", format=tarfile.USTAR_FORMAT
        ) as dest:
            for member in source.getmembers():
                dest.addfile(member, source.extractfile(member) if member.isfile() else None)
            info = tarfile.TarInfo("release/migrations/20260907T100000__alice__one.sql")
            info.size = 3
            dest.addfile(info, io.BytesIO(b"sql"))
        with self.assertRaisesRegex(ReleaseError, "cross-kind|payload"):
            verify_release(tampered_app)


if __name__ == "__main__":
    unittest.main()
