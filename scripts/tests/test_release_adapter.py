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

from teamlib.config import Target
from teamlib.release import ApplyReport, ReleasePlan
from teamlib.release_adapter import ReleaseAdapterError, apply_verified_release, main


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
            return ApplyReport("planned", received_plan.pending, archive_digest="a" * 64)

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
            ]
            # The environment profile is absent, so main() must fail with the
            # diagnostic SystemExit -- not with NameError from a missing import.
            with self.assertRaises(SystemExit) as caught:
                main(argv)
            self.assertIn("environment profile file not found", str(caught.exception))


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


if __name__ == "__main__":
    unittest.main()
