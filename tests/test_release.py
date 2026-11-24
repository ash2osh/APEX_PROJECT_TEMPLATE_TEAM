"""Offline tests for scripts/release.py using fake_sqlcl (no live database)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _test_subprocess_safety  # noqa: F401
import fake_sqlcl

from scripts import release


BASE_ENV = {
    "PROJECT_NAME": "DEMO",
    "DEVELOPER_NAME": "ASHARIF",
    "DB_ENVIRONMENT": "development",
    "APEX_APP_ID": "130",
    "TABLES_SCHEMA": "APP_DATA",
    "TABLES_PREFIXES": "APP_",
    "TABLES_SQLCL_CONNECTION": "dev_conn",
    "TABLES_EXPECTED_USER": "APP_DATA",
    "CODE_SCHEMA": "APP_CODE",
    "CODE_PREFIXES": "APP_",
    "CODE_SQLCL_CONNECTION": "dev_conn",
    "CODE_EXPECTED_USER": "APP_CODE",
    "APEX_PARSING_SCHEMA": "APP_CODE",
    "APEX_SQLCL_CONNECTION": "dev_conn",
    "APEX_EXPECTED_USER": "APP_CODE",
    "STAGING_SCHEMA": "APP_CODE",
    "STAGING_SQLCL_CONNECTION": "stage_conn",
    "STAGING_EXPECTED_USER": "APP_LOGIN",
    "PROD_SCHEMA": "APP_CODE",
    "PROD_SQLCL_CONNECTION": "prod_conn",
    "PROD_EXPECTED_USER": "APP_LOGIN",
}

TARGET_PKG_SOURCE = (
    "PACKAGE BODY ORDERS_PKG AS\n"
    "  FUNCTION get_count RETURN NUMBER IS BEGIN RETURN 1; END;\n"
    "END ORDERS_PKG;\n"
)
DEV_PKG_SOURCE = (
    "PACKAGE BODY ORDERS_PKG AS\n"
    "  FUNCTION get_count RETURN NUMBER IS BEGIN RETURN 2; END;\n"
    "END ORDERS_PKG;\n"
)


def _write_fake_sqlcl(bin_dir: Path, state_file: Path) -> None:
    """Install a fake `sql` script that responds to release.py drivers using JSON state."""
    script = r"""#!/usr/bin/env bash
set -euo pipefail
conn=""
driver=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -name) conn="$2"; shift 2 ;;
    @*) driver="${1#@}"; shift ;;
    *) shift ;;
  esac
done
python3 - "$conn" "$driver" "__STATE_FILE__" <<'PYEOF'
import json
import re
import sys
from pathlib import Path

conn, driver_path, state_path = sys.argv[1], sys.argv[2], sys.argv[3]
driver = Path(driver_path).read_text(encoding="utf-8")
state = json.loads(Path(state_path).read_text(encoding="utf-8"))
state.setdefault("calls", []).append({"conn": conn, "driverName": Path(driver_path).name, "driver": driver})
Path(state_path).write_text(json.dumps(state), encoding="utf-8")

env_key = state["connMap"][conn]
env_state = state["envs"][env_key]
identity = env_state["identity"]

if "RELEASE_FETCH_BEGIN" in driver:
    items = []
    # Extract queried (objectType, objectName) pairs from driver
    pairs = re.findall(
        r"l_item\.put\('objectType', '([^']+)'\);\s*l_item\.put\('objectName', '([^']+)'\);",
        driver,
    )
    for obj_type, obj_name in pairs:
        rec = env_state.get("objects", {}).get(f"{obj_type}:{obj_name}")
        if rec is None:
            items.append({"objectType": obj_type, "objectName": obj_name, "exists": False, "source": ""})
        else:
            entry = {"objectType": obj_type, "objectName": obj_name, "exists": True, "source": rec.get("source", "")}
            if "ddl" in rec:
                entry["ddl"] = rec["ddl"]
            items.append(entry)
    payload = {"identity": identity, "objects": items}
    print("RELEASE_FETCH_BEGIN")
    print(json.dumps(payload))
    print("RELEASE_FETCH_END")
    print("RELEASE_FETCH_VERIFIED")
    sys.exit(0)

