"""Top menu (name, highlighted page), Settings jump buttons and phone zoom."""
import importlib.util
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]


class NavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        with patch.dict(os.environ, {
            'DATABASE_PATH': str(base / 'test.db'), 'UPLOAD_DIR': str(base / 'uploads'),
            'BACKUP_DIR': str(base / 'backups'), 'SECRET_KEY': 'test-only',
            'INITIAL_ADMIN_USERNAME': 'admin', 'INITIAL_ADMIN_PASSWORD': 'admin-test-pass',
        }):
            spec = importlib.util.spec_from_file_location('nav_app', ROOT / 'app.py')
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)
            cls.module.init_db()
        cls.module.app.config['TESTING'] = True
        with cls.module.app.app_context():
            db = cls.module.get_db()
            db.execute("update users set must_change_password = 0")
            cls.admin_id = db.execute("select id from users where username = 'admin'").fetchone()['id']
            db.commit()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def page(self, path):
        web = self.module.app.test_client()
        with web.session_transaction() as sess:
            sess['user_id'] = self.admin_id
            sess['_csrf_token'] = 'csrf'
        response = web.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.get_data(as_text=True)

    def test_menu_highlights_the_current_page(self):
        for path, label in (('/', 'Dashboard'), ('/settings', 'Settings'), ('/expenses/', 'Expenses'),
                            ('/quick-submit', 'Quick Submit')):
            # Phone menu links start with an icon: <svg ...>...</svg><span>Label</span>
            current = re.findall(r'aria-current="page">(?:<svg.*?</svg><span>)?([^<]+)<', self.page(path), re.S)
            self.assertTrue(current, path)
            self.assertEqual(set(current), {label}, path)

    def test_menu_shows_the_login_screen_company_name(self):
        self.assertIn('class="nav-brand" href="/">Firefly Statements<', self.page('/'))
        with self.module.app.app_context():
            self.module.set_setting('login_title', 'Acme <Ltd>')
        try:
            html = self.page('/')
            self.assertIn('class="nav-brand" href="/">Acme &lt;Ltd&gt;<', html)
            self.assertIn('class="mobile-title" href="/">Acme &lt;Ltd&gt;<', html)
        finally:
            with self.module.app.app_context():
                self.module.set_setting('login_title', '')

    def test_settings_tabs_each_have_a_panel(self):
        html = self.page('/settings')
        tabs = re.findall(r'data-tab="([a-z]+)"', html)
        self.assertEqual(tabs, ['updates', 'general', 'ai', 'backups', 'users', 'tokens', 'activity', 'password'])
        for tab in tabs:
            self.assertIn(f'data-panel="{tab}"', html, tab)
        # Links elsewhere (banners, redirects) point at these sections; each must still exist.
        for section in ('software-updates', 'exchange-rate', 'login-screen', 'ai-settings', 'ai-features',
                        'backup-restore', 'cloud-backup', 'user-management', 'api-tokens', 'activity-log', 'change-password'):
            self.assertIn(f'id="{section}"', html, section)

    def test_each_settings_section_saves_on_its_own(self):
        html = self.page('/settings')
        for section in self.module.SETTINGS_FORM_SECTIONS:
            form = html.split(f'id="{section}"', 1)[1].split('</form>', 1)[0]
            self.assertIn(f'name="section" value="{section}"', form)
            self.assertIn('type="submit"', form)

    def test_saving_returns_to_the_same_section(self):
        web = self.module.app.test_client()
        with web.session_transaction() as sess:
            sess['user_id'] = self.admin_id
            sess['_csrf_token'] = 'csrf'
        response = web.post('/settings', data={'csrf_token': 'csrf', 'section': 'exchange-rate', 'fx_rate_source': 'live'})
        self.assertTrue(response.headers['Location'].endswith('/settings#exchange-rate'))
        response = web.post('/settings', data={'csrf_token': 'csrf', 'section': '"><script>', 'fx_rate_source': 'live'})
        self.assertTrue(response.headers['Location'].endswith('/settings'))

    def test_password_managers_are_not_offered_api_keys(self):
        html = self.page('/settings')
        password_boxes = re.findall(r'<input[^>]*type="password"[^>]*>', html)
        real_passwords = ('current_password', 'new_password', 'confirm_password', 'password')
        self.assertTrue(all(re.search(r'name="(%s)"' % '|'.join(real_passwords), box) for box in password_boxes), password_boxes)
        for name in ('openrouter_api_key', 'cloud_backup_app_key'):
            box = re.search(rf'<input[^>]*name="{name}"[^>]*>', html).group(0)
            self.assertIn('secret-input', box)
            self.assertIn('data-lpignore="true"', box)

    def test_github_backup_is_gone_and_its_settings_deleted(self):
        with self.module.app.app_context():
            db = self.module.get_db()
            for key in ('github_backup_repo', 'github_backup_token', 'github_backup_interval_hours',
                        'github_backup_retention_days', 'github_last_backup'):
                db.execute("insert or replace into app_settings(key, value) values (?, 'x')", (key,))
            db.execute("insert or replace into app_settings(key, value) values ('fx_rate_source', 'live')")
            db.commit()
            self.assertEqual(self.module.remove_github_backup_settings(db), 5)
            db.commit()
            left = [r[0] for r in db.execute("select key from app_settings where key like 'github%'")]
            self.assertEqual(left, [])
            self.assertIsNotNone(db.execute("select 1 from app_settings where key = 'fx_rate_source'").fetchone())
        rules = {rule.rule for rule in self.module.app.url_map.iter_rules()}
        self.assertFalse([r for r in rules if 'github' in r], 'no GitHub backup pages or API routes left')
        self.assertNotIn('github', self.page('/settings').replace('View on GitHub', '').lower())

    def test_backup_reasons_read_like_words(self):
        reason = self.module.backup_reason
        self.assertEqual(reason('statement-full-backup-20261020-180352-before-update-to-5.2.4.tar.gz'), 'Before update to 5.2.4')
        self.assertEqual(reason('statement-full-backup-20261018-100401-before-upload-statement-full-backup-latest-1-.tar.gz.tar.gz'), 'Before a restore')
        self.assertEqual(reason('statement-full-backup-20261018-103209-before-cloud-ef018521.tar.gz'), 'Before a cloud restore')
        self.assertEqual(reason('statement-full-backup-20261018-103209.tar.gz'), 'Backup')
        self.assertEqual(reason('20261018T103209Z-before-restore-1a2b3c4d.db'), 'Before a restore')
        self.assertEqual(reason('my-own-file.db'), 'Backup')

    def test_phones_can_zoom(self):
        for template in (ROOT / 'templates').glob('*.html'):
            text = template.read_text(encoding='utf-8')
            self.assertNotRegex(text, r'maximum-scale|user-scalable', template.name)

    def test_skip_link_is_on_every_menu_page(self):
        self.assertIn('class="skip-link"', self.page('/'))


if __name__ == '__main__':
    unittest.main()
