"""Explicit read-only qualification must guard identity before all observations."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from tools import qualify_template_readonly as qualify
from tools.probe_apex_26_2 import ProbeTarget
from test_application_lifecycle import snapshot


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); (self.root/'scratch').mkdir()
        self.target=ProbeTarget('docker-demo','DEMO','DEMO','DEMO',150,'local-host','freepdb1')
        self.identity=dict(sessionUser='DEMO',currentSchema='DEMO',serverHost='local-host',service='freepdb1',workspace='DEMO',workspaceId=123,apexVersion='26.2.0',databaseVersion='23.26.3.0.0',appCount=1)
        self.scripts=[]
    def runner(self,target,driver,directory):
        self.scripts.append(driver.read_text())
        return SimpleNamespace(returncode=0,output='APEX_PROBE_IDENTITY:'+json.dumps(self.identity)+'\nAPEX_PROBE_VERIFIED:identity\n')
    def run_qualification(self):
        with patch.object(qualify,'capture_inventory',return_value=SimpleNamespace(objects={'object':{}},identity={})),patch.object(qualify,'capture_lifecycle',return_value=snapshot()):
            return qualify.qualify(self.target,self.root/'scratch'/'result',repo_root=self.root,runner=self.runner,version_reader=lambda p:'26.3.0.0')
    def test_readonly_no_implicit_export_and_report_has_no_logs(self):
        report=self.run_qualification()
        self.assertEqual(report['status'],'pass');self.assertEqual(report['schemaVersion'],1)
        text='\n'.join(self.scripts).lower()
        self.assertIn('set transaction read only;',text)
        for word in ('apex export','apex import','commit;','remove_application'):self.assertNotIn(word,text)
        self.assertNotIn('APEX_PROBE_IDENTITY',json.dumps(report))
        self.assertTrue(all('durationSeconds' in check for check in report['checks']))
    def test_wrong_identity_prevents_catalog_and_lifecycle(self):
        self.identity['serverHost']='wrong'
        with patch.object(qualify,'capture_inventory') as catalog,patch.object(qualify,'capture_lifecycle') as lifecycle:
            report=qualify.qualify(self.target,self.root/'scratch'/'result',repo_root=self.root,runner=self.runner,version_reader=lambda p:'26.3.0.0')
        self.assertEqual(report['status'],'fail');catalog.assert_not_called();lifecycle.assert_not_called()
    def test_absent_application_refuses_before_catalog_or_runtime_health_claim(self):
        self.identity['appCount']=0
        with patch.object(qualify,'capture_inventory') as catalog,patch.object(qualify,'capture_lifecycle') as lifecycle:
            report=qualify.qualify(self.target,self.root/'scratch'/'result',repo_root=self.root,runner=self.runner,version_reader=lambda p:'26.3.0.0')
        self.assertEqual(report['status'],'fail');catalog.assert_not_called();lifecycle.assert_not_called()
    def test_existing_adapters_keep_one_readonly_transaction_and_exact_session_schema(self):
        def catalog(target,directory,*,_runner):
            directory.mkdir(parents=True); driver=directory/'catalog-driver.sql'
            driver.write_text('SET DEFINE ON\nALTER SESSION SET CURRENT_SCHEMA = DEMO;\nSET TRANSACTION READ ONLY;\nSELECT 1 FROM DUAL;\nEXIT SUCCESS ROLLBACK\n')
            _runner(target,driver,directory)
            return SimpleNamespace(objects={},identity={})
        with patch.object(qualify,'capture_inventory',side_effect=catalog),patch.object(qualify,'capture_lifecycle',return_value=snapshot()):
            report=qualify.qualify(self.target,self.root/'scratch'/'result',repo_root=self.root,runner=self.runner,version_reader=lambda p:'26.3.0.0')
        self.assertEqual(report['status'],'pass')
        for script in self.scripts:self.assertEqual(script.upper().count('SET TRANSACTION READ ONLY;'),1)
    def test_unknown_database_version_or_old_client_refuses(self):
        for version in ('26.2.0.0','unknown'):
            with self.subTest(version=version):
                report=qualify.qualify(self.target,self.root/'scratch'/version,repo_root=self.root,runner=self.runner,version_reader=lambda p,version=version:version)
                self.assertEqual(report['status'],'unavailable')
        self.assertEqual(self.scripts,[])
    def test_unavailable_logs_never_leak_secret(self):
        def fail(*args):raise RuntimeError('PASSWORD=private-secret token=private-secret')
        report=qualify.qualify(self.target,self.root/'scratch'/'result',repo_root=self.root,runner=fail,version_reader=lambda p:'26.3.0.0')
        self.assertEqual(report['status'],'unavailable');self.assertNotIn('private-secret',json.dumps(report))
    def test_unsafe_or_existing_output_refuses_before_connecting(self):
        outside=self.root/'other'; existing=self.root/'scratch'/'existing';existing.mkdir();(existing/'keep').write_text('keep')
        link=self.root/'scratch'/'linked';link.symlink_to(self.root,target_is_directory=True)
        for path in (outside,existing,link/'result'):
            with self.subTest(path=path),self.assertRaises(ValueError):
                qualify.qualify(self.target,path,repo_root=self.root,runner=self.runner,version_reader=lambda p:'26.3.0.0')
        self.assertEqual(self.scripts,[]);self.assertEqual((existing/'keep').read_text(),'keep')
