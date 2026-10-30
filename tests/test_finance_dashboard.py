"""Money on the dashboard: the admin's own exchange rate, clients split into held and owed,
and auto commissions counting as company profit."""
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
TOKEN = 'ffs_finance_test'


class FinanceDashboardTests(unittest.TestCase):
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
        db = self.db = self.module.get_db()
        for table in ('statement_entries', 'clients', 'client_orders', 'expense_entries', 'expense_accounts', 'api_tokens'):
            db.execute(f"delete from {table}")
        db.execute("delete from app_settings where key like 'fx_%'")
        db.execute("delete from users where username = 'staff'")
        db.execute("update users set must_change_password = 0")
        db.execute("insert into users(username, password_hash, role, created_at) values ('staff', 'x', 'user', '')")
        db.execute(
            "insert into api_tokens(name, token_hash, token_prefix, access_level, created_at) values ('t', ?, 'ffs_fi', 'full_control', '2026')",
            (sha256(TOKEN.encode()).hexdigest(),),
        )
        self.company_id = db.execute(
            "insert into expense_accounts(name, enabled_currencies) values ('Firefly Trading', 'CNY')"
        ).lastrowid
        db.commit()
        self.admin = self.login('boss')
        self.staff = self.login('staff')

    def login(self, username):
        user = self.db.execute("select id, session_version from users where username = ?", (username,)).fetchone()
        client = self.module.app.test_client()
        with client.session_transaction() as sess:
            sess['user_id'], sess['sv'], sess['_csrf_token'] = user['id'], user['session_version'], 'csrf'
        return client

    def post(self, who, path, **data):
        return who.post(path, data={'csrf_token': 'csrf', **data})

    def new_client(self, name, *entries):
        client_id = self.db.execute("insert into clients(name) values (?)", (name,)).lastrowid
        for currency, direction, amount in entries:
            self.db.execute(
                "insert into statement_entries(client_id, source_no, entry_date, description, currency, direction,"
                " amount, kind, category_hint) values (?, 0, '2026-10-01', 'x', ?, ?, ?, 'movement', 'uncategorized')",
                (client_id, currency, direction, amount),
            )
        self.db.commit()
        return client_id

    def setting(self, key, value):
        self.module.set_setting(key, value)

    # -- Exchange rate ----------------------------------------------------------

    def test_admin_types_own_rate_on_dashboard_and_it_is_used(self):
        self.setting('fx_live_rate', '7.1')
        response = self.post(self.admin, '/fx-rate/manual', fx_manual_rate='7.25')
        self.assertEqual(response.status_code, 302)
        summary = self.module.exchange_rate_summary()
        self.assertEqual(summary['manual_rate'], 7.25)
        self.assertEqual(summary['rate_in_use'], 'manual')
        self.assertEqual(summary['display_rate'], 7.25)
        self.assertTrue(summary['manual_rate_date'])

        page = self.admin.get('/').get_data(as_text=True)
        self.assertIn('My rate', page)
        self.assertIn('7.2500', page)
        self.assertNotIn('Average', page)

    def test_only_admins_can_set_the_rate_and_typos_are_refused(self):
        self.post(self.staff, '/fx-rate/manual', fx_manual_rate='7.3')
        self.assertIsNone(self.module.exchange_rate_summary()['manual_rate'])
        for bad in ('abc', '0', '-7', '5000', 'inf'):
            self.post(self.admin, '/fx-rate/manual', fx_manual_rate=bad)
            self.assertIsNone(self.module.exchange_rate_summary()['manual_rate'], bad)

    def test_rate_falls_back_when_the_chosen_one_is_not_set(self):
        summary = self.module.exchange_rate_summary()
        self.assertIsNone(summary['display_rate'])
        self.setting('fx_live_rate', '7.1')
        self.setting('fx_rate_source', 'manual')
        self.assertEqual(self.module.exchange_rate_summary()['rate_in_use'], 'live')
        self.setting('fx_manual_rate', '7.3')
        self.setting('fx_rate_source', 'live')
        self.assertEqual(self.module.exchange_rate_summary()['display_rate'], 7.1)
        # "average" from older versions now means the typed rate.
        self.setting('fx_rate_source', 'average')
        self.assertEqual(self.module.exchange_rate_summary()['display_rate'], 7.3)

    def test_client_page_approx_total_uses_own_rate(self):
        client_id = self.new_client('Rate Client', ('USD', 'IN', 100), ('CNY', 'OUT', 50))
        self.setting('fx_live_rate', '7.0')
        self.post(self.admin, '/fx-rate/manual', fx_manual_rate='7.5')
        page = self.admin.get(f'/clients/{client_id}').get_data(as_text=True)
        self.assertIn('¥700.00', page)  # 100 x 7.5 - 50

    def test_settings_saves_rate_and_source_but_keeps_date_when_unchanged(self):
        self.post(self.admin, '/settings', section='exchange-rate', fx_rate_source='manual', fx_manual_rate='7.2')
        first_date = self.module.get_setting('fx_manual_rate_updated_at')
        self.assertEqual(self.module.exchange_rate_summary()['manual_rate'], 7.2)
        self.setting('fx_manual_rate_updated_at', '2026-01-01T00:00:00Z')
        self.post(self.admin, '/settings', section='exchange-rate', fx_rate_source='live', fx_manual_rate='7.2000')
        self.assertTrue(first_date)
        self.assertEqual(self.module.get_setting('fx_manual_rate_updated_at'), '2026-01-01T00:00:00Z')
        self.assertEqual(self.module.fx_rate_source(), 'live')

    # -- Clients held / owed ----------------------------------------------------

    def test_dashboard_keeps_held_and_owed_apart(self):
        self.new_client('Holds money', ('USD', 'IN', 50000))
        self.new_client('Owes money', ('USD', 'IN', 1000), ('USD', 'OUT', 31000))
        # +$1,000 and -¥7,000 at 7.0 is all square: on neither side.
        self.new_client('Square', ('USD', 'IN', 1000), ('CNY', 'OUT', 7000))
        self.setting('fx_manual_rate', '7.0')
        self.setting('fx_rate_source', 'manual')

        split = self.module.client_balance_split(self.module.client_list(), 7.0)
        self.assertEqual(split['held'], {'usd': 50000.0, 'cny': 0.0, 'count': 1, 'rmb': 350000.0})
        self.assertEqual(split['owed'], {'usd': -30000.0, 'cny': 0.0, 'count': 1, 'rmb': -210000.0})

        page = self.admin.get('/').get_data(as_text=True)
        self.assertIn('You hold for clients', page)
        self.assertIn('Clients owe you', page)
        self.assertIn('¥350,000.00', page)
        self.assertIn('¥210,000.00', page)

        body = self.admin.get('/api/v1/dashboard', headers={'Authorization': f'Bearer {TOKEN}'}).get_json()
        self.assertEqual(body['client_split']['owed']['usd'], -30000.0)
        self.assertEqual(body['company_status']['receivable_usd'], 30000.0)

    def test_without_a_rate_each_currency_is_sorted_on_its_own(self):
        self.new_client('Mixed', ('USD', 'IN', 1000), ('CNY', 'OUT', 7000))
        split = self.module.client_balance_split(self.module.client_list(), None)
        self.assertEqual((split['held']['usd'], split['held']['count']), (1000.0, 1))
        self.assertEqual((split['owed']['cny'], split['owed']['count']), (-7000.0, 1))
        self.assertIsNone(split['held']['rmb'])

    # -- Auto commission as company profit --------------------------------------

    def factory_payment(self, currency='CNY'):
        client_id = self.new_client('Commission Client', (currency, 'IN', 50000), (currency, 'OUT', 10000))
        entry_id = self.db.execute(
            "select id from statement_entries where client_id = ? and direction = 'OUT'", (client_id,)
        ).fetchone()[0]
        return client_id, entry_id

    def commission_row(self, source_id):
        return self.db.execute("select * from statement_entries where commission_source_entry_id = ?", (source_id,)).fetchone()

    def profit_rows(self):
        return self.db.execute("select * from expense_entries where account_id = ?", (self.company_id,)).fetchall()

    def test_auto_commission_form_adds_to_company_profit(self):
        client_id, source_id = self.factory_payment()
        page = self.admin.get(f'/clients/{client_id}').get_data(as_text=True)
        self.assertIn('name="add_to_company_profit" value="1" checked', page)

        self.post(self.staff, f'/clients/{client_id}/commission', source_entry_id=source_id, commission_date='2026-10-02',
                  percentage='2.5', add_to_company_profit='1', profit_expense_account_id=self.company_id)
        commission = self.commission_row(source_id)
        self.assertEqual(commission['amount'], 250.0)
        self.assertEqual(commission['profit_expense_account_id'], self.company_id)
        [profit] = self.profit_rows()
        self.assertEqual((profit['direction'], profit['amount'], profit['currency']), ('IN', 250.0, 'CNY'))

        # Undo takes the profit away with the commission.
        event_id = self.db.execute("select id from statement_entry_events order by id desc limit 1").fetchone()[0]
        self.post(self.staff, f'/clients/{client_id}/undo', event_id=event_id)
        self.assertIsNone(self.commission_row(source_id))
        self.assertEqual(self.profit_rows(), [])

    def test_unticked_box_leaves_profit_out(self):
        client_id, source_id = self.factory_payment()
        self.post(self.staff, f'/clients/{client_id}/commission', source_entry_id=source_id, commission_date='2026-10-02',
                  percentage='2', profit_expense_account_id=self.company_id)
        self.assertIsNone(self.commission_row(source_id)['profit_expense_account_id'])
        self.assertEqual(self.profit_rows(), [])

    def test_account_without_the_currency_still_makes_the_commission(self):
        client_id, source_id = self.factory_payment('USD')
        response = self.post(self.staff, f'/clients/{client_id}/commission', source_entry_id=source_id,
                             commission_date='2026-10-02', percentage='2', add_to_company_profit='1',
                             profit_expense_account_id=self.company_id)
        self.assertEqual(response.status_code, 302)
        commission = self.commission_row(source_id)
        self.assertEqual(commission['amount'], 200.0)
        self.assertIsNone(commission['profit_expense_account_id'])
        with self.staff.session_transaction() as sess:
            messages = ' '.join(text for _, text in sess.get('_flashes', []))
        self.assertIn('not added to company profit', messages)

    def test_api_commission_counts_as_profit_unless_told_not_to(self):
        headers = {'Authorization': f'Bearer {TOKEN}'}
        _, source_id = self.factory_payment()
        body = self.admin.post(f'/api/v1/entries/{source_id}/commission', json={'percentage': 3}, headers=headers).get_json()
        self.assertTrue(body['added_to_company_profit'])
        self.assertEqual(len(self.profit_rows()), 1)

        self.db.execute("delete from clients")
        self.db.commit()
        _, source_id = self.factory_payment()
        body = self.admin.post(f'/api/v1/entries/{source_id}/commission',
                               json={'percentage': 3, 'add_to_company_profit': False}, headers=headers).get_json()
        self.assertFalse(body['added_to_company_profit'])
        self.assertIsNone(self.commission_row(source_id)['profit_expense_account_id'])

    def test_api_commission_in_a_currency_the_account_lacks(self):
        headers = {'Authorization': f'Bearer {TOKEN}'}
        _, source_id = self.factory_payment('USD')
        refused = self.admin.post(f'/api/v1/entries/{source_id}/commission',
                                  json={'percentage': 3, 'add_to_company_profit': True}, headers=headers)
        self.assertEqual(refused.status_code, 400)
        self.assertIsNone(self.commission_row(source_id))
        made = self.admin.post(f'/api/v1/entries/{source_id}/commission', json={'percentage': 3}, headers=headers)
        self.assertEqual(made.status_code, 201)
        self.assertFalse(made.get_json()['added_to_company_profit'])
        self.assertIn('company_profit_note', made.get_json())


if __name__ == '__main__':
    unittest.main()
