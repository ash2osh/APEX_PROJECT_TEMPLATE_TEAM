import json
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.partial_publish import select_page_files, require_selected_pages_unchanged, stage_partial_source, verify_partial_source
from scripts.source_evidence import tree_hashes

ROOT = Path(__file__).resolve().parents[1]


class PartialPublishTests(unittest.TestCase):
    def test_no_notice_requires_explicit_preserved_url_cutoff(self):
        from scripts.partial_publish import require_no_notice_cutoff
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, _ = self.fixture(root)
            with self.assertRaisesRegex(ValueError, 'coordinated'):
                require_no_notice_cutoff(source)
            d = source / 'deployments/default.json'; data = json.loads(d.read_text())
            data['app']['sessionStateProtection']['allowUrlsCreatedAfter'] = '2026-10-01T00:00:00'; d.write_text(json.dumps(data))
            app = source / 'application.apx'; app.write_text(app.read_text().replace('    security {', '    sessionStateProtection {\n        allowUrlsCreatedAfter: "2026-10-01T00:00:00"\n    }\n    security {'))
            require_no_notice_cutoff(source, data)
            data['app']['sessionStateProtection']['allowUrlsCreatedAfter'] = '2026-10-02T00:00:00'; d.write_text(json.dumps(data))
            configured = json.loads((source / 'deployments/dev.json').read_text())
            configured['app']['sessionStateProtection'] = {'allowUrlsCreatedAfter': '2026-10-01T00:00:00'}
            with self.assertRaises(ValueError): require_no_notice_cutoff(source, configured)

    def test_no_notice_refuses_implicit_salt_even_with_explicit_cutoff(self):
        from scripts.partial_publish import require_no_notice_cutoff
        with tempfile.TemporaryDirectory() as temporary:
            source, _ = self.fixture(Path(temporary))
            d = source / 'deployments/default.json'; data = json.loads(d.read_text())
            data['app']['sessionStateProtection']['allowUrlsCreatedAfter'] = '2026-10-01T00:00:00'
            d.write_text(json.dumps(data))
            configured = json.loads(json.dumps(data))
            configured['app']['sessionStateProtection'].pop('checksumSalt')
            with self.assertRaisesRegex(ValueError, 'explicit.*salt'):
                require_no_notice_cutoff(source, configured)

    def fixture(self, root):
        source = root / 'local'
        shutil.copytree(ROOT / 'tests/fixtures/apex_26_2/canonical_26_2', source)
        from scripts.upgrade_apexlang import normalize_source
        normalize_source(source)
        (source / 'deployments/dev.json').write_text('{"workspace":{"name":"DEMO"},"app":{"id":100,"databaseSession":{"parsingSchema":"DEMO"}}}\n')
        return source, tree_hashes(source)

    def test_only_existing_non_global_pages_can_be_selected(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, baseline = self.fixture(Path(temporary))
            for selection in ([], ['pages/p00000-global-page.apx'], ['application.apx'], ['deployments/dev.json'], ['shared-components/build-options.apx'], ['pages/missing.apx'], ['/pages/p00001-home.apx'], ['pages/../application.apx'], ['pages\\p00001-home.apx'], ['pages/p00001-home.apx'] * 2):
                with self.subTest(selection=selection), self.assertRaises(ValueError):
                    select_page_files(source, selection, baseline)

    def test_unselected_changes_additions_and_deletions_refuse(self):
        for change in ('edit', 'add', 'delete', 'descriptor', 'binary'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as temporary:
                source, baseline = self.fixture(Path(temporary))
                if change == 'edit': (source / 'pages/p09999-login.apx').write_text('page 9999 (title: Dirty)\n')
                if change == 'add': (source / 'pages/new.apx').write_text('page 2 ()\n')
                if change == 'delete': (source / 'pages/p09999-login.apx').unlink()
                if change == 'descriptor': (source / 'deployments/dev.json').write_text('{}')
                if change == 'binary': next(source.rglob('*.png')).write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'unselected'):
                    select_page_files(source, ['pages/p00001-home.apx'], baseline)

    def test_links_new_pages_and_ambiguous_page_ids_refuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, baseline = self.fixture(Path(temporary))
            page = source / 'pages/p00001-home.apx'
            page.unlink(); page.symlink_to(source / 'pages/p09999-login.apx')
            with self.assertRaises(ValueError): select_page_files(source, ['pages/p00001-home.apx'], baseline)
        with tempfile.TemporaryDirectory() as temporary:
            source, baseline = self.fixture(Path(temporary))
            login = source / 'pages/p09999-login.apx'
            login.write_text(login.read_text().replace('page 9999 (', 'page 1 (', 1))
            with self.assertRaisesRegex(ValueError, 'ambiguous'):
                select_page_files(source, ['pages/p00001-home.apx', 'pages/p09999-login.apx'], baseline)

    def test_selected_file_cannot_hide_another_top_level_component(self):
        with tempfile.TemporaryDirectory() as temporary:
            source, baseline = self.fixture(Path(temporary))
            page = source / 'pages/p00001-home.apx'
            page.write_text(page.read_text() + '\nbuildOption hidden-global (\n    name: Hidden global change\n)\n')
            with self.assertRaisesRegex(ValueError, 'page-only'):
                select_page_files(source, ['pages/p00001-home.apx'], baseline)

    def test_fresh_live_unrelated_work_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, baseline = self.fixture(root)
            live = root / 'live'; shutil.copytree(source, live)
            selected = source / 'pages/p00001-home.apx'; selected.write_text(selected.read_text().replace('title: Home', 'title: My change'))
            unrelated = live / 'pages/p09999-login.apx'; unrelated.write_text(unrelated.read_text().replace('name: Login Page', 'name: Teammate change'))
            files = select_page_files(source, ['pages/p00001-home.apx'], baseline)
            require_selected_pages_unchanged(live, files, baseline)
            stage = stage_partial_source(live, source, files, root / 'stage')
            self.assertEqual((stage / unrelated.relative_to(live)).read_bytes(), unrelated.read_bytes())
            self.assertEqual((stage / selected.relative_to(source)).read_bytes(), selected.read_bytes())
            self.assertEqual((stage / 'application.apx').read_bytes(), (live / 'application.apx').read_bytes())

    def test_selected_live_drift_refuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, baseline = self.fixture(root)
            files = select_page_files(source, ['pages/p00001-home.apx'], baseline)
            live = root / 'live'; shutil.copytree(source, live)
            (live / 'pages/p00001-home.apx').write_text('page 1 (title: Other editor)\n')
            with self.assertRaisesRegex(ValueError, 'selected page'):
                require_selected_pages_unchanged(live, files, baseline)

    def test_page_alias_changes_refuse_before_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, baseline = self.fixture(root)
            live = root / 'live'; shutil.copytree(source, live)
            selected = source / 'pages/p00001-home.apx'
            selected.write_text(selected.read_text().replace('alias: HOME', 'alias: RENAMED'))
            with self.assertRaisesRegex(ValueError, 'alias'):
                require_selected_pages_unchanged(live, (selected,), baseline)

    def test_whole_snapshot_verification_refuses_unintended_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, _ = self.fixture(root)
            observed = root / 'observed'; shutil.copytree(source, observed)
            verify_partial_source(source, observed, None)
            (observed / 'shared-components/build-options.apx').write_text('buildOption unexpected ()\n')
            with self.assertRaisesRegex(ValueError, 'source'):
                verify_partial_source(source, observed, None)

    def test_only_qualified_cutoff_materialization_is_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, _ = self.fixture(root)
            observed = root / 'observed'; shutil.copytree(source, observed)
            d = observed / 'deployments/default.json'; data = json.loads(d.read_text())
            data['app'].setdefault('sessionStateProtection', {})['allowUrlsCreatedAfter'] = '2026-10-08T12:30:00'; d.write_text(json.dumps(data))
            verify_partial_source(source, observed, '2026-10-08T12:30:00')
            with self.assertRaisesRegex(ValueError, 'cutoff'):
                verify_partial_source(source, observed, '2026-10-08T12:30:01')
            data['app']['runtime'] = {'debugging': False}; d.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'deployment'):
                verify_partial_source(source, observed, '2026-10-08T12:30:00')

