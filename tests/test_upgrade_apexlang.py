import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.upgrade_apexlang import upgrade_source, NativeBackend


ROOT=Path(__file__).resolve().parents[1]


class Backend:
    def __init__(self,fixture,fail=None):self.fixture=fixture;self.calls=[];self.fail=fail;self.exports=0
    def require_clean(self,source):self.calls.append('clean')
    def validate_target(self):self.calls.append('target')
    def acquire(self):self.calls.append('acquire')
    def drift(self,source):
        self.calls.append('drift')
        if self.fail=='drift':raise ValueError('Builder drift')
    def import_source(self,source):
        self.calls.append('import')
        if self.fail == 'interrupt':
            raise KeyboardInterrupt
    def export(self,name):
        self.calls.append('export');self.exports+=1
        if self.fail == 'revision':
            self.last_revision = self.exports
        path=self.fixture.parent/name;shutil.copytree(self.fixture,path)
        if self.fail=='unstable' and self.exports>1:(path/'application.apx').write_text(f'app CHANGED{self.exports} ()\n')
        if self.fail == 'translation-loss':
            (path / 'generated-artifacts/translations.sql').write_bytes(b'corrupted repository')
        if self.fail=='static-loss':
            next(path.rglob('*.png')).write_bytes(b'corrupted static payload')
        return path
    def verify_effective(self,source,exported):self.calls.append('effective')
    def check(self):self.calls.append('check')
    def release(self):self.calls.append('release')


