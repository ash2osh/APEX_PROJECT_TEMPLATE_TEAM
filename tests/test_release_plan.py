from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import _test_subprocess_safety  # noqa: F401
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
from scripts import feature_cli, release_plan, rollout


GIT = [
    "git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
    "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false",
]
CHECKS = {
    "schemaVersion": 1,
    "preconditions": [],
    "postconditions": [{"id": "ready", "sql": "SELECT 1 FROM dual", "expected": 1}],
}
DDL = "CREATE TABLE APP_CODE.DISPATCH (ID NUMBER);\n"
DDL_CHANGED = "CREATE TABLE APP_CODE.DISPATCH (ID NUMBER, NOTE VARCHAR2(10));\n"
PKS = "CREATE OR REPLACE PACKAGE item_lookup AS\n  PROCEDURE p;\nEND item_lookup;\n/\n"
PKB = "CREATE OR REPLACE PACKAGE BODY item_lookup AS\n  PROCEDURE p IS BEGIN NULL; END p;\nEND item_lookup;\n/\n"
PKB_CHANGED = "CREATE OR REPLACE PACKAGE BODY item_lookup AS\n  PROCEDURE p IS BEGIN NULL; NULL; END p;\nEND item_lookup;\n/\n"
TRG = "CREATE OR REPLACE TRIGGER orders_trg BEFORE INSERT ON orders FOR EACH ROW BEGIN NULL; END;\n/\n"
TRG_CHANGED = "CREATE OR REPLACE TRIGGER orders_trg BEFORE INSERT ON orders FOR EACH ROW BEGIN NULL; NULL; END;\n/\n"
DATA = "INSERT INTO APP_CODE.DISPATCH (ID) VALUES (1);\n"
MIGRATION_SQL = "CREATE TABLE APP_CODE.LEGACY_T (ID NUMBER);\n"
MIGRATION = "migrations/2026-01-05_add-legacy-r001"
APP_FILE = "apps/APP_CODE/100/application.apx"


class ReleasePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.git("init", "-q")
        self.git("config", "core.autocrlf", "false")
        self.write("features/dispatch/APP_CODE/ddl.sql", DDL)
        self.write("features/dispatch/APP_CODE/checks.json", json.dumps(CHECKS) + "\n")
        self.write("features/dispatch/APP_CODE/packages/item_lookup.pks.sql", PKS)
        self.write("features/dispatch/APP_CODE/packages/item_lookup.pkb.sql", PKB)
        self.write("features/dispatch/APP_CODE/triggers/orders_trg.trg.sql", TRG)
        self.write("features/dispatch/APP_CODE/data/seed.sql", DATA)
        self.write(f"{MIGRATION}/001-create-table.sql", MIGRATION_SQL)
        self.write(f"{MIGRATION}/checks.json", json.dumps(CHECKS) + "\n")
        self.write(APP_FILE, "application export\n")
        self.write("apps/APP_CODE/100/deployments/staging.json", "{}\n")
        self.base = self.commit("baseline")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    # helpers
    def git(self, *args: str) -> str:
        return subprocess.run(
            [*GIT, "-C", str(self.root), *args], capture_output=True, check=True,
        ).stdout.decode("utf-8").strip()

    def write(self, relative: str, text: str) -> None:
        path = self.root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def remove(self, relative: str) -> None:
        self.root.joinpath(*relative.split("/")).unlink()

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = feature_cli.main(["release-plan", *args], repo_root=self.root)
        return code, out.getvalue(), err.getvalue()

    def output(self) -> Path:
        return self.root / "releases" / "current.json"

    def step_types(self, plan) -> list[str]:
        return [step["type"] for step in plan.steps]

    # baseline selection
    def test_since_ref_plans_only_committed_changes_after_it(self) -> None:
        self.write("features/dispatch/APP_CODE/ddl.sql", DDL_CHANGED)
        self.commit("change ddl")
        plan = release_plan.create_release_plan(self.root, since=self.base)
        self.assertEqual(plan.steps, ({"type": "feature-migrate", "schema": "APP_CODE",
                                       "inputs": ["features/dispatch/APP_CODE/ddl.sql"]},))
        self.assertTrue(plan.written)
        document = json.loads(self.output().read_text(encoding="utf-8"))
        self.assertEqual(document["schemaVersion"], 2)
        self.assertEqual(document["databaseObjectBudgetSeconds"], 120)
        self.assertEqual(document["steps"], list(plan.steps))

    def test_default_baseline_is_the_newest_release_tag(self) -> None:
        self.git("tag", "release/2026-11-20-r001", self.base)
        self.write("features/dispatch/APP_CODE/ddl.sql", DDL_CHANGED)
        self.commit("change ddl")
        plan = release_plan.create_release_plan(self.root)
        self.assertEqual(plan.baseline, "release/2026-11-20-r001")
        self.assertEqual(self.step_types(plan), ["feature-migrate"])

    def test_no_release_tag_and_no_all_refuses_and_names_both_options(self) -> None:
        with self.assertRaises(release_plan.ReleasePlanError) as raised:
            release_plan.create_release_plan(self.root)
        self.assertIn("--since REF", str(raised.exception))
        self.assertIn("--all", str(raised.exception))
        code, _out, err = self.cli()
        self.assertEqual(code, 2)
        self.assertIn("--since REF", err)
        self.assertFalse(self.output().exists())

    def test_since_and_all_are_mutually_exclusive(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            code, _out, _err = self.cli("--since", self.base, "--all")
        self.assertEqual(code, 2)
        self.assertFalse(self.output().exists())

    def test_since_refuses_a_ref_that_is_not_a_commit(self) -> None:
        with self.assertRaises(release_plan.ReleasePlanError):
            release_plan.create_release_plan(self.root, since="no-such-ref")

    # ordering and selection
    def test_all_orders_ddl_units_triggers_data_migrate_and_app_deploy(self) -> None:
        plan = release_plan.create_release_plan(self.root, include_all=True)
        self.assertEqual(plan.steps, (
            {"type": "feature-migrate", "schema": "APP_CODE", "inputs": ["features/dispatch/APP_CODE/ddl.sql"]},
            {"type": "feature-migrate", "schema": "APP_CODE", "inputs": [
                "features/dispatch/APP_CODE/packages/item_lookup.pks.sql",
                "features/dispatch/APP_CODE/packages/item_lookup.pkb.sql",
            ]},
            {"type": "feature-migrate", "schema": "APP_CODE", "inputs": [
                "features/dispatch/APP_CODE/triggers/orders_trg.trg.sql",
            ]},
            {"type": "sql-script", "file": "features/dispatch/APP_CODE/data/seed.sql", "schema": "APP_CODE"},
            {"type": "migrate", "folders": [MIGRATION]},
            {"type": "app-deploy", "appId": 100},
        ))
        self.assertIn("first release", plan.baseline)

    def test_a_body_change_adds_its_specification_and_triggers_are_a_separate_step(self) -> None:
        self.write("features/dispatch/APP_CODE/packages/item_lookup.pkb.sql", PKB_CHANGED)
        self.write("features/dispatch/APP_CODE/triggers/orders_trg.trg.sql", TRG_CHANGED)
        self.commit("change body and trigger")
        plan = release_plan.create_release_plan(self.root, since=self.base)
        self.assertEqual([step["inputs"] for step in plan.steps if step["type"] == "feature-migrate"], [
            ["features/dispatch/APP_CODE/packages/item_lookup.pks.sql", "features/dispatch/APP_CODE/packages/item_lookup.pkb.sql"],
            ["features/dispatch/APP_CODE/triggers/orders_trg.trg.sql"],
        ])

    def test_data_script_and_migration_folder_changes_plan_their_steps(self) -> None:
        self.write("features/dispatch/APP_CODE/data/seed.sql", DATA + "COMMIT;\n")
        self.write("migrations/2026-02-01_add-index-r001/001-create-index.sql", "CREATE INDEX APP_CODE.IX1 ON APP_CODE.DISPATCH (ID);\n")
        self.write("migrations/2026-02-01_add-index-r001/checks.json", json.dumps(CHECKS) + "\n")
        self.commit("data and migration")
        plan = release_plan.create_release_plan(self.root, since=self.base)
        self.assertEqual(plan.steps, (
            {"type": "sql-script", "file": "features/dispatch/APP_CODE/data/seed.sql", "schema": "APP_CODE"},
            {"type": "migrate", "folders": ["migrations/2026-02-01_add-index-r001"]},
        ))

    def test_evidence_only_migration_folder_change_is_ignored(self) -> None:
        self.write(f"{MIGRATION}/status.dev.json", "{}\n")
        self.write(f"{MIGRATION}/README.md", "notes\n")
        self.commit("evidence only")
        plan = release_plan.create_release_plan(self.root, since=self.base)
        self.assertTrue(plan.empty)
        self.assertFalse(self.output().exists())

    def test_resolved_migration_folder_is_excluded_and_reported(self) -> None:
        self.write(f"{MIGRATION}/resolution.dev.json", "{}\n")
        self.write("migrations/2026-02-01_add-index-r001/001-create-index.sql", "CREATE INDEX APP_CODE.IX1 ON APP_CODE.DISPATCH (ID);\n")
        self.write("migrations/2026-02-01_add-index-r001/checks.json", json.dumps(CHECKS) + "\n")
        self.commit("resolution and a new folder")
        plan = release_plan.create_release_plan(self.root, since=self.base)
        self.assertEqual(plan.steps, ({"type": "migrate", "folders": ["migrations/2026-02-01_add-index-r001"]},))
        self.assertEqual([folder for folder, _reason in plan.excluded], [MIGRATION])
        self.assertIn("resolution.dev.json", plan.excluded[0][1])
        _code, out, _err = self.cli("--since", self.base, "--force")
        self.assertIn("Excluded for review:", out)

    def test_deployments_only_app_change_is_ignored(self) -> None:
        self.write("apps/APP_CODE/100/deployments/staging.json", '{"changed": true}\n')
        self.commit("descriptor only")
        plan = release_plan.create_release_plan(self.root, since=self.base)
        self.assertTrue(plan.empty)

    def test_app_source_change_plans_one_app_deploy_with_an_integer_id(self) -> None:
        self.write(APP_FILE, "changed export\n")
        self.commit("app source")
        plan = release_plan.create_release_plan(self.root, since=self.base)
        self.assertEqual(plan.steps, ({"type": "app-deploy", "appId": 100},))
        self.assertIsInstance(plan.steps[0]["appId"], int)

    # output and manifest rules
    def test_empty_plan_writes_nothing_and_exits_zero(self) -> None:
        code, out, _err = self.cli("--since", self.base)
        self.assertEqual(code, 0)
        self.assertIn(f"nothing changed since {self.base}", out)
        self.assertFalse(self.output().exists())

    def test_existing_output_is_refused_without_force_and_replaced_with_it(self) -> None:
        self.write("features/dispatch/APP_CODE/ddl.sql", DDL_CHANGED)
        self.commit("change ddl")
        release_plan.create_release_plan(self.root, since=self.base)
        before = self.output().read_bytes()
        self.write("features/dispatch/APP_CODE/packages/item_lookup.pkb.sql", PKB_CHANGED)
        self.commit("change body")
        with self.assertRaises(release_plan.ReleasePlanError) as raised:
            release_plan.create_release_plan(self.root, since=self.base)
        self.assertIn("--force", str(raised.exception))
        self.assertEqual(self.output().read_bytes(), before)
        code, _out, _err = self.cli("--since", self.base, "--force")
        self.assertEqual(code, 0)
        self.assertNotEqual(self.output().read_bytes(), before)

    def test_output_outside_the_repository_is_refused(self) -> None:
        self.write("features/dispatch/APP_CODE/ddl.sql", DDL_CHANGED)
        self.commit("change ddl")
        outside = Path(self.temporary.name).parent / "release-plan-outside.json"
        with self.assertRaises(release_plan.ReleasePlanError):
            release_plan.create_release_plan(self.root, since=self.base, output=outside)
        self.assertFalse(outside.exists())

    def test_written_manifest_passes_the_rollout_loader_and_is_lf_with_a_trailing_newline(self) -> None:
        plan = release_plan.create_release_plan(self.root, include_all=True)
        raw = self.output().read_bytes()
        self.assertTrue(raw.endswith(b"}\n"))
        self.assertNotIn(b"\r", raw)
        self.assertEqual(raw, (json.dumps(json.loads(raw), ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        manifest = rollout.load_manifest(self.output(), self.root, version=2)
        self.assertEqual(len(manifest.steps), len(plan.steps))

    def test_a_manifest_the_loader_refuses_is_not_written(self) -> None:
        self.remove("features/dispatch/APP_CODE/checks.json")
        self.write("features/dispatch/APP_CODE/ddl.sql", DDL_CHANGED)
        self.commit("change ddl without checks")
        with self.assertRaises(release_plan.ReleasePlanError) as raised:
            release_plan.create_release_plan(self.root, since=self.base)
        self.assertIn("rollout loader", str(raised.exception))
        self.assertFalse(self.output().exists())

    def test_json_summary_lists_counts_and_steps(self) -> None:
        self.write("features/dispatch/APP_CODE/ddl.sql", DDL_CHANGED)
        self.commit("change ddl")
        code, out, _err = self.cli("--since", self.base, "--format", "json")
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual(summary["stepCounts"], {"feature-migrate": 1})
        self.assertTrue(summary["written"])
        self.assertEqual(summary["output"], "releases/current.json")

    # uncommitted work
    def test_uncommitted_changes_are_warned_about_and_not_included(self) -> None:
        self.write("features/dispatch/APP_CODE/packages/item_lookup.pkb.sql", PKB_CHANGED)
        self.commit("committed body")
        self.write("features/dispatch/APP_CODE/ddl.sql", DDL_CHANGED)
        plan = release_plan.create_release_plan(self.root, since=self.base)
        self.assertEqual(plan.uncommitted, ("features/dispatch/APP_CODE/ddl.sql",))
        self.assertEqual(
            [step["inputs"] for step in plan.steps],
            [["features/dispatch/APP_CODE/packages/item_lookup.pks.sql", "features/dispatch/APP_CODE/packages/item_lookup.pkb.sql"]],
        )
        text = release_plan.render(plan)
        self.assertIn("Warning: 1 uncommitted path(s)", text)
        self.assertIn("scripts/team.sh feature freeze releases/current.json", text)


if __name__ == "__main__":
    unittest.main()
