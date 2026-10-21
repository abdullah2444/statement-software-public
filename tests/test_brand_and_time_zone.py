"""The company name from Settings is used everywhere; times follow the chosen time zone."""
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


class BrandAndTimeZoneTests(unittest.TestCase):
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
        self.ctx = self.module.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)
        db = self.module.get_db()
        db.execute("delete from app_settings where key in ('login_title', 'login_logo_file', 'time_zone')")
        db.execute("update users set must_change_password = 0")
        db.commit()
        user = db.execute("select id, session_version from users where username = 'boss'").fetchone()
        self.client = self.module.app.test_client()
        with self.client.session_transaction() as sess:
            sess['user_id'], sess['sv'], sess['_csrf_token'] = user['id'], user['session_version'], 'csrf'

    # --- Company name -----------------------------------------------------------------

    def test_pages_use_the_company_name(self):
        self.module.set_setting("login_title", "Horizon Logistics")
        for url in ('/', '/quick-submit', '/quick-submit/history', '/clients/new'):
            page = self.client.get(url).get_data(as_text=True)
            self.assertNotIn('Firefly', page, url)
        self.assertIn('<title>Horizon Logistics</title>', self.client.get('/').get_data(as_text=True))
        settings = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('<title>Settings - Horizon Logistics</title>', settings)
        # Only the hint naming the factory default is left.
        self.assertEqual(settings.count('Firefly'), 2)

    def test_pdfs_use_the_company_name_and_uploaded_logo(self):
        m = self.module
        self.assertIn('static', m.pdf_branding()['logo_url'])  # built-in logo while the name is unchanged
        m.set_setting("login_title", "Horizon Logistics")
        self.assertEqual(m.pdf_branding()['logo_url'], '')  # never the old company's logo
        logo = Path(self.env['UPLOAD_DIR']) / 'login-logo-test.png'
        logo.parent.mkdir(parents=True, exist_ok=True)
        logo.write_bytes(PNG)
        m.set_setting("login_logo_file", logo.name)
        brand = m.pdf_branding()
        self.assertEqual(brand['name'], 'Horizon Logistics')
        self.assertTrue(m.pdf_url_allowed(brand['logo_url']))
        other = logo.with_name('receipt.png')
        other.write_bytes(PNG)
        self.assertFalse(m.pdf_url_allowed(other.resolve().as_uri()), "other uploads stay off-limits")
        with m.app.test_request_context():
            html = m.render_template('pdf.html', brand=brand, client={'name': 'C'}, rows=[],
                                     generated_date='2026-10-21', final_usd=0, final_cny=0)
        self.assertIn('Horizon Logistics', html)
        self.assertNotIn('Firefly', html)

    def test_default_commission_account_follows_the_company_name(self):
        m = self.module
        m.set_setting("login_title", "Horizon Logistics")
        options = [{"id": 1, "name": "Firefly Trading"}, {"id": 2, "name": "Horizon Logistics"}]
        self.assertEqual(m.default_profit_expense_account_id(options), 2)
        self.assertEqual(m.default_profit_expense_account_id(options[:1]), 1)

    # --- Time zone --------------------------------------------------------------------

    def test_times_follow_the_chosen_time_zone(self):
        m = self.module
        self.assertEqual(m.app_time_zone_name(), "Asia/Shanghai")
        self.assertEqual(m.local_time("2026-10-21T06:50:00Z"), "2026-10-21 14:50")
        m.set_setting("time_zone", "Asia/Dubai")
        self.assertEqual(m.local_time("2026-10-21T06:50:00Z"), "2026-10-21 10:50")
        self.assertEqual(m.local_time("2026-10-21T14:50:00+08:00"), "2026-10-21 10:50")
        self.assertEqual(m.local_time("2026-10-20T10:00:00.123456789Z", seconds=True), "2026-10-20 14:00:00")
        self.assertEqual(m.local_time("2026-10-21 06:50:00"), "2026-10-21 10:50")
        self.assertEqual(m.local_time(""), "")
        self.assertEqual(m.local_now().utcoffset().total_seconds(), 4 * 3600)
        with patch.object(m, "utc_now", return_value=datetime(2026, 10, 21, 22, 0, tzinfo=timezone.utc)):
            self.assertEqual(m.local_today().isoformat(), "2026-10-22")
        m.set_setting("time_zone", "Not/AZone")
        self.assertEqual(m.app_time_zone_name(), "Asia/Shanghai")

    def test_super_admin_saves_the_time_zone(self):
        m = self.module
        page = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('id="time-zone"', page)
        resp = self.client.post('/settings', data={'csrf_token': 'csrf', 'section': 'time-zone', 'time_zone': 'Europe/Istanbul'})
        self.assertTrue(resp.headers['Location'].endswith('#time-zone'))
        self.assertEqual(m.get_setting("time_zone"), "Europe/Istanbul")
        self.client.post('/settings', data={'csrf_token': 'csrf', 'section': 'time-zone', 'time_zone': 'Mars/Base'})
        self.assertEqual(m.get_setting("time_zone"), "Europe/Istanbul")


if __name__ == '__main__':
    unittest.main()