class UpgradeApexlangTests(unittest.TestCase):
    def test_legacy_files_conversion_refuses_unqualified_overrides_before_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, _ = self.fixture(root)
            backend = NativeBackend.__new__(NativeBackend)
            backend.descriptor = {'workspace': {'name': 'TEAM'}, 'app': {'id': 100, 'databaseSession': {'parsingSchema': 'DEMO'}, 'sessionStateProtection': {'checksumSalt': 'A' * 64}}}
            with self.assertRaisesRegex(ValueError, 'identity-only'):
                backend.validate_files_input(source)
            del backend.descriptor['app']['sessionStateProtection']
            backend.validate_files_input(source)

    @unittest.skipUnless(shutil.which('pwsh'), 'PowerShell unavailable')
    def test_team_powershell_reports_completed_conversion_exit_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            scripts = Path(temporary) / 'scripts'
            scripts.mkdir()
            shutil.copyfile(ROOT / 'scripts/team.ps1', scripts / 'team.ps1')
            (scripts / 'upgrade_apexlang.ps1').write_text("Write-Output 'fixture conversion completed'\nexit 0\n")
            result = subprocess.run(['pwsh', '-NoProfile', '-File', str(scripts / 'team.ps1'), 'upgrade-apexlang', '100', '--mode', 'builder'], capture_output=True, text=True)
            self.assertIn('fixture conversion completed', result.stdout)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_powershell_wrapper_loads_python_resolver_in_its_own_scope(self):
        wrapper = (ROOT / 'scripts/upgrade_apexlang.ps1').read_text()
        load = ". (Join-Path $PSScriptRoot 'resolve_python.ps1')"
        self.assertIn(load, wrapper)
        self.assertLess(wrapper.index(load), wrapper.index('$python = Resolve-TeamPython'))

    def fixture(self,root,fail=None):
        source=root/'source';canonical=root/'canonical'
        shutil.copytree(ROOT/'tests/fixtures/apex_26_2/source_26_1',source)
        shutil.copytree(ROOT/'tests/fixtures/apex_26_2/canonical_26_2',canonical)
        (source/'deployments/dev.json').write_text('{"authored":true}\n')
        return source,Backend(canonical,fail)

    def run_upgrade(self,root,mode='builder',fail=None):
        source,backend=self.fixture(root,fail)
        result=upgrade_source(None,100,source,'TEAM',mode,root/'run',backend=backend)
        return source,backend,result

    def test_builder_mode_never_imports(self):
        with tempfile.TemporaryDirectory() as temporary:
            _,backend,_=self.run_upgrade(Path(temporary))
            self.assertNotIn('import',backend.calls);self.assertNotIn('acquire',backend.calls)
            self.assertEqual(backend.calls.count('export'),2)

    def test_files_mode_refuses_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source,backend=self.fixture(root,'drift')
            with self.assertRaisesRegex(ValueError,'Builder drift'):upgrade_source(None,100,source,'TEAM','files',root/'run',backend=backend)
            self.assertNotIn('import',backend.calls);self.assertIn('release',backend.calls)

    def test_upgrade_removes_obsolete_component_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            source,_,stage=self.run_upgrade(Path(temporary))
            self.assertEqual(json.loads((stage/'.apex/apexlang.json').read_text())['mmdVersion'],'26.2.0+3479')
            self.assertNotEqual(set(p.relative_to(source) for p in source.rglob('*.apx')),set(p.relative_to(stage) for p in stage.rglob('*.apx')))

    def test_authored_descriptors_and_binary_assets_survive(self):
        with tempfile.TemporaryDirectory() as temporary:
            source,_,stage=self.run_upgrade(Path(temporary))
            self.assertEqual((source/'deployments/dev.json').read_bytes(),(stage/'deployments/dev.json').read_bytes())
            for original in source.rglob('*.png'):self.assertEqual(original.read_bytes(),(stage/original.relative_to(source)).read_bytes())

    def test_files_conversion_rejects_changed_static_payloads(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, backend = self.fixture(root, 'static-loss')
            with self.assertRaisesRegex(ValueError, 'static source'):
                upgrade_source(None, 100, source, 'TEAM', 'files', root / 'run', backend=backend)
            self.assertNotIn('release', backend.calls)

    def test_canonical_files_conversion_refuses_ignored_application_edits(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, backend = self.fixture(root)
            shutil.rmtree(source); shutil.copytree(backend.fixture, source)
            page = source / 'pages/p00001-home.apx'; page.write_text(page.read_text().replace('title: Home', 'title: Required edit'))
            with self.assertRaisesRegex(ValueError, 'source bytes'):
                upgrade_source(None, 100, source, 'TEAM', 'files', root / 'run', backend=backend)
            self.assertNotIn('release', backend.calls)

    def test_files_conversion_preserves_translation_repository_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, backend = self.fixture(root, 'translation-loss')
            for tree in (source, backend.fixture):
                p = tree / 'generated-artifacts/translations.sql'; p.parent.mkdir(); p.write_bytes(b'canonical repository bytes\n')
            with self.assertRaisesRegex(ValueError, 'translation|generated'):
                upgrade_source(None, 100, source, 'TEAM', 'files', root / 'run', backend=backend)
            self.assertNotIn('release', backend.calls)

    def test_unstable_second_export_refuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source,backend=self.fixture(root,'unstable')
            with self.assertRaisesRegex(ValueError,'stable'):upgrade_source(None,100,source,'TEAM','builder',root/'run',backend=backend)

    def test_revision_change_refuses_even_when_exported_bytes_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, backend = self.fixture(root, 'revision')
            with self.assertRaisesRegex(ValueError, 'revision'):
                upgrade_source(None, 100, source, 'TEAM', 'builder', root / 'run', backend=backend)

    def test_interrupted_import_retains_originals_and_native_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, backend = self.fixture(root, 'interrupt')
            original = (source / 'application.apx').read_bytes()
            with self.assertRaises(KeyboardInterrupt):
                upgrade_source(None, 100, source, 'TEAM', 'files', root / 'run', backend=backend)
            self.assertEqual(original, (source / 'application.apx').read_bytes())
            report = json.loads((root / 'run/conversion.json').read_text())
            self.assertTrue(report['lockRetained'])
            self.assertTrue(report['importAttempted'])
            self.assertNotIn('release', backend.calls)

    def test_conversion_failure_preserves_original_source_and_baseline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source,backend=self.fixture(root,'unstable')
            original={p.relative_to(source):p.read_bytes() for p in source.rglob('*') if p.is_file()}
            with self.assertRaises(ValueError):upgrade_source(None,100,source,'TEAM','files',root/'run',backend=backend)
            self.assertEqual(original,{p.relative_to(source):p.read_bytes() for p in source.rglob('*') if p.is_file()})
            self.assertNotIn('release',backend.calls,'unverified write retains its lock')
