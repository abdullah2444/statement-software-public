import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch


class RetentionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {
            'DATABASE_PATH': str(Path(cls.tmp.name) / 'test.db'),
            'UPLOAD_DIR': str(Path(cls.tmp.name) / 'uploads'),
            'BACKUP_DIR': str(Path(cls.tmp.name) / 'backups'),
            'SECRET_KEY': 'test-only', 'INITIAL_ADMIN_USERNAME': 'test',
            'INITIAL_ADMIN_PASSWORD': 'test-only',
        }):
            spec = importlib.util.spec_from_file_location('retention_app', Path(__file__).parents[1] / 'app.py')
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)
            cls.module.init_db()
        cls.module.app.config['TESTING'] = True

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def entry(self, filename):
        return {'path': 'backups/' + filename, 'type': 'blob', 'mode': '100644'}

    def test_calendar_boundary_and_protected_files(self):
        entries = [self.entry(name) for name in [
            'statement-full-backup-20260926-235959.tar.gz',
            'statement-full-backup-20260927-000000.tar.gz',
            'statement-full-backup-20261003-120000.tar.gz',
            'statement-full-backup-latest.tar.gz', 'other.tar.gz',
            'statement-full-backup-20260900-120000.tar.gz',
        ]]
        old = self.module._github_cleanup_candidates(entries, 7, datetime(2026, 10, 3, tzinfo=timezone.utc))
        self.assertEqual([entry['path'] for entry in old], [entries[0]['path']])

    def test_stale_newest_backup_is_preserved(self):
        entries = [self.entry('statement-full-backup-20260901-120000.tar.gz'),
                   self.entry('statement-full-backup-20260902-120000.tar.gz')]
        self.assertEqual(self.module._github_cleanup_candidates(entries, 7,
                         datetime(2026, 10, 3, tzinfo=timezone.utc)), entries[:1])

    def cleanup(self, days='7', truncated=False, changed=False, delete_all=False):
        calls = []
        settings = {'github_backup_retention_days': days, 'github_backup_repo': 'owner/repo',
                    'github_backup_token': 'test-token'}
        entries = [self.entry('statement-full-backup-20260901-120000.tar.gz'),
                   self.entry('statement-full-backup-20261003-120000.tar.gz'),
                   self.entry('statement-full-backup-latest.tar.gz'), self.entry('unrelated.txt')]
        refs = 0
        def urlopen(request, timeout):
            nonlocal refs
            path = request.full_url.split('/repos/owner/repo')[1]
            body = json.loads(request.data) if request.data else None
            calls.append((request.method, path, body))
            if path == '': data = {'default_branch': 'main'}
            elif path == '/git/ref/heads/main':
                refs += 1
                data = {'object': {'sha': 'other' if changed and refs > 1 else 'head'}}
            elif path == '/git/commits/head': data = {'tree': {'sha': 'tree'}}
            elif path.startswith('/git/trees/tree?'): data = {'tree': entries, 'truncated': truncated}
            else: data = {'sha': 'new'}
            class Response:
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self): return json.dumps(data).encode()
            return Response()
        with patch.object(self.module, 'get_setting', side_effect=lambda key, default='': settings.get(key, default)), \
             patch.object(self.module, 'utc_now', return_value=datetime(2026, 10, 3, tzinfo=timezone.utc)), \
             patch('urllib.request.urlopen', side_effect=urlopen):
            result = self.module._cleanup_github_backups(delete_all=delete_all)
        return result, calls

    def test_cleanup_deletes_in_one_commit_without_force(self):
        result, calls = self.cleanup()
        self.assertEqual(result['deleted'], 1)
        tree = next(body for method, path, body in calls if path == '/git/trees' and method == 'POST')
        self.assertEqual(tree['tree'][0]['sha'], None)
        self.assertEqual(calls[-1][2]['force'], False)

    def test_disabled_does_not_contact_github(self):
        result, calls = self.cleanup(days='0')
        self.assertTrue(result['disabled'])
        self.assertEqual(calls, [])

    def test_delete_all_includes_latest_and_preserves_unrelated_files(self):
        result, calls = self.cleanup(days='0', delete_all=True)
        self.assertEqual(result['deleted'], 3)
        tree = next(body for method, path, body in calls if path == '/git/trees' and method == 'POST')
        paths = [entry['path'] for entry in tree['tree']]
        self.assertIn('backups/statement-full-backup-latest.tar.gz', paths)
        self.assertNotIn('backups/unrelated.txt', paths)

    def test_delete_all_requires_confirmation_and_admin(self):
        client = self.module.app.test_client()
        self.assertEqual(client.post('/api/github-backup/delete-all', json={}).status_code, 401)
        with client.session_transaction() as session:
            session['user_id'] = 1
            session['_csrf_token'] = 'test-csrf'
        headers = {'X-CSRF-Token': 'test-csrf'}
        with patch.object(self.module, '_cleanup_github_backups', return_value={'ok': True, 'deleted': 3}) as cleanup:
            for payload in [{}, {'confirmation': 'delete all'}, []]:
                self.assertEqual(client.post('/api/github-backup/delete-all', json=payload, headers=headers).status_code, 400)
            cleanup.assert_not_called()
            self.assertEqual(client.post('/api/github-backup/delete-all', json={'confirmation': 'DELETE ALL'}).status_code, 400)
            response = client.post('/api/github-backup/delete-all', json={'confirmation': 'DELETE ALL'}, headers=headers)
            self.assertEqual(response.get_json()['deleted'], 3)
            cleanup.assert_called_once_with(delete_all=True)
            with patch.object(self.module, '_github_backup_operation_lock') as lock:
                lock.acquire.return_value = False
                response = client.post('/api/github-backup/delete-all', json={'confirmation': 'DELETE ALL'}, headers=headers)
                self.assertEqual(response.status_code, 409)
            with client.session_transaction() as session:
                session['user_id'] = 2
            with self.module.app.app_context():
                db = self.module.get_db()
                db.execute("insert into users(id,username,password_hash,role,is_active,must_change_password,created_at) values(2,'viewer','test','user',1,0,'test')")
                db.commit()
            self.assertEqual(client.post('/api/github-backup/delete-all', json={'confirmation': 'DELETE ALL'}, headers=headers).status_code, 403)

    def test_incomplete_listing_and_branch_race_stop_cleanup(self):
        for args in [{'truncated': True}, {'changed': True}]:
            result, calls = self.cleanup(**args)
            self.assertFalse(result['ok'])
            self.assertFalse(any(method == 'PATCH' for method, _, _ in calls))

    def test_automatic_cleanup_requires_both_uploads(self):
        import urllib.error
        for fail_latest in [False, True]:
            def urlopen(request, **kwargs):
                if fail_latest and request.method == 'PUT' and 'latest' in request.full_url:
                    raise urllib.error.HTTPError(request.full_url, 500, 'failed', {},
                                                 __import__('io').BytesIO(b'failed'))
                class Response:
                    status = 200
                    def __enter__(self): return self
                    def __exit__(self, *args): pass
                    def read(self): return b'{}'
                return Response()
            with patch.object(self.module, 'get_setting', side_effect=lambda key, default='':
                              {'github_backup_repo': 'owner/repo', 'github_backup_token': 'test'}.get(key, default)), \
                 patch.object(self.module, 'snapshot_database', side_effect=lambda path: path.write_bytes(b'test db')), \
                 patch('urllib.request.urlopen', side_effect=urlopen), \
                 patch.object(self.module, '_cleanup_github_backups', return_value={'ok': True, 'deleted': 0}) as cleanup:
                self.module._do_github_backup()
                self.assertEqual(cleanup.call_count, 0 if fail_latest else 1)

    def test_overlapping_operations_are_rejected(self):
        self.module._github_backup_operation_lock.acquire()
        try:
            self.assertFalse(self.module._do_github_backup()['ok'])
        finally:
            self.module._github_backup_operation_lock.release()

    def test_ui_save_validation_and_cleanup_access(self):
        client = self.module.app.test_client()
        self.assertEqual(client.post('/api/github-backup/cleanup', json={}).status_code, 401)
        with client.session_transaction() as session:
            session['user_id'] = 1
            session['_csrf_token'] = 'test-csrf'
        self.assertEqual(client.post('/api/github-backup/cleanup', json={}).status_code, 400)
        page = client.get('/settings')
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'Keep GitHub Backups For', page.data)
        for days, expected in [('14', '14'), ('invalid', '14')]:
            client.post('/settings/github-backup/save', data={
                'csrf_token': 'test-csrf', 'github_backup_retention_days': days,
                'github_backup_interval_hours': '0.5',
            })
            with self.module.app.app_context():
                self.assertEqual(self.module.get_setting('github_backup_retention_days'), expected)
        with patch.object(self.module, '_cleanup_github_backups', return_value={'ok': True, 'deleted': 2}):
            response = client.post('/api/github-backup/cleanup', json={}, headers={'X-CSRF-Token': 'test-csrf'})
            self.assertEqual(response.get_json()['deleted'], 2)


if __name__ == '__main__':
    unittest.main()
