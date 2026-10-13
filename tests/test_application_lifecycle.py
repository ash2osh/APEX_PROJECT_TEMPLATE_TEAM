"""Runtime verification cannot confuse missing visibility with a healthy app."""
import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from scripts import application_lifecycle as lifecycle
from scripts.db_targets import Target

TARGET = Target('dev', 'docker-demo', 'DEMO', 'DEMO', 'test')


def snapshot():
    return {'schemaVersion': 1, 'applicationPresent':True, 'identity': {'session_user':'DEMO', 'current_schema':'DEMO', 'db_unique_name':'FREE', 'container_id':'3', 'container_name':'FREEPDB1', 'edition':'ORA$BASE', 'server_host':'docker', 'service_name':'freepdb1'}, 'release':'26.2.0', 'workspace':'DEMO', 'workspaceId':'123', 'appId':150, 'startedAt':'2026-10-09T10:00:00Z', 'completedAt':'2026-10-09T10:00:01Z', 'coverage':{'application':True,'workspace':True,'automations':True,'workflows':True,'tasks':True}, 'automations':[], 'workflows':[], 'tasks':[]}


def instance(state='ACTIVE', kind='workflows', identifier='1'):
    terminal = state in (lifecycle.WORKFLOW_TERMINAL if kind=='workflows' else lifecycle.TASK_TERMINAL)
    return {'instanceId':identifier, 'definitionId':'9', 'staticId':'DEFINITION', 'state':state, 'terminal':terminal}