class VerifiedSourceReplacementTests(unittest.TestCase):
    def test_dirty_source_replacement_requires_exact_preimport_receipt(self):
        import subprocess
        from scripts.source_evidence import tree_hashes
        for engine in ('bash', 'pwsh'):
            if not shutil.which(engine):
                continue
            for stale in (False, True):
                with self.subTest(engine=engine, stale=stale), tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    scripts = root / 'scripts'; scripts.mkdir()
                    for name in ('replace_mirror.sh', 'replace_mirror.ps1', 'resolve_python.ps1', 'check_mirror_source.py', 'source_evidence.py', 'validate_app_source.py'):
                        if (ROOT / 'scripts' / name).exists(): shutil.copyfile(ROOT / 'scripts' / name, scripts / name)
                    source = root / 'apps/DEMO/100'; source.mkdir(parents=True)
                    (source / 'application.apx').write_text('original\n')
                    subprocess.run(['git', 'init', '-q', str(root)], check=True)
                    subprocess.run(['git', '-C', str(root), 'add', 'apps'], check=True)
                    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'Fixture'], check=True)
                    (source / 'application.apx').write_text('reviewed edit\n')
                    stage = root / 'scratch/stage'; stage.mkdir(parents=True)
                    (stage / 'application.apx').write_text('verified canonical edit\n')
                    receipt = root / 'scratch/source.json'
                    receipt.write_text(json.dumps({'schemaVersion': 1, 'relativeDirectory': 'apps/DEMO/100', 'sourceFiles': tree_hashes(source), 'baselineHash': None}))
                    if stale: (source / 'application.apx').write_text('new unpublished edit\n')
                    command = ['bash', str(scripts / 'replace_mirror.sh'), '--verified-source', str(receipt), str(stage), 'apps/DEMO/100'] if engine == 'bash' else ['pwsh', '-NoProfile', '-File', str(scripts / 'replace_mirror.ps1'), '-VerifiedSource', str(receipt), str(stage), 'apps/DEMO/100']
                    result = subprocess.run(command, cwd=root, capture_output=True, text=True)
                    if stale:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn('source changed', result.stdout + result.stderr)
                        self.assertEqual((source / 'application.apx').read_text(), 'new unpublished edit\n')
                    else:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual((source / 'application.apx').read_text(), 'verified canonical edit\n')

