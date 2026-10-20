import importlib.util
import io
import os
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class AiFeatureSwitchTests(unittest.TestCase):
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
            spec = importlib.util.spec_from_file_location('ai_switch_app', Path(__file__).parents[1] / 'app.py')
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)
            cls.module.init_db()
        cls.module.app.config['TESTING'] = True
        with cls.module.app.app_context():
            db = cls.module.get_db()
            db.execute("update users set must_change_password = 0")
            cls.admin_id = db.execute("select id from users where username = 'test'").fetchone()['id']
            cls.client_id = db.execute("insert into clients(name) values ('Switch Test')").lastrowid
            db.execute(
                "insert into api_tokens(name, token_hash, token_prefix, access_level, created_at) values (?, ?, ?, ?, ?)",
                ('t', sha256(b'ffs_test').hexdigest(), 'ffs_te', 'full_control', '2026-10-18T00:00:00Z'),
            )
            db.commit()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.web = self.module.app.test_client()
        with self.web.session_transaction() as sess:
            sess['user_id'] = self.admin_id
            sess['_csrf_token'] = 'csrf'
        self.set_switches(parse=True, extract=True)

    def set_switches(self, parse, extract):
        form = {'csrf_token': 'csrf', 'ai_features_form': '1'}
        if parse:
            form['ai_parse_image_enabled'] = '1'
        if extract:
            form['ai_extract_text_enabled'] = '1'
        self.assertEqual(self.web.post('/settings', data=form).status_code, 302)

    def post_image(self, path, **headers):
        data = {'image': (io.BytesIO(b'fake'), 'r.png'), 'csrf_token': 'csrf'}
        with patch.object(self.module, '_call_vision', return_value='{"amount": 5}'):
            return self.web.post(path, data=data, headers=headers, content_type='multipart/form-data')

    def api(self, path):
        return self.post_image(path, Authorization='Bearer ffs_test')

    def test_features_are_on_by_default(self):
        with self.module.app.app_context():
            for key in self.module.AI_FEATURES:
                self.assertTrue(self.module.ai_feature_enabled(key))
        page = self.web.get(f'/clients/{self.client_id}').get_data(as_text=True)
        self.assertIn('Parse with AI', page)
        self.assertEqual(self.post_image('/api/parse-image').status_code, 200)
        self.assertEqual(self.post_image('/api/extract-text').status_code, 200)
        self.assertEqual(self.api('/api/v1/parse-image').status_code, 200)
        self.assertEqual(self.api('/api/v1/extract-text').status_code, 200)

    def test_turning_off_receipt_reading_hides_button_and_refuses_routes(self):
        self.set_switches(parse=False, extract=True)
        page = self.web.get(f'/clients/{self.client_id}').get_data(as_text=True)
        self.assertNotIn('Parse with AI', page)
        self.assertEqual(self.post_image('/api/parse-image').status_code, 403)
        self.assertEqual(self.api('/api/v1/parse-image').status_code, 403)
        self.assertEqual(self.api('/api/v1/extract-text').status_code, 200)

    def test_turning_off_text_copying_refuses_routes(self):
        self.set_switches(parse=True, extract=False)
        self.assertEqual(self.post_image('/api/extract-text').status_code, 403)
        self.assertEqual(self.api('/api/v1/extract-text').status_code, 403)
        self.assertEqual(self.api('/api/v1/parse-image').status_code, 200)

    def test_settings_page_shows_switch_state_and_api_reports_it(self):
        self.set_switches(parse=False, extract=True)
        page = self.web.get('/settings').get_data(as_text=True)
        self.assertIn('AI features', page)
        self.assertRegex(page, r'name="ai_parse_image_enabled" value="1"\s+style')
        self.assertRegex(page, r'name="ai_extract_text_enabled" value="1"\s+checked')
        settings = self.web.get('/api/v1/settings', headers={'Authorization': 'Bearer ffs_test'}).get_json()
        self.assertEqual(settings['ai_features'], {'ai_parse_image_enabled': False, 'ai_extract_text_enabled': True})

    def test_saving_settings_without_switches_leaves_them_alone(self):
        self.set_switches(parse=False, extract=False)
        self.web.post('/settings', data={'csrf_token': 'csrf', 'fx_rate_source': 'live'})
        with self.module.app.app_context():
            self.assertFalse(self.module.ai_feature_enabled('ai_parse_image_enabled'))


if __name__ == '__main__':
    unittest.main()
