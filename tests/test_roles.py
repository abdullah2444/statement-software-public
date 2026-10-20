"""Super admin, admin and user: what each can see and do."""
import importlib.util
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


class RoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        with patch.dict(os.environ, {
            'DATABASE_PATH': str(base / 'test.db'), 'UPLOAD_DIR': str(base / 'uploads'),
            'BACKUP_DIR': str(base / 'backups'), 'SECRET_KEY': 'test-only',
            'INITIAL_ADMIN_USERNAME': 'boss', 'INITIAL_ADMIN_PASSWORD': 'boss-test-pass',
        }):
            spec = importlib.util.spec_from_file_location('roles_app', ROOT / 'app.py')
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)
            cls.module.init_db()
        cls.module.app.config['TESTING'] = True

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        m = self.module
        with m.app.app_context():
            db = m.get_db()
            db.execute("delete from users where username != 'boss'")
            db.execute("update users set must_change_password = 0, role = 'super_admin', is_active = 1")
            for name, role in (('office', 'admin'), ('clerk', 'user'), ('deputy', 'super_admin')):
                db.execute(
                    "insert into users(username, password_hash, role, is_active, must_change_password) values (?, ?, ?, 1, 0)",
                    (name, m.generate_password_hash('long-pass-123'), role),
                )
            db.commit()
            self.ids = {r['username']: r['id'] for r in db.execute("select id, username from users")}

    def web(self, username):
        client = self.module.app.test_client()
        with self.module.app.app_context():
            version = self.module.get_db().execute(
                "select session_version from users where username = ?", (username,)).fetchone()[0]
        with client.session_transaction() as sess:
            sess['user_id'] = self.ids[username]
            sess['sv'] = version
            sess['_csrf_token'] = 'csrf'
        return client

    def role_of(self, username):
        with self.module.app.app_context():
            row = self.module.get_db().execute("select role from users where username = ?", (username,)).fetchone()
        return row['role'] if row else None

    def tabs(self, username):
        return re.findall(r'data-tab="([a-z]+)"', self.web(username).get('/settings').get_data(as_text=True))

    # --- What each role sees -------------------------------------------------------

    def test_existing_admins_become_super_admins_once(self):
        m = self.module
        with m.app.app_context():
            db = m.get_db()
            db.execute("delete from app_settings where key = ?", (m.ROLES_MARKER,))
            db.execute("update users set role = 'admin' where username = 'boss'")
            db.commit()
            self.assertEqual(m.convert_admins_to_super_admins(db), 2)  # boss and office
            db.commit()
            self.assertEqual(self.role_of('boss'), 'super_admin')
            # Afterwards, new admins stay admins.
            db.execute("update users set role = 'admin' where username = 'office'")
            db.commit()
            self.assertEqual(m.convert_admins_to_super_admins(db), 0)
            self.assertEqual(self.role_of('office'), 'admin')

    def test_tabs_per_role(self):
        self.assertEqual(self.tabs('boss'), ['updates', 'general', 'ai', 'backups', 'users', 'tokens', 'activity', 'password'])
        self.assertEqual(self.tabs('office'), ['general', 'backups', 'users', 'activity', 'password'])
        self.assertEqual(self.tabs('clerk'), ['password'])

    def test_admin_sees_login_screen_and_backups_but_not_setup(self):
        html = self.web('office').get('/settings').get_data(as_text=True)
        self.assertIn('id="login-screen"', html)
        self.assertIn('id="backup-restore"', html)
        self.assertIn('id="user-management"', html)
        self.assertIn('id="activity-log"', html)
        for hidden in ('id="exchange-rate"', 'id="ai-settings"', 'id="software-updates"', 'id="api-tokens"',
                       'name="cloud_backup_app_key"', 'Turn off cloud backup', 'Where files are stored'):
            self.assertNotIn(hidden, html, hidden)

    # --- What each role may do ---------------------------------------------------------

    def test_admin_cannot_change_setup(self):
        office = self.web('office')
        for path in ('/settings/updates/check', '/settings/cloud-backup/save', '/settings/cloud-backup/disconnect',
                     '/settings/cloud-backup/reveal-key', '/admin/tokens/create', '/reload'):
            self.assertEqual(office.post(path, data={'csrf_token': 'csrf'}).status_code, 403, path)
        self.assertEqual(office.post('/settings', data={'csrf_token': 'csrf', 'section': 'exchange-rate',
                                                        'fx_rate_source': 'average'}).status_code, 403)
        sneaky = office.post('/settings', data={'csrf_token': 'csrf', 'section': 'login-screen',
                                                'login_branding_form': '1', 'openrouter_api_key': 'sk-or-x'})
        self.assertEqual(sneaky.status_code, 403)
        ok = office.post('/settings', data={'csrf_token': 'csrf', 'section': 'login-screen', 'login_branding_form': '1',
                                            'login_title': 'Office Co', 'login_subtitle': ''})
        self.assertEqual(ok.status_code, 302)

    def test_admin_can_back_up_and_restore(self):
        office = self.web('office')
        if os.name != 'nt':  # Windows can't delete the temporary download while the test still holds it
            self.assertEqual(office.get('/settings/backup/download').status_code, 200)
        self.assertNotEqual(office.post('/api/cloud-backup/run', data={'csrf_token': 'csrf'},
                                        headers={'X-CSRF-Token': 'csrf'}).status_code, 403)

    def test_user_sees_only_password(self):
        clerk = self.web('clerk')
        self.assertEqual(clerk.get('/admin/users').status_code, 403)
        self.assertEqual(clerk.post('/admin/users/add', data={'csrf_token': 'csrf', 'username': 'x', 'password': 'long-pass-123'}).status_code, 403)
        self.assertEqual(clerk.get('/settings/backup/download').status_code, 403)

    def test_admin_manages_users_but_not_super_admins(self):
        office = self.web('office')
        office.post('/admin/users/add', data={'csrf_token': 'csrf', 'username': 'temp', 'password': 'long-pass-123', 'role': 'admin'})
        self.assertEqual(self.role_of('temp'), 'admin')
        office.post('/admin/users/add', data={'csrf_token': 'csrf', 'username': 'boss2', 'password': 'long-pass-123', 'role': 'super_admin'})
        self.assertIsNone(self.role_of('boss2'))
        office.post(f"/admin/users/{self.ids['clerk']}/role", data={'csrf_token': 'csrf', 'role': 'super_admin'})
        self.assertEqual(self.role_of('clerk'), 'user')
        office.post(f"/admin/users/{self.ids['clerk']}/role", data={'csrf_token': 'csrf', 'role': 'admin'})
        self.assertEqual(self.role_of('clerk'), 'admin')
        for action, data in (('role', {'role': 'user'}), ('toggle', {}), ('delete', {}),
                             ('reset-password', {'new_password': 'another-pass-1'})):
            office.post(f"/admin/users/{self.ids['deputy']}/{action}", data={'csrf_token': 'csrf', **data})
        with self.module.app.app_context():
            deputy = self.module.get_db().execute("select * from users where username = 'deputy'").fetchone()
        self.assertEqual((deputy['role'], deputy['is_active'], deputy['must_change_password']), ('super_admin', 1, 0))
        html = office.get('/settings').get_data(as_text=True)
        self.assertNotIn('value="super_admin"', html, 'admins are not offered the super admin role')

    def test_super_admin_can_promote_but_never_remove_the_last_super_admin(self):
        boss = self.web('boss')
        boss.post(f"/admin/users/{self.ids['office']}/role", data={'csrf_token': 'csrf', 'role': 'super_admin'})
        self.assertEqual(self.role_of('office'), 'super_admin')
        boss.post(f"/admin/users/{self.ids['office']}/role", data={'csrf_token': 'csrf', 'role': 'admin'})
        boss.post(f"/admin/users/{self.ids['deputy']}/delete", data={'csrf_token': 'csrf'})
        self.assertIsNone(self.role_of('deputy'))
        # boss is now the only super admin; another super admin may not demote or remove them.
        with self.module.app.app_context():
            db = self.module.get_db()
            problem = self.module.user_change_problem(
                {'id': 0, 'role': 'super_admin'}, db.execute("select * from users where username = 'boss'").fetchone(),
                new_role='admin')
        self.assertIn('at least one active super admin', problem)

    def test_api_tokens_need_super_admin_but_full_tokens_keep_working(self):
        office = self.web('office')
        self.assertEqual(office.get('/api/v1/tokens').status_code, 403)
        self.assertEqual(office.get('/api/v1/users').status_code, 200)
        self.assertEqual(self.web('boss').get('/api/v1/tokens').status_code, 200)
        from hashlib import sha256
        raw = 'ffs_' + 'd' * 64
        with self.module.app.app_context():
            db = self.module.get_db()
            db.execute("insert into api_tokens(name, token_hash, token_prefix, access_level, created_at) values ('t', ?, 'ffs_d', 'full_control', '2026')",
                       (sha256(raw.encode()).hexdigest(),))
            db.commit()
        token_client = self.module.app.test_client()
        self.assertEqual(token_client.get('/api/v1/tokens', headers={'Authorization': f'Bearer {raw}'}).status_code, 200)
        made = token_client.post('/api/v1/users', headers={'Authorization': f'Bearer {raw}'},
                                 json={'username': 'viaapi', 'password': 'long-pass-123', 'role': 'super_admin'})
        self.assertEqual(made.status_code, 201)

    def test_menu_is_the_same_short_list_for_everyone(self):
        for username in ('boss', 'office', 'clerk'):
            html = self.web(username).get('/').get_data(as_text=True)
            nav = html.split('class="user-bar desktop-only"', 1)[1].split('</div>', 1)[0]
            links = re.findall(r'<a href="[^"]*"(?: aria-current="page")?>([A-Za-z ]+)</a>', nav)
            self.assertEqual(links, ['Dashboard', 'Expenses', 'Settings'], username)

    def test_password_tab_changes_password_and_returns(self):
        clerk = self.web('clerk')
        response = clerk.post('/change-password', data={'csrf_token': 'csrf', 'return_to': 'settings',
                                                        'current_password': 'long-pass-123',
                                                        'new_password': 'brand-new-pass-9', 'confirm_password': 'brand-new-pass-9'})
        self.assertTrue(response.headers['Location'].endswith('/settings#password'))
        with self.module.app.app_context():
            row = self.module.get_db().execute("select password_hash from users where username = 'clerk'").fetchone()
        self.assertTrue(self.module.check_password_hash(row['password_hash'], 'brand-new-pass-9'))


if __name__ == '__main__':
    unittest.main()