class PartialBackend:
    def __init__(self, root, live, fail=None):
        from types import SimpleNamespace
        from scripts.record_export_state import AppState
        self.root, self.live, self.fail = root, live, fail
        self.calls = []; self.count = 0
        self.evidence = SimpleNamespace(developer='DEMO')
        self.last_revision = AppState(True, None, 'Current live base')
        self.last_page_locks = []
    def validate_target(self): self.calls.append('target')
    def acquire(self): self.calls.append('acquire')
    def check(self): self.calls.append('check')
    def release(self): self.calls.append('release')
    def capture_lifecycle(self):
        from test_application_lifecycle import snapshot
        self.calls.append('lifecycle-before')
        if self.fail=='lifecycle-before': raise ValueError('lifecycle pre-read unavailable')
        return snapshot()
    def verify_lifecycle(self,before):
        self.calls.append('lifecycle-after')
        return {'status':'unavailable' if self.fail=='lifecycle-after' else 'pass'}
    def export(self, name):
        self.calls.append('export'); self.count += 1
        destination = self.root / ('export-' + str(self.count))
        shutil.copytree(self.live, destination)
        return destination
    def verify_effective(self, source, live): self.calls.append('effective')
    def validate_source(self, source):
        self.calls.append('compile')
        if self.fail == 'compile': raise ValueError('merged references invalid')
    def import_pages(self, stage, selected, version, locks):
        from scripts.record_export_state import AppState
        self.calls.append('import')
        if self.fail == 'import': raise ValueError('import result unknown')
        if self.fail == 'stamp': raise ValueError('public version stamp failed after page import')
        shutil.rmtree(self.live); shutil.copytree(stage, self.live)
        self.last_revision = AppState(True, None, 'Unexpected version' if self.fail == 'version' else version)
        if self.fail == 'page-lock': self.last_page_locks = []
        if self.fail == 'unselected': (self.live / 'page-groups.apx').write_text('unexpected source\n')