if "RELEASE_BEFORE_INVALID_BEGIN" in driver and "RELEASE_POST_APPLY_BEGIN" in driver:
    if env_state.get("failDdl"):
        print("ORA-00955: name is already used by an existing object")
        sys.exit(1)
    before_payload = {"identity": identity, "invalids": env_state.get("beforeInvalids", [])}
    post_payload = {"errors": env_state.get("codeErrors", []), "invalids": env_state.get("afterInvalids", [])}
    print("RELEASE_BEFORE_INVALID_BEGIN")
    print(json.dumps(before_payload))
    print("RELEASE_BEFORE_INVALID_END")
    print("RELEASE_BEFORE_INVALID_VERIFIED")
    print("RELEASE_POST_APPLY_BEGIN")
    print(json.dumps(post_payload))
    print("RELEASE_POST_APPLY_END")
    print("RELEASE_POST_APPLY_VERIFIED")
    sys.exit(0)

if "RELEASE_BEFORE_INVALID_BEGIN" in driver:
    before_payload = {"identity": identity, "invalids": env_state.get("beforeInvalids", [])}
    print("RELEASE_BEFORE_INVALID_BEGIN")
    print(json.dumps(before_payload))
    print("RELEASE_BEFORE_INVALID_END")
    print("RELEASE_BEFORE_INVALID_VERIFIED")
    sys.exit(0)

if "RELEASE_BATCH_APPLIED" in driver:
    if env_state.get("failDdl"):
        print("ORA-00955: name is already used by an existing object")
        sys.exit(1)
    print("RELEASE_BATCH_APPLIED")
    sys.exit(0)

if "RELEASE_POST_APPLY_BEGIN" in driver:
    post_payload = {"errors": env_state.get("codeErrors", []), "invalids": env_state.get("afterInvalids", [])}
    print("RELEASE_POST_APPLY_BEGIN")
    print(json.dumps(post_payload))
    print("RELEASE_POST_APPLY_END")
    print("RELEASE_POST_APPLY_VERIFIED")
    sys.exit(0)

if "RELEASE_VERIFY_OK" in driver:
    if env_state.get("failVerify"):
        print("ORA-20001: smoke check failed")
        sys.exit(1)
    print("RELEASE_VERIFY_OK")
    sys.exit(0)

