"""Offline tests for scripts/release.py using fake_sqlcl (no live database)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
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
import hashlib
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


def _norm_sha(text: str) -> str:
    cleaned = re.sub(
        r"(?im)^[ \t]*ALTER[ \t]+SESSION[ \t]+SET[ \t]+PLSQL_CODE_TYPE[ \t]*=[ \t]*INTERPRETED[ \t]*;?[ \t]*\r?\n?",
        "",
        text.replace("\r\n", "\n").replace("\r", "\n"),
    )
    lines = [line.rstrip() for line in cleaned.split("\n")]
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    norm = ("\n".join(lines) + "\n") if lines else ""
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _check_identity_guards() -> None:
    if "-20901" not in driver:
        return
    db_m = re.search(r"SYS_CONTEXT\('USERENV','DB_UNIQUE_NAME'\)\)\s*<>\s*UPPER\('([^']+)'\)", driver)
    if db_m and db_m.group(1).upper() != identity.get("db_unique_name", "").upper():
        print(f"ORA-20901: Target DB_UNIQUE_NAME mismatch (expected {db_m.group(1)}, observed {identity.get('db_unique_name')})")
        sys.exit(1)
    con_m = re.search(r"NVL\(SYS_CONTEXT\('USERENV','CON_NAME'\), 'NON-CDB'\)\)\s*<>\s*UPPER\('([^']+)'\)", driver)
    if con_m and con_m.group(1).upper() != identity.get("con_name", "NON-CDB").upper():
        print(f"ORA-20901: Target CON_NAME mismatch (expected {con_m.group(1)}, observed {identity.get('con_name')})")
        sys.exit(1)
    usr_m = re.search(r"UPPER\(USER\)\s*<>\s*UPPER\('([^']+)'\)", driver)
    if usr_m and usr_m.group(1).upper() != identity.get("session_user", "").upper():
        print("ORA-20901: Target USER mismatch")
        sys.exit(1)
    sch_m = re.search(r"UPPER\(SYS_CONTEXT\('USERENV','CURRENT_SCHEMA'\)\)\s*<>\s*UPPER\('([^']+)'\)", driver)
    if sch_m and sch_m.group(1).upper() != identity.get("current_schema", "").upper():
        print("ORA-20901: Target CURRENT_SCHEMA mismatch")
        sys.exit(1)


def _check_step_guards() -> None:
    for nm in re.finditer(r"-- RELEASE_NEW_GUARD objectType=([^\n]+) objectName=([^\n ]+)", driver):
        g_type, g_name = nm.group(1).strip(), nm.group(2).strip()
        if f"{g_type}:{g_name}" in env_state.get("objects", {}):
            print(f"ORA-20904: Object {g_type} {g_name} already exists at apply time")
            sys.exit(1)
    for gm in re.finditer(r"-- RELEASE_PATCH_GUARD objectType=([^\n]+) objectName=([^\n ]+) baseSha256=([0-9a-f]{64})", driver):
        g_type, g_name, g_sha = gm.group(1).strip(), gm.group(2).strip(), gm.group(3)
        rec = env_state.get("objects", {}).get(f"{g_type}:{g_name}")
        live_txt = "" if rec is None else (rec.get("source") or rec.get("ddl") or "")
        if rec is None or _norm_sha(live_txt) != g_sha:
            print(f"ORA-20902: Patch base source changed between preflight and apply for {g_type} {g_name}")
            sys.exit(1)


def _record_applied_objects() -> None:
    if env_state.get("simulateMissingObjects"):
        return
    cur_schema = identity.get("current_schema", "").upper()
    step_blocks = re.findall(r"PROMPT RELEASE_STEP_START:[^\n]+\n(.*?)\nPROMPT RELEASE_STEP_END:", driver, re.DOTALL)
    for block in step_blocks:
        for cm in re.finditer(
            r'(?is)\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:FORCE|NOFORCE|EDITIONABLE|NONEDITIONABLE)\s+)*'
            r'(PACKAGE\s+BODY|TYPE\s+BODY|PACKAGE|TYPE|VIEW|TRIGGER|PROCEDURE|FUNCTION)\s+'
            r'(?:("?[A-Za-z][A-Za-z0-9_$#]*"?)\s*\.\s*)?("?[A-Za-z][A-Za-z0-9_$#]*"?)',
            block,
        ):
            obj_type = " ".join(cm.group(1).upper().split())
            owner = cm.group(2).strip('"').upper() if cm.group(2) else cur_schema
            obj_name = cm.group(3).strip('"').upper()
            if owner == cur_schema:
                if env_state.get("replaceOnApply"):
                    env_state.setdefault("objects", {})[f"{obj_type}:{obj_name}"] = {"source": block}
                else:
                    env_state.setdefault("objects", {}).setdefault(f"{obj_type}:{obj_name}", {"source": block})


def _expected_objects_payload() -> list[dict[str, str]]:
    invalids = {(i.get("objectType", "").upper(), i.get("objectName", "").upper()) for i in env_state.get("afterInvalids", [])}
    invalids |= {(i.get("objectType", "").upper(), i.get("objectName", "").upper()) for i in env_state.get("invalidCodeObjects", [])}
    missing = {tuple(x.split(":", 1)) for x in env_state.get("missingCodeObjects", [])}
    exp_m = re.search(r"FROM ALL_OBJECTS\s+WHERE owner = '[^']+' AND \(object_type, object_name\) IN \((.+?)\)\s*ORDER BY", driver, re.DOTALL)
    if not exp_m:
        return []
    out = []
    for t, n in re.findall(r"\('([^']+)', '([^']+)'\)", exp_m.group(1)):
        if (t, n) in missing:
            continue
        if f"{t}:{n}" in env_state.get("objects", {}):
            out.append({
                "objectType": t,
                "objectName": n,
                "status": "INVALID" if (t, n) in invalids else "VALID",
            })
    return out


if "RELEASE_FETCH_BEGIN" in driver:
    items = []
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
    _check_identity_guards()
    _check_step_guards()
    if env_state.get("failDdl"):
        print("ORA-00955: name is already used by an existing object")
        sys.exit(1)
    steps = re.findall(r"PROMPT RELEASE_STEP_START:([^\n]+)", driver)
    fail_at = env_state.get("failAtStep")
    if fail_at in steps:
        done = steps[:steps.index(fail_at)]
        state.setdefault("appliedSteps", []).extend(done)
        Path(state_path).write_text(json.dumps(state), encoding="utf-8")
        for s in done:
            print(f"RELEASE_STEP_START:{s}")
            print(f"RELEASE_STEP_END:{s}")
        print(f"RELEASE_STEP_START:{fail_at}")
        print("ORA-00904: \"NOTE_TXT\": invalid identifier")
        sys.exit(1)
    state.setdefault("appliedSteps", []).extend(steps)
    _record_applied_objects()
    Path(state_path).write_text(json.dumps(state), encoding="utf-8")
    before_payload = {"identity": identity, "invalids": env_state.get("beforeInvalids", [])}
    post_payload = {
        "errors": env_state.get("codeErrors", []),
        "warnings": env_state.get("codeWarnings", []),
        "expectedObjects": _expected_objects_payload(),
        "invalids": env_state.get("afterInvalids", []),
    }
    print("RELEASE_BEFORE_INVALID_BEGIN")
    print(json.dumps(before_payload))
    print("RELEASE_BEFORE_INVALID_END")
    print("RELEASE_BEFORE_INVALID_VERIFIED")
    for s in steps:
        print(f"RELEASE_STEP_START:{s}")
        print(f"RELEASE_STEP_END:{s}")
    print("RELEASE_POST_APPLY_BEGIN")
    print(json.dumps(post_payload))
    print("RELEASE_POST_APPLY_END")
    print("RELEASE_POST_APPLY_VERIFIED")
    sys.exit(0)

if "RELEASE_BEFORE_INVALID_BEGIN" in driver:
    _check_identity_guards()
    before_payload = {"identity": identity, "invalids": env_state.get("beforeInvalids", [])}
    print("RELEASE_BEFORE_INVALID_BEGIN")
    print(json.dumps(before_payload))
    print("RELEASE_BEFORE_INVALID_END")
    print("RELEASE_BEFORE_INVALID_VERIFIED")
    sys.exit(0)

if "RELEASE_BATCH_APPLIED" in driver:
    _check_identity_guards()
    _check_step_guards()
    if env_state.get("failDdl"):
        print("ORA-00955: name is already used by an existing object")
        sys.exit(1)
    steps = re.findall(r"PROMPT RELEASE_STEP_START:([^\n]+)", driver)
    state.setdefault("appliedSteps", []).extend(steps)
    _record_applied_objects()
    Path(state_path).write_text(json.dumps(state), encoding="utf-8")
    if not env_state.get("omitBatchMarkers"):
        for s in steps:
            print(f"RELEASE_STEP_START:{s}")
            print(f"RELEASE_STEP_END:{s}")
        print("RELEASE_BATCH_APPLIED")
    sys.exit(0)

if "RELEASE_POST_APPLY_BEGIN" in driver:
    _check_identity_guards()
    post_payload = {
        "errors": env_state.get("codeErrors", []),
        "warnings": env_state.get("codeWarnings", []),
        "expectedObjects": _expected_objects_payload(),
        "invalids": env_state.get("afterInvalids", []),
    }
    print("RELEASE_POST_APPLY_BEGIN")
    print(json.dumps(post_payload))
    print("RELEASE_POST_APPLY_END")
    print("RELEASE_POST_APPLY_VERIFIED")
    sys.exit(0)

if "RELEASE_VERIFY_OK" in driver:
    _check_identity_guards()
    if env_state.get("failVerify"):
        print("ORA-20001: smoke check failed")
        sys.exit(1)
    for s in re.findall(r"PROMPT RELEASE_VERIFY_START:([^\n]+)", driver):
        print(f"RELEASE_VERIFY_START:{s}")
        print(f"RELEASE_VERIFY_END:{s}")
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
        code_file = bundle / "02-code" / "app_code_orders_pkg_package_body.sql"
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
        view_sql = (bundle / "02-code" / "app_code_orders_v_view.sql").read_text(encoding="utf-8")
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
        report_match = re.search(r"Report:\s*(.+\.md)", out)
        self.assertIsNotNone(report_match, out)
        report_path = Path(report_match.group(1).strip())
        self.assertTrue(report_path.is_file())
        report_text = report_path.read_text(encoding="utf-8")
        self.assertIn("- **Outcome:** SUCCEEDED", report_text)
        self.assertIn("before_invalid=1, after_invalid=1, new_invalid=0", report_text)

        # Verify apply driver used ALTER SESSION SET CURRENT_SCHEMA, WHENEVER SQLERROR EXIT FAILURE ROLLBACK, and no PLSQL_CODE_TYPE
        state = self._read_state()
        apply_calls = [c for c in state["calls"] if c["driverName"] == "release-apply.sql"]
        self.assertEqual(len(apply_calls), 1)
        apply_driver = apply_calls[0]["driver"]
        self.assertIn("ALTER SESSION SET CURRENT_SCHEMA = APP_CODE;", apply_driver)
        self.assertIn("WHENEVER SQLERROR EXIT FAILURE ROLLBACK", apply_driver)
        self.assertNotIn("SQL.SQLCODE", apply_driver)
        self.assertNotIn("PLSQL_CODE_TYPE", apply_driver)

        # ASHARIF 2026-11-25: a successful staging run marks the bundle staged and records state/staging.json
        after_manifest = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(after_manifest["status"], "staged")
        self.assertTrue((bundle / "state" / "staging.json").is_file())

        # New invalid object causes failure
        state["envs"]["staging"]["afterInvalids"] = [
            {"objectType": "VIEW", "objectName": "PREEXISTING_BROKEN_V"},
            {"objectType": "PACKAGE BODY", "objectName": "ORDERS_PKG"},
        ]
        state["calls"] = []
        self._write_state(state)
        code, _, err = self._run_cli(
            ["run", "run-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--full"],
            app_deployer=fake_deploy,
        )
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

        code, out, err = self._run_cli(["run", "prod-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ", "--no-staging-proof"])
        self.assertEqual(code, 0, err)
        self.assertIn("succeeded", out)
        self.assertIn("Next: commit releases/prod-rel/release.json", out)
        self.assertIn("push the tag", out)

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
        self.assertEqual(self._run_cli(["run", "prod-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ", "--no-staging-proof"])[0], 1)
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
            ["run", "cross-schema-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--skip-backup", "--allow-unbacked-ords"],
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
                # ASHARIF 2026-11-25: read-only live-source reads for state/staging.json liveSha256
                ("stg_code", "release-fetch.sql"),
                ("stg_cust", "release-fetch.sql"),
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
        enabled_sql = (self.root / "releases" / "trg-rel" / "02-code" / "app_code_trg_enabled_trigger.sql").read_text(encoding="utf-8")
        self.assertNotIn("ALTER TRIGGER", enabled_sql)
        self.assertTrue(enabled_sql.endswith("\n/\n"))
        self.assertEqual(enabled_sql.count("\n/"), 1)

        code, _, err = self._run_cli([
            "collect", "trg-rel", "--from", "dev", "--schema", "APP_CODE",
            "--object", "TRIGGER:TRG_DISABLED", "--mode", "new",
        ])
        self.assertEqual(code, 0, err)
        disabled_sql = (self.root / "releases" / "trg-rel" / "02-code" / "app_code_trg_disabled_trigger.sql").read_text(encoding="utf-8")
        self.assertIn('ALTER TRIGGER "TRG_DISABLED" DISABLE;', disabled_sql)
        self.assertNotIn('"APP_CODE".', disabled_sql)
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

    def _run_cli_with_runner(
        self,
        argv: list[str],
        *,
        runner,
        app_deployer=None,
        environ: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        proc_env = self._env()
        kwargs: dict = {"environ": dict(BASE_ENV if environ is None else environ), "runner": runner}
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

    def test_in_session_guard_aborts_if_db_unique_name_or_patch_source_changes_after_preflight(self) -> None:
        # Verify both _render_apply_driver and _render_batch_apply_driver emit the -20901 target guard
        # and -20902 patch guard before DDL/code executes.
        target = release.Target("staging", "stage_conn", "APP_LOGIN", "APP_CODE", "staging")
        with tempfile.TemporaryDirectory() as tmp:
            patch_file = Path(tmp) / "p.sql"
            patch_file.write_text("CREATE OR REPLACE " + DEV_PKG_SOURCE + "/\n", encoding="utf-8", newline="\n")
            step = release.BundleStep(
                section="code",
                schema="APP_CODE",
                rel_file="02-code/p.sql",
                path=patch_file,
                object_type="PACKAGE BODY",
                object_name="ORDERS_PKG",
                mode="patch",
                base_sha256=release.compute_source_sha256(TARGET_PKG_SOURCE),
            )
            for rendered in (
                release._render_apply_driver(
                    target,
                    [step],
                    confirm_target="STAGEDB_UNQ",
                    preflight_con_name="STAGEPDB",
                    patch_sources={("PACKAGE BODY", "ORDERS_PKG"): TARGET_PKG_SOURCE},
                ),
                release._render_batch_apply_driver(
                    target,
                    [step],
                    confirm_target="STAGEDB_UNQ",
                    preflight_con_name="STAGEPDB",
                    patch_sources={("PACKAGE BODY", "ORDERS_PKG"): TARGET_PKG_SOURCE},
                ),
            ):
                self.assertIn("SYS_CONTEXT('USERENV','DB_UNIQUE_NAME')", rendered)
                self.assertIn("SYS_CONTEXT('USERENV','CON_NAME')", rendered)
                self.assertIn("UPPER(USER)", rendered)
                self.assertIn("SYS_CONTEXT('USERENV','CURRENT_SCHEMA')", rendered)
                self.assertIn("-20901", rendered)
                self.assertIn("-20902", rendered)
                self.assertIn("DBMS_LOB.COMPARE", rendered)
                self.assertLess(rendered.index("-20901"), rendered.index("RELEASE_STEP_START:02-code/p.sql"))
                self.assertLess(rendered.index("-20902"), rendered.index("RELEASE_STEP_START:02-code/p.sql"))

        # (a) DB_UNIQUE_NAME changes between preflight and apply
        self.assertEqual(self._run_cli(["new", "guard-db-rel"])[0], 0)
        bundle = self.root / "releases" / "guard-db-rel"
        (bundle / "01-ddl" / "001-ddl.sql").write_text("CREATE TABLE G_T (ID NUMBER);\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1,
            "slug": "guard-db-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [{"schema": "APP_CODE", "file": "01-ddl/001-ddl.sql"}],
            "code": [],
            "ords": [],
            "verify": [],
            "apps": [],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        def flip_db_after_preflight(t, driver_path, run_dir, environment=None, phase="apply"):
            res = release.run_sqlcl(t, driver_path, run_dir, environment=environment, phase=phase)
            if phase == "inventory":
                st = self._read_state()
                st["envs"]["staging"]["identity"]["db_unique_name"] = "WRONG_CLONE_DB"
                self._write_state(st)
            return res

        code, _, err = self._run_cli_with_runner(
            ["run", "guard-db-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--skip-backup"],
            runner=flip_db_after_preflight,
        )
        self.assertEqual(code, 1)
        self.assertIn("ORA-20901", err)
        self.assertEqual(self._read_state().get("appliedSteps", []), [])

        # (b) Patch object's live source changes between preflight and apply
        self._write_state(self.default_state)
        self.assertEqual(self._run_cli(["new", "guard-patch-rel"])[0], 0)
        p_bundle = self.root / "releases" / "guard-patch-rel"
        (p_bundle / "02-code" / "app_code_orders_pkg_package_body.sql").write_text(
            "CREATE OR REPLACE " + DEV_PKG_SOURCE + "/\n", encoding="utf-8", newline="\n"
        )
        p_manifest = {
            "schemaVersion": 1,
            "slug": "guard-patch-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [],
            "code": [{
                "schema": "APP_CODE",
                "file": "02-code/app_code_orders_pkg_package_body.sql",
                "objectType": "PACKAGE BODY",
                "objectName": "ORDERS_PKG",
                "mode": "patch",
                "baseSha256": release.compute_source_sha256(TARGET_PKG_SOURCE),
            }],
            "ords": [],
            "verify": [],
            "apps": [],
            "forbiddenPatterns": [],
        }
        (p_bundle / "release.json").write_text(json.dumps(p_manifest), encoding="utf-8", newline="\n")

        def flip_source_after_preflight(t, driver_path, run_dir, environment=None, phase="apply"):
            res = release.run_sqlcl(t, driver_path, run_dir, environment=environment, phase=phase)
            if phase == "inventory":
                st = self._read_state()
                st["envs"]["staging"]["objects"]["PACKAGE BODY:ORDERS_PKG"]["source"] = (
                    "PACKAGE BODY ORDERS_PKG AS\n  FUNCTION get_count RETURN NUMBER IS BEGIN RETURN 999; END;\nEND ORDERS_PKG;\n"
                )
                self._write_state(st)
            return res

        code, _, err = self._run_cli_with_runner(
            ["run", "guard-patch-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--skip-backup"],
            runner=flip_source_after_preflight,
        )
        self.assertEqual(code, 1)
        self.assertIn("ORA-20902", err)
        self.assertEqual(self._read_state().get("appliedSteps", []), [])

    def test_ddl_and_ords_backup_coverage_and_fail_closed_on_empty_ddl(self) -> None:
        state = self._read_state()
        state["envs"]["staging"]["objects"]["TABLE:ORDERS"] = {
            "source": "",
            "ddl": 'CREATE TABLE "APP_CODE"."ORDERS" ("ID" NUMBER);',
        }
        state["envs"]["staging"]["objects"]["VIEW:LEGACY_V"] = {
            "source": "",
            "ddl": 'CREATE OR REPLACE FORCE VIEW "APP_CODE"."LEGACY_V" AS SELECT 1 AS X FROM DUAL;',
        }
        state["envs"]["staging"]["objects"]["PACKAGE:ORDS_API_PKG"] = {
            "source": "PACKAGE ORDS_API_PKG AS PROCEDURE run_get; END ORDS_API_PKG;\n",
        }
        self._write_state(state)

        self.assertEqual(self._run_cli(["new", "backup-cov-rel"])[0], 0)
        bundle = self.root / "releases" / "backup-cov-rel"
        (bundle / "01-ddl" / "001-alter.sql").write_text(
            "ALTER TABLE APP_CODE.ORDERS ADD (NOTES VARCHAR2(50));\nDROP VIEW LEGACY_V;\n",
            encoding="utf-8",
            newline="\n",
        )
        (bundle / "03-ords" / "001-ords.sql").write_text("BEGIN NULL; END;\n/\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1,
            "slug": "backup-cov-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [{"schema": "APP_CODE", "file": "01-ddl/001-alter.sql"}],
            "code": [],
            "ords": [{
                "schema": "APP_CODE",
                "file": "03-ords/001-ords.sql",
                "objects": ["PACKAGE:ORDS_API_PKG"],
            }],
            "verify": [],
            "apps": [],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        # Requires --allow-unbacked-ords because 03-ords/ metadata is not exported even when backing package exists
        code, _, err = self._run_cli(["run", "backup-cov-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ"])
        self.assertEqual(code, 1)
        self.assertIn("lack ORDS metadata backup coverage", err)

        code, out, err = self._run_cli([
            "run", "backup-cov-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--allow-unbacked-ords",
        ])
        self.assertEqual(code, 0, f"out={out}\nerr={err}")

        backups = sorted((self.root / "scratch" / "backups").glob("backup-cov-rel-staging-*"))
        self.assertEqual(len(backups), 1)
        bmanifest = json.loads((backups[0] / "backup-manifest.json").read_text(encoding="utf-8"))
        expected_pairs = {(o["objectType"], o["objectName"]) for o in bmanifest["expectedObjects"]}
        backed_pairs = {(o["objectType"], o["objectName"]) for o in bmanifest["objects"]}
        self.assertEqual(
            expected_pairs,
            {("TABLE", "ORDERS"), ("VIEW", "LEGACY_V"), ("PACKAGE", "ORDS_API_PKG")},
        )
        self.assertEqual(backed_pairs, expected_pairs)
        self.assertEqual(bmanifest["ordsBackingObjects"], "covered")
        self.assertEqual(bmanifest["ordsMetadataCoverage"], "unbacked")
        self.assertEqual(bmanifest["ordsBackupCoverage"], "unbacked")
        formats = {o["objectName"]: o["format"] for o in bmanifest["objects"]}
        self.assertEqual(formats["ORDERS"], "ddl")
        self.assertEqual(formats["ORDS_API_PKG"], "source-ddl")
        pkg_backup = (backups[0] / "APP_CODE.PACKAGE.ORDS_API_PKG.sql").read_text(encoding="utf-8")
        self.assertTrue(pkg_backup.startswith("CREATE OR REPLACE "))
        self.assertTrue(pkg_backup.endswith("/\n"))
        self.assertTrue((backups[0] / "APP_CODE.TABLE.ORDERS.sql").is_file())
        self.assertTrue((backups[0] / "APP_CODE.VIEW.LEGACY_V.sql").is_file())
        self.assertTrue((backups[0] / "APP_CODE.PACKAGE.ORDS_API_PKG.sql").is_file())

        # Fail closed before apply if an existing object returns empty DDL/source
        state = self._read_state()
        state["envs"]["staging"]["objects"]["TABLE:ORDERS"]["ddl"] = ""
        state["calls"] = []
        state["appliedSteps"] = []
        self._write_state(state)

        code, _, err = self._run_cli([
            "run", "backup-cov-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--allow-unbacked-ords",
            "--full",
        ])
        self.assertEqual(code, 1)
        self.assertIn("Backup failed before apply", err)
        self.assertIn("APP_CODE.ORDERS (TABLE)", err)
        self.assertEqual(self._read_state().get("appliedSteps", []), [])

    def test_prod_requires_clean_tracked_git_bundle_and_tags_release_commit(self) -> None:
        self.assertEqual(self._run_cli(["new", "prod-clean-rel"])[0], 0)
        bundle = self.root / "releases" / "prod-clean-rel"
        (bundle / "02-code" / "app_code_orders_pkg_package_body.sql").write_text(
            "CREATE OR REPLACE " + DEV_PKG_SOURCE + "/\n", encoding="utf-8", newline="\n"
        )
        manifest = {
            "schemaVersion": 1,
            "slug": "prod-clean-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [],
            "code": [{
                "schema": "APP_CODE",
                "file": "02-code/app_code_orders_pkg_package_body.sql",
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

        # Not a Git repo -> refused before any SQL runs
        code, _, err = self._run_cli(["run", "prod-clean-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ", "--no-staging-proof"])
        self.assertEqual(code, 1)
        self.assertIn("Git repository", err)
        self.assertEqual(self._read_state().get("calls", []), [])

        # Git repo initialized, bundle untracked -> refused before any SQL runs
        subprocess.run(["git", "init"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        (self.root / "README.md").write_text("init\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Init"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)

        code, _, err = self._run_cli(["run", "prod-clean-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ", "--no-staging-proof"])
        self.assertEqual(code, 1)
        self.assertIn("tracked in Git", err)
        self.assertEqual(self._read_state().get("calls", []), [])

        # Commit bundle, then leave an untracked file inside bundle -> refused before SQL runs
        subprocess.run(["git", "add", "releases"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add prod-clean-rel"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        release_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

        (bundle / "01-ddl" / "untracked.sql").write_text("SELECT 1 FROM DUAL;\n", encoding="utf-8", newline="\n")
        code, _, err = self._run_cli(["run", "prod-clean-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ", "--no-staging-proof"])
        self.assertEqual(code, 1)
        self.assertIn("clean Git working tree", err)
        self.assertEqual(self._read_state().get("calls", []), [])

        # Remove untracked file -> succeeds, tags exact release_commit BEFORE writing deployed status
        (bundle / "01-ddl" / "untracked.sql").unlink()
        code, _, err = self._run_cli(["run", "prod-clean-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ", "--no-staging-proof"])
        self.assertEqual(code, 0, err)
        tagged_sha = subprocess.run(
            ["git", "rev-list", "-n", "1", "release/prod-clean-rel"],
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        self.assertEqual(tagged_sha, release_commit)
        locked = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(locked["status"], "deployed")
        self.assertEqual(locked["deployedCommit"], release_commit)

    def test_schema_qualified_collect_filenames_and_duplicate_file_rejection(self) -> None:
        multi_env = {
            **BASE_ENV,
            "CODE_SCHEMA": "APP_DATA,APP_CODE",
            "CODE_PREFIXES": "*,*",
            "CODE_SQLCL_CONNECTION": "dev_data,dev_code",
            "CODE_EXPECTED_USER": "APP_DATA,APP_CODE",
        }
        state = self._read_state()
        state["connMap"]["dev_data"] = "dev_data"
        state["connMap"]["dev_code"] = "dev"
        state["envs"]["dev_data"] = {
            "identity": {
                "session_user": "APP_DATA",
                "current_schema": "APP_DATA",
                "db_name": "DEVDB",
                "db_unique_name": "DEVDB_UNQ",
                "service_name": "dev_svc",
                "con_name": "DEVPDB",
            },
            "objects": {
                "PACKAGE BODY:ORDERS_PKG": {
                    "source": "PACKAGE BODY ORDERS_PKG AS FUNCTION f RETURN NUMBER IS BEGIN RETURN 10; END; END ORDERS_PKG;\n",
                },
            },
        }
        self._write_state(state)

        self.assertEqual(self._run_cli(["new", "dup-rel"], environ=multi_env)[0], 0)
        self.assertEqual(
            self._run_cli([
                "collect", "dup-rel", "--from", "dev", "--schema", "APP_CODE",
                "--object", "PACKAGE_BODY:ORDERS_PKG", "--mode", "new",
            ], environ=multi_env)[0],
            0,
        )
        self.assertEqual(
            self._run_cli([
                "collect", "dup-rel", "--from", "dev", "--schema", "APP_DATA",
                "--object", "PACKAGE_BODY:ORDERS_PKG", "--mode", "new",
            ], environ=multi_env)[0],
            0,
        )
        bundle = self.root / "releases" / "dup-rel"
        self.assertTrue((bundle / "02-code" / "app_code_orders_pkg_package_body.sql").is_file())
        self.assertTrue((bundle / "02-code" / "app_data_orders_pkg_package_body.sql").is_file())

        # collect --file colliding with different entry is refused without overwriting
        code, _, err = self._run_cli([
            "collect", "dup-rel", "--from", "dev", "--schema", "APP_DATA",
            "--object", "PACKAGE_BODY:ORDERS_PKG", "--mode", "new",
            "--file", "app_code_orders_pkg_package_body.sql",
        ], environ=multi_env)
        self.assertEqual(code, 1)
        self.assertIn("already used by APP_CODE.ORDERS_PKG", err)
        self.assertIn("RETURN 2", (bundle / "02-code" / "app_code_orders_pkg_package_body.sql").read_text(encoding="utf-8"))

        # Duplicate file reference in release.json is refused by release check
        manifest = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        manifest["code"][1]["file"] = "02-code/app_code_orders_pkg_package_body.sql"
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        code, _, err = self._run_cli(["check", "dup-rel", "--env", "dev"], environ=multi_env)
        self.assertEqual(code, 1)
        self.assertIn("duplicate bundle file reference", err)

    def test_app_env_preflight_validation_and_failure_report_after_sql_apply(self) -> None:
        self.assertEqual(self._run_cli(["new", "app-rel"])[0], 0)
        bundle = self.root / "releases" / "app-rel"
        (bundle / "01-ddl" / "001-t.sql").write_text("CREATE TABLE APP_T (ID NUMBER);\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1,
            "slug": "app-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [{"schema": "APP_CODE", "file": "01-ddl/001-t.sql"}],
            "code": [],
            "ords": [],
            "verify": [],
            "apps": [130],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        # (a) ASHARIF 2026-11-25: on --env dev the SQL runs and the apps are skipped (DEV apps go through publish)
        deployed: list[int] = []
        code, out, err = self._run_cli(
            ["run", "app-rel", "--env", "dev", "--confirm-target", "DEVDB_UNQ"],
            app_deployer=lambda _r, app_id, *_a: deployed.append(app_id),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(deployed, [])
        self.assertEqual(self._read_state().get("appliedSteps", []), ["01-ddl/001-t.sql"])
        self.assertIn("APEX app 130 skipped on DEV (use 'scripts/team.sh publish 130 --env dev')", out)
        report = Path(re.search(r"Report:\s*(.+\.md)", out).group(1).strip()).read_text(encoding="utf-8")
        self.assertIn("- **Apps skipped on DEV:** 130", report)
        dev_state = json.loads((bundle / "state" / "dev.json").read_text(encoding="utf-8"))
        self.assertEqual(dev_state["apps"], {})
        code, out, _ = self._run_cli(["check", "app-rel", "--env", "dev"])
        self.assertIn("APEX app deploy: 130 (skipped on DEV: use publish)", out)
        state = self._read_state()
        state["appliedSteps"] = []
        self._write_state(state)

        # (b) missing scripts/deploy.sh and missing apps/*/130/deployments/staging.json fail preflight
        (self.root / "apps" / "APP_CODE" / "130" / "deployments").mkdir(parents=True)
        code, out, _ = self._run_cli(["check", "app-rel", "--env", "staging"])
        self.assertEqual(code, 1)
        self.assertIn("scripts/deploy.sh not found", out)
        self.assertIn("apps/*/130/deployments/staging.json", out)

        # Create deploy.sh and staging.json, then fail Step 5 app_deployer after SQL apply succeeds
        (self.scripts_dir / "deploy.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
        (self.root / "apps" / "APP_CODE" / "130" / "deployments" / "staging.json").write_text("{}\n", encoding="utf-8", newline="\n")

        def failing_deploy(_root: Path, app_id: int, _env: str, _confirm: str, _vals) -> None:
            raise release.ReleaseError(f"APEX app {app_id} deploy failed: simulated failure")

        code, _, err = self._run_cli(
            ["run", "app-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ"],
            app_deployer=failing_deploy,
        )
        self.assertEqual(code, 1)
        self.assertIn("simulated failure", err)
        reports = sorted((self.root / "scratch" / "release-reports").glob("app-rel-staging-*.md"))
        self.assertEqual(len(reports), 1)
        report_text = reports[0].read_text(encoding="utf-8")
        self.assertIn("- **Outcome:** FAILED", report_text)
        self.assertIn("- **Error:** APEX app 130 deploy failed: simulated failure", report_text)
        self.assertIn("- **Backup:** `scratch/backups/app-rel-staging-", report_text)
        self.assertIn("- **Completed phases:** preflight, backup, apply, verify", report_text)
        self.assertIn("- `apps`:", report_text)

    def test_prod_rejects_git_ignored_files_and_executes_pinned_commit_bytes(self) -> None:
        subprocess.run(["git", "init"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        (self.root / ".gitignore").write_text("*.ignored.sql\nscratch/\n", encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Init"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)

        self.assertEqual(self._run_cli(["new", "prod-pin-rel"])[0], 0)
        bundle = self.root / "releases" / "prod-pin-rel"
        (bundle / "01-ddl" / "001-t.sql").write_text("CREATE TABLE COMMITTED_T (ID NUMBER);\n", encoding="utf-8", newline="\n")
        (bundle / "01-ddl" / "secret.ignored.sql").write_text("SELECT 1 FROM DUAL;\n", encoding="utf-8", newline="\n")
        (bundle / "02-code" / "orders_pkg.sql").write_text("CREATE OR REPLACE " + DEV_PKG_SOURCE + "/\n", encoding="utf-8", newline="\n")
        (bundle / "04-verify" / "001-check.sql").write_text("SELECT 'COMMITTED_VERIFY' FROM DUAL;\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1,
            "slug": "prod-pin-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [
                {"schema": "APP_CODE", "file": "01-ddl/001-t.sql"},
                {"schema": "APP_CODE", "file": "01-ddl/secret.ignored.sql"},
            ],
            "code": [{
                "schema": "APP_CODE",
                "file": "02-code/orders_pkg.sql",
                "objectType": "PACKAGE BODY",
                "objectName": "ORDERS_PKG",
                "mode": "replace",
            }],
            "ords": [],
            "verify": [{"schema": "APP_CODE", "file": "04-verify/001-check.sql"}],
            "apps": [],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        # Force-add ignored file and commit so even committed-via-(-f) ignored files are caught.
        subprocess.run(["git", "add", "-f", "releases/prod-pin-rel"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add with ignored"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)

        code, out, _ = self._run_cli(["check", "prod-pin-rel", "--env", "prod"])
        self.assertEqual(code, 1)
        self.assertIn("Git-ignored", out)
        code, _, err = self._run_cli(["run", "prod-pin-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ", "--no-staging-proof"])
        self.assertEqual(code, 1)
        self.assertIn("Git-ignored", err)
        self.assertEqual(self._read_state().get("appliedSteps", []), [])

        # Remove ignored file from manifest and commit cleanly; also add an untracked ignored file to test rejection.
        manifest["ddl"] = [{"schema": "APP_CODE", "file": "01-ddl/001-t.sql"}]
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        subprocess.run(["git", "rm", "-f", "releases/prod-pin-rel/01-ddl/secret.ignored.sql"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "add", "releases/prod-pin-rel/release.json"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Clean prod-pin-rel"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        release_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True, text=True, encoding="utf-8"
        ).stdout.strip()

        (bundle / "01-ddl" / "untracked.ignored.sql").write_text("SELECT 1 FROM DUAL;\n", encoding="utf-8", newline="\n")
        self._write_state(self.default_state)
        code, _, err = self._run_cli(["run", "prod-pin-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ", "--no-staging-proof"])
        self.assertEqual(code, 1)
        self.assertIn("clean Git working tree", err)
        self.assertEqual(self._read_state().get("calls", []), [])
        (bundle / "01-ddl" / "untracked.ignored.sql").unlink()

        # Mutate tracked SQL files on disk *after* preflight starts (during inventory runner call):
        # apply and verify must execute ONLY the release_commit bytes!
        self._write_state(self.default_state)
        mutated = False

        def mutate_disk_after_preflight(t, driver_path, run_dir, environment=None, phase="apply"):
            nonlocal mutated
            res = release.run_sqlcl(t, driver_path, run_dir, environment=environment, phase=phase)
            if not mutated and run_dir.name.startswith("preflight-"):
                mutated = True
                (bundle / "01-ddl" / "001-t.sql").write_text("CREATE TABLE EVIL_UNCOMMITTED (ID NUMBER);\n", encoding="utf-8", newline="\n")
                (bundle / "02-code" / "orders_pkg.sql").write_text(
                    "CREATE OR REPLACE PACKAGE BODY ORDERS_PKG AS FUNCTION evil RETURN NUMBER IS BEGIN RETURN 9; END; END ORDERS_PKG;\n/\n",
                    encoding="utf-8",
                    newline="\n",
                )
                (bundle / "04-verify" / "001-check.sql").write_text("SELECT 'EVIL_VERIFY' FROM DUAL;\n", encoding="utf-8", newline="\n")
            return res

        code, _, err = self._run_cli_with_runner(
            ["run", "prod-pin-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ", "--no-staging-proof"],
            runner=mutate_disk_after_preflight,
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(mutated)
        calls = self._read_state()["calls"]
        apply_drivers = [c["driver"] for c in calls if c["driverName"] == "release-apply.sql"]
        verify_drivers = [c["driver"] for c in calls if c["driverName"] == "release-verify.sql"]
        self.assertEqual(len(apply_drivers), 1)
        self.assertEqual(len(verify_drivers), 1)
        self.assertIn("COMMITTED_T", apply_drivers[0])
        self.assertNotIn("EVIL_UNCOMMITTED", apply_drivers[0])
        self.assertNotIn("FUNCTION evil", apply_drivers[0])
        self.assertIn("COMMITTED_VERIFY", verify_drivers[0])
        self.assertNotIn("EVIL_VERIFY", verify_drivers[0])
        tagged_sha = subprocess.run(
            ["git", "rev-list", "-n", "1", "release/prod-pin-rel"],
            cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True, text=True, encoding="utf-8",
        ).stdout.strip()
        self.assertEqual(tagged_sha, release_commit)

    def test_backup_session_identity_mismatch_aborts_before_writing_or_applying(self) -> None:
        self.assertEqual(self._run_cli(["new", "backup-id-rel"])[0], 0)
        bundle = self.root / "releases" / "backup-id-rel"
        (bundle / "02-code" / "orders_pkg.sql").write_text("CREATE OR REPLACE " + DEV_PKG_SOURCE + "/\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1,
            "slug": "backup-id-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [],
            "code": [{
                "schema": "APP_CODE",
                "file": "02-code/orders_pkg.sql",
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

        for field, bad_val, expected_msg in (
            ("db_unique_name", "WRONG_BACKUP_DB", "DB_UNIQUE_NAME mismatch in backup session"),
            ("con_name", "WRONG_PDB", "CON_NAME mismatch in backup session"),
        ):
            self._write_state(self.default_state)

            def flip_during_backup(t, driver_path, run_dir, environment=None, phase="apply", field_name=field, bad_value=bad_val):
                if run_dir.name.startswith("backup-"):
                    st = self._read_state()
                    st["envs"]["staging"]["identity"][field_name] = bad_value
                    self._write_state(st)
                    try:
                        return release.run_sqlcl(t, driver_path, run_dir, environment=environment, phase=phase)
                    finally:
                        st = self._read_state()
                        st["envs"]["staging"]["identity"] = self.default_state["envs"]["staging"]["identity"]
                        self._write_state(st)
                return release.run_sqlcl(t, driver_path, run_dir, environment=environment, phase=phase)

            code, _, err = self._run_cli_with_runner(
                ["run", "backup-id-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ"],
                runner=flip_during_backup,
            )
            self.assertEqual(code, 1)
            self.assertIn(expected_msg, err)
            self.assertEqual(self._read_state().get("appliedSteps", []), [])
            backup_sql_files = list((self.root / "scratch" / "backups").rglob("*.sql"))
            self.assertEqual(backup_sql_files, [])

    def test_ords_unbacked_steps_fail_closed_and_allow_unbacked_ords_records_coverage(self) -> None:
        self.assertEqual(self._run_cli(["new", "ords-unbacked-rel"])[0], 0)
        bundle = self.root / "releases" / "ords-unbacked-rel"
        (bundle / "03-ords" / "001-module.sql").write_text(
            "BEGIN ORDS.DEFINE_MODULE(p_module_name => 'demo', p_base_path => '/demo/'); END;\n/\n",
            encoding="utf-8",
            newline="\n",
        )
        manifest = {
            "schemaVersion": 1,
            "slug": "ords-unbacked-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [],
            "code": [],
            "ords": [{"schema": "APP_CODE", "file": "03-ords/001-module.sql"}],
            "verify": [],
            "apps": [],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        # release check warns and reports unbackedOrdsSteps
        code, out, _ = self._run_cli(["check", "ords-unbacked-rel", "--env", "staging"])
        self.assertEqual(code, 0, out)
        self.assertIn("ORDS backup coverage: unbacked steps: 03-ords/001-module.sql", out)

        code, out_json, _ = self._run_cli(["check", "ords-unbacked-rel", "--env", "staging", "--format", "json"])
        self.assertEqual(code, 0)
        plan = json.loads(out_json)
        self.assertEqual(plan["ordsBackupCoverage"], "unbacked")
        self.assertEqual(plan["unbackedOrdsSteps"], ["03-ords/001-module.sql"])

        # release run without --allow-unbacked-ords fails closed before apply
        code, _, err = self._run_cli(["run", "ords-unbacked-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ"])
        self.assertEqual(code, 1)
        self.assertIn("lack ORDS metadata backup coverage", err)
        self.assertIn("03-ords/001-module.sql", err)
        self.assertEqual(self._read_state().get("appliedSteps", []), [])

        # release run --allow-unbacked-ords succeeds and records explicit unbacked status in backup-manifest + report
        code, out, err = self._run_cli([
            "run", "ords-unbacked-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--allow-unbacked-ords",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("succeeded", out)
        self.assertEqual(self._read_state().get("appliedSteps", []), ["03-ords/001-module.sql"])

        backups = sorted((self.root / "scratch" / "backups").glob("ords-unbacked-rel-staging-*"))
        self.assertEqual(len(backups), 1)
        bmanifest = json.loads((backups[0] / "backup-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(bmanifest["ordsBackupCoverage"], "unbacked")
        self.assertEqual(bmanifest["unbackedOrdsSteps"], ["03-ords/001-module.sql"])

        report_match = re.search(r"Report:\s*(.+\.md)", out)
        self.assertIsNotNone(report_match, out)
        report_text = Path(report_match.group(1).strip()).read_text(encoding="utf-8")
        self.assertIn("- **ORDS backup coverage:** `unbacked`", report_text)
        self.assertIn("`03-ords/001-module.sql`", report_text)

    def test_app_descriptor_validated_via_publish_app_describe_during_preflight(self) -> None:
        self.assertEqual(self._run_cli(["new", "app-desc-rel"])[0], 0)
        bundle = self.root / "releases" / "app-desc-rel"
        (bundle / "01-ddl" / "001-t.sql").write_text("CREATE TABLE APP_DESC_T (ID NUMBER);\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1,
            "slug": "app-desc-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [{"schema": "APP_CODE", "file": "01-ddl/001-t.sql"}],
            "code": [],
            "ords": [],
            "verify": [],
            "apps": [130],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        app_dir = self.root / "apps" / "APP_CODE" / "130"
        (app_dir / "deployments").mkdir(parents=True)
        (app_dir / "application.apx").write_text("app DEMO (\n  name: Demo\n)\n", encoding="utf-8", newline="\n")
        (self.scripts_dir / "deploy.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
        (self.scripts_dir / "publish_app.sh").write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'app_id="$1"; shift\n'
            'env_name=""\n'
            'while [ "$#" -gt 0 ]; do case "$1" in --env) env_name="$2"; shift 2;; *) shift;; esac; done\n'
            'repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"\n'
            'python3 - "$repo_root/apps/APP_CODE/$app_id/deployments/$env_name.json" "$app_id" <<\'PY\'\n'
            'import json, sys\n'
            'd = json.loads(open(sys.argv[1], encoding="utf-8").read())\n'
            'assert d["workspace"]["name"] and d["app"]["id"] == int(sys.argv[2])\n'
            'print("/tmp/app")\n'
            'print(d["workspace"]["name"])\n'
            'print(d["app"]["databaseSession"]["parsingSchema"])\n'
            'print("stage_conn")\n'
            'print("APP_LOGIN")\n'
            'print("staging")\n'
            'PY\n',
            encoding="utf-8",
            newline="\n",
        )

        # Malformed staging.json fails in release check and release run preflight before any SQL apply
        (app_dir / "deployments" / "staging.json").write_text('{"bad": true}\n', encoding="utf-8", newline="\n")
        code, out, _ = self._run_cli(["check", "app-desc-rel", "--env", "staging"])
        self.assertEqual(code, 1)
        self.assertIn("descriptor validation failed", out)

        code, _, err = self._run_cli(["run", "app-desc-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ"])
        self.assertEqual(code, 1)
        self.assertIn("descriptor validation failed", err)
        self.assertEqual(self._read_state().get("appliedSteps", []), [])
        self.assertFalse((self.root / "scratch" / "backups").exists())

        # Valid staging.json passes check
        (app_dir / "deployments" / "staging.json").write_text(
            json.dumps({
                "workspace": {"name": "STAGE_WS"},
                "app": {"id": 130, "databaseSession": {"parsingSchema": "APP_CODE"}},
            }) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        code, out, err = self._run_cli(["check", "app-desc-rel", "--env", "staging"])
        self.assertEqual(code, 0, out + err)

    def test_real_publish_app_describe_rejects_malformed_descriptor(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        if not (repo_root / "scripts" / "publish_app.sh").is_file() or not (repo_root / "template-manifest.json").is_file():
            self.skipTest("requires full template scripts/publish_app.sh and template-manifest.json")

        import shutil

        for name in (
            "deploy.sh",
            "publish_app.sh",
            "load_env.sh",
            "sqlcl_safe.sh",
            "validate_app_source.py",
            "path_utils.py",
            "deployment_descriptor.py",
            "template_release.py",
        ):
            src = repo_root / "scripts" / name
            if src.is_file():
                shutil.copy2(src, self.scripts_dir / name)
        shutil.copy2(repo_root / "template-manifest.json", self.root / "template-manifest.json")

        self.assertEqual(self._run_cli(["new", "real-desc-rel"])[0], 0)
        bundle = self.root / "releases" / "real-desc-rel"
        (bundle / "01-ddl" / "001-t.sql").write_text("CREATE TABLE REAL_DESC_T (ID NUMBER);\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1,
            "slug": "real-desc-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [{"schema": "APP_CODE", "file": "01-ddl/001-t.sql"}],
            "code": [],
            "ords": [],
            "verify": [],
            "apps": [130],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        app_dir = self.root / "apps" / "APP_CODE" / "130"
        (app_dir / "deployments").mkdir(parents=True)
        (app_dir / "application.apx").write_text("app DEMO (\n  name: Demo\n)\n", encoding="utf-8", newline="\n")

        # Malformed staging.json (wrong app.id) fails via real publish_app.sh --describe before any SQL runs
        (app_dir / "deployments" / "staging.json").write_text(
            json.dumps({
                "workspace": {"name": "STAGE_WS"},
                "app": {"id": 999, "databaseSession": {"parsingSchema": "APP_CODE"}},
            }) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        code, out, _ = self._run_cli(["check", "real-desc-rel", "--env", "staging"])
        self.assertEqual(code, 1)
        self.assertIn("descriptor validation failed", out)

        code, _, err = self._run_cli(["run", "real-desc-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ"])
        self.assertEqual(code, 1)
        self.assertIn("descriptor validation failed", err)
        self.assertEqual(self._read_state().get("appliedSteps", []), [])
        self.assertFalse((self.root / "scratch" / "backups").exists())

        # Valid staging.json passes release check
        (app_dir / "deployments" / "staging.json").write_text(
            json.dumps({
                "workspace": {"name": "STAGE_WS"},
                "app": {"id": 130, "databaseSession": {"parsingSchema": "APP_CODE"}},
            }) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        code, out, err = self._run_cli(["check", "real-desc-rel", "--env", "staging"])
        self.assertEqual(code, 0, out + err)

    def test_p1_1_rejects_nested_sqlcl_directives_allows_comments_strings_and_start_with(self) -> None:
        self.assertEqual(self._run_cli(["new", "nested-dir-rel"])[0], 0)
        bundle = self.root / "releases" / "nested-dir-rel"
        safe_sql = (
            "-- @author comment\n"
            "/* multi-line\n"
            "@@ignored_in_comment.sql\n"
            "START ignored_in_comment.sql\n"
            "*/\n"
            "CREATE SEQUENCE my_seq\n"
            "START WITH 1;\n"
            "SELECT 'line1\n"
            "@not_a_directive.com'\n"
            "  AS email, q'[START not_directive]' AS txt FROM DUAL;\n"
        )
        (bundle / "01-ddl" / "001-safe.sql").write_text(safe_sql, encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1,
            "slug": "nested-dir-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [{"schema": "APP_CODE", "file": "01-ddl/001-safe.sql"}],
            "code": [],
            "ords": [],
            "verify": [],
            "apps": [],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        code, out, err = self._run_cli(["check", "nested-dir-rel", "--env", "staging"])
        self.assertEqual(code, 0, out + err)

        for bad_line in ("@sub/evil.sql", "@@evil.sql", "START evil.sql"):
            with self.subTest(directive=bad_line):
                (bundle / "01-ddl" / "001-safe.sql").write_text(
                    f"SELECT 1 FROM DUAL;\n{bad_line}\n", encoding="utf-8", newline="\n"
                )
                code, _, err = self._run_cli(["check", "nested-dir-rel", "--env", "staging"])
                self.assertEqual(code, 1)
                self.assertIn("nested SQLcl script-loading directive", err)

    def test_p1_1_prod_pins_apps_from_commit_and_fails_closed_on_check_ignore_error(self) -> None:
        subprocess.run(["git", "init"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        (self.root / ".gitignore").write_text("*.ignored\nscratch/\n", encoding="utf-8", newline="\n")

        self.assertEqual(self._run_cli(["new", "prod-app-rel"])[0], 0)
        bundle = self.root / "releases" / "prod-app-rel"
        (bundle / "01-ddl" / "001-t.sql").write_text("CREATE TABLE PROD_T (ID NUMBER);\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1,
            "slug": "prod-app-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [{"schema": "APP_CODE", "file": "01-ddl/001-t.sql"}],
            "code": [],
            "ords": [],
            "verify": [],
            "apps": [130],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        app_dir = self.root / "apps" / "APP_CODE" / "130"
        (app_dir / "deployments").mkdir(parents=True)
        (app_dir / "application.apx").write_text("app DEMO (\n  name: CommittedApp\n)\n", encoding="utf-8", newline="\n")
        (app_dir / "deployments" / "prod.json").write_text(
            json.dumps({"workspace": {"name": "PROD_WS"}, "app": {"id": 130, "databaseSession": {"parsingSchema": "APP_CODE"}}}) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        seen_app_sources: list[str] = []
        (self.scripts_dir / "deploy.sh").write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'app_src=""\n'
            'while [ "$#" -gt 0 ]; do case "$1" in --app-source-dir) app_src="$2"; shift 2;; *) shift;; esac; done\n'
            'test -n "$app_src"\n'
            'grep -q "CommittedApp" "$app_src/application.apx"\n',
            encoding="utf-8",
            newline="\n",
        )
        (self.scripts_dir / "publish_app.sh").write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'app_src=""\n'
            'while [ "$#" -gt 0 ]; do case "$1" in --app-source-dir) app_src="$2"; shift 2;; *) shift;; esac; done\n'
            'test -n "$app_src"\n'
            'grep -q "CommittedApp" "$app_src/application.apx"\n'
            'printf "%s\\nPROD_WS\\nAPP_CODE\\nprod_conn\\nAPP_LOGIN\\nproduction\\n" "$app_src"\n',
            encoding="utf-8",
            newline="\n",
        )
        subprocess.run(["git", "add", "."], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Commit bundle and app"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)

        # Dirty app tree on disk -> refused before any SQL runs
        (app_dir / "application.apx").write_text("app DEMO (\n  name: DirtyApp\n)\n", encoding="utf-8", newline="\n")
        code, _, err = self._run_cli(["run", "prod-app-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ", "--no-staging-proof"])
        self.assertEqual(code, 1)
        self.assertIn("clean Git working tree", err)
        self.assertEqual(self._read_state().get("calls", []), [])
        subprocess.run(["git", "checkout", "--", "apps"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)

        # git check-ignore exit >= 2 fails closed
        orig_run = subprocess.run

        def fail_check_ignore(cmd, *args, **kwargs):
            if isinstance(cmd, list) and "check-ignore" in cmd:
                return subprocess.CompletedProcess(cmd, 128, b"", b"fatal: simulated check-ignore error")
            return orig_run(cmd, *args, **kwargs)

        with mock.patch("scripts.release.subprocess.run", side_effect=fail_check_ignore):
            code, _, err = self._run_cli(["run", "prod-app-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ", "--no-staging-proof"])
        self.assertEqual(code, 1)
        self.assertIn("git check-ignore failed", err)

        # Mutating app source on disk AFTER preflight starts cannot affect describe or deploy (both use materialized commit snapshot)
        mutated = False

        def mutate_app_after_preflight(t, driver_path, run_dir, environment=None, phase="apply"):
            nonlocal mutated
            res = release.run_sqlcl(t, driver_path, run_dir, environment=environment, phase=phase)
            if not mutated and run_dir.name.startswith("preflight-"):
                mutated = True
                (app_dir / "application.apx").write_text("app DEMO (\n  name: EvilDiskApp\n)\n", encoding="utf-8", newline="\n")
            return res

        def record_deploy(repo_root: Path, app_id: int, env: str, confirm_target: str, values, app_source_dir=None) -> None:
            self.assertIsNotNone(app_source_dir)
            seen_app_sources.append((app_source_dir / "application.apx").read_text(encoding="utf-8"))
            release._invoke_app_deploy(repo_root, app_id, env, confirm_target, values, app_source_dir=app_source_dir)

        code, _, err = self._run_cli_with_runner(
            ["run", "prod-app-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ", "--no-staging-proof"],
            runner=mutate_app_after_preflight,
            app_deployer=record_deploy,
        )
        self.assertEqual(code, 0, err)
        self.assertTrue(mutated)
        self.assertEqual(len(seen_app_sources), 1)
        self.assertIn("CommittedApp", seen_app_sources[0])
        self.assertNotIn("EvilDiskApp", seen_app_sources[0])

    def test_p1_2_mapped_ddl_strips_owner_rejects_foreign_owner_and_verifies_target_object(self) -> None:
        mapped_env = {
            **BASE_ENV,
            "CODE_SCHEMA": "DEV_OWNER",
            "CODE_SQLCL_CONNECTION": "dev_conn",
            "CODE_EXPECTED_USER": "DEV_OWNER",
            "STAGING_SCHEMA": "STAGE_OWNER",
            "STAGING_SQLCL_CONNECTION": "stage_conn",
            "STAGING_EXPECTED_USER": "APP_LOGIN",
        }
        state = {
            "connMap": {"dev_conn": "dev", "stage_conn": "staging"},
            "envs": {
                "dev": {
                    "identity": {
                        "session_user": "DEV_OWNER",
                        "current_schema": "DEV_OWNER",
                        "db_name": "DEVDB",
                        "db_unique_name": "DEVDB_UNQ",
                        "service_name": "dev_svc",
                        "con_name": "DEVPDB",
                    },
                    "objects": {
                        "VIEW:ORDERS_V": {
                            "source": "",
                            "ddl": '  CREATE OR REPLACE FORCE EDITIONABLE VIEW "DEV_OWNER"."ORDERS_V" AS SELECT 1 AS ID FROM DUAL;',
                        },
                    },
                    "beforeInvalids": [],
                    "afterInvalids": [],
                    "codeErrors": [],
                },
                "staging": {
                    "identity": {
                        "session_user": "APP_LOGIN",
                        "current_schema": "STAGE_OWNER",
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

        self.assertEqual(self._run_cli(["new", "mapped-ddl-rel"], environ=mapped_env)[0], 0)
        code, _, err = self._run_cli([
            "collect", "mapped-ddl-rel", "--from", "dev", "--schema", "DEV_OWNER",
            "--object", "VIEW:ORDERS_V", "--mode", "new",
        ], environ=mapped_env)
        self.assertEqual(code, 0, err)
        bundle = self.root / "releases" / "mapped-ddl-rel"
        view_sql = (bundle / "02-code" / "dev_owner_orders_v_view.sql").read_text(encoding="utf-8")
        self.assertNotIn('"DEV_OWNER".', view_sql)
        self.assertIn('VIEW "ORDERS_V"', view_sql)

        # Missing object in STAGE_OWNER after apply fails post-apply check
        state["envs"]["staging"]["simulateMissingObjects"] = True
        self._write_state(state)
        code, _, err = self._run_cli(
            ["run", "mapped-ddl-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--skip-backup"],
            environ=mapped_env,
        )
        self.assertEqual(code, 1)
        self.assertIn("missing in STAGE_OWNER after apply", err)

        # Explicit foreign owner in 02-code SQL conflicts with resolved target STAGE_OWNER
        state["envs"]["staging"]["simulateMissingObjects"] = False
        self._write_state(state)
        (bundle / "02-code" / "dev_owner_orders_v_view.sql").write_text(
            'CREATE OR REPLACE FORCE VIEW "DEV_OWNER"."ORDERS_V" AS SELECT 1 AS ID FROM DUAL;\n',
            encoding="utf-8",
            newline="\n",
        )
        code, out, _ = self._run_cli(["check", "mapped-ddl-rel", "--env", "staging"], environ=mapped_env)
        self.assertEqual(code, 1)
        self.assertIn("conflicts with resolved target schema STAGE_OWNER", out)

    def test_p1_3_post_apply_verify_identity_guards_and_batch_marker(self) -> None:
        multi_env = {
            **BASE_ENV,
            "CODE_SCHEMA": "RUNERP_CUSTDATA,RUNERP_CODE",
            "CODE_PREFIXES": "*,*",
            "CODE_SQLCL_CONNECTION": "dev_cust,dev_code",
            "CODE_EXPECTED_USER": "RUNERP_CUSTDATA,RUNERP_CODE",
            "STAGING_SCHEMA": "RUNERP_CUSTDATA,RUNERP_CODE",
            "STAGING_SQLCL_CONNECTION": "stg_cust,stg_code",
            "STAGING_EXPECTED_USER": "DEPLOYER,DEPLOYER",
        }
        base_multi_state = {
            "connMap": {"stg_cust": "stg_cust", "stg_code": "stg_code"},
            "envs": {
                "stg_cust": {
                    "identity": {
                        "session_user": "DEPLOYER", "current_schema": "RUNERP_CUSTDATA",
                        "db_name": "STAGEDB", "db_unique_name": "STAGEDB_UNQ", "service_name": "stage_svc", "con_name": "STAGEPDB",
                    },
                    "objects": {}, "beforeInvalids": [], "afterInvalids": [], "codeErrors": [],
                },
                "stg_code": {
                    "identity": {
                        "session_user": "DEPLOYER", "current_schema": "RUNERP_CODE",
                        "db_name": "STAGEDB", "db_unique_name": "STAGEDB_UNQ", "service_name": "stage_svc", "con_name": "STAGEPDB",
                    },
                    "objects": {}, "beforeInvalids": [], "afterInvalids": [], "codeErrors": [],
                },
            },
        }
        self._write_state(base_multi_state)
        self.assertEqual(self._run_cli(["new", "p13-rel"], environ=multi_env)[0], 0)
        bundle = self.root / "releases" / "p13-rel"
        (bundle / "01-ddl" / "001.sql").write_text("CREATE TABLE cust_t (id NUMBER);\n", encoding="utf-8", newline="\n")
        (bundle / "02-code" / "001.sql").write_text("CREATE OR REPLACE PACKAGE code_pkg AS PROCEDURE p; END code_pkg;\n/\n", encoding="utf-8", newline="\n")
        (bundle / "04-verify" / "001.sql").write_text("SELECT 1 FROM DUAL;\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1, "slug": "p13-rel", "status": "draft", "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["RUNERP_CUSTDATA", "RUNERP_CODE"],
            "ddl": [{"schema": "RUNERP_CUSTDATA", "file": "01-ddl/001.sql"}],
            "code": [{"schema": "RUNERP_CODE", "file": "02-code/001.sql", "objectType": "PACKAGE", "objectName": "CODE_PKG", "mode": "new"}],
            "ords": [],
            "verify": [{"schema": "RUNERP_CODE", "file": "04-verify/001.sql"}],
            "apps": [], "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        # (a) Missing RELEASE_BATCH_APPLIED marker fails
        st = json.loads(json.dumps(base_multi_state))
        st["envs"]["stg_cust"]["omitBatchMarkers"] = True
        self._write_state(st)
        code, _, err = self._run_cli(["run", "p13-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--skip-backup"], environ=multi_env)
        self.assertEqual(code, 1)
        self.assertIn("RELEASE_BATCH_APPLIED", err)

        # (b) DB_UNIQUE_NAME drift during post-apply fails with ORA-20901
        self._write_state(base_multi_state)

        def drift_on_post_apply(t, driver_path, run_dir, environment=None, phase="apply"):
            if run_dir.name.startswith("post-apply-"):
                s = self._read_state()
                s["envs"]["stg_cust"]["identity"]["db_unique_name"] = "DRIFTED_DB"
                self._write_state(s)
            return release.run_sqlcl(t, driver_path, run_dir, environment=environment, phase=phase)

        code, _, err = self._run_cli_with_runner(
            ["run", "p13-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--skip-backup"],
            runner=drift_on_post_apply, environ=multi_env,
        )
        self.assertEqual(code, 1)
        self.assertIn("ORA-20901", err)

        # (c) CON_NAME drift during 04-verify fails with ORA-20901
        self._write_state(base_multi_state)

        def drift_on_verify(t, driver_path, run_dir, environment=None, phase="apply"):
            if run_dir.name.startswith("verify-"):
                s = self._read_state()
                s["envs"]["stg_code"]["identity"]["con_name"] = "WRONG_PDB"
                self._write_state(s)
            return release.run_sqlcl(t, driver_path, run_dir, environment=environment, phase=phase)

        code, _, err = self._run_cli_with_runner(
            ["run", "p13-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--skip-backup"],
            runner=drift_on_verify, environ=multi_env,
        )
        self.assertEqual(code, 1)
        self.assertIn("ORA-20901", err)

    def test_p1_4_slug_lock_preflight_tag_check_checkout_lock_and_unique_paths(self) -> None:
        # sample.lock slug rejected
        code, _, err = self._run_cli(["new", "sample.lock"])
        self.assertEqual(code, 2)
        self.assertIn("invalid release slug", err)

        # Existing tag on prod fails BEFORE any SQL runs
        subprocess.run(["git", "init"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        self.assertEqual(self._run_cli(["new", "tag-pre-rel"])[0], 0)
        bundle = self.root / "releases" / "tag-pre-rel"
        (bundle / "01-ddl" / "001.sql").write_text("CREATE TABLE T (ID NUMBER);\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1, "slug": "tag-pre-rel", "status": "draft", "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"], "ddl": [{"schema": "APP_CODE", "file": "01-ddl/001.sql"}],
            "code": [], "ords": [], "verify": [], "apps": [], "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        subprocess.run(["git", "add", "releases"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add bundle"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "tag", "release/tag-pre-rel"], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)

        code, _, err = self._run_cli(["run", "tag-pre-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ", "--no-staging-proof"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual(self._read_state().get("calls", []), [])

        # Exclusive lock prevents concurrent run on same slug
        lock_file = self.root / "scratch" / ".release-tag-pre-rel.lock"
        with release._acquire_release_lock(lock_file, "tag-pre-rel"):
            with self.assertRaises(release.ReleaseError) as ctx:
                with release._acquire_release_lock(lock_file, "tag-pre-rel"):
                    pass
            self.assertIn("already in progress", str(ctx.exception))

        # Two runs in same second produce distinct backup and report paths
        with mock.patch("scripts.release._utc_stamp", return_value="20261124T120000Z"):
            self.assertEqual(self._run_cli(["run", "tag-pre-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ"])[0], 0)
            self.assertEqual(self._run_cli(["run", "tag-pre-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ"])[0], 0)
        backups = list((self.root / "scratch" / "backups").glob("tag-pre-rel-staging-*"))
        reports = list((self.root / "scratch" / "release-reports").glob("tag-pre-rel-staging-*.md"))
        self.assertEqual(len(backups), 2)
        self.assertEqual(len(reports), 2)

    def test_p1_5_ords_skip_backup_requires_allow_unbacked_ords(self) -> None:
        state = self._read_state()
        state["envs"]["staging"]["objects"]["PACKAGE:ORDS_API_PKG"] = {
            "source": "PACKAGE ORDS_API_PKG AS PROCEDURE run_get; END ORDS_API_PKG;\n",
        }
        self._write_state(state)
        self.assertEqual(self._run_cli(["new", "ords-skip-rel"])[0], 0)
        bundle = self.root / "releases" / "ords-skip-rel"
        (bundle / "03-ords" / "001.sql").write_text("BEGIN NULL; END;\n/\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1, "slug": "ords-skip-rel", "status": "draft", "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"], "ddl": [], "code": [],
            "ords": [{"schema": "APP_CODE", "file": "03-ords/001.sql", "objects": ["PACKAGE:ORDS_API_PKG"]}],
            "verify": [], "apps": [], "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        code, out_json, _ = self._run_cli(["check", "ords-skip-rel", "--env", "staging", "--format", "json"])
        self.assertEqual(code, 0)
        plan = json.loads(out_json)
        self.assertEqual(plan["ordsBackingObjects"], "covered")
        self.assertEqual(plan["ordsMetadataCoverage"], "unbacked")
        self.assertEqual(plan["ordsBackupCoverage"], "unbacked")

        # --skip-backup alone still refuses without --allow-unbacked-ords
        code, _, err = self._run_cli(["run", "ords-skip-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--skip-backup"])
        self.assertEqual(code, 1)
        self.assertIn("lack ORDS metadata backup coverage", err)

        code, _, err = self._run_cli([
            "run", "ords-skip-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--skip-backup", "--allow-unbacked-ords",
        ])
        self.assertEqual(code, 0, err)

    def test_p1_6_mode_new_write_session_race_guard_aborts_on_intervening_create(self) -> None:
        self.assertEqual(self._run_cli(["new", "new-guard-rel"])[0], 0)
        bundle = self.root / "releases" / "new-guard-rel"
        (bundle / "02-code" / "new_pkg.sql").write_text(
            "CREATE OR REPLACE PACKAGE NEW_PKG AS PROCEDURE p; END NEW_PKG;\n/\n", encoding="utf-8", newline="\n"
        )
        manifest = {
            "schemaVersion": 1, "slug": "new-guard-rel", "status": "draft", "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"], "ddl": [],
            "code": [{"schema": "APP_CODE", "file": "02-code/new_pkg.sql", "objectType": "PACKAGE", "objectName": "NEW_PKG", "mode": "new"}],
            "ords": [], "verify": [], "apps": [], "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        def create_after_preflight(t, driver_path, run_dir, environment=None, phase="apply"):
            res = release.run_sqlcl(t, driver_path, run_dir, environment=environment, phase=phase)
            if phase == "inventory":
                st = self._read_state()
                st["envs"]["staging"]["objects"]["PACKAGE:NEW_PKG"] = {"source": "PACKAGE NEW_PKG AS END;"}
                self._write_state(st)
            return res

        code, _, err = self._run_cli_with_runner(
            ["run", "new-guard-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--skip-backup"],
            runner=create_after_preflight,
        )
        self.assertEqual(code, 1)
        self.assertIn("ORA-20904", err)
        self.assertEqual(self._read_state().get("appliedSteps", []), [])

        # With --allow-existing, replacement succeeds
        code, _, err = self._run_cli([
            "run", "new-guard-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--skip-backup", "--allow-existing",
        ])
        self.assertEqual(code, 0, err)

    def test_p2_1_missing_publish_app_sh_fails_preflight_without_fallback(self) -> None:
        self.assertEqual(self._run_cli(["new", "no-pub-rel"])[0], 0)
        bundle = self.root / "releases" / "no-pub-rel"
        (bundle / "01-ddl" / "001.sql").write_text("CREATE TABLE T (ID NUMBER);\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1, "slug": "no-pub-rel", "status": "draft", "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"], "ddl": [{"schema": "APP_CODE", "file": "01-ddl/001.sql"}],
            "code": [], "ords": [], "verify": [], "apps": [130], "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        app_dir = self.root / "apps" / "APP_CODE" / "130" / "deployments"
        app_dir.mkdir(parents=True)
        (app_dir / "staging.json").write_text(
            json.dumps({"workspace": {"name": "WS"}, "app": {"id": 130, "databaseSession": {"parsingSchema": "APP_CODE"}}}) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.scripts_dir / "deploy.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8", newline="\n")
        code, out, _ = self._run_cli(["check", "no-pub-rel", "--env", "staging"])
        self.assertEqual(code, 1)
        self.assertIn("scripts/publish_app.sh not found", out)

    def test_p2_2_patch_guard_handles_leading_interior_and_trailing_blank_lines(self) -> None:
        src = "\n\nPACKAGE BODY BLANK_PKG AS\n  FUNCTION a RETURN NUMBER IS\n\n  BEGIN\n    RETURN 1;\n  END;\nEND BLANK_PKG;\n\n\n"
        target = release.Target("staging", "stage_conn", "APP_LOGIN", "APP_CODE", "staging")
        step = release.BundleStep(
            section="code", schema="APP_CODE", rel_file="02-code/blank.sql",
            path=self.root / "blank.sql", object_type="PACKAGE BODY", object_name="BLANK_PKG",
            mode="patch", base_sha256=release.compute_source_sha256(src),
        )
        plsql = "\n".join(release._patch_base_guard_lines(target, step, src))
        self.assertIn("IF l_line IS NULL OR NOT REGEXP_LIKE(l_line, l_code_type_re, 'i') THEN", plsql)
        self.assertIn("IF l_line IS NOT NULL THEN", plsql)

        # Simulate Oracle's exact three-valued NULL + '' == NULL behavior over raw lines
        raw_lines = [line.rstrip("\r") for line in src.split("\n")]
        if raw_lines and raw_lines[-1] == "":
            raw_lines.pop()
        started = False
        pending_blanks = 0
        live_chunks: list[str] = []
        for raw_line in raw_lines:
            l_line = None if raw_line == "" else raw_line
            if l_line is None or not re.match(r"^[ \t]*ALTER[ \t]+SESSION[ \t]+SET[ \t]+PLSQL_CODE_TYPE[ \t]*=[ \t]*INTERPRETED[ \t]*;?[ \t]*$", l_line, re.I):
                if l_line is not None:
                    stripped = re.sub(r"[ \t]+$", "", l_line)
                    l_line = None if stripped == "" else stripped
                if l_line is None:
                    if started:
                        pending_blanks += 1
                else:
                    started = True
                    while pending_blanks > 0:
                        live_chunks.append("\n")
                        pending_blanks -= 1
                    live_chunks.append(l_line + "\n")
        self.assertEqual("".join(live_chunks), release.normalize_source_for_hash(src))

    def test_p2_3_and_p2_4_and_p2_6_ddl_foreign_owner_case_collision_and_warnings(self) -> None:
        self.assertEqual(self._run_cli(["new", "p2-rel"])[0], 0)
        bundle = self.root / "releases" / "p2-rel"
        (bundle / "01-ddl" / "001.sql").write_text("ALTER TABLE OTHER_SCHEMA.ORDERS ADD (X NUMBER);\n", encoding="utf-8", newline="\n")
        (bundle / "02-code" / "orders_pkg.sql").write_text("CREATE OR REPLACE " + DEV_PKG_SOURCE + "/\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1, "slug": "p2-rel", "status": "draft", "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [{"schema": "APP_CODE", "file": "01-ddl/001.sql"}],
            "code": [{"schema": "APP_CODE", "file": "02-code/orders_pkg.sql", "objectType": "PACKAGE BODY", "objectName": "ORDERS_PKG", "mode": "replace"}],
            "ords": [], "verify": [], "apps": [], "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        # P2.3: DDL referencing undeclared schema fails preflight
        code, out, _ = self._run_cli(["check", "p2-rel", "--env", "staging"])
        self.assertEqual(code, 1)
        self.assertIn("has no backup target in manifest schemas", out)

        # Fix DDL, add case-variant duplicate file -> refused
        (bundle / "01-ddl" / "001.sql").write_text("CREATE TABLE T_OK (ID NUMBER);\n", encoding="utf-8", newline="\n")
        manifest["ddl"].append({"schema": "APP_CODE", "file": "01-ddl/001.SQL"})
        (bundle / "01-ddl" / "001.SQL").write_text("CREATE TABLE T_OK (ID NUMBER);\n", encoding="utf-8", newline="\n")
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        code, _, err = self._run_cli(["check", "p2-rel", "--env", "staging"])
        self.assertEqual(code, 1)
        self.assertIn("duplicate bundle file reference", err)

        # Remove duplicate, verify P2.6: ALL_ERRORS filters ATTRIBUTE = 'ERROR' and warnings do NOT fail release
        manifest["ddl"] = [{"schema": "APP_CODE", "file": "01-ddl/001.sql"}]
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        state = self._read_state()
        state["envs"]["staging"]["codeWarnings"] = [
            {"objectType": "PACKAGE BODY", "objectName": "ORDERS_PKG", "line": 2, "position": 1, "text": "PLW-06002: Unreachable code"}
        ]
        self._write_state(state)
        code, out, err = self._run_cli(["run", "p2-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ", "--skip-backup"])
        self.assertEqual(code, 0, out + err)
        apply_calls = [c for c in self._read_state()["calls"] if c["driverName"] == "release-apply.sql"]
        self.assertIn("AND attribute = 'ERROR'", apply_calls[0]["driver"])
        reports = sorted((self.root / "scratch" / "release-reports").glob("p2-rel-staging-*.md"))
        self.assertIn("PLW-06002: Unreachable code", reports[-1].read_text(encoding="utf-8"))

    def test_unterminated_plsql_is_refused_offline_and_terminated_sql_is_accepted(self) -> None:
        def step(section: str, text_name: str, object_type: str | None = None) -> release.BundleStep:
            subdir = dict(release.SECTION_DIRS)[section]
            return release.BundleStep(
                section=section,
                schema="APP_CODE",
                rel_file=f"{subdir}/{text_name}.sql",
                path=self.root / text_name,
                object_type=object_type,
                object_name="OBJ" if object_type else None,
                mode="replace" if object_type else None,
            )

        refused = {
            "proc_no_slash": ("code", "PROCEDURE", "CREATE OR REPLACE PROCEDURE p AS\nBEGIN\n  NULL;\nEND;\n"),
            "pkg_named_end": ("code", "PACKAGE BODY", "CREATE OR REPLACE PACKAGE BODY orders_pkg AS\nEND orders_pkg;\n\n"),
            "end_then_comment": ("code", "FUNCTION", "CREATE FUNCTION f RETURN NUMBER IS\nBEGIN\n  RETURN 1;\nEND; -- done\n"),
            "spec_no_slash": ("code", "PACKAGE", "CREATE OR REPLACE PACKAGE p AS\n  c CONSTANT NUMBER := 1;\nEND p;\n"),
            "second_unit_after_slash": (
                "code",
                "PROCEDURE",
                "CREATE PROCEDURE p AS BEGIN NULL; END;\n/\nCREATE OR REPLACE PROCEDURE q AS BEGIN NULL; END;\n",
            ),
            "type_without_slash": ("code", "TYPE", "CREATE OR REPLACE TYPE t AS OBJECT (id NUMBER);\n"),
            "ddl_block": ("ddl", None, "CREATE TABLE t (id NUMBER);\nBEGIN\n  NULL;\nEND;\n"),
            "ords_block": ("ords", None, "BEGIN\n  ORDS.define_module(p_module_name => 'm');\nEND;\n"),
        }
        for name, (section, object_type, sql) in refused.items():
            with self.subTest(refused=name):
                s = step(section, name, object_type)
                with self.assertRaises(release.ReleaseError) as ctx:
                    release._check_bundle_contents([s], [], step_texts={s.rel_file: sql})
                self.assertIn("'/'", str(ctx.exception))

        accepted = {
            "proc_with_slash": ("code", "PROCEDURE", "CREATE OR REPLACE PROCEDURE p AS\nBEGIN\n  NULL;\nEND;\n/\n"),
            "trigger_disable_tail": (
                "code",
                "TRIGGER",
                'CREATE OR REPLACE TRIGGER trg BEFORE INSERT ON t FOR EACH ROW\nBEGIN\n  NULL;\nEND;\n/\nALTER TRIGGER "TRG" DISABLE;\n',
            ),
            "grant_after_slash": ("code", "PROCEDURE", "CREATE PROCEDURE p AS BEGIN NULL; END;\n/\nGRANT EXECUTE ON p TO app_role;\n"),
            "comment_after_slash": ("code", "PROCEDURE", "CREATE PROCEDURE p AS BEGIN NULL; END;\n/\n-- END;\n"),
            "view": ("code", "VIEW", "CREATE OR REPLACE VIEW v AS SELECT 1 AS id FROM dual;\n"),
            "dml_case_end": ("ddl", None, "UPDATE t SET a = 1\n WHERE b = CASE WHEN c = 1 THEN 2 ELSE 3\nEND;\n"),
            "ddl_block_then_sql": ("ddl", None, "BEGIN\n  NULL;\nEND;\n/\nCREATE TABLE t (id NUMBER);\n"),
            "end_in_string": ("ddl", None, "INSERT INTO t (txt) VALUES ('BEGIN\nEND;');\n"),
        }
        for name, (section, object_type, sql) in accepted.items():
            with self.subTest(accepted=name):
                s = step(section, name, object_type)
                release._check_bundle_contents([s], [], step_texts={s.rel_file: sql})

    def test_unterminated_code_step_refused_before_any_sqlcl_session(self) -> None:
        self.assertEqual(self._run_cli(["new", "noslash-rel"])[0], 0)
        bundle = self.root / "releases" / "noslash-rel"
        (bundle / "02-code" / "orders_pkg_package_body.sql").write_text(
            "CREATE OR REPLACE " + DEV_PKG_SOURCE, encoding="utf-8", newline="\n"
        )
        manifest = {
            "schemaVersion": 1,
            "slug": "noslash-rel",
            "status": "draft",
            "createdAt": "2026-11-25T00:00:00Z",
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

        def runner(*_args, **_kwargs):
            raise AssertionError("no SQLcl session may start for an unterminated PL/SQL step")

        code, _, err = self._run_cli_with_runner(
            ["run", "noslash-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ"], runner=runner
        )
        self.assertEqual(code, 1, err)
        self.assertIn("is not followed by a '/' line", err)
        self.assertFalse((self.root / "scratch" / "backups").exists())

    def test_preflight_sqlcl_failure_in_run_preserves_log(self) -> None:
        self.assertEqual(self._run_cli(["new", "pre-fail-rel"])[0], 0)
        bundle = self.root / "releases" / "pre-fail-rel"
        (bundle / "01-ddl" / "001-t.sql").write_text("CREATE TABLE T (ID NUMBER);\n", encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1,
            "slug": "pre-fail-rel",
            "status": "draft",
            "createdAt": "2026-11-25T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [{"schema": "APP_CODE", "file": "01-ddl/001-t.sql"}],
            "code": [],
            "ords": [],
            "verify": [],
            "apps": [],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        def runner(target, driver, run_dir, **_kwargs):
            raise release.SqlclError("SQLcl failed", run_dir, "ORA-12541: Cannot connect. No listener\n")

        code, _, err = self._run_cli_with_runner(
            ["run", "pre-fail-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ"], runner=runner
        )
        self.assertEqual(code, 1)
        self.assertIn("ORA-12541", err)
        logs = list((self.root / "scratch" / "release-logs").glob("*-preflight-APP_CODE-sqlcl-output.log"))
        self.assertEqual(len(logs), 1, err)
        self.assertIn("ORA-12541", logs[0].read_text(encoding="utf-8"))
        self.assertIn(str(logs[0]), err)

    def test_dbms_output_enable_null_in_all_clob_emitting_drivers(self) -> None:
        target = release.Target("staging", "stage_conn", "APP_LOGIN", "APP_CODE", "staging")
        items = [("PACKAGE", "ORDERS_PKG")]

        fetch_sql = release._render_fetch_objects_driver(target, items)
        self.assertIn("BEGIN\n  DBMS_OUTPUT.ENABLE(NULL);", fetch_sql)

        before_lines = "\n".join(release._before_invalids_lines(target))
        self.assertIn("BEGIN\n  DBMS_OUTPUT.ENABLE(NULL);", before_lines)
        self.assertIn("BEGIN\n  DBMS_OUTPUT.ENABLE(NULL);", release._render_before_invalids_driver(target))

        post_lines = "\n".join(release._post_apply_lines(target, []))
        self.assertIn("BEGIN\n  DBMS_OUTPUT.ENABLE(NULL);", post_lines)
        self.assertIn("BEGIN\n  DBMS_OUTPUT.ENABLE(NULL);", release._render_post_apply_driver(target, []))

        apply_sql = release._render_apply_driver(target, [])
        self.assertEqual(apply_sql.count("DBMS_OUTPUT.ENABLE(NULL);"), 2)

    def test_render_fetch_objects_driver_source_items_filters_source_and_ddl_only(self) -> None:
        target = release.Target("staging", "stage_conn", "APP_LOGIN", "APP_CODE", "staging")
        items = [
            ("PACKAGE", "NEW_PKG"),
            ("PACKAGE BODY", "REPLACE_PKG"),
            ("PACKAGE", "PATCH_PKG"),
            ("VIEW", "REPLACE_V"),
            ("VIEW", "PATCH_V"),
        ]

        filtered_sql = release._render_fetch_objects_driver(
            target,
            items,
            include_ddl=True,
            source_items={("PACKAGE", "PATCH_PKG"), ("VIEW", "PATCH_V")},
        )
        for obj_type, obj_name in items:
            self.assertIn(f"l_item.put('objectName', '{obj_name}');", filtered_sql)
            self.assertIn(f"AND OBJECT_TYPE = '{obj_type}' AND OBJECT_NAME = '{obj_name}';", filtered_sql)

        # ALL_SOURCE only queried for source_items
        self.assertIn("AND TYPE = 'PACKAGE' AND NAME = 'PATCH_PKG'", filtered_sql)
        self.assertIn("AND TYPE = 'VIEW' AND NAME = 'PATCH_V'", filtered_sql)
        self.assertNotIn("AND TYPE = 'PACKAGE' AND NAME = 'NEW_PKG'", filtered_sql)
        self.assertNotIn("AND TYPE = 'PACKAGE BODY' AND NAME = 'REPLACE_PKG'", filtered_sql)
        self.assertNotIn("AND TYPE = 'VIEW' AND NAME = 'REPLACE_V'", filtered_sql)

        # GET_DDL only queried for source_items
        self.assertIn("DBMS_METADATA.GET_DDL('PACKAGE', 'PATCH_PKG', 'APP_CODE')", filtered_sql)
        self.assertIn("DBMS_METADATA.GET_DDL('VIEW', 'PATCH_V', 'APP_CODE')", filtered_sql)
        self.assertNotIn("DBMS_METADATA.GET_DDL('PACKAGE', 'NEW_PKG', 'APP_CODE')", filtered_sql)
        self.assertNotIn("DBMS_METADATA.GET_DDL('VIEW', 'REPLACE_V', 'APP_CODE')", filtered_sql)

        # Empty source_items skips all ALL_SOURCE / GET_DDL while keeping ALL_OBJECTS checks
        empty_src_sql = release._render_fetch_objects_driver(target, items, include_ddl=True, source_items=set())
        self.assertEqual(empty_src_sql.count("FROM ALL_OBJECTS"), len(items))
        self.assertNotIn("FROM ALL_SOURCE", empty_src_sql)
        self.assertNotIn("DBMS_METADATA.GET_DDL", empty_src_sql)

        # source_items=None (default) fetches source/DDL for all items
        full_sql = release._render_fetch_objects_driver(target, items, include_ddl=True)
        self.assertEqual(full_sql.count("FROM ALL_SOURCE"), len(items))
        self.assertEqual(full_sql.count("DBMS_METADATA.GET_DDL"), len(items))

    def test_preflight_fetches_source_only_for_patch_while_backup_fetches_all_existing(self) -> None:
        patch_base = "PACKAGE PATCH_PKG AS FUNCTION f RETURN NUMBER; END PATCH_PKG;\n"
        patch_new = "CREATE OR REPLACE PACKAGE PATCH_PKG AS FUNCTION f RETURN NUMBER; FUNCTION g RETURN NUMBER; END PATCH_PKG;\n/\n"
        view_sql = "CREATE OR REPLACE FORCE VIEW ORDERS_V AS SELECT 1 AS ID FROM DUAL;\n"
        state = self._read_state()
        state["envs"]["staging"]["objects"]["PACKAGE:PATCH_PKG"] = {"source": patch_base}
        state["envs"]["staging"]["objects"]["VIEW:ORDERS_V"] = {"source": "", "ddl": view_sql}
        self._write_state(state)

        self.assertEqual(self._run_cli(["new", "preflight-filter-rel"])[0], 0)
        bundle = self.root / "releases" / "preflight-filter-rel"
        (bundle / "02-code" / "001-new.sql").write_text(
            "CREATE OR REPLACE PACKAGE NEW_PKG AS PROCEDURE p; END NEW_PKG;\n/\n",
            encoding="utf-8",
            newline="\n",
        )
        (bundle / "02-code" / "002-replace.sql").write_text(
            "CREATE OR REPLACE " + DEV_PKG_SOURCE + "/\n",
            encoding="utf-8",
            newline="\n",
        )
        (bundle / "02-code" / "003-replace-view.sql").write_text(view_sql, encoding="utf-8", newline="\n")
        (bundle / "02-code" / "004-patch.sql").write_text(patch_new, encoding="utf-8", newline="\n")
        manifest = {
            "schemaVersion": 1,
            "slug": "preflight-filter-rel",
            "status": "draft",
            "createdAt": "2026-11-24T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [],
            "code": [
                {"schema": "APP_CODE", "file": "02-code/001-new.sql", "objectType": "PACKAGE", "objectName": "NEW_PKG", "mode": "new"},
                {"schema": "APP_CODE", "file": "02-code/002-replace.sql", "objectType": "PACKAGE BODY", "objectName": "ORDERS_PKG", "mode": "replace"},
                {"schema": "APP_CODE", "file": "02-code/003-replace-view.sql", "objectType": "VIEW", "objectName": "ORDERS_V", "mode": "replace"},
                {
                    "schema": "APP_CODE",
                    "file": "02-code/004-patch.sql",
                    "objectType": "PACKAGE",
                    "objectName": "PATCH_PKG",
                    "mode": "patch",
                    "baseSha256": release.compute_source_sha256(patch_base),
                },
            ],
            "ords": [],
            "verify": [],
            "apps": [],
            "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")

        # 1) release check: preflight checks existence for all items, but requests ALL_SOURCE only for patch
        code, out, err = self._run_cli(["check", "preflight-filter-rel", "--env", "staging"])
        self.assertEqual(code, 0, out + err)
        fetch_calls = [c for c in self._read_state()["calls"] if c["driverName"] == "release-fetch.sql"]
        self.assertEqual(len(fetch_calls), 1)
        preflight_driver = fetch_calls[0]["driver"]
        self.assertIn("DBMS_OUTPUT.ENABLE(NULL);", preflight_driver)
        for obj_name in ("NEW_PKG", "ORDERS_PKG", "ORDERS_V", "PATCH_PKG"):
            self.assertIn(f"l_item.put('objectName', '{obj_name}');", preflight_driver)
        self.assertIn("AND TYPE = 'PACKAGE' AND NAME = 'PATCH_PKG'", preflight_driver)
        self.assertNotIn("AND TYPE = 'PACKAGE' AND NAME = 'NEW_PKG'", preflight_driver)
        self.assertNotIn("AND TYPE = 'PACKAGE BODY' AND NAME = 'ORDERS_PKG'", preflight_driver)
        self.assertNotIn("AND TYPE = 'VIEW' AND NAME = 'ORDERS_V'", preflight_driver)
        self.assertNotIn("DBMS_METADATA.GET_DDL", preflight_driver)

        # 2) release run: Step 3 backup still uses source_items=None so all existing objects get full DDL/source backup
        self._write_state(state)
        code, out, err = self._run_cli(["run", "preflight-filter-rel", "--env", "staging", "--confirm-target", "STAGEDB_UNQ"])
        self.assertEqual(code, 0, out + err)
        fetch_calls = [c for c in self._read_state()["calls"] if c["driverName"] == "release-fetch.sql"]
        # ASHARIF 2026-11-25: preflight, backup, then the read-only live-source read for state/staging.json
        self.assertEqual(len(fetch_calls), 3)
        backup_driver = fetch_calls[1]["driver"]
        self.assertIn("DBMS_OUTPUT.ENABLE(NULL);", backup_driver)
        for obj_name in ("ORDERS_PKG", "ORDERS_V", "PATCH_PKG"):
            self.assertIn(f"AND NAME = '{obj_name}'", backup_driver)
            self.assertIn(f"'{obj_name}', 'APP_CODE'", backup_driver)
        self.assertNotIn("NEW_PKG", backup_driver)

        backups = sorted((self.root / "scratch" / "backups").glob("preflight-filter-rel-staging-*"))
        self.assertEqual(len(backups), 1)
        bmanifest = json.loads((backups[0] / "backup-manifest.json").read_text(encoding="utf-8"))
        backed_names = {(o["objectType"], o["objectName"]) for o in bmanifest["objects"]}
        self.assertEqual(
            backed_names,
            {("PACKAGE BODY", "ORDERS_PKG"), ("VIEW", "ORDERS_V"), ("PACKAGE", "PATCH_PKG")},
        )
        for entry in bmanifest["objects"]:
            self.assertTrue((backups[0] / entry["file"]).read_text(encoding="utf-8").strip())

        # 3) mode: "new" existence check still fails when the object exists, even without fetching its source
        state_existing_new = json.loads(json.dumps(state))
        state_existing_new["envs"]["staging"]["objects"]["PACKAGE:NEW_PKG"] = {"source": "PACKAGE NEW_PKG AS END;"}
        self._write_state(state_existing_new)
        code, out, _ = self._run_cli(["check", "preflight-filter-rel", "--env", "staging"])
        self.assertEqual(code, 1)
        self.assertIn("APP_CODE.NEW_PKG (PACKAGE) already exists in staging", out)


    # ---- ASHARIF 2026-11-25: per-environment release state (state/<env>.json), staged status, delta reruns ----

    def _make_state_bundle(self, slug: str, *, code_mode: str = "replace", apps: list[int] | None = None,
                           base_sha: str | None = None) -> Path:
        self.assertEqual(self._run_cli(["new", slug])[0], 0)
        bundle = self.root / "releases" / slug
        (bundle / "01-ddl" / "001-add-col.sql").write_text(
            "ALTER TABLE ORDERS ADD (NOTES VARCHAR2(50));\n", encoding="utf-8", newline="\n")
        (bundle / "02-code" / "orders_pkg_package_body.sql").write_text(
            "CREATE OR REPLACE " + DEV_PKG_SOURCE + "/\n", encoding="utf-8", newline="\n")
        (bundle / "04-verify" / "001-smoke.sql").write_text("SELECT 1 FROM DUAL;\n", encoding="utf-8", newline="\n")
        code_entry = {"schema": "APP_CODE", "file": "02-code/orders_pkg_package_body.sql",
                      "objectType": "PACKAGE BODY", "objectName": "ORDERS_PKG", "mode": code_mode}
        if code_mode == "patch":
            code_entry["baseSha256"] = base_sha or release.compute_source_sha256(TARGET_PKG_SOURCE)
        manifest = {
            "schemaVersion": 1, "slug": slug, "status": "draft", "createdAt": "2026-11-25T00:00:00Z",
            "schemas": ["APP_CODE"],
            "ddl": [{"schema": "APP_CODE", "file": "01-ddl/001-add-col.sql"}],
            "code": [code_entry], "ords": [],
            "verify": [{"schema": "APP_CODE", "file": "04-verify/001-smoke.sql"}],
            "apps": apps or [], "forbiddenPatterns": [],
        }
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        return bundle

    def _edit_code(self, bundle: Path, value: str = "3") -> None:
        (bundle / "02-code" / "orders_pkg_package_body.sql").write_text(
            "CREATE OR REPLACE " + DEV_PKG_SOURCE.replace("RETURN 2", f"RETURN {value}") + "/\n",
            encoding="utf-8", newline="\n")

    def _stage(self, slug: str, *extra: str, app_deployer=None) -> tuple[int, str, str]:
        return self._run_cli(["run", slug, "--env", "staging", "--confirm-target", "STAGEDB_UNQ", *extra],
                             app_deployer=app_deployer)

    def _applied_since(self, state: dict) -> list[str]:
        return [step for step in state.get("appliedSteps", [])]

    def _reset_calls(self) -> None:
        state = self._read_state()
        state["calls"] = []
        state["appliedSteps"] = []
        self._write_state(state)

    def _git_init(self) -> None:
        for args in (["init"], ["config", "user.name", "Test"], ["config", "user.email", "test@example.com"]):
            subprocess.run(["git", *args], cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)

    def _git_commit_all(self, message: str) -> None:
        subprocess.run(["git", "add", "-A", "releases", "apps"] if (self.root / "apps").exists() else ["git", "add", "-A", "releases"],
                       cwd=self.root, stdin=subprocess.DEVNULL, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.root, stdin=subprocess.DEVNULL,
                       check=True, capture_output=True)

    def test_manifest_accepts_staged_status(self) -> None:
        bundle = self._make_state_bundle("status-rel")
        manifest = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        manifest["status"] = "staged"
        release._load_and_validate_manifest(bundle, "status-rel", manifest_override=manifest)
        manifest["status"] = "bogus"
        with self.assertRaisesRegex(release.ReleaseError, "'draft', 'staged' or 'deployed'"):
            release._load_and_validate_manifest(bundle, "status-rel", manifest_override=manifest)

    def test_collect_on_staged_returns_to_draft_and_drops_staged_fields(self) -> None:
        bundle = self._make_state_bundle("collect-staged-rel")
        manifest = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        manifest.update({"status": "staged", "stagedAt": "T", "stagedTarget": "STAGEDB_UNQ", "stagedCommit": "c"})
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        code, out, err = self._run_cli([
            "collect", "collect-staged-rel", "--from", "dev", "--schema", "APP_CODE",
            "--object", "PACKAGE_BODY:ORDERS_PKG", "--mode", "replace", "--file", "orders_pkg_package_body.sql",
        ])
        self.assertEqual(code, 0, err)
        self.assertIn("status returned to draft", out)
        after = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(after["status"], "draft")
        for key in ("stagedAt", "stagedTarget", "stagedCommit"):
            self.assertNotIn(key, after)

    def test_list_shows_staged_stale_and_not_on_staging_counts(self) -> None:
        bundle = self._make_state_bundle("list-rel")
        code, out, _ = self._run_cli(["list"])
        self.assertEqual(code, 0)
        self.assertIn("1- list-rel [draft]", out)
        self.assertEqual(self._stage("list-rel")[0], 0)
        code, out, _ = self._run_cli(["list"])
        self.assertIn("1- list-rel [staged]", out)
        self._edit_code(bundle)
        code, out, _ = self._run_cli(["list"])
        self.assertIn("1- list-rel [staged (stale: 1 changed)]", out)
        listing = json.loads(self._run_cli(["list", "--format", "json"])[1])
        self.assertEqual(listing["draft"][0]["effectiveStatus"], "stale")
        self.assertEqual(listing["draft"][0]["notOnStaging"], ["02-code/orders_pkg_package_body.sql"])
        manifest = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        manifest["status"] = "draft"
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        code, out, _ = self._run_cli(["list"])
        self.assertIn("1- list-rel [draft: 1 of 2 steps not on staging]", out)

    def test_staging_rerun_applies_only_changed_and_marks_staged(self) -> None:
        bundle = self._make_state_bundle("delta-rel")
        code, out, err = self._stage("delta-rel")
        self.assertEqual(code, 0, err)
        self.assertIn("commit releases/delta-rel/release.json and releases/delta-rel/state/staging.json", out)
        manifest = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "staged")
        self.assertEqual(manifest["stagedTarget"], "STAGEDB_UNQ")
        self.assertTrue(manifest["stagedAt"])
        self.assertTrue(manifest["stagedCommit"])
        st = json.loads((bundle / "state" / "staging.json").read_text(encoding="utf-8"))
        self.assertEqual(set(st["steps"]), {"01-ddl/001-add-col.sql", "02-code/orders_pkg_package_body.sql"})
        self.assertIn("liveSha256", st["steps"]["02-code/orders_pkg_package_body.sql"])
        self.assertNotIn("liveSha256", st["steps"]["01-ddl/001-add-col.sql"])
        run = st["lastRun"]
        self.assertEqual((run["mode"], run["applied"], run["skipped"]), ("delta", 2, 0))
        self.assertEqual(run["identities"]["APP_CODE"]["dbUniqueName"], "STAGEDB_UNQ")
        self.assertEqual(run["identities"]["APP_CODE"]["user"], "APP_LOGIN")
        self.assertEqual(run["identities"]["APP_CODE"]["invalidBefore"], 1)
        self.assertEqual(run["identities"]["APP_CODE"]["newInvalid"], 0)
        self.assertTrue({"preflight", "backup", "apply", "verify", "apps"} <= set(run["timings"]))
        self.assertTrue(run["report"].startswith("scratch/release-reports/delta-rel-staging-"))

        self._edit_code(bundle)
        self._reset_calls()
        code, out, err = self._stage("delta-rel")
        self.assertEqual(code, 0, err)
        self.assertEqual(self._read_state()["appliedSteps"], ["02-code/orders_pkg_package_body.sql"])
        st2 = json.loads((bundle / "state" / "staging.json").read_text(encoding="utf-8"))
        self.assertEqual((st2["lastRun"]["applied"], st2["lastRun"]["skipped"]), (1, 1))
        self.assertEqual(st2["steps"]["01-ddl/001-add-col.sql"], st["steps"]["01-ddl/001-add-col.sql"])
        self.assertEqual(json.loads((bundle / "release.json").read_text(encoding="utf-8"))["status"], "staged")
        report = Path(re.search(r"Report:\s*(.+\.md)", out).group(1).strip()).read_text(encoding="utf-8")
        self.assertIn("- **Mode:** delta (applied 1, skipped 1)", report)

    def test_noop_delta_skips_apply_runs_verify(self) -> None:
        bundle = self._make_state_bundle("noop-rel")
        self.assertEqual(self._stage("noop-rel")[0], 0)
        self._reset_calls()
        code, _, err = self._stage("noop-rel")
        self.assertEqual(code, 0, err)
        names = [c["driverName"] for c in self._read_state()["calls"]]
        self.assertNotIn("release-apply.sql", names)
        self.assertIn("release-verify.sql", names)
        st = json.loads((bundle / "state" / "staging.json").read_text(encoding="utf-8"))
        self.assertEqual((st["lastRun"]["applied"], st["lastRun"]["skipped"]), (0, 2))
        self.assertEqual(self._read_state()["appliedSteps"], [])

    def test_full_rerun_applies_everything(self) -> None:
        bundle = self._make_state_bundle("full-rel")
        self.assertEqual(self._stage("full-rel")[0], 0)
        self._reset_calls()
        code, _, err = self._stage("full-rel", "--full")
        self.assertEqual(code, 0, err)
        self.assertEqual(self._read_state()["appliedSteps"],
                         ["01-ddl/001-add-col.sql", "02-code/orders_pkg_package_body.sql"])
        st = json.loads((bundle / "state" / "staging.json").read_text(encoding="utf-8"))
        self.assertEqual(st["lastRun"]["mode"], "full")

    def test_failed_run_writes_no_state_and_keeps_manifest(self) -> None:
        bundle = self._make_state_bundle("fail-rel")
        self.assertEqual(self._stage("fail-rel")[0], 0)
        before_state = (bundle / "state" / "staging.json").read_bytes()
        self._edit_code(bundle)
        before_manifest = (bundle / "release.json").read_bytes()
        state = self._read_state()
        state["envs"]["staging"]["failVerify"] = True
        self._write_state(state)
        code, _, _ = self._stage("fail-rel")
        self.assertEqual(code, 1)
        self.assertEqual((bundle / "state" / "staging.json").read_bytes(), before_state)
        self.assertEqual((bundle / "release.json").read_bytes(), before_manifest)

    # ---- ASHARIF 2026-11-25: failed-run record (state/<env>.partial.json) and --resume ----

    def _make_two_ddl_bundle(self, slug: str) -> Path:
        bundle = self._make_state_bundle(slug)
        (bundle / "01-ddl" / "002-add-note.sql").write_text(
            "ALTER TABLE ORDERS ADD (NOTE_TXT VARCHAR2(50));\n", encoding="utf-8", newline="\n")
        manifest = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        manifest["ddl"].append({"schema": "APP_CODE", "file": "01-ddl/002-add-note.sql"})
        (bundle / "release.json").write_text(json.dumps(manifest), encoding="utf-8", newline="\n")
        return bundle

    def _fail_at(self, step: str | None) -> None:
        state = self._read_state()
        if step is None:
            state["envs"]["staging"].pop("failAtStep", None)
        else:
            state["envs"]["staging"]["failAtStep"] = step
        state["calls"] = []
        state["appliedSteps"] = []
        self._write_state(state)

    def test_ddl_steps_commit_before_their_end_marker(self) -> None:
        self._make_two_ddl_bundle("commit-rel")
        self.assertEqual(self._stage("commit-rel")[0], 0)
        apply_driver = next(c["driver"] for c in self._read_state()["calls"] if c["driverName"] == "release-apply.sql")
        self.assertIn("ALTER TABLE ORDERS ADD (NOTES VARCHAR2(50));\nCOMMIT;\nPROMPT RELEASE_STEP_END:01-ddl/001-add-col.sql", apply_driver)
        self.assertNotIn("COMMIT;\nPROMPT RELEASE_STEP_END:02-code/", apply_driver)

    def test_failed_run_records_finished_ddl_and_resume_skips_them(self) -> None:
        bundle = self._make_two_ddl_bundle("resume-rel")
        self._fail_at("01-ddl/002-add-note.sql")
        code, out, err = self._stage("resume-rel")
        self.assertEqual(code, 1)
        # Verbose run output: a before and an after line for each file
        self.assertIn("[Step 4/6] Apply", out)
        self.assertIn("[1/3] start    01-ddl/001-add-col.sql  [APP_CODE, DDL]", out)
        self.assertIn("[1/3] done     01-ddl/001-add-col.sql", out)
        self.assertIn("[2/3] FAILED   01-ddl/002-add-note.sql", out)
        self.assertIn("[3/3] not run  02-code/orders_pkg_package_body.sql", out)
        self.assertIn("rerun with --resume", err)
        # state/staging.json still describes successful runs only
        self.assertFalse((bundle / "state" / "staging.json").exists())
        partial = json.loads((bundle / "state" / "staging.partial.json").read_text(encoding="utf-8"))
        self.assertEqual(partial["kind"], "partial")
        self.assertEqual(list(partial["steps"]), ["01-ddl/001-add-col.sql"])
        self.assertIn("ORA-00904", partial["error"])
        self.assertNotIn(str(self.root), partial["error"])  # no local paths in a file that may be committed
        report = Path(re.search(r"Report:\s*(.+\.md)", err).group(1).strip()).read_text(encoding="utf-8")
        self.assertIn("staging.partial.json` (written (1 finished 01-ddl step(s)))", report)

        # Without --resume, check explains the record and plans every step
        code, out, _ = self._run_cli(["check", "resume-rel", "--env", "staging"])
        self.assertEqual(code, 0)
        self.assertIn("NOTE: a failed run left releases/resume-rel/state/staging.partial.json", out)
        self.assertIn("apply: 01-ddl/001-add-col.sql", out)
        code, out, _ = self._run_cli(["check", "resume-rel", "--env", "staging", "--resume"])
        self.assertIn("resume: 01-ddl/001-add-col.sql (finished by the failed run; skipped by --resume)", out)

        # Fixed: --resume starts at the failed file, then the run records every step and drops the partial record
        self._fail_at(None)
        code, out, err = self._stage("resume-rel", "--resume")
        self.assertEqual(code, 0, err)
        self.assertEqual(self._read_state()["appliedSteps"],
                         ["01-ddl/002-add-note.sql", "02-code/orders_pkg_package_body.sql"])
        self.assertFalse((bundle / "state" / "staging.partial.json").exists())
        st = json.loads((bundle / "state" / "staging.json").read_text(encoding="utf-8"))
        self.assertEqual(set(st["steps"]), {"01-ddl/001-add-col.sql", "01-ddl/002-add-note.sql",
                                            "02-code/orders_pkg_package_body.sql"})
        self.assertEqual(st["lastRun"]["resumed"], ["01-ddl/001-add-col.sql"])
        self.assertEqual(json.loads((bundle / "release.json").read_text(encoding="utf-8"))["status"], "staged")
        self.assertIn("Removed releases/resume-rel/state/staging.partial.json", out)
        self.assertIn("[1/1] done     04-verify/001-smoke.sql", out)

    def test_resume_reapplies_a_finished_ddl_file_edited_since(self) -> None:
        bundle = self._make_two_ddl_bundle("resume-edit-rel")
        self._fail_at("01-ddl/002-add-note.sql")
        self.assertEqual(self._stage("resume-edit-rel")[0], 1)
        (bundle / "01-ddl" / "001-add-col.sql").write_text(
            "ALTER TABLE ORDERS ADD (NOTES VARCHAR2(80));\n", encoding="utf-8", newline="\n")
        self._fail_at(None)
        code, out, _ = self._run_cli(["check", "resume-edit-rel", "--env", "staging", "--resume"])
        self.assertIn("apply: 01-ddl/001-add-col.sql (finished by the failed run, but edited since; runs again)", out)
        code, _, err = self._stage("resume-edit-rel", "--resume")
        self.assertEqual(code, 0, err)
        self.assertEqual(self._read_state()["appliedSteps"][0], "01-ddl/001-add-col.sql")

    def test_resume_refusals(self) -> None:
        self._make_two_ddl_bundle("resume-refuse-rel")
        code, _, err = self._stage("resume-refuse-rel", "--resume")
        self.assertEqual(code, 1)
        self.assertIn("no failed-run record releases/resume-refuse-rel/state/staging.partial.json", err)
        self._fail_at("01-ddl/002-add-note.sql")
        self.assertEqual(self._stage("resume-refuse-rel")[0], 1)
        code, _, err = self._stage("resume-refuse-rel", "--resume", "--full")
        self.assertEqual(code, 1)
        self.assertIn("--resume and --full cannot be combined", err)

    def test_drifted_skipped_object_is_reapplied(self) -> None:
        bundle = self._make_state_bundle("drift-rel")
        self.assertEqual(self._stage("drift-rel")[0], 0)
        state = self._read_state()
        state["envs"]["staging"]["objects"]["PACKAGE BODY:ORDERS_PKG"] = {"source": "PACKAGE BODY ORDERS_PKG AS HOTFIX END;\n"}
        state["calls"] = []
        state["appliedSteps"] = []
        self._write_state(state)
        code, out, _ = self._run_cli(["check", "drift-rel", "--env", "staging"])
        self.assertEqual(code, 0)
        self.assertIn("drift: 02-code/orders_pkg_package_body.sql (changed on staging since our apply)", out)
        self.assertIn("skip:  01-ddl/001-add-col.sql", out)
        code, out, err = self._stage("drift-rel")
        self.assertEqual(code, 0, err)
        self.assertEqual(self._read_state()["appliedSteps"], ["02-code/orders_pkg_package_body.sql"])
        st = json.loads((bundle / "state" / "staging.json").read_text(encoding="utf-8"))
        self.assertEqual(st["lastRun"]["drifted"], ["02-code/orders_pkg_package_body.sql"])

    def test_patch_rerun_accepts_recorded_live_hash(self) -> None:
        state = self._read_state()
        state["envs"]["staging"]["replaceOnApply"] = True
        self._write_state(state)
        bundle = self._make_state_bundle("patch-rerun-rel", code_mode="patch")
        code, _, err = self._stage("patch-rerun-rel")
        self.assertEqual(code, 0, err)
        self._edit_code(bundle)
        self._reset_calls()
        code, _, err = self._stage("patch-rerun-rel")
        self.assertEqual(code, 0, err)
        self.assertEqual(self._read_state()["appliedSteps"], ["02-code/orders_pkg_package_body.sql"])
        # A hot-fix on staging after our apply is a conflict for a patch step, not a silent overwrite
        state = self._read_state()
        state["envs"]["staging"]["objects"]["PACKAGE BODY:ORDERS_PKG"] = {"source": "PACKAGE BODY ORDERS_PKG AS HOTFIX END;\n"}
        self._write_state(state)
        self._edit_code(bundle, "4")
        code, _, err = self._stage("patch-rerun-rel")
        self.assertEqual(code, 1)
        self.assertIn("patch baseSha256 mismatch", err)

    def test_unchanged_app_is_not_redeployed(self) -> None:
        bundle = self._make_state_bundle("app-delta-rel", apps=[130])
        descriptor = self.root / "apps" / "APP_CODE" / "130" / "deployments" / "staging.json"
        descriptor.parent.mkdir(parents=True)
        descriptor.write_text("{}\n", encoding="utf-8")
        deployed: list[int] = []

        def deploy(_root, app_id, _env, _confirm, _vals) -> None:
            deployed.append(app_id)

        self.assertEqual(self._stage("app-delta-rel", app_deployer=deploy)[0], 0)
        self.assertEqual(deployed, [130])
        st = json.loads((bundle / "state" / "staging.json").read_text(encoding="utf-8"))
        self.assertIn("130", st["apps"])
        self._edit_code(bundle)
        self.assertEqual(self._stage("app-delta-rel", app_deployer=deploy)[0], 0)
        self.assertEqual(deployed, [130])
        (self.root / "apps" / "APP_CODE" / "130" / "application.apx").write_text("changed\n", encoding="utf-8")
        self.assertEqual(self._stage("app-delta-rel", app_deployer=deploy)[0], 0)
        self.assertEqual(deployed, [130, 130])

    def test_dev_run_records_state_without_status_change(self) -> None:
        bundle = self._make_state_bundle("dev-state-rel")
        code, out, err = self._run_cli(["run", "dev-state-rel", "--env", "dev", "--confirm-target", "DEVDB_UNQ"])
        self.assertEqual(code, 0, err)
        self.assertTrue((bundle / "state" / "dev.json").is_file())
        self.assertFalse((bundle / "state" / "staging.json").exists())
        self.assertEqual(json.loads((bundle / "release.json").read_text(encoding="utf-8"))["status"], "draft")
        self.assertIn("commit releases/dev-state-rel/state/dev.json", out)

    def test_check_lists_apply_skip_and_drift(self) -> None:
        bundle = self._make_state_bundle("check-delta-rel")
        self.assertEqual(self._stage("check-delta-rel")[0], 0)
        self._edit_code(bundle)
        code, out, _ = self._run_cli(["check", "check-delta-rel", "--env", "staging"])
        self.assertEqual(code, 0, out)
        self.assertIn("apply: 02-code/orders_pkg_package_body.sql", out)
        self.assertIn("skip:  01-ddl/001-add-col.sql", out)
        plan = json.loads(self._run_cli(["check", "check-delta-rel", "--env", "staging", "--format", "json"])[1])
        self.assertEqual(plan["apply"], ["02-code/orders_pkg_package_body.sql"])
        self.assertEqual(plan["skipped"], ["01-ddl/001-add-col.sql"])
        self.assertEqual(plan["drifted"], [])
        plan_full = json.loads(self._run_cli(["check", "check-delta-rel", "--env", "staging", "--format", "json", "--full"])[1])
        self.assertEqual(plan_full["skipped"], [])

    def test_prod_refuses_without_staged_record(self) -> None:
        self._git_init()
        self._make_state_bundle("prod-gate-rel")
        self._git_commit_all("bundle")
        code, _, err = self._run_cli(["run", "prod-gate-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ"])
        self.assertEqual(code, 1)
        self.assertIn("not staged", err)
        self.assertIn("--no-staging-proof", err)

    def test_prod_refuses_stale_staged_bundle_naming_file(self) -> None:
        self._git_init()
        bundle = self._make_state_bundle("prod-stale-rel")
        self.assertEqual(self._stage("prod-stale-rel")[0], 0)
        self._edit_code(bundle)
        self._git_commit_all("staged then edited")
        code, _, err = self._run_cli(["run", "prod-stale-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ"])
        self.assertEqual(code, 1)
        self.assertIn("differs from what staging received: 02-code/orders_pkg_package_body.sql", err)

    def test_prod_no_staging_proof_records_override(self) -> None:
        self._git_init()
        bundle = self._make_state_bundle("prod-override-rel")
        self._git_commit_all("bundle")
        code, out, err = self._run_cli(["run", "prod-override-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ",
                                        "--no-staging-proof"])
        self.assertEqual(code, 0, err)
        locked = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(locked["status"], "deployed")
        self.assertIs(locked["deployedWithoutStagingProof"], True)
        self.assertTrue((bundle / "state" / "prod.json").is_file())
        report = Path(re.search(r"Report:\s*(.+\.md)", out).group(1).strip()).read_text(encoding="utf-8")
        self.assertIn("- **Staging proof:** skipped (--no-staging-proof)", report)

    def test_prod_after_staging_runs_all_and_writes_prod_state(self) -> None:
        self._git_init()
        bundle = self._make_state_bundle("prod-staged-rel")
        self.assertEqual(self._stage("prod-staged-rel")[0], 0)
        self._git_commit_all("staged")
        self._reset_calls()
        code, out, err = self._run_cli(["run", "prod-staged-rel", "--env", "prod", "--confirm-target", "PRODDB_UNQ"])
        self.assertEqual(code, 0, err)
        self.assertIn("Next: commit releases/prod-staged-rel/release.json and releases/prod-staged-rel/state/prod.json", out)
        self.assertEqual(self._read_state()["appliedSteps"],
                         ["01-ddl/001-add-col.sql", "02-code/orders_pkg_package_body.sql"])
        locked = json.loads((bundle / "release.json").read_text(encoding="utf-8"))
        self.assertEqual(locked["status"], "deployed")
        self.assertNotIn("deployedWithoutStagingProof", locked)
        prod_state = json.loads((bundle / "state" / "prod.json").read_text(encoding="utf-8"))
        self.assertEqual((prod_state["lastRun"]["mode"], prod_state["lastRun"]["applied"]), ("full", 2))


    def test_status_table_text_and_json(self) -> None:
        bundle = self._make_state_bundle("status-tbl-rel")
        self.assertEqual(self._stage("status-tbl-rel")[0], 0)
        self._edit_code(bundle)
        code, out, err = self._run_cli(["status", "status-tbl-rel"])
        self.assertEqual(code, 0, err)
        self.assertIn("status-tbl-rel   status: staged (effective: stale)", out)
        self.assertIn("last runs: dev -, staging ", out)
        self.assertRegex(out, r"01-ddl/001-add-col\.sql\s+-\s+ok\s+-")
        self.assertRegex(out, r"02-code/orders_pkg_package_body\.sql\s+-\s+old\s+-")
        self.assertIn("legend: ok = current version applied, old = an older version applied, - = never applied", out)
        doc = json.loads(self._run_cli(["status", "status-tbl-rel", "--format", "json"])[1])
        self.assertEqual(doc["effectiveStatus"], "stale")
        rows = {r["item"]: r for r in doc["rows"]}
        self.assertEqual(rows["01-ddl/001-add-col.sql"], {"item": "01-ddl/001-add-col.sql", "dev": "-", "staging": "ok", "prod": "-"})
        self.assertEqual(rows["02-code/orders_pkg_package_body.sql"]["staging"], "old")
        calls = self._read_state()["calls"]
        self.assertFalse(any(c["driverName"] != "release-fetch.sql" and "release-run" in c["driverName"] for c in calls))

    def test_status_live_reports_drift(self) -> None:
        self._make_state_bundle("status-live-rel")
        self.assertEqual(self._stage("status-live-rel")[0], 0)
        state = self._read_state()
        state["envs"]["staging"]["objects"]["PACKAGE BODY:ORDERS_PKG"] = {"source": "PACKAGE BODY ORDERS_PKG AS HOTFIX END;\n"}
        state["calls"] = []
        self._write_state(state)
        code, out, err = self._run_cli(["status", "status-live-rel", "--live", "--env", "staging"])
        self.assertEqual(code, 0, err)
        self.assertIn("drift on staging: 02-code/orders_pkg_package_body.sql (changed since our apply)", out)
        names = [c["driverName"] for c in self._read_state()["calls"]]
        self.assertEqual(names, ["release-fetch.sql"])
        self.assertEqual(self._run_cli(["status", "status-live-rel", "--live"])[0], 2)


if __name__ == "__main__":
    unittest.main()