class PartialWorkflowTests(unittest.TestCase):
    fixture = PartialPublishTests.fixture
    def test_lifecycle_gates_preserve_original_and_retain_lock_only_after_write(self):
        from scripts.partial_publish import publish_partial
        for failure in ('lifecycle-before','lifecycle-after'):
            with self.subTest(failure=failure),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary); source,backend=self.prepare(root,failure)
                baseline=(source/'apex-team-export.json').read_bytes()
                with self.assertRaisesRegex(ValueError,'lifecycle'):
                    publish_partial(source,100,['pages/p00001-home.apx'],'DEMO',root/'run',backend,'ALICE')
                self.assertEqual((source/'apex-team-export.json').read_bytes(),baseline)
                report=json.loads((root/'run/partial.json').read_text())
                self.assertEqual(report['lockRetained'],failure=='lifecycle-after')
                self.assertEqual(report['sourceVerified'],failure=='lifecycle-after')
                self.assertEqual('import' in backend.calls,failure=='lifecycle-after')
    def prepare(self, root, fail=None, no_notice=False):
        from scripts.record_export_state import marker_payload, AppState
        source, _ = self.fixture(root)
        locks = []
        if no_notice:
            for file in ('deployments/dev.json', 'deployments/default.json'):
                path = source / file; data = json.loads(path.read_text())
                data['app'].setdefault('sessionStateProtection', {}).update({'allowUrlsCreatedAfter': '2026-10-01T00:00:00', 'checksumSalt': '0' * 64})
                path.write_text(json.dumps(data))
            locks = [{'applicationId': 100, 'workspace': 'DEMO', 'pageId': 1, 'lockId': 123456, 'owner': 'DEMO', 'comment': 'Acquired before editing', 'lockedOn': '2026-10-08T12:30:00'}]
        (source / 'apex-team-export.json').write_text(json.dumps(marker_payload(100, AppState(True, None, 'Release 1.0'), source, locks)))
        live = root / 'live'; shutil.copytree(source, live)
        (live / 'apex-team-export.json').unlink()
        app = live / 'application.apx'
        import re
        app.write_text(re.sub(r'(?m)^(    name:.*)$', r'\1\n    version: "Current live base"', app.read_text(), count=1))
        page = source / 'pages/p00001-home.apx'; page.write_text(page.read_text().replace('title: Home', 'title: My edit'))
        backend = PartialBackend(root, live, fail)
        backend.last_page_locks = locks
        backend.descriptor = json.loads((source / 'deployments/dev.json').read_text())
        return source, backend

    def test_no_team_notice_requires_baseline_locks_with_unchanged_identity(self):
        from scripts.partial_publish import publish_partial
        for replacement in (False, True):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); source, backend = self.prepare(root, no_notice=True)
                if replacement:
                    backend.last_page_locks = [dict(backend.last_page_locks[0], lockId=999999)]
                    with self.assertRaisesRegex(ValueError, 'unchanged exclusive'):
                        publish_partial(source, 100, ['pages/p00001-home.apx'], 'DEMO', root / 'run', backend, 'ALICE', no_team_notice=True)
                    self.assertNotIn('import', backend.calls)
                else:
                    publish_partial(source, 100, ['pages/p00001-home.apx'], 'DEMO', root / 'run', backend, 'ALICE', no_team_notice=True)
                    self.assertTrue(json.loads((root / 'run/partial.json').read_text())['noTeamNoticeEligible'])

    def test_notice_refuses_missing_foreign_or_changed_lock_before_import(self):
        from scripts.partial_publish import publish_partial
        for changed in ('missing', 'foreign', 'comment', 'time', 'baseline'):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); source, backend = self.prepare(root, no_notice=True)
                if changed == 'missing': backend.last_page_locks = []
                if changed == 'foreign': backend.last_page_locks[0]['owner'] = 'OTHER'
                if changed == 'comment': backend.last_page_locks[0]['comment'] = 'Replaced comment'
                if changed == 'time': backend.last_page_locks[0]['lockedOn'] = '2026-10-08T12:30:01'
                if changed == 'baseline':
                    p = source / 'apex-team-export.json'; data = json.loads(p.read_text()); data.pop('pageLocks'); p.write_text(json.dumps(data))
                marker = (source / 'apex-team-export.json').read_bytes()
                with self.assertRaises(ValueError):
                    publish_partial(source, 100, ['pages/p00001-home.apx'], 'DEMO', root / 'run', backend, 'ALICE', no_team_notice=True)
                self.assertNotIn('import', backend.calls)
                self.assertIn('release', backend.calls)
                self.assertEqual((source / 'apex-team-export.json').read_bytes(), marker)

    def test_every_selected_page_requires_baseline_lock_for_notice_exemption(self):
        from scripts.partial_publish import publish_partial
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, backend = self.prepare(root, no_notice=True)
            with self.assertRaisesRegex(ValueError, 'every selected page'):
                publish_partial(source, 100, ['pages/p00001-home.apx', 'pages/p09999-login.apx'], 'DEMO', root / 'run', backend, 'ALICE', no_team_notice=True)
            self.assertNotIn('import', backend.calls)

    def test_changed_page_lock_after_write_retains_native_lock_and_baseline(self):
        from scripts.partial_publish import publish_partial
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, backend = self.prepare(root, 'page-lock', no_notice=True)
            marker = (source / 'apex-team-export.json').read_bytes()
            with self.assertRaisesRegex(ValueError, 'locks changed'):
                publish_partial(source, 100, ['pages/p00001-home.apx'], 'DEMO', root / 'run', backend, 'ALICE', no_team_notice=True)
            self.assertEqual((source / 'apex-team-export.json').read_bytes(), marker)
            self.assertNotIn('release', backend.calls)
            self.assertTrue(json.loads((root / 'run/partial.json').read_text())['lockRetained'])

    def test_success_stamps_fresh_version_and_keeps_original_until_installation(self):
        from scripts.partial_publish import publish_partial
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source, backend = self.prepare(root)
            original = tree_hashes(source)
            result = publish_partial(source, 100, ['pages/p00001-home.apx'], 'DEMO', root / 'run', backend, 'ALICE')
            self.assertEqual(tree_hashes(source), original)
            self.assertIn('Current live base [ALICE-', (result / 'application.apx').read_text())
            self.assertEqual(json.loads((result / 'apex-team-export.json').read_text())['schemaVersion'], 2)
            self.assertIn('release', backend.calls)
            self.assertLess(backend.calls.index('compile'), backend.calls.index('import'))

    def test_prewrite_failure_releases_but_unknown_write_retains_native_lock(self):
        from scripts.partial_publish import publish_partial
        for failure in ('compile', 'import', 'stamp', 'version', 'unselected'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); source, backend = self.prepare(root, failure)
                original = tree_hashes(source); marker = (source / 'apex-team-export.json').read_bytes()
                with self.assertRaises(ValueError):
                    publish_partial(source, 100, ['pages/p00001-home.apx'], 'DEMO', root / 'run', backend, 'ALICE')
                self.assertEqual(tree_hashes(source), original)
                self.assertEqual((source / 'apex-team-export.json').read_bytes(), marker)
                self.assertEqual('release' in backend.calls, failure == 'compile')
                self.assertEqual(json.loads((root / 'run/partial.json').read_text())['lockRetained'], failure != 'compile')

