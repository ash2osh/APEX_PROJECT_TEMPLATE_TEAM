import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fake_sqlcl import BASH
import test_team_cli

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ('db_unique_name', 'service_name', 'container_id', 'container_name', 'edition', 'server_host', 'session_user', 'current_schema')


def snapshot():
    release = '26.2.0' if '26.2' in (ROOT / 'scripts/application_lifecycle.py').read_text()[:100] else '26.1.0'
    return {'schemaVersion': 1, 'applicationPresent': True,
            'identity': dict(zip(FIELDS, ('PRODDB', 'prod-service', '3', 'PRODPDB', 'ORA$BASE', 'dbhost', 'PROD_DEPLOYER', 'PROD_APP'), strict=True)),
            'release': release, 'workspace': 'PROD_WORKSPACE', 'workspaceId': '123', 'appId': 100,
            'startedAt': '2026-10-20T10:00:00Z', 'completedAt': '2026-10-20T10:00:01Z',
            'coverage': dict.fromkeys(('application', 'workspace', 'automations', 'workflows', 'tasks'), True),
            'automations': [], 'workflows': [], 'tasks': []}


class PublicationIdentityTests(unittest.TestCase):
    def run_generated_import(self, malformed=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = test_team_cli.TeamCliTests()
            script, log = case.make_deploy_checkout(root)
            for name in ('application_lifecycle.py', 'application_lifecycle.sql', 'db_targets.py', 'sqlcl_session.py', 'windows_job.py'):
                shutil.copy2(ROOT / 'scripts' / name, root / 'scripts' / name)
            result = case.run_deploy(script, log, '--manual')
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = [line.strip() for line in result.stdout.splitlines() if 'publication_identity' in line]
            self.assertTrue(commands, 'runbook must extract the captured identity before invoking SQLcl')
            capture = root / 'captured/lifecycle-before'
            capture.mkdir(parents=True)
            data = snapshot()
            if malformed:
                del data['identity']['service_name']
            (capture / 'lifecycle-snapshot.json').write_text(json.dumps(data), encoding='utf-8')
            environment = os.environ.copy()
            environment.update(PATH=str(root / 'bin') + os.pathsep + environment['PATH'],
                               FAKE_SQL_LOG=str(log), FAKE_SOURCE_DIR=str(root / 'apps/DEMO/100'))
            shell = 'set -euo pipefail\nsqlcl_dir="$1"\n' + '\n'.join(commands)
            executed = subprocess.run([BASH, '-c', shell, 'bash', str(capture.parent)], cwd=root,
                                      env=environment, text=True, capture_output=True)
            if malformed:
                self.assertNotEqual(executed.returncode, 0, executed.stdout + executed.stderr)
                self.assertFalse(log.exists(), 'malformed captured identity must refuse before import')
            else:
                self.assertEqual(executed.returncode, 0, executed.stdout + executed.stderr)
                arguments = log.read_text().splitlines()
                self.assertEqual(arguments[-8:], [data['identity'][field].encode('utf-8').hex().upper() for field in FIELDS])

    def test_manual_import_receives_all_captured_identity_arguments(self):
        self.run_generated_import()

    def test_manual_import_refuses_malformed_snapshot_before_sqlcl(self):
        self.run_generated_import(malformed=True)

    def test_writer_establishes_parsing_schema_before_identity_guard(self):
        source = (ROOT / 'scripts/publish_app.sql').read_text()
        setup = re.search(r'(?im)^ALTER SESSION SET CURRENT_SCHEMA = &&target_schema;\s*$', source)
        self.assertIsNotNone(setup, 'separate deployment login needs the same schema context as lifecycle capture')
        self.assertLess(source.index('Expected session user'), setup.start())
        self.assertLess(source.index('Target parsing schema does not exist'), setup.start())
        self.assertLess(setup.start(), source.index('@@publish_identity_guard.sql'))

    def test_manual_runbook_retains_recovery_until_lifecycle_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = test_team_cli.TeamCliTests()
            script, log = case.make_deploy_checkout(root)
            result = case.run_deploy(script, log, '--manual')
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = [line.strip() for line in result.stdout.splitlines()]
            traps = [line for line in lines if line.startswith('trap ')]
            self.assertEqual(len(traps), 2)
            self.assertTrue(all('deployment_verified' in line and 'Retained recovery:' in line for line in traps))
            restore = next(i for i, line in enumerate(lines) if 'application_lifecycle.py restore-automations ' in line)
            verify = next(i for i, line in enumerate(lines) if 'application_lifecycle.py verify ' in line)
            self.assertLess(restore, verify)
            self.assertLess(verify, lines.index('deployment_verified=true'))
            for verified in ('false', 'true'):
                evidence = root / ('evidence-' + verified)
                export = root / ('export-' + verified)
                evidence.mkdir(); export.mkdir()
                shell = 'sqlcl_dir="$1"; verify_dir="$2"; deployment_verified="$3"\n' + traps[-1] + '\nexit 2\n'
                subprocess.run([BASH, '-c', shell, 'bash', str(evidence), str(export), verified],
                               cwd=root, text=True, capture_output=True)
                self.assertEqual(evidence.exists(), verified == 'false')
                self.assertEqual(export.exists(), verified == 'false')

    def test_manual_summary_records_verified_source_before_lifecycle_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = test_team_cli.TeamCliTests()
            script, log = case.make_deploy_checkout(root)
            result = case.run_deploy(script, log, '--manual')
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = [line.strip() for line in result.stdout.splitlines()]
            writers = [line for line in lines if line.startswith('python3 ') and 'sourceVerified' in line]
            self.assertEqual(len(writers), 1, 'manual recovery summary must record the completed source check')
            source_index = next(i for i, line in enumerate(lines) if 'verify_publish_state.py ' in line)
            lifecycle_index = next(i for i, line in enumerate(lines) if 'application_lifecycle.py verify ' in line)
            self.assertLess(source_index, lines.index(writers[0]))
            self.assertLess(lines.index(writers[0]), lifecycle_index)
            evidence = root / 'manual-evidence'
            evidence.mkdir()
            executed = subprocess.run([BASH, '-c', 'set -euo pipefail\nsqlcl_dir="$1"\n' + writers[0],
                                       'bash', str(evidence)], cwd=root, text=True, capture_output=True)
            self.assertEqual(executed.returncode, 0, executed.stderr)
            self.assertEqual(json.loads((evidence / 'publish-verification.json').read_text()),
                             {'schemaVersion': 1, 'sourceVerified': True, 'lifecycleStatus': 'unavailable'})
