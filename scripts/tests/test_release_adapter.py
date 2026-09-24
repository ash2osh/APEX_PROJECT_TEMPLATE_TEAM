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
from teamlib.destructive_confirmation import (
    ConfirmationError,
    ConfirmationRequirement,
    confirmation_template,
    load_confirmation,
    require_confirmations,
)
from teamlib.release import ApplyReport, ReleasePlan
from teamlib.release_adapter import (
    _APEX_BINDING_FIELDS,
    _assert_binding_matches_profile,
    _format3_confirmation_requirements,
    ReleaseAdapterError,
    ReleaseApplyContext,
    apply_verified_release,
    apply_verified_release_live,
    main,
)


ROOT = Path(__file__).resolve().parents[2]


class ApexBindingGuardTests(unittest.TestCase):
    """Both apply paths must reject the same set of redirected bindings.

    ``_verify_result_identity`` probes the database session only -- session
    user, schema, DB name, service, instance -- so nothing downstream notices
    that a contract named a different workspace or application ID than the
    environment profile. This comparison is the only guard, and the CLI path
    used to check six of the ten fields while the live path checked all ten.
    """

    def profile(self, **overrides) -> Target:
        values = dict(
            project="example-team-apex", role="test", environment="test",
            connection="test-apex", instance_id="INSTANCE", db_name="FREEPDB1",
            service="test-service", session_user="APP", current_schema="APP",
            alias="employee", workspace_id=100, app_id=200,
            parsing_schema="APP", ownership_mode="shared", binding_digest="a" * 64,
        )
        values.update(overrides)
        return Target(**values)

    def test_a_matching_binding_is_accepted(self):
        _assert_binding_matches_profile(self.profile(), self.profile(), "employee")

    def test_every_binding_field_is_compared(self):
        replacements = {
            "connection": "other-apex", "instance_id": "OTHER", "db_name": "OTHERPDB",
            "service": "other-service", "session_user": "OTHER", "current_schema": "OTHER",
            "workspace_id": 999, "app_id": 999, "parsing_schema": "OTHER",
            "ownership_mode": "single",
        }
        self.assertEqual(set(replacements), set(_APEX_BINDING_FIELDS))
        for field, value in replacements.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(ReleaseAdapterError, field):
                    _assert_binding_matches_profile(
                        self.profile(**{field: value}), self.profile(), "employee"
                    )

    def test_neither_apply_path_keeps_a_private_field_list(self):
        import inspect

        from teamlib import release_adapter

        for function in (
            release_adapter._validated_release_context,
            release_adapter.apply_verified_release,
        ):
            with self.subTest(function=function.__name__):
                source = inspect.getsource(function)
                self.assertIn("_assert_binding_matches_profile", source)
                self.assertNotIn('"session_user", "current_schema"', source)


