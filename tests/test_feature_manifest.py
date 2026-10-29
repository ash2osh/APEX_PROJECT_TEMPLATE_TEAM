from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.migration_checks import analyze_batch, compiled_units


def checks(check_id: str = "ready") -> dict:
    return {
        "schemaVersion": 1,
        "preconditions": [],
        "postconditions": [{"id": check_id, "sql": "SELECT 1 FROM dual", "expected": 1}],
    }


class FeatureManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        try:
            self.feature_manifest = importlib.import_module("scripts.feature_manifest")
        except ImportError as error:
            self.fail(f"feature manifest validation is not implemented: {error}")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def add_feature(
        self,
        slug: str,
        source: str | bytes,
        *,
        schema: str = "APP_CODE",
        filename: str = "ddl.sql",
        check_document: dict | None = None,
    ) -> str:
        base = self.root / "features" / slug / schema
        path = base / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source if isinstance(source, bytes) else source.encode("utf-8"))
        if check_document is not False:
            document = checks() if check_document is None else check_document
            (base / "checks.json").write_text(json.dumps(document) + "\n", encoding="utf-8")
        return path.relative_to(self.root).as_posix()

    def write_check_file(self, slug: str, filename: str, document: dict, *, schema: str = "APP_CODE") -> str:
        path = self.root / "features" / slug / schema / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        return path.relative_to(self.root).as_posix()

    def step(self, inputs: list[str], *, schema: str = "APP_CODE", **extra) -> dict:
        return {"type": "feature-migrate", "schema": schema, "inputs": inputs, **extra}

    def load(self, step: dict):
        return self.feature_manifest.load_feature_checkpoint(self.root, step)

    def test_feature_inputs_preserve_manifest_order_and_namespace_default_checks(self) -> None:
        beta = self.add_feature("beta", "CREATE TABLE APP_CODE.BETA (ID NUMBER);\n")
        alpha = self.add_feature("alpha", "CREATE TABLE APP_CODE.ALPHA (ID NUMBER);\n")

        checkpoint = self.load(self.step([beta, alpha]))

        self.assertEqual([item.path for item in checkpoint.inputs], [beta, alpha])
        self.assertEqual(
            [check.id for check in checkpoint.postconditions],
            ["beta-checks-ready", "alpha-checks-ready"],
        )
        self.assertEqual(checkpoint.schema, "APP_CODE")

    def test_explicit_check_files_replace_default_and_keep_file_namespace(self) -> None:
        source = self.add_feature("dispatch", "CREATE TABLE APP_CODE.DISPATCH (ID NUMBER);\n")
        explicit = self.write_check_file("dispatch", "checks/after-stage.json", checks("staged"))

        checkpoint = self.load(self.step([source], checks=[explicit]))

        self.assertEqual([check.id for check in checkpoint.postconditions], ["dispatch-after-stage-staged"])
        self.assertNotIn("dispatch-checks-ready", [check.id for check in checkpoint.postconditions])

    def test_feature_checkpoint_classifies_complete_stored_source_and_in_memory_adapter(self) -> None:
        package = (
            'CREATE OR REPLACE PACKAGE "APP_CODE"."DISPATCH_PKG" IS\n'
            "  PROCEDURE RUN;\n"
            "END DISPATCH_PKG;\n/\n"
        )
        path = self.add_feature("dispatch", package, filename="packages/dispatch_pkg.pks.sql")

        checkpoint = self.load(self.step([path]))
        migration = self.feature_manifest.analysis_migration(checkpoint)

        self.assertEqual(checkpoint.inputs[0].category, "stored-source")
        self.assertEqual(checkpoint.inputs[0].unit, ("APP_CODE", "PACKAGE", "DISPATCH_PKG"))
        self.assertEqual(compiled_units(migration, "APP_CODE"), (("APP_CODE", "PACKAGE", "DISPATCH_PKG", True),))
        self.assertEqual(analyze_batch([migration], "APP_CODE")[0]["kind"], "OPAQUE")
        self.assertFalse((self.root / "migrations").exists(), "analysis adapter must not create dated migrations")

    def test_only_validated_compiler_setting_prefixes_may_precede_stored_source(self) -> None:
        package = (
            "ALTER SESSION SET PLSQL_OPTIMIZE_LEVEL = 2;\n"
            "ALTER SESSION SET PLSQL_CODE_TYPE = INTERPRETED;\n"
            "ALTER SESSION SET PLSQL_CCFLAGS = 'FEATURE_X:TRUE';\n"
            "CREATE OR REPLACE PACKAGE BODY APP_CODE.DISPATCH_PKG IS\n"
            "  PROCEDURE RUN IS BEGIN NULL; END;\n"
            "END DISPATCH_PKG;\n/\n"
        )
        path = self.add_feature("dispatch", package, filename="packages/dispatch_pkg.pkb.sql")

        checkpoint = self.load(self.step([path]))

        self.assertEqual(checkpoint.inputs[0].category, "stored-source")
        self.assertEqual(checkpoint.inputs[0].unit, ("APP_CODE", "PACKAGE BODY", "DISPATCH_PKG"))

    def test_statically_analyzable_ddl_is_classified_for_the_selected_schema(self) -> None:
        path = self.add_feature("dispatch", "CREATE TABLE APP_CODE.DISPATCH_QUEUE (ID NUMBER);\n")

        checkpoint = self.load(self.step([path]))
        migration = self.feature_manifest.analysis_migration(checkpoint)
        operations = analyze_batch([migration], "APP_CODE")

        self.assertEqual(checkpoint.inputs[0].category, "ddl")
        self.assertTrue(operations[0]["supported"])
        self.assertEqual(operations[0]["owner"], "APP_CODE")

    def test_static_object_grant_is_allowed_only_for_the_checkpoint_schema(self) -> None:
        path = self.add_feature("dispatch", "GRANT SELECT ON APP_CODE.DISPATCH_QUEUE TO APP_RUNTIME;\n")

        checkpoint = self.load(self.step([path]))

        self.assertEqual(checkpoint.inputs[0].category, "ddl")
        self.assertEqual(checkpoint.object_keys, (("APP_CODE", "DISPATCH_QUEUE"),))

        wrong_owner = self.add_feature("other-grant", "GRANT SELECT ON OTHER.DISPATCH_QUEUE TO APP_RUNTIME;\n")
        with self.assertRaises(self.feature_manifest.FeatureManifestError):
            self.load(self.step([wrong_owner]))

    def test_missing_default_checks_refuse_the_selected_feature(self) -> None:
        path = self.add_feature("dispatch", "CREATE TABLE APP_CODE.DISPATCH (ID NUMBER);\n", check_document=False)

        with self.assertRaises(self.feature_manifest.FeatureManifestError):
            self.load(self.step([path]))

    def test_ddl_without_nonempty_postcondition_checks_is_refused(self) -> None:
        path = self.add_feature(
            "dispatch",
            "CREATE TABLE APP_CODE.DISPATCH (ID NUMBER);\n",
            check_document={"schemaVersion": 1, "preconditions": [], "postconditions": []},
        )

        with self.assertRaises(self.feature_manifest.FeatureManifestError):
            self.load(self.step([path]))

    def test_explicit_check_list_must_be_nonempty(self) -> None:
        path = self.add_feature("dispatch", "CREATE TABLE APP_CODE.DISPATCH (ID NUMBER);\n")

        with self.assertRaisesRegex(self.feature_manifest.FeatureManifestError, "checks must be a nonempty"):
            self.load(self.step([path], checks=[]))

    def test_duplicate_stored_unit_identity_across_features_is_refused(self) -> None:
        source = "CREATE OR REPLACE PACKAGE APP_CODE.SHARED_PKG IS END SHARED_PKG;\n/\n"
        first = self.add_feature("first", source, filename="packages/shared.pks.sql")
        second = self.add_feature("second", source, filename="packages/shared.pks.sql")

        with self.assertRaisesRegex(self.feature_manifest.FeatureManifestError, "duplicate"):
            self.load(self.step([first, second]))

    def test_release_rejects_duplicate_units_across_checkpoints_but_keeps_distinct_identities(self) -> None:
        package = "CREATE OR REPLACE PACKAGE APP_CODE.SHARED_PKG IS PROCEDURE RUN; END SHARED_PKG;\n/\n"
        changed = "CREATE OR REPLACE PACKAGE APP_CODE.SHARED_PKG IS PROCEDURE STOP; END SHARED_PKG;\n/\n"
        first = self.add_feature("first", package, filename="packages/shared.pks.sql")
        same_bytes_other_path = self.add_feature("second", package, filename="packages/shared.pks.sql")
        changed_bytes_other_path = self.add_feature("third", changed, filename="packages/shared.pks.sql")

        for second_path in (same_bytes_other_path, changed_bytes_other_path, first):
            with self.subTest(second_path=second_path):
                checkpoints = (self.load(self.step([first])), self.load(self.step([second_path])))
                with self.assertRaisesRegex(self.feature_manifest.FeatureManifestError, "duplicate"):
                    self.feature_manifest.validate_release_units(checkpoints)

        body = self.add_feature(
            "body", "CREATE OR REPLACE PACKAGE BODY APP_CODE.SHARED_PKG IS PROCEDURE RUN IS BEGIN NULL; END; END SHARED_PKG;\n/\n",
            filename="packages/shared.pkb.sql",
        )
        other_owner = self.add_feature(
            "other-owner", "CREATE OR REPLACE PACKAGE APP_DATA.SHARED_PKG IS PROCEDURE RUN; END SHARED_PKG;\n/\n",
            schema="APP_DATA", filename="packages/shared.pks.sql",
        )
        quoted_mixed = self.add_feature(
            "quoted-mixed", 'CREATE OR REPLACE PACKAGE APP_CODE."Mixed_Pkg" IS END "Mixed_Pkg";\n/\n',
            filename="packages/mixed.pks.sql",
        )
        quoted_upper = self.add_feature(
            "quoted-upper", 'CREATE OR REPLACE PACKAGE APP_CODE."MIXED_PKG" IS END "MIXED_PKG";\n/\n',
            filename="packages/mixed.pks.sql",
        )
        allowed = (
            self.load(self.step([first])),
            self.load(self.step([body])),
            self.load(self.step([other_owner], schema="APP_DATA")),
            self.load(self.step([quoted_mixed])),
            self.load(self.step([quoted_upper])),
        )
        self.feature_manifest.validate_release_units(allowed)

    def test_stored_unit_owner_must_match_the_step_schema(self) -> None:
        path = self.add_feature(
            "dispatch",
            "CREATE OR REPLACE PACKAGE OTHER.DISPATCH_PKG IS END DISPATCH_PKG;\n/\n",
            filename="packages/dispatch_pkg.pks.sql",
        )

        with self.assertRaises(self.feature_manifest.FeatureManifestError):
            self.load(self.step([path]))

    def test_unknown_fields_traversal_and_non_feature_inputs_are_refused(self) -> None:
        path = self.add_feature("dispatch", "CREATE TABLE APP_CODE.DISPATCH (ID NUMBER);\n")

        for step in (
            self.step([path], typo=True),
            self.step(["features/../outside.sql"]),
            self.step(["scripts/maintenance.sql"]),
        ):
            with self.subTest(step=step), self.assertRaises(self.feature_manifest.FeatureManifestError):
                self.load(step)

    def test_feature_input_rejects_symlink_escape(self) -> None:
        self.add_feature("dispatch", "CREATE TABLE APP_CODE.DISPATCH (ID NUMBER);\n")
        outside = self.root / "outside.sql"
        outside.write_text("CREATE TABLE APP_CODE.OUTSIDE (ID NUMBER);\n", encoding="utf-8")
        link = self.root / "features" / "dispatch" / "APP_CODE" / "linked.sql"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"symlinks are unavailable: {error}")
        relative_link = link.relative_to(self.root).as_posix()

        with self.assertRaises(self.feature_manifest.FeatureManifestError):
            self.load(self.step([relative_link]))

    def test_malformed_utf8_and_crlf_feature_sources_are_refused(self) -> None:
        invalid_utf8 = self.add_feature("invalid-utf8", b"CREATE TABLE APP_CODE.BAD (\xff);\n")
        crlf = self.add_feature("crlf", b"CREATE TABLE APP_CODE.BAD (ID NUMBER);\r\n")

        for path in (invalid_utf8, crlf):
            with self.subTest(path=path), self.assertRaises(self.feature_manifest.FeatureManifestError):
                self.load(self.step([path]))

    def test_runtime_dml_dynamic_blocks_and_schema_wide_recompile_are_refused(self) -> None:
        rejected = (
            "INSERT INTO APP_CODE.DISPATCH_QUEUE (ID) VALUES (1);\n",
            "BEGIN\n  EXECUTE IMMEDIATE 'CREATE TABLE APP_CODE.X (ID NUMBER)';\nEND;\n/\n",
            "BEGIN\n  DBMS_UTILITY.COMPILE_SCHEMA(schema => 'APP_CODE');\nEND;\n/\n",
            "HOST touch /tmp/feature-manifest-injection\n",
        )
        for index, source in enumerate(rejected):
            path = self.add_feature(f"unsafe-{index}", source)
            with self.subTest(source=source), self.assertRaises(self.feature_manifest.FeatureManifestError):
                self.load(self.step([path]))


if __name__ == "__main__":
    unittest.main()
