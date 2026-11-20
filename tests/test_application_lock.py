import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import application_lock as locks
from scripts.db_targets import Target


class ApplicationLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.target = Target('dev', 'docker-demo', 'DEMO', 'DEMO', 'development')
        self.calls = []
        self.observed = None
        self.fail_phase = None
        self.runner_patch = patch.object(locks, 'run_sqlcl', self.runner)
        self.runner_patch.start()
        self.addCleanup(self.runner_patch.stop)

    def runner(self, target, driver, directory):
        sql = driver.read_text(encoding="utf-8")
        phase = directory.name.split('-', 1)[0]
        self.calls.append((phase, sql))
        if phase == self.fail_phase:
            raise RuntimeError('connection lost after write')
        if phase == 'acquire':
            # The result carries what the database read back after COMMIT.
            intent = json.loads((directory.parent / 'recovery.json').read_text(encoding='utf-8'))
            self.observed = dict(intent['evidence'], locked_on='2026-10-08T11:00:00.000000')
        elif phase in ('release', 'unlock'):
            self.observed = None
        return SimpleNamespace(output='APEX_LOCK_STATE:' + json.dumps(self.observed) + '\nAPEX_LOCK_VERIFIED:' + phase + '\n')

    def acquire(self, developer="Dev.O'Neil@example.com", comment=''):
        return locks.acquire_lock(self.target, 100, 'TEAM', developer, self.directory, comment=comment)

    def test_acquire_stores_verified_commit_and_exact_owner_without_fallback(self):
        evidence = self.acquire()
        self.assertEqual(evidence.developer, "Dev.O'Neil@example.com")
        self.assertEqual(evidence.schema, 'DEMO')
        self.assertNotEqual(evidence.developer, evidence.schema)
        self.assertEqual(json.loads((self.directory / 'recovery.json').read_text())['status'], 'held')
        sql = self.calls[0][1]
        self.assertIn("Dev.O''Neil@example.com", sql)
        self.assertIn('apex_workspace_apex_users', sql)
        self.assertIn("is_application_developer='Yes'", sql)
        self.assertIn('commit;', sql.lower())

    def test_missing_developer_refuses_before_sqlcl(self):
        for developer in ('', ' ', None, 'DEV\nADMIN', 'DEV\x00'):
            with self.subTest(developer=developer), self.assertRaises(locks.LockError):
                self.acquire(developer)
        self.assertEqual(self.calls, [])

    def test_non_dev_or_production_looking_target_refuses_before_sqlcl(self):
        for target in (replace(self.target, environment='prod'), replace(self.target, classification='staging'),
                       replace(self.target, connection='team-prod')):
            with self.subTest(target=target), self.assertRaises(locks.LockError):
                locks.acquire_lock(target, 100, 'TEAM', 'ALICE', self.directory)
        self.assertEqual(self.calls, [])

    def test_invalid_id_and_symlinked_run_directory_refuse_before_write(self):
        for app_id in (True, 0, -1, 10**18, '100'):
            with self.subTest(app_id=app_id), self.assertRaises(locks.LockError):
                locks.acquire_lock(self.target, app_id, 'TEAM', 'ALICE', self.directory)
        link = self.directory / 'link'
        link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(locks.LockError):
            locks.acquire_lock(self.target, 100, 'TEAM', 'ALICE', link)
        self.assertEqual(self.calls, [])

    def test_preexisting_same_or_other_owner_is_refused_by_sql_before_lock_call(self):
        sql = locks.acquisition_sql(self.target, 100, 'TEAM', 'ALICE', 'a'*32, 'comment')
        self.assertLess(sql.index('Application already locked'), sql.index('apex_application_admin.lock_application('))
        self.assertIn('v_owner is not null', sql.lower())
        self.assertIn('Working copies are deferred', sql)
        self.assertIn('Application target not found', sql)

    def test_unicode_comment_and_ampersand_are_literals_with_substitution_disabled(self):
        evidence = self.acquire(comment="Review O'Neil & أحمد")
        self.assertIn("Review O''Neil & أحمد", self.calls[0][1])
        self.assertIn('set define off', self.calls[0][1].lower())
        self.assertTrue(evidence.comment.startswith('apex-team:'))

    def test_check_refuses_changed_owner_comment_or_timestamp(self):
        evidence = self.acquire()
        for key, value in (('developer', 'OTHER'), ('comment', 'different run'), ('locked_on', '2026-10-08T11:00:01.000000')):
            self.observed = dict(vars(evidence), **{key: value})
            with self.subTest(key=key), self.assertRaises(locks.LockError):
                locks.check_lock(self.target, evidence, self.directory)
        self.assertEqual(json.loads((self.directory / 'recovery.json').read_text())['status'], 'held')

    def test_release_uses_exact_proof_and_owner_filtered_public_unlock(self):
        evidence = self.acquire()
        locks.release_lock(self.target, evidence, self.directory)
        sql = self.calls[-1][1]
        self.assertIn('p_unlock_as_user =>', sql)
        self.assertIn("Dev.O''Neil@example.com", sql)
        self.assertIn(evidence.comment, sql)
        self.assertIn(evidence.locked_on, sql)
        self.assertEqual(json.loads((self.directory / 'recovery.json').read_text())['status'], 'released')

    def test_ambiguous_acquisition_retains_intent_and_does_not_unlock(self):
        self.fail_phase = 'acquire'
        with self.assertRaises(locks.LockError):
            self.acquire()
        record = json.loads((self.directory / 'recovery.json').read_text())
        self.assertEqual(record['status'], 'acquisition-unknown')
        self.assertEqual([phase for phase, sql in self.calls], ['acquire'])

    def test_release_failure_keeps_success_proof_for_recovery(self):
        evidence = self.acquire()
        self.fail_phase = 'release'
        with self.assertRaises(locks.LockError):
            locks.release_lock(self.target, evidence, self.directory)
        record = json.loads((self.directory / 'recovery.json').read_text())
        self.assertEqual(record['status'], 'release-unknown')
        self.assertEqual(record['evidence'], vars(evidence))

    def test_unknown_command_duplicate_state_or_missing_sentinel_cannot_verify_lock(self):
        for output in ('Unknown Command: lock\nAPEX_LOCK_STATE:null\nAPEX_LOCK_VERIFIED:acquire\n',
                       'APEX_LOCK_STATE:null\n', 'APEX_LOCK_STATE:null\nAPEX_LOCK_STATE:null\nAPEX_LOCK_VERIFIED:acquire\n'):
            with self.subTest(output=output), patch.object(locks, 'run_sqlcl', return_value=SimpleNamespace(output=output)):
                with self.assertRaises(locks.LockError):
                    self.acquire()

    def test_recovery_evidence_for_different_target_cannot_release(self):
        evidence = self.acquire()
        self.calls.clear()
        with self.assertRaises(locks.LockError):
            locks.release_lock(replace(self.target, schema='OTHER'), evidence, self.directory)
        self.assertEqual(self.calls, [])

    def test_manual_unlock_is_owner_filtered_and_refuses_other_owner(self):
        sql = locks.manual_unlock_sql(self.target, 100, 'TEAM', "Dev.O'Neil@example.com", 'a'*32)
        self.assertIn('upper(v_owner)<>upper(v_user)', sql)
        self.assertIn('p_unlock_as_user => v_user', sql)
        self.assertLess(sql.index('Application is locked by another developer'), sql.index('apex_application_admin.unlock_application('))

    def test_user_validation_requires_no_existing_application_or_write(self):
        sql = locks.user_validation_sql(self.target, 'TEAM', "Dev.O'Neil@example.com")
        self.assertIn('apex_workspace_apex_users', sql)
        self.assertNotIn('from apex_applications where application_id=', sql)
        self.assertNotIn('lock_application(', sql)
        self.assertNotIn('commit;', sql)

    OWNER = "Dev.O'Neil@example.com"

    def held_evidence(self, **changes):
        base = locks.LockEvidence(100, 'TEAM', 'DEMO', self.OWNER, 'b' * 32,
                                  '2026-10-08T11:00:00.000000', 'apex-team:' + 'b' * 32)
        return replace(base, **changes)

    def write_record(self, run_dir, status='held', target=None, evidence=None):
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / 'recovery.json'
        data = {'schemaVersion': 1, 'target': asdict(self.target if target is None else target),
                'status': status, 'evidence': asdict(self.held_evidence() if evidence is None else evidence)}
        path.write_text(json.dumps(data), encoding='utf-8')
        return path

    def apps_dir(self):
        return self.directory / 'locks' / '100'

    def unlock(self, run_dir, developer=None):
        developer = self.OWNER.upper() if developer is None else developer
        argv = ['unlock', '--connection', 'docker-demo', '--expected-user', 'DEMO', '--schema', 'DEMO',
                '--workspace', 'TEAM', '--developer', developer, '--app-id', '100', '--run-dir', str(run_dir)]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(locks, 'require_client'), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = locks.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_verified_unlock_closes_matching_earlier_held_record_keeping_its_evidence(self):
        path = self.write_record(self.apps_dir() / 'run.aaaaaa')
        before = json.loads(path.read_text(encoding='utf-8'))
        code, out, err = self.unlock(self.apps_dir() / 'run.bbbbbb')
        self.assertEqual(code, 0, err)
        after = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(after['status'], 'released')
        self.assertEqual(after['target'], before['target'])
        self.assertEqual(after['evidence'], before['evidence'])
        self.assertEqual(after['schemaVersion'], 1)
        self.assertIn('closed earlier held lock record: 100/run.aaaaaa', out)
        own = json.loads((self.apps_dir() / 'run.bbbbbb' / 'recovery.json').read_text(encoding='utf-8'))
        self.assertEqual(own['status'], 'released')

    def test_unmatched_records_are_left_exactly_as_they_were(self):
        other_target = replace(self.target, connection='other-dev')
        cases = {
            'run.other1': dict(evidence=self.held_evidence(developer='OTHER')),
            'run.other2': dict(evidence=self.held_evidence(workspace='OTHER_WS')),
            'run.other3': dict(evidence=self.held_evidence(app_id=101)),
            'run.other4': dict(evidence=self.held_evidence(schema='OTHER')),
            'run.other5': dict(target=other_target),
            'run.other6': dict(status='acquisition-unknown'),
            'run.other7': dict(status='release-unknown'),
        }
        paths = {name: self.write_record(self.apps_dir() / name, **kwargs) for name, kwargs in cases.items()}
        before = {name: path.read_bytes() for name, path in paths.items()}
        broken = self.apps_dir() / 'run.other8'
        broken.mkdir(parents=True)
        (broken / 'recovery.json').write_text('{not json', encoding='utf-8')
        outside = self.directory / 'outside-recovery.json'
        outside.write_text(json.dumps({'schemaVersion': 1, 'target': asdict(self.target), 'status': 'held',
                                       'evidence': asdict(self.held_evidence())}), encoding='utf-8')
        link_dir = self.apps_dir() / 'run.other9'
        link_dir.mkdir(parents=True)
        (link_dir / 'recovery.json').symlink_to(outside)
        other_app = self.directory / 'locks' / '101' / 'run.aaaaaa'
        self.write_record(other_app, evidence=self.held_evidence(app_id=101))
        other_app_before = (other_app / 'recovery.json').read_bytes()

        code, out, err = self.unlock(self.apps_dir() / 'run.bbbbbb')

        self.assertEqual(code, 0, err)
        self.assertNotIn('closed earlier', out)
        for name, data in before.items():
            self.assertEqual(paths[name].read_bytes(), data, name)
        self.assertEqual((broken / 'recovery.json').read_text(encoding='utf-8'), '{not json')
        self.assertTrue((link_dir / 'recovery.json').is_symlink())
        self.assertEqual(json.loads(outside.read_text(encoding='utf-8'))['status'], 'held')
        self.assertEqual((other_app / 'recovery.json').read_bytes(), other_app_before)

    def test_close_never_touches_the_unlocks_own_directory(self):
        own = self.apps_dir() / 'run.own000'
        path = self.write_record(own)
        before = path.read_bytes()
        intent = locks.LockEvidence(100, 'TEAM', 'DEMO', self.OWNER, 'c' * 32, '', 'manual owner-filtered unlock')
        locks.close_held_records(self.target, intent, own, time.time())
        self.assertEqual(path.read_bytes(), before)

    def test_unverified_unlock_closes_nothing(self):
        path = self.write_record(self.apps_dir() / 'run.aaaaaa')
        before = path.read_bytes()
        with patch.object(locks, 'execute', side_effect=locks.LockError('unverified unlock')):
            code, out, err = self.unlock(self.apps_dir() / 'run.bbbbbb')
        self.assertEqual(code, 2)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn('closed earlier', out)

    def test_closing_failure_is_reported_and_other_record_still_closes(self):
        failing_path = self.write_record(self.apps_dir() / 'run.aaaaaa')
        ok_path = self.write_record(self.apps_dir() / 'run.cccccc')
        real_save = locks.save_record

        def flaky(directory, target, status, evidence):
            if Path(directory).name == 'run.aaaaaa' and status == 'released':
                raise OSError('directory is read-only')
            return real_save(directory, target, status, evidence)

        with patch.object(locks, 'save_record', side_effect=flaky):
            code, out, err = self.unlock(self.apps_dir() / 'run.bbbbbb')

        self.assertEqual(code, 0, err)
        self.assertIn('could not close earlier held lock record', err)
        self.assertIn('run.aaaaaa', err)
        self.assertIn('closed earlier held lock record: 100/run.cccccc', out)
        self.assertNotIn('run.aaaaaa', out)
        self.assertEqual(json.loads(failing_path.read_text(encoding='utf-8'))['status'], 'held')
        self.assertEqual(json.loads(ok_path.read_text(encoding='utf-8'))['status'], 'released')

    def test_record_written_after_the_unlock_started_stays_held(self):
        path = self.write_record(self.apps_dir() / 'run.aaaaaa')
        later = time.time() + 3600
        os.utime(path, (later, later))
        before = path.read_bytes()
        code, out, err = self.unlock(self.apps_dir() / 'run.bbbbbb')
        self.assertEqual(code, 0, err)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn('run.aaaaaa', out)

    def test_matching_publish_record_closes_and_keeps_its_evidence(self):
        path = self.write_record(self.apps_dir() / 'publish.aaaaaa')
        before = json.loads(path.read_text(encoding='utf-8'))
        code, out, err = self.unlock(self.apps_dir() / 'run.bbbbbb')
        self.assertEqual(code, 0, err)
        after = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(after['status'], 'released')
        self.assertEqual(after['target'], before['target'])
        self.assertEqual(after['evidence'], before['evidence'])
        self.assertIn('closed earlier held lock record: 100/publish.aaaaaa', out)

    def test_non_matching_publish_record_stays_held(self):
        path = self.write_record(self.apps_dir() / 'publish.aaaaaa', evidence=self.held_evidence(developer='OTHER'))
        before = path.read_bytes()
        code, out, err = self.unlock(self.apps_dir() / 'run.bbbbbb')
        self.assertEqual(code, 0, err)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn('closed earlier', out)

    def test_reacquire_never_overwrites_unresolved_recovery_proof(self):
        self.acquire()
        original = (self.directory / 'recovery.json').read_bytes()
        self.calls.clear()
        with self.assertRaises(locks.LockError):
            self.acquire()
        self.assertEqual((self.directory / 'recovery.json').read_bytes(), original)
        self.assertEqual(self.calls, [])
