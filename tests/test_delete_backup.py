"""Deleting one backup stored on this server."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


class DeleteBackupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.env = {
            'DATABASE_PATH': str(base / 'test.db'), 'UPLOAD_DIR': str(base / 'uploads'),
            'BACKUP_DIR': str(base / 'backups'), 'SECRET_KEY': 'test-only',
            'INITIAL_ADMIN_USERNAME': 'boss', 'INITIAL_ADMIN_PASSWORD': 'boss-test-pass',
        }
        with patch.dict(os.environ, cls.env):
            spec = importlib.util.spec_from_file_location('app', ROOT / 'app.py')
            cls.module = importlib.util.module_from_spec(spec)
            sys.modules['app'] = cls.module
            spec.loader.exec_module(cls.module)
            cls.module.init_db()
        cls.module.app.config['TESTING'] = True

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop('app', None)
        cls.tmp.cleanup()

    def setUp(self):
        env = patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        m = self.module
        with m.app.app_context():
            db = m.get_db()
            db.execute("update users set must_change_password = 0")
            db.execute("insert or ignore into users(username, password_hash, role, is_active, must_change_password) values ('office', 'x', 'admin', 1, 0)")
            db.execute("insert or ignore into users(username, password_hash, role, is_active, must_change_password) values ('clerk', 'x', 'user', 1, 0)")
            db.commit()
            self.ids = {r['username']: (r['id'], r['session_version']) for r in db.execute("select username, id, session_version from users")}

    def web(self, name):
        client = self.module.app.test_client()
        with client.session_transaction() as sess:
            sess['user_id'], sess['sv'], sess['_csrf_token'] = self.ids[name][0], self.ids[name][1], 'csrf'
        return client

    def make_backup(self):
        with self.module.app.app_context():
            return self.module.create_database_backup("test")

    def test_admin_deletes_one_backup_and_keeps_the_others(self):
        keep, gone = self.make_backup(), self.make_backup()
        page = self.web('office').get('/settings').get_data(as_text=True)
        self.assertIn('>Delete</button>', page)
        resp = self.web('office').post('/settings/database/backups/delete', data={'csrf_token': 'csrf', 'backup_name': gone.name})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers['Location'].endswith('#backup-restore'))
        self.assertFalse(gone.exists())
        self.assertTrue(keep.exists())

    def test_only_backup_files_inside_the_backup_folder_can_be_deleted(self):
        outside = Path(self.tmp.name) / 'test.db'
        for name in ('../test.db', str(outside), 'missing.db', 'notes.txt'):
            resp = self.web('boss').post('/settings/database/backups/delete', data={'csrf_token': 'csrf', 'backup_name': name})
            self.assertEqual(resp.status_code, 404, name)
        self.assertTrue(outside.exists())

    def test_users_cannot_delete_backups(self):
        backup = self.make_backup()
        resp = self.web('clerk').post('/settings/database/backups/delete', data={'csrf_token': 'csrf', 'backup_name': backup.name})
        self.assertIn(resp.status_code, (302, 403))
        self.assertTrue(backup.exists())


if __name__ == '__main__':
    unittest.main()