class PartialDriverTests(unittest.TestCase):
    fixture = PartialPublishTests.fixture
    def test_driver_imports_only_the_selected_paths_and_stamps_public_version(self):
        from scripts.partial_publish import build_partial_driver
        with tempfile.TemporaryDirectory() as temporary:
            source, _ = self.fixture(Path(temporary))
            selected = (source / 'pages/p00001-home.apx', source / 'pages/p09999-login.apx')
            driver = build_partial_driver(selected, source / 'deployments/dev.json', 100, "Release O'Neil & أحمد")
            imports = [line for line in driver.splitlines() if line.startswith('apex import ')]
            self.assertEqual(len(imports), 1)
            files = imports[0].split(' -files ', 1)[1]
            for path in selected: self.assertIn(path.resolve().as_posix(), files)
            self.assertNotIn('application.apx', files)
            self.assertIn('partial_publish.sql', driver)
            self.assertNotIn('Release O', driver, 'version is passed as verified UTF-8 hex')

class PartialCliTests(unittest.TestCase):
    def test_partial_options_refuse_non_dev_force_or_missing_selection_before_config(self):
        import os
        import subprocess
        for suffix, launcher in [('sh', ['bash']), ('ps1', ['pwsh', '-NoProfile', '-File'])]:
            if not shutil.which(launcher[0]): continue
            for args, message in [(['--file', 'pages/p00001-home.apx', '--env', 'staging'], 'partial publishing targets DEV only'), (['--file', 'pages/p00001-home.apx', '--force'], 'partial publishing does not accept --force'), (['--no-team-notice'], '--no-team-notice requires --file')]:
                with self.subTest(wrapper=suffix, args=args):
                    result = subprocess.run([*launcher, str(ROOT / ('scripts/publish_app.' + suffix)), '100', *args], env=dict(os.environ, PROJECT_ENV_FILE='/no/such/partial-config'), capture_output=True, text=True)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(message, result.stdout + result.stderr)
                    self.assertNotIn('configuration file not found', result.stdout + result.stderr)
