import json
import re
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
import _no_real_sqlcl  # noqa: F401  (keeps tests away from a real SQLcl)
import _console_ctrl_c
import fake_sqlcl
from fake_sqlcl import BASH


ROOT = Path(__file__).resolve().parents[1]
PUBLISH = ROOT / "scripts" / "publish_app.sh"
# .env.example uses DEVELOPER_NAME=ALICE; the date is the workstation's today.
STAMPED_VERSION = r"^Release 1\.0 \[ALICE-\d{{4}}-\d{{2}}-\d{{2}}r{counter}\]$"


class PublishAppCliTests(unittest.TestCase):
    def test_lifecycle_failure_before_or_after_import_preserves_baseline(self):
        launchers=[('bash',[BASH])] + ([('pwsh',[shutil.which('pwsh'),'-NoProfile','-File'])] if shutil.which('pwsh') else [])
        for name,launcher in launchers:
            for mode in ('pre-unavailable','post-unavailable','automation-disabled','lost-instance'):
                with self.subTest(wrapper=name,mode=mode),tempfile.TemporaryDirectory() as temporary:
                    root=Path(temporary); runner,_,_,environment,app,state=self.make_stateful_dev_fixture(root)
                    environment['PROJECT_ENV_FILE']=str(root/'.env'); environment['FAKE_LIFECYCLE_MODE']=mode
                    baseline=(app/'apex-team-export.json').read_bytes()
                    script=runner if name=='bash' else runner.with_suffix('.ps1')
                    result=subprocess.run([*launcher,str(script),'100'],env=environment,capture_output=True,text=True)
                    self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                    self.assertEqual((app/'apex-team-export.json').read_bytes(),baseline)
                    self.assertEqual((state/'import-count.txt').read_text().strip(),'0' if mode=='pre-unavailable' else '1')
                    if mode!='pre-unavailable':
                        self.assertNotIn('release',Path(environment['FAKE_LOCK_CALLS']).read_text().splitlines())
                        reports=list((root/'scratch').rglob('publish-verification.json'))
                        report=json.loads(reports[0].read_text())
                        self.assertTrue(report['sourceVerified'])
                        self.assertIn(report['lifecycleStatus'],('attention','unavailable'))

    def test_automatic_supporting_objects_refuse_before_native_lock_or_import(self):
        launchers = [('bash', [BASH])] + ([('pwsh', [shutil.which('pwsh'), '-NoProfile', '-File'])] if shutil.which('pwsh') else [])
        for name, launcher in launchers:
            with self.subTest(wrapper=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runner, sql_log, _, environment, app, state = self.make_stateful_dev_fixture(root)
                environment['PROJECT_ENV_FILE'] = str(root / '.env')
                support = app / 'supporting-objects/supporting-objects.apx'
                support.parent.mkdir()
                support.write_text('supportingObject (\n    advanced {\n        includeInAppExport: autoInstall\n    }\n)\n')
                script = runner if name == 'bash' else runner.with_suffix('.ps1')
                result = subprocess.run([*launcher, str(script), '100', '--force'], env=environment, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('automatic supporting-object execution', result.stdout + result.stderr)
                self.assertFalse(sql_log.exists())
                self.assertEqual((state / 'import-count.txt').read_text().strip(), '0')
                self.assertFalse(Path(environment['FAKE_LOCK_CALLS']).exists())

    def test_effective_deployment_mismatch_retains_native_lock_and_old_baseline(self):
        launchers=[('bash',[BASH])] + ([('pwsh',[shutil.which('pwsh'),'-NoProfile','-File'])] if shutil.which('pwsh') else [])
        for name,launcher in launchers:
            with self.subTest(wrapper=name),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                runner,_,_,environment,app,_=self.make_stateful_dev_fixture(root)
                environment['PROJECT_ENV_FILE']=str(root/'.env')
                environment['FAKE_DEPLOYMENT_MISMATCH']='1'
                baseline=(app/'apex-team-export.json').read_bytes()
                script=runner if name=='bash' else runner.with_suffix('.ps1')
                result=subprocess.run([*launcher,str(script),'100'],env=environment,capture_output=True,text=True)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn('effective deployment mismatch: workspace.name',result.stdout+result.stderr)
                self.assertEqual((app/'apex-team-export.json').read_bytes(),baseline)
                records=list((root/'.sync-state/application-locks/100').glob('publish.*/recovery.json'))
                self.assertEqual(json.loads(records[0].read_text())['status'],'held')

    def test_preexisting_native_lock_or_invalid_user_refuses_even_with_force(self):
        launchers=[('bash',[BASH])] + ([('pwsh',[shutil.which('pwsh'),'-NoProfile','-File'])] if shutil.which('pwsh') else [])
        for name,launcher in launchers:
            for mode in ('same-owner','other-owner','unknown-user','no-developer-role','working-copy'):
                with self.subTest(wrapper=name,mode=mode), tempfile.TemporaryDirectory() as temporary:
                    root=Path(temporary)
                    runner,sql_log,_,environment,app,state=self.make_stateful_dev_fixture(root)
                    environment['PROJECT_ENV_FILE']=str(root/'.env')
                    environment['FAKE_APEX_LOCK_MODE']=mode
                    original=(app/'application.apx').read_bytes()
                    script=runner if name=='bash' else runner.with_suffix('.ps1')
                    result=subprocess.run([*launcher,str(script),'100','--force'],env=environment,capture_output=True,text=True)
                    self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                    self.assertFalse(sql_log.exists(),'must not import before lock acquisition')
                    self.assertEqual((app/'application.apx').read_bytes(),original)
                    self.assertEqual((state/'import-count.txt').read_text().strip(),'0')

    def test_lock_lost_after_import_keeps_old_baseline_and_recovery(self):
        launchers=[('bash',[BASH])] + ([('pwsh',[shutil.which('pwsh'),'-NoProfile','-File'])] if shutil.which('pwsh') else [])
        for name,launcher in launchers:
            with self.subTest(wrapper=name), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary)
                runner,_,_,environment,app,state=self.make_stateful_dev_fixture(root)
                environment['PROJECT_ENV_FILE']=str(root/'.env')
                environment['FAKE_APEX_LOCK_MODE']='drop-after-import'
                original=(app/'apex-team-export.json').read_bytes()
                script=runner if name=='bash' else runner.with_suffix('.ps1')
                result=subprocess.run([*launcher,str(script),'100'],env=environment,capture_output=True,text=True)
                self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn('application lock ownership changed',result.stdout+result.stderr)
                self.assertEqual((app/'apex-team-export.json').read_bytes(),original)
                records=list((root/'.sync-state/application-locks/100').glob('publish.*/recovery.json'))
                self.assertEqual(len(records),1)
                self.assertEqual(json.loads(records[0].read_text())['status'],'held')
                self.assertNotIn('release',Path(environment['FAKE_LOCK_CALLS']).read_text().splitlines())
                self.assertEqual((state/'import-count.txt').read_text().strip(),'1')

    def test_dev_publish_requires_workspace_username_even_with_force(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, _, _, environment = self.make_publish_fixture(root)
            env_path = root/'.env'
            env_path.write_text(re.sub(r'(?m)^APEX_WORKSPACE_USERNAME=.*\n', '', env_path.read_text()))
            environment['APEX_WORKSPACE_USERNAME'] = 'INHERITED'
            result = subprocess.run([BASH, str(runner), '100', '--force'], env=environment,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stdout+result.stderr)
            self.assertIn('set APEX_WORKSPACE_USERNAME', result.stderr)
            self.assertNotIn('Published APEX', result.stdout)
            self.assertFalse((root/'sql-args.txt').exists())

    def run_publish(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [BASH, str(PUBLISH), *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def make_publish_fixture(self, root: Path) -> tuple[Path, Path, Path, dict[str, str]]:
        scripts = root / "scripts"
        scripts.mkdir()
        for name in (
            "publish_app.sh",
            "publish_app.sql", "deployment_descriptor.py", "application_lock.py", "application_lock.sql", "no_application_lock.sql", "db_targets.py", "sqlcl_session.py", "sqlcl_session.sh", "windows_job.py",
            "apex_compatibility.py", "verify_apex_release.sql", "application_lifecycle.py", "application_lifecycle.sql",
            "application_lifecycle.py", "application_lifecycle.sql",
            "load_env.sh",
            "check_db_target.sh",
            "export_apps.sql", "verify_deployment_state.sql",
            "lookup_app_schema.sql",
            "verify_db_access.sql",
            "normalize_apx.sh",
            "record_export_state.py", "source_evidence.py",
            "verify_publish_state.py",
            "validate_app_source.py",
            "stamp_publish_version.py",
        ):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        helper = ROOT / "scripts" / "sqlcl_safe.sh"
        if helper.exists():
            shutil.copy2(helper, scripts / helper.name)
        shutil.copy2(ROOT / ".env.example", root / ".env")

        app = root / "apps" / "DEMO" / "100"
        deployments = app / "deployments"
        deployments.mkdir(parents=True)
        (app / "application.apx").write_text("app SAMPLE (\n    name: Sample\n)\n", encoding="utf-8")
        (app / ".apex").mkdir()
        (app / ".apex" / "apexlang.json").write_text('{"version":1}\n', encoding="utf-8")
        (deployments / "dev.json").write_text(
            json.dumps(
                {
                    "workspace": {"name": "DEV_WORKSPACE"},
                    "app": {"id": 100, "databaseSession": {"parsingSchema": "DEMO"}},
                }
            ),
            encoding="utf-8",
        )

        fake_bin = root / "bin"
        fake_bin.mkdir()
        sql_log = root / "sql-args.txt"
        sql_cwd = root / "sql-cwd.txt"
        fake_sql = fake_bin / "sql"
        fake_sql.write_text(
            "#!/usr/bin/env bash\n"
            "cat > /dev/null\n"
            "if [[ ${1:-} == -V ]]; then printf 'SQLcl: Release 26.3 Production\\n'; exit 0; fi\n"
            'if [[ -n "${FAKE_LOCK_HANDLER:-}" ]]; then python3 "$FAKE_LOCK_HANDLER" "$@"; lock_status=$?; [[ $lock_status == 3 ]] || exit "$lock_status"; fi\n'
            "mode=other\n"
            "for arg in \"$@\"; do\n"
            "  case \"$arg\" in\n"
            "    *@*check_builder_drift.sql) mode=drift ;;\n"
            "    *@*publish_app.sql) mode=import ;;\n"
            "    *@*export_apps.sql) mode=export ;;\n"
            "  esac\n"
            "done\n"
            "if [[ -f login.sql ]]; then touch \"$FAKE_LOGIN_MARKER\"; exit 0; fi\n"
            "if [[ -n \"${FAKE_SQL_CALLS:-}\" ]]; then printf '%s\\n' \"$mode\" >> \"$FAKE_SQL_CALLS\"; fi\n"
            "export_schema=DEMO\n"
            "if [[ $mode == export ]]; then found_script=0; for arg in \"$@\"; do if [[ $found_script == 1 ]]; then export_schema=$arg; break; fi; case \"$arg\" in *@*export_apps.sql) found_script=1 ;; esac; done; fi\n"
            "case \"$mode\" in\n"
            "  drift)\n"
            "    if [[ -n \"${FAKE_STATE_DIR:-}\" ]]; then\n"
            "      printf '%s|%s|%s\\n' \"$(cat \"$FAKE_STATE_DIR/live.txt\")\" \"$(cat \"$FAKE_STATE_DIR/database.txt\")\" \"$(cat \"$FAKE_STATE_DIR/version.txt\")\"\n"
            "    fi\n"
            "    printf 'APEX_DRIFT_QUERY_VERIFIED\\n'\n"
            "    ;;\n"
            "  import)\n"
            "    printf '%s\\n' \"$@\" > \"$FAKE_SQL_LOG\"\n"
            "    pwd > \"$FAKE_SQL_CWD\"\n"
            "    if [[ -n \"${FAKE_LOCK_SCRATCH:-}\" ]]; then mkdir -p locked && : > locked/held && chmod 500 locked; fi\n"
            "    if [[ -n \"${FAKE_STATE_DIR:-}\" ]]; then\n"
            "      count=$(cat \"$FAKE_STATE_DIR/import-count.txt\")\n"
            "      count=$((count + 1))\n"
            "      printf '%s\\n' \"$count\" > \"$FAKE_STATE_DIR/import-count.txt\"\n"
            "      if [[ $count -eq 1 ]]; then live=2026-09-26T09:30:00; db=2026-09-26T09:30:02; else live=2026-09-26T09:45:00; db=2026-09-26T09:45:02; fi\n"
            "      if [[ \"${FAKE_IMPORT_CLEARS_TIMESTAMP:-0}\" == 1 ]]; then live=NO_TIMESTAMP; fi\n"
            "      source_dir=\"$FAKE_SOURCE_DIR\"\n"
            "      version=$(sed -n 's/^    version: //p' \"$source_dir/application.apx\"); version=${version#\\\"}; version=${version%\\\"}\n"
            "      printf '%s\\n' \"$version\" > \"$FAKE_STATE_DIR/version.txt\"\n"
            "      printf '%s\\n' \"$live\" > \"$FAKE_STATE_DIR/live.txt\"\n"
            "      printf '%s\\n' \"$db\" > \"$FAKE_STATE_DIR/database.txt\"\n"
            "    fi\n"
            "    case \"${FAKE_SQL_MODE:-success}\" in\n"
            "      sp2) printf '%s\\n' 'SP2-0640: Not connected' ;;\n"
            "      edit-then-fail) printf '%s\\n' '// saved during publish' >> \"$FAKE_SOURCE_DIR/application.apx\"; printf '%s\\n' 'SP2-0640: Not connected' ;;\n"
            "      no-sentinel) printf '%s\\n' 'Import successful.' ;;\n"
            "      skipped) printf '%s\\n' 'Workspace: NO_SUCH_WORKSPACE from deployment file: deployments/dev.json is invalid' 'APEX_IMPORT_VERIFIED:100' ;;\n"
            "      interrupted) exit 130 ;;\n"
            "      terminated) exit 143 ;;\n"
            "      *) printf '%s\\n' 'Import successful.' 'APEX_IMPORT_VERIFIED:100' ;;\n"
            "    esac\n"
            "    if [[ -n \"${FAKE_IMPORT_PAUSE:-}\" ]]; then : > \"$FAKE_IMPORT_PAUSE\"; sleep 120; fi\n"
            "    ;;\n"
            "  export)\n"
            "    if [[ -n \"${FAKE_EXPORT_STATUS:-}\" ]]; then exit \"$FAKE_EXPORT_STATUS\"; fi\n"
            "    exported=\"$PWD/apps/$export_schema/100\"\n"
            "    mkdir -p \"$exported/.apex\"\n"
            "    cp \"$FAKE_SOURCE_DIR/application.apx\" \"$exported/application.apx\"\n"
            "    cp \"$FAKE_SOURCE_DIR/.apex/apexlang.json\" \"$exported/.apex/apexlang.json\"\n"
            "    if [[ -f \"$FAKE_SOURCE_DIR/login.sql\" ]]; then cp \"$FAKE_SOURCE_DIR/login.sql\" \"$exported/login.sql\"; fi\n"
            "    if [[ \"${FAKE_EXPORT_MISMATCH:-0}\" == 1 ]]; then printf '%s\\n' '// race' >> \"$exported/application.apx\"; fi\n"
            "    if [[ -n \"${FAKE_STATE_DIR:-}\" ]]; then live=$(cat \"$FAKE_STATE_DIR/live.txt\"); db=$(cat \"$FAKE_STATE_DIR/database.txt\"); version=$(cat \"$FAKE_STATE_DIR/version.txt\"); else live=${FAKE_REVISION:-2026-09-26T09:30:00}; db=${FAKE_DATABASE_TIME:-2026-09-26T09:30:02}; version=${FAKE_VERSION:-Release 1.0}; fi\n"
            "    printf '%s|%s|%s\\n' \"$live\" \"$db\" \"$version\" > \"$PWD/.apex-export-before.txt\"\n"
            "    cp \"$PWD/.apex-export-before.txt\" \"$PWD/.apex-export-after.txt\"\n"
            "    ;;\n"
            "  *) : ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        fake_sql.chmod(0o755)
        fake_sqlcl.add_launcher(fake_bin)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        environment["FAKE_SQL_LOG"] = str(sql_log)
        environment["FAKE_SQL_CWD"] = str(sql_cwd)
        environment["FAKE_LOGIN_MARKER"] = str(root / "login-marker")
        environment["FAKE_SOURCE_DIR"] = str(app)
        environment["FAKE_LOCK_HANDLER"] = str(ROOT/"tests/fake_application_lock_sql.py")
        environment["FAKE_LOCK_CALLS"] = str(root/"lock-calls.txt")
        return scripts / "publish_app.sh", sql_log, sql_cwd, environment

    def make_stateful_dev_fixture(self, root: Path):
        runner, sql_log, sql_cwd, environment = self.make_publish_fixture(root)
        scripts = runner.parent
        for name in (
            "check_builder_drift.py",
            "check_builder_drift.sql",
            "export_apps.sql", "verify_deployment_state.sql",
            "verify_db_access.sql",
            "normalize_apx.sh",
            "normalize_apx.ps1",
            "record_export_state.py", "source_evidence.py",
            "preserve_deployments.py",
            "verify_publish_state.py",
            "publish_app.ps1",
            "load_env.ps1",
            "invoke_sqlcl.ps1", "resolve_python.ps1",
            "check_db_target.ps1",
        ):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        app = root / "apps" / "DEMO" / "100"
        (app / "apex-team-export.json").write_text(
            json.dumps(
                {
                    "applicationId": 100,
                    "applicationPresent": True,
                    "builderLastUpdatedOn": "2026-09-26T08:00:00",
                    "version": "Release 1.0",
                }
            ) + "\n",
            encoding="utf-8",
        )
        state_dir = root / "fake-db"
        state_dir.mkdir()
        (state_dir / "live.txt").write_text("2026-09-26T08:00:00\n", encoding="utf-8")
        (state_dir / "database.txt").write_text("2026-09-26T09:00:00\n", encoding="utf-8")
        (state_dir / "import-count.txt").write_text("0\n", encoding="utf-8")
        (state_dir / "version.txt").write_text("Release 1.0\n", encoding="utf-8")
        environment["FAKE_STATE_DIR"] = str(state_dir)
        environment["FAKE_SOURCE_DIR"] = str(app)
        return runner, sql_log, sql_cwd, environment, app, state_dir

    def test_help_describes_numeric_id_and_environment_options(self) -> None:
        result = self.run_publish("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<app_id>", result.stdout)
        self.assertIn("--env", result.stdout)
        self.assertIn("--force", result.stdout)

    def test_rejects_non_numeric_application_id_before_loading_configuration(self) -> None:
        result = self.run_publish("my-app")
        self.assertEqual(result.returncode, 2)
        self.assertIn("positive numeric application id", result.stderr)

    def test_deployment_templates_are_explicit_and_use_numeric_app_ids(self) -> None:
        expected = {
            "dev": ("DEV_WORKSPACE", "DEV_APP"),
            "staging": ("STAGE_WORKSPACE", "STAGE_APP"),
            "prod": ("PROD_WORKSPACE", "PROD_APP"),
        }
        for environment, (workspace, schema) in expected.items():
            with self.subTest(environment=environment):
                path = ROOT / "apps" / "templates" / "deployments" / f"{environment}.json"
                self.assertTrue(path.is_file(), f"missing deployment template: {path}")
                deployment = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(deployment["workspace"]["name"], workspace)
                self.assertEqual(deployment["app"]["id"], 100)
                self.assertEqual(
                    deployment["app"]["databaseSession"]["parsingSchema"], schema
                )

    def test_sqlcl_import_uses_the_explicit_deployment_descriptor(self) -> None:
        import_script = ROOT / "scripts" / "publish_app.sql"
        self.assertTrue(import_script.is_file(), f"missing import script: {import_script}")
        self.assertIn(
            'apex import -input "&&application_source" -deployment "&&deployment_file"',
            import_script.read_text(encoding="utf-8"),
        )
        self.assertIn("APEX_IMPORT_VERIFIED:&&expected_app_id", import_script.read_text(encoding="utf-8"))

    def test_force_publish_passes_numeric_id_and_environment_descriptor_to_sqlcl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, sql_log, sql_cwd, environment = self.make_publish_fixture(root)
            result = subprocess.run(
                [BASH, str(runner), "100", "--force"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            args = sql_log.read_text(encoding="utf-8").splitlines()
            self.assertIn("-name", args)
            self.assertIn("docker-demo", args)
            self.assertIn("@" + fake_sqlcl.native(runner.parent / "publish_app.sql"), args)
            self.assertIn(fake_sqlcl.native(root / "apps/DEMO/100"), args)
            self.assertIn(fake_sqlcl.native(root / "apps/DEMO/100" / "deployments/dev.json"), args)
            self.assertNotEqual(sql_cwd.read_text(encoding="utf-8").strip(), str(root / "apps/DEMO/100"))

    def test_publish_does_not_start_sqlcl_in_application_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, _, _, environment = self.make_publish_fixture(root)
            (root / "apps/DEMO/100/login.sql").write_text("HOST touch should-not-run\n", encoding="utf-8")

            result = subprocess.run(
                [BASH, str(runner), "100", "--force"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("login.sql", result.stdout + result.stderr)
            self.assertFalse((root / "login-marker").exists())

    def test_symlinked_app_root_is_rejected_before_sqlcl_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, sql_log, _, environment = self.make_publish_fixture(root)
            app = root / "apps" / "DEMO" / "100"
            outside = root / "external-app"
            app.rename(outside)
            try:
                app.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            result = subprocess.run(
                [BASH, str(runner), "100", "--force"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symbolic links or reparse points", result.stderr)
            self.assertFalse(sql_log.exists(), "SQLcl must not run for a symlinked application root")

    def test_powershell_symlinked_app_root_is_rejected_before_sqlcl_import(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, sql_log, _, environment, app, _ = self.make_stateful_dev_fixture(root)
            outside = root / "external-app"
            app.rename(outside)
            try:
                app.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            result = subprocess.run(
                [pwsh, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100", "--force"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("symbolic links or reparse points", result.stdout + result.stderr)
            self.assertFalse(sql_log.exists(), "SQLcl must not run for a symlinked application root")

    def test_client_error_or_missing_verification_never_reports_published(self) -> None:
        for mode in ("sp2", "no-sentinel"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runner, _, _, environment = self.make_publish_fixture(root)
                environment["FAKE_SQL_MODE"] = mode
                result = subprocess.run(
                    [BASH, str(runner), "100", "--force"],
                    cwd=root,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("Published APEX App", result.stdout + result.stderr)

    def test_verified_dev_publish_advances_baseline_and_second_publish_passes_drift_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, _, _, environment, app, _ = self.make_stateful_dev_fixture(root)

            first = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertIn("APEX_PUBLISH_SOURCE_VERIFIED:100", first.stdout)
            marker = json.loads((app / "apex-team-export.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["builderLastUpdatedOn"], "2026-09-26T09:30:00")

            second = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("No uncaptured Builder edits", second.stdout)
            marker = json.loads((app / "apex-team-export.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["builderLastUpdatedOn"], "2026-09-26T09:45:00")

    # Live APEX leaves last_updated_on NULL after an APEXlang import. Publish
    # must treat that as an installed app and let the next publish through.
    def test_import_that_clears_builder_timestamp_publishes_and_records_present_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, _, _, environment, app, _ = self.make_stateful_dev_fixture(root)
            environment["FAKE_IMPORT_CLEARS_TIMESTAMP"] = "1"

            first = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertIn("APEX_PUBLISH_SOURCE_VERIFIED:100", first.stdout)
            marker = json.loads((app / "apex-team-export.json").read_text(encoding="utf-8"))
            self.assertIs(marker["applicationPresent"], True)
            self.assertIsNone(marker["builderLastUpdatedOn"])
            self.assertRegex(marker["version"], STAMPED_VERSION.format(counter="001"))

            second = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("no Builder edits since its last import", second.stdout)

    def test_dev_publish_stamps_version_before_import_for_the_developer_to_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, _, _, environment, app, state_dir = self.make_stateful_dev_fixture(root)
            environment["FAKE_IMPORT_CLEARS_TIMESTAMP"] = "1"

            result = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            source = (app / "application.apx").read_text(encoding="utf-8")
            version = (state_dir / "version.txt").read_text(encoding="utf-8").strip()
            self.assertRegex(version, STAMPED_VERSION.format(counter="001"))
            self.assertIn(f'    version: "{version}"\n', source)
            self.assertIn("Commit the stamped version in apps/DEMO/100/application.apx", result.stdout)

    def test_teammate_import_over_import_is_refused_by_the_version_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, _, _, environment, _, state_dir = self.make_stateful_dev_fixture(root)
            environment["FAKE_IMPORT_CLEARS_TIMESTAMP"] = "1"
            first = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            # A teammate publishes from their own checkout: still no timestamp.
            (state_dir / "version.txt").write_text("Release 1.0 [BOB-2026-09-26r001]\n", encoding="utf-8")

            second = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertNotEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("[DRIFT DETECTED]", second.stdout)
            self.assertIn("[BOB-2026-09-26r001]", second.stdout)
            self.assertEqual((state_dir / "import-count.txt").read_text(encoding="utf-8").strip(), "1")

    def test_descriptor_with_a_byte_order_mark_is_refused_early_in_both_shells(self) -> None:
        # SQLcl cannot parse a deployment file that starts with a UTF-8 BOM (it
        # prints "Deployment file cannot be parsed", exits 0 and imports nothing),
        # and Windows PowerShell's -Encoding UTF8 writes one. Both shells say so
        # before connecting.
        pwsh = shutil.which("pwsh")
        shells = ["bash"] + (["pwsh"] if pwsh else [])
        for shell in shells:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runner, _, _, environment, app, _ = self.make_stateful_dev_fixture(root)
                environment["PROJECT_ENV_FILE"] = str(root / ".env")
                descriptor = app / "deployments" / "dev.json"
                descriptor.write_bytes(b"\xef\xbb\xbf" + descriptor.read_bytes())
                if shell == "bash":
                    command = [BASH, str(runner), "100", "--describe"]
                else:
                    command = [pwsh, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100", "--describe"]

                result = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)

                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                output = " ".join(re.sub(r"\x1b\[[0-9;]*m|\|", " ", result.stdout + result.stderr).split())
                self.assertIn("byte-order mark", output)

    def test_a_descriptor_for_another_application_names_both_ids_in_both_shells(self) -> None:
        # The template's own descriptor example has "id": 100; copied unchanged for another
        # application it is a number, just the wrong one. Say so, not "must be numeric".
        pwsh = shutil.which("pwsh")
        shells = ["bash"] + (["pwsh"] if pwsh else [])
        cases = (
            ('"id": 7', "is 7 but this is application 100"),
            ('"id": "seven"', "must be a number"),
        )
        for shell in shells:
            for replacement, expected in cases:
                with self.subTest(shell=shell, descriptor=replacement), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    runner, _, _, environment, app, _ = self.make_stateful_dev_fixture(root)
                    environment["PROJECT_ENV_FILE"] = str(root / ".env")
                    descriptor = app / "deployments" / "dev.json"
                    text = descriptor.read_text(encoding="utf-8")
                    changed = re.sub(r'"id":\s*100', replacement, text)
                    self.assertNotEqual(changed, text)
                    descriptor.write_text(changed, encoding="utf-8")
                    if shell == "bash":
                        command = [BASH, str(runner), "100", "--describe"]
                    else:
                        command = [pwsh, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100", "--describe"]

                    result = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)

                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    output = " ".join(re.sub(r"\x1b\[[0-9;]*m|\|", " ", result.stdout + result.stderr).split())
                    self.assertIn(expected, output)
                    self.assertIn('set "id": 100 in the descriptor', output)
                    self.assertNotIn("must be numeric", output)

    @unittest.skipIf(os.name == "nt", "needs POSIX process groups and signals (preexec_fn, os.killpg); Windows twin: test_ctrl_c_in_a_windows_console_while_the_import_runs_says_its_result_is_unknown")
    def test_interrupt_while_the_import_runs_says_its_result_is_unknown(self) -> None:
        # SQLcl may already have finished the import when the interrupt arrives,
        # before publish has read its output; the old source is put back, so
        # the developer must be told that DEV may have changed.
        pwsh = shutil.which("pwsh")
        # pwsh ends at once on SIGTERM and runs no finally block, so only Ctrl-C is
        # handled in PowerShell (docs/publish-rules.md says so).
        shells = [("bash", signal.SIGTERM, "team.sh")] + ([("pwsh", signal.SIGINT, "team.ps1")] if pwsh else [])
        for shell, sig, wrapper in shells:
            with self.subTest(shell=shell, signal=sig.name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runner, _, _, environment, app, state_dir = self.make_stateful_dev_fixture(root)
                environment["PROJECT_ENV_FILE"] = str(root / ".env")
                pause = root / "import-paused"
                environment["FAKE_IMPORT_PAUSE"] = str(pause)
                original = (app / "application.apx").read_bytes()
                if shell == "bash":
                    command = [BASH, str(runner), "100"]
                else:
                    # Through the wrapper: it is what turns Ctrl-C into status 130.
                    shutil.copy2(ROOT / "scripts" / "team.ps1", root / "scripts" / "team.ps1")
                    command = [pwsh, "-NoProfile", "-File", str(root / "scripts/team.ps1"), "publish", "100"]
                process = subprocess.Popen(
                    command, cwd=root, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    start_new_session=True, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
                )
                deadline = time.monotonic() + 90
                while not pause.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(pause.exists(), "the fake import never ran")
                os.killpg(process.pid, sig)
                output, _ = process.communicate(timeout=60)

                if shell == "pwsh":
                    # pwsh used to exit 0 here, which a caller reads as success.
                    self.assertEqual(process.returncode, 130, output)
                    self.assertTrue(list((root / "scratch").glob("apex-publish*")), "ambiguous import diagnostics must remain")
                plain = " ".join(re.sub(r"\x1b\[[0-9;]*m|\|", " ", output).split())
                self.assertIn("interrupted while the import was running", plain)
                self.assertIn("its result is unknown", plain)
                self.assertIn(f"scripts/{wrapper} export 100", plain)
                self.assertEqual((state_dir / "import-count.txt").read_text(encoding="utf-8").strip(), "1")
                self.assertEqual((app / "application.apx").read_bytes(), original)

    @unittest.skipIf(os.name == "nt", "a read-only directory stands in for a file an editor holds open; Windows locks are not simulated")
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_publish_names_the_scratch_directory_it_could_not_remove(self) -> None:
        # Where a program holds a file of the publish directory open (on Windows), or the
        # directory cannot be emptied, the cleanup failed silently in PowerShell and with a
        # bare rm error in Bash, and the developer was never told to remove it.
        pwsh = shutil.which("pwsh")
        shells = ["bash"] + (["pwsh"] if pwsh else [])
        for shell in shells:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runner, _, _, environment, _, _ = self.make_stateful_dev_fixture(root)
                environment["PROJECT_ENV_FILE"] = str(root / ".env")
                environment["FAKE_LOCK_SCRATCH"] = "1"
                if shell == "bash":
                    command = [BASH, str(runner), "100"]
                else:
                    command = [pwsh, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100"]
                try:
                    result = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)
                    leftovers = sorted((root / "scratch").glob("apex-publish*"))
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(len(leftovers), 1, result.stdout + result.stderr)
                    plain = " ".join(re.sub(r"\x1b\[[0-9;]*m|\|", " ", result.stdout + result.stderr).split())
                    self.assertIn("could not remove the temporary directory", plain)
                    self.assertIn(leftovers[0].name, plain)
                    self.assertNotIn("Permission denied", plain)
                finally:
                    for locked in (root / "scratch").glob("apex-publish*/locked"):
                        locked.chmod(0o700)

    @unittest.skipUnless(os.name == "nt", "presses Ctrl-C in a Windows console; the POSIX twin is above")
    def test_ctrl_c_in_a_windows_console_while_the_import_runs_says_its_result_is_unknown(self) -> None:
        # Through team.ps1 in each PowerShell, as in the test above. The fake SQLcl is a Bash
        # script that Ctrl-C cannot end, so publish waits for it for ten seconds and then ends
        # it; real SQLcl is one native program that the keypress ends at once.
        engines = [
            [path, *flags]
            for path, flags in (
                (shutil.which("powershell"), ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]),
                (shutil.which("pwsh"), ["-NoProfile", "-File"]),
            )
            if path
        ]
        for engine in engines:
            with self.subTest(shell=Path(engine[0]).name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _, _, _, environment, app, state_dir = self.make_stateful_dev_fixture(root)
                environment["PROJECT_ENV_FILE"] = str(root / ".env")
                pause = root / "import-paused"
                environment["FAKE_IMPORT_PAUSE"] = str(pause)
                original = (app / "application.apx").read_bytes()
                shutil.copy2(ROOT / "scripts" / "team.ps1", root / "scripts" / "team.ps1")
                command = [*engine, str(root / "scripts" / "team.ps1"), "publish", "100"]

                process, screen, _ = _console_ctrl_c.interrupt(command, root, environment, pause, wait_seconds=60)

                self.assertEqual(process.returncode, 130, screen)
                self.assertEqual(sorted(path.name for path in (root / "scratch").glob("apex-publish*")), [])
                plain = " ".join(re.sub(r"\x1b\[[0-9;]*m|\|", " ", screen).split())
                self.assertIn("interrupted while the import was running", plain)
                self.assertIn("its result is unknown", plain)
                self.assertIn("scripts/team.ps1 export 100", plain)
                self.assertEqual((state_dir / "import-count.txt").read_text(encoding="utf-8").strip(), "1")
                self.assertEqual((app / "application.apx").read_bytes(), original)

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell only meets PowerShell 7's module path on Windows")
    def test_windows_powershell_publishes_with_powershell_7s_module_path_inherited(self) -> None:
        # Whatever runs under PowerShell 7 (an agent's shell, an IDE task, a test runner) hands its
        # environment to the programs it starts. A Windows PowerShell started that way loads
        # PowerShell 7's Utility and Management modules and loses Get-FileHash, Get-Acl and
        # Set-Acl, which the publish swap of application.apx used to need.
        powershell, pwsh = shutil.which("powershell"), shutil.which("pwsh")
        if not (powershell and pwsh):
            self.skipTest("needs Windows PowerShell and PowerShell 7")
        module_path = subprocess.run(
            [pwsh, "-NoProfile", "-Command", "$env:PSModulePath"], text=True, capture_output=True, check=True
        ).stdout.strip()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, environment, app, state_dir = self.make_stateful_dev_fixture(root)
            environment["PROJECT_ENV_FILE"] = str(root / ".env")
            environment["FAKE_IMPORT_CLEARS_TIMESTAMP"] = "1"
            for name in [name for name in environment if name.upper() == "PSMODULEPATH"]:
                del environment[name]
            environment["PSModulePath"] = module_path
            lookup = subprocess.run(
                [powershell, "-NoProfile", "-Command", "if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) { 'present' }"],
                env=environment, text=True, capture_output=True, check=False,
            )
            if "present" in lookup.stdout:
                self.skipTest("PowerShell 7's module path does not hide Get-FileHash from Windows PowerShell here")

            result = subprocess.run(
                [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(root / "scripts" / "publish_app.ps1"), "100"],
                cwd=root, env=environment, text=True, capture_output=True, check=False,
            )

            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("Published APEX App 100", output)
            version = (state_dir / "version.txt").read_text(encoding="utf-8").strip()
            self.assertRegex(version, STAMPED_VERSION.format(counter="001"))
            self.assertIn(f'    version: "{version}"\n', (app / "application.apx").read_text(encoding="utf-8"))

    def test_drift_refusal_names_the_export_command_of_the_shell_in_use(self) -> None:
        # A PowerShell user must not be told to run the Bash wrapper.
        pwsh = shutil.which("pwsh")
        shells = [("bash", "team.sh")] + ([("pwsh", "team.ps1")] if pwsh else [])
        for shell, wrapper in shells:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runner, _, _, environment, _, state_dir = self.make_stateful_dev_fixture(root)
                environment["PROJECT_ENV_FILE"] = str(root / ".env")
                environment["FAKE_IMPORT_CLEARS_TIMESTAMP"] = "1"
                if shell == "bash":
                    command = [BASH, str(runner), "100"]
                else:
                    command = [pwsh, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100"]
                first = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                (state_dir / "version.txt").write_text("Release 1.0 [BOB-2026-09-26r001]\n", encoding="utf-8")

                second = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)

                self.assertNotEqual(second.returncode, 0, second.stdout + second.stderr)
                output = second.stdout + second.stderr
                self.assertIn("[DRIFT DETECTED]", output)
                self.assertIn(f"scripts/{wrapper} export 100", output)
                other = "team.ps1" if wrapper == "team.sh" else "team.sh"
                self.assertNotIn(f"scripts/{other} export", output)

    def test_failed_import_restores_the_unstamped_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, _, _, environment, app, _ = self.make_stateful_dev_fixture(root)
            environment["FAKE_SQL_MODE"] = "sp2"
            original = (app / "application.apx").read_bytes()

            result = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Stamped application version", result.stdout)
            self.assertEqual((app / "application.apx").read_bytes(), original)

    def test_failed_import_keeps_an_edit_saved_during_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, _, _, environment, app, _ = self.make_stateful_dev_fixture(root)
            environment["FAKE_SQL_MODE"] = "edit-then-fail"

            result = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("changed while publishing; left as is", result.stderr)
            self.assertIn("// saved during publish", (app / "application.apx").read_text(encoding="utf-8"))

    def test_application_source_that_cannot_be_moved_is_reported_as_such(self) -> None:
        # On Windows an editor holding the file open blocks the rename; a
        # read-only folder blocks it the same way here.
        pwsh = shutil.which("pwsh")
        shells = [("bash", None)] + ([("pwsh", pwsh)] if pwsh else [])
        for shell, executable in shells:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runner, _, _, environment, app, state_dir = self.make_stateful_dev_fixture(root)
                environment["PROJECT_ENV_FILE"] = str(root / ".env")
                original = (app / "application.apx").read_bytes()
                if shell == "bash":
                    command = [BASH, str(runner), "100"]
                else:
                    command = [executable, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100"]
                if os.name == "nt":
                    # The real Windows cause: a program holds the file open. Python's open() does not
                    # share DELETE, so renaming the file fails while the handle is held.
                    with open(app / "application.apx", "rb"):
                        result = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)
                else:
                    app.chmod(0o555)
                    try:
                        result = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)
                    finally:
                        app.chmod(0o755)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("could not move application.apx", result.stdout + result.stderr)
                # Both shells give the advice for the usual Windows cause (an editor holds the file open).
                self.assertIn("close any program holding it open", result.stdout + result.stderr)
                self.assertNotIn("changed while the publish tag was stamped", result.stdout + result.stderr)
                self.assertEqual((app / "application.apx").read_bytes(), original)
                self.assertEqual((state_dir / "import-count.txt").read_text(encoding="utf-8").strip(), "0")

    def test_unverified_import_says_to_commit_the_stamp_before_exporting(self) -> None:
        # Export refuses to write over uncommitted changes, so the kept stamp
        # must be committed before the documented recovery export.
        pwsh = shutil.which("pwsh")
        shells = [("bash", "team.sh")] + ([("pwsh", "team.ps1")] if pwsh else [])
        for shell, wrapper in shells:
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runner, _, _, environment, app, _ = self.make_stateful_dev_fixture(root)
                environment["PROJECT_ENV_FILE"] = str(root / ".env")
                if shell == "bash":
                    command = [BASH, str(runner), "100"]
                else:
                    command = [pwsh, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100"]
                ok = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)
                self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
                self.assertNotIn("was not verified", ok.stdout + ok.stderr)
                subprocess.run(["git", "-C", str(root), "add", "-A"], check=False, capture_output=True)

                environment["FAKE_EXPORT_MISMATCH"] = "1"
                failed = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)

                output = " ".join((failed.stdout + failed.stderr).split())
                self.assertNotEqual(failed.returncode, 0)
                self.assertIn("DEV now runs the imported source, but it was not verified", output)
                self.assertIn("Commit the stamped", output)
                self.assertIn(f"scripts/{wrapper} export 100", output)
                self.assertRegex((app / "application.apx").read_text(encoding="utf-8"), r"r002")

    def test_import_session_receives_the_approved_live_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, sql_log, _, environment, _, _ = self.make_stateful_dev_fixture(root)

            result = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            arguments = sql_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(arguments[-2], "P.2026-09-26T08:00:00." + b"Release 1.0".hex().upper())

            forced = subprocess.run(
                [BASH, str(runner), "100", "--force"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(forced.returncode, 0, forced.stdout + forced.stderr)
            self.assertEqual(sql_log.read_text(encoding="utf-8").splitlines()[-2], "-")

    def make_unexported_app(self, root: Path, *, app_exists: bool):
        """A stateful fixture whose app was never exported, in a target that has it or not."""
        runner, sql_log, _, environment, app, state_dir = self.make_stateful_dev_fixture(root)
        (app / "apex-team-export.json").unlink()
        if not app_exists:
            (state_dir / "live.txt").write_text("NOT_FOUND\n", encoding="utf-8")
            (state_dir / "version.txt").write_text("\n", encoding="utf-8")
        environment["FAKE_SQL_CALLS"] = str(root / "sql-calls.txt")
        return runner, sql_log, environment, app, state_dir

    def test_first_publish_of_an_absent_app_refuses_before_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, sql_log, environment, app, _ = self.make_unexported_app(root, app_exists=False)

            result = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn('application target not found', result.stdout+result.stderr)
            self.assertFalse(sql_log.exists())
            self.assertFalse((app/'apex-team-export.json').exists())

    def test_publish_of_an_unexported_app_that_exists_in_the_target_is_refused_before_any_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, _, environment, _, state_dir = self.make_unexported_app(root, app_exists=True)

            result = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Database export baseline is unavailable", result.stderr)
            self.assertNotIn("import", (root / "sql-calls.txt").read_text(encoding="utf-8").split())
            self.assertEqual((state_dir / "import-count.txt").read_text(encoding="utf-8").strip(), "0")

    # SQLcl prints "...is invalid" and exits 0 when the descriptor workspace
    # does not exist, without importing (verified on docker-demo).
    def test_import_skipped_by_sqlcl_is_a_failure_and_restores_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, _, _, environment, app, _ = self.make_stateful_dev_fixture(root)
            environment["FAKE_SQL_MODE"] = "skipped"
            original = (app / "application.apx").read_bytes()

            result = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("did not report a successful APEX import", result.stderr)
            self.assertNotIn("Published APEX App", result.stdout)
            self.assertEqual((app / "application.apx").read_bytes(), original)

    # SQLcl ended by Ctrl-C exits 130 (on Windows Git Bash does not run the script's own
    # trap then, so the status is all publish sees). The import's result is unknown.
    def test_an_import_that_a_signal_ends_is_reported_as_interrupted(self) -> None:
        # 143: a SIGTERM that reached only the subshell running SQLcl (its command line
        # is publish_app.sh's, so `pkill -f publish_app.sh` hits it); SQLcl may go on.
        for mode, status in (("interrupted", 130), ("terminated", 143)):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runner, _, _, environment, app, state_dir = self.make_stateful_dev_fixture(root)
                environment["FAKE_SQL_MODE"] = mode
                original = (app / "application.apx").read_bytes()

                result = subprocess.run([BASH, str(runner), "100"], cwd=root, env=environment, text=True, capture_output=True, check=False)

                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                plain = " ".join(result.stderr.split())
                self.assertIn("interrupted while the import was running, so its result is unknown", plain)
                self.assertNotIn("SQLcl application import failed", plain)
                self.assertEqual((state_dir / "import-count.txt").read_text(encoding="utf-8").strip(), "1")
                self.assertEqual((app / "application.apx").read_bytes(), original)

    def test_a_post_import_export_that_a_signal_ends_says_the_import_was_not_verified(self) -> None:
        for status in (130, 143):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runner, _, _, environment, app, _ = self.make_stateful_dev_fixture(root)
                environment["FAKE_EXPORT_STATUS"] = str(status)
                original = (app / "application.apx").read_bytes()
                marker = app / "apex-team-export.json"
                marker_before = marker.read_bytes() if marker.exists() else None

                result = subprocess.run([BASH, str(runner), "100"], cwd=root, env=environment, text=True, capture_output=True, check=False)

                self.assertEqual(result.returncode, status, result.stdout + result.stderr)
                plain = " ".join(result.stderr.split())
                self.assertIn("DEV now runs the imported source, but it was not verified", plain)
                self.assertNotIn("post-import APEX export failed", plain)
                # The stamped source is what DEV runs, so it stays for the developer to commit.
                self.assertNotEqual((app / "application.apx").read_bytes(), original)
                self.assertEqual(marker.read_bytes() if marker.exists() else None, marker_before)

    def test_powershell_import_skipped_by_sqlcl_is_a_failure_and_restores_the_source(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, environment, app, _ = self.make_stateful_dev_fixture(root)
            environment["PROJECT_ENV_FILE"] = str(root / ".env")
            environment["FAKE_SQL_MODE"] = "skipped"
            original = (app / "application.apx").read_bytes()
            command = [pwsh, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100"]

            result = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("did not report a successful APEX import", result.stdout + result.stderr)
            self.assertNotIn("Published APEX App", result.stdout)
            self.assertEqual((app / "application.apx").read_bytes(), original)

    def test_powershell_failed_import_keeps_an_edit_saved_during_publish(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, environment, app, _ = self.make_stateful_dev_fixture(root)
            environment["PROJECT_ENV_FILE"] = str(root / ".env")
            environment["FAKE_SQL_MODE"] = "edit-then-fail"
            command = [pwsh, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100"]

            result = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("changed while publishing; left as is", result.stdout + result.stderr)
            self.assertIn("// saved during publish", (app / "application.apx").read_text(encoding="utf-8"))

    def test_powershell_import_session_receives_the_approved_live_state(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, sql_log, _, environment, _, _ = self.make_stateful_dev_fixture(root)
            environment["PROJECT_ENV_FILE"] = str(root / ".env")
            command = [pwsh, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100"]

            result = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                sql_log.read_text(encoding="utf-8").splitlines()[-2],
                "P.2026-09-26T08:00:00." + b"Release 1.0".hex().upper(),
            )

    def test_powershell_first_publish_of_an_absent_app_refuses_before_import(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, sql_log, environment, app, _ = self.make_unexported_app(root, app_exists=False)
            environment["PROJECT_ENV_FILE"] = str(root / ".env")
            command = [pwsh, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100"]

            result = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)

            self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
            self.assertIn('application target not found', result.stdout+result.stderr)
            self.assertFalse(sql_log.exists())
            self.assertFalse((app/'apex-team-export.json').exists())

    def test_powershell_import_that_clears_builder_timestamp_publishes(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, environment, app, _ = self.make_stateful_dev_fixture(root)
            environment["PROJECT_ENV_FILE"] = str(root / ".env")
            environment["FAKE_IMPORT_CLEARS_TIMESTAMP"] = "1"
            command = [pwsh, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100"]

            first = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            marker = json.loads((app / "apex-team-export.json").read_text(encoding="utf-8"))
            self.assertIs(marker["applicationPresent"], True)
            self.assertIsNone(marker["builderLastUpdatedOn"])

            source = (app / "application.apx").read_text(encoding="utf-8")
            self.assertRegex(marker["version"], STAMPED_VERSION.format(counter="001"))
            self.assertIn(f'    version: "{marker["version"]}"\n', source)

            second = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("no Builder edits since its last import", second.stdout)
            marker = json.loads((app / "apex-team-export.json").read_text(encoding="utf-8"))
            self.assertRegex(marker["version"], STAMPED_VERSION.format(counter="002"))

    def test_powershell_failed_import_restores_the_unstamped_source(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, environment, app, _ = self.make_stateful_dev_fixture(root)
            environment["PROJECT_ENV_FILE"] = str(root / ".env")
            environment["FAKE_SQL_MODE"] = "sp2"
            original = (app / "application.apx").read_bytes()
            command = [pwsh, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100"]

            result = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Stamped application version", result.stdout)
            self.assertEqual((app / "application.apx").read_bytes(), original)

    def test_production_like_dev_connection_is_refused_before_any_sqlcl_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, _, _, environment, _, _ = self.make_stateful_dev_fixture(root)
            env_file = root / ".env"
            env_file.write_text(
                env_file.read_text(encoding="utf-8").replace(
                    "APEX_SQLCL_CONNECTION=docker-demo", "APEX_SQLCL_CONNECTION=prod-db"
                ),
                encoding="utf-8",
            )
            calls = root / "sql-calls.txt"
            environment["FAKE_SQL_CALLS"] = str(calls)

            result = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("resembles production", result.stderr)
            self.assertFalse(calls.exists(), calls.read_text(encoding="utf-8") if calls.exists() else "")

    def test_powershell_production_like_dev_connection_is_refused_before_any_sqlcl_session(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, environment, _, _ = self.make_stateful_dev_fixture(root)
            env_file = root / ".env"
            env_file.write_text(
                env_file.read_text(encoding="utf-8").replace(
                    "APEX_SQLCL_CONNECTION=docker-demo", "APEX_SQLCL_CONNECTION=prod-db"
                ),
                encoding="utf-8",
            )
            environment["PROJECT_ENV_FILE"] = str(env_file)
            calls = root / "sql-calls.txt"
            environment["FAKE_SQL_CALLS"] = str(calls)
            command = [pwsh, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100"]

            result = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("resembles production", result.stdout + result.stderr)
            self.assertFalse(calls.exists(), calls.read_text(encoding="utf-8") if calls.exists() else "")

    def test_unverified_publish_keeps_old_baseline_and_next_attempt_refuses_retained_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, _, _, environment, app, state_dir = self.make_stateful_dev_fixture(root)
            environment["FAKE_EXPORT_MISMATCH"] = "1"
            marker_path = app / "apex-team-export.json"
            original_marker = marker_path.read_bytes()

            first = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertNotIn("Published APEX App", first.stdout + first.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

            environment.pop("FAKE_EXPORT_MISMATCH")
            retry = subprocess.run(
                [BASH, str(runner), "100"], cwd=root, env=environment,
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(retry.returncode, 0, retry.stdout + retry.stderr)
            self.assertIn("application already locked", retry.stdout + retry.stderr)
            self.assertEqual((state_dir / "import-count.txt").read_text(encoding="utf-8").strip(), "1")

    def test_powershell_verified_dev_publish_advances_baseline_for_the_next_publish(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, environment, app, _ = self.make_stateful_dev_fixture(root)
            environment["PROJECT_ENV_FILE"] = str(root / ".env")
            command = [pwsh, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100"]

            first = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertIn("APEX_PUBLISH_SOURCE_VERIFIED:100", first.stdout)
            marker = json.loads((app / "apex-team-export.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["builderLastUpdatedOn"], "2026-09-26T09:30:00")

            second = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertIn("No uncaptured Builder edits", second.stdout)
            marker = json.loads((app / "apex-team-export.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["builderLastUpdatedOn"], "2026-09-26T09:45:00")

    def test_powershell_unverified_publish_keeps_old_baseline_and_next_attempt_refuses(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, _, environment, app, state_dir = self.make_stateful_dev_fixture(root)
            environment["PROJECT_ENV_FILE"] = str(root / ".env")
            environment["FAKE_EXPORT_MISMATCH"] = "1"
            marker_path = app / "apex-team-export.json"
            original_marker = marker_path.read_bytes()
            command = [pwsh, "-NoProfile", "-File", str(root / "scripts/publish_app.ps1"), "100"]

            first = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)
            self.assertNotEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertNotIn("Published APEX App", first.stdout + first.stderr)
            self.assertEqual(marker_path.read_bytes(), original_marker)

            environment.pop("FAKE_EXPORT_MISMATCH")
            retry = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, check=False)
            self.assertNotEqual(retry.returncode, 0, retry.stdout + retry.stderr)
            self.assertIn("application already locked", retry.stdout + retry.stderr)
            self.assertEqual((state_dir / "import-count.txt").read_text(encoding="utf-8").strip(), "1")

    def test_powershell_publish_requires_clean_client_output_and_sentinel(self) -> None:
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("PowerShell Core is not installed")

        for mode, expected_success in (("sp2", False), ("no-sentinel", False), ("success", True)):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                scripts = root / "scripts"
                scripts.mkdir()
                for name in (
                    "publish_app.ps1",
                    "publish_app.sql", "deployment_descriptor.py", "application_lock.py", "application_lock.sql", "no_application_lock.sql", "db_targets.py", "sqlcl_session.py", "sqlcl_session.sh", "windows_job.py", "sqlcl_safe.sh",
            "apex_compatibility.py", "verify_apex_release.sql", "application_lifecycle.py", "application_lifecycle.sql",
                    "load_env.ps1",
                    "invoke_sqlcl.ps1", "resolve_python.ps1",
                    "check_db_target.ps1",
                    "export_apps.sql", "verify_deployment_state.sql",
                    "verify_db_access.sql",
                    "normalize_apx.ps1",
                    "record_export_state.py", "source_evidence.py",
                    "verify_publish_state.py",
                    "validate_app_source.py",
                    "stamp_publish_version.py",
                ):
                    shutil.copy2(ROOT / "scripts" / name, scripts / name)
                shutil.copy2(ROOT / ".env.example", root / ".env")
                app = root / "apps" / "DEMO" / "100"
                deployments = app / "deployments"
                deployments.mkdir(parents=True)
                (app / "application.apx").write_text("app SAMPLE (\n    name: Sample\n)\n", encoding="utf-8")
                (app / ".apex").mkdir()
                (app / ".apex" / "apexlang.json").write_text('{"version":1}\n', encoding="utf-8")
                (deployments / "dev.json").write_text(
                    json.dumps({
                        "workspace": {"name": "DEV_WORKSPACE"},
                        "app": {"id": 100, "databaseSession": {"parsingSchema": "DEMO"}},
                    }),
                    encoding="utf-8",
                )
                fake_bin = root / "bin"
                fake_bin.mkdir()
                fake_sql = fake_bin / "sql"
                fake_sql.write_text(
                    "#!/usr/bin/env bash\n"
                    "cat > /dev/null\n"
            "if [[ ${1:-} == -V ]]; then printf 'SQLcl: Release 26.3 Production\\n'; exit 0; fi\n"
                    'if [[ -n "${FAKE_LOCK_HANDLER:-}" ]]; then python3 "$FAKE_LOCK_HANDLER" "$@"; lock_status=$?; [[ $lock_status == 3 ]] || exit "$lock_status"; fi\n'
                    "mode=other\n"
                    "for arg in \"$@\"; do case \"$arg\" in *@*publish_app.sql) mode=import ;; *@*export_apps.sql) mode=export ;; esac; done\n"
                    "if [[ $mode == import ]]; then\n"
                    "  case \"$FAKE_SQL_MODE\" in\n"
                    "    sp2) printf '%s\\n' 'SP2-0640: Not connected' ;;\n"
                    "    no-sentinel) printf '%s\\n' 'Import successful.' ;;\n"
                    "    skipped) printf '%s\\n' 'Workspace: NO_SUCH_WORKSPACE from deployment file: deployments/dev.json is invalid' 'APEX_IMPORT_VERIFIED:100' ;;\n"
                    "    *) printf '%s\\n' 'Import successful.' 'APEX_IMPORT_VERIFIED:100' ;;\n"
                    "  esac\n"
                    "elif [[ $mode == export ]]; then\n"
                    "  exported=\"$PWD/apps/DEMO/100\"; mkdir -p \"$exported/.apex\"\n"
                    "  cp \"$FAKE_SOURCE_DIR/application.apx\" \"$exported/application.apx\"\n"
                    "  cp \"$FAKE_SOURCE_DIR/.apex/apexlang.json\" \"$exported/.apex/apexlang.json\"\n"
                    "  printf '%s\\n' '2026-09-26T09:30:00|2026-09-26T09:30:02|Release 1.0' > \"$PWD/.apex-export-before.txt\"\n"
                    "  cp \"$PWD/.apex-export-before.txt\" \"$PWD/.apex-export-after.txt\"\n"
                    "fi\n",
                    encoding="utf-8",
                )
                fake_sql.chmod(0o755)
                fake_sqlcl.add_launcher(fake_bin)
                environment = os.environ.copy()
                environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
                environment["PROJECT_ENV_FILE"] = str(root / ".env")
                environment["FAKE_SQL_MODE"] = mode
                environment["FAKE_SOURCE_DIR"] = str(app)
                environment["FAKE_LOCK_HANDLER"] = str(ROOT/"tests/fake_application_lock_sql.py")

                result = subprocess.run(
                    [pwsh, "-NoProfile", "-File", str(scripts / "publish_app.ps1"), "100", "--force"],
                    cwd=root,
                    env=environment,
                    text=True,
                    capture_output=True,
                    check=False,
                )

                if expected_success:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("Published APEX App 100", result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn("Published APEX App 100", result.stdout + result.stderr)

    def staging_promotion_confirmed_with(self, answer: bytes) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner, sql_log, _, environment = self.make_publish_fixture(root)
            scripts = runner.parent
            env_text = (root / ".env").read_text(encoding="utf-8")
            env_text += "\nSTAGING_SQLCL_CONNECTION=stage-db\nSTAGING_EXPECTED_USER=STAGE_DEPLOYER\n"
            (root / ".env").write_text(env_text, encoding="utf-8")
            environment["PROJECT_ENV_FILE"] = str(root / ".env")
            app = root / "apps" / "DEMO" / "100"
            deployments = app / "deployments"
            (deployments / "staging.json").write_text(
                json.dumps(
                    {
                        "workspace": {"name": "STAGE_WORKSPACE"},
                        "app": {"id": 100, "databaseSession": {"parsingSchema": "STAGE_APP"}},
                    }
                ),
                encoding="utf-8",
            )

            guard_marker = root / "drift-guard-called"
            drift_guard = scripts / "check_builder_drift.py"
            drift_guard.write_text(
                "import pathlib, sys\n"
                "pathlib.Path(sys.argv[1]).write_text('called')\n"
                "raise SystemExit(77)\n",
                encoding="utf-8",
            )
            original_source = (app / "application.apx").read_bytes()

            result = subprocess.run(
                [BASH, str(runner), "100", "--env", "staging"],
                cwd=root,
                env=environment,
                # Bytes, not text: text-mode input turns "\n" into "\r\n" on Windows.
                input=answer,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            self.assertFalse(guard_marker.exists(), "staging promotions must not compare Builder edit timestamps")
            self.assertIn("deployments/staging.json", sql_log.read_text(encoding="utf-8"))
            # Promotion ships the committed DEV publish tag unchanged.
            self.assertEqual((app / "application.apx").read_bytes(), original_source)
            self.assertNotIn("Stamped application version", result.stdout.decode("utf-8", "replace"))

    def test_staging_publish_skips_dev_builder_drift_guard(self) -> None:
        self.staging_promotion_confirmed_with(b"y\n")

    def test_a_crlf_yes_is_accepted_for_a_promotion(self) -> None:
        # Windows PowerShell appends CR LF to what it pipes into a native program, so
        # `"y" | scripts\team.ps1 deploy 100 --env staging` reaches Bash as "y\r\n".
        self.staging_promotion_confirmed_with(b"y\r\n")


if __name__ == "__main__":
    unittest.main()
