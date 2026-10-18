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
                            ('/admin/users', 'Users'), ('/quick-submit', 'Quick Submit')):
            current = re.findall(r'aria-current="page">([^<]+)<', self.page(path))
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

    def test_settings_jump_buttons_point_at_real_sections(self):
        html = self.page('/settings')
        targets = re.findall(r'<a href="#([a-z-]+)">', html.split('class="settings-sections"', 1)[1].split('</nav>', 1)[0])
        self.assertGreaterEqual(len(targets), 7)
        for target in targets:
            self.assertIn(f'id="{target}"', html, target)

    def test_phones_can_zoom(self):
        for template in (ROOT / 'templates').glob('*.html'):
            text = template.read_text(encoding='utf-8')
            self.assertNotRegex(text, r'maximum-scale|user-scalable', template.name)

    def test_skip_link_is_on_every_menu_page(self):
        self.assertIn('class="skip-link"', self.page('/'))


if __name__ == '__main__':
    unittest.main()