print("Unexpected driver", file=sys.stderr)
sys.exit(2)
PYEOF
""".replace("__STATE_FILE__", state_file.as_posix())
    fake_sqlcl.install(bin_dir, script)


class ReleaseBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.scripts_dir = self.root / "scripts"
        self.scripts_dir.mkdir(parents=True)
        # sqlcl_session.run_sqlcl locates sqlcl_session.sh and sqlcl_safe.sh beside itself,
        # so fake_sqlcl uses the real scripts from the repo via release.run_sqlcl.
        self.bin_dir = self.root / "fake_bin"
        self.state_file = self.root / "fake_state.json"
        self.default_state = {
            "connMap": {"dev_conn": "dev", "stage_conn": "staging", "prod_conn": "prod"},
            "envs": {
                "dev": {
                    "identity": {
                        "session_user": "APP_CODE",
                        "current_schema": "APP_CODE",
                        "db_name": "DEVDB",
                        "db_unique_name": "DEVDB_UNQ",
                        "service_name": "dev_svc",
                        "con_name": "DEVPDB",
                    },
                    "objects": {
                        "PACKAGE BODY:ORDERS_PKG": {
                            "source": DEV_PKG_SOURCE,
                            "ddl": "ALTER SESSION SET PLSQL_CODE_TYPE = INTERPRETED;\nCREATE OR REPLACE " + DEV_PKG_SOURCE,
                        },
                        "VIEW:ORDERS_V": {
                            "source": "",
                            "ddl": "ALTER SESSION SET PLSQL_CODE_TYPE = INTERPRETED;\nCREATE OR REPLACE FORCE VIEW APP_CODE.ORDERS_V AS SELECT 1 AS ID FROM DUAL;",
                        },
                    },
                    "beforeInvalids": [],
                    "afterInvalids": [],
                    "codeErrors": [],
                },
                "staging": {
                    "identity": {
                        "session_user": "APP_LOGIN",
                        "current_schema": "APP_CODE",
                        "db_name": "STAGEDB",
                        "db_unique_name": "STAGEDB_UNQ",
                        "service_name": "stage_svc",
                        "con_name": "STAGEPDB",
                    },
                    "objects": {
                        "PACKAGE BODY:ORDERS_PKG": {"source": TARGET_PKG_SOURCE},
                    },
                    "beforeInvalids": [{"objectType": "VIEW", "objectName": "PREEXISTING_BROKEN_V"}],
                    "afterInvalids": [{"objectType": "VIEW", "objectName": "PREEXISTING_BROKEN_V"}],
                    "codeErrors": [],
                },
                "prod": {
                    "identity": {
                        "session_user": "APP_LOGIN",
                        "current_schema": "APP_CODE",
                        "db_name": "PRODDB",
                        "db_unique_name": "PRODDB_UNQ",
                        "service_name": "prod_svc",
                        "con_name": "PRODPDB",
                    },
                    "objects": {
                        "PACKAGE BODY:ORDERS_PKG": {"source": TARGET_PKG_SOURCE},
                    },
                    "beforeInvalids": [],
                    "afterInvalids": [],
                    "codeErrors": [],
                },
            },
        }
        self._write_state(self.default_state)
        _write_fake_sqlcl(self.bin_dir, self.state_file)

    def _write_state(self, state: dict) -> None:
        self.state_file.write_text(json.dumps(state), encoding="utf-8", newline="\n")

    def _read_state(self) -> dict:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def _env(self) -> dict[str, str]:
        return fake_sqlcl.environment(self.bin_dir, SQLCL_PERSISTENT_SESSIONS="0")

    def _run_cli(self, argv: list[str], *, app_deployer=None, environ: dict[str, str] | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        proc_env = self._env()
        kwargs: dict = {"environ": dict(BASE_ENV if environ is None else environ)}
        if app_deployer is not None:
            kwargs["app_deployer"] = app_deployer
        with (
            mock.patch.dict(os.environ, proc_env, clear=False),
            mock.patch("builtins.input", side_effect=AssertionError("release must never call input()")),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = release.main(["--repo-root", str(self.root), *argv], **kwargs)
        return code, out.getvalue(), err.getvalue()

    def test_new_and_list_draft_and_deployed(self) -> None:
        code, out, _ = self._run_cli(["new", "r01-orders"])
        self.assertEqual(code, 0, out)
        bundle = self.root / "releases" / "r01-orders"
        for sub in ("01-ddl", "02-code", "03-ords", "04-verify"):
            self.assertTrue((bundle / sub).is_dir())
        manifest = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "draft")
        self.assertEqual(manifest["slug"], "r01-orders")

        # Duplicate slug refused
        code, _, err = self._run_cli(["new", "r01-orders"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

        # Create a second draft and mark one deployed
        self.assertEqual(self._run_cli(["new", "r02-billing"])[0], 0)
        manifest2 = json.loads((self.root / "releases" / "r02-billing" / "release.json").read_text(encoding="utf-8"))
        manifest2["status"] = "deployed"
        manifest2["deployedAt"] = "2026-11-24T00:00:00Z"
        manifest2["deployedTarget"] = "PRODDB_UNQ"
        (self.root / "releases" / "r02-billing" / "release.json").write_text(json.dumps(manifest2), encoding="utf-8", newline="\n")

        code, out, _ = self._run_cli(["list", "--format", "text"])
        self.assertEqual(code, 0)
        self.assertIn("1- r01-orders", out)
        self.assertIn("- r02-billing", out)

        code, out_json, _ = self._run_cli(["list", "--format", "json"])
        self.assertEqual(code, 0)
        parsed = json.loads(out_json)
        self.assertEqual([d["slug"] for d in parsed["draft"]], ["r01-orders"])
        self.assertEqual([d["slug"] for d in parsed["deployed"]], ["r02-billing"])

    def test_collect_strips_plsql_code_type_and_computes_patch_hash(self) -> None:
        self.assertEqual(self._run_cli(["new", "patch-rel"])[0], 0)
        code, out, err = self._run_cli([
            "collect", "patch-rel", "--from", "dev", "--schema", "APP_CODE",
            "--object", "PACKAGE_BODY:ORDERS_PKG", "--mode", "patch", "--target-env", "prod",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("baseSha256=", out)

        bundle = self.root / "releases" / "patch-rel"
        code_file = bundle / "02-code" / "orders_pkg_package_body.sql"
        self.assertTrue(code_file.is_file())
        content = code_file.read_text(encoding="utf-8")
        self.assertNotIn("PLSQL_CODE_TYPE", content)
        self.assertIn("CREATE OR REPLACE PACKAGE BODY ORDERS_PKG", content)

        manifest = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        entry = manifest["code"][0]
        self.assertEqual(entry["mode"], "patch")
        self.assertEqual(entry["baseSha256"], release.compute_source_sha256(TARGET_PKG_SOURCE))
        self.assertEqual(manifest["schemas"], ["APP_CODE"])

        # Collect VIEW strips PLSQL_CODE_TYPE from DBMS_METADATA.GET_DDL too
        code, _, err = self._run_cli([
            "collect", "patch-rel", "--from", "dev", "--schema", "APP_CODE",
            "--object", "VIEW:ORDERS_V", "--mode", "new",
        ])
        self.assertEqual(code, 0, err)
        view_sql = (bundle / "02-code" / "orders_v_view.sql").read_text(encoding="utf-8")
        self.assertNotIn("PLSQL_CODE_TYPE", view_sql)
        self.assertIn("CREATE OR REPLACE FORCE VIEW", view_sql)

    def test_check_enforces_new_existence_allow_existing_patch_hash_and_forbidden_patterns(self) -> None:
        self.assertEqual(self._run_cli(["new", "chk-rel"])[0], 0)
        bundle = self.root / "releases" / "chk-rel"
        (bundle / "02-code" / "orders_pkg_package_body.sql").write_text(
            "CREATE OR REPLACE " + DEV_PKG_SOURCE + "/\n", encoding="utf-8", newline="\n"
        )
        manifest = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        manifest["schemas"] = ["APP_CODE"]
        manifest["code"] = [{
            "schema": "APP_CODE",
            "file": "02-code/orders_pkg_package_body.sql",
            "objectType": "PACKAGE BODY",
            "objectName": "ORDERS_PKG",
            "mode": "new",
        }]
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        # mode=new fails when object exists in staging
        code, out, _ = self._run_cli(["check", "chk-rel", "--env", "staging"])
        self.assertEqual(code, 1, out)
        self.assertIn("already exists in staging", out)

        # --allow-existing allows it
        code, out, _ = self._run_cli(["check", "chk-rel", "--env", "staging", "--allow-existing"])
        self.assertEqual(code, 0, out)

        # mode=patch mismatch writes unified diff and refuses
        manifest["code"][0]["mode"] = "patch"
        manifest["code"][0]["baseSha256"] = "0" * 64
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        code, out, _ = self._run_cli(["check", "chk-rel", "--env", "staging"])
        self.assertEqual(code, 1)
        self.assertIn("baseSha256 mismatch", out)
        diffs = list((self.root / "scratch" / "release-diffs").glob("*.diff"))
        self.assertEqual(len(diffs), 1)
        self.assertIn("--- live:staging/APP_CODE.ORDERS_PKG", diffs[0].read_text(encoding="utf-8"))

        # Matching baseSha256 passes
        manifest["code"][0]["baseSha256"] = release.compute_source_sha256(TARGET_PKG_SOURCE)
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        self.assertEqual(self._run_cli(["check", "chk-rel", "--env", "staging"])[0], 0)

        # forbiddenPatterns refusal
        manifest["forbiddenPatterns"] = ["ORDERS_*"]
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        code, _, err = self._run_cli(["check", "chk-rel", "--env", "staging"])
        self.assertEqual(code, 1)
        self.assertIn("forbidden pattern", err)

        # PLSQL_CODE_TYPE = INTERPRETED in SQL file refused
        manifest["forbiddenPatterns"] = []
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        (bundle / "02-code" / "orders_pkg_package_body.sql").write_text(
            "ALTER SESSION SET PLSQL_CODE_TYPE = INTERPRETED;\nCREATE OR REPLACE " + DEV_PKG_SOURCE + "/\n",
            encoding="utf-8",
            newline="\n",
        )
        code, _, err = self._run_cli(["check", "chk-rel", "--env", "staging"])
        self.assertEqual(code, 1)
        self.assertIn("PLSQL_CODE_TYPE", err)

    def test_run_staging_backup_invalid_delta_verify_and_confirm_target(self) -> None:
        self.assertEqual(self._run_cli(["new", "run-rel"])[0], 0)
        bundle = self.root / "releases" / "run-rel"
        (bundle / "01-ddl" / "001-add-col.sql").write_text("ALTER TABLE ORDERS ADD (NOTES VARCHAR2(50));\n", encoding="utf-8", newline="\n")
        (bundle / "02-code" / "orders_pkg_package_body.sql").write_text(
            "CREATE OR REPLACE " + DEV_PKG_SOURCE + "/\n", encoding="utf-8", newline="\n"
        )
        (bundle / "04-verify" / "001-smoke.sql").write_text("SELECT 1 FROM DUAL;\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1,
            "slug": "run-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [{"schema": "APP_CODE", "file": "01-ddl/001-add-col.sql"}],
            "code": [{
                "schema": "APP_CODE",
                "file": "02-code/orders_pkg_package_body.sql",
                "objectType": "PACKAGE BODY",
                "objectName": "ORDERS_PKG",
                "mode": "patch",
                "baseSha256": release.compute_source_sha256(TARGET_PKG_SOURCE),
            }],
            "ords": [],
            "verify": [{"schema": "APP_CODE", "file": "04-verify/001-smoke.sql"}],
            "apps": [130],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        # Wrong --confirm-target refuses before any backup or apply
        code, _, err = self._run_cli([
            "run", "run-rel", "--env", "staging", "--confirm-target", "WRONG_DB",
        ])
        self.assertEqual(code, 1)
        self.assertIn("--confirm-target", err)
        self.assertFalse((self.root / "scratch" / "backups").exists())

        # Successful staging run (ignores pre-existing invalid PREEXISTING_BROKEN_V!)
        deployed_apps: list[tuple[int, str, str]] = []

        def fake_deploy(repo_root: Path, app_id: int, env: str, confirm: str, _vals) -> None:
            deployed_apps.append((app_id, env, confirm))

        code, out, err = self._run_cli(
            ["run", "run-rel", "--env", "staging", "--confirm-target", "stagedb_unq"],
            app_deployer=fake_deploy,
        )
        self.assertEqual(code, 0, err)
        self.assertIn("succeeded", out)
        self.assertEqual(deployed_apps, [(130, "staging", "stagedb_unq")])

        # Backup created
        backups = list((self.root / "scratch" / "backups").glob("run-rel-staging-*"))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / "APP_CODE.PACKAGE_BODY.ORDERS_PKG.sql").is_file())

        # Report created
        reports = list((self.root / "scratch" / "release-reports").glob("run-rel-staging-*.md"))
        self.assertEqual(len(reports), 1)
        report_text = reports[0].read_text(encoding="utf-8")
        self.assertIn("before_invalid=1, after_invalid=1, new_invalid=0", report_text)

        # Verify apply driver used ALTER SESSION SET CURRENT_SCHEMA, WHENEVER SQLERROR EXIT SQL.SQLCODE, and no PLSQL_CODE_TYPE
        state = self._read_state()
        apply_calls = [c for c in state["calls"] if c["driverName"] == "release-apply.sql"]
        self.assertEqual(len(apply_calls), 1)
        apply_driver = apply_calls[0]["driver"]
        self.assertIn("ALTER SESSION SET CURRENT_SCHEMA = APP_CODE;", apply_driver)
        self.assertIn("WHENEVER SQLERROR EXIT SQL.SQLCODE", apply_driver)
        self.assertNotIn("PLSQL_CODE_TYPE", apply_driver)

        # Staging leaves status as draft
        after_manifest = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(after_manifest["status"], "draft")

        # New invalid object causes failure
        state["envs"]["staging"]["afterInvalids"] = [
            {"objectType": "VIEW", "objectName": "PREEXISTING_BROKEN_V"},
            {"objectType": "PACKAGE BODY", "objectName": "ORDERS_PKG"},
        ]
        state["calls"] = []
        self._write_state(state)
        code, _, err = self._run_cli(["run", "run-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ"])
        self.assertEqual(code, 1)
        self.assertIn("New invalid objects appeared", err)

    def test_prod_run_locks_bundle_creates_git_tag_and_refuses_re_run(self) -> None:
        # Initialize a temporary git repo so tag creation is exercised offline
        subprocess.run(["git", "init"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)

        self.assertEqual(self._run_cli(["new", "prod-rel"])[0], 0)
        bundle = self.root / "releases" / "prod-rel"
        (bundle / "02-code" / "orders_pkg_package_body.sql").write_text(
            "CREATE OR REPLACE " + DEV_PKG_SOURCE + "/\n", encoding="utf-8", newline="\n"
        )
        manifest = {
            "schemaVersion": 1,
            "slug": "prod-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [],
            "code": [{
                "schema": "APP_CODE",
                "file": "02-code/orders_pkg_package_body.sql",
                "objectType": "PACKAGE BODY",
                "objectName": "ORDERS_PKG",
                "mode": "replace",
            }],
            "ords": [],
            "verify": [],
            "apps": [],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", "releases"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add prod-rel"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)

        code, out, err = self._run_cli(["run", "prod-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ"])
        self.assertEqual(code, 0, err)
        self.assertIn("succeeded", out)

        locked = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(locked["status"], "deployed")
        self.assertEqual(locked["deployedTarget"], "PRODDB_UNQ")
        self.assertRegex(locked["deployedCommit"], r"^[0-9a-f]{40}$")
        self.assertTrue(locked["deployedAt"])

        # Git tag created
        tags = subprocess.run(
            ["git", "tag", "--list", "release/prod-rel"],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout.splitlines()
        self.assertEqual(tags, ["release/prod-rel"])

        # Post-deploy check, collect, and run all refuse
        self.assertEqual(self._run_cli(["check", "prod-rel", "--env", "prod"])[0], 1)
        self.assertEqual(self._run_cli(["run", "prod-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ"])[0], 1)
        code, _, err = self._run_cli([
            "collect", "prod-rel", "--from", "dev", "--schema", "APP_CODE",
            "--object", "PACKAGE_BODY:ORDERS_PKG", "--mode", "replace",
        ])
        self.assertEqual(code, 1)
        self.assertIn("deployed and locked", err)

    def test_code_compilation_errors_skip_backup_json_check_and_path_traversal(self) -> None:
        self.assertEqual(self._run_cli(["new", "edge-rel"])[0], 0)
        bundle = self.root / "releases" / "edge-rel"
        (bundle / "02-code" / "orders_pkg_package_body.sql").write_text(
            "CREATE OR REPLACE " + DEV_PKG_SOURCE + "/\n", encoding="utf-8", newline="\n"
        )
        manifest = {
            "schemaVersion": 1,
            "slug": "edge-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [],
            "code": [{
                "schema": "APP_CODE",
                "file": "02-code/orders_pkg_package_body.sql",
                "objectType": "PACKAGE BODY",
                "objectName": "ORDERS_PKG",
                "mode": "replace",
            }],
            "ords": [],
            "verify": [],
            "apps": [],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        # JSON check format
        code, out_json, _ = self._run_cli(["check", "edge-rel", "--env", "staging", "--format", "json"])
        self.assertEqual(code, 0)
        parsed = json.loads(out_json)
        self.assertTrue(parsed["passed"])
        self.assertEqual(parsed["targets"]["APP_CODE"]["identity"]["db_unique_name"], "STAGEDB_UNQ")

        # Compilation error in ALL_ERRORS fails run
        state = self._read_state()
        state["envs"]["staging"]["codeErrors"] = [
            {"objectType": "PACKAGE BODY", "objectName": "ORDERS_PKG", "line": 2, "position": 5, "text": "PLS-00103: bad syntax"}
        ]
        self._write_state(state)
        code, _, err = self._run_cli(["run", "edge-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--skip-backup"])
        self.assertEqual(code, 1)
        self.assertIn("Compilation errors in APP_CODE", err)
        self.assertFalse((self.root / "scratch" / "backups").exists())

        # Path traversal in release.json refused
        manifest["ddl"] = [{"schema": "APP_CODE", "file": "../../etc/passwd"}]
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        code, _, err = self._run_cli(["check", "edge-rel", "--env", "staging"])
        self.assertEqual(code, 1)
        self.assertIn("invalid file path", err)

    def test_multi_schema_executes_all_ddl_before_any_code_and_preserves_code_order(self) -> None:
        multi_env = {
            **BASE_ENV,
            "CODE_SCHEMA": "RUNERP_CUSTDATA,RUNERP_CODE",
            "CODE_PREFIXES": "*,*",
            "CODE_SQLCL_CONNECTION": "dev_cust,dev_code",
            "CODE_EXPECTED_USER": "RUNERP_CUSTDATA,RUNERP_CODE",
            "TABLES_SCHEMA": "RUNERP_CUSTDATA,RUNERP_CODE",
            "TABLES_PREFIXES": "*,*",
            "TABLES_SQLCL_CONNECTION": "dev_cust,dev_code",
            "TABLES_EXPECTED_USER": "RUNERP_CUSTDATA,RUNERP_CODE",
            "APEX_PARSING_SCHEMA": "RUNERP_CODE",
            "APEX_SQLCL_CONNECTION": "dev_code",
            "APEX_EXPECTED_USER": "RUNERP_CODE",
            "STAGING_SCHEMA": "RUNERP_CUSTDATA,RUNERP_CODE",
            "STAGING_SQLCL_CONNECTION": "stg_cust,stg_code",
            "STAGING_EXPECTED_USER": "DEPLOYER,DEPLOYER",
        }
        state = {
            "connMap": {"stg_cust": "stg_cust", "stg_code": "stg_code"},
            "envs": {
                "stg_cust": {
                    "identity": {
                        "session_user": "DEPLOYER",
                        "current_schema": "RUNERP_CUSTDATA",
                        "db_name": "STAGEDB",
                        "db_unique_name": "STAGEDB_UNQ",
                        "service_name": "stage_svc",
                        "con_name": "STAGEPDB",
                    },
                    "objects": {},
                    "beforeInvalids": [],
                    "afterInvalids": [],
                    "codeErrors": [],
                },
                "stg_code": {
                    "identity": {
                        "session_user": "DEPLOYER",
                        "current_schema": "RUNERP_CODE",
                        "db_name": "STAGEDB",
                        "db_unique_name": "STAGEDB_UNQ",
                        "service_name": "stage_svc",
                        "con_name": "STAGEPDB",
                    },
                    "objects": {},
                    "beforeInvalids": [],
                    "afterInvalids": [],
                    "codeErrors": [],
                },
            },
        }
        self._write_state(state)

        code, _, err = self._run_cli(["new", "cross-schema-rel"], environ=multi_env)
        self.assertEqual(code, 0, err)
        bundle = self.root / "releases" / "cross-schema-rel"
        (bundle / "01-ddl" / "001-cust-table.sql").write_text("CREATE TABLE cust_t (id NUMBER);\n", encoding="utf-8", newline="\n")
        (bundle / "02-code" / "01-code-spec.sql").write_text(
            "CREATE OR REPLACE PACKAGE code_pkg AS PROCEDURE p; END code_pkg;\n/\n", encoding="utf-8", newline="\n"
        )
        (bundle / "02-code" / "02-code-body.sql").write_text(
            "CREATE OR REPLACE PACKAGE BODY code_pkg AS PROCEDURE p IS BEGIN NULL; END; END code_pkg;\n/\n", encoding="utf-8", newline="\n"
        )
        (bundle / "02-code" / "03-cust-trg.sql").write_text(
            "CREATE OR REPLACE TRIGGER cust_t_trg BEFORE INSERT ON cust_t FOR EACH ROW BEGIN NULL; END;\n/\n", encoding="utf-8", newline="\n"
        )
        (bundle / "03-ords" / "01-api.sql").write_text("BEGIN NULL; END;\n/\n", encoding="utf-8", newline="\n")

        manifest = {
            "schemaVersion": 1,
            "slug": "cross-schema-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["RUNERP_CUSTDATA", "RUNERP_CODE"],
            "ddl": [
                {"schema": "RUNERP_CUSTDATA", "file": "01-ddl/001-cust-table.sql"},
            ],
            "code": [
                {
                    "schema": "RUNERP_CODE",
                    "file": "02-code/01-code-spec.sql",
                    "objectType": "PACKAGE",
                    "objectName": "CODE_PKG",
                    "mode": "new",
                },
                {
                    "schema": "RUNERP_CODE",
                    "file": "02-code/02-code-body.sql",
                    "objectType": "PACKAGE BODY",
                    "objectName": "CODE_PKG",
                    "mode": "new",
                },
                {
                    "schema": "RUNERP_CUSTDATA",
                    "file": "02-code/03-cust-trg.sql",
                    "objectType": "TRIGGER",
                    "objectName": "CUST_T_TRG",
                    "mode": "new",
                },
            ],
            "ords": [
                {"schema": "RUNERP_CODE", "file": "03-ords/01-api.sql"},
            ],
            "verify": [],
            "apps": [],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        code, out, err = self._run_cli(
            ["run", "cross-schema-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--skip-backup"],
            environ=multi_env,
        )
        self.assertEqual(code, 0, f"out={out}\nerr={err}")

        calls = self._read_state()["calls"]
        # First 2 calls are preflight fetch sessions (one per schema).
        apply_phase_calls = calls[2:]
        driver_sequence = [(c["conn"], c["driverName"]) for c in apply_phase_calls]
        self.assertEqual(
            driver_sequence,
            [
                ("stg_cust", "release-before-invalids.sql"),
                ("stg_code", "release-before-invalids.sql"),
                ("stg_cust", "release-apply-ddl-RUNERP_CUSTDATA.sql"),
                ("stg_code", "release-apply-code-RUNERP_CODE.sql"),
                ("stg_cust", "release-apply-code-RUNERP_CUSTDATA.sql"),
                ("stg_code", "release-apply-ords-RUNERP_CODE.sql"),
                ("stg_cust", "release-post-apply.sql"),
                ("stg_code", "release-post-apply.sql"),
            ],
        )
        # Consecutive RUNERP_CODE 02-code steps were batched into 1 SQLcl session in exact order.
        code_batch_driver = apply_phase_calls[3]["driver"]
        self.assertIn("RELEASE_STEP_START:02-code/01-code-spec.sql", code_batch_driver)
        self.assertIn("RELEASE_STEP_START:02-code/02-code-body.sql", code_batch_driver)
        self.assertLess(
            code_batch_driver.index("RELEASE_STEP_START:02-code/01-code-spec.sql"),
            code_batch_driver.index("RELEASE_STEP_START:02-code/02-code-body.sql"),
        )

    def test_collect_trigger_strips_enable_tail_and_preserves_disable(self) -> None:
        state = self._read_state()
        state["envs"]["dev"]["objects"]["TRIGGER:TRG_ENABLED"] = {
            "source": "",
            "ddl": (
                '  CREATE OR REPLACE EDITIONABLE TRIGGER "APP_CODE"."TRG_ENABLED"\n'
                "BEFORE INSERT ON APP_CODE.ORDERS FOR EACH ROW BEGIN NULL; END;\n\n/\n"
                'ALTER TRIGGER "APP_CODE"."TRG_ENABLED" ENABLE;\n'
            ),
        }
        state["envs"]["dev"]["objects"]["TRIGGER:TRG_DISABLED"] = {
            "source": "",
            "ddl": (
                '  CREATE OR REPLACE EDITIONABLE TRIGGER "APP_CODE"."TRG_DISABLED"\n'
                "BEFORE INSERT ON APP_CODE.ORDERS FOR EACH ROW BEGIN NULL; END;\n\n/\n"
                'ALTER TRIGGER "APP_CODE"."TRG_DISABLED" DISABLE;\n'
            ),
        }
        self._write_state(state)

        self.assertEqual(self._run_cli(["new", "trg-rel"])[0], 0)
        code, _, err = self._run_cli([
            "collect", "trg-rel", "--from", "dev", "--schema", "APP_CODE",
            "--object", "TRIGGER:TRG_ENABLED", "--mode", "new",
        ])
        self.assertEqual(code, 0, err)
        enabled_sql = (self.root / "releases" / "trg-rel" / "02-code" / "trg_enabled_trigger.sql").read_text(encoding="utf-8")
        self.assertNotIn("ALTER TRIGGER", enabled_sql)
        self.assertTrue(enabled_sql.endswith("\n/\n"))
        self.assertEqual(enabled_sql.count("\n/"), 1)

        code, _, err = self._run_cli([
            "collect", "trg-rel", "--from", "dev", "--schema", "APP_CODE",
            "--object", "TRIGGER:TRG_DISABLED", "--mode", "new",
        ])
        self.assertEqual(code, 0, err)
        disabled_sql = (self.root / "releases" / "trg-rel" / "02-code" / "trg_disabled_trigger.sql").read_text(encoding="utf-8")
        self.assertIn('ALTER TRIGGER "APP_CODE"."TRG_DISABLED" DISABLE;', disabled_sql)
        self.assertTrue(disabled_sql.endswith("DISABLE;\n"))

    def test_sqlcl_failure_preserves_log_and_surfaces_ora_error(self) -> None:
        self.assertEqual(self._run_cli(["new", "fail-rel"])[0], 0)
        bundle = self.root / "releases" / "fail-rel"
        (bundle / "01-ddl" / "001-bad.sql").write_text("CREATE TABLE T (ID NUMBER);\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1,
            "slug": "fail-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [{"schema": "APP_CODE", "file": "01-ddl/001-bad.sql"}],
            "code": [],
            "ords": [],
            "verify": [],
            "apps": [],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        state = self._read_state()
        state["envs"]["staging"]["failDdl"] = True
        self._write_state(state)

        code, _, err = self._run_cli(["run", "fail-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ"])
        self.assertEqual(code, 1)
        self.assertIn("ORA-00955: name is already used by an existing object", err)
        logs = list((self.root / "scratch" / "release-logs").glob("*-apply-APP_CODE-sqlcl-output.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("ORA-00955", logs[0].read_text(encoding="utf-8"))
        self.assertIn(str(logs[0]), err)


if __name__ == "__main__":
    unittest.main()