class LifecycleTests(unittest.TestCase):
    def test_verified_empty_scope_passes(self):
        self.assertEqual(lifecycle.compare_lifecycle(snapshot(), snapshot())['status'], 'pass')

    def test_initial_promotion_requires_verified_absent_scope_and_visible_post_app(self):
        before=snapshot(); before['applicationPresent']=False
        after=snapshot(); after['applicationPresent']=True
        after['automations']=[{'staticId':'NEW_AUTO','status':'DISABLED'}]
        self.assertEqual(lifecycle.compare_lifecycle(before,after)['status'],'pass')
        before['tasks']=[instance('ASSIGNED','tasks')]
        self.assertEqual(lifecycle.compare_lifecycle(before,after)['status'],'unavailable')
        before=snapshot(); after=snapshot(); after['applicationPresent']=False
        self.assertEqual(lifecycle.compare_lifecycle(before,after)['status'],'attention')

    def test_bad_scope_coverage_and_rows_are_unavailable(self):
        mutations = [lambda d:d.pop('applicationPresent'), lambda d:d.update(schemaVersion=True), lambda d:d.update(release='26.1.0'), lambda d:d.update(appId=True), lambda d:d.update(workspace='OTHER'), lambda d:d['identity'].update(session_user='OTHER'), lambda d:d['coverage'].pop('tasks'), lambda d:d['coverage'].update(tasks=False), lambda d:d.update(startedAt='nonsense'), lambda d:d.update(workflows=[instance(),instance()]), lambda d:d.update(tasks=[instance('ASSIGNED','tasks','0')]), lambda d:d.update(workflows=[{**instance(),'terminal':True}]), lambda d:d.update(workflows=[instance('UNKNOWN')])]
        for mutate in mutations:
            after=snapshot(); mutate(after)
            with self.subTest(after=after):
                report=lifecycle.compare_lifecycle(snapshot(), after)
                self.assertEqual(report['status'],'unavailable')
                with self.assertRaises(ValueError): lifecycle.require_verified_lifecycle(report)

    def test_natural_progress_and_completion_pass_but_missing_active_refuses(self):
        for kind, active, done in [('workflows','ACTIVE','COMPLETED'),('tasks','ASSIGNED','COMPLETED')]:
            before=snapshot(); before[kind]=[instance(active,kind)]
            after=copy.deepcopy(before); after[kind][0]=instance(done,kind)
            self.assertEqual(lifecycle.compare_lifecycle(before,after)['status'],'pass')
            after[kind]=[]
            self.assertEqual(lifecycle.compare_lifecycle(before,after)['status'],'attention')
            before[kind]=[instance(done,kind)]
            self.assertEqual(lifecycle.compare_lifecycle(before,after)['status'],'pass')

    def test_new_errors_and_new_suspension_refuse_existing_errors_do_not(self):
        for kind,state in [('workflows','FAULTED'),('workflows','SUSPENDED'),('tasks','ERRORED')]:
            after=snapshot(); after[kind]=[instance(state,kind)]
            self.assertEqual(lifecycle.compare_lifecycle(snapshot(),after)['status'],'attention')
            self.assertEqual(lifecycle.compare_lifecycle(after,after)['status'],'pass')

    def test_changed_automation_status_is_attention(self):
        before=snapshot(); before['automations']=[{'staticId':'AUTO','status':'ACTIVE'}]
        after=copy.deepcopy(before); after['automations'][0]['status']='DISABLED'
        self.assertEqual(lifecycle.compare_lifecycle(before,after)['status'],'attention')
        after['automations']=[]
        self.assertEqual(lifecycle.compare_lifecycle(before,after)['status'],'attention')

    def test_suspension_gets_only_one_bounded_second_observation(self):
        before=snapshot(); before['workflows']=[instance()]
        suspended=copy.deepcopy(before); suspended['workflows']=[instance('SUSPENDED')]
        with tempfile.TemporaryDirectory() as directory, patch.object(lifecycle,'capture_lifecycle',side_effect=[suspended,before]) as capture, patch.object(lifecycle.time,'sleep') as pause:
            report=lifecycle.verify_after_import(TARGET,'DEMO',150,before,Path(directory))
        self.assertEqual(report['status'],'pass'); self.assertEqual(capture.call_count,2); pause.assert_called_once_with(5)
        with tempfile.TemporaryDirectory() as directory, patch.object(lifecycle,'capture_lifecycle',return_value=suspended) as capture, patch.object(lifecycle.time,'sleep'):
            self.assertEqual(lifecycle.verify_after_import(TARGET,'DEMO',150,before,Path(directory))['status'],'attention')
        self.assertEqual(capture.call_count,2)

    def test_post_read_failure_remains_unavailable(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(lifecycle,'capture_lifecycle',side_effect=ValueError('unavailable')):
            self.assertEqual(lifecycle.verify_after_import(TARGET,'DEMO',150,snapshot(),Path(directory))['status'],'unavailable')

    def test_capture_requires_all_sentinels_and_exact_identity(self):
        import json
        from scripts.sqlcl_session import SqlclResult
        def runner(target,driver,directory):
            script=driver.read_text()
            self.assertIn('ALTER SESSION SET CURRENT_SCHEMA = DEMO;',script)
            self.assertLess(script.index('ALTER SESSION'),script.index('SET TRANSACTION READ ONLY'))
            return SqlclResult(0,'LIFECYCLE_PAYLOAD_BEGIN\n'+json.dumps(snapshot())+'\nLIFECYCLE_PAYLOAD_END\nLIFECYCLE_VERIFIED\n',directory)
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(lifecycle.capture_lifecycle(TARGET,'DEMO',150,Path(directory),_runner=runner)['appId'],150)
            with self.assertRaises(ValueError): lifecycle.capture_lifecycle(TARGET,'OTHER',150,Path(directory)/'bad',_runner=runner)
        def missing(target,driver,directory): return SqlclResult(0,'LIFECYCLE_VERIFIED\n',directory)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError): lifecycle.capture_lifecycle(TARGET,'DEMO',150,Path(directory),_runner=missing)

    def test_restore_enables_only_pre_import_active_automations_and_prints_readback(self):
        from scripts.sqlcl_session import SqlclResult

        before = snapshot()
        before['automations'] = [
            {'staticId': 'ACTIVE_AUTO', 'status': 'ACTIVE'},
            {'staticId': 'DISABLED_AUTO', 'status': 'DISABLED'},
        ]
        imported = copy.deepcopy(before)
        imported['automations'] = [
            {'staticId': 'ACTIVE_AUTO', 'status': 'DISABLED'},
            {'staticId': 'DISABLED_AUTO', 'status': 'DISABLED'},
            {'staticId': 'NEW_AUTO', 'status': 'DISABLED'},
        ]
        restored = copy.deepcopy(imported)
        restored['automations'][0]['status'] = 'ACTIVE'
        calls = []

        def runner(target, driver, directory):
            text = driver.read_text(encoding='utf-8')
            if 'automation-restore-driver.sql' in str(driver):
                calls.append(text)
                return SqlclResult(0, 'LIFECYCLE_CONTEXT_VERIFIED\nAUTOMATION_RESTORE_OK:4143544956455F4155544F\nAUTOMATION_RESTORE_COMPLETE\n', directory)
            if 'observation-before-enable' in str(driver):
                data = imported
            else:
                data = restored
            return SqlclResult(0, 'LIFECYCLE_PAYLOAD_BEGIN\n' + json.dumps(data) + '\nLIFECYCLE_PAYLOAD_END\nLIFECYCLE_VERIFIED\n', directory)

        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()) as output:
            report = lifecycle.restore_automations(TARGET, 'DEMO', 150, before, Path(directory), _runner=runner)

        self.assertEqual(report['status'], 'pass')
        self.assertEqual(report['enabledStaticIds'], ['ACTIVE_AUTO'])
        driver = calls[0]
        self.assertEqual(driver.count('APEX_AUTOMATION.ENABLE'), 1)
        self.assertIn("APEX_AUTOMATION.ENABLE(p_application_id => 150, p_static_id => UTL_I18N.RAW_TO_CHAR(HEXTORAW('4143544956455F4155544F'),'AL32UTF8'))", driver)
        self.assertLess(driver.index('RESTORE_CONTEXT'), driver.index('APEX_AUTOMATION.ENABLE'))
        self.assertNotIn('DISABLED_AUTO', driver)
        self.assertNotIn('NEW_AUTO', driver)
        self.assertIn('ACTIVE_AUTO | ACTIVE | ACTIVE', output.getvalue())
        self.assertIn('DISABLED_AUTO | DISABLED | DISABLED', output.getvalue())

    def test_restore_reports_attention_when_a_previously_active_automation_stays_disabled(self):
        from scripts.sqlcl_session import SqlclResult

        before = snapshot()
        before['automations'] = [{'staticId': 'ACTIVE_AUTO', 'status': 'ACTIVE'}]
        imported = copy.deepcopy(before)
        imported['automations'][0]['status'] = 'DISABLED'
        final = copy.deepcopy(imported)

        def runner(target, driver, directory):
            if 'automation-restore-driver.sql' in str(driver):
                return SqlclResult(0, 'LIFECYCLE_CONTEXT_VERIFIED\nAUTOMATION_RESTORE_OK:4143544956455F4155544F\nAUTOMATION_RESTORE_COMPLETE\n', directory)
            data = imported if 'observation-before-enable' in str(driver) else final
            return SqlclResult(0, 'LIFECYCLE_PAYLOAD_BEGIN\n' + json.dumps(data) + '\nLIFECYCLE_PAYLOAD_END\nLIFECYCLE_VERIFIED\n', directory)

        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            report = lifecycle.restore_automations(TARGET, 'DEMO', 150, before, Path(directory), _runner=runner)

        self.assertEqual(report['status'], 'attention')
        self.assertEqual(report['reasons'], ['previously active automation is not active after restore: ACTIVE_AUTO'])

    def test_restore_action_exits_nonzero_on_attention(self):
        before = snapshot()
        with tempfile.TemporaryDirectory() as directory:
            before_path = Path(directory) / 'before.json'
            before_path.write_text(json.dumps(before), encoding='utf-8')
            output = io.StringIO()
            with contextlib.redirect_stderr(output), patch.object(
                lifecycle, 'restore_automations', return_value={'status': 'attention', 'reasons': ['missed restore']}
            ):
                result = lifecycle.main([
                    'restore-automations', '--connection', 'docker-demo', '--expected-user', 'DEMO',
                    '--schema', 'DEMO', '--workspace', 'DEMO', '--environment', 'dev',
                    '--run-dir', str(Path(directory) / 'run'), '--app-id', '150', '--before', str(before_path),
                ])
        self.assertEqual(result, 2)
        self.assertIn('missed restore', output.getvalue())

    def test_lifecycle_summary_preserves_source_verification_and_release_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before_path = root / 'before.json'
            before_path.write_text(json.dumps(snapshot()), encoding='utf-8')
            summary_path = root / 'publish-verification.json'
            source_summary = {
                'schemaVersion': 1,
                'sourceVerified': True,
                'lifecycleStatus': 'unavailable',
                'sourceProvenanceStatus': 'verified',
                'sourceApexRelease': '26.2.0',
                'targetApexRelease': '26.2.0',
                'versionPair': 'source APEX 26.2.0; target APEX 26.2.0',
            }
            summary_path.write_text(json.dumps(source_summary), encoding='utf-8')
            args = [
                'verify', '--connection', 'docker-demo', '--expected-user', 'DEMO',
                '--schema', 'DEMO', '--workspace', 'DEMO', '--environment', 'dev',
                '--run-dir', str(root / 'after'), '--app-id', '150', '--before', str(before_path),
                '--summary-path', str(summary_path),
            ]

            with patch.object(lifecycle, 'verify_after_import', return_value={'status': 'pass'}), \
                 patch.object(lifecycle, 'require_verified_lifecycle'), \
                 contextlib.redirect_stdout(io.StringIO()):
                result = lifecycle.main(args)

            self.assertEqual(result, 0)
            summary = json.loads(summary_path.read_text(encoding='utf-8'))
            self.assertEqual(summary['lifecycleStatus'], 'pass')
            self.assertIs(summary['sourceVerified'], True)
            self.assertEqual(summary['sourceProvenanceStatus'], 'verified')
            self.assertEqual(summary['sourceApexRelease'], '26.2.0')
            self.assertEqual(summary['targetApexRelease'], '26.2.0')
            self.assertEqual(summary['versionPair'], source_summary['versionPair'])
