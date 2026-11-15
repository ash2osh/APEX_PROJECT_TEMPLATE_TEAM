import json
import subprocess
import _test_subprocess_safety  # noqa: F401
import tempfile
import unittest
from pathlib import Path

import test_publish_cli
from fake_sqlcl import BASH

ROOT = Path(__file__).resolve().parents[1]


class PublishLifecycleGateTests(unittest.TestCase):
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
                script = runner if engine == 'bash' else runner.with_suffix('.ps1')
                result = subprocess.run([*launcher, str(script), '100', '--env', 'staging', '-y'], cwd=root, env=environment,
                                        text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('application lifecycle helpers are unavailable', result.stdout + result.stderr)
                self.assertFalse(sql_log.exists(), 'missing lifecycle helpers must prevent the import')
