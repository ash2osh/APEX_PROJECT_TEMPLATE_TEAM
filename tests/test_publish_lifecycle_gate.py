import json
import shutil
import subprocess
import _test_subprocess_safety  # noqa: F401
import tempfile
import unittest
from pathlib import Path

import test_publish_cli
from fake_sqlcl import BASH

ROOT = Path(__file__).resolve().parents[1]


def pwsh_7_3_or_newer():
    """Return the pwsh launcher when it is PowerShell 7.3 or newer, else None (the native-error preference needs 7.3)."""
    executable = shutil.which('pwsh')
    if not executable:
        return None
    result = subprocess.run([executable, '-NoProfile', '-Command', '$PSVersionTable.PSVersion.ToString()'],
                            text=True, capture_output=True, check=False)
    parts = result.stdout.strip().split('.')
    if result.returncode != 0 or len(parts) < 2 or not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    return executable if (int(parts[0]), int(parts[1])) >= (7, 3) else None


class PublishLifecycleGateTests(unittest.TestCase):
    def test_lifecycle_refusal_keeps_the_diagnostics_its_message_names_in_both_shells(self):
        launchers = [('bash', [BASH])]
        if _test_subprocess_safety.working_pwsh():
            launchers.append(('pwsh', [_test_subprocess_safety.working_pwsh(), '-NoProfile', '-File']))
        for engine, launcher in launchers:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fixture = test_publish_cli.PublishAppCliTests()
                runner, _, _, environment, app, state = fixture.make_stateful_dev_fixture(root)
                environment['PROJECT_ENV_FILE'] = str(root / '.env')
                (root / 'scripts/application_lifecycle.sql').write_text('-- fixture\n')
                # The capture refuses the way the 26.1 adapter does on an unsupported release: it names its
                # SQLcl diagnostics file on stderr and exits 2, as application_lifecycle.py does.
                (root / 'scripts/application_lifecycle.py').write_text(
                    'import sys\nfrom pathlib import Path\n'
                    'a=sys.argv;r=Path(a[a.index("--run-dir")+1]);r.mkdir(parents=True,exist_ok=True)\n'
                    'if a[1]=="capture":\n'
                    ' log=r/"sqlcl-output.log"\n'
                    ' log.write_text("ORA-20018: Lifecycle adapter requires APEX 26.1 or 26.2; found 26.3.0\\n")\n'
                    ' sys.stderr.write("lifecycle: SQLcl exited with status 2; diagnostics: "+str(log)+"\\n")\n'
                    ' raise SystemExit(2)\n'
                )
                imports_before = (state / 'import-count.txt').read_text() if (state / 'import-count.txt').exists() else ''
                original = (app / 'apex-team-export.json').read_bytes()
                script = runner if engine == 'bash' else runner.with_suffix('.ps1')
                result = subprocess.run([*launcher, str(script), '100'], cwd=root, env=environment,
                                        text=True, capture_output=True)
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0, output)
                self.assertIn('nothing imported', output)
                imports_after = (state / 'import-count.txt').read_text() if (state / 'import-count.txt').exists() else ''
                self.assertEqual(imports_after, imports_before, 'a refused capture must not import')
                self.assertEqual((app / 'apex-team-export.json').read_bytes(), original)
                retained = list((root / 'scratch').glob('apex-publish*'))
                self.assertEqual(len(retained), 1, 'the scratch folder must survive when its message names a file in it')
                self.assertTrue((retained[0] / 'lifecycle-before' / 'sqlcl-output.log').exists(),
                                'the diagnostics file the refusal names must exist')
                self.assertIn('lifecycle diagnostics retained', output)

    def test_a_refusal_keeps_its_diagnostics_when_a_native_error_is_terminating(self):
        # With $PSNativeCommandUseErrorActionPreference set (a profile can set it, and team.ps1 runs publish_app.ps1 in
        # the same session), a non-zero exit from the capture ends the script at that line, before its $LASTEXITCODE
        # check. The diagnostics flag must already be set by then, or cleanup deletes the folder the message names.
        pwsh = pwsh_7_3_or_newer()
        if pwsh is None:
            self.skipTest('PowerShell 7.3 or newer (pwsh) is not installed')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = test_publish_cli.PublishAppCliTests()
            runner, _, _, environment, app, state = fixture.make_stateful_dev_fixture(root)
            environment['PROJECT_ENV_FILE'] = str(root / '.env')
            (root / 'scripts/application_lifecycle.sql').write_text('-- fixture\n')
            # The same refusal as the test above: the capture names its SQLcl diagnostics file and exits 2.
            (root / 'scripts/application_lifecycle.py').write_text(
                'import sys\nfrom pathlib import Path\n'
                'a=sys.argv;r=Path(a[a.index("--run-dir")+1]);r.mkdir(parents=True,exist_ok=True)\n'
                'if a[1]=="capture":\n'
                ' log=r/"sqlcl-output.log"\n'
                ' log.write_text("ORA-20018: Lifecycle adapter requires APEX 26.1 or 26.2; found 26.3.0\\n")\n'
                ' sys.stderr.write("lifecycle: SQLcl exited with status 2; diagnostics: "+str(log)+"\\n")\n'
                ' raise SystemExit(2)\n'
            )
            imports_before = (state / 'import-count.txt').read_text() if (state / 'import-count.txt').exists() else ''
            original = (app / 'apex-team-export.json').read_bytes()
            script = runner.with_suffix('.ps1')
            # The preference is set in the calling session and inherited by the script it invokes with &.
            wrapper = f"$PSNativeCommandUseErrorActionPreference = $true; & '{script}' 100"
            result = subprocess.run([pwsh, '-NoProfile', '-Command', wrapper], cwd=root, env=environment,
                                    text=True, capture_output=True)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, output)
            imports_after = (state / 'import-count.txt').read_text() if (state / 'import-count.txt').exists() else ''
            self.assertEqual(imports_after, imports_before, 'a refused capture must not import')
            self.assertEqual((app / 'apex-team-export.json').read_bytes(), original)
            retained = list((root / 'scratch').glob('apex-publish*'))
            self.assertEqual(len(retained), 1, 'the scratch folder must survive a terminating native error from the capture')
            self.assertTrue((retained[0] / 'lifecycle-before' / 'sqlcl-output.log').exists(),
                            'the diagnostics file the refusal names must exist')
            self.assertIn('lifecycle diagnostics retained', output)

    def test_lifecycle_failure_preserves_baseline_and_recovery_in_both_shells(self):
        launchers = [('bash', [BASH])]
        if _test_subprocess_safety.working_pwsh():
            launchers.append(('pwsh', [_test_subprocess_safety.working_pwsh(), '-NoProfile', '-File']))
        for engine, launcher in launchers:
            for mode in ('post-attention', 'restore-failure', 'source-mismatch'):
                with self.subTest(engine=engine, mode=mode), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    fixture = test_publish_cli.PublishAppCliTests()
                    runner, _, _, environment, app, state = fixture.make_stateful_dev_fixture(root)
                    environment['PROJECT_ENV_FILE'] = str(root / '.env')
                    environment['LIFECYCLE_GATE_MODE'] = mode
                    if mode == 'source-mismatch':
                        environment['FAKE_EXPORT_MISMATCH'] = '1'
                    (root / 'scripts/application_lifecycle.sql').write_text('-- fixture\n')
                    (root / 'scripts/application_lifecycle.py').write_text(
                        'import sys,json,os\nfrom pathlib import Path\n'
                        'a=sys.argv;r=Path(a[a.index("--run-dir")+1]);r.mkdir(parents=True,exist_ok=True)\n'
                        'if a[1]=="capture":\n'
                        ' (r/"lifecycle-snapshot.json").write_text(json.dumps({"identity":dict(zip(("db_unique_name","service_name","container_id","container_name","edition","server_host","session_user","current_schema"),("FREE","freepdb1","3","FREEPDB1","ORA$BASE","docker","DEMO","DEMO")))}))\n'
                        'if a[1]=="verify":\n'
                        ' (r/"lifecycle-report.json").write_text("attention: new workflow suspension")\n'
                        ' p=Path(a[a.index("--summary-path")+1]);d=json.loads(p.read_text());d["lifecycleStatus"]="attention";p.write_text(json.dumps(d))\n'
                        ' if os.environ["LIFECYCLE_GATE_MODE"]=="post-attention":raise SystemExit(2)\n'
                        'if a[1]=="restore-automations" and os.environ["LIFECYCLE_GATE_MODE"]=="restore-failure":raise SystemExit(2)\n'
                    )
                    original = (app / 'apex-team-export.json').read_bytes()
                    script = runner if engine == 'bash' else runner.with_suffix('.ps1')
                    result = subprocess.run([*launcher, str(script), '100'], cwd=root, env=environment,
                                            text=True, capture_output=True)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual((state / 'import-count.txt').read_text().strip(), '1')
                    self.assertEqual((app / 'apex-team-export.json').read_bytes(), original,
                                     'failed lifecycle/source verification must not advance the DEV baseline')
                    retained = list((root / 'scratch').glob('apex-publish*'))
                    self.assertEqual(len(retained), 1, 'attempted unverified imports must retain diagnostics')
                    self.assertTrue((retained[0] / 'lifecycle-before/lifecycle-snapshot.json').exists())
                    transcript_name = 'sqlcl-output.log' if engine == 'bash' else 'import-transcript.log'
                    self.assertTrue((retained[0] / transcript_name).exists(), 'the import transcript must survive cleanup')
                    summary = json.loads((retained[0] / 'publish-verification.json').read_text())
                    self.assertEqual(summary['sourceVerified'], mode == 'post-attention')
                    self.assertEqual(summary['lifecycleStatus'], 'attention' if mode == 'post-attention' else 'unavailable')

    def test_missing_lifecycle_helper_refuses_before_import_in_both_shells(self):
        launchers = [('bash', [BASH])]
        if _test_subprocess_safety.working_pwsh():
            launchers.append(('pwsh', [_test_subprocess_safety.working_pwsh(), '-NoProfile', '-File']))
        for engine, launcher in launchers:
            for missing in ('scripts/application_lifecycle.py', 'scripts/application_lifecycle.sql'):
                with self.subTest(engine=engine, missing=missing), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    fixture = test_publish_cli.PublishAppCliTests()
                    runner, _, _, environment, app, state = fixture.make_stateful_dev_fixture(root)
                    environment['PROJECT_ENV_FILE'] = str(root / '.env')
                    (root / 'scripts/application_lifecycle.sql').write_text('-- fixture\n', encoding='utf-8')
                    (root / 'scripts/application_lifecycle.py').write_text(fixture.FAKE_LIFECYCLE, encoding='utf-8')
                    (root / missing).unlink()
                    original = (app / 'apex-team-export.json').read_bytes()
                    script = runner if engine == 'bash' else runner.with_suffix('.ps1')
                    result = subprocess.run([*launcher, str(script), '100'], cwd=root, env=environment,
                                            text=True, capture_output=True)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('application lifecycle helpers are unavailable', result.stdout + result.stderr)
                    self.assertEqual((state / 'import-count.txt').read_text(encoding='utf-8').strip(), '0')
                    self.assertEqual((app / 'apex-team-export.json').read_bytes(), original,
                                     'missing lifecycle helpers must not advance the DEV baseline')

    def test_describe_survives_missing_lifecycle_helpers_in_both_shells(self):
        launchers = [('bash', [BASH])]
        if _test_subprocess_safety.working_pwsh():
            launchers.append(('pwsh', [_test_subprocess_safety.working_pwsh(), '-NoProfile', '-File']))
        for engine, launcher in launchers:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fixture = test_publish_cli.PublishAppCliTests()
                runner, _, _, environment, app, _ = fixture.make_stateful_dev_fixture(root)
                environment['PROJECT_ENV_FILE'] = str(root / '.env')
                for helper in (root / 'scripts/application_lifecycle.py', root / 'scripts/application_lifecycle.sql'):
                    if helper.exists():
                        helper.unlink()
                script = runner if engine == 'bash' else runner.with_suffix('.ps1')
                result = subprocess.run([*launcher, str(script), '100', '--describe'], cwd=root, env=environment,
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('DEMO', result.stdout)

    def test_staging_publish_refuses_when_lifecycle_helpers_missing(self):
        launchers = [('bash', [BASH])]
        if _test_subprocess_safety.working_pwsh():
            launchers.append(('pwsh', [_test_subprocess_safety.working_pwsh(), '-NoProfile', '-File']))
        for engine, launcher in launchers:
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fixture = test_publish_cli.PublishAppCliTests()
                runner, sql_log, environment = fixture.make_promotion_fixture(root, 'staging', lifecycle=False)
                for helper in (root / 'scripts/application_lifecycle.py', root / 'scripts/application_lifecycle.sql'):
                    if helper.exists():
                        helper.unlink()
                if engine == 'pwsh':
                    import shutil
                    for name in ('publish_app.ps1', 'load_env.ps1', 'invoke_sqlcl.ps1', 'resolve_python.ps1', 'check_db_target.ps1', 'normalize_apx.ps1'):
                        shutil.copy2(ROOT / 'scripts' / name, root / 'scripts' / name)
                    shutil.copy2(ROOT / 'template-manifest.json', root / 'template-manifest.json')
                    if (ROOT / 'scripts' / 'template_release.ps1').exists():
                        shutil.copy2(ROOT / 'scripts' / 'template_release.ps1', root / 'scripts' / 'template_release.ps1')
                script = runner if engine == 'bash' else runner.with_suffix('.ps1')
                result = subprocess.run([*launcher, str(script), '100', '--env', 'staging', '-y'], cwd=root, env=environment,
                                        text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('application lifecycle helpers are unavailable', result.stdout + result.stderr)
                self.assertFalse(sql_log.exists(), 'missing lifecycle helpers must prevent the import')
