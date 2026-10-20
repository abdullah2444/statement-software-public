"""Regression tests for the 2026-10-18 readiness review (items 1-6 and spreadsheet injection)."""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import openpyxl


class SecurityFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        with patch.dict(os.environ, {
            'DATABASE_PATH': str(base / 'test.db'),
            'UPLOAD_DIR': str(base / 'uploads'),
            'BACKUP_DIR': str(base / 'backups'),
            'SECRET_KEY': 'test-only', 'INITIAL_ADMIN_USERNAME': 'admin',
            'INITIAL_ADMIN_PASSWORD': 'admin-test-pass',
        }):
            spec = importlib.util.spec_from_file_location('security_app', Path(__file__).parents[1] / 'app.py')
            cls.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.module)
            cls.module.init_db()
        m = cls.module
        m.app.config['TESTING'] = True
        with m.app.app_context():
            db = m.get_db()
            db.execute("update users set must_change_password = 0")
            cls.admin_id = db.execute("select id from users where username = 'admin'").fetchone()['id']
            db.execute(
                "insert into users(username, password_hash, role, is_active, must_change_password) values (?, ?, 'user', 1, 0)",
                ('alice', m.generate_password_hash('alice-pass-1')),
            )
            cls.user_id = db.execute("select id from users where username = 'alice'").fetchone()['id']
            db.execute("insert into clients(name) values ('Client A')")
            db.execute("insert into clients(name) values ('Client B')")
            cls.client_a, cls.client_b = [r['id'] for r in db.execute("select id from clients order by id")]
            db.commit()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        with self.module._login_failures_lock:
            self.module._login_failures.clear()

    def web(self, user_id):
        client = self.module.app.test_client()
        with self.module.app.app_context():
            version = self.db().execute("select session_version from users where id = ?", (user_id,)).fetchone()[0]
        with client.session_transaction() as sess:
            sess['user_id'] = user_id
            sess['sv'] = version
            sess['_csrf_token'] = 'csrf'
        return client

    def db(self):
        return self.module.get_db()

    # 1. Files outside the receipts folder can't be deleted -------------------

    def test_entry_delete_cannot_remove_files_outside_uploads(self):
        victim = Path(self.tmp.name) / 'victim.png'
        victim.write_bytes(b'keep me')
        with self.module.app.app_context():
            db = self.db()
            db.execute(
                "insert into statement_entries(client_id, source_no, entry_date, description, currency, direction, amount, kind, category_hint, image_path)"
                " values (?, 1, '2026-10-01', 'x', 'USD', 'IN', 1, 'movement', 'uncategorized', ?)",
                (self.client_a, '../victim.png'),
            )
            entry_id = db.execute("select max(id) from statement_entries").fetchone()[0]
            db.commit()
        self.web(self.user_id).post(f'/entries/{entry_id}/delete', data={'csrf_token': 'csrf'})
        self.assertTrue(victim.exists())
        for bad in ['../victim.png', '/etc/passwd', 'a/b.png', '..\\x.png', '.hidden.png', 'note.txt', '', None]:
            self.assertIsNone(self.module.safe_upload_name(bad), bad)
        self.assertEqual(self.module.safe_upload_name('abc123.png'), 'abc123.png')

    def test_add_entry_ignores_image_path_from_form(self):
        self.web(self.user_id).post(f'/clients/{self.client_a}/entries', data={
            'csrf_token': 'csrf', 'entry_date': '2026-10-01', 'description': 'from form', 'currency': 'USD',
            'direction': 'IN', 'amount': '5', 'kind': 'movement', 'category_hint': 'uncategorized', 'existing_image_path': '../victim.png',
        })
        with self.module.app.app_context():
            row = self.db().execute("select image_path from statement_entries where description = 'from form'").fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row['image_path'])

    # 2. Activity page can't run injected script ------------------------------

    def test_activity_detail_attribute_is_escaped(self):
        evil = '" onmouseover="alert(1)" x="'
        self.web(self.user_id).post('/clients/new', data={'csrf_token': 'csrf', 'name': evil})
        html = self.web(self.admin_id).get('/settings').get_data(as_text=True)
        self.assertNotIn('onmouseover="alert(1)"', html)

    def test_pages_send_security_headers(self):
        response = self.web(self.admin_id).get('/')
        self.assertIn("frame-ancestors 'self'", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    # 3. Secrets never reach the activity log ---------------------------------

    def test_secret_keys_are_hidden_in_activity_log(self):
        secret = 'sk-or-v1-' + 'A' * 40
        self.web(self.admin_id).post('/settings', data={
            'csrf_token': 'csrf', 'section': 'ai-settings', 'openrouter_api_key': secret,
        })
        with self.module.app.app_context():
            rows = [r[0] + (r[1] or '') for r in self.db().execute("select detail, undo_data from api_audit_log")]
        self.assertTrue(rows)
        self.assertFalse(any(secret in row for row in rows))
        secret = 'ghp_' + 'A' * 36
        clean = self.module._sanitize_audit_value('detail', {'note': 'key is ' + secret, 'must_change_password': True})
        self.assertEqual(clean, {'note': 'key is [redacted]', 'must_change_password': True})

    def test_old_activity_rows_are_cleaned_once(self):
        secret = 'ghp_' + 'B' * 36
        with self.module.app.app_context():
            db = self.db()
            db.execute(
                "insert into api_audit_log(action, resource_type, detail, created_at) values ('x', 'y', ?, '2026-01-01')",
                (json.dumps({'form': {'github_backup_token': secret}}),),
            )
            db.execute("delete from app_settings where key = ?", (self.module.AUDIT_REDACTION_MARKER,))
            db.commit()
            self.assertGreaterEqual(self.module.redact_existing_audit_log(db), 1)
            db.commit()
            self.assertFalse(db.execute("select count(*) from api_audit_log where detail like ?", (f'%{secret}%',)).fetchone()[0])
            self.assertEqual(self.module.redact_existing_audit_log(db), 0, 'runs only once')

    # 4. Sessions, login limits, CSRF on the API -------------------------------

    def login(self, username, password):
        client = self.module.app.test_client()
        client.get('/login')
        with client.session_transaction() as sess:
            sess['_csrf_token'] = 'csrf'
        response = client.post('/login', data={'csrf_token': 'csrf', 'username': username, 'password': password})
        return client, response

    def test_password_reset_logs_out_existing_sessions(self):
        phone, response = self.login('alice', 'alice-pass-1')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(phone.get('/').status_code, 200)
        self.web(self.admin_id).post(f'/admin/users/{self.user_id}/reset-password',
                                     data={'csrf_token': 'csrf', 'new_password': 'temporary-pass-9'})
        response = phone.get('/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers['Location'])
        with self.module.app.app_context():
            db = self.db()
            db.execute("update users set password_hash = ?, must_change_password = 0 where id = ?",
                       (self.module.generate_password_hash('alice-pass-1'), self.user_id))
            db.commit()

    def test_changing_own_password_keeps_this_session(self):
        laptop, _ = self.login('alice', 'alice-pass-1')
        phone, _ = self.login('alice', 'alice-pass-1')
        with laptop.session_transaction() as sess:
            sess['_csrf_token'] = 'csrf'
        laptop.post('/change-password', data={'csrf_token': 'csrf', 'current_password': 'alice-pass-1',
                                              'new_password': 'alice-pass-2', 'confirm_password': 'alice-pass-2'})
        self.assertEqual(laptop.get('/').status_code, 200)
        self.assertEqual(phone.get('/').status_code, 302)
        with self.module.app.app_context():
            db = self.db()
            db.execute("update users set password_hash = ? where id = ?",
                       (self.module.generate_password_hash('alice-pass-1'), self.user_id))
            db.commit()

    def test_too_many_wrong_passwords_pause_logins(self):
        for _ in range(self.module.LOGIN_FAILURE_LIMIT):
            self.login('alice', 'wrong')
        _, response = self.login('alice', 'alice-pass-1')
        self.assertEqual(response.status_code, 429)
        api = self.module.app.test_client().post('/api/v1/auth/login', json={'username': 'alice', 'password': 'alice-pass-1'})
        self.assertEqual(api.status_code, 429)

    def test_spoofed_forwarded_for_does_not_reset_the_limit(self):
        for i in range(self.module.LOGIN_FAILURE_LIMIT):
            client = self.module.app.test_client()
            with client.session_transaction() as sess:
                sess['_csrf_token'] = 'csrf'
            client.post('/login', data={'csrf_token': 'csrf', 'username': 'alice', 'password': 'wrong'},
                        headers={'X-Forwarded-For': f'10.0.0.{i}'})
        _, response = self.login('alice', 'alice-pass-1')
        self.assertEqual(response.status_code, 429)

    def test_short_new_passwords_are_refused(self):
        self.assertIsNotNone(self.module.password_problem('short'))
        self.assertIsNotNone(self.module.password_problem('alicealice', 'AliceAlice'))
        self.assertIsNone(self.module.password_problem('long-enough-1', 'alice'))

    def test_browser_session_api_writes_need_csrf_token(self):
        web = self.web(self.user_id)
        refused = web.post('/api/v1/clients', json={'name': 'No token'})
        self.assertEqual(refused.status_code, 403)
        allowed = web.post('/api/v1/clients', json={'name': 'With token'}, headers={'X-CSRF-Token': 'csrf'})
        self.assertEqual(allowed.status_code, 201)

    def test_api_token_writes_do_not_need_csrf(self):
        from hashlib import sha256
        raw = 'ffs_' + 'c' * 64
        with self.module.app.app_context():
            db = self.db()
            db.execute(
                "insert into api_tokens(name, token_hash, token_prefix, access_level, created_at) values (?, ?, ?, ?, ?)",
                ('tool', sha256(raw.encode()).hexdigest(), raw[:12], self.module.API_ACCESS_FULL, '2026-10-01'),
            )
            db.commit()
        response = self.module.app.test_client().post(
            '/api/v1/clients', json={'name': 'Via token'}, headers={'Authorization': f'Bearer {raw}'})
        self.assertEqual(response.status_code, 201)

    # 5. Exchanges in the same second don't mix rates --------------------------

    def test_exchanges_in_same_second_keep_their_own_rates(self):
        with patch.object(self.module, 'utc_now', return_value=self.module.utc_now()):
            ids = {self.module.make_transfer_group() for _ in range(5)}
        self.assertEqual(len(ids), 5)
        with self.module.app.app_context():
            db = self.db()
            for client_id, cny in ((self.client_a, 700), (self.client_b, 600)):
                for currency, direction, amount in (('USD', 'OUT', 100), ('CNY', 'IN', cny)):
                    db.execute(
                        "insert into statement_entries(client_id, source_no, entry_date, description, currency, direction, amount, kind, category_hint, transfer_group)"
                        " values (?, 0, '2026-10-02', 'fx', ?, ?, ?, 'fx_transfer', 'uncategorized', 'FX-SAME')",
                        (client_id, currency, direction, amount),
                    )
            self.module.sync_exchange_group(db, 'FX-SAME')
            db.commit()
            rates = dict(db.execute("select client_id, max(exchange_rate) from statement_entries where transfer_group = 'FX-SAME' group by client_id").fetchall())
        self.assertAlmostEqual(rates[self.client_a], 7.0)
        self.assertAlmostEqual(rates[self.client_b], 6.0)

    # 6. Invalid amounts are refused --------------------------------------------

    def test_infinity_and_nan_are_refused(self):
        for bad in ['Infinity', 'inf', 'NaN', '1e300', '-5', 'abc', True]:
            with self.assertRaises(self.module.InvalidAmount, msg=bad):
                self.module.money(bad)
        self.assertEqual(self.module.money('1,234.50'), 1234.5)
        self.assertEqual(self.module.money('-3', allow_negative=True), -3)
        web = self.web(self.user_id)
        response = web.post(f'/clients/{self.client_a}/entries', data={
            'csrf_token': 'csrf', 'entry_date': '2026-10-01', 'description': 'infinite', 'currency': 'USD',
            'direction': 'IN', 'amount': 'Infinity', 'kind': 'movement', 'category_hint': 'uncategorized'})
        self.assertEqual(response.status_code, 302)
        api = web.post(f'/api/v1/clients/{self.client_a}/entries', headers={'X-CSRF-Token': 'csrf'}, json={
            'entry_date': '2026-10-01', 'description': 'infinite', 'currency': 'USD', 'direction': 'IN', 'amount': 'NaN'})
        self.assertEqual(api.status_code, 400)
        with self.module.app.app_context():
            self.assertFalse(self.db().execute("select count(*) from statement_entries where description = 'infinite'").fetchone()[0])

    # Spreadsheet exports don't run user text as formulas ----------------------

    def test_exports_keep_formulas_as_text(self):
        with self.module.app.app_context():
            db = self.db()
            db.execute(
                "insert into statement_entries(client_id, source_no, entry_date, description, currency, direction, amount, kind, category_hint)"
                " values (?, 9, '2026-10-03', '=HYPERLINK(\"http://x\")', 'USD', 'IN', 1, 'movement', 'uncategorized')",
                (self.client_b,),
            )
            db.commit()
        web = self.web(self.admin_id)
        rows = list(csv.reader(io.StringIO(web.get(f'/clients/{self.client_b}/export.csv').get_data(as_text=True))))
        self.assertIn("'=HYPERLINK(\"http://x\")", [cell for row in rows for cell in row])
        book = openpyxl.load_workbook(io.BytesIO(web.get(f'/clients/{self.client_b}/export.xlsx').data))
        cells = [c for row in book.active.iter_rows() for c in row if c.value == '=HYPERLINK("http://x")']
        self.assertTrue(cells)
        self.assertTrue(all(c.data_type == 's' for c in cells))
        self.assertEqual(self.module.csv_unsafe("'=1+1"), '=1+1')
        self.assertEqual(self.module.csv_unsafe("it's"), "it's")


if __name__ == '__main__':
    unittest.main()
