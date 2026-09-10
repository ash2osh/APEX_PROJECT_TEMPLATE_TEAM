from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parents[1 if Path(__file__).resolve().parent.name == "tests" else 2])
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from pathlib import Path
import hashlib
import json
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from teamlib.assertions import AssertionVerificationError
from teamlib.config import Target
from teamlib.release import ApplyReport, ReleasePlan
from teamlib.release_adapter import (
    ReleaseAdapterError,
    ReleaseApplyContext,
    apply_verified_release,
    apply_verified_release_live,
    main,
)


ROOT = Path(__file__).resolve().parents[2]


class ReleaseAdapterTests(unittest.TestCase):
    def test_production_contract_refuses_before_loading_environment_or_archive(self):
        plan = ReleasePlan("a" * 64, "b" * 64, (), "c" * 64, {"environment": "production"}, "d" * 64)
        with tempfile.TemporaryDirectory(prefix="team-release-adapter-") as directory:
            with self.assertRaisesRegex(ReleaseAdapterError, "production"):
                apply_verified_release(
                    Path(directory) / "missing.tar",
                    ROOT / "targets" / "production.json",
                    Path(directory) / "missing.env",
                    plan,
                    {},
                )

    def test_apply_uses_the_complete_planned_target_document(self):
        target_path = ROOT / "targets" / "test.json"
        target_document = json.loads(target_path.read_text(encoding="utf-8"))
        target_digest = hashlib.sha256(
            json.dumps(target_document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        plan = ReleasePlan("a" * 64, target_digest, (), "c" * 64, target_document, "d" * 64)
        profile = Target(
            project="example-team-apex", role="test", environment="test", connection="test-apex",
            instance_id="EXAMPLE_TEST_INSTANCE", db_name="FREEPDB1", service="test-service",
            session_user="EXAMPLE_APP", current_schema="EXAMPLE_APP", alias=None,
            workspace_id=None, app_id=None, parsing_schema=None, ownership_mode="shared", binding_digest="b" * 64,
        )
        config = SimpleNamespace(
            role="test", environment="test", tables_schema="EXAMPLE_APP", code_schema="EXAMPLE_APP",
            metadata_schema="EXAMPLE_META",
        )
        migration_store = SimpleNamespace(bootstrap=lambda *args, **kwargs: None)
        control_store = SimpleNamespace(setup_state=lambda *args, **kwargs: None)
        observed = {}

        def capture_apply(archive, target, received_plan, **kwargs):
            observed["target"] = target
            return ApplyReport("planned", received_plan.pending, archive_digest="a" * 64, target_state_key=profile.state_key)

        with patch("teamlib.release_adapter.load_config", return_value=config), \
             patch("teamlib.release_adapter.verify_release", return_value=SimpleNamespace(source_commit="abc")), \
             patch("teamlib.release_adapter.profile_target", return_value=profile), \
             patch("teamlib.release_adapter.SqlMigrationStore", return_value=migration_store), \
             patch("teamlib.release_adapter.SqlControlStore", return_value=control_store), \
             patch("teamlib.release_adapter.release_app_trees", return_value={}), \
             patch("teamlib.release_adapter.release_migration_files", return_value={}), \
             patch("teamlib.release_adapter.apply_release", side_effect=capture_apply):
            with tempfile.TemporaryDirectory(prefix="team-release-adapter-positive-") as directory:
                archive = Path(directory) / "release.tar"
                archive.write_bytes(b"fixture")
                result = apply_verified_release(archive, target_path, Path(directory) / "env", plan, {})

        self.assertEqual(result.status, "planned")
        self.assertEqual(result.target_state_key, profile.state_key)
        self.assertEqual(observed["target"], target_document)

    def test_main_resolves_environment_without_an_explicit_env_flag(self):
        with tempfile.TemporaryDirectory(prefix="team-release-adapter-cli-") as directory:
            root = Path(directory)
            (root / "dummy.json").write_text("{}", encoding="utf-8")
            (root / "dummy.tar").write_bytes(b"")
            argv = [
                "apply-release",
                str(root / "dummy.tar"),
                "--plan", str(root / "dummy.json"),
                "--history", str(root / "dummy.json"),
                "--target", str(ROOT / "targets" / "test.json"),
                "--out", str(root / "apply-report.json"),
            ]
            # The environment profile is absent, so main() must fail with the
            # diagnostic SystemExit -- not with NameError from a missing import.
            with self.assertRaises(SystemExit) as caught:
                main(argv)
            self.assertIn("environment profile file not found", str(caught.exception))

    def test_migration_verification_rejects_a_failed_assertion(self):
        target_path = ROOT / "targets" / "test.json"
        target_document = json.loads(target_path.read_text(encoding="utf-8"))
        target_digest = hashlib.sha256(
            json.dumps(
                target_document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        migration_id = "20260910T120000__review__assertion"
        plan = ReleasePlan(
            "a" * 64,
            target_digest,
            (migration_id,),
            "c" * 64,
            target_document,
            "d" * 64,
        )
        profile = Target(
            project="example-team-apex",
            role="test",
            environment="test",
            connection="test-apex",
            instance_id="EXAMPLE_TEST_INSTANCE",
            db_name="FREEPDB1",
            service="test-service",
            session_user="EXAMPLE_APP",
            current_schema="EXAMPLE_APP",
            alias=None,
            workspace_id=None,
            app_id=None,
            parsing_schema=None,
            ownership_mode="shared",
            binding_digest="b" * 64,
        )
        config = SimpleNamespace(
            role="test",
            environment="test",
            tables_schema="EXAMPLE_APP",
            code_schema="EXAMPLE_APP",
            metadata_schema="EXAMPLE_META",
        )
        migration_store = SimpleNamespace(bootstrap=lambda *args, **kwargs: None)
        control_store = SimpleNamespace(setup_state=lambda *args, **kwargs: None)

        def invoke_adapter(archive, target, reviewed, **kwargs):
            kwargs["apply_migrations"](({"id": migration_id},), reviewed)
            return ApplyReport(
                "applied",
                reviewed.pending,
                archive_digest="a" * 64,
                target_state_key=profile.state_key,
            )

        def invoke_verification(source, profiles, **kwargs):
            profiles["verify"](
                SimpleNamespace(id=migration_id, target="tables"),
                "migrate",
                Path(source) / f"{migration_id}.verify.sql",
            )

        migration_files = {
            f"{migration_id}.sql": (
                b"-- migration-version: 1\n-- target: tables\n"
                b"-- destructive: false\n\nSELECT 1 FROM dual;\n"
            ),
            f"{migration_id}.verify.sql": (
                b"SELECT 'TEAM_ASSERT|postcondition|FAIL' FROM dual;\n"
            ),
        }
        with patch("teamlib.release_adapter.load_config", return_value=config), \
             patch(
                 "teamlib.release_adapter.verify_release",
                 return_value=SimpleNamespace(source_commit="abc"),
             ), \
             patch("teamlib.release_adapter.profile_target", return_value=profile), \
             patch(
                 "teamlib.release_adapter.SqlMigrationStore",
                 return_value=migration_store,
             ), \
             patch(
                 "teamlib.release_adapter.SqlControlStore",
                 return_value=control_store,
             ), \
             patch("teamlib.release_adapter.release_app_trees", return_value={}), \
             patch(
                 "teamlib.release_adapter.release_migration_files",
                 return_value=migration_files,
             ), \
             patch(
                 "teamlib.release_adapter.run_sqlcl",
                 return_value=SimpleNamespace(
                     stdout="TEAM_ASSERT|postcondition|FAIL\n"
                 ),
             ), \
             patch(
                 "teamlib.release_adapter.apply_plan",
                 side_effect=invoke_verification,
             ), \
             patch(
                 "teamlib.release_adapter.apply_release",
                 side_effect=invoke_adapter,
             ):
            with tempfile.TemporaryDirectory(
                prefix="team-release-adapter-assertion-"
            ) as directory:
                archive = Path(directory) / "release.tar"
                archive.write_bytes(b"fixture")
                with self.assertRaisesRegex(
                    AssertionVerificationError, "postcondition"
                ):
                    apply_verified_release(
                        archive,
                        target_path,
                        Path(directory) / "env",
                        plan,
                        {},
                    )


class SchemaSetDigestTests(unittest.TestCase):
    def test_bootstrap_receives_the_computed_schema_set_digest(self):
        import inspect

        from teamlib import release_adapter

        source = inspect.getsource(release_adapter.apply_verified_release)
        self.assertNotIn(
            'schema_set_digest="release"',
            source,
            "apply-release must bootstrap with the computed digest, not a literal; "
            "a literal makes every record_inventory raise ORA-20011",
        )
        self.assertIn("bootstrap(metadata, schema_set_digest=schema_set_digest)", source)


class LiveReleaseAdapterTests(unittest.TestCase):
    def context(self, *, destructive: bool = False, events: list[str] | None = None):
        events = events if events is not None else []
        metadata = Target(
            project="team", role="test", environment="test", connection="meta",
            instance_id="INSTANCE", db_name="FREEPDB1", service="service",
            session_user="META", current_schema="META", alias=None, workspace_id=None,
            app_id=None, parsing_schema=None, ownership_mode="shared", binding_digest="a" * 64,
        )
        manifest = SimpleNamespace(
            source_commit="a" * 40,
            archive_digest="b" * 64,
            app_tree_digests={"employee": "c" * 64},
            migrations=[{"id": "m1", "destructive": destructive}],
        )
        config = SimpleNamespace(role="test", environment="test")

        class Store:
            def bootstrap(self, *_args, **_kwargs):
                events.append("bootstrap")

            def read_history(self, *_args, **_kwargs):
                events.append("read-live-history")
                return {"m1": {"status": "APPLIED"}}

        class Control:
            def setup_state(self, *_args, **_kwargs):
                events.append("setup-control")

        context = ReleaseApplyContext(
            manifest=manifest,
            target_document={"version": 1, "role": "test", "environment": "test"},
            config=config,
            metadata=metadata,
            migration_store=Store(),
            control_store=Control(),
            schema_set_digest="d" * 64,
            app_targets=(),
        )
        return context, events

    def test_live_adapter_recomputes_plan_from_store_without_plan_or_history_files(self):
        events: list[str] = []
        context, events = self.context(events=events)
        plan = ReleasePlan(
            context.manifest.archive_digest, "e" * 64, (), "f" * 64,
            context.target_document, "1" * 64,
        )
        applied = ApplyReport("applied", (), context.manifest.archive_digest, source_commit=context.manifest.source_commit)
        with patch("teamlib.release_adapter._validated_release_context", return_value=context), \
             patch("teamlib.release_adapter.plan_release", return_value=plan) as planned, \
             patch("teamlib.release_adapter._apply_release_context", return_value=applied) as applied_context:
            result = apply_verified_release_live(
                self.context_file("release.tar"),
                self.context_file("targets/test.json"),
                context.config,
                repo=self.context_file("repo"),
            )
        self.assertEqual(result.status, "applied")
        self.assertEqual(events, ["bootstrap", "read-live-history", "setup-control"])
        planned.assert_called_once_with(self.context_file("release.tar"), {"m1": {"status": "APPLIED"}}, context.target_document)
        applied_context.assert_called_once()

    def test_live_adapter_refuses_destructive_pending_work_before_control_setup(self):
        events: list[str] = []
        context, events = self.context(destructive=True, events=events)
        plan = ReleasePlan(
            context.manifest.archive_digest, "e" * 64, ("m1",), "f" * 64,
            context.target_document, "1" * 64,
        )
        with patch("teamlib.release_adapter._validated_release_context", return_value=context), \
             patch("teamlib.release_adapter.plan_release", return_value=plan), \
             patch("teamlib.release_adapter._apply_release_context") as applied_context:
            with self.assertRaisesRegex(ReleaseAdapterError, "destructive maintenance"):
                apply_verified_release_live(
                    self.context_file("release.tar"),
                    self.context_file("targets/test.json"),
                    context.config,
                    repo=self.context_file("repo"),
                )
        self.assertEqual(events, ["bootstrap", "read-live-history"])
        applied_context.assert_not_called()

    @staticmethod
    def context_file(name: str) -> Path:
        # The live adapter test patches context construction, so paths only
        # need to prove that the API accepts archive/contract objects directly.
        return Path(name)


if __name__ == "__main__":
    unittest.main()
