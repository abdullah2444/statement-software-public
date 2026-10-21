"""Only one server does cloud backups; command-line password reset; installer sets up the updater."""
import importlib
import importlib.util
import os
from pathlib import Path
from contextlib import closing
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
CLOUD = {
    "cloud_backup_region": "us-west-004", "cloud_backup_bucket": "test-bucket-123",
    "cloud_backup_prefix": "statement-software", "cloud_backup_key_id": "0040000000000000000000001",
    "cloud_backup_app_key": "K004FAKEAPPLICATIONKEYVALUE12345",
    "cloud_backup_recovery_key": "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF-0000-1111",
}


class OneBackupServerTests(unittest.TestCase):
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
        sys.modules.pop('update_helper', None)
        cls.tmp.cleanup()

    def setUp(self):
        env = patch.dict(os.environ, self.env)
        env.start()
        self.addCleanup(env.stop)
        self.ctx = self.module.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        db = self.module.get_db()
        db.execute("delete from app_settings where key like 'cloud_backup_%'")
        db.execute("update users set must_change_password = 0")
        db.commit()

    def connect_here(self):
        for key, value in CLOUD.items():
            self.module.set_setting(key, value)
        self.module.set_setting("cloud_backup_instance", self.module.this_instance_id())

    # --- Which server backs up ---------------------------------------------------------

    def test_existing_server_keeps_backing_up_after_upgrade(self):
        m = self.module
        db = m.get_db()
        for key, value in CLOUD.items():
            db.execute("insert or replace into app_settings(key, value) values (?, ?)", (key, value))
        db.execute("delete from app_settings where key = ?", (m.CLOUD_OWNER_MARKER,))
        db.commit()
        m.settle_cloud_backup_owner(db)
        db.commit()
        self.assertTrue(m.cloud_backup_active())

    def test_fresh_install_is_not_the_backup_server(self):
        m = self.module
        db = m.get_db()
        db.execute("delete from app_settings where key = ?", (m.CLOUD_OWNER_MARKER,))
        db.commit()
        m.settle_cloud_backup_owner(db)
        db.commit()
        self.assertEqual(m.get_setting("cloud_backup_instance", ""), "")

    def test_restoring_another_servers_backup_pauses_cloud_backup(self):
        m = self.module
        self.connect_here()
        candidate = Path(self.tmp.name) / "from-other-server.db"
        m.snapshot_database(candidate)
        with closing(sqlite3.connect(candidate)) as other, other:
            other.execute("update app_settings set value = 'abcdefabcdef' where key = 'cloud_backup_instance'")
        m.activate_restore_candidate(candidate, "restore-test")
        self.assertTrue(m.cloud_backup_configured())
        self.assertFalse(m.cloud_backup_active())
        self.assertFalse(m.cloud_backup_due())
        self.assertEqual(m.get_setting("cloud_backup_paused_reason"), "restored")
        self.assertIn("paused", m.cloud_backup_alert())

    def test_restoring_an_old_backup_of_this_server_keeps_backing_up(self):
        m = self.module
        self.connect_here()
        candidate = Path(self.tmp.name) / "older-same-server.db"
        m.snapshot_database(candidate)
        with closing(sqlite3.connect(candidate)) as old, old:  # made before servers had ids
            old.execute("delete from app_settings where key in ('cloud_backup_instance', ?)", (m.CLOUD_OWNER_MARKER,))
        m.activate_restore_candidate(candidate, "restore-test")
        self.assertTrue(m.cloud_backup_active())

    def test_restoring_an_old_backup_on_a_new_machine_pauses(self):
        m = self.module
        candidate = Path(self.tmp.name) / "old-other.db"
        for key, value in CLOUD.items():
            m.set_setting(key, value)
        m.snapshot_database(candidate)
        with closing(sqlite3.connect(candidate)) as old, old:
            old.execute("delete from app_settings where key in ('cloud_backup_instance', ?)", (m.CLOUD_OWNER_MARKER,))
        db = m.get_db()
        db.execute("delete from app_settings where key like 'cloud_backup_%'")  # the new machine had no cloud set up
        db.commit()
        m.activate_restore_candidate(candidate, "restore-test")
        self.assertTrue(m.cloud_backup_configured())
        self.assertFalse(m.cloud_backup_active())
        # Restarting the app must not quietly make it the backup server.
        m.init_db()
        self.assertFalse(m.cloud_backup_active())

    def test_backup_now_is_refused_while_paused(self):
        m = self.module
        for key, value in CLOUD.items():
            m.set_setting(key, value)
        m.set_setting("cloud_backup_instance", "")
        result = m.run_cloud_backup()
        self.assertFalse(result["ok"])
        self.assertIn("paused", result["error"])

    def test_a_server_stops_when_another_takes_over(self):
        m = self.module
        self.connect_here()
        m.set_setting("cloud_backup_last_success", "2026-10-20T10:00:00Z")
        m.set_setting("cloud_backup_owner_since", "2026-10-01T00:00:00Z")

        class FakeRestic:
            def __init__(self, rows):
                self.rows = rows

            def snapshots(self):
                return self.rows

        mine = {"time": "2026-10-20T10:00:00.5Z", "tags": ["statement-software", f"instance-{m.this_instance_id()}"]}
        theirs_old = {"time": "2026-10-20T09:00:00Z", "tags": ["statement-software", "instance-abcdefabcdef"]}
        theirs_new = {"time": "2026-10-20T11:00:00Z", "tags": ["statement-software", "instance-abcdefabcdef"]}
        legacy = {"time": "2026-10-20T12:00:00Z", "tags": ["statement-software"]}
        self.assertIsNone(m._cloud_taken_over_by(FakeRestic([])))
        self.assertIsNone(m._cloud_taken_over_by(FakeRestic([mine, theirs_old])))
        self.assertIsNone(m._cloud_taken_over_by(FakeRestic([mine, legacy])))
        self.assertEqual(m._cloud_taken_over_by(FakeRestic([mine, theirs_new]))["instance"], "abcdefabcdef")
        # Right after this server takes over, the other server's older backups don't count.
        m.set_setting("cloud_backup_owner_since", "2026-10-20T11:30:00Z")
        self.assertIsNone(m._cloud_taken_over_by(FakeRestic([mine, theirs_new])))

    def test_only_a_super_admin_can_take_over(self):
        m = self.module
        for key, value in CLOUD.items():
            m.set_setting(key, value)
        m.set_setting("cloud_backup_instance", "")
        db = m.get_db()
        db.execute("insert or ignore into users(username, password_hash, role, is_active, must_change_password) values ('office', 'x', 'admin', 1, 0)")
        db.commit()
        ids = {r['username']: (r['id'], r['session_version']) for r in db.execute("select username, id, session_version from users")}

        def web(name):
            client = m.app.test_client()
            with client.session_transaction() as sess:
                sess['user_id'], sess['sv'], sess['_csrf_token'] = ids[name][0], ids[name][1], 'csrf'
            return client

        self.assertEqual(web('office').post('/settings/cloud-backup/take-over', data={'csrf_token': 'csrf'}).status_code, 403)
        page = web('boss').get('/settings').get_data(as_text=True)
        self.assertIn('Use this server for cloud backups', page)
        self.assertNotIn('id="cloud-run-btn"', page)
        run = web('boss').post('/api/cloud-backup/run', data={'csrf_token': 'csrf'}, headers={'X-CSRF-Token': 'csrf'})
        self.assertEqual(run.status_code, 409)
        web('boss').post('/settings/cloud-backup/take-over', data={'csrf_token': 'csrf'})
        self.assertTrue(m.cloud_backup_active())
        self.assertIn('id="cloud-run-btn"', web('boss').get('/settings').get_data(as_text=True))

    # --- Password reset from the command line ------------------------------------------

    def test_reset_password_makes_an_active_super_admin_and_clears_login_pause(self):
        m = self.module
        with patch.dict(os.environ, {**self.env, "RESET_ADMIN_PASSWORD": "fresh-pass-2026"}):
            helper = importlib.import_module("update_helper")
            db = m.get_db()
            db.execute("update users set role = 'admin', is_active = 0, must_change_password = 1 where username = 'boss'")
            db.commit()
            with m.app.test_request_context(environ_base={"REMOTE_ADDR": "203.0.113.9"}):
                for _ in range(m.LOGIN_FAILURE_LIMIT):
                    m.record_login_failure("boss")
                self.assertTrue(m.login_blocked("boss"))
                result = helper.reset_password("boss")
                self.assertFalse(result["created"])
                self.assertFalse(m.login_blocked("boss"), "the reset clears the wrong-password pause")
            user = m.get_db().execute("select * from users where username = 'boss'").fetchone()
            self.assertEqual((user['role'], user['is_active'], user['must_change_password']), ('super_admin', 1, 0))
            self.assertTrue(m.check_password_hash(user['password_hash'], "fresh-pass-2026"))
            with patch.dict(os.environ, {"RESET_ADMIN_PASSWORD": "short"}):
                with self.assertRaises(ValueError):
                    helper.reset_password("boss")

    # --- Installer --------------------------------------------------------------------

    def test_setup_script_installs_the_updater_and_keeps_its_settings(self):
        setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("setup_updater()", setup)
        self.assertIn('ok "Started $APP_NAME"\n    setup_updater', setup)
        self.assertIn("-f docker-compose.yml -f docker-compose.updater.yml", setup)
        self.assertIn("python update_helper.py reset-password", setup)
        self.assertNotIn("role = 'admin'", setup)


if __name__ == '__main__':
    unittest.main()
