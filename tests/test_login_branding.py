import importlib.util
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32


class LoginBrandingTests(unittest.TestCase):
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
            spec = importlib.util.spec_from_file_location('login_branding_app', Path(__file__).parents[1] / 'app.py')
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)
            cls.module.init_db()
        cls.module.app.config['TESTING'] = True
        with cls.module.app.app_context():
            db = cls.module.get_db()
            db.execute("update users set must_change_password = 0")
            cls.admin_id = db.execute("select id from users where username = 'test'").fetchone()['id']
            db.execute(
                "insert into users(username, password_hash, role, is_active, must_change_password) values ('viewer', 'x', 'viewer', 1, 0)"
            )
            cls.viewer_id = db.execute("select id from users where username = 'viewer'").fetchone()['id']
            db.commit()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.admin = self.client_as(self.admin_id)
        self.save()

    def client_as(self, user_id):
        web = self.module.app.test_client()
        with web.session_transaction() as sess:
            sess['user_id'] = user_id
            sess['_csrf_token'] = 'csrf'
        return web

    def save(self, web=None, logo=None, **fields):
        form = {'csrf_token': 'csrf', 'login_branding_form': '1', 'login_title': '', 'login_subtitle': ''}
        form.update(fields)
        if logo is not None:
            form['login_logo'] = (io.BytesIO(logo[1]), logo[0])
        if 'remove_login_logo' not in fields:
            form['remove_login_logo'] = '1'
        return (web or self.admin).post('/settings', data=form, content_type='multipart/form-data')

    def login_html(self):
        return self.module.app.test_client().get('/login').get_data(as_text=True)

    def test_defaults_when_nothing_set(self):
        html = self.login_html()
        self.assertIn('<h1>Firefly Statements</h1>', html)
        self.assertIn('Sign in to continue', html)
        self.assertNotIn('login-logo"', html)

    def test_custom_name_and_subtitle_show_on_login(self):
        self.assertEqual(self.save(login_title='Acme Ltd', login_subtitle='Welcome back').status_code, 302)
        html = self.login_html()
        self.assertIn('<h1>Acme Ltd</h1>', html)
        self.assertIn('<title>Login - Acme Ltd</title>', html)
        self.assertIn('Welcome back', html)

    def test_text_is_escaped_and_trimmed(self):
        self.save(login_title='<script>x</script>' + 'a' * 200)
        html = self.login_html()
        self.assertNotIn('<script>x</script>', html)
        with self.module.app.app_context():
            self.assertEqual(len(self.module.get_setting('login_title')), self.module.LOGIN_BRANDING_MAX_LENGTH)

    def test_logo_upload_shows_and_can_be_removed(self):
        self.save(logo=('logo.png', PNG), remove_login_logo='0')
        html = self.login_html()
        self.assertIn('class="login-logo"', html)
        anon = self.module.app.test_client()
        resp = anon.get('/login/logo')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, PNG)
        resp.close()
        self.save()
        self.assertNotIn('class="login-logo"', self.login_html())
        self.assertEqual(anon.get('/login/logo').status_code, 404)
        self.assertEqual(list(Path(self.tmp.name, 'uploads').glob('login-logo-*')), [])

    def test_bad_logo_is_rejected_and_nothing_saved(self):
        resp = self.save(logo=('evil.svg', b'<svg onload="alert(1)"/>'), login_title='Should Not Save', remove_login_logo='0')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('<h1>Firefly Statements</h1>', self.login_html())
        self.assertEqual(self.module.app.test_client().get('/login/logo').status_code, 404)

    def test_settings_stay_admin_only(self):
        viewer = self.client_as(self.viewer_id)
        resp = self.save(web=viewer, login_title='Hacked')
        self.assertNotEqual(resp.status_code, 200)
        self.assertIn('<h1>Firefly Statements</h1>', self.login_html())

    def test_other_settings_saves_leave_branding_alone(self):
        self.save(login_title='Acme Ltd')
        self.admin.post('/settings', data={'csrf_token': 'csrf', 'fx_rate_source': 'live'})
        self.assertIn('<h1>Acme Ltd</h1>', self.login_html())


if __name__ == '__main__':
    unittest.main()