class ReleaseAdapterTests(unittest.TestCase):
    def test_format3_down_replay_requires_exact_undo_confirmation(self):
        migration_id = "20260924T120000__alice__accounts"
        checksum = "a" * 64
        manifest = SimpleNamespace(migrations=({
            "id": migration_id, "checksum": checksum, "target": "tables",
            "destructive": False, "down_destructive": True,
        },))
        plan = ReleasePlan(
            archive_digest="b" * 64,
            target_digest="c" * 64,
            pending=(migration_id,),
            artifact_history_digest="d" * 64,
            target={},
            events=({"sequence": 2, "id": migration_id, "operation": "down", "checksum": checksum},),
            replay_from=1,
        )
        config = SimpleNamespace()
        with patch(
            "teamlib.release_adapter.profile_target",
            return_value=SimpleNamespace(state_key="e" * 64),
        ):
            requirements = _format3_confirmation_requirements(
                manifest,
                plan,
                {migration_id: {"status": "APPLIED", "checksum": checksum, "sequence": 1}},
                config,
            )
        self.assertEqual(len(requirements), 1)
        requirement = requirements[0]
        self.assertEqual(requirement.migration_id, migration_id)
        self.assertEqual(requirement.action, "undo")
        self.assertEqual(requirement.bundle_checksum, checksum)
        self.assertEqual(requirement.payload_target_state_key, "e" * 64)
        with self.assertRaises(ConfirmationError):
            require_confirmations(requirements, None)
        document = confirmation_template(requirements)
        document["confirmations"][0]["confirmed"] = True
        require_confirmations(requirements, document)

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

    def test_main_writes_a_false_confirmation_template_for_format3_replay(self):
        requirement = ConfirmationRequirement("m1", "migrate", "a" * 64, "b" * 64)
        target = {"role": "test", "environment": "test"}
        current_plan = ReleasePlan("c" * 64, "d" * 64, (), "e" * 64, target, "f" * 64)
        with tempfile.TemporaryDirectory(prefix="team-release-confirmation-cli-") as directory:
            root = Path(directory)
            env_path = root / "test.env"
            env_path.write_text("fixture\n", encoding="utf-8")
            archive = root / "release.tar"
            archive.write_bytes(b"fixture")
            history = root / "history.json"
            history.write_text("{}\n", encoding="utf-8")
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps({
                "archive_digest": "c" * 64,
                "target_digest": "d" * 64,
                "pending": [],
                "artifact_history_digest": "e" * 64,
                "target": target,
                "history_digest": "f" * 64,
                "events": [],
                "replay_from": 0,
            }), encoding="utf-8")
            confirmation_path = root / "confirmation.json"
            with patch("teamlib.release_adapter.load_config", return_value=SimpleNamespace()), \
                 patch("teamlib.release_adapter.verify_release", return_value=SimpleNamespace()), \
                 patch("teamlib.release_adapter.plan_release", return_value=current_plan), \
                 patch("teamlib.release_adapter._format3_confirmation_requirements", return_value=(requirement,)):
                result = main([
                    str(archive),
                    "--plan", str(plan_path),
                    "--history", str(history),
                    "--target", str(ROOT / "targets" / "test.json"),
                    "--env", str(env_path),
                    "--confirmation-out", str(confirmation_path),
                    "--out", str(root / "report.json"),
                ])

            actual, _digest = load_confirmation(confirmation_path)
        self.assertEqual(result, 0)
        self.assertEqual(actual, confirmation_template((requirement,)))

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
             patch("teamlib.migration_runtime.profile_target", return_value=profile), \
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
                 "teamlib.migration_runtime.run_sqlcl",
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
                # The failed assertion still names the failing check, but it
                # now reaches the operator as the adapter's structured refusal
                # rather than as an AssertionVerificationError traceback --
                # release_adapter.main() does not catch that class.
                with self.assertRaisesRegex(
                    ReleaseAdapterError, "postcondition"
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
        """apply-release must bootstrap with the real digest, not a literal.

        A placeholder is written into TEAM_MIGRATION_META and makes every
        record_inventory in the run -- and every later migrate against the same
        metadata schema -- raise ORA-20011 SCHEMA_SET_DIGEST_MISMATCH.
        """
        from teamlib.config import schema_set_digest

        config = SimpleNamespace(
            role="test", environment="test", tables_schema="EXAMPLE_APP",
            code_schema="EXAMPLE_APP", metadata_schema="EXAMPLE_META",
        )
        expected = schema_set_digest(config)
        recorded: list[str] = []
        migration_store = SimpleNamespace(
            bootstrap=lambda _target, *, schema_set_digest: recorded.append(schema_set_digest)
        )
        target_path = ROOT / "targets" / "test.json"
        metadata = Target(
            project="example-team-apex", role="test", environment="test", connection="meta",
            instance_id="EXAMPLE_TEST_INSTANCE", db_name="FREEPDB1", service="test-service",
            session_user="EXAMPLE_META", current_schema="EXAMPLE_META", alias=None,
            workspace_id=None, app_id=None, parsing_schema=None, ownership_mode="shared",
            binding_digest="e" * 64,
        )
        with patch("teamlib.release_adapter.load_config", return_value=config), \
             patch("teamlib.release_adapter.verify_release", return_value=SimpleNamespace(source_commit="abc")), \
             patch("teamlib.release_adapter.profile_target", return_value=metadata), \
             patch("teamlib.release_adapter.SqlMigrationStore", return_value=migration_store), \
             patch("teamlib.release_adapter.SqlControlStore", return_value=SimpleNamespace(setup_state=lambda *_a: None)), \
             patch("teamlib.release_adapter.release_app_trees", return_value={}), \
             patch("teamlib.release_adapter._apply_release_context", return_value=None), \
             tempfile.TemporaryDirectory(prefix="team-release-digest-") as directory:
            apply_verified_release(
                Path(directory) / "release.tar", target_path, Path(directory) / "env",
                ReleasePlan("a" * 64, "b" * 64, (), "c" * 64, {}, "d" * 64), {},
                root=Path(directory) / "state",
            )
        self.assertEqual(recorded, [expected])
        self.assertNotIn("release", recorded)


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

    def test_verify_required_migrations(self):
        from teamlib.release_adapter import verify_required_migrations
        history = {
            "m1": {"status": "APPLIED", "checksum": "a" * 64},
            "m2": {"status": "REVERTED", "checksum": "b" * 64},
            "m3": {"status": "APPLIED", "checksum": "c" * 64},
        }
        # Matching requirements
        verify_required_migrations(({"id": "m1", "checksum": "a" * 64}, {"id": "m3", "checksum": "c" * 64}), history)
        # Empty requirements
        verify_required_migrations((), history)
        # Missing
        with self.assertRaisesRegex(ReleaseAdapterError, "required migration unavailable: m_missing"):
            verify_required_migrations(({"id": "m_missing", "checksum": "a" * 64},), history)
        # Reverted
        with self.assertRaisesRegex(ReleaseAdapterError, "required migration unavailable: m2"):
            verify_required_migrations(({"id": "m2", "checksum": "b" * 64},), history)
        # Checksum mismatch
        with self.assertRaisesRegex(ReleaseAdapterError, "required migration unavailable: m1"):
            verify_required_migrations(({"id": "m1", "checksum": "f" * 64},), history)

    def test_schema_archive_calls_only_migration_apply_spy(self):
        from teamlib.release_adapter import _apply_release_context
        manifest = SimpleNamespace(
            kind="schema", alias=None, source_commit="a" * 40, archive_digest="b" * 64,
            app_tree_digests={}, migrations=[{"id": "m1", "destructive": False}], required_migrations=(),
        )
        migration_spy = []
        deploy_spy = []
        context = ReleaseApplyContext(
            manifest=manifest, target_document={"role": "test", "environment": "test"},
            config=SimpleNamespace(
                role="test", environment="test",
                tables_schema="APP", code_schema="APP", metadata_schema="META",
            ),
            metadata=Target("team", "test", "test", "conn", "inst", "db", "svc", "usr", "cur", None, None, None, None, "shared", "a" * 64),
            migration_store=SimpleNamespace(bootstrap=lambda *a, **kw: None, read_history=lambda *a: {}),
            control_store=SimpleNamespace(setup_state=lambda *a: None),
            schema_set_digest="s" * 64, app_targets=(),
        )
        plan = ReleasePlan("b" * 64, "t" * 64, ("m1",), "art" * 16, context.target_document, "h" * 64)

        def fake_apply(tar, doc, p, *, history=None, apply_migrations=None, deploy_application=None, target_state_key=None):
            if apply_migrations:
                apply_migrations((), p)
            if deploy_application:
                deploy_application("hr", {}, p)
            return ApplyReport("applied", p.pending, manifest.archive_digest)

        with patch("teamlib.release_adapter.release_migration_files", return_value={"m1.sql": b"-- m1"}), \
             patch("teamlib.release_adapter.migration_profiles", return_value={}), \
             patch("teamlib.release_adapter.apply_release", side_effect=fake_apply), \
             patch("teamlib.release_adapter.apply_plan", side_effect=lambda *a, **kw: migration_spy.append("migrate")), \
             patch("teamlib.release_adapter.deploy_app", side_effect=lambda *a, **kw: deploy_spy.append("deploy")):
            report = _apply_release_context(context, self.context_file("release.tar"), plan, {}, repo=Path("/tmp"))
        self.assertEqual(migration_spy, ["migrate"])
        self.assertEqual(deploy_spy, [])
        self.assertEqual(report.status, "applied")

    def test_schema_replay_partitions_confirmation_document_per_operation(self):
        from teamlib.release_adapter import _apply_release_context

        requirements = (
            ConfirmationRequirement("m1", "migrate", "a" * 64, "b" * 64),
            ConfirmationRequirement("m2", "migrate", "c" * 64, "d" * 64),
        )
        confirmation = confirmation_template(requirements)
        for entry in confirmation["confirmations"]:
            entry["confirmed"] = True
        require_confirmations(requirements, confirmation)

        history_digest = "e" * 64
        manifest = SimpleNamespace(
            kind="schema", alias=None, source_commit="", archive_digest="f" * 64,
            source={"history_digest": history_digest}, app_tree_digests={},
            migrations=[
                {"id": "plain", "destructive": False},
                {"id": "m1", "destructive": True},
                {"id": "m2", "destructive": True},
            ], required_migrations=(),
        )
        metadata = Target(
            "team", "test", "test", "meta", "TEST@host", "TEST", "testpdb",
            "META", "META", None, None, None, None, "shared", "1" * 64,
        )
        target_document = {"role": "test", "environment": "test"}
        plan = ReleasePlan(
            manifest.archive_digest, "2" * 64, ("plain", "m1", "m2"), "3" * 64,
            target_document, "4" * 64,
            events=(
                {"sequence": 1, "id": "plain", "operation": "up", "checksum": "5" * 64},
                {"sequence": 2, "id": "m1", "operation": "up", "checksum": "a" * 64},
                {"sequence": 3, "id": "m2", "operation": "up", "checksum": "c" * 64},
            ), replay_from=0,
        )
        context = ReleaseApplyContext(
            manifest=manifest, target_document=target_document,
            config=SimpleNamespace(role="test", environment="test"), metadata=metadata,
            migration_store=SimpleNamespace(read_history=lambda *_args: {}),
            control_store=SimpleNamespace(), schema_set_digest="6" * 64, app_targets=(),
        )
        applied = []

        def invoke_apply(_archive, _target, reviewed, *, apply_migrations=None, **_kwargs):
            apply_migrations((), reviewed)
            return ApplyReport("applied", reviewed.pending, manifest.archive_digest)

        with patch("teamlib.release_adapter.release_migration_files", return_value={}), \
             patch("teamlib.release_adapter.migration_profiles", return_value={}), \
             patch(
                 "teamlib.release_adapter.apply_forward",
                 side_effect=lambda _root, migration_id, profiles, *, confirmation=None: applied.append(
                     (migration_id, confirmation, profiles["source_commit"])
                 ),
             ), \
             patch("teamlib.release_adapter.apply_release", side_effect=invoke_apply):
            report = _apply_release_context(
                context, Path("fixture.tar"), plan, {}, repo=Path("."), confirmation=confirmation
            )

        self.assertEqual(report.status, "applied")
        self.assertEqual([item[0] for item in applied], ["plain", "m1", "m2"])
        self.assertIsNone(applied[0][1])
        for index, requirement in ((1, requirements[0]), (2, requirements[1])):
            subset = applied[index][1]
            require_confirmations((requirement,), subset)
            self.assertEqual(len(subset["confirmations"]), 1)
        self.assertEqual(applied[1][2], f"db-release:{history_digest}:2:0")
        self.assertEqual(applied[2][2], f"db-release:{history_digest}:3:0")

    def test_app_archive_calls_only_selected_app_deploy_after_requirements_verified(self):
        from teamlib.release_adapter import _apply_release_context
        manifest = SimpleNamespace(
            kind="app", alias="hr", source_commit="a" * 40, archive_digest="b" * 64,
            app_tree_digests={"hr": "c" * 64}, migrations=(),
            required_migrations=({"id": "m1", "checksum": "a" * 64},),
        )
        migration_spy = []
        deploy_spy = []
        hr_target = Target("team", "test", "test", "conn", "inst", "db", "svc", "usr", "cur", "hr", 100, 200, "HR", "shared", "a" * 64)
        context = ReleaseApplyContext(
            manifest=manifest, target_document={"role": "test", "environment": "test", "app_ids": {"hr": 200, "payroll": 300}},
            config=SimpleNamespace(role="test", environment="test", apps={"hr": 200, "payroll": 300}),
            metadata=Target("team", "test", "test", "conn", "inst", "db", "svc", "usr", "cur", None, None, None, None, "shared", "a" * 64),
            migration_store=SimpleNamespace(bootstrap=lambda *a, **kw: None, read_history=lambda *a: {"m1": {"status": "APPLIED", "checksum": "a" * 64}}),
            control_store=SimpleNamespace(setup_state=lambda *a: None),
            schema_set_digest="s" * 64, app_targets=(hr_target,),
        )
        plan = ReleasePlan("b" * 64, "t" * 64, (), "art" * 16, context.target_document, "h" * 64)

        def fake_apply(tar, doc, p, *, history=None, apply_migrations=None, deploy_application=None, target_state_key=None):
            if apply_migrations:
                apply_migrations((), p)
            if deploy_application:
                deploy_application("hr", {}, p)
            return ApplyReport("applied", p.pending, manifest.archive_digest)

        packaged = {"masters": [{"alias": "hr"}]}
        contracts = []

        def fake_deploy(target, *a, **kw):
            deploy_spy.append(target.alias)
            contracts.append(kw.get("master_contract"))

        with patch("teamlib.release_adapter.release_migration_files", return_value={}), \
             patch("teamlib.release_adapter.release_master_contract", return_value=packaged), \
             patch("teamlib.release_adapter.apply_release", side_effect=fake_apply), \
             patch("teamlib.release_adapter.apply_plan", side_effect=lambda *a, **kw: migration_spy.append("migrate")), \
             patch("teamlib.release_adapter.deploy_app", side_effect=fake_deploy):
            report = _apply_release_context(context, self.context_file("release.tar"), plan, {"m1": {"status": "APPLIED", "checksum": "a" * 64}}, repo=Path("/tmp"))
        self.assertEqual(migration_spy, [])
        self.assertEqual(deploy_spy, ["hr"])
        # The packaged contract, not the operator repository's copy, is validated.
        self.assertEqual(contracts, [packaged])
        self.assertEqual(report.status, "applied")

    def test_app_archive_refuses_when_requirement_unavailable(self):
        from teamlib.release_adapter import verify_required_migrations
        cases = [
            ("missing", {}),
            ("reverted", {"m1": {"status": "REVERTED", "checksum": "a" * 64}}),
            ("checksum", {"m1": {"status": "APPLIED", "checksum": "0" * 64}}),
        ]
        for name, history in cases:
            with self.subTest(name=name):
                with self.assertRaisesRegex(ReleaseAdapterError, "required migration unavailable"):
                    verify_required_migrations(({"id": "m1", "checksum": "a" * 64},), history)

    def test_live_adapter_app_release_refuses_when_requirement_unavailable(self):
        events: list[str] = []
        metadata = Target(
            project="team", role="test", environment="test", connection="meta",
            instance_id="INSTANCE", db_name="FREEPDB1", service="service",
            session_user="META", current_schema="META", alias=None, workspace_id=None,
            app_id=None, parsing_schema=None, ownership_mode="shared", binding_digest="a" * 64,
        )
        manifest = SimpleNamespace(
            kind="app", alias="hr", source_commit="a" * 40, archive_digest="b" * 64,
            app_tree_digests={"hr": "c" * 64}, migrations=(),
            required_migrations=({"id": "m1", "checksum": "a" * 64},),
        )
        config = SimpleNamespace(role="test", environment="test")
        plan = ReleasePlan(manifest.archive_digest, "e" * 64, (), "f" * 64, {}, "1" * 64)
        store = SimpleNamespace(
            bootstrap=lambda *a, **kw: events.append("bootstrap"),
            read_history=lambda *a: {"m1": {"status": "REVERTED", "checksum": "a" * 64}},
        )
        control = SimpleNamespace(setup_state=lambda *a: events.append("setup-control"))
        context = ReleaseApplyContext(
            manifest=manifest, target_document={"role": "test", "environment": "test"},
            config=config, metadata=metadata, migration_store=store, control_store=control,
            schema_set_digest="d" * 64, app_targets=(),
        )
        with patch("teamlib.release_adapter._validated_release_context", return_value=context), \
             patch("teamlib.release_adapter.plan_release", return_value=plan), \
             patch("teamlib.release_adapter._apply_release_context") as applied_context:
            with self.assertRaisesRegex(ReleaseAdapterError, "required migration unavailable"):
                apply_verified_release_live(
                    self.context_file("release.tar"),
                    self.context_file("targets/test.json"),
                    config,
                    repo=self.context_file("repo"),
                )
        self.assertEqual(events, ["bootstrap"])
        applied_context.assert_not_called()


if __name__ == "__main__":
    unittest.main()
